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


@pytest.fixture(autouse=True)
def close_all_connections(tmp_path: Path):
    """Autouse safety net.

    After each test, wipe `_connections` and `_migrated_dbs`. The pool is
    module-level state today (R21 transitional — target is `threading.local()`)
    so without this cleanup, later tests would observe memoized connections
    and pre-migrated flags from earlier tests.
    """
    yield
    with scdb._conn_lock:
        for conn in list(scdb._connections.values()):
            try:
                conn.close()
            except Exception:
                pass
        scdb._connections.clear()
        scdb._migrated_dbs.clear()


@pytest.fixture
def engine_server():
    """Stub — overridden by e2e tasks that need a real HTTP/WS server.

    Tasks T75-T87 install a real fixture in their own file or extend this
    conftest. Until then, tests that request it skip cleanly.
    """
    pytest.skip("engine_server fixture not installed for this test")
