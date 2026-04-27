"""M16 T59: Structural-mutation lock + post-handler timeline validator.

DEPRECATED by T65 — test-harness routes removed. These tests exercised
the structural lock via ``/api/test-harness/{name}/structural-a`` debug
routes. Real structural routes (``add-keyframe``, etc.) now prove the
same invariants — see ``test_fastapi_keyframes_transitions.py``.

Entire module skipped.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.skip(reason="T65: test-harness routes removed; covered by real structural route tests")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def work_dir(tmp_path: Path) -> Path:
    """Two projects side-by-side so per-project-lock isolation is testable."""
    root = tmp_path / "work"
    for name in ("P1", "P2"):
        (root / name).mkdir(parents=True)
    return root


@pytest.fixture()
def app(work_dir: Path):
    """Build the FastAPI app in testing-mode (mounts the harness router)."""
    from scenecraft.api.app import create_app

    app = create_app(work_dir=work_dir, testing=True)
    # Ensure a clean harness log for every test.
    from scenecraft.api.routers import test_harness

    test_harness._HARNESS_LOG.clear()
    test_harness._HANDLER_HOOKS.clear()
    return app


@pytest.fixture()
def client(app):
    """Sync ``TestClient`` for single-request tests."""
    from fastapi.testclient import TestClient

    return TestClient(app, raise_server_exceptions=False)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _harness_rows():
    from scenecraft.api.routers import test_harness

    return list(test_harness._HARNESS_LOG)


def _entry_exit(rows, name: str):
    entry = next(r for r in rows if r["project"] == name and r["phase"] == "enter")
    exit_ = next(r for r in rows if r["project"] == name and r["phase"] == "exit")
    return entry["ts"], exit_["ts"]


async def _post(app, url: str, json_body: dict | None = None):
    """Async POST via httpx.ASGITransport — lets asyncio.gather run routes concurrently.

    TestClient is sync and serializes requests on a single thread, which
    defeats the purpose of a concurrency test. ASGITransport dispatches
    straight into the ASGI app without a real socket, so gather() actually
    runs two requests at once.
    """
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
        return await ac.post(url, json=json_body or {})


# ---------------------------------------------------------------------------
# 1. Lock serializes same-project mutations
# ---------------------------------------------------------------------------


def test_structural_lock_serializes(app):
    """Two concurrent POSTs to the same project must serialize.

    Handler A sleeps 50 ms. If the lock works, B's entry timestamp
    must be >= A's exit timestamp (modulo a small scheduling fuzz).
    We assert the entry windows do NOT overlap.
    """
    from scenecraft.api.routers import test_harness

    test_harness._HANDLER_HOOKS["structural-a"] = lambda: time.sleep(0.05)

    async def runner():
        return await asyncio.gather(
            _post(app, "/api/test-harness/P1/structural-a", {"req": 1}),
            _post(app, "/api/test-harness/P1/structural-a", {"req": 2}),
        )

    r1, r2 = asyncio.run(runner())
    assert r1.status_code == 200, r1.text
    assert r2.status_code == 200, r2.text

    rows = _harness_rows()
    # Collect both (enter, exit) pairs ordered by entry time.
    enters = sorted([r for r in rows if r["phase"] == "enter"], key=lambda r: r["ts"])
    exits = sorted([r for r in rows if r["phase"] == "exit"], key=lambda r: r["ts"])
    assert len(enters) == 2
    assert len(exits) == 2

    first_enter, second_enter = enters
    first_exit = exits[0]

    # Second handler must enter at or after the first handler exits.
    # Allow 1 ms tolerance for scheduler jitter on slow runners.
    assert second_enter["ts"] >= first_exit["ts"] - 0.001, (
        f"Lock failed to serialize — second entered at {second_enter['ts']} "
        f"before first exited at {first_exit['ts']}"
    )
    # And the total span should be ≥ 2 * 50 ms (proves both actually ran).
    span = exits[-1]["ts"] - first_enter["ts"]
    assert span >= 0.09, f"Span too short ({span * 1000:.1f} ms) — did handlers actually sleep?"


# ---------------------------------------------------------------------------
# 2. Lock is per-project — different projects run in parallel
# ---------------------------------------------------------------------------


def test_structural_lock_is_per_project(app):
    """POSTs on P1 and P2 must overlap (different locks)."""
    from scenecraft.api.routers import test_harness

    test_harness._HANDLER_HOOKS["structural-a"] = lambda: time.sleep(0.05)

    async def runner():
        return await asyncio.gather(
            _post(app, "/api/test-harness/P1/structural-a"),
            _post(app, "/api/test-harness/P2/structural-a"),
        )

    r1, r2 = asyncio.run(runner())
    assert r1.status_code == 200
    assert r2.status_code == 200

    rows = _harness_rows()
    p1_enter, p1_exit = _entry_exit(rows, "P1")
    p2_enter, p2_exit = _entry_exit(rows, "P2")

    # Overlap: one project enters before the other exits.
    assert p2_enter < p1_exit, (
        f"P2 entered at {p2_enter} but P1 didn't exit until {p1_exit} — "
        "per-project lock is incorrectly global"
    )
    assert p1_enter < p2_exit


# ---------------------------------------------------------------------------
# 3. Timeline validator runs after mutation and broadcasts warnings
# ---------------------------------------------------------------------------


def test_timeline_validator_runs_after_mutation(app, work_dir, client, monkeypatch):
    """Structural POST on project with a project.db triggers the validator,
    and warnings propagate through ``job_manager._broadcast`` with the
    legacy envelope ``{type, route, warnings}``.
    """
    # Make the project look real so the validator runs (it short-circuits
    # when project.db is missing).
    (work_dir / "P1" / "project.db").write_bytes(b"sqlite-stub")

    calls = []

    def fake_validate(project_dir: Path):
        calls.append(project_dir)
        return ["duplicate-bridge-at-00:05"]

    broadcasts = []

    def fake_broadcast(self, message):  # bound-method signature to match _broadcast
        broadcasts.append(message)

    import scenecraft.db as _db_mod
    from scenecraft.ws_server import job_manager

    monkeypatch.setattr(_db_mod, "validate_timeline", fake_validate)
    monkeypatch.setattr(job_manager.__class__, "_broadcast", fake_broadcast)

    resp = client.post("/api/test-harness/P1/structural-a", json={})
    assert resp.status_code == 200, resp.text

    assert calls == [work_dir / "P1"], f"validator called with {calls}"
    assert any(
        msg.get("type") == "timeline_warning"
        and msg.get("route") == "structural-a"
        and msg.get("warnings") == ["duplicate-bridge-at-00:05"]
        for msg in broadcasts
    ), f"broadcasts={broadcasts}"


# ---------------------------------------------------------------------------
# 4. Validator exception must NOT fail the request
# ---------------------------------------------------------------------------


def test_validator_exception_non_fatal(app, work_dir, client, monkeypatch, caplog):
    (work_dir / "P1" / "project.db").write_bytes(b"sqlite-stub")

    def boom(project_dir):
        raise ValueError("boom")

    import scenecraft.db as _db_mod

    monkeypatch.setattr(_db_mod, "validate_timeline", boom)

    # Capture logs at INFO level so _log() output lands in caplog.
    import logging
    caplog.set_level(logging.INFO)

    # Capture anything _log() writes to stderr too, since _log prints
    # via print(..., file=sys.stderr). We rely on the dependency's own
    # "Validation error: <msg>" being logged somewhere observable.
    import io, contextlib
    buf = io.StringIO()
    with contextlib.redirect_stderr(buf):
        resp = client.post("/api/test-harness/P1/structural-a", json={})

    assert resp.status_code == 200, resp.text
    err_stream = buf.getvalue()
    assert "Validation error: boom" in err_stream or "boom" in caplog.text, (
        f"Expected 'Validation error: boom' in stderr or caplog, got stderr={err_stream!r} caplog={caplog.text!r}"
    )


# ---------------------------------------------------------------------------
# 5. Lock released even when the handler itself raises
# ---------------------------------------------------------------------------


def test_lock_released_on_exception(app, client):
    from scenecraft.api.routers import test_harness

    # First call: raise. Second call: succeed. If lock leaks, second hangs.
    counter = {"n": 0}

    def hook():
        counter["n"] += 1
        if counter["n"] == 1:
            raise RuntimeError("handler-boom")

    test_harness._HANDLER_HOOKS["structural-a"] = hook

    r1 = client.post("/api/test-harness/P1/structural-a", json={})
    assert r1.status_code == 500, r1.text

    # Second call within a short time budget — if the lock is held, this hangs
    # and we'll see a test-timeout failure instead of a clean 200.
    t0 = time.monotonic()
    r2 = client.post("/api/test-harness/P1/structural-a", json={})
    elapsed = time.monotonic() - t0
    assert r2.status_code == 200, r2.text
    assert elapsed < 0.1, f"Second POST took {elapsed * 1000:.0f} ms — lock likely leaked"


# ---------------------------------------------------------------------------
# 6. Lock released even when the validator raises
# ---------------------------------------------------------------------------


def test_validator_exception_lock_released(app, work_dir, client, monkeypatch):
    (work_dir / "P1" / "project.db").write_bytes(b"sqlite-stub")

    def boom(project_dir):
        raise ValueError("validator-boom")

    import scenecraft.db as _db_mod

    monkeypatch.setattr(_db_mod, "validate_timeline", boom)

    r1 = client.post("/api/test-harness/P1/structural-a", json={})
    assert r1.status_code == 200

    t0 = time.monotonic()
    r2 = client.post("/api/test-harness/P1/structural-a", json={})
    elapsed = time.monotonic() - t0
    assert r2.status_code == 200
    assert elapsed < 0.1, f"Second POST took {elapsed * 1000:.0f} ms — validator exception leaked lock"


# ---------------------------------------------------------------------------
# Sanity: the harness router really is gated on testing=True
# ---------------------------------------------------------------------------


def test_harness_routes_are_testing_only(work_dir):
    from fastapi.testclient import TestClient
    from scenecraft.api.app import create_app

    prod = create_app(work_dir=work_dir)  # testing defaults to False
    c = TestClient(prod, raise_server_exceptions=False)
    resp = c.post("/api/test-harness/P1/structural-a", json={})
    # 404 because the route was never mounted.
    assert resp.status_code == 404
