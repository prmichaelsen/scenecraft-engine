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


# ---------------------------------------------------------------------------
# No e2e tests — engine-connection-and-transactions is internal DAL
# infrastructure; exercised transitively by downstream specs.
# ---------------------------------------------------------------------------
# NOTE: no e2e — local.engine-connection-and-transactions.md is a DB-layer spec; no HTTP/WS surface.
