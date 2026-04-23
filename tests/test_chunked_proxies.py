"""Tests for chunked proxy generation (task-44, extends task-39).

Covers:
- manifest dataclass load/save round trip
- chunk_for_time boundary + mid-chunk mapping
- generate_chunked_proxy produces N chunks covering full source
- ProxyCoordinator mode='auto' falls back to single-file for short sources
- Stale / corrupt manifest → chunked_proxy_manifest returns None

Tests that need ffmpeg skip gracefully when it's missing.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from scenecraft.render.proxy_generator import (
    CHUNKED_MANIFEST_NAME,
    CHUNKED_MANIFEST_VERSION,
    DEFAULT_PROXY_CHUNK_SECONDS,
    Chunk,
    Manifest,
    ProxyCoordinator,
    chunk_for_time,
    chunked_proxy_dir_for,
    chunked_proxy_manifest,
    generate_chunked_proxy,
    proxy_exists,
)


# ── Model / path tests — no ffmpeg required ──────────────────────────────


def test_chunked_proxy_dir_missing_source(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    assert chunked_proxy_dir_for(project, str(tmp_path / "nope.mp4")) is None


def test_chunked_proxy_dir_stable_same_mtime(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    source = tmp_path / "source.mp4"
    source.write_bytes(b"fake content")

    d1 = chunked_proxy_dir_for(project, str(source))
    d2 = chunked_proxy_dir_for(project, str(source))
    assert d1 is not None
    assert d1 == d2
    # Lives under proxies/ and has no file extension (it's a directory)
    assert d1.parent == project / "proxies"
    assert d1.suffix == ""


def _sample_manifest(source: Path) -> Manifest:
    """Hand-built manifest matching a synthetic 10s source split into 5s chunks."""
    return Manifest(
        version=CHUNKED_MANIFEST_VERSION,
        source_path=str(source.resolve()),
        source_mtime_ns=source.stat().st_mtime_ns,
        chunk_seconds=5.0,
        total_seconds=10.0,
        chunks=(
            Chunk(index=0, file="chunk-000.mp4", start=0.0, end=5.0),
            Chunk(index=1, file="chunk-001.mp4", start=5.0, end=10.0),
        ),
    )


def _write_manifest_sidecar(pd: Path, manifest: Manifest) -> None:
    pd.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": manifest.version,
        "source_path": manifest.source_path,
        "source_mtime_ns": manifest.source_mtime_ns,
        "chunk_seconds": manifest.chunk_seconds,
        "total_seconds": manifest.total_seconds,
        "chunks": [
            {"index": c.index, "file": c.file, "start": c.start, "end": c.end}
            for c in manifest.chunks
        ],
    }
    (pd / CHUNKED_MANIFEST_NAME).write_text(json.dumps(payload))


def test_chunked_proxy_manifest_none_when_absent(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    source = tmp_path / "s.mp4"
    source.write_bytes(b"fake")
    assert chunked_proxy_manifest(project, str(source)) is None


def test_chunked_proxy_manifest_round_trip(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    source = tmp_path / "s.mp4"
    source.write_bytes(b"fake")

    pd = chunked_proxy_dir_for(project, str(source))
    assert pd is not None
    manifest = _sample_manifest(source)
    _write_manifest_sidecar(pd, manifest)

    loaded = chunked_proxy_manifest(project, str(source))
    assert loaded is not None
    assert loaded.version == CHUNKED_MANIFEST_VERSION
    assert loaded.total_seconds == 10.0
    assert len(loaded.chunks) == 2
    assert loaded.chunks[0] == Chunk(index=0, file="chunk-000.mp4", start=0.0, end=5.0)


def test_chunked_proxy_manifest_returns_none_on_corrupt_json(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    source = tmp_path / "s.mp4"
    source.write_bytes(b"fake")

    pd = chunked_proxy_dir_for(project, str(source))
    assert pd is not None
    pd.mkdir(parents=True, exist_ok=True)
    (pd / CHUNKED_MANIFEST_NAME).write_text("{ this is not json")

    assert chunked_proxy_manifest(project, str(source)) is None


def test_chunked_proxy_manifest_returns_none_on_bad_version(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    source = tmp_path / "s.mp4"
    source.write_bytes(b"fake")

    pd = chunked_proxy_dir_for(project, str(source))
    assert pd is not None
    pd.mkdir(parents=True, exist_ok=True)
    (pd / CHUNKED_MANIFEST_NAME).write_text(json.dumps({
        "version": CHUNKED_MANIFEST_VERSION + 99,
        "source_path": str(source),
        "source_mtime_ns": source.stat().st_mtime_ns,
        "chunk_seconds": 5.0,
        "total_seconds": 10.0,
        "chunks": [],
    }))
    assert chunked_proxy_manifest(project, str(source)) is None


def test_chunked_proxy_manifest_invalidated_by_mtime(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    source = tmp_path / "s.mp4"
    source.write_bytes(b"fake")

    pd = chunked_proxy_dir_for(project, str(source))
    assert pd is not None
    manifest = _sample_manifest(source)
    _write_manifest_sidecar(pd, manifest)
    # Sanity: loads cleanly before invalidation
    assert chunked_proxy_manifest(project, str(source)) is not None

    # Touch the source — hash key moves, and as a bonus the stored
    # source_mtime_ns in the manifest no longer matches. Both paths
    # should produce None.
    time.sleep(0.01)
    source.write_bytes(b"changed")
    assert chunked_proxy_manifest(project, str(source)) is None


# ── chunk_for_time ───────────────────────────────────────────────────────


def test_chunk_for_time_start_of_first_chunk(tmp_path: Path) -> None:
    source = tmp_path / "s.mp4"
    source.write_bytes(b"x")
    m = _sample_manifest(source)
    assert chunk_for_time(m, 0.0) == (0, 0.0)


def test_chunk_for_time_midchunk(tmp_path: Path) -> None:
    source = tmp_path / "s.mp4"
    source.write_bytes(b"x")
    m = _sample_manifest(source)
    idx, off = chunk_for_time(m, 2.5)
    assert idx == 0
    assert abs(off - 2.5) < 1e-9


def test_chunk_for_time_boundary_goes_to_next(tmp_path: Path) -> None:
    """At an exact boundary we choose the NEXT chunk — [start, end) semantics
    except for the final chunk which is inclusive of its end."""
    source = tmp_path / "s.mp4"
    source.write_bytes(b"x")
    m = _sample_manifest(source)
    idx, off = chunk_for_time(m, 5.0)
    assert idx == 1
    assert abs(off - 0.0) < 1e-9


def test_chunk_for_time_final_second(tmp_path: Path) -> None:
    source = tmp_path / "s.mp4"
    source.write_bytes(b"x")
    m = _sample_manifest(source)
    idx, off = chunk_for_time(m, 9.999)
    assert idx == 1
    assert abs(off - 4.999) < 1e-3


def test_chunk_for_time_exact_end_resolves_to_last_chunk(tmp_path: Path) -> None:
    source = tmp_path / "s.mp4"
    source.write_bytes(b"x")
    m = _sample_manifest(source)
    result = chunk_for_time(m, 10.0)
    assert result is not None
    idx, off = result
    assert idx == 1


def test_chunk_for_time_past_end_returns_none(tmp_path: Path) -> None:
    source = tmp_path / "s.mp4"
    source.write_bytes(b"x")
    m = _sample_manifest(source)
    assert chunk_for_time(m, 11.0) is None


def test_chunk_for_time_negative_clamps_to_zero(tmp_path: Path) -> None:
    source = tmp_path / "s.mp4"
    source.write_bytes(b"x")
    m = _sample_manifest(source)
    assert chunk_for_time(m, -0.1) == (0, 0.0)


# ── Transcode tests — require ffmpeg ─────────────────────────────────────


def _ffmpeg_available() -> bool:
    return shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None


def _make_test_video(path: Path, duration_s: int, width: int = 320, height: int = 240) -> None:
    """Create a tiny test H.264 mp4 via ffmpeg's lavfi color source.

    Keyframe every 24 frames (~1s) so segment boundaries can land near
    the requested chunk_seconds.
    """
    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel", "error",
        "-y",
        "-f", "lavfi",
        "-i", f"color=c=red:size={width}x{height}:rate=24",
        "-t", str(duration_s),
        "-c:v", "libx264",
        "-preset", "ultrafast",
        "-g", "24",  # GOP size — keyframes ~1s apart
        "-pix_fmt", "yuv420p",
        str(path),
    ]
    subprocess.run(cmd, check=True, capture_output=True)


@pytest.mark.skipif(not _ffmpeg_available(), reason="ffmpeg/ffprobe not on PATH")
def test_generate_chunked_proxy_produces_N_chunks(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    source = tmp_path / "source.mp4"
    # 10 seconds of content, split into 3s chunks → ~4 chunks
    _make_test_video(source, duration_s=10)

    manifest = generate_chunked_proxy(
        project, str(source), target_height=120, chunk_seconds=3.0
    )
    assert manifest is not None
    assert manifest.version == CHUNKED_MANIFEST_VERSION
    assert manifest.chunk_seconds == 3.0
    # Chunks should cover the full source duration within a small margin.
    assert 8.0 <= manifest.total_seconds <= 12.0
    assert 3 <= len(manifest.chunks) <= 5

    # First chunk starts at 0, last chunk ends at total_seconds
    assert manifest.chunks[0].start == 0.0
    assert abs(manifest.chunks[-1].end - manifest.total_seconds) < 0.1

    # Chunks are contiguous — each chunk's start equals the previous end
    for prev, curr in zip(manifest.chunks, manifest.chunks[1:]):
        assert abs(curr.start - prev.end) < 1e-6

    # All chunk files exist on disk under the proxy dir
    pd = chunked_proxy_dir_for(project, str(source))
    assert pd is not None
    for c in manifest.chunks:
        assert (pd / c.file).exists()
        assert (pd / c.file).stat().st_size > 0


@pytest.mark.skipif(not _ffmpeg_available(), reason="ffmpeg/ffprobe not on PATH")
def test_generate_chunked_proxy_idempotent(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    source = tmp_path / "source.mp4"
    _make_test_video(source, duration_s=6)

    m1 = generate_chunked_proxy(
        project, str(source), target_height=120, chunk_seconds=2.0
    )
    assert m1 is not None
    pd = chunked_proxy_dir_for(project, str(source))
    assert pd is not None
    first_mtime = (pd / CHUNKED_MANIFEST_NAME).stat().st_mtime_ns

    # Second call — should short-circuit and NOT re-transcode
    m2 = generate_chunked_proxy(
        project, str(source), target_height=120, chunk_seconds=2.0
    )
    assert m2 is not None
    assert (pd / CHUNKED_MANIFEST_NAME).stat().st_mtime_ns == first_mtime


@pytest.mark.skipif(not _ffmpeg_available(), reason="ffmpeg/ffprobe not on PATH")
def test_generate_chunked_proxy_writes_manifest_last(tmp_path: Path) -> None:
    """The .partial directory should exist during generation but never be
    readable as a complete manifest. In our sync generator we can't easily
    observe the in-progress state, but we CAN verify that the final
    directory exists with manifest.json present and that no .partial
    sibling survives."""
    project = tmp_path / "project"
    project.mkdir()
    source = tmp_path / "source.mp4"
    _make_test_video(source, duration_s=5)

    manifest = generate_chunked_proxy(
        project, str(source), target_height=120, chunk_seconds=2.0
    )
    assert manifest is not None

    pd = chunked_proxy_dir_for(project, str(source))
    assert pd is not None
    assert pd.is_dir()
    assert (pd / CHUNKED_MANIFEST_NAME).exists()

    # The tmp ".partial" sibling must have been cleaned up via the rename
    partial = pd.with_name(pd.name + ".partial")
    assert not partial.exists()


@pytest.mark.skipif(not _ffmpeg_available(), reason="ffmpeg/ffprobe not on PATH")
def test_chunk_for_time_lines_up_with_generated_chunks(tmp_path: Path) -> None:
    """End-to-end: generate real chunks and verify chunk_for_time() picks
    the correct chunk for timestamps spanning the whole source."""
    project = tmp_path / "project"
    project.mkdir()
    source = tmp_path / "source.mp4"
    _make_test_video(source, duration_s=10)

    manifest = generate_chunked_proxy(
        project, str(source), target_height=120, chunk_seconds=3.0
    )
    assert manifest is not None

    # Sample times across the source — each should land in the chunk
    # whose [start, end) window contains it.
    for t in (0.0, 1.0, 2.99, 3.0, 5.5, 9.0):
        mapped = chunk_for_time(manifest, t)
        assert mapped is not None, f"t={t} mapped to None"
        idx, off = mapped
        c = manifest.chunks[idx]
        assert c.start <= t <= c.end + 1e-6
        # offset within chunk matches
        assert abs((t - c.start) - off) < 1e-6 or idx == len(manifest.chunks) - 1


# ── ProxyCoordinator mode='auto' dispatch ─────────────────────────────────


@pytest.mark.skipif(not _ffmpeg_available(), reason="ffmpeg/ffprobe not on PATH")
def test_coordinator_auto_short_source_is_single_file(tmp_path: Path) -> None:
    """Source shorter than chunk_seconds → single-file proxy, no manifest."""
    project = tmp_path / "project"
    project.mkdir()
    source = tmp_path / "short.mp4"
    _make_test_video(source, duration_s=2)  # well under default 300s

    coord = ProxyCoordinator.instance()
    fut = coord.ensure_proxy(project, str(source))
    result = fut.result(timeout=60)

    # Single-file mode returns a Path to the .mp4 proxy
    assert isinstance(result, Path)
    assert result.exists()
    assert proxy_exists(project, str(source))
    # And no chunked manifest was created.
    assert chunked_proxy_manifest(project, str(source)) is None


@pytest.mark.skipif(not _ffmpeg_available(), reason="ffmpeg/ffprobe not on PATH")
def test_coordinator_auto_long_source_is_chunked(tmp_path: Path) -> None:
    """Source >= chunk_seconds threshold → chunked mode picked automatically."""
    project = tmp_path / "project"
    project.mkdir()
    source = tmp_path / "long.mp4"
    # 6s source, but with chunk_seconds=2 this exceeds the threshold
    _make_test_video(source, duration_s=6)

    coord = ProxyCoordinator.instance()
    fut = coord.ensure_proxy(project, str(source), chunk_seconds=2.0)
    result = fut.result(timeout=60)

    assert isinstance(result, Manifest)
    assert result.total_seconds >= 5.5
    assert len(result.chunks) >= 2


@pytest.mark.skipif(not _ffmpeg_available(), reason="ffmpeg/ffprobe not on PATH")
def test_coordinator_chunked_mode_explicit(tmp_path: Path) -> None:
    """Forcing mode='chunked' on a short source still produces chunks."""
    project = tmp_path / "project"
    project.mkdir()
    source = tmp_path / "tiny.mp4"
    _make_test_video(source, duration_s=4)

    coord = ProxyCoordinator.instance()
    fut = coord.ensure_proxy(
        project, str(source), mode="chunked", chunk_seconds=2.0
    )
    result = fut.result(timeout=60)

    assert isinstance(result, Manifest)
    assert len(result.chunks) >= 2


# ── Compositor integration — chunked proxies routed through _resolve_source_for_read

@pytest.mark.skipif(not _ffmpeg_available(), reason="ffmpeg/ffprobe not on PATH")
def test_resolve_source_prefers_chunked_over_single_and_original(tmp_path: Path) -> None:
    """_resolve_source_for_read picks the chunk file (not original, not
    single-file proxy) when a chunked proxy is available — and returns
    the chunk's start time as the offset."""
    from scenecraft.render.compositor import _resolve_source_for_read

    project = tmp_path / "project"
    project.mkdir()
    source = tmp_path / "long.mp4"
    _make_test_video(source, duration_s=6)

    manifest = generate_chunked_proxy(
        project, str(source), target_height=120, chunk_seconds=2.0
    )
    assert manifest is not None
    assert len(manifest.chunks) >= 2

    seg = {"source": str(source)}

    # First chunk
    path0, off0 = _resolve_source_for_read(seg, project, True, 0.5)
    assert off0 == 0.0
    assert manifest.chunks[0].file in path0

    # Later chunk — pick a time inside the second chunk
    t_in_second = (manifest.chunks[1].start + manifest.chunks[1].end) / 2.0
    path1, off1 = _resolve_source_for_read(seg, project, True, t_in_second)
    assert abs(off1 - manifest.chunks[1].start) < 1e-6
    assert manifest.chunks[1].file in path1
    # Different chunk files — so stream_caps cache keyed on (seg, path)
    # will naturally open a fresh cap per chunk.
    assert path0 != path1


@pytest.mark.skipif(not _ffmpeg_available(), reason="ffmpeg/ffprobe not on PATH")
def test_resolve_source_falls_back_when_not_preferring_proxy(tmp_path: Path) -> None:
    """prefer_proxy=False → always original, offset 0, even if chunked proxy exists."""
    from scenecraft.render.compositor import _resolve_source_for_read

    project = tmp_path / "project"
    project.mkdir()
    source = tmp_path / "long.mp4"
    _make_test_video(source, duration_s=4)

    m = generate_chunked_proxy(project, str(source), target_height=120, chunk_seconds=2.0)
    assert m is not None

    seg = {"source": str(source)}
    path, off = _resolve_source_for_read(seg, project, False, 1.0)
    assert off == 0.0
    assert path == str(source)


@pytest.mark.skipif(not _ffmpeg_available(), reason="ffmpeg/ffprobe not on PATH")
def test_resolve_source_returns_single_file_proxy_when_only_that_exists(tmp_path: Path) -> None:
    """Single-file proxy present but no chunked proxy → return single-file
    proxy with offset 0. Preserves task-39 behavior when chunked-mode
    hasn't been invoked for this source."""
    from scenecraft.render.compositor import _resolve_source_for_read
    from scenecraft.render.proxy_generator import generate_proxy

    project = tmp_path / "project"
    project.mkdir()
    source = tmp_path / "single.mp4"
    _make_test_video(source, duration_s=2)

    single_pp = generate_proxy(project, str(source), target_height=120)
    assert single_pp is not None

    seg = {"source": str(source)}
    path, off = _resolve_source_for_read(seg, project, True, 0.5)
    assert off == 0.0
    assert path == str(single_pp)
