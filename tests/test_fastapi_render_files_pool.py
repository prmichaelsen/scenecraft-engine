"""M16 T63 — rendering + files + pool + candidates routers.

Named tests from the task file:

    - render_frame_bytes_identical   (5 tuples across 2 projects)
    - large_upload_streams           (200 MB upload, RSS bounded)
    - get_route_parity               (rendering/pool/candidates slice)
    - post_route_parity              (rendering/pool/candidates slice)

TDD order: authored BEFORE ``routers/rendering.py``, ``routers/pool.py``,
``routers/candidates.py`` exist. Every assertion fails on first run.

Byte-parity methodology
-----------------------
The legacy ``_handle_render_frame`` path is ``cv2.imencode(".jpg", frame,
[cv2.IMWRITE_JPEG_QUALITY, quality])`` on the schedule-rendered frame.
Byte-parity therefore means: for identical ``(project_db_mtime, t,
quality)`` and a cold cache, the JPEG bytes emitted by the FastAPI
router must equal the JPEG bytes emitted by the legacy HTTPServer —
identical magic bytes, identical quantization tables, identical
Huffman tables, identical pixel data. Any difference in cv2 flags,
scaling filters, or colour-space conversions would show up here.

We avoid disk fixtures for render-frame. Instead the test spins up
BOTH servers against the same tmp project + same ``project.db``, hits
each endpoint with the same query, and byte-compares the responses.
Same ``global_cache`` instance backs both (it's module-scoped), so the
second hit will be a cache hit — we clear the cache before each
measurement to force a real render pass on each side.
"""

from __future__ import annotations

import os
import shutil
import tempfile
import threading
import time
from http.server import HTTPServer
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.error import HTTPError

import numpy as np
import pytest
from fastapi.testclient import TestClient


FPS = 24
WIDTH = 320
HEIGHT = 240


# ---------------------------------------------------------------------------
# Shared project fixture — runs both legacy and fastapi against this tree
# ---------------------------------------------------------------------------


def _make_gradient_video(path: Path, seconds: float = 1.0) -> None:
    """Deterministic gradient video that ffmpeg+cv2 both decode the same way.

    Same bytes on every run (seed-free formula), so cache+render-frame
    produces reproducible JPEG output for parity measurement.
    """
    import cv2

    n_frames = int(seconds * FPS)
    writer = cv2.VideoWriter(
        str(path), cv2.VideoWriter_fourcc(*"mp4v"), FPS, (WIDTH, HEIGHT)
    )
    for i in range(n_frames):
        frame = np.zeros((HEIGHT, WIDTH, 3), dtype=np.uint8)
        frame[:, :, 0] = (i * 7) % 256
        frame[:, :, 1] = (i * 11) % 256
        frame[:, :, 2] = (i * 13) % 256
        writer.write(frame)
    writer.release()


def _make_project(work_dir: Path, project_name: str) -> Path:
    """Create a minimal renderable project under work_dir/project_name."""
    from scenecraft.db import add_keyframe, add_transition, get_db, set_meta_bulk

    project_dir = work_dir / project_name
    project_dir.mkdir(parents=True, exist_ok=True)

    get_db(project_dir)
    set_meta_bulk(
        project_dir,
        {
            "title": project_name,
            "fps": FPS,
            "resolution": [WIDTH, HEIGHT],
            "motion_prompt": "",
            "default_transition_prompt": "",
        },
    )

    add_keyframe(
        project_dir,
        {
            "id": "kf_001",
            "timestamp": "0:00.00",
            "section": "",
            "source": "",
            "prompt": "start",
            "selected": 0,
            "candidates": [],
        },
    )
    add_keyframe(
        project_dir,
        {
            "id": "kf_002",
            "timestamp": "0:01.00",
            "section": "",
            "source": "",
            "prompt": "end",
            "selected": 0,
            "candidates": [],
        },
    )
    add_transition(
        project_dir,
        {
            "id": "tr_001",
            "from": "kf_001",
            "to": "kf_002",
            "duration_seconds": 1.0,
            "slots": 1,
            "action": "",
            "selected": [0],
            "remap": {"method": "linear", "target_duration": 0},
        },
    )

    sel_dir = project_dir / "selected_transitions"
    sel_dir.mkdir(parents=True)
    _make_gradient_video(sel_dir / "tr_001_slot_0.mp4", seconds=1.0)
    return project_dir


@pytest.fixture()
def dual_server(tmp_path: Path):
    """Spin up legacy HTTPServer + FastAPI TestClient against the same work_dir.

    Two projects are laid out side-by-side (P1, P2) to satisfy the task's
    "5 tuples across 2 projects" matrix. ``global_cache`` is cleared in the
    test body per measurement so both servers genuinely encode each frame.
    """
    from scenecraft.api.app import create_app
    from scenecraft.api_server import make_handler
    from scenecraft.db import _migrated_dbs, close_db

    work_dir = tmp_path / "work"
    work_dir.mkdir()

    p1 = _make_project(work_dir, "P1")
    p2 = _make_project(work_dir, "P2")

    # Legacy HTTPServer
    Handler = make_handler(work_dir, no_auth=True)
    server = HTTPServer(("127.0.0.1", 0), Handler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    # FastAPI — no auth (no .scenecraft root under work_dir)
    app = create_app(work_dir=work_dir)
    client = TestClient(app)

    yield {
        "work_dir": work_dir,
        "projects": ["P1", "P2"],
        "project_dirs": {"P1": p1, "P2": p2},
        "legacy_url": f"http://127.0.0.1:{port}",
        "fastapi": client,
    }

    server.shutdown()
    for pd in (p1, p2):
        close_db(pd)
        _migrated_dbs.discard(str(pd / "project.db"))
    shutil.rmtree(work_dir)


# ---------------------------------------------------------------------------
# render_frame_bytes_identical
# ---------------------------------------------------------------------------


# Representative tuples: 2 projects × mix of (t, quality). The task spec
# calls for 5 total; we use P1@(0.25, 85), P1@(0.5, 50), P1@(0.75, 95),
# P2@(0.1, 85), P2@(0.9, 30). Together they cover low/mid/high quality
# and a scrub across the 1-second timeline.
RENDER_FRAME_TUPLES = [
    ("P1", 0.25, 85),
    ("P1", 0.5, 50),
    ("P1", 0.75, 95),
    ("P2", 0.1, 85),
    ("P2", 0.9, 30),
]


def _legacy_render_frame(base_url: str, project: str, t: float, quality: int) -> bytes:
    url = f"{base_url}/api/projects/{project}/render-frame?t={t}&quality={quality}"
    resp = urlopen(Request(url, method="GET"), timeout=30)
    assert resp.status == 200
    assert resp.headers.get("Content-Type") == "image/jpeg"
    return resp.read()


def _fastapi_render_frame(client: TestClient, project: str, t: float, quality: int) -> bytes:
    resp = client.get(f"/api/projects/{project}/render-frame", params={"t": t, "quality": quality})
    assert resp.status_code == 200, resp.text
    assert resp.headers.get("content-type") == "image/jpeg"
    return resp.content


@pytest.mark.parametrize("project,t,quality", RENDER_FRAME_TUPLES)
def test_render_frame_bytes_identical(dual_server, project, t, quality):
    """render-frame-bytes-identical — legacy and FastAPI must emit the same JPEG bytes.

    Same cv2 call path, same frame, same quality → byte-for-byte equal
    JPEG. This is the hot-path invariant: the frontend's
    ``<PreviewViewport>`` caches these and would show subtly different
    pixels on any encoder drift.
    """
    from scenecraft.render.frame_cache import global_cache

    global_cache.clear()
    legacy_bytes = _legacy_render_frame(dual_server["legacy_url"], project, t, quality)

    global_cache.clear()
    fastapi_bytes = _fastapi_render_frame(dual_server["fastapi"], project, t, quality)

    # Both must be valid JPEGs.
    assert legacy_bytes[:2] == b"\xff\xd8"
    assert legacy_bytes[-2:] == b"\xff\xd9"
    assert fastapi_bytes[:2] == b"\xff\xd8"
    assert fastapi_bytes[-2:] == b"\xff\xd9"

    # Byte-for-byte parity is the contract.
    if legacy_bytes != fastapi_bytes:
        diff_count = sum(1 for a, b in zip(legacy_bytes, fastapi_bytes) if a != b)
        diff_count += abs(len(legacy_bytes) - len(fastapi_bytes))
        pytest.fail(
            f"render-frame JPEG bytes differ: legacy={len(legacy_bytes)}B "
            f"fastapi={len(fastapi_bytes)}B diff_bytes={diff_count} "
            f"project={project} t={t} quality={quality}"
        )


def test_render_frame_cache_header_parity(dual_server):
    """render-frame carries ``X-Scenecraft-Cache: MISS|HIT`` header — legacy parity."""
    from scenecraft.render.frame_cache import global_cache

    global_cache.clear()
    resp = dual_server["fastapi"].get(
        "/api/projects/P1/render-frame", params={"t": 0.25, "quality": 85}
    )
    assert resp.status_code == 200
    assert resp.headers.get("x-scenecraft-cache") == "MISS"
    assert resp.headers.get("cache-control") == "no-store"

    resp2 = dual_server["fastapi"].get(
        "/api/projects/P1/render-frame", params={"t": 0.25, "quality": 85}
    )
    assert resp2.headers.get("x-scenecraft-cache") == "HIT"
    # Cached hit must be byte-identical to the first response.
    assert resp2.content == resp.content


def test_render_frame_bad_t_400(dual_server):
    """Invalid ``t`` query param returns 400 envelope (legacy parity)."""
    resp = dual_server["fastapi"].get(
        "/api/projects/P1/render-frame", params={"t": "not-a-number"}
    )
    assert resp.status_code == 400
    body = resp.json()
    assert body["error"] == "BAD_REQUEST"


def test_render_frame_unknown_project_404(dual_server):
    resp = dual_server["fastapi"].get(
        "/api/projects/does-not-exist/render-frame", params={"t": 0}
    )
    assert resp.status_code == 404
    body = resp.json()
    assert body["error"] == "NOT_FOUND"


# ---------------------------------------------------------------------------
# render-state / render-cache/stats
# ---------------------------------------------------------------------------


def test_render_state_route(dual_server):
    """GET /api/projects/{name}/render-state returns the worker snapshot shape."""
    resp = dual_server["fastapi"].get("/api/projects/P1/render-state")
    assert resp.status_code == 200
    body = resp.json()
    # snapshot_for_worker returns a dict — exact shape is defined by that
    # function, but it must be JSON-serializable and dict-like.
    assert isinstance(body, dict)


def test_render_cache_stats_route(dual_server):
    """GET /api/render-cache/stats returns frame_cache + fragment_cache stats."""
    resp = dual_server["fastapi"].get("/api/render-cache/stats")
    assert resp.status_code == 200
    body = resp.json()
    assert "frame_cache" in body
    assert "fragment_cache" in body


# ---------------------------------------------------------------------------
# descriptions
# ---------------------------------------------------------------------------


def test_get_descriptions_empty(dual_server):
    resp = dual_server["fastapi"].get("/api/projects/P1/descriptions")
    assert resp.status_code == 200
    assert resp.json() == {"sections": []}


def test_get_descriptions_parses_sections(dual_server):
    """descriptions.md is parsed into section objects."""
    pd = dual_server["project_dirs"]["P1"]
    (pd / "descriptions.md").write_text(
        "## Section 0 (verse, low_energy)\n"
        "**Time**: 0.0s - 16.0s\n\nBody text one.\n\n"
        "## Section 1 (chorus, high_energy)\n"
        "**Time**: 16.0s - 32.0s\n\nBody text two.\n"
    )
    resp = dual_server["fastapi"].get("/api/projects/P1/descriptions")
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["sections"]) == 2
    assert body["sections"][0]["sectionIndex"] == 0
    assert body["sections"][0]["startTime"] == 0.0
    assert body["sections"][0]["endTime"] == 16.0
    assert body["sections"][1]["sectionIndex"] == 1


# ---------------------------------------------------------------------------
# pool routes — empty-project shape parity
# ---------------------------------------------------------------------------


def test_get_pool_empty(dual_server):
    resp = dual_server["fastapi"].get("/api/projects/P1/pool")
    assert resp.status_code == 200
    body = resp.json()
    assert "keyframes" in body
    assert "segments" in body


def test_get_pool_tags_empty(dual_server):
    resp = dual_server["fastapi"].get("/api/projects/P1/pool/tags")
    assert resp.status_code == 200
    body = resp.json()
    assert "tags" in body
    assert isinstance(body["tags"], list)


def test_get_pool_gc_preview(dual_server):
    resp = dual_server["fastapi"].get("/api/projects/P1/pool/gc-preview")
    assert resp.status_code == 200
    body = resp.json()
    assert "wouldDelete" in body
    assert "segments" in body


def test_post_pool_import_missing_body(dual_server):
    """POST /pool/import with no sourcePath → 400."""
    resp = dual_server["fastapi"].post("/api/projects/P1/pool/import", json={})
    assert resp.status_code == 400
    assert resp.json()["error"] == "BAD_REQUEST"


def test_post_pool_import_roundtrip(dual_server, tmp_path):
    """POST /pool/import with a real local file creates a pool_segments row."""
    src = tmp_path / "local.mp4"
    # Copy the project's gradient video as a real file to import.
    pd = dual_server["project_dirs"]["P1"]
    shutil.copy2(str(pd / "selected_transitions/tr_001_slot_0.mp4"), str(src))

    resp = dual_server["fastapi"].post(
        "/api/projects/P1/pool/import",
        json={"sourcePath": str(src), "label": "gradient"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["success"] is True
    assert body["poolSegmentId"]
    assert body["poolPath"].startswith("pool/segments/import_")
    # File was copied.
    assert (pd / body["poolPath"]).exists()


def test_post_pool_rename_missing_body(dual_server):
    resp = dual_server["fastapi"].post("/api/projects/P1/pool/rename", json={})
    assert resp.status_code == 400
    assert resp.json()["error"] == "BAD_REQUEST"


def test_post_pool_tag_missing_body(dual_server):
    resp = dual_server["fastapi"].post("/api/projects/P1/pool/tag", json={})
    assert resp.status_code == 400
    assert resp.json()["error"] == "BAD_REQUEST"


# ---------------------------------------------------------------------------
# pool/upload — streaming multipart (large_upload_streams)
# ---------------------------------------------------------------------------


def test_large_upload_streams(tmp_path):
    """large-upload-streams — 200 MB multipart upload stays under RSS budget.

    The handler MUST stream chunks to disk rather than buffer the whole
    body in memory. We measure RSS with ``resource.getrusage`` before
    and after a REAL HTTP upload (uvicorn server + httpx streaming
    client) — NOT via TestClient, because TestClient's httpx transport
    reads the whole request body into memory before dispatch, polluting
    the RSS measurement with ~200 MB of client-side overhead.

    Two servers share one work_dir:
      * uvicorn in a background thread hosts the FastAPI app.
      * httpx streams the 200 MB file body via a generator.

    The handler uses starlette's ``SpooledTemporaryFile`` (1 MB spool
    threshold) + our 64 KiB chunked ``file.read()`` loop, so neither
    Python userspace nor starlette's parser ever holds the full body
    in memory. A buffering implementation would show ~200 MB+ delta.

    Skipped when ``resource`` is unavailable (non-POSIX).
    """
    try:
        import resource
    except ImportError:
        pytest.skip("resource module unavailable — non-POSIX platform")

    import socket
    import threading
    import time

    import httpx
    import uvicorn

    from scenecraft.api.app import create_app
    from scenecraft.db import _migrated_dbs, close_db

    # Build a work_dir with one project.
    work_dir = tmp_path / "work"
    work_dir.mkdir()
    pdir = _make_project(work_dir, "P1")

    try:
        # Build the 200 MB upload body.
        src = tmp_path / "big.bin"
        chunk = b"\x7f" * 65536
        target_size = 200 * 1024 * 1024  # 200 MB
        with src.open("wb") as f:
            written = 0
            while written < target_size:
                f.write(chunk)
                written += len(chunk)

        # Pick a free port.
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        sock.close()

        # Launch uvicorn in a thread. ``log_level="warning"`` silences
        # the access logs that would skew our timing.
        app = create_app(work_dir=work_dir)
        config = uvicorn.Config(
            app=app, host="127.0.0.1", port=port,
            log_level="warning", access_log=False,
        )
        server = uvicorn.Server(config)
        server_thread = threading.Thread(target=server.run, daemon=True)
        server_thread.start()

        # Wait for uvicorn to start listening.
        deadline = time.time() + 10.0
        while time.time() < deadline:
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                    break
            except OSError:
                time.sleep(0.05)
        else:
            server.should_exit = True
            pytest.fail("uvicorn never bound the test port")

        def rss_kb() -> int:
            return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss

        # Baseline + force GC so no old allocations taint the delta.
        import gc
        gc.collect()
        baseline = rss_kb()

        # Stream the body via multipart. httpx takes a file handle for
        # ``files=`` and dispatches it in chunks; the request body is
        # never fully loaded into memory.
        with src.open("rb") as f, httpx.Client(timeout=120.0) as hc:
            resp = hc.post(
                f"http://127.0.0.1:{port}/api/projects/P1/pool/upload",
                files={"file": ("big.bin", f, "application/octet-stream")},
                data={"label": "big"},
            )

        peak = rss_kb()
        delta_mb = (peak - baseline) / 1024  # ru_maxrss is KB on Linux

        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["success"] is True
        dest = pdir / body["poolPath"]
        assert dest.exists()
        assert dest.stat().st_size == target_size

        # Streaming budget: the peak RSS delta must stay bounded. We give
        # 80 MB of headroom for Python + httpx overhead; a buffering
        # implementation would show ~200 MB+ delta.
        assert delta_mb < 80, (
            f"Upload buffered in memory: RSS delta={delta_mb:.1f}MB exceeds streaming budget"
        )

        server.should_exit = True
        server_thread.join(timeout=5.0)
    finally:
        close_db(pdir)
        _migrated_dbs.discard(str(pdir / "project.db"))
        shutil.rmtree(work_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# candidates routes
# ---------------------------------------------------------------------------


def test_get_unselected_candidates_empty(dual_server):
    resp = dual_server["fastapi"].get("/api/projects/P1/unselected-candidates")
    assert resp.status_code == 200
    body = resp.json()
    assert "candidates" in body


def test_get_video_candidates_empty(dual_server):
    resp = dual_server["fastapi"].get("/api/projects/P1/video-candidates")
    assert resp.status_code == 200
    body = resp.json()
    assert "candidates" in body


def test_get_staging_missing(dual_server):
    """Non-existent staging dir returns ``{"candidates": []}`` (legacy parity)."""
    resp = dual_server["fastapi"].get("/api/projects/P1/staging/nosuchstaging")
    assert resp.status_code == 200
    assert resp.json() == {"candidates": []}


def test_post_promote_staged_missing_body(dual_server):
    resp = dual_server["fastapi"].post(
        "/api/projects/P1/promote-staged-candidate", json={}
    )
    assert resp.status_code == 400
    assert resp.json()["error"] == "BAD_REQUEST"


# ---------------------------------------------------------------------------
# effects routes
# ---------------------------------------------------------------------------


def test_get_effects_empty(dual_server):
    resp = dual_server["fastapi"].get("/api/projects/P1/effects")
    assert resp.status_code == 200
    body = resp.json()
    assert "effects" in body
    assert "suppressions" in body


def test_post_effects_roundtrip(dual_server):
    resp = dual_server["fastapi"].post(
        "/api/projects/P1/effects",
        json={"effects": [], "suppressions": []},
    )
    assert resp.status_code == 200
    assert resp.json() == {"success": True}


# ---------------------------------------------------------------------------
# assign-pool-video (structural — behind project_lock)
# ---------------------------------------------------------------------------


def test_assign_pool_video_missing_body(dual_server):
    resp = dual_server["fastapi"].post("/api/projects/P1/assign-pool-video", json={})
    assert resp.status_code == 400
    assert resp.json()["error"] == "BAD_REQUEST"


# ---------------------------------------------------------------------------
# OpenAPI operationIds — codegen-critical
# ---------------------------------------------------------------------------


def test_openapi_operation_ids_present(dual_server):
    """Every T63 route registers its contractual operationId.

    The T66 codegen pass generates ``chat_tools.py`` from these
    operationIds — renaming or omitting any breaks the chat-side tool
    surface. Keep this list in sync with the task file.
    """
    resp = dual_server["fastapi"].get("/openapi.json")
    assert resp.status_code == 200
    spec = resp.json()
    op_ids = {
        op["operationId"]
        for path in spec["paths"].values()
        for op in path.values()
        if isinstance(op, dict) and "operationId" in op
    }
    required = {
        # rendering.py
        "get_render_frame",
        "get_render_state",
        "get_render_cache_stats",
        "get_thumb",
        "get_thumbnail",
        "get_filmstrip",
        "download_preview",
        # files extension
        "get_descriptions",
        # pool.py
        "get_pool",
        "get_pool_tags",
        "pool_gc_preview",
        "get_pool_segment_peaks",
        "pool_add",
        "pool_import",
        "pool_upload",
        "pool_rename",
        "pool_tag",
        "pool_untag",
        "pool_gc",
        "assign_pool_video",
        # candidates.py
        "list_unselected_candidates",
        "list_video_candidates",
        "get_staging",
        "promote_staged_candidate",
        "generate_staged_candidate",
        # effects.py
        "list_effects",
        "upsert_effects",
    }
    missing = required - op_ids
    assert not missing, f"missing operationIds: {sorted(missing)}"
