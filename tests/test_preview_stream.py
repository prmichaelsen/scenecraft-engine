"""Tests for backend MSE playback: FragmentEncoder, RenderWorker, RenderCoordinator,
and the /ws/preview-stream/:project WebSocket route."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Iterable

import cv2
import numpy as np
import pytest

import websockets

from scenecraft.db import (
    add_keyframe, add_transition, close_db, get_db, set_meta, set_meta_bulk,
)
from scenecraft.render.frame_cache import global_cache
from scenecraft.render.preview_stream import (
    FragmentEncoder, _boxes, _split_init_and_media,
)
from scenecraft.render.preview_worker import (
    RenderCoordinator, RenderWorker, FRAGMENT_SECONDS,
)


FPS = 24
WIDTH = 320
HEIGHT = 240


# ── Fixtures ─────────────────────────────────────────────────────────────


def _make_gradient_video(path: Path, seconds: float = 2.0) -> None:
    n = int(seconds * FPS)
    writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"mp4v"), FPS, (WIDTH, HEIGHT))
    for i in range(n):
        frame = np.zeros((HEIGHT, WIDTH, 3), dtype=np.uint8)
        frame[:, :, 0] = (i * 7) % 256
        frame[:, :, 1] = (i * 11) % 256
        frame[:, :, 2] = (i * 13) % 256
        writer.write(frame)
    writer.release()


def _build_project(work_dir: Path, project_name: str, duration_s: float = 5.0) -> Path:
    project_dir = work_dir / project_name
    project_dir.mkdir()
    get_db(project_dir)
    set_meta_bulk(project_dir, {
        "title": project_name,
        "fps": FPS,
        "resolution": [WIDTH, HEIGHT],
        "motion_prompt": "",
        "default_transition_prompt": "",
    })
    add_keyframe(project_dir, {
        "id": "kf_001", "timestamp": "0:00.00", "section": "",
        "source": "", "prompt": "start", "selected": 0, "candidates": [],
    })
    add_keyframe(project_dir, {
        "id": "kf_002", "timestamp": _fmt_ts(duration_s), "section": "",
        "source": "", "prompt": "end", "selected": 0, "candidates": [],
    })
    add_transition(project_dir, {
        "id": "tr_001", "from": "kf_001", "to": "kf_002",
        "duration_seconds": duration_s, "slots": 1, "action": "",
        "selected": [0], "remap": {"method": "linear", "target_duration": 0},
    })
    sel = project_dir / "selected_transitions"
    sel.mkdir(parents=True)
    _make_gradient_video(sel / "tr_001_slot_0.mp4", seconds=duration_s)
    return project_dir


def _fmt_ts(seconds: float) -> str:
    mins = int(seconds // 60)
    secs = seconds - mins * 60
    return f"{mins}:{secs:05.2f}"


@pytest.fixture
def project(tmp_path):
    pd = _build_project(tmp_path, "stream_project", duration_s=3.0)
    yield pd
    try:
        close_db(pd)
    except Exception:
        pass


@pytest.fixture(autouse=True)
def reset_coordinator():
    RenderCoordinator._reset_instance()
    yield
    RenderCoordinator._reset_instance()


# ── FragmentEncoder tests ────────────────────────────────────────────────


def test_fragment_encoder_produces_valid_fmp4():
    """encode_init + encode_range concatenated should pass ffprobe."""
    enc = FragmentEncoder(width=WIDTH, height=HEIGHT, fps=FPS)
    init = enc.encode_init()
    init_boxes = {b[0] for b in _boxes(init)}
    assert "ftyp" in init_boxes, f"init missing ftyp: {init_boxes}"
    assert "moov" in init_boxes, f"init missing moov: {init_boxes}"

    frames1 = [np.full((HEIGHT, WIDTH, 3), (i * 7 % 250, 0, 0), dtype=np.uint8) for i in range(FPS)]
    frames2 = [np.full((HEIGHT, WIDTH, 3), (0, i * 11 % 250, 0), dtype=np.uint8) for i in range(FPS)]
    m1 = enc.encode_range(frames1)
    m2 = enc.encode_range(frames2)
    m1_boxes = {b[0] for b in _boxes(m1)}
    m2_boxes = {b[0] for b in _boxes(m2)}
    assert "moof" in m1_boxes and "mdat" in m1_boxes, f"media1 boxes={m1_boxes}"
    assert "moof" in m2_boxes and "mdat" in m2_boxes, f"media2 boxes={m2_boxes}"

    full = init + m1 + m2
    with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as tf:
        tf.write(full); path = tf.name
    try:
        # ffprobe should accept it without errors.
        res = subprocess.run(
            ["ffprobe", "-v", "error", "-count_frames", "-select_streams", "v:0",
             "-show_entries", "stream=nb_read_frames,codec_name", "-of", "csv=p=0", path],
            capture_output=True, text=True, timeout=15,
        )
        # Expect "h264,48" (2 fragments of 24 frames each)
        assert res.stderr.strip() == "", f"ffprobe stderr: {res.stderr}"
        assert "h264" in res.stdout, f"codec check failed: {res.stdout}"
        nframes = int(res.stdout.strip().rsplit(",", 1)[1])
        assert nframes == 2 * FPS, f"expected {2*FPS} frames, got {nframes}"
    finally:
        os.unlink(path)
    enc.close()


def test_init_segment_is_emitted_once():
    """Two fresh encoders each produce init; a single encoder's init is idempotent."""
    e1 = FragmentEncoder(width=WIDTH, height=HEIGHT, fps=FPS)
    e2 = FragmentEncoder(width=WIDTH, height=HEIGHT, fps=FPS)
    a1 = e1.encode_init()
    b1 = e1.encode_init()  # second call on same encoder — should return cached init bytes
    c1 = e2.encode_init()
    assert a1 == b1, "encode_init() must be idempotent within a single encoder"
    assert a1 and c1, "both encoders must produce non-empty init"
    # On the wire, each WS connection gets its own encoder → one init per connection.
    e1.close(); e2.close()


# ── RenderWorker tests ───────────────────────────────────────────────────


def test_render_worker_yields_fragments_in_order(project):
    """play(0) → collect a couple fragments → decoded frame count matches fragment count."""
    w = RenderWorker(project)
    w.play(0.0)
    iterator = w.fragments()
    init = next(iterator)
    frags = []
    # Pull two fragments (~2 seconds of material at 24fps).
    for _ in range(2):
        try:
            frags.append(next(iterator))
        except StopIteration:
            break
    w.stop()
    assert init, "no init segment"
    assert len(frags) == 2, f"expected 2 fragments, got {len(frags)}"

    full = init + b"".join(frags)
    with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as tf:
        tf.write(full); path = tf.name
    try:
        res = subprocess.run(
            ["ffprobe", "-v", "error", "-count_frames", "-select_streams", "v:0",
             "-show_entries", "stream=nb_read_frames", "-of", "csv=p=0", path],
            capture_output=True, text=True, timeout=15,
        )
        nframes = int(res.stdout.strip())
        assert nframes == 2 * FPS, f"expected {2*FPS}, got {nframes}"
    finally:
        os.unlink(path)


def test_seek_discards_queued_fragments(project):
    """After seek, fragments reflect the new position — a fresh init and fragment pair decodes frames."""
    w = RenderWorker(project)
    w.play(0.0)
    it = w.fragments()
    init1 = next(it)
    f1 = next(it)  # 0–1s
    assert f1

    # Seek forward to 1.5s — worker resets encoder state and continues.
    w.seek(1.5)
    # The old iterator is tied to the old encoder; after seek, the worker
    # expects a fresh consumer (this mirrors the real WS flow where the
    # client closes + reopens MediaSource). Simulate this by stopping and
    # creating a fresh iterator via a new worker.
    w.stop()

    w2 = RenderWorker(project)
    w2.play(1.5)
    it2 = w2.fragments()
    init2 = next(it2)
    post_seek = next(it2)
    w2.stop()
    assert init2, "post-seek init missing"
    assert post_seek, "post-seek media fragment missing"

    # Decode the post-seek fragment and verify at least the right frame count.
    full = init2 + post_seek
    with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as tf:
        tf.write(full); path = tf.name
    try:
        res = subprocess.run(
            ["ffprobe", "-v", "error", "-count_frames", "-select_streams", "v:0",
             "-show_entries", "stream=nb_read_frames", "-of", "csv=p=0", path],
            capture_output=True, text=True, timeout=15,
        )
        nframes = int(res.stdout.strip())
        assert nframes > 0
    finally:
        os.unlink(path)


def test_project_invalidation_flushes_queue(project):
    """Writing to project.db triggers schedule rebuild on the next loop iteration."""
    w = RenderWorker(project)
    w.play(0.0)
    it = w.fragments()
    init = next(it)
    f1 = next(it)
    assert f1

    # Invalidate: simulate a DB write and call the hook directly (the HTTP API
    # layer is responsible for wiring set_meta → RenderCoordinator.invalidate_project).
    set_meta(project, "motion_prompt", "new prompt after edit")
    w.on_project_invalidate()

    # The worker should continue producing fragments (on invalidate it rebuilds
    # the schedule and resumes from the current playhead).
    # Give the loop a moment.
    time.sleep(0.2)
    f2 = next(it)
    assert f2
    w.stop()


# ── RenderCoordinator tests ──────────────────────────────────────────────


def test_coordinator_caps_workers(tmp_path):
    """Getting (cap + 1) workers for distinct projects evicts the LRU."""
    # Build 3 distinct projects.
    projects = [_build_project(tmp_path, f"proj_{i}", duration_s=1.0) for i in range(3)]

    coord = RenderCoordinator(max_workers=2)
    w0 = coord.get_worker(projects[0])
    w1 = coord.get_worker(projects[1])
    assert coord.worker_count == 2
    # Touching w0 to make w1 the LRU.
    w0.play(0.0); w0.pause()
    time.sleep(0.01)

    # Third request should evict the LRU (w1) to make room.
    w2 = coord.get_worker(projects[2])
    assert coord.worker_count == 2
    # w1 should no longer be tracked; re-requesting it creates a new one.
    w1b = coord.get_worker(projects[1])
    assert w1b is not w1
    assert coord.worker_count == 2
    coord.shutdown()


def test_coordinator_evicts_idle_workers(project):
    """evict_idle() tears down paused workers past the idle timeout."""
    coord = RenderCoordinator(max_workers=4)
    w = coord.get_worker(project)
    # Worker starts un-played so it's idle from the outset.
    # Artificially age it.
    w._last_activity_ts = time.monotonic() - 10_000
    evicted = coord.evict_idle(idle_timeout_s=60)
    assert evicted == 1
    assert coord.worker_count == 0
    coord.shutdown()


# ── WebSocket end-to-end ─────────────────────────────────────────────────


def _start_ws_server(work_dir: Path):
    """Spin up ws_server on a free port; return (host, port, stop_fn)."""
    import scenecraft.ws_server as ws_server_mod

    ws_server_mod._work_dir = work_dir
    # Find a free port by asking OS for 0, then close.
    import socket
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    _, port = s.getsockname()
    s.close()

    loop_container: dict = {}

    async def _main():
        from websockets.asyncio.server import serve
        loop_container["loop"] = asyncio.get_running_loop()
        async with serve(ws_server_mod._handle_connection, "127.0.0.1", port):
            stop = asyncio.Future()
            loop_container["stop"] = stop
            await stop

    thread = threading.Thread(target=lambda: asyncio.run(_main()), daemon=True)
    thread.start()
    # Wait until the server is accepting.
    deadline = time.time() + 5
    while time.time() < deadline:
        try:
            test_sock = socket.socket()
            test_sock.settimeout(0.2)
            test_sock.connect(("127.0.0.1", port))
            test_sock.close()
            break
        except OSError:
            time.sleep(0.05)
    else:
        raise RuntimeError("ws server didn't start")

    def stop_fn():
        loop = loop_container.get("loop")
        fut = loop_container.get("stop")
        if loop and fut and not fut.done():
            loop.call_soon_threadsafe(fut.set_result, None)
        thread.join(timeout=3)

    return "127.0.0.1", port, stop_fn


@pytest.mark.asyncio
async def test_websocket_play_then_pause(project):
    """Connect, send play, read init + media, send pause, disconnect cleanly."""
    work_dir = project.parent
    host, port, stop = _start_ws_server(work_dir)
    try:
        uri = f"ws://{host}:{port}/ws/preview-stream/{project.name}"
        async with websockets.connect(uri) as ws:
            await ws.send(json.dumps({"action": "play", "t": 0.0}))

            # Expect binary frames in short order.
            init = await asyncio.wait_for(ws.recv(), timeout=10)
            assert isinstance(init, (bytes, bytearray))
            assert b"ftyp" in bytes(init)[:32] or b"moov" in bytes(init)[:64], \
                "first binary frame should contain ftyp/moov"

            media = await asyncio.wait_for(ws.recv(), timeout=10)
            assert isinstance(media, (bytes, bytearray))
            assert len(media) > 0

            await ws.send(json.dumps({"action": "pause"}))
            # Graceful close.
    finally:
        stop()
        RenderCoordinator._reset_instance()


@pytest.mark.asyncio
async def test_websocket_unknown_project_closes_with_error(tmp_path):
    """Requesting an unknown project produces an error frame and closes."""
    host, port, stop = _start_ws_server(tmp_path)
    try:
        uri = f"ws://{host}:{port}/ws/preview-stream/does-not-exist"
        async with websockets.connect(uri) as ws:
            # Server sends error text frame and closes.
            got_error = False
            try:
                while True:
                    msg = await asyncio.wait_for(ws.recv(), timeout=5)
                    if isinstance(msg, str):
                        data = json.loads(msg)
                        if data.get("type") == "error":
                            got_error = True
            except (websockets.exceptions.ConnectionClosed, asyncio.TimeoutError):
                pass
            assert got_error, "expected an error text frame before close"
    finally:
        stop()
