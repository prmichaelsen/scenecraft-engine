"""Low-resolution proxies for source video files.

NLE-standard trick: transcode every base-track source into a cheap-to-decode
lower-resolution copy (default 540p H.264), store alongside the project, and
route the preview compositor through the proxy instead of the original. Base-
frame decode cost drops ~4x at 540p vs 1080p, which is the pipeline
bottleneck identified in the M11→M12 performance audit.

Export / final-render paths bypass proxies (read originals for quality).

Storage layout:
    {project_dir}/proxies/{hash}.mp4

where `hash` is a truncated SHA-256 of `(absolute source path + source
mtime_ns)`. Changing the source — overwrite, timestamp bump, anything that
affects mtime — invalidates the proxy automatically because the key moves.

Public API:
    proxy_path_for(project_dir, source_path) -> Path | None
    proxy_exists(project_dir, source_path) -> bool
    generate_proxy(project_dir, source_path, target_height=540) -> Path | None
    ProxyCoordinator.instance()
        .ensure_proxy(project_dir, source_path) -> Future[Path | None]
"""

from __future__ import annotations

import concurrent.futures
import hashlib
import logging
import os
import subprocess
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable


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


# ── Background coordinator (singleton) ───────────────────────────────────


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
    ) -> concurrent.futures.Future:
        """Return a Future that resolves to the proxy Path, or None on failure.

        Fast path: if a fresh proxy already exists, returns a completed
        future immediately.

        Dedup path: if generation is already in flight for this (project,
        source), returns the existing future.

        Otherwise schedules a background transcode and returns the new
        future.
        """
        pp = proxy_path_for(project_dir, source_path)
        if pp is not None and pp.exists() and pp.stat().st_size > 0:
            done: concurrent.futures.Future = concurrent.futures.Future()
            done.set_result(pp)
            return done

        dedup_key = f"{str(project_dir.resolve())}::{source_path}::{target_height}"
        with self._lock:
            existing = self._pending.get(dedup_key)
            if existing is not None:
                existing.subscribers += 1
                return existing.future

            fut = self._pool.submit(
                self._worker_generate, project_dir, source_path, target_height, dedup_key
            )
            self._pending[dedup_key] = _PendingEntry(future=fut)
            return fut

    def _worker_generate(
        self,
        project_dir: Path,
        source_path: str,
        target_height: int,
        dedup_key: str,
    ) -> Path | None:
        try:
            return generate_proxy(project_dir, source_path, target_height=target_height)
        finally:
            with self._lock:
                self._pending.pop(dedup_key, None)

    def shutdown(self) -> None:
        """Stop accepting new work and close the thread pool."""
        self._pool.shutdown(wait=False)


# Module-level convenience: auto-init the coordinator lazily via instance().
