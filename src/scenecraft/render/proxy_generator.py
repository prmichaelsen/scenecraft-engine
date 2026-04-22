"""Low-resolution proxies for source video files.

NLE-standard trick: transcode every base-track source into a cheap-to-decode
lower-resolution copy (default 540p H.264), store alongside the project, and
route the preview compositor through the proxy instead of the original. Base-
frame decode cost drops ~4x at 540p vs 1080p, which is the pipeline
bottleneck identified in the M11→M12 performance audit.

Export / final-render paths bypass proxies (read originals for quality).

Two storage layouts depending on source duration:

    Single-file (short sources, duration < chunk_seconds):
        {project_dir}/proxies/{hash}.mp4

    Chunked (long sources, duration >= chunk_seconds):
        {project_dir}/proxies/{hash}/
            manifest.json
            chunk-000.mp4
            chunk-001.mp4
            ...

where `hash` is a truncated SHA-256 of `(absolute source path + source
mtime_ns)`. Changing the source — overwrite, timestamp bump, anything that
affects mtime — invalidates the proxy automatically because the key moves.

Public API:
    proxy_path_for(project_dir, source_path) -> Path | None
    proxy_exists(project_dir, source_path) -> bool
    generate_proxy(project_dir, source_path, target_height=540) -> Path | None
    chunked_proxy_dir_for(project_dir, source_path) -> Path | None
    chunked_proxy_manifest(project_dir, source_path) -> Manifest | None
    chunk_for_time(manifest, t) -> tuple[int, float] | None
    generate_chunked_proxy(project_dir, source_path, ...) -> Manifest | None
    ProxyCoordinator.instance()
        .ensure_proxy(project_dir, source_path, mode='single'|'chunked'|'auto')
            -> Future[Path | Manifest | None]
"""

from __future__ import annotations

import concurrent.futures
import hashlib
import json
import logging
import os
import shutil
import subprocess
import threading
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable, Literal


logger = logging.getLogger(__name__)


# ── Configuration ────────────────────────────────────────────────────────

DEFAULT_PROXY_HEIGHT = 540
"""Default vertical resolution for proxy transcodes (pixels).

540p is the sweet spot: 4x pixel reduction vs 1080p (25% the decode cost)
with quality still fine for preview compositing. Lower (360p) shows in the
`<video>` element; higher (720p) halves the decode-cost savings.
"""

DEFAULT_PROXY_CRF = 28
"""x264 CRF for proxy transcode. 28 is "visually acceptable for preview"
per the FFmpeg wiki's H.264 guide — not broadcast quality, but fine when
the proxy only feeds the preview pipeline."""

DEFAULT_PROXY_PRESET = "faster"
"""x264 preset for proxy transcode. `faster` balances transcode wall time
against file size; `veryfast`/`ultrafast` would speed transcode at the cost
of 40-80% larger proxy files (which matters because we decode them every
playback)."""

MAX_CONCURRENT_TRANSCODES = 2
"""How many ffmpeg transcodes the ProxyCoordinator runs in parallel.
More = faster catch-up on large projects, but each ffmpeg uses multiple
threads internally and also competes with the live preview render for CPU.
Two keeps headroom."""

DEFAULT_PROXY_CHUNK_SECONDS = 300.0
"""Default chunk duration for chunked proxies (seconds).

5 minutes balances file size (~30MB each at 540p) and chunk count (~29
chunks for a 2.4h source) — small enough that random-access seeks are
cheap per chunk, large enough that the chunk list stays short.
Keyframe-aligned chunk boundaries may deviate from exact multiples of this
value; manifest records the actual GOP-aligned durations."""

CHUNKED_MANIFEST_NAME = "manifest.json"
"""Sidecar JSON inside the per-source chunked proxy directory. Presence
of this file marks the chunked proxy as ready — the generator writes it
last, after all chunk files are in place, via an atomic dir rename."""

CHUNKED_MANIFEST_VERSION = 1
"""Bump on schema changes. Loader treats mismatched versions as stale."""


# ── Path / hash helpers ──────────────────────────────────────────────────


def _proxy_dir(project_dir: Path) -> Path:
    return project_dir / "proxies"


def _source_key(source_path: str) -> tuple[str, int] | None:
    """Return (abs_source_path, mtime_ns). None if the source is missing.

    mtime_ns is what makes the hash move when the source changes — edit the
    file, touch it, re-copy it, any of those bump mtime and the proxy is
    auto-invalidated.
    """
    try:
        p = Path(source_path).resolve()
        return (str(p), p.stat().st_mtime_ns)
    except OSError:
        return None


def _hash_for_source(source_path: str) -> str | None:
    key = _source_key(source_path)
    if key is None:
        return None
    abs_path, mtime_ns = key
    h = hashlib.sha256(f"{abs_path}:{mtime_ns}".encode()).hexdigest()
    return h[:24]  # 24 hex chars = 96 bits; collision-free for any realistic project


def proxy_path_for(project_dir: Path, source_path: str) -> Path | None:
    """Canonical proxy location for (project_dir, source_path).

    Returns None if the source file is missing. Does NOT check whether the
    proxy file itself exists — use `proxy_exists()` for that.
    """
    h = _hash_for_source(source_path)
    if h is None:
        return None
    return _proxy_dir(project_dir) / f"{h}.mp4"


def proxy_exists(project_dir: Path, source_path: str) -> bool:
    """True if a fresh proxy is present on disk for this source.

    "Fresh" means the proxy was generated against the source's current
    mtime. A stale proxy (mtime changed after the proxy was made) returns
    False even if an older proxy file still exists on disk — its hash no
    longer matches.
    """
    pp = proxy_path_for(project_dir, source_path)
    if pp is None:
        return False
    return pp.exists() and pp.stat().st_size > 0


# ── Synchronous proxy generation ─────────────────────────────────────────


def generate_proxy(
    project_dir: Path,
    source_path: str,
    target_height: int = DEFAULT_PROXY_HEIGHT,
    crf: int = DEFAULT_PROXY_CRF,
    preset: str = DEFAULT_PROXY_PRESET,
) -> Path | None:
    """Synchronously transcode `source_path` to a proxy under `project_dir`.

    Blocks until ffmpeg exits. Returns the proxy path on success, None on
    failure (missing source, ffmpeg not installed, transcode error).

    Idempotent: if a fresh proxy already exists, returns its path without
    re-transcoding.
    """
    pp = proxy_path_for(project_dir, source_path)
    if pp is None:
        logger.warning("proxy_generator: source missing %s", source_path)
        return None
    if pp.exists() and pp.stat().st_size > 0:
        return pp

    _proxy_dir(project_dir).mkdir(parents=True, exist_ok=True)
    # Transcode into a temp path, then rename — atomic from the
    # proxy_exists() observer's perspective.
    tmp = pp.with_suffix(".mp4.partial")

    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        str(Path(source_path).resolve()),
        "-vf",
        f"scale=-2:{target_height}",
        "-c:v",
        "libx264",
        "-preset",
        preset,
        "-crf",
        str(crf),
        "-pix_fmt",
        "yuv420p",
        "-an",  # preview doesn't need audio from video; audio-mixer handles audio
        # Force MP4 container: the .partial suffix hides the real extension
        # from ffmpeg's format autodetect, so we specify explicitly.
        "-f",
        "mp4",
        str(tmp),
    ]

    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=3600
        )
    except FileNotFoundError:
        logger.error("proxy_generator: ffmpeg not found on PATH")
        return None
    except subprocess.TimeoutExpired:
        logger.error("proxy_generator: ffmpeg timed out transcoding %s", source_path)
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        return None

    if result.returncode != 0:
        logger.error(
            "proxy_generator: ffmpeg failed (rc=%d) for %s: %s",
            result.returncode, source_path, (result.stderr or "")[:500],
        )
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        return None

    try:
        os.replace(tmp, pp)
    except OSError as e:
        logger.error("proxy_generator: rename failed for %s: %s", source_path, e)
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        return None

    logger.info("proxy_generator: generated %s → %s (%d bytes)",
                source_path, pp.name, pp.stat().st_size)
    return pp


# ── Chunked proxy model ──────────────────────────────────────────────────


@dataclass(frozen=True)
class Chunk:
    """A single proxy segment of `Manifest.chunks`.

    `start`/`end` are timestamps on the SOURCE's timeline — i.e. the
    absolute time within the original video that this chunk covers.
    `file` is the filename of the chunk (resolved relative to the
    manifest's directory; never absolute).
    """
    index: int
    file: str
    start: float
    end: float


@dataclass(frozen=True)
class Manifest:
    """Chunked-proxy manifest. One per source, sits at
    `{project_dir}/proxies/{hash}/manifest.json`.

    `source_mtime_ns` is redundant with `{hash}` (which already embeds
    mtime) but kept for defensive consistency checks on load.
    `total_seconds` is the ACTUAL source duration as reported by ffprobe,
    not the sum of chunk lengths — segment muxer may produce slightly
    rounded endpoints, so this is the source-of-truth span.
    """
    version: int
    source_path: str
    source_mtime_ns: int
    chunk_seconds: float
    total_seconds: float
    chunks: tuple[Chunk, ...]


def chunked_proxy_dir_for(project_dir: Path, source_path: str) -> Path | None:
    """Directory where this source's chunked proxy would live.

    Returns None if the source is missing. Does not check for existence
    on disk — use `chunked_proxy_manifest` for that.
    """
    h = _hash_for_source(source_path)
    if h is None:
        return None
    return _proxy_dir(project_dir) / h


def chunked_proxy_manifest(project_dir: Path, source_path: str) -> Manifest | None:
    """Load the chunked proxy manifest for this source, or None if absent/corrupt.

    Returns None when:
    - Source file is missing
    - Proxy directory doesn't exist
    - manifest.json is missing (generator still running or never ran)
    - JSON is malformed
    - version mismatch (treated as stale)
    - source_mtime_ns in manifest doesn't match current source mtime
      (extra paranoia — the hash already encodes mtime)
    """
    pd = chunked_proxy_dir_for(project_dir, source_path)
    if pd is None:
        return None
    mpath = pd / CHUNKED_MANIFEST_NAME
    if not mpath.exists():
        return None
    try:
        raw = json.loads(mpath.read_text())
    except (OSError, json.JSONDecodeError) as e:
        logger.warning("proxy_generator: corrupt manifest %s: %s", mpath, e)
        return None

    try:
        if int(raw.get("version", 0)) != CHUNKED_MANIFEST_VERSION:
            return None
        key = _source_key(source_path)
        if key is not None and int(raw.get("source_mtime_ns", -1)) != key[1]:
            return None
        chunks = tuple(
            Chunk(
                index=int(c["index"]),
                file=str(c["file"]),
                start=float(c["start"]),
                end=float(c["end"]),
            )
            for c in raw["chunks"]
        )
        return Manifest(
            version=int(raw["version"]),
            source_path=str(raw["source_path"]),
            source_mtime_ns=int(raw["source_mtime_ns"]),
            chunk_seconds=float(raw["chunk_seconds"]),
            total_seconds=float(raw["total_seconds"]),
            chunks=chunks,
        )
    except (KeyError, TypeError, ValueError) as e:
        logger.warning("proxy_generator: malformed manifest %s: %s", mpath, e)
        return None


def chunk_for_time(manifest: Manifest, t: float) -> tuple[int, float] | None:
    """Map a source-timeline time `t` to `(chunk_index, time_within_chunk)`.

    Returns None if `t` falls past the manifest's last chunk end. Times
    slightly before 0 clamp to chunk 0 at offset 0.0 — callers may pass
    small negatives due to rounding.

    Chunk lookup is linear — chunks are short (O(10s) per source) so
    binary search buys nothing and this keeps the implementation
    transparent.
    """
    if not manifest.chunks:
        return None
    if t < 0:
        t = 0.0
    for c in manifest.chunks:
        # Use [start, end) semantics except for the last chunk, which
        # is inclusive of its end (so t == total_seconds resolves to
        # the final chunk's last frame, not None).
        if c.start <= t < c.end:
            return (c.index, t - c.start)
    last = manifest.chunks[-1]
    if t <= last.end + 1e-6:
        return (last.index, max(0.0, t - last.start))
    return None


def _probe_source_duration(source_path: str) -> float | None:
    """Return source duration in seconds via ffprobe, or None on failure.

    ffprobe is part of the ffmpeg distribution; if ffmpeg is available,
    ffprobe almost always is too. Used to decide chunked-vs-single mode
    and to record `total_seconds` on the manifest.
    """
    try:
        result = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                str(Path(source_path).resolve()),
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        logger.warning("proxy_generator: ffprobe failed on %s: %s", source_path, e)
        return None
    if result.returncode != 0:
        return None
    try:
        return float(result.stdout.strip())
    except ValueError:
        return None


def _probe_chunk_duration(chunk_path: Path) -> float | None:
    """Return a chunk file's duration in seconds via ffprobe, or None."""
    try:
        result = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                str(chunk_path),
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    try:
        return float(result.stdout.strip())
    except ValueError:
        return None


def generate_chunked_proxy(
    project_dir: Path,
    source_path: str,
    target_height: int = DEFAULT_PROXY_HEIGHT,
    chunk_seconds: float = DEFAULT_PROXY_CHUNK_SECONDS,
    crf: int = DEFAULT_PROXY_CRF,
    preset: str = DEFAULT_PROXY_PRESET,
) -> Manifest | None:
    """Synchronously transcode `source_path` into a directory of chunk proxies.

    Uses ffmpeg's `segment` muxer to produce one 540p .mp4 per
    `chunk_seconds` of source; writes a manifest JSON last and atomically
    renames the tmp directory to the final location. "Ready" is defined
    as the manifest.json file being present.

    Returns the Manifest on success, None on failure. Idempotent: if a
    fresh chunked proxy already exists, returns its manifest without
    regenerating.

    Short-source fallback is the caller's concern — this function always
    produces chunks. Use `ProxyCoordinator.ensure_proxy(mode='chunked')`
    for the auto-fallback behavior.
    """
    pd = chunked_proxy_dir_for(project_dir, source_path)
    if pd is None:
        logger.warning("proxy_generator: source missing %s", source_path)
        return None

    # Fast path: already generated.
    existing = chunked_proxy_manifest(project_dir, source_path)
    if existing is not None:
        return existing

    key = _source_key(source_path)
    if key is None:
        return None
    abs_source, source_mtime_ns = key

    total_seconds = _probe_source_duration(abs_source)
    if total_seconds is None or total_seconds <= 0:
        logger.warning("proxy_generator: could not probe duration for %s", source_path)
        return None

    _proxy_dir(project_dir).mkdir(parents=True, exist_ok=True)

    # Transcode into a sibling `.partial` directory, then atomically
    # rename to the real location. Partial = no manifest file yet, so
    # `chunked_proxy_manifest` correctly reports "not ready".
    tmp_dir = pd.with_name(pd.name + ".partial")
    if tmp_dir.exists():
        try:
            shutil.rmtree(tmp_dir)
        except OSError as e:
            logger.error("proxy_generator: could not clear stale tmp %s: %s", tmp_dir, e)
            return None
    tmp_dir.mkdir(parents=True)

    segment_list = tmp_dir / "chunks.txt"
    segment_pattern = tmp_dir / "chunk-%03d.mp4"

    # Force GOP so the segment muxer actually cuts at the requested
    # chunk_seconds. The segment muxer only breaks on IDR frames; without
    # a GOP constraint, libx264 can place keyframes hundreds of frames
    # apart, yielding one giant "chunk" that ignores our boundary request.
    # We pin the GOP to chunk_seconds-worth of frames at a conservative
    # 60fps upper bound (= chunk_seconds * 60), matching ffmpeg's usual
    # keyframe cadence and letting segment boundaries land near the
    # requested time.
    #
    # `expr:gte(t,n_forced*chunk_seconds)` on `-force_key_frames` is the
    # canonical pattern: emit a keyframe at every multiple of
    # chunk_seconds regardless of fps. Combined with `-g` as a ceiling,
    # this gives us predictable cut points.
    force_keyframes_expr = f"expr:gte(t,n_forced*{chunk_seconds})"
    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        abs_source,
        "-vf",
        f"scale=-2:{target_height}",
        "-c:v",
        "libx264",
        "-preset",
        preset,
        "-crf",
        str(crf),
        "-pix_fmt",
        "yuv420p",
        "-force_key_frames",
        force_keyframes_expr,
        "-an",  # preview path ignores video-embedded audio; audio-mixer owns audio
        "-f",
        "segment",
        "-segment_time",
        str(chunk_seconds),
        "-reset_timestamps",
        "1",
        "-segment_list",
        str(segment_list),
        str(segment_pattern),
    ]

    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=7200
        )
    except FileNotFoundError:
        logger.error("proxy_generator: ffmpeg not found on PATH")
        _safe_rmtree(tmp_dir)
        return None
    except subprocess.TimeoutExpired:
        logger.error("proxy_generator: chunked transcode timed out for %s", source_path)
        _safe_rmtree(tmp_dir)
        return None

    if result.returncode != 0:
        logger.error(
            "proxy_generator: chunked ffmpeg failed (rc=%d) for %s: %s",
            result.returncode, source_path, (result.stderr or "")[:500],
        )
        _safe_rmtree(tmp_dir)
        return None

    # Parse segment_list — one filename per line in write order. ffmpeg's
    # `-segment_list` writes the basenames without path prefix.
    try:
        seg_names = [
            line.strip()
            for line in segment_list.read_text().splitlines()
            if line.strip()
        ]
    except OSError as e:
        logger.error("proxy_generator: segment list unreadable: %s", e)
        _safe_rmtree(tmp_dir)
        return None

    if not seg_names:
        logger.error("proxy_generator: no chunks produced for %s", source_path)
        _safe_rmtree(tmp_dir)
        return None

    # Probe each chunk for its actual duration and build chronological
    # boundaries. `-reset_timestamps 1` means each chunk starts at t=0
    # internally, so its ffprobe duration IS its span on the source
    # timeline (keyframe-aligned, possibly != chunk_seconds exactly).
    chunks: list[Chunk] = []
    running = 0.0
    for idx, name in enumerate(seg_names):
        cpath = tmp_dir / name
        if not cpath.exists() or cpath.stat().st_size == 0:
            logger.error("proxy_generator: chunk %s missing or empty", cpath)
            _safe_rmtree(tmp_dir)
            return None
        dur = _probe_chunk_duration(cpath)
        if dur is None or dur <= 0:
            logger.error("proxy_generator: bad chunk duration for %s", cpath)
            _safe_rmtree(tmp_dir)
            return None
        start = running
        end = running + dur
        # Clamp final chunk's end to the probed source duration — avoids
        # tiny drift when summed chunk durations disagree with source
        # duration by a frame or two.
        if idx == len(seg_names) - 1:
            end = max(end, total_seconds)
        chunks.append(Chunk(index=idx, file=name, start=start, end=end))
        running = end

    manifest = Manifest(
        version=CHUNKED_MANIFEST_VERSION,
        source_path=abs_source,
        source_mtime_ns=source_mtime_ns,
        chunk_seconds=chunk_seconds,
        total_seconds=total_seconds,
        chunks=tuple(chunks),
    )

    manifest_tmp = tmp_dir / CHUNKED_MANIFEST_NAME
    try:
        manifest_tmp.write_text(_manifest_to_json(manifest))
    except OSError as e:
        logger.error("proxy_generator: could not write manifest: %s", e)
        _safe_rmtree(tmp_dir)
        return None

    # Remove the now-redundant ffmpeg segment list before the rename —
    # keeps the final directory clean of transient scaffolding.
    try:
        segment_list.unlink(missing_ok=True)
    except OSError:
        pass

    # Atomic rename from .partial → final. If `pd` already exists from a
    # prior aborted generation, remove it first (the hash key embeds
    # mtime, so a stale `pd` at this path can only be leftover tmp junk).
    if pd.exists():
        _safe_rmtree(pd)
    try:
        os.replace(tmp_dir, pd)
    except OSError as e:
        logger.error("proxy_generator: chunked rename failed for %s: %s", source_path, e)
        _safe_rmtree(tmp_dir)
        return None

    logger.info(
        "proxy_generator: generated chunked proxy %s → %d chunks (%.1fs total)",
        source_path, len(chunks), total_seconds,
    )
    return manifest


def _manifest_to_json(manifest: Manifest) -> str:
    """Serialize a Manifest to pretty-printed JSON."""
    return json.dumps(
        {
            "version": manifest.version,
            "source_path": manifest.source_path,
            "source_mtime_ns": manifest.source_mtime_ns,
            "chunk_seconds": manifest.chunk_seconds,
            "total_seconds": manifest.total_seconds,
            "chunks": [asdict(c) for c in manifest.chunks],
        },
        indent=2,
    )


def _safe_rmtree(path: Path) -> None:
    """shutil.rmtree but never raises."""
    try:
        shutil.rmtree(path)
    except OSError:
        pass


# ── Background coordinator (singleton) ───────────────────────────────────


ProxyMode = Literal["auto", "single", "chunked"]


@dataclass
class _PendingEntry:
    future: concurrent.futures.Future
    subscribers: int = 1
    done_callbacks: list[Callable] = field(default_factory=list)


class ProxyCoordinator:
    """Process-global proxy generator. One background pool, deduped work.

    Clients call `ensure_proxy(project_dir, source_path)` and get a Future
    that resolves to the proxy Path (or None on failure). Multiple calls
    for the same (project, source) collapse to a single in-flight future.
    """

    _instance: "ProxyCoordinator | None" = None
    _class_lock = threading.Lock()

    @classmethod
    def instance(cls) -> "ProxyCoordinator":
        with cls._class_lock:
            if cls._instance is None:
                cls._instance = cls()
            return cls._instance

    def __init__(self, max_workers: int = MAX_CONCURRENT_TRANSCODES) -> None:
        self._pool = concurrent.futures.ThreadPoolExecutor(
            max_workers=max_workers,
            thread_name_prefix="proxy-gen",
        )
        self._pending: dict[str, _PendingEntry] = {}
        self._lock = threading.Lock()

    def ensure_proxy(
        self,
        project_dir: Path,
        source_path: str,
        target_height: int = DEFAULT_PROXY_HEIGHT,
        *,
        mode: ProxyMode = "auto",
        chunk_seconds: float = DEFAULT_PROXY_CHUNK_SECONDS,
    ) -> concurrent.futures.Future:
        """Return a Future that resolves to the proxy artifact.

        Result type depends on `mode`:
        - `single`: Future resolves to `Path | None` (single-file proxy)
        - `chunked`: Future resolves to `Manifest | None`
        - `auto` (default): probes source duration; short sources → single
          file (`Path | None`), long sources → chunked (`Manifest | None`).
          Shortens migration: callers that don't care which mode runs just
          receive "whatever fresh proxy is ready for this source".

        Fast path: if a fresh proxy already exists for the chosen mode,
        returns a completed future immediately.

        Dedup path: concurrent calls for the same (project, source, mode)
        collapse to a single in-flight future.
        """
        resolved_mode = self._resolve_mode(mode, source_path, chunk_seconds)

        # Fast path — already generated.
        if resolved_mode == "single":
            pp = proxy_path_for(project_dir, source_path)
            if pp is not None and pp.exists() and pp.stat().st_size > 0:
                done: concurrent.futures.Future = concurrent.futures.Future()
                done.set_result(pp)
                return done
        else:
            existing = chunked_proxy_manifest(project_dir, source_path)
            if existing is not None:
                done = concurrent.futures.Future()
                done.set_result(existing)
                return done

        dedup_key = (
            f"{str(project_dir.resolve())}::{source_path}::"
            f"{target_height}::{resolved_mode}::{chunk_seconds}"
        )
        with self._lock:
            existing_entry = self._pending.get(dedup_key)
            if existing_entry is not None:
                existing_entry.subscribers += 1
                return existing_entry.future

            fut = self._pool.submit(
                self._worker_generate,
                project_dir, source_path, target_height,
                resolved_mode, chunk_seconds, dedup_key,
            )
            self._pending[dedup_key] = _PendingEntry(future=fut)
            return fut

    def _resolve_mode(
        self,
        mode: ProxyMode,
        source_path: str,
        chunk_seconds: float,
    ) -> Literal["single", "chunked"]:
        """Turn `auto` into the concrete mode by probing source duration.

        Short sources (< `chunk_seconds`) → single-file proxy so we don't
        pay directory/manifest overhead for clips that decode fast anyway.
        Long sources → chunked.

        If duration probe fails (ffprobe missing, unsupported format),
        falls back to single-file — it's the safer choice and preserves
        the pre-chunked-proxy behavior.
        """
        if mode == "single":
            return "single"
        if mode == "chunked":
            return "chunked"
        # auto
        duration = _probe_source_duration(source_path)
        if duration is None:
            return "single"
        return "chunked" if duration >= chunk_seconds else "single"

    def _worker_generate(
        self,
        project_dir: Path,
        source_path: str,
        target_height: int,
        mode: Literal["single", "chunked"],
        chunk_seconds: float,
        dedup_key: str,
    ) -> Path | Manifest | None:
        try:
            if mode == "chunked":
                return generate_chunked_proxy(
                    project_dir, source_path,
                    target_height=target_height,
                    chunk_seconds=chunk_seconds,
                )
            return generate_proxy(project_dir, source_path, target_height=target_height)
        finally:
            with self._lock:
                self._pending.pop(dedup_key, None)

    def shutdown(self) -> None:
        """Stop accepting new work and close the thread pool."""
        self._pool.shutdown(wait=False)


# Module-level convenience: auto-init the coordinator lazily via instance().
