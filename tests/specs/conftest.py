"""Shared fixtures for M18 spec-locked regression tests.

Seeded by task-70 (engine-connection-and-transactions). Every subsequent M18
task (T71..T87) reuses these fixtures; do not duplicate them in per-test
conftests. Extend here.

Scope summary:
- `project_dir` (function): fresh, isolated temp project directory.
- `db_conn` (function): `get_db(project_dir)` handle on the main project DB,
  guaranteed closed at teardown.
- `thread_pool` (function): bounded ThreadPoolExecutor for concurrency tests.
- `thread_factory` (function): helper to spawn/join one-off `threading.Thread`s.
- `close_all_connections` (autouse, function): safety net — after every test,
  close any lingering memoized connections to prevent state leakage across
  tests (the connection pool is module-level and survives individual tests
  until R21's `threading.local()` refactor lands).
- `engine_server` (function): stub — tasks that need a real HTTP/WS server
  (T75-T87) override this in their own file.
"""
from __future__ import annotations

import concurrent.futures
import threading
from pathlib import Path
from typing import Callable, List

import pytest

from scenecraft import db as scdb


@pytest.fixture
def project_dir(tmp_path: Path) -> Path:
    """Fresh per-test project directory. Reused by all M18 DAO tests."""
    p = tmp_path / "proj"
    p.mkdir()
    return p


@pytest.fixture
def db_conn(project_dir: Path):
    """Main-project-DB connection for the current thread. Closed on teardown."""
    conn = scdb.get_db(project_dir)
    try:
        yield conn
    finally:
        scdb.close_db(project_dir)


@pytest.fixture
def thread_pool():
    """Bounded ThreadPoolExecutor for concurrency tests. Max 8 workers."""
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
        yield ex


@pytest.fixture
def thread_factory():
    """Spawn ad-hoc `threading.Thread`s and ensure they are joined at teardown.

    Returns a callable `(target, *args, **kwargs) -> threading.Thread`.
    All spawned threads are joined (with a small timeout) at teardown to
    prevent zombie threads from leaking connection-pool entries.
    """
    threads: List[threading.Thread] = []

    def _spawn(target: Callable, *args, **kwargs) -> threading.Thread:
        t = threading.Thread(target=target, args=args, kwargs=kwargs, daemon=True)
        threads.append(t)
        t.start()
        return t

    yield _spawn

    for t in threads:
        t.join(timeout=5.0)


_ENGINE_SERVER_WORK_DIR: Path | None = None


@pytest.fixture(autouse=True)
def close_all_connections(tmp_path: Path, request):
    """Autouse safety net.

    After each test, wipe `_connections` and `_migrated_dbs`. The pool is
    module-level state today (R21 transitional — target is `threading.local()`)
    so without this cleanup, later tests would observe memoized connections
    and pre-migrated flags from earlier tests.

    Exception: if the test uses the session-scoped `engine_server` fixture,
    skip the wipe for connections belonging to the live server's work_dir —
    those conns are owned by in-flight worker threads and closing them would
    crash the server. We still wipe any OTHER entries and the migration flag
    for non-server paths.
    """
    yield
    with scdb._conn_lock:
        skip_prefix = None
        if _ENGINE_SERVER_WORK_DIR is not None and "engine_server" in request.fixturenames:
            skip_prefix = str(_ENGINE_SERVER_WORK_DIR)
        keys_to_remove = []
        for key, conn in list(scdb._connections.items()):
            if skip_prefix and key.startswith(skip_prefix):
                continue
            try:
                conn.close()
            except Exception:
                pass
            keys_to_remove.append(key)
        for k in keys_to_remove:
            scdb._connections.pop(k, None)
        # Only clear migrated flags for paths we're closing.
        if skip_prefix:
            scdb._migrated_dbs = {
                p for p in scdb._migrated_dbs if p.startswith(skip_prefix)
            }
        else:
            scdb._migrated_dbs.clear()


@pytest.fixture(scope="session")
def engine_server(tmp_path_factory):
    """Live server fixture for e2e tests — FastAPI via TestClient.

    Provides the same interface as the legacy HTTPServer fixture:
      - `.base_url`  : "http://testserver" (TestClient's default)
      - `.work_dir`  : temp work_dir (session-scoped)
      - `.request(method, path, body=None, timeout=10)`
                     : returns (status, headers_dict, body_bytes)
      - `.json(method, path, body=None, timeout=10)`
                     : returns (status, parsed_json)
    """
    import json as _json

    global _ENGINE_SERVER_WORK_DIR
    work_dir = tmp_path_factory.mktemp("engine_server_workdir")
    _ENGINE_SERVER_WORK_DIR = work_dir

    from fastapi.testclient import TestClient
    from scenecraft.api.app import create_app

    app = create_app(work_dir=work_dir, enable_docs=True, testing=True)
    client = TestClient(app, raise_server_exceptions=False)

    class _Server:
        def __init__(self):
            self.base_url = "http://testserver"
            self.work_dir = work_dir
            self._client = client

        def request(self, method: str, path: str, body=None, timeout: float = 10.0):
            kwargs = {"timeout": timeout}
            if body is not None:
                kwargs["json"] = body
            resp = self._client.request(method, path, **kwargs)
            headers = dict(resp.headers)
            return resp.status_code, headers, resp.content

        def json(self, method: str, path: str, body=None, timeout: float = 10.0):
            status, _headers, raw = self.request(method, path, body=body, timeout=timeout)
            if not raw:
                return status, None
            try:
                return status, _json.loads(raw.decode("utf-8"))
            except Exception:
                return status, raw

    yield _Server()


@pytest.fixture
def project_name(engine_server):
    """Create a fresh project on the running engine_server; return its name.

    Uses a counter + pid so parallel or repeated tests never collide.
    """
    import json as _json
    import os
    import uuid

    name = f"proj_{os.getpid()}_{uuid.uuid4().hex[:8]}"
    status, body = engine_server.json(
        "POST", "/api/projects/create", {"name": name}
    )
    assert status == 200, f"project create failed: {status} {body!r}"
    return name
