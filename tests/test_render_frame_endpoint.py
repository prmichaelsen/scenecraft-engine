"""End-to-end tests for GET /api/projects/:name/render-frame."""

from __future__ import annotations

import shutil
import tempfile
import threading
from http.server import HTTPServer
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.error import HTTPError

import numpy as np
import pytest

from scenecraft.api_server import make_handler
from scenecraft.db import (
    _migrated_dbs, add_keyframe, add_transition, close_db, get_db, set_meta_bulk,
)


FPS = 24
WIDTH = 320
HEIGHT = 240


def _make_gradient_video(path: Path, seconds: float = 1.0) -> None:
    import cv2

    n_frames = int(seconds * FPS)
    writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"mp4v"), FPS, (WIDTH, HEIGHT))
    for i in range(n_frames):
        frame = np.zeros((HEIGHT, WIDTH, 3), dtype=np.uint8)
        frame[:, :, 0] = (i * 7) % 256
        frame[:, :, 1] = (i * 11) % 256
        frame[:, :, 2] = (i * 13) % 256
        writer.write(frame)
    writer.release()


@pytest.fixture
def project_env():
    work_dir = Path(tempfile.mkdtemp())
    project_name = "renderframe_project"
    project_dir = work_dir / project_name
    project_dir.mkdir()

    get_db(project_dir)
    set_meta_bulk(project_dir, {
        "title": "renderframe",
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
        "id": "kf_002", "timestamp": "0:01.00", "section": "",
        "source": "", "prompt": "end", "selected": 0, "candidates": [],
    })
    add_transition(project_dir, {
        "id": "tr_001", "from": "kf_001", "to": "kf_002",
        "duration_seconds": 1.0, "slots": 1, "action": "",
        "selected": [0], "remap": {"method": "linear", "target_duration": 0},
    })

    sel_dir = project_dir / "selected_transitions"
    sel_dir.mkdir(parents=True)
    _make_gradient_video(sel_dir / "tr_001_slot_0.mp4", seconds=1.0)

    Handler = make_handler(work_dir, no_auth=True)
    server = HTTPServer(("127.0.0.1", 0), Handler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    yield {
        "project_name": project_name,
        "project_dir": project_dir,
        "base_url": f"http://127.0.0.1:{port}",
    }

    server.shutdown()
    close_db(project_dir)
    _migrated_dbs.discard(str(project_dir / "project.db"))
    shutil.rmtree(work_dir)


def _get(url: str):
    req = Request(url, method="GET")
    return urlopen(req, timeout=10)


def test_render_frame_returns_jpeg(project_env):
    url = f"{project_env['base_url']}/api/projects/{project_env['project_name']}/render-frame?t=0.25"
    resp = _get(url)
    assert resp.status == 200
    assert resp.headers.get("Content-Type") == "image/jpeg"
    body = resp.read()
    assert len(body) > 100
    # JPEG magic bytes
    assert body[:2] == b"\xff\xd8"
    assert body[-2:] == b"\xff\xd9"


def test_render_frame_honors_quality_param(project_env):
    base = f"{project_env['base_url']}/api/projects/{project_env['project_name']}/render-frame"
    high = _get(f"{base}?t=0.25&quality=95").read()
    low = _get(f"{base}?t=0.25&quality=30").read()
    assert len(high) > len(low), "q=95 JPEG should be larger than q=30"


def test_render_frame_clamps_out_of_range_t(project_env):
    """Requesting t past the timeline should return the last frame, not an error."""
    url = f"{project_env['base_url']}/api/projects/{project_env['project_name']}/render-frame?t=99"
    resp = _get(url)
    assert resp.status == 200


def test_render_frame_rejects_bad_t(project_env):
    url = f"{project_env['base_url']}/api/projects/{project_env['project_name']}/render-frame?t=not-a-number"
    with pytest.raises(HTTPError) as exc_info:
        _get(url)
    assert exc_info.value.code == 400


def test_render_frame_unknown_project_404(project_env):
    url = f"{project_env['base_url']}/api/projects/does-not-exist/render-frame?t=0"
    with pytest.raises(HTTPError) as exc_info:
        _get(url)
    assert exc_info.value.code == 404


def test_render_frame_is_cached(project_env):
    """Second request for the same (t, quality) hits the cache."""
    from scenecraft.render.frame_cache import global_cache
    global_cache.clear()

    url = f"{project_env['base_url']}/api/projects/{project_env['project_name']}/render-frame?t=0.25"
    first = _get(url)
    first_body = first.read()
    assert first.headers.get("X-Scenecraft-Cache") == "MISS"

    second = _get(url)
    second_body = second.read()
    assert second.headers.get("X-Scenecraft-Cache") == "HIT"
    # Identical bytes from cache
    assert first_body == second_body


def test_cache_invalidates_on_db_write(project_env):
    """Writing to project.db bumps mtime → cache key changes → re-render."""
    from scenecraft.db import set_meta
    from scenecraft.render.frame_cache import global_cache
    global_cache.clear()

    url = f"{project_env['base_url']}/api/projects/{project_env['project_name']}/render-frame?t=0.25"
    _get(url).read()
    assert _get(url).headers.get("X-Scenecraft-Cache") == "HIT"

    # Any DB write bumps mtime and invalidates
    import time; time.sleep(0.01)  # ensure mtime changes
    set_meta(project_env["project_dir"], "motion_prompt", "new prompt")
    assert _get(url).headers.get("X-Scenecraft-Cache") == "MISS"


def test_render_frame_empty_project_returns_no_content(project_env):
    """A project with no transitions yet can't be rendered — return a clear 404."""
    work_dir = project_env["project_dir"].parent
    empty = work_dir / "empty_project"
    empty.mkdir()
    get_db(empty)
    set_meta_bulk(empty, {"title": "empty", "fps": FPS, "resolution": [WIDTH, HEIGHT]})

    url = f"{project_env['base_url']}/api/projects/empty_project/render-frame?t=0"
    with pytest.raises(HTTPError) as exc_info:
        _get(url)
    assert exc_info.value.code == 404
    close_db(empty)
    _migrated_dbs.discard(str(empty / "project.db"))
