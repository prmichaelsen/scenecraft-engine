"""M16 T61 — keyframes + transitions routers.

Named tests from the task "Tests to Pass" section:

    - get_route_parity          (keyframes/transitions slice)  — see notes
    - post_route_parity         (keyframes/transitions slice)
    - structural_lock_serializes                               (real routes)
    - structural_lock_is_per_project                           (real routes)

Plus smoke coverage for:
    - operation_ids registered + chat-tool-aligned
    - missing-field validation emits legacy envelope
    - batch_delete_transitions is a new REST surface

``get_route_parity`` is declared N/A by the task (no GETs in this slice —
the keyframes GET lives in T60's projects router). We leave a
placeholder test that asserts no GET routes from this slice are in
``keyframes.router`` / ``transitions.router`` so reviewers can see the
"skip" was intentional.
"""

from __future__ import annotations

import asyncio
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def work_dir(tmp_path: Path) -> Path:
    """Create two project dirs with minimal ``project.db`` schema.

    ``add_keyframe`` and friends need ``get_db(project_dir)`` to work —
    which runs ``_ensure_schema``. That's enough to exercise the full
    handler for real parity assertions.
    """
    root = tmp_path / "work"
    from scenecraft.db import close_db, get_db

    for name in ("P1", "P2"):
        pdir = root / name
        pdir.mkdir(parents=True)
        # Initialize schema so handlers that call get_keyframes don't explode.
        # Use close_db — ``conn.close()`` alone would leave a dead handle in
        # the per-thread pool and the first real request would ``sqlite3.
        # ProgrammingError("Cannot operate on a closed database")``.
        get_db(pdir)
        close_db(pdir)
    return root


@pytest.fixture()
def app(work_dir: Path) -> FastAPI:
    from scenecraft.api.app import create_app

    # testing=False so the harness routes aren't mounted — we want real
    # structural routes to drive the lock tests.
    return create_app(work_dir=work_dir, testing=False)


@pytest.fixture()
def client(app: FastAPI) -> TestClient:
    return TestClient(app, raise_server_exceptions=False)


# ---------------------------------------------------------------------------
# Route registration — operation_ids
# ---------------------------------------------------------------------------

# Tuple order is meaningful only for readability: structural (🔒) first within
# each router, non-structural after.
KEYFRAME_OPERATION_IDS = frozenset(
    {
        "add_keyframe",
        "duplicate_keyframe",
        "paste_group",
        "delete_keyframe",
        "batch_delete_keyframes",
        "restore_keyframe",
        "insert_pool_item",
        "select_keyframes",
        "select_slot_keyframes",
        "update_keyframe_timestamp",
        "update_keyframe_prompt",
        "batch_set_base_image",
        "set_base_image",
        "unlink_keyframe",
        "escalate_keyframe",
        "update_keyframe_label",
        "update_keyframe_style",
        "assign_keyframe_image",
        "generate_keyframe_variations",
        "generate_keyframe_candidates",
        "generate_slot_keyframe_candidates",
        "suggest_keyframe_prompts",
        "enhance_keyframe_prompt",
        "update_keyframe",
    }
)

TRANSITION_OPERATION_IDS = frozenset(
    {
        "delete_transition",
        "restore_transition",
        "split_transition",
        "batch_delete_transitions",  # NEW in T61
        "select_transitions",
        "update_transition_trim",
        "clip_trim_edge",
        "move_transitions",
        "update_transition_action",
        "update_transition_remap",
        "generate_transition_action",
        "enhance_transition_action",
        "update_transition_style",
        "update_transition_label",
        "copy_transition_style",
        "duplicate_transition_video",
        "generate_transition_candidates",
        "link_transition_audio",
        "add_transition_effect",
        "update_transition_effect",
        "delete_transition_effect",
        "update_transition",
    }
)

STRUCTURAL_KEYFRAMES = {
    "add-keyframe",
    "duplicate-keyframe",
    "paste-group",
    "delete-keyframe",
    "batch-delete-keyframes",
    "restore-keyframe",
    "insert-pool-item",
}

STRUCTURAL_TRANSITIONS = {
    "delete-transition",
    "restore-transition",
    "split-transition",
    "batch-delete-transitions",
}


def _collect_operation_ids(app: FastAPI) -> set[str]:
    out = set()
    for route in app.routes:
        oid = getattr(route, "operation_id", None)
        if oid:
            out.add(oid)
    return out


def test_keyframe_operation_ids_registered(app: FastAPI):
    """post-route-parity (keyframes slice) — every operation_id is mounted."""
    ids = _collect_operation_ids(app)
    missing = KEYFRAME_OPERATION_IDS - ids
    assert not missing, f"Missing keyframe operation_ids: {sorted(missing)}"


def test_transition_operation_ids_registered(app: FastAPI):
    """post-route-parity (transitions slice) — every operation_id is mounted."""
    ids = _collect_operation_ids(app)
    missing = TRANSITION_OPERATION_IDS - ids
    assert not missing, f"Missing transition operation_ids: {sorted(missing)}"


def test_chat_tool_aligned_operation_ids(app: FastAPI):
    """Per task § 4 — chat tool names are load-bearing for T67."""
    ids = _collect_operation_ids(app)
    # These names MUST exist verbatim:
    required = {
        "update_keyframe_prompt",    # route is /update-prompt
        "update_keyframe_timestamp", # route is /update-timestamp
        "delete_keyframe",
        "delete_transition",
        "batch_delete_keyframes",
        "batch_delete_transitions",  # new
        "add_keyframe",
        "update_keyframe",
        "update_transition",
        "split_transition",
        "assign_keyframe_image",
        "generate_keyframe_candidates",
        "generate_transition_candidates",
    }
    missing = required - ids
    assert not missing, f"Chat-tool-aligned operation_ids missing: {sorted(missing)}"


# ---------------------------------------------------------------------------
# get-route-parity — marked N/A by task; sanity check: no GETs in this slice
# ---------------------------------------------------------------------------


def test_no_get_routes_in_this_slice():
    """get-route-parity — N/A. All T61 routes are POST."""
    from scenecraft.api.routers import keyframes as _k, transitions as _t

    for router in (_k.router, _t.router):
        for route in router.routes:
            methods = getattr(route, "methods", set())
            assert methods <= {"POST"} or methods <= {"HEAD", "GET", "OPTIONS"} and False, (
                f"T61 routers should only POST: {route.path} has {methods}"
            )


# ---------------------------------------------------------------------------
# Structural lock — real routes
# ---------------------------------------------------------------------------


async def _async_post(
    app: FastAPI, url: str, json_body: dict[str, Any] | None = None
) -> httpx.Response:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
        return await ac.post(url, json=json_body or {})


def test_structural_lock_serializes_real_route(app: FastAPI, monkeypatch):
    """structural-lock-serializes — two add-keyframe POSTs on the same project serialize.

    We monkey-patch the legacy handler's ``_handle_add_keyframe`` with a
    stub that sleeps 50 ms and records its lifecycle. The dependency
    ``project_lock`` must serialize the two calls — otherwise their
    entry-windows overlap.
    """
    events: list[tuple[str, float]] = []
    lock = threading.Lock()

    def _rec(phase: str) -> None:
        with lock:
            events.append((phase, time.monotonic()))

    from scenecraft import api_server as _legacy

    original_make_handler = _legacy.make_handler

    def _patched_make_handler(work_dir, no_auth=False):
        cls = original_make_handler(work_dir, no_auth=no_auth)

        def _stub_add(self, project_name):
            _rec("enter")
            try:
                time.sleep(0.05)
            finally:
                _rec("exit")
            self._json_response({"success": True, "stub": True})

        cls._handle_add_keyframe = _stub_add
        return cls

    monkeypatch.setattr(_legacy, "make_handler", _patched_make_handler)

    async def runner():
        body = {"timestamp": "0:01.00"}
        return await asyncio.gather(
            _async_post(app, "/api/projects/P1/add-keyframe", body),
            _async_post(app, "/api/projects/P1/add-keyframe", body),
        )

    r1, r2 = asyncio.run(runner())
    assert r1.status_code == 200, r1.text
    assert r2.status_code == 200, r2.text

    enters = [ts for phase, ts in events if phase == "enter"]
    exits_ = [ts for phase, ts in events if phase == "exit"]
    assert len(enters) == 2 and len(exits_) == 2

    enters.sort()
    exits_.sort()
    # Second entry must NOT precede first exit — serialization proof.
    assert enters[1] >= exits_[0] - 0.001, (
        f"Second handler entered at {enters[1]:.3f} before first exited at "
        f"{exits_[0]:.3f} — structural lock failed on real route"
    )


def test_structural_lock_is_per_project_real_route(app: FastAPI, monkeypatch):
    """structural-lock-is-per-project — P1 + P2 add-keyframe must overlap."""
    events: list[tuple[str, str, float]] = []
    lock = threading.Lock()

    def _rec(project: str, phase: str) -> None:
        with lock:
            events.append((project, phase, time.monotonic()))

    from scenecraft import api_server as _legacy

    original_make_handler = _legacy.make_handler

    def _patched_make_handler(work_dir, no_auth=False):
        cls = original_make_handler(work_dir, no_auth=no_auth)

        def _stub_add(self, project_name):
            _rec(project_name, "enter")
            try:
                time.sleep(0.05)
            finally:
                _rec(project_name, "exit")
            self._json_response({"success": True})

        cls._handle_add_keyframe = _stub_add
        return cls

    monkeypatch.setattr(_legacy, "make_handler", _patched_make_handler)

    async def runner():
        body = {"timestamp": "0:01.00"}
        return await asyncio.gather(
            _async_post(app, "/api/projects/P1/add-keyframe", body),
            _async_post(app, "/api/projects/P2/add-keyframe", body),
        )

    r1, r2 = asyncio.run(runner())
    assert r1.status_code == 200
    assert r2.status_code == 200

    by_project: dict[str, list[tuple[str, float]]] = {}
    for project, phase, ts in events:
        by_project.setdefault(project, []).append((phase, ts))

    p1_enter = next(ts for ph, ts in by_project["P1"] if ph == "enter")
    p1_exit = next(ts for ph, ts in by_project["P1"] if ph == "exit")
    p2_enter = next(ts for ph, ts in by_project["P2"] if ph == "enter")
    p2_exit = next(ts for ph, ts in by_project["P2"] if ph == "exit")

    # The two handlers must overlap — different projects = different locks.
    assert p2_enter < p1_exit, "P2 waited for P1 — lock is global, not per-project"
    assert p1_enter < p2_exit


# ---------------------------------------------------------------------------
# Validation envelope (T58 integration)
# ---------------------------------------------------------------------------


def test_add_keyframe_missing_timestamp_returns_400(client: TestClient):
    """Missing required field → 400 with legacy ``Missing 'timestamp'`` envelope."""
    resp = client.post("/api/projects/P1/add-keyframe", json={})
    assert resp.status_code == 400, resp.text
    body = resp.json()
    assert body["error"] == "BAD_REQUEST"
    assert body["message"] == "Missing 'timestamp'"


def test_delete_keyframe_missing_field_returns_400(client: TestClient):
    resp = client.post("/api/projects/P1/delete-keyframe", json={})
    assert resp.status_code == 400
    body = resp.json()
    assert body["error"] == "BAD_REQUEST"
    assert body["message"] == "Missing 'keyframeId'"


def test_split_transition_missing_field_returns_400(client: TestClient):
    resp = client.post("/api/projects/P1/split-transition", json={})
    assert resp.status_code == 400
    body = resp.json()
    assert body["error"] == "BAD_REQUEST"
    assert body["message"] == "Missing 'transitionId'"


def test_unknown_project_returns_404(client: TestClient):
    """project_dir dep rejects nonexistent projects with NOT_FOUND envelope."""
    resp = client.post(
        "/api/projects/does_not_exist/add-keyframe", json={"timestamp": "0:01.00"}
    )
    assert resp.status_code == 404
    body = resp.json()
    assert body["error"] == "NOT_FOUND"
    assert "does_not_exist" in body["message"]


# ---------------------------------------------------------------------------
# batch_delete_transitions — NEW REST route sanity check
# ---------------------------------------------------------------------------


def test_batch_delete_transitions_rejects_empty_list(client: TestClient):
    """Empty transition_ids → 400 via chat.py error wrapper."""
    resp = client.post(
        "/api/projects/P1/batch-delete-transitions", json={"transition_ids": []}
    )
    assert resp.status_code == 400, resp.text
    body = resp.json()
    # The wrapped error message comes from _exec_batch_delete_transitions;
    # T58's validation handler doesn't rewrite it because Pydantic accepts
    # the empty list — the handler raises ApiError manually.
    # Alternatively, Pydantic may reject empty list if we'd added min_length,
    # but we mirror legacy which accepts the payload and 400s inside the
    # chat-tool helper. Assert either envelope shape.
    assert body.get("error") in ("BAD_REQUEST", "BAD_REQUEST")


def test_batch_delete_transitions_deletes_when_rows_exist(
    client: TestClient, work_dir: Path
):
    """Happy path: add two transitions via raw SQL, batch-delete, verify soft-delete."""
    from scenecraft.db import add_transition, get_db, next_transition_id

    pdir = work_dir / "P1"
    ids = []
    for _ in range(2):
        tid = next_transition_id(pdir)
        add_transition(
            pdir,
            {
                "id": tid,
                "from": "kf_001",
                "to": "kf_002",
                "duration_seconds": 1.0,
                "slots": 1,
                "selected": None,
                "remap": {"method": "linear", "target_duration": 1.0},
                "track_id": "track_1",
            },
        )
        ids.append(tid)

    resp = client.post(
        "/api/projects/P1/batch-delete-transitions", json={"transition_ids": ids}
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["deleted_count"] == 2
    assert body["skipped_count"] == 0


# ---------------------------------------------------------------------------
# Post-parity sanity: a representative happy-path add-keyframe
# ---------------------------------------------------------------------------


def test_update_keyframe_label_inline_handler(client: TestClient, work_dir: Path):
    """Inline handler (no _handle_* method) reachable through dispatch_legacy_path.

    Adds a keyframe, then updates its label through the FastAPI route. The
    legacy handler body is inlined inside ``_do_POST``; the proxy needs to
    hit it via path dispatch rather than direct method call. Regression
    guard: breaks if the router forgets to use ``dispatch_legacy_path``.
    """
    # First create a keyframe so update-label has a row to target.
    r1 = client.post(
        "/api/projects/P1/add-keyframe", json={"timestamp": "0:03.00"}
    )
    assert r1.status_code == 200, r1.text
    kf_id = r1.json()["keyframe"]["id"]

    r2 = client.post(
        "/api/projects/P1/update-keyframe-label",
        json={"keyframeId": kf_id, "label": "hero", "labelColor": "#f00"},
    )
    assert r2.status_code == 200, r2.text
    assert r2.json()["success"] is True


def test_transition_effect_add_inline_handler(client: TestClient, work_dir: Path):
    """Effects routes are inlined in ``_do_POST``. Verify parity via REST."""
    # Build a transition first via raw SQL.
    from scenecraft.db import add_transition, next_transition_id

    pdir = work_dir / "P1"
    tid = next_transition_id(pdir)
    add_transition(
        pdir,
        {
            "id": tid,
            "from": "kf_001",
            "to": "kf_002",
            "duration_seconds": 1.0,
            "slots": 1,
            "selected": None,
            "remap": {"method": "linear", "target_duration": 1.0},
            "track_id": "track_1",
        },
    )

    resp = client.post(
        "/api/projects/P1/transition-effects/add",
        json={"transitionId": tid, "type": "feather", "params": {"radius": 4}},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["success"] is True
    assert "id" in body


def test_add_keyframe_happy_path_persists_row(client: TestClient, work_dir: Path):
    """add_keyframe (structural) end-to-end: response ok + row in DB."""
    resp = client.post(
        "/api/projects/P1/add-keyframe",
        json={"timestamp": "0:02.00", "section": "a", "prompt": "hi"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["success"] is True
    assert body["keyframe"]["timestamp"] == "0:02.00"

    # Verify the DB actually got the row.
    conn = sqlite3.connect(str(work_dir / "P1" / "project.db"))
    try:
        row = conn.execute(
            "SELECT timestamp FROM keyframes WHERE timestamp = ?", ("0:02.00",)
        ).fetchone()
    finally:
        conn.close()
    assert row is not None, "add-keyframe did not persist to DB"
