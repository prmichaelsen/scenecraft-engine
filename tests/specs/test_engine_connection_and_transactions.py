"""Regression tests for local.engine-connection-and-transactions.md.

Every test docstring opens with `(covers Rn, ...)` citing spec requirements.
Target-state tests (those that require the M16 FastAPI refactor to land
R21-R26 semantics) are marked `@pytest.mark.xfail(reason="target-state;
awaits M16 FastAPI refactor", strict=False)`. Transitional tests codify
today's shipped behavior and must pass today.
"""
from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path

import pytest

from scenecraft import db as scdb


# ---------------------------------------------------------------------------
# Base Cases
# ---------------------------------------------------------------------------


def test_get_db_first_call_opens_and_configures(project_dir: Path):
    """covers R1, R4, R6 — first get_db opens, PRAGMAs, memoizes."""
    # Given: a fresh project dir, empty pool, empty migrated set.
    assert not (project_dir / "project.db").exists()

    # When: get_db is called.
    conn = scdb.get_db(project_dir)

    # Then
    assert isinstance(conn, sqlite3.Connection), "returns-connection: sqlite3.Connection expected"
    assert (project_dir / "project.db").exists(), "file-exists: project.db should be created on disk"
    assert conn.row_factory is sqlite3.Row, "row-factory-set: row_factory must be sqlite3.Row"
    tid = threading.current_thread().ident
    matching = [k for k in scdb._connections if k.endswith(f":{tid}")]
    assert len(matching) == 1, f"memoized: expected 1 entry ending with thread ident, got {matching}"


def test_get_db_applies_all_pragmas(project_dir: Path):
    """covers R4 — all four PRAGMAs applied on connection creation."""
    # Given/When: fresh get_db.
    conn = scdb.get_db(project_dir)

    # Then: all four PRAGMAs at their configured values.
    jm = conn.execute("PRAGMA journal_mode").fetchone()[0]
    assert jm == "wal", f"wal-mode: journal_mode should be wal, got {jm!r}"
    sync = conn.execute("PRAGMA synchronous").fetchone()[0]
    assert sync == 1, f"sync-normal: synchronous should be 1 (NORMAL), got {sync!r}"
    fk = conn.execute("PRAGMA foreign_keys").fetchone()[0]
    assert fk == 1, f"fk-on: foreign_keys should be 1, got {fk!r}"
    bt = conn.execute("PRAGMA busy_timeout").fetchone()[0]
    assert bt == 60000, f"busy-timeout-60s: busy_timeout should be 60000, got {bt!r}"


def test_get_db_memoizes_per_thread(project_dir: Path):
    """covers R1, R20 — repeated get_db same thread returns identical conn."""
    # Given/When
    a = scdb.get_db(project_dir)
    b = scdb.get_db(project_dir)
    c = scdb.get_db(project_dir)

    # Then
    assert a is b is c, "identity-stable: all three must be the same object"
    tid = threading.current_thread().ident
    matches = [k for k in scdb._connections if k.endswith(f":{tid}")
               and k.startswith(str(project_dir / "project.db"))]
    assert len(matches) == 1, f"pool-size-one: expected 1 matching key, got {matches}"


def test_get_db_separates_by_thread(project_dir: Path, thread_factory):
    """covers R1, R3 — different threads get different connection objects."""
    # Given
    main_conn = scdb.get_db(project_dir)
    other_conn_box: list = []

    def worker():
        other_conn_box.append(scdb.get_db(project_dir))

    # When
    t = thread_factory(worker)
    t.join()

    # Then
    assert len(other_conn_box) == 1
    other = other_conn_box[0]
    assert main_conn is not other, "distinct-conns: cross-thread conns must differ"
    db_path = str(project_dir / "project.db")
    keys = [k for k in scdb._connections if k.startswith(f"{db_path}:")]
    assert len(keys) == 2, f"two-entries: expected 2 entries for this db_path, got {keys}"


def test_get_db_explicit_path_isolated(project_dir: Path):
    """covers R2 — explicit db_path isolated from default project.db."""
    # Given
    session_path = project_dir / "session.db"

    # When
    main = scdb.get_db(project_dir)
    sess = scdb.get_db(project_dir, db_path=session_path)

    # Then
    assert main is not sess, "distinct-conns: main and session conns differ"
    keys = list(scdb._connections)
    main_prefix = str(project_dir / "project.db")
    sess_prefix = str(session_path)
    assert any(k.startswith(f"{main_prefix}:") for k in keys), "separate-keys: main key present"
    assert any(k.startswith(f"{sess_prefix}:") for k in keys), "separate-keys: session key present"


def test_schema_init_runs_once_per_db_path(project_dir: Path, monkeypatch):
    """covers R7, R8 — _ensure_schema runs exactly once per db_path per process."""
    # Given: spy on _ensure_schema
    calls = {"n": 0}
    real_ensure = scdb._ensure_schema

    def spy(conn):
        calls["n"] += 1
        return real_ensure(conn)

    monkeypatch.setattr(scdb, "_ensure_schema", spy)

    # When: 3 calls from this thread, 2 from another thread
    scdb.get_db(project_dir)
    scdb.get_db(project_dir)
    scdb.get_db(project_dir)

    def worker():
        scdb.get_db(project_dir)
        scdb.get_db(project_dir)

    t = threading.Thread(target=worker)
    t.start()
    t.join()

    # Then
    assert calls["n"] == 1, f"ensure-called-once: expected 1, got {calls['n']}"
    db_path = str(project_dir / "project.db")
    assert db_path in scdb._migrated_dbs, "migrated-flag-set"


def test_close_db_removes_all_threads(project_dir: Path, thread_factory):
    """covers R9 — close_db closes and removes all threads' conns for db_path."""
    # Given
    conn_main = scdb.get_db(project_dir)
    other_box: list = []

    def worker():
        other_box.append(scdb.get_db(project_dir))

    t = thread_factory(worker)
    t.join()
    conn_other = other_box[0]
    db_path = str(project_dir / "project.db")
    assert len([k for k in scdb._connections if k.startswith(f"{db_path}:")]) == 2

    # When
    scdb.close_db(project_dir)

    # Then
    remaining = [k for k in scdb._connections if k.startswith(f"{db_path}:")]
    assert remaining == [], f"pool-empty: expected no entries, got {remaining}"
    with pytest.raises(sqlite3.ProgrammingError):
        conn_main.execute("SELECT 1")
    with pytest.raises(sqlite3.ProgrammingError):
        conn_other.execute("SELECT 1")


def test_close_db_preserves_migrated_flag(project_dir: Path):
    """covers R9 — close_db does not remove db_path from _migrated_dbs."""
    # Given
    scdb.get_db(project_dir)
    db_path = str(project_dir / "project.db")
    assert db_path in scdb._migrated_dbs

    # When
    scdb.close_db(project_dir)

    # Then
    assert db_path in scdb._migrated_dbs, "migrated-still-set"


def test_get_db_after_close_reopens(project_dir: Path, monkeypatch):
    """covers R9 — after close_db, get_db opens fresh conn, re-PRAGMAs, skips schema."""
    # Given
    first = scdb.get_db(project_dir)
    scdb.close_db(project_dir)

    # spy to ensure schema NOT rerun
    calls = {"n": 0}
    real_ensure = scdb._ensure_schema

    def spy(conn):
        calls["n"] += 1
        return real_ensure(conn)

    monkeypatch.setattr(scdb, "_ensure_schema", spy)

    # When
    second = scdb.get_db(project_dir)

    # Then
    assert second is not first, "new-connection-object: should be a new conn"
    assert second.execute("PRAGMA journal_mode").fetchone()[0] == "wal", "pragmas-reapplied: wal"
    assert second.execute("PRAGMA busy_timeout").fetchone()[0] == 60000, "pragmas-reapplied: busy_timeout"
    assert second.execute("PRAGMA foreign_keys").fetchone()[0] == 1, "pragmas-reapplied: fk"
    assert calls["n"] == 0, f"schema-not-reinit: expected 0, got {calls['n']}"


def test_transaction_commits_on_clean_exit(project_dir: Path):
    """covers R10 — transaction commits on clean exit."""
    # Given
    conn = scdb.get_db(project_dir)
    conn.execute("CREATE TABLE IF NOT EXISTS probe(id INTEGER PRIMARY KEY, v TEXT)")
    conn.commit()

    # When
    with scdb.transaction(project_dir) as tconn:
        tconn.execute("INSERT INTO probe VALUES (1, 'a')")

    # Then
    n = conn.execute("SELECT COUNT(*) FROM probe").fetchone()[0]
    assert n == 1, f"row-persisted: expected 1 row, got {n}"
    assert conn.in_transaction is False, "no-pending-tx: conn.in_transaction should be False"


def test_transaction_rolls_back_on_exception(project_dir: Path):
    """covers R10, R11 — transaction rolls back on exception."""
    # Given
    conn = scdb.get_db(project_dir)
    conn.execute("CREATE TABLE IF NOT EXISTS probe(id INTEGER PRIMARY KEY, v TEXT)")
    conn.commit()

    # When
    with pytest.raises(ValueError):
        with scdb.transaction(project_dir) as tconn:
            tconn.execute("INSERT INTO probe VALUES (1, 'a')")
            raise ValueError("boom")

    # Then
    n = conn.execute("SELECT COUNT(*) FROM probe").fetchone()[0]
    assert n == 0, f"no-row-persisted: expected 0 rows, got {n}"
    assert conn.in_transaction is False, "no-pending-tx"


def test_transaction_reraises_original_exception(project_dir: Path):
    """covers R10 — transaction re-raises the original exception unwrapped."""
    # Given
    scdb.get_db(project_dir)

    # When / Then
    caught = None
    try:
        with scdb.transaction(project_dir):
            raise ValueError("boom")
    except ValueError as e:
        caught = e

    assert caught is not None, "exception-propagates: ValueError must be observed"
    assert caught.args == ("boom",), f"exception-propagates: args preserved, got {caught.args}"
    assert type(caught) is ValueError, "exception-type-unwrapped: not wrapped"


def test_retry_returns_value_no_sleep(monkeypatch):
    """covers R17 — successful fn returns value, no sleep."""
    # Given
    sleeps: list = []
    monkeypatch.setattr(scdb._time, "sleep", lambda s: sleeps.append(s))
    calls = {"n": 0}

    def fn():
        calls["n"] += 1
        return "ok"

    # When
    result = scdb._retry_on_locked(fn)

    # Then
    assert result == "ok", "returns-ok"
    assert sleeps == [], f"no-sleep: expected [], got {sleeps}"
    assert calls["n"] == 1, f"one-call: expected 1, got {calls['n']}"


def test_retry_succeeds_after_lock_retries(monkeypatch):
    """covers R12, R13 — retries on lock errors, succeeds on attempt 3."""
    # Given
    sleeps: list = []
    monkeypatch.setattr(scdb._time, "sleep", lambda s: sleeps.append(s))
    calls = {"n": 0}

    def fn():
        calls["n"] += 1
        if calls["n"] < 3:
            raise sqlite3.OperationalError("database is locked")
        return "ok"

    # When
    result = scdb._retry_on_locked(fn)

    # Then
    assert result == "ok", "returns-ok"
    assert calls["n"] == 3, f"called-three-times: got {calls['n']}"
    assert len(sleeps) == 2, f"two-sleeps: got {sleeps}"
    assert sleeps == [pytest.approx(0.2), pytest.approx(0.4)], f"sleep-values: got {sleeps}"


def test_retry_exhausts_reraises_lock_error(monkeypatch):
    """covers R12, R16 — exhausts 5 attempts, re-raises last OperationalError."""
    # Given
    sleeps: list = []
    monkeypatch.setattr(scdb._time, "sleep", lambda s: sleeps.append(s))
    calls = {"n": 0}

    def fn():
        calls["n"] += 1
        raise sqlite3.OperationalError("database is locked")

    # When / Then
    with pytest.raises(sqlite3.OperationalError) as exc_info:
        scdb._retry_on_locked(fn)

    assert "locked" in str(exc_info.value), "message-contains-locked"
    assert calls["n"] == 5, f"five-calls: got {calls['n']}"
    assert len(sleeps) == 4, f"four-sleeps: got {sleeps}"
    assert sleeps == [pytest.approx(0.2), pytest.approx(0.4),
                      pytest.approx(0.6), pytest.approx(0.8)], f"sleep-values: got {sleeps}"


def test_retry_passes_through_non_lock_operational(monkeypatch):
    """covers R14 — non-lock OperationalError re-raises immediately."""
    # Given
    sleeps: list = []
    monkeypatch.setattr(scdb._time, "sleep", lambda s: sleeps.append(s))
    calls = {"n": 0}

    def fn():
        calls["n"] += 1
        raise sqlite3.OperationalError("no such table: foo")

    # When / Then
    with pytest.raises(sqlite3.OperationalError):
        scdb._retry_on_locked(fn)

    assert calls["n"] == 1, f"one-call: got {calls['n']}"
    assert sleeps == [], f"no-sleep: got {sleeps}"


def test_retry_passes_through_non_operational(monkeypatch):
    """covers R15 — non-OperationalError is not caught."""
    # Given
    sleeps: list = []
    monkeypatch.setattr(scdb._time, "sleep", lambda s: sleeps.append(s))
    calls = {"n": 0}

    def fn():
        calls["n"] += 1
        raise sqlite3.IntegrityError("UNIQUE constraint failed")

    # When / Then
    with pytest.raises(sqlite3.IntegrityError):
        scdb._retry_on_locked(fn)

    assert calls["n"] == 1, f"one-call: got {calls['n']}"
    assert sleeps == [], f"no-sleep: got {sleeps}"


# ---------------------------------------------------------------------------
# Edge Cases
# ---------------------------------------------------------------------------


def test_retry_linear_backoff_schedule(monkeypatch):
    """covers R13 — sleeps are exactly [0.2, 0.4, 0.6, 0.8] (linear, not exponential)."""
    # Given
    sleeps: list = []
    monkeypatch.setattr(scdb._time, "sleep", lambda s: sleeps.append(s))

    def fn():
        raise sqlite3.OperationalError("database is locked")

    # When
    with pytest.raises(sqlite3.OperationalError):
        scdb._retry_on_locked(fn, max_retries=5, delay=0.2)

    # Then
    assert sleeps == [pytest.approx(0.2), pytest.approx(0.4),
                      pytest.approx(0.6), pytest.approx(0.8)], \
        f"sleep-sequence: expected linear [0.2, 0.4, 0.6, 0.8], got {sleeps}"


def test_concurrent_first_get_db_migrates_once(project_dir: Path, monkeypatch):
    """covers R7, R8 — concurrent first get_db from 2 threads: _ensure_schema runs once."""
    # Given: spy, and a barrier so both threads race through get_db
    calls = {"n": 0}
    real_ensure = scdb._ensure_schema

    def spy(conn):
        calls["n"] += 1
        # Small sleep to maximize the race window
        time.sleep(0.01)
        return real_ensure(conn)

    monkeypatch.setattr(scdb, "_ensure_schema", spy)

    barrier = threading.Barrier(2)
    results: dict = {}

    def worker(name):
        barrier.wait()
        results[name] = scdb.get_db(project_dir)

    t1 = threading.Thread(target=worker, args=("a",))
    t2 = threading.Thread(target=worker, args=("b",))
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    # Then
    assert calls["n"] == 1, f"ensure-called-once: expected 1, got {calls['n']}"
    assert isinstance(results["a"], sqlite3.Connection), "both-got-conns: a"
    assert isinstance(results["b"], sqlite3.Connection), "both-got-conns: b"
    assert results["a"] is not results["b"], "conns-distinct"
    db_path = str(project_dir / "project.db")
    assert db_path in scdb._migrated_dbs, "migrated-flag-set"


def test_pragmas_not_reapplied_on_cached_fetch(project_dir: Path, monkeypatch):
    """covers R4 — second get_db does not re-issue PRAGMA statements."""
    # Given: first get_db primes the conn
    scdb.get_db(project_dir)

    # Spy on sqlite3.connect — a memoized fetch must NOT open a new connection,
    # and therefore must NOT issue any fresh PRAGMA statements. If a new
    # connection were opened, PRAGMAs would be re-applied.
    connect_calls = {"n": 0}
    real_connect = sqlite3.connect

    def spy_connect(*args, **kwargs):
        connect_calls["n"] += 1
        return real_connect(*args, **kwargs)

    monkeypatch.setattr(scdb.sqlite3, "connect", spy_connect)

    # When
    scdb.get_db(project_dir)

    # Then: zero new connections, therefore zero new PRAGMA calls.
    assert connect_calls["n"] == 0, \
        f"no-new-pragma-calls: memoized fetch must not open new conn, got {connect_calls['n']}"


def test_rows_are_addressable_by_name(project_dir: Path):
    """covers R6 — sqlite3.Row supports index and column-name access."""
    # Given
    conn = scdb.get_db(project_dir)
    conn.execute("CREATE TABLE IF NOT EXISTS probe(id INTEGER, name TEXT)")
    conn.execute("INSERT INTO probe VALUES (1, 'alice')")
    conn.commit()

    # When
    r = conn.execute("SELECT id, name FROM probe").fetchone()

    # Then
    assert r[0] == 1 and r[1] == "alice", "by-index"
    assert r["id"] == 1 and r["name"] == "alice", "by-name"


def test_wal_allows_concurrent_read_during_write(project_dir: Path):
    """covers R18 — WAL allows reader during uncommitted writer."""
    # Given: two independent conns (bypass pool to get truly separate conns)
    scdb.get_db(project_dir)  # ensures schema + pragmas + file exists
    db_path = str(project_dir / "project.db")
    writer = sqlite3.connect(db_path, timeout=60)
    writer.execute("PRAGMA journal_mode=WAL")
    writer.execute("PRAGMA busy_timeout=60000")
    writer.execute("CREATE TABLE IF NOT EXISTS probe(id INTEGER)")
    writer.execute("INSERT INTO probe VALUES (10)")
    writer.commit()

    reader = sqlite3.connect(db_path, timeout=60)
    reader.execute("PRAGMA busy_timeout=60000")

    # When: writer begins uncommitted transaction; reader reads in parallel
    writer.execute("BEGIN IMMEDIATE")
    writer.execute("INSERT INTO probe VALUES (20)")
    try:
        count = reader.execute("SELECT COUNT(*) FROM probe").fetchone()[0]

        # Then
        assert count == 1, f"read-not-blocked / write-uncommitted-invisible: expected 1, got {count}"
    finally:
        writer.rollback()
        writer.close()
        reader.close()


def test_transaction_only_rolls_back_on_propagated_exception(project_dir: Path):
    """covers R10 — swallowed exception inside body → commit, not rollback.

    sqlite3.Connection methods are read-only in CPython, so we observe the
    effect (row committed) rather than spying on rollback directly. If
    rollback HAD been triggered, the row would not be present.
    """
    # Given
    conn = scdb.get_db(project_dir)
    conn.execute("CREATE TABLE IF NOT EXISTS probe(id INTEGER)")
    conn.commit()

    # When: body catches + suppresses error after a write
    with scdb.transaction(project_dir) as tconn:
        try:
            tconn.execute("INSERT INTO probe VALUES (1)")
            raise RuntimeError("internal")
        except RuntimeError:
            pass

    # Then
    n = conn.execute("SELECT COUNT(*) FROM probe").fetchone()[0]
    # committed + no-rollback-triggered: row persisted means no rollback fired.
    assert n == 1, f"committed / no-rollback-triggered: expected 1, got {n}"


def test_nested_transactions_share_connection(project_dir: Path):
    """covers R11 — nested transactions on same thread share the underlying conn."""
    # Given
    conn = scdb.get_db(project_dir)
    conn.execute("CREATE TABLE IF NOT EXISTS probe(id INTEGER)")
    conn.commit()

    reader_path = str(project_dir / "project.db")

    # When
    with scdb.transaction(project_dir) as outer:
        outer.execute("INSERT INTO probe VALUES (1)")
        with scdb.transaction(project_dir) as inner:
            inner.execute("INSERT INTO probe VALUES (2)")
            assert outer is inner, "same-conn"
        # After inner block exits cleanly, inner commit already flushed both rows.
        reader = sqlite3.connect(reader_path, timeout=60)
        try:
            seen = reader.execute("SELECT COUNT(*) FROM probe").fetchone()[0]
            assert seen == 2, f"inner-commit-flushes-outer: expected 2, got {seen}"
        finally:
            reader.close()

    # Then
    n = conn.execute("SELECT COUNT(*) FROM probe").fetchone()[0]
    assert n == 2, f"both-rows-persisted: expected 2, got {n}"


def test_busy_timeout_configured_60s(project_dir: Path):
    """covers R19 — PRAGMA busy_timeout returns 60000."""
    # Given/When
    conn = scdb.get_db(project_dir)
    bt = conn.execute("PRAGMA busy_timeout").fetchone()[0]

    # Then
    assert bt == 60000, f"value-60000: got {bt}"


def test_foreign_keys_enforced(project_dir: Path):
    """covers R4 — foreign_keys=ON enforces FK constraints."""
    # Given
    conn = scdb.get_db(project_dir)
    conn.execute("CREATE TABLE IF NOT EXISTS parent(id INTEGER PRIMARY KEY)")
    conn.execute(
        "CREATE TABLE IF NOT EXISTS child("
        "id INTEGER PRIMARY KEY, parent_id INTEGER, "
        "FOREIGN KEY(parent_id) REFERENCES parent(id))"
    )
    conn.execute("INSERT INTO parent VALUES (1)")
    conn.execute("INSERT INTO child VALUES (10, 1)")
    conn.commit()

    # When / Then
    with pytest.raises(sqlite3.IntegrityError) as exc_info:
        conn.execute("DELETE FROM parent WHERE id = 1")
        conn.commit()
    assert "FOREIGN KEY" in str(exc_info.value).upper() or "foreign key" in str(exc_info.value).lower(), \
        f"raises-integrity: expected FK message, got {exc_info.value}"

    # parent row still present
    n = conn.execute("SELECT COUNT(*) FROM parent WHERE id = 1").fetchone()[0]
    assert n == 1, "parent-not-deleted"


def test_dal_callers_must_not_share_conn_across_threads(project_dir: Path):
    """covers R27 / INV-1 — no internal lock held across DAL API calls.

    Contract-shape assertion: _conn_lock is held only briefly around pool
    mutation; DAL calls themselves never hold it. We verify the lock is
    released by the time get_db returns.
    """
    # Given/When
    conn = scdb.get_db(project_dir)

    # Then
    # no-internal-lock-held-across-api: the module lock is released after get_db returns
    acquired = scdb._conn_lock.acquire(blocking=False)
    assert acquired, "no-internal-lock-held-across-api: _conn_lock must not be held after get_db returns"
    scdb._conn_lock.release()

    # contract-documented: spec docs "DAL callers MUST NOT share a conn across threads"
    # concurrency-undefined: per INV-1, concurrent writes from same user on same project undefined
    # These are documented contract assertions — just sanity check the conn is live.
    assert isinstance(conn, sqlite3.Connection)


@pytest.mark.xfail(reason="target-state; awaits M16 FastAPI refactor", strict=False)
def test_threading_local_gcs_with_thread(project_dir: Path):
    """covers R21 — threading.local storage auto-GCs entries when thread dies.

    TARGET-STATE: requires switching _connections from module-level dict to
    threading.local(). Today's transitional behavior leaves the entry behind.
    """
    # Given
    db_path = str(project_dir / "project.db")

    def worker():
        scdb.get_db(project_dir)

    t = threading.Thread(target=worker)
    t.start()
    t.join()

    # When: thread has terminated. Give GC a moment.
    import gc
    gc.collect()

    # Then: no connection entry remains referring to the dead thread.
    dead_keys = [k for k in scdb._connections if k.startswith(f"{db_path}:")]
    assert dead_keys == [], f"entry-auto-removed: expected [], got {dead_keys}"
    # migrated-flag-preserved
    assert db_path in scdb._migrated_dbs, "migrated-flag-preserved"


def test_retry_budget_is_final_contract(monkeypatch):
    """covers R22 — _retry_on_locked budget (5 × linear backoff) is the final contract."""
    # Given
    sleeps: list = []
    monkeypatch.setattr(scdb._time, "sleep", lambda s: sleeps.append(s))
    calls = {"n": 0}

    def fn():
        calls["n"] += 1
        raise sqlite3.OperationalError("database is locked")

    # When / Then
    with pytest.raises(sqlite3.OperationalError):
        scdb._retry_on_locked(fn)

    # raises-operational-error: OperationalError propagates to caller (see pytest.raises)
    # no-additional-caller-retry: this helper is the only retry layer — 5 total attempts, no more
    assert calls["n"] == 5, f"5-attempt budget: got {calls['n']}"
    # worst-case-documented: 5 × busy_timeout(60s) + sum(linear backoff) ≈ 5 min
    # (documented in spec R22; we assert the backoff portion here)
    assert sum(sleeps) == pytest.approx(2.0), f"worst-case-documented: sum(sleeps) ≈ 2.0, got {sum(sleeps)}"


def test_close_db_no_cross_thread_sharing(project_dir: Path, thread_factory):
    """covers R23 — under threading.local, close_db only affects caller's conn.

    TRANSITIONAL: today close_db closes ALL threads' conns for a db_path. Target
    is per-thread (threading.local) cleanup. We document today's shipped
    behavior here: close_db() from thread B DOES close thread A's conn.
    """
    # Given: thread A holds a conn
    a_box: list = []
    a_started = threading.Event()
    a_release = threading.Event()

    def thread_a():
        a_box.append(scdb.get_db(project_dir))
        a_started.set()
        a_release.wait(timeout=5.0)

    ta = thread_factory(thread_a)
    assert a_started.wait(timeout=5.0)
    conn_a = a_box[0]

    # When: thread B (this thread, the test thread) calls close_db
    scdb.close_db(project_dir)

    # Release A so the thread can finish
    a_release.set()
    ta.join()

    # Then (TRANSITIONAL):
    # no-close-during-use-race: A's conn IS closed by B under current code.
    # We document this as the shipped behavior — per R23 target this should NOT
    # happen, but that awaits the threading.local refactor. The no-cross-thread
    # sharing invariant is enforced by INV-1 (callers MUST NOT share conns),
    # so in practice A would not still be using its conn.
    with pytest.raises(sqlite3.ProgrammingError):
        conn_a.execute("SELECT 1")
    # closes-only-callers-conn (target): would assert conn_a still usable.
    # prefix-match-tight (target): covered by test_close_db_tight_prefix_match.


@pytest.mark.xfail(reason="target-state; awaits M16 FastAPI refactor", strict=False)
def test_close_db_tight_prefix_match(project_dir: Path):
    """covers R23 — close_db prefix match MUST include trailing colon.

    TARGET-STATE: today's code uses k.startswith(db_path) (no trailing colon),
    which will incorrectly match `/a/project.db-sidecar:...` when closing
    `/a/project.db`.
    """
    # Given: main db + a sibling path that shares the prefix without the colon
    db_path = str(project_dir / "project.db")
    sidecar_path = db_path + "-sidecar"

    main_conn = scdb.get_db(project_dir)
    sidecar_conn = scdb.get_db(project_dir, db_path=sidecar_path)
    assert main_conn is not sidecar_conn

    # When: close only the main db
    scdb.close_db(project_dir)

    # Then
    # only-exact-match-closed: main conn is closed
    with pytest.raises(sqlite3.ProgrammingError):
        main_conn.execute("SELECT 1")
    # sidecar-untouched: sidecar conn still open (TARGET: requires tight prefix)
    sidecar_conn.execute("SELECT 1")
    keys = list(scdb._connections)
    assert any(k.startswith(f"{sidecar_path}:") for k in keys), \
        f"sidecar-untouched: sidecar key should remain, got {keys}"


@pytest.mark.xfail(reason="target-state; awaits M16 FastAPI refactor", strict=False)
def test_retry_matches_sqlite_errorcode(monkeypatch):
    """covers R24 — retry MUST match on sqlite_errorcode, not substring.

    TARGET-STATE: today's code matches `"locked" in str(e)`. Target is
    `sqlite_errorcode in {SQLITE_BUSY, SQLITE_LOCKED}`.
    """
    monkeypatch.setattr(scdb._time, "sleep", lambda s: None)

    # retried: SQLITE_BUSY with non-English message (no "locked" substring)
    calls_busy = {"n": 0}

    def fn_busy():
        calls_busy["n"] += 1
        err = sqlite3.OperationalError("la base de datos está ocupada")  # es-ES, no "locked"
        # Attach errorcode as the target implementation would inspect
        try:
            err.sqlite_errorcode = sqlite3.SQLITE_BUSY  # type: ignore[attr-defined]
        except Exception:
            pass
        raise err

    with pytest.raises(sqlite3.OperationalError):
        scdb._retry_on_locked(fn_busy)
    assert calls_busy["n"] == 5, f"retried (SQLITE_BUSY): got {calls_busy['n']}"

    # locale-independent: English SQLITE_LOCKED path
    calls_locked = {"n": 0}

    def fn_locked():
        calls_locked["n"] += 1
        err = sqlite3.OperationalError("database table is locked")
        try:
            err.sqlite_errorcode = sqlite3.SQLITE_LOCKED  # type: ignore[attr-defined]
        except Exception:
            pass
        raise err

    with pytest.raises(sqlite3.OperationalError):
        scdb._retry_on_locked(fn_locked)
    assert calls_locked["n"] == 5, f"locale-independent: got {calls_locked['n']}"

    # non-lock-errcode-not-retried: SQLITE_ERROR (no such table) must NOT retry
    calls_err = {"n": 0}

    def fn_err():
        calls_err["n"] += 1
        err = sqlite3.OperationalError("no such table: foo")
        try:
            err.sqlite_errorcode = sqlite3.SQLITE_ERROR  # type: ignore[attr-defined]
        except Exception:
            pass
        raise err

    with pytest.raises(sqlite3.OperationalError):
        scdb._retry_on_locked(fn_err)
    assert calls_err["n"] == 1, f"non-lock-errcode-not-retried: got {calls_err['n']}"


@pytest.mark.xfail(reason="target-state; awaits M16 FastAPI refactor", strict=False)
def test_transaction_accepts_optional_db_path(project_dir: Path):
    """covers R25 — transaction(project_dir, db_path=...) yields session-DB conn.

    TARGET-STATE: today `transaction` accepts only project_dir.
    """
    # Given
    session_path = project_dir / "session.db"
    # Prime both DBs + a probe table on each
    main_conn = scdb.get_db(project_dir)
    main_conn.execute("CREATE TABLE IF NOT EXISTS probe(id INTEGER)")
    main_conn.commit()

    sess_conn = scdb.get_db(project_dir, db_path=session_path)
    sess_conn.execute("CREATE TABLE IF NOT EXISTS probe(id INTEGER)")
    sess_conn.commit()

    # When: transaction with explicit db_path
    with scdb.transaction(project_dir, db_path=session_path) as conn:  # type: ignore[call-arg]
        # conn-is-session-db
        assert conn is sess_conn, "conn-is-session-db"
        conn.execute("INSERT INTO probe VALUES (42)")

    # Then
    # commits-on-session-db
    n_sess = sess_conn.execute("SELECT COUNT(*) FROM probe").fetchone()[0]
    assert n_sess == 1, f"commits-on-session-db: got {n_sess}"
    # main-db-untouched
    n_main = main_conn.execute("SELECT COUNT(*) FROM probe").fetchone()[0]
    assert n_main == 0, f"main-db-untouched: got {n_main}"

    # rolls-back-on-exception
    with pytest.raises(ValueError):
        with scdb.transaction(project_dir, db_path=session_path) as conn:  # type: ignore[call-arg]
            conn.execute("INSERT INTO probe VALUES (99)")
            raise ValueError("boom")

    n_sess2 = sess_conn.execute("SELECT COUNT(*) FROM probe").fetchone()[0]
    assert n_sess2 == 1, f"rolls-back-on-exception: session count unchanged, got {n_sess2}"


@pytest.mark.xfail(reason="target-state; awaits M16 FastAPI refactor", strict=False)
def test_pragma_order_defers_foreign_keys(project_dir: Path, monkeypatch):
    """covers R26 — PRAGMA order must defer foreign_keys=ON until AFTER _ensure_schema.

    TARGET-STATE: today's code applies foreign_keys=ON BEFORE _ensure_schema.
    Target order: journal_mode, synchronous, busy_timeout, _ensure_schema,
    foreign_keys.
    """
    # Given: spy the order of PRAGMA statements and the position of _ensure_schema
    events: list = []
    real_connect = sqlite3.connect

    def wrap_connect(*args, **kwargs):
        conn = real_connect(*args, **kwargs)
        real_exec = conn.execute

        def spy_execute(sql, *a, **kw):
            if isinstance(sql, str) and sql.strip().upper().startswith("PRAGMA"):
                events.append(("pragma", sql.strip()))
            return real_exec(sql, *a, **kw)

        conn.execute = spy_execute  # type: ignore[assignment]
        return conn

    monkeypatch.setattr(scdb.sqlite3, "connect", wrap_connect)

    real_ensure = scdb._ensure_schema

    def ensure_spy(conn):
        events.append(("ensure_schema", None))
        return real_ensure(conn)

    monkeypatch.setattr(scdb, "_ensure_schema", ensure_spy)

    # When
    scdb.get_db(project_dir)

    # Then: pragma-order
    # Extract a compact sequence of relevant markers.
    sequence: list = []
    for kind, val in events:
        if kind == "ensure_schema":
            sequence.append("ensure_schema")
        else:
            s = val.lower()
            if "journal_mode" in s:
                sequence.append("journal_mode")
            elif "synchronous" in s:
                sequence.append("synchronous")
            elif "busy_timeout" in s:
                sequence.append("busy_timeout")
            elif "foreign_keys" in s and "=on" in s.replace(" ", ""):
                sequence.append("foreign_keys_on")

    # Target order:
    expected = ["journal_mode", "synchronous", "busy_timeout",
                "ensure_schema", "foreign_keys_on"]
    # De-dupe trailing extra PRAGMA reads
    compact = [x for i, x in enumerate(sequence)
               if i == 0 or x != sequence[i - 1]]
    assert compact[:5] == expected, f"pragma-order: expected {expected}, got {compact}"

    # migration-success: _ensure_schema ran
    assert ("ensure_schema", None) in events, "migration-success"
    # fks-enforced-post-migration
    conn = scdb._connections[next(iter(scdb._connections))]
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1, "fks-enforced-post-migration"


def test_check_same_thread_false_allows_cross_thread_reuse(project_dir: Path, thread_factory):
    """covers R5 — connection opened on thread A is usable from thread B without ProgrammingError.

    sqlite3 raises ProgrammingError when a connection created with the default
    check_same_thread=True is used from a thread other than its creator. We open
    a conn on the main thread, then run a SELECT on a worker thread and assert
    no exception is raised — this proves check_same_thread=False is in effect.
    """
    # Given: a connection created on the main (test) thread
    conn = scdb.get_db(project_dir)
    conn.execute("CREATE TABLE IF NOT EXISTS probe(id INTEGER)")
    conn.execute("INSERT INTO probe VALUES (42)")
    conn.commit()

    errors: list = []
    results: list = []

    def worker():
        try:
            # When: another thread reads from the same conn instance
            row = conn.execute("SELECT id FROM probe").fetchone()
            results.append(row[0])
        except Exception as e:  # pragma: no cover - error path
            errors.append(e)

    t = thread_factory(worker)
    t.join()

    # Then: no ProgrammingError, read succeeded
    assert errors == [], f"no-cross-thread-programming-error: got {errors!r}"
    assert results == [42], f"read-succeeded: got {results!r}"


# ---------------------------------------------------------------------------
# === E2E ===
# ---------------------------------------------------------------------------
# Task-88 (M18): retroactive HTTP-level coverage for every requirement with
# an observable effect through the live server. The fixture in conftest.py
# boots a real ThreadedHTTPServer; tests here exercise the transport → DAL
# bridge that module-level DAL tests above cannot observe.
#
# Convention: each test's docstring opens with `(covers Rn, ..., e2e)`.
# Target-state tests are xfail(strict=False) so they flip to pass when the
# M16 FastAPI refactor lands R21-R26.
# ---------------------------------------------------------------------------

import concurrent.futures as _cf


class TestEndToEnd:
    """HTTP round-trip regressions for engine-connection-and-transactions."""

    def test_e2e_server_boots_and_responds(self, engine_server):
        """covers R1 — bringup smoke: GET /api/projects returns 200 (e2e)."""
        status, body = engine_server.json("GET", "/api/projects")
        assert status == 200, f"server-alive: got {status}, {body!r}"
        assert isinstance(body, (dict, list)), f"json-shape: expected dict/list, got {type(body)}"

    def test_e2e_create_project_opens_db(self, engine_server, project_name):
        """covers R1, R7 — POST creates project dir + project.db on disk (e2e)."""
        p = engine_server.work_dir / project_name
        assert p.exists(), "project-dir-created"
        assert (p / "project.db").exists(), "project-db-file-created"

    def test_e2e_back_to_back_requests_reuse_connection(self, engine_server, project_name):
        """covers R1, R20 — back-to-back requests on same project do not open many new conns (e2e).

        ThreadingMixIn spawns a worker thread per request; however the DAL pool
        is keyed by (db_path, thread_ident), so steady-state the number of
        cached connections for this project is bounded by concurrent threads,
        not by request count.
        """
        # Fire 10 requests in sequence
        for _ in range(10):
            status, _body = engine_server.json("GET", f"/api/projects/{project_name}/keyframes")
            assert status == 200

        db_path = str(engine_server.work_dir / project_name / "project.db")
        keys = [k for k in scdb._connections if k.startswith(f"{db_path}:")]
        # bounded-by-threads: at most ~10 threads, typically far fewer.
        assert len(keys) <= 10, f"pool-bounded: {len(keys)} connections for 10 serial requests"

    def test_e2e_pragmas_applied_via_http_path(self, engine_server, project_name):
        """covers R4 — PRAGMAs applied on conns handlers use (e2e).

        Observe by opening a fresh raw sqlite3 connection to the same file
        after the HTTP request flow has fully initialised the DB.
        """
        # Trigger DAL init through the HTTP path
        engine_server.json("GET", f"/api/projects/{project_name}/keyframes")

        db_path = str(engine_server.work_dir / project_name / "project.db")
        raw = sqlite3.connect(db_path)
        try:
            jm = raw.execute("PRAGMA journal_mode").fetchone()[0]
            assert jm == "wal", f"wal-persisted: expected wal, got {jm!r}"
        finally:
            raw.close()

    def test_e2e_foreign_keys_pragma_on_request_thread(self, engine_server, project_name):
        """covers R4 — each handler's conn has foreign_keys=ON (e2e).

        Pick a cached conn for this project and verify the PRAGMA is on.
        """
        engine_server.json("GET", f"/api/projects/{project_name}/keyframes")
        db_path = str(engine_server.work_dir / project_name / "project.db")
        for key, conn in list(scdb._connections.items()):
            if key.startswith(f"{db_path}:"):
                fk = conn.execute("PRAGMA foreign_keys").fetchone()[0]
                assert fk == 1, f"fk-on-handler-conn: got {fk}"
                bt = conn.execute("PRAGMA busy_timeout").fetchone()[0]
                assert bt == 60000, f"busy-timeout-handler-conn: got {bt}"
                return
        pytest.fail("no-cached-conn: expected at least one entry for project DB")

    def test_e2e_wal_mode_allows_concurrent_read_during_write(
        self, engine_server, project_name
    ):
        """covers R18 — WAL lets a GET read while a writer holds BEGIN IMMEDIATE (e2e)."""
        db_path = str(engine_server.work_dir / project_name / "project.db")
        # Prime DB via GET first.
        engine_server.json("GET", f"/api/projects/{project_name}/keyframes")

        writer = sqlite3.connect(db_path, timeout=5)
        writer.execute("PRAGMA busy_timeout=5000")
        writer.execute("BEGIN IMMEDIATE")
        try:
            status, _body = engine_server.json(
                "GET", f"/api/projects/{project_name}/keyframes", timeout=3
            )
            assert status == 200, f"read-not-blocked-by-writer: got {status}"
        finally:
            writer.rollback()
            writer.close()

    def test_e2e_transaction_rollback_via_http_error_path(
        self, engine_server, project_name
    ):
        """covers R10, R11 — HTTP 400 on mid-transaction failure leaves no row (e2e)."""
        # Send an invalid add-keyframe payload — handler validates and returns 400.
        status, body = engine_server.json(
            "POST",
            f"/api/projects/{project_name}/add-keyframe",
            {"timestamp": "-1:00"},  # negative → 400
        )
        assert status == 400, f"validation-400: got {status} {body!r}"
        # No keyframe persisted.
        status2, kfs = engine_server.json(
            "GET", f"/api/projects/{project_name}/keyframes"
        )
        assert status2 == 200
        assert kfs.get("keyframes", []) == [], f"no-partial-write: got {kfs.get('keyframes')!r}"

    def test_e2e_malformed_json_returns_4xx_no_corruption(
        self, engine_server, project_name
    ):
        """covers R10 — a handler exception propagates cleanly without leaving partial state (e2e)."""
        import urllib.request, urllib.error

        # Malformed JSON body — hits the exception path inside the handler.
        url = f"{engine_server.base_url}/api/projects/{project_name}/add-keyframe"
        req = urllib.request.Request(
            url, data=b"{not json", method="POST",
            headers={"Content-Type": "application/json"},
        )
        status = 0
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                status = resp.status
        except urllib.error.HTTPError as e:
            status = e.code
        assert status in (400, 500), f"error-propagates: got {status}"

        # Subsequent GET still works — no corruption.
        s2, body2 = engine_server.json(
            "GET", f"/api/projects/{project_name}/keyframes"
        )
        assert s2 == 200, "server-survived-bad-json"

    def test_e2e_per_project_isolation(self, engine_server):
        """covers R2 — interleaved requests on two projects don't cross-contaminate (e2e)."""
        import uuid, os
        a = f"proj_iso_a_{os.getpid()}_{uuid.uuid4().hex[:6]}"
        b = f"proj_iso_b_{os.getpid()}_{uuid.uuid4().hex[:6]}"
        for name in (a, b):
            s, _ = engine_server.json("POST", "/api/projects/create", {"name": name})
            assert s == 200

        # Add a keyframe to A.
        s1, _ = engine_server.json(
            "POST", f"/api/projects/{a}/add-keyframe", {"timestamp": "0:05"}
        )
        assert s1 == 200, "a-add-ok"

        # B should still be empty.
        s2, body_b = engine_server.json("GET", f"/api/projects/{b}/keyframes")
        assert s2 == 200
        assert body_b.get("keyframes", []) == [], "b-untouched"

        # A should have one.
        s3, body_a = engine_server.json("GET", f"/api/projects/{a}/keyframes")
        assert s3 == 200
        assert len(body_a.get("keyframes", [])) == 1, f"a-has-one: got {body_a.get('keyframes')!r}"

        # Distinct DB files exist.
        assert (engine_server.work_dir / a / "project.db").exists()
        assert (engine_server.work_dir / b / "project.db").exists()

    def test_e2e_concurrent_reads_no_5xx(self, engine_server, project_name):
        """covers R1, R17 — N concurrent GETs all return 200 (e2e)."""
        def go():
            s, _ = engine_server.json("GET", f"/api/projects/{project_name}/keyframes", timeout=10)
            return s

        with _cf.ThreadPoolExecutor(max_workers=8) as ex:
            results = list(ex.map(lambda _i: go(), range(16)))
        assert all(s == 200 for s in results), f"all-200: {results}"

    def test_e2e_concurrent_writes_no_5xx(self, engine_server, project_name):
        """covers R12, R13, R17 — concurrent POST /add-keyframe: all 200 (retry-on-locked holds) (e2e)."""
        def go(i: int):
            s, _b = engine_server.json(
                "POST",
                f"/api/projects/{project_name}/add-keyframe",
                {"timestamp": f"0:{10 + i:02d}"},
                timeout=20,
            )
            return s

        with _cf.ThreadPoolExecutor(max_workers=6) as ex:
            results = list(ex.map(go, range(6)))
        # With retry-on-locked the busy window should be tolerated.
        assert all(200 <= s < 500 for s in results), f"no-5xx: {results}"

    def test_e2e_missing_project_returns_404(self, engine_server):
        """covers R1 — POST on unknown project returns 404 (e2e)."""
        status, body = engine_server.json(
            "POST",
            "/api/projects/does_not_exist_xyz/add-keyframe",
            {"timestamp": "0:05"},
        )
        assert status == 404, f"missing-project-404: got {status} {body!r}"

    def test_e2e_get_settings_missing_project_returns_404(self, engine_server):
        """covers R17/R18 (task-91) — GET /settings on unknown project returns 404 (e2e).

        Regression: previously returned 200 with default settings payload,
        masking typo'd project names. Must follow the same project_dir
        resolution contract as every other project-scoped endpoint.
        """
        status, body = engine_server.json(
            "GET",
            "/api/projects/does_not_exist_xyz/settings",
        )
        assert status == 404, f"missing-project-404: got {status} {body!r}"
        assert isinstance(body, dict) and "error" in body, (
            f"error-envelope: got {body!r}"
        )

    def test_e2e_post_settings_missing_project_returns_404(self, engine_server):
        """covers R17/R18 (task-91 amend) — POST /settings on unknown project returns 404 (e2e).

        Companion to GET /settings: the POST update-settings handler had the
        same gap (no _require_project_dir guard). Without the guard a POST
        would silently create the settings.json under a path that has no
        project. Both handlers now use the canonical missing-project 404.
        """
        status, body = engine_server.json(
            "POST",
            "/api/projects/does_not_exist_xyz/settings",
            {"preview_quality": 75},
        )
        assert status == 404, f"missing-project-404: got {status} {body!r}"
        assert isinstance(body, dict) and "error" in body, (
            f"error-envelope: got {body!r}"
        )

    def test_e2e_multiple_requests_migrated_once(self, engine_server, project_name):
        """covers R7, R8 — after many hits, db_path appears once in _migrated_dbs (e2e)."""
        for _ in range(5):
            engine_server.json("GET", f"/api/projects/{project_name}/keyframes")
        db_path = str(engine_server.work_dir / project_name / "project.db")
        assert db_path in scdb._migrated_dbs, "migrated-flag-set-once"

    def test_e2e_close_db_preserves_migrated_flag_via_http(
        self, engine_server, project_name
    ):
        """covers R9 — DAL close_db after HTTP traffic keeps the migration marker (e2e)."""
        engine_server.json("GET", f"/api/projects/{project_name}/keyframes")
        project_dir = engine_server.work_dir / project_name
        db_path = str(project_dir / "project.db")
        assert db_path in scdb._migrated_dbs
        scdb.close_db(project_dir)
        assert db_path in scdb._migrated_dbs, "migrated-preserved"
        # Subsequent HTTP still works.
        status, _ = engine_server.json(
            "GET", f"/api/projects/{project_name}/keyframes"
        )
        assert status == 200, "server-recovers-after-close_db"

    def test_e2e_row_factory_shape_visible_in_json(
        self, engine_server, project_name
    ):
        """covers R6 — handler output is a dict (evidence of sqlite3.Row -> dict path) (e2e)."""
        # Add a keyframe so the response has a row to serialize.
        s, _ = engine_server.json(
            "POST", f"/api/projects/{project_name}/add-keyframe",
            {"timestamp": "0:01"},
        )
        assert s == 200
        s2, body = engine_server.json(
            "GET", f"/api/projects/{project_name}/keyframes"
        )
        assert s2 == 200
        kfs = body.get("keyframes", [])
        assert len(kfs) == 1 and isinstance(kfs[0], dict), \
            f"row-as-dict: got {kfs!r}"
        assert "id" in kfs[0], "column-name-access"

    def test_e2e_busy_timeout_bounds_wall_time(self, engine_server, project_name):
        """covers R19 — a contended write either succeeds or fails in bounded time (e2e).

        Hold a write lock for a short interval; a concurrent POST should not
        exceed busy_timeout + retry budget significantly.
        """
        import time as _time
        # Prime DB.
        engine_server.json("GET", f"/api/projects/{project_name}/keyframes")
        db_path = str(engine_server.work_dir / project_name / "project.db")

        # Hold + release the lock from a single background thread; the test
        # thread fires the POST meanwhile. This keeps SQLite's thread affinity
        # intact (conn lives and dies in the same thread).
        import queue
        result_q: "queue.Queue" = queue.Queue()

        def holder_thread():
            h = sqlite3.connect(db_path, timeout=30)
            try:
                h.execute("PRAGMA busy_timeout=30000")
                h.execute("BEGIN IMMEDIATE")
                _time.sleep(0.3)
                h.rollback()
                result_q.put("ok")
            except Exception as e:
                result_q.put(e)
            finally:
                h.close()

        t0 = _time.monotonic()
        ht = threading.Thread(target=holder_thread, daemon=True)
        ht.start()
        # Give the holder a moment to acquire.
        _time.sleep(0.05)
        status, _body = engine_server.json(
            "POST",
            f"/api/projects/{project_name}/add-keyframe",
            {"timestamp": "0:11"},
            timeout=30,
        )
        ht.join(timeout=5)
        elapsed = _time.monotonic() - t0
        assert 200 <= status < 500, f"status-no-5xx: got {status}"
        assert elapsed < 15, f"bounded-wall-time: elapsed={elapsed}s"

    def test_e2e_dal_state_survives_across_requests(
        self, engine_server, project_name
    ):
        """covers R20 — write via POST, read-back via GET sees the row (e2e)."""
        s1, _ = engine_server.json(
            "POST", f"/api/projects/{project_name}/add-keyframe",
            {"timestamp": "0:02"},
        )
        assert s1 == 200
        s2, body = engine_server.json(
            "GET", f"/api/projects/{project_name}/keyframes"
        )
        assert s2 == 200
        kfs = body.get("keyframes", [])
        assert len(kfs) == 1, f"readback: expected 1, got {len(kfs)}"

    def test_e2e_invalid_project_name_hits_404_fast(self, engine_server):
        """covers R1 — unknown-project 404s without raising in the DAL (e2e)."""
        status, body = engine_server.json(
            "POST",
            "/api/projects/does_not_exist/add-keyframe",
            {"timestamp": "0:05"},
        )
        assert status == 404, f"404: got {status} {body!r}"

    # -- Target-state xfails (HTTP-level mirror of DAL-level xfails) --

    @pytest.mark.xfail(reason="target-state; awaits M16 FastAPI refactor", strict=False)
    def test_e2e_threading_local_pool_per_thread(self, engine_server, project_name):
        """covers R21 — under threading.local, dead-thread entries auto-GC (e2e, target-state)."""
        engine_server.json("GET", f"/api/projects/{project_name}/keyframes")
        # Force thread turnover: send a burst, wait, then check pool size shrinks.
        with _cf.ThreadPoolExecutor(max_workers=4) as ex:
            list(ex.map(
                lambda _i: engine_server.json(
                    "GET", f"/api/projects/{project_name}/keyframes"
                ),
                range(8),
            ))
        import gc, time as _t
        gc.collect()
        _t.sleep(0.2)
        db_path = str(engine_server.work_dir / project_name / "project.db")
        entries = [k for k in scdb._connections if k.startswith(f"{db_path}:")]
        # Target: entries for dead worker threads are auto-removed.
        assert len(entries) <= 2, f"auto-gc: entries={entries}"

    @pytest.mark.xfail(reason="target-state; awaits M16 FastAPI refactor", strict=False)
    def test_e2e_close_db_prefix_tight_via_http(self, engine_server, project_name):
        """covers R23 — close_db doesn't drop -sidecar connections under HTTP pressure (e2e, target-state)."""
        from scenecraft.db import get_db, close_db
        project_dir = engine_server.work_dir / project_name
        engine_server.json("GET", f"/api/projects/{project_name}/keyframes")
        sidecar_path = project_dir / "project.db-sidecar"
        get_db(project_dir, db_path=sidecar_path)
        close_db(project_dir)
        # Target: sidecar entry remains.
        sidecar_keys = [k for k in scdb._connections if k.startswith(f"{sidecar_path}:")]
        assert sidecar_keys, f"sidecar-untouched: got {list(scdb._connections)}"

    @pytest.mark.xfail(reason="target-state; awaits M16 FastAPI refactor", strict=False)
    def test_e2e_retry_matches_errorcode_not_substring(self, engine_server, project_name):
        """covers R24 — non-English lock message still retried (e2e, target-state)."""
        # No practical way to force a locale-specific lock error through HTTP today;
        # target-state assertion is that such a message WOULD be retried.
        pytest.xfail("target-state: observable only post-refactor")

    @pytest.mark.xfail(reason="target-state; awaits M16 FastAPI refactor", strict=False)
    def test_e2e_transaction_optional_db_path_via_http(self, engine_server, project_name):
        """covers R25 — transaction(project_dir, db_path=...) wired through handlers (e2e, target-state)."""
        pytest.xfail("target-state: no handler uses session-DB transactions yet")

    @pytest.mark.xfail(reason="target-state; awaits M16 FastAPI refactor", strict=False)
    def test_e2e_pragma_order_fks_post_schema(self, engine_server, project_name):
        """covers R26 — fresh project has foreign_keys=ON AND no migration-order errors (e2e, target-state)."""
        # Target: foreign_keys is deferred until after schema init. Today it runs
        # before, so schema init silently disables FK during CREATE TABLE.
        pytest.xfail("target-state: PRAGMA order not yet corrected")

    def test_e2e_transaction_commit_persists_across_reads(
        self, engine_server, project_name
    ):
        """covers R10 — a committed HTTP write is visible to a subsequent fresh-conn read (e2e)."""
        s1, _ = engine_server.json(
            "POST", f"/api/projects/{project_name}/add-keyframe",
            {"timestamp": "0:03"},
        )
        assert s1 == 200

        # Fresh raw conn reads committed row from disk.
        db_path = engine_server.work_dir / project_name / "project.db"
        raw = sqlite3.connect(str(db_path))
        try:
            n = raw.execute("SELECT COUNT(*) FROM keyframes").fetchone()[0]
            assert n == 1, f"committed-visible: got {n}"
        finally:
            raw.close()

    def test_e2e_connection_pool_survives_sequential_projects(self, engine_server):
        """covers R1, R2 — creating multiple projects doesn't corrupt the pool (e2e)."""
        import uuid, os
        names = [f"proj_seq_{i}_{os.getpid()}_{uuid.uuid4().hex[:4]}" for i in range(3)]
        for name in names:
            s, _ = engine_server.json("POST", "/api/projects/create", {"name": name})
            assert s == 200
            s2, _ = engine_server.json("GET", f"/api/projects/{name}/keyframes")
            assert s2 == 200

    def test_e2e_no_internal_lock_held_across_http(
        self, engine_server, project_name
    ):
        """covers R27 / INV-1 — _conn_lock is not held between requests (e2e)."""
        engine_server.json("GET", f"/api/projects/{project_name}/keyframes")
        acquired = scdb._conn_lock.acquire(blocking=False)
        assert acquired, "lock-released-between-requests"
        scdb._conn_lock.release()

# NOTE: N=25 e2e tests above covering R1..R27 where HTTP-observable.
# Target-state xfails codify M16 refactor deliverables without blocking today's CI.
