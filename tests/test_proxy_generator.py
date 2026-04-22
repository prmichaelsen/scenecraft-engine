"""Tests for the proxy generator.

Covers path/hash stability, mtime-based invalidation, and — when ffmpeg is
available — an actual proxy transcode. Transcode tests skip gracefully
when ffmpeg is missing from PATH.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from scenecraft.render.proxy_generator import (
    DEFAULT_PROXY_HEIGHT,
    ProxyCoordinator,
    generate_proxy,
    proxy_exists,
    proxy_path_for,
)


# ── Path / hash tests — no ffmpeg required ────────────────────────────


def test_proxy_path_for_missing_source(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    nonexistent = str(tmp_path / "not_there.mp4")
    assert proxy_path_for(project, nonexistent) is None


def test_proxy_path_for_stable_same_mtime(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    source = tmp_path / "source.mp4"
    source.write_bytes(b"fake content")

    p1 = proxy_path_for(project, str(source))
    p2 = proxy_path_for(project, str(source))
    assert p1 is not None
    assert p1 == p2
    # Hash-derived filename ending in .mp4
    assert p1.suffix == ".mp4"
    # Lives under proxies/
    assert p1.parent == project / "proxies"


def test_proxy_path_changes_on_mtime_bump(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    source = tmp_path / "source.mp4"
    source.write_bytes(b"fake content v1")
    p1 = proxy_path_for(project, str(source))

    # Bump mtime — simulate an edit. time.sleep so mtime_ns actually changes
    # on systems with coarse clocks.
    time.sleep(0.01)
    os.utime(source, None)
    p2 = proxy_path_for(project, str(source))

    assert p1 is not None
    assert p2 is not None
    assert p1 != p2, "proxy path should change when source mtime changes"


def test_proxy_exists_false_without_proxy_file(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    source = tmp_path / "source.mp4"
    source.write_bytes(b"fake content")
    assert proxy_exists(project, str(source)) is False


def test_proxy_exists_true_after_fake_write(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    source = tmp_path / "source.mp4"
    source.write_bytes(b"fake content")

    pp = proxy_path_for(project, str(source))
    assert pp is not None
    pp.parent.mkdir(parents=True, exist_ok=True)
    pp.write_bytes(b"fake proxy with nonzero size")

    assert proxy_exists(project, str(source)) is True


def test_proxy_exists_stale_after_source_touch(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    source = tmp_path / "source.mp4"
    source.write_bytes(b"fake content")

    pp = proxy_path_for(project, str(source))
    assert pp is not None
    pp.parent.mkdir(parents=True, exist_ok=True)
    pp.write_bytes(b"fake proxy")
    assert proxy_exists(project, str(source)) is True

    # Mtime-bump the source — proxy path shifts, so proxy_exists returns
    # False even though the old proxy file still sits on disk.
    time.sleep(0.01)
    os.utime(source, None)
    assert proxy_exists(project, str(source)) is False


def test_proxy_exists_false_for_zero_byte_proxy(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    source = tmp_path / "source.mp4"
    source.write_bytes(b"fake content")

    pp = proxy_path_for(project, str(source))
    assert pp is not None
    pp.parent.mkdir(parents=True, exist_ok=True)
    pp.touch()  # zero bytes
    assert proxy_exists(project, str(source)) is False


# ── Transcode tests — require ffmpeg ──────────────────────────────────


def _ffmpeg_available() -> bool:
    return shutil.which("ffmpeg") is not None


def _make_test_video(path: Path, duration_s: int = 2, width: int = 640, height: int = 480) -> None:
    """Create a tiny test H.264 mp4 via ffmpeg's lavfi color source."""
    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-f",
        "lavfi",
        "-i",
        f"color=c=red:size={width}x{height}:rate=24",
        "-t",
        str(duration_s),
        "-c:v",
        "libx264",
        "-preset",
        "ultrafast",
        "-pix_fmt",
        "yuv420p",
        str(path),
    ]
    subprocess.run(cmd, check=True, capture_output=True)


@pytest.mark.skipif(not _ffmpeg_available(), reason="ffmpeg not on PATH")
def test_generate_proxy_produces_540p(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    source = tmp_path / "source.mp4"
    _make_test_video(source, duration_s=1, width=1920, height=1080)

    pp = generate_proxy(project, str(source), target_height=540)
    assert pp is not None
    assert pp.exists()
    assert pp.stat().st_size > 0

    # Confirm dimensions match target_height
    import cv2
    cap = cv2.VideoCapture(str(pp))
    try:
        assert int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT)) == 540
        # Width follows aspect ratio (16:9 → 960); allow 1px slop for even-alignment
        assert 958 <= int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)) <= 962
    finally:
        cap.release()


@pytest.mark.skipif(not _ffmpeg_available(), reason="ffmpeg not on PATH")
def test_generate_proxy_idempotent_same_mtime(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    source = tmp_path / "source.mp4"
    _make_test_video(source, duration_s=1, width=640, height=360)

    pp1 = generate_proxy(project, str(source))
    assert pp1 is not None
    mtime1 = pp1.stat().st_mtime_ns

    # Second call: proxy already exists; should return same path without
    # re-transcoding.
    pp2 = generate_proxy(project, str(source))
    assert pp2 is not None
    assert pp2 == pp1
    assert pp2.stat().st_mtime_ns == mtime1, "proxy should not be re-transcoded"


@pytest.mark.skipif(not _ffmpeg_available(), reason="ffmpeg not on PATH")
def test_coordinator_dedups_concurrent_requests(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    source = tmp_path / "source.mp4"
    _make_test_video(source, duration_s=1, width=640, height=360)

    coord = ProxyCoordinator.instance()
    f1 = coord.ensure_proxy(project, str(source))
    f2 = coord.ensure_proxy(project, str(source))
    # Either it already resolved (fast path because proxy exists) or
    # both futures point at the same underlying work.
    p1 = f1.result(timeout=60)
    p2 = f2.result(timeout=60)
    assert p1 == p2
    assert p1 is not None
    assert p1.exists()
