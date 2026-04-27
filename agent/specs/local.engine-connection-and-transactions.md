# Spec: Engine DB — Connection Pool, Transactions, Retry Semantics

**Namespace**: local
**Version**: 1.0.0
**Created**: 2026-04-27
**Last Updated**: 2026-04-27
**Status**: Draft

---

**Purpose**: Define the black-box contract for scenecraft-engine's SQLite connection pool, transaction context manager, and lock-retry helper — the foundation every DAO in the engine sits on.

**Source**: `--from-draft` (retry of stalled spec job)
- Reference implementation: `/home/prmichaelsen/.acp/projects/scenecraft-engine/src/scenecraft/db.py` lines 1–100

---

## Scope

**In scope**:
- Connection lifecycle — creation, per-thread memoization, close, garbage behavior on thread death
- Thread safety of the `_connections` dict and `_migrated_dbs` set (protected by `_conn_lock`)
- `_retry_on_locked` semantics — attempt count, delay, substring matcher, non-lock passthrough
- `transaction(project_dir)` context manager contract — yields conn, commits on clean exit, rolls back and re-raises on exception
- PRAGMA settings applied on connection creation and their observable effects (WAL journal mode, NORMAL synchronous, foreign keys ON, 60s busy timeout)
- `check_same_thread=False` trade-offs (cross-thread reuse allowed; SQLite connection is still not concurrency-safe for simultaneous writes)
- Per-`db_path` isolation — session working-copy DBs and main project DBs are independently memoized

**Out of scope**:
- Specific table schemas and DAOs (separate specs per table/entity)
- Schema migration logic inside `_ensure_schema` (separate spec)
- Plugin sidecar table creation (separate spec)
- Cross-process coordination (single-process assumption)
- Read replicas / sharding

---

## Requirements

**R1 — Per-(db_path, thread) memoization**. `get_db` returns the same `sqlite3.Connection` object for repeated calls with the same resolved `db_path` from the same thread. A different thread calling `get_db` with the same `db_path` gets a different connection object.

**R2 — db_path resolution**. When `db_path` is None, the effective path is `project_dir / "project.db"` (as a string). When `db_path` is provided, it is used directly (allowing session working-copy DBs to be memoized separately from the main project DB).

**R3 — Thread-key format**. The memoization key is `f"{db_path}:{threading.current_thread().ident}"`. Two threads never collide on keys; two calls from the same thread to the same `db_path` always collide on keys.

**R4 — PRAGMAs applied at creation**. Every newly created connection has, before being returned for the first time: `journal_mode=WAL`, `synchronous=NORMAL`, `foreign_keys=ON`, `busy_timeout=60000`. These PRAGMAs are NOT re-applied on subsequent `get_db` calls that return a memoized connection.

**R5 — `check_same_thread=False`**. Connections are opened with `check_same_thread=False`, allowing a connection to be used from a thread other than the one that created it without sqlite3 raising `ProgrammingError`. Callers remain responsible for serializing concurrent writes on a single connection.

**R6 — Row factory**. `conn.row_factory` is set to `sqlite3.Row` on creation, so cursors yield index- and name-addressable rows.

**R7 — Schema migration runs once per db_path per process**. On first `get_db` for a given resolved `db_path`, `_ensure_schema(conn)` is invoked and the path is added to `_migrated_dbs`. Subsequent `get_db` calls for that `db_path` (from any thread) MUST NOT re-run `_ensure_schema`.

**R8 — `_conn_lock` protects shared state**. All mutations of `_connections` and `_migrated_dbs` happen while holding `_conn_lock`. The lock is held across the full "check-miss / open / PRAGMA / migrate / insert" sequence so two threads racing on a new `db_path` cannot both call `_ensure_schema`.

**R9 — `close_db` closes and unmemoizes all connections for a db_path**. `close_db(project_dir, db_path=None)` resolves `db_path` the same way `get_db` does, then finds every entry in `_connections` whose key starts with `f"{db_path}:"` (i.e., across all threads), calls `.close()` on each, and deletes the entry. After `close_db`, a subsequent `get_db` for the same `db_path` opens a fresh connection and re-runs PRAGMAs. `close_db` does NOT remove `db_path` from `_migrated_dbs`, so `_ensure_schema` still does not re-run.

**R10 — `transaction` yields a live connection, auto-commits on clean exit**. `transaction(project_dir)` is a context manager. On `__enter__` it returns the connection from `get_db(project_dir)` (with `db_path=None` — always the main project DB). On clean `__exit__` it calls `conn.commit()`. On exception during the `with` body, it calls `conn.rollback()` and re-raises the original exception unchanged.

**R11 — `transaction` does not open a NEW connection**. `transaction` reuses the memoized per-thread connection from `get_db`. Nested `with transaction(...)` blocks on the same thread share the same underlying connection (SQLite does not support true nested transactions via this helper; the inner `commit` flushes the outer's work).

**R12 — `_retry_on_locked` retries up to 5 attempts**. Signature: `_retry_on_locked(fn, max_retries=5, delay=0.2)`. The helper calls `fn()` up to `max_retries` times. On attempt `attempt` (zero-indexed), if `fn()` raises `sqlite3.OperationalError` AND the exception message contains the substring `"locked"` AND `attempt < max_retries - 1`, the helper sleeps `delay * (attempt + 1)` seconds and retries.

**R13 — Linear backoff, not exponential**. With the default `delay=0.2`, sleeps between attempts are `0.2, 0.4, 0.6, 0.8` seconds (between attempts 0→1, 1→2, 2→3, 3→4). Total worst-case wall time before final raise ≈ 2.0 s of sleep plus the work inside each `fn()` call.

**R14 — Non-lock `OperationalError` is not retried**. If `fn()` raises `sqlite3.OperationalError` whose message does NOT contain `"locked"`, the helper re-raises immediately on the first occurrence.

**R15 — Non-`OperationalError` is not caught**. Any other exception class (`IntegrityError`, `ProgrammingError`, arbitrary `Exception`) propagates out of `_retry_on_locked` on the first occurrence without retry.

**R16 — Final attempt failure re-raises the original exception**. When `attempt == max_retries - 1` and `fn()` still raises a lock error, the helper re-raises that exception (no wrapping, no retry count added).

**R17 — Return value passthrough**. On first successful attempt (or any successful retry), the helper returns whatever `fn()` returned.

**R18 — WAL journal mode enables concurrent reads during writes**. Because `journal_mode=WAL` is set, multiple reader threads/processes can read the database while a single writer commits, without blocking each other beyond the busy_timeout window.

**R19 — 60-second busy timeout at the SQLite layer**. `busy_timeout=60000` means SQLite itself will internally wait up to 60 seconds for a lock before raising `OperationalError: database is locked`. Combined with `_retry_on_locked`, the effective ceiling before a caller sees an exception is roughly `5 × 60 s` (SQLite-layer) `+ 4 × linear backoff sleeps` (retry layer) — **flagged as undefined** whether this compound behavior is intentional.

**R20 — Connection object identity is stable across `get_db` calls**. Repeated `get_db` calls (same thread, same `db_path`) return the exact same `sqlite3.Connection` instance (identity comparable via `is`), not a wrapper.

---

## Interfaces

### `get_db(project_dir: Path, db_path: Path | str | None = None) -> sqlite3.Connection`

Returns the per-thread memoized SQLite connection for the resolved `db_path`. Creates and PRAGMA-configures it on first call for that `(db_path, thread)` key.

### `close_db(project_dir: Path, db_path: Path | str | None = None) -> None`

Closes every thread-local connection whose thread_key starts with the resolved `db_path`, and removes them from `_connections`. Leaves `_migrated_dbs` untouched.

### `transaction(project_dir: Path) -> ContextManager[sqlite3.Connection]`

Context manager. Yields the per-thread connection. Commits on clean exit; rolls back and re-raises on exception.

### `_retry_on_locked(fn: Callable[[], T], max_retries: int = 5, delay: float = 0.2) -> T`

Calls `fn()` up to `max_retries` times, sleeping `delay * (attempt + 1)` seconds between attempts when the exception is an `sqlite3.OperationalError` whose message contains `"locked"`. Re-raises any other exception or the final lock error.

### Module-level state

- `_connections: dict[str, sqlite3.Connection]` — keyed by `f"{db_path}:{thread.ident}"`
- `_conn_lock: threading.Lock` — guards `_connections` and `_migrated_dbs`
- `_migrated_dbs: set[str]` — `db_path` strings for which `_ensure_schema` has run this process

---

## Behavior Table

| # | Scenario | Expected Behavior | Tests |
|---|----------|-------------------|-------|
| 1 | First `get_db` for a project from thread A | Opens connection, applies 4 PRAGMAs, runs schema init, memoizes, returns conn | `get-db-first-call-opens-and-configures`, `get-db-applies-all-pragmas` |
| 2 | Second `get_db` from same thread same path | Returns the same connection object (identity) | `get-db-memoizes-per-thread` |
| 3 | `get_db` from a different thread same path | Returns a DIFFERENT connection object | `get-db-separates-by-thread` |
| 4 | `get_db` with explicit `db_path` override | Uses that path, separate memoization key from default | `get-db-explicit-path-isolated` |
| 5 | Schema init runs once per db_path per process | First call runs `_ensure_schema`, later calls (any thread) don't | `schema-init-runs-once-per-db-path` |
| 6 | `close_db` after connections held by two threads | Both threads' connections closed and removed from pool | `close-db-removes-all-threads` |
| 7 | `get_db` after `close_db` | Opens a fresh connection, re-applies PRAGMAs, does NOT re-run schema | `get-db-after-close-reopens`, `close-db-preserves-migrated-flag` |
| 8 | `transaction` clean exit | `commit()` called, body's changes visible after block | `transaction-commits-on-clean-exit` |
| 9 | `transaction` body raises | `rollback()` called, exception re-raised unchanged, changes discarded | `transaction-rolls-back-on-exception`, `transaction-reraises-original-exception` |
| 10 | `_retry_on_locked` with fn succeeding first try | Returns fn's value, no sleep | `retry-returns-value-no-sleep` |
| 11 | `_retry_on_locked` with lock error, succeeds on attempt 3 | Retries, sleeps `0.2, 0.4`, returns value | `retry-succeeds-after-lock-retries` |
| 12 | `_retry_on_locked` exhausts 5 attempts all lock errors | Re-raises the last `OperationalError` | `retry-exhausts-reraises-lock-error` |
| 13 | `_retry_on_locked` with non-"locked" OperationalError | Re-raises on first occurrence, no retry | `retry-passes-through-non-lock-operational` |
| 14 | `_retry_on_locked` with IntegrityError | Re-raises on first occurrence, no retry | `retry-passes-through-non-operational` |
| 15 | Backoff schedule with default delay | Sleeps are `0.2, 0.4, 0.6, 0.8` (linear, NOT exponential) | `retry-linear-backoff-schedule` |
| 16 | Concurrent `get_db` from two threads for a new db_path | `_ensure_schema` runs exactly once, both threads get distinct conns | `concurrent-first-get-db-migrates-once` |
| 17 | PRAGMAs not reapplied on memoized fetch | Second `get_db` does not re-issue any PRAGMA statement | `pragmas-not-reapplied-on-cached-fetch` |
| 18 | `conn.row_factory` is `sqlite3.Row` | Query results support both index and column-name access | `rows-are-addressable-by-name` |
| 19 | WAL allows concurrent read during write | Reader thread can SELECT while writer is mid-transaction on another conn | `wal-allows-concurrent-read-during-write` |
| 20 | Two threads write concurrently via the SAME memoized connection | `undefined` — `check_same_thread=False` bypasses sqlite3's guard, but SQLite connection is not safe for simultaneous writes | → [OQ-1](#open-questions) |
| 21 | Thread dies without calling `close_db` | `undefined` — connection stays in `_connections` dict indefinitely (apparent leak) | → [OQ-2](#open-questions) |
| 22 | Retry exhaustion at 5×0.2s is the final contract? | `undefined` — callers may need higher-level retry; not documented | → [OQ-3](#open-questions) |
| 23 | `close_db` called while another thread is mid-query on that conn | `undefined` — closes the conn the other thread holds; behavior under concurrent use unspecified | → [OQ-4](#open-questions) |
| 24 | `transaction` body swallows exception (catches + suppresses internally) | No rollback triggered — exits cleanly, commits | `transaction-only-rolls-back-on-propagated-exception` |
| 25 | Nested `with transaction(...)` on same thread | Both use same underlying conn; inner commit flushes outer's work (no true nesting) | `nested-transactions-share-connection` |
| 26 | `busy_timeout=60000` honored | SQLite waits up to 60s at C layer before raising locked | `busy-timeout-configured-60s` |
| 27 | foreign_keys=ON enforced | DELETE violating FK raises IntegrityError | `foreign-keys-enforced` |

---

## Behavior (step-by-step)

### `get_db(project_dir, db_path=None)`

1. Resolve `db_path`: if None, use `str(project_dir / "project.db")`; else `str(db_path)`.
2. Compute `thread_key = f"{db_path}:{threading.current_thread().ident}"`.
3. Acquire `_conn_lock`.
4. If `thread_key in _connections`, return `_connections[thread_key]` (still holding lock until return statement unwinds).
5. Else, open `sqlite3.connect(db_path, check_same_thread=False, timeout=60)`.
6. Set `conn.row_factory = sqlite3.Row`.
7. Execute the four PRAGMAs in order: `journal_mode=WAL`, `synchronous=NORMAL`, `foreign_keys=ON`, `busy_timeout=60000`.
8. If `db_path not in _migrated_dbs`: call `_ensure_schema(conn)`, then `_migrated_dbs.add(db_path)`.
9. Store `_connections[thread_key] = conn`.
10. Return `conn`.
11. Release `_conn_lock`.

### `close_db(project_dir, db_path=None)`

1. Resolve `db_path` the same way as `get_db`.
2. Acquire `_conn_lock`.
3. Compute `to_remove = [k for k in _connections if k.startswith(f"{db_path}:")]` (note: current implementation uses `k.startswith(db_path)` which is technically a looser prefix match — see OQ-5).
4. For each key: call `_connections[k].close()`; `del _connections[k]`.
5. Do NOT touch `_migrated_dbs`.
6. Release `_conn_lock`.

### `transaction(project_dir)`

1. `conn = get_db(project_dir)` (no `db_path` override — always main project DB).
2. `yield conn`.
3. On normal resume: `conn.commit()`.
4. On exception: `conn.rollback()`, then re-raise.

### `_retry_on_locked(fn, max_retries=5, delay=0.2)`

1. Loop `attempt in range(max_retries)`:
   a. Try `return fn()`.
   b. On `sqlite3.OperationalError as e`:
      - If `"locked" in str(e)` AND `attempt < max_retries - 1`: `time.sleep(delay * (attempt + 1))`; continue.
      - Else: re-raise.

---

## Acceptance Criteria

- [ ] Same thread + same `db_path` → `get_db` returns identical connection object on repeated calls.
- [ ] Two threads on same `db_path` → distinct connection objects.
- [ ] First `get_db` for a `db_path` runs `_ensure_schema` exactly once; no other `get_db` call in the process runs it again for that path.
- [ ] All four PRAGMAs applied on connection creation, in the order: journal_mode, synchronous, foreign_keys, busy_timeout.
- [ ] `close_db` closes every per-thread connection for the resolved `db_path` and unmemoizes them; does not reset `_migrated_dbs`.
- [ ] `transaction` commits on clean exit, rolls back and re-raises on exception, reuses the per-thread connection.
- [ ] `_retry_on_locked` retries up to 5 attempts on lock errors with linear backoff `delay * (attempt + 1)`.
- [ ] `_retry_on_locked` does NOT retry on non-lock `OperationalError` or any other exception type.
- [ ] Concurrent first-open on a new `db_path` from two threads invokes `_ensure_schema` exactly once.

---

## Tests

### Base Cases

#### Test: get-db-first-call-opens-and-configures (covers R1, R4, R6)

**Given**: a fresh project dir with no existing `project.db`, an empty `_connections`, and an empty `_migrated_dbs`.
**When**: `get_db(project_dir)` is called.
**Then**:
- **returns-connection**: the return value is a `sqlite3.Connection` instance.
- **file-exists**: `project_dir / "project.db"` now exists on disk.
- **row-factory-set**: the returned connection's `row_factory` is `sqlite3.Row`.
- **memoized**: `_connections` now has exactly one entry whose key ends with the current thread's ident.

#### Test: get-db-applies-all-pragmas (covers R4)

**Given**: a fresh project dir.
**When**: `get_db(project_dir)` is called and we query pragma state via the returned conn.
**Then**:
- **wal-mode**: `PRAGMA journal_mode` returns `"wal"`.
- **sync-normal**: `PRAGMA synchronous` returns `1` (NORMAL).
- **fk-on**: `PRAGMA foreign_keys` returns `1`.
- **busy-timeout-60s**: `PRAGMA busy_timeout` returns `60000`.

#### Test: get-db-memoizes-per-thread (covers R1, R20)

**Given**: a project dir.
**When**: `get_db(project_dir)` is called three times in a row from the same thread.
**Then**:
- **identity-stable**: all three return values are the same object (`is`-equal).
- **pool-size-one**: `_connections` contains exactly one matching key after all three calls.

#### Test: get-db-separates-by-thread (covers R1, R3)

**Given**: a project dir, `_connections` empty.
**When**: thread A calls `get_db(project_dir)`, then thread B calls `get_db(project_dir)`.
**Then**:
- **distinct-conns**: A's and B's returned connections are NOT the same object.
- **two-entries**: `_connections` has exactly two entries, keys differing only in the trailing thread ident.

#### Test: get-db-explicit-path-isolated (covers R2)

**Given**: a project dir with default `project.db`, and a custom session db path `session.db`.
**When**: thread A calls `get_db(project_dir)` and `get_db(project_dir, db_path=session_path)`.
**Then**:
- **distinct-conns**: the two connections are different objects.
- **separate-keys**: `_connections` has two entries with different `db_path` prefixes.

#### Test: schema-init-runs-once-per-db-path (covers R7)

**Given**: a test double or spy on `_ensure_schema`; a fresh project dir.
**When**: `get_db` is called 5 times (3 from thread A, 2 from thread B) for the same `db_path`.
**Then**:
- **ensure-called-once**: `_ensure_schema` was invoked exactly once.
- **migrated-flag-set**: `db_path` string is in `_migrated_dbs` after the first call.

#### Test: close-db-removes-all-threads (covers R9)

**Given**: two threads have each called `get_db(project_dir)` and hold live conns; `_connections` has two matching entries.
**When**: `close_db(project_dir)` is called.
**Then**:
- **pool-empty**: no `_connections` entry remains whose key starts with `db_path`.
- **conns-closed**: both previously-held connections are closed (any further `.execute` raises `ProgrammingError: Cannot operate on a closed database`).

#### Test: close-db-preserves-migrated-flag (covers R9)

**Given**: `_migrated_dbs` contains `db_path` after a prior `get_db`.
**When**: `close_db(project_dir)` is called.
**Then**:
- **migrated-still-set**: `db_path` remains in `_migrated_dbs`.

#### Test: get-db-after-close-reopens (covers R9)

**Given**: `close_db(project_dir)` has been called; `_connections` is empty for this `db_path`.
**When**: `get_db(project_dir)` is called again from the same thread.
**Then**:
- **new-connection-object**: the returned conn is NOT the same object as the pre-close conn.
- **pragmas-reapplied**: all four PRAGMAs return their configured values on the new conn.
- **schema-not-reinit**: `_ensure_schema` is NOT invoked again (verified via spy).

#### Test: transaction-commits-on-clean-exit (covers R10)

**Given**: an empty table `probe(id INTEGER PRIMARY KEY, v TEXT)`.
**When**: `with transaction(project_dir) as conn: conn.execute("INSERT INTO probe VALUES (1, 'a')")` completes without raising.
**Then**:
- **row-persisted**: a fresh `SELECT COUNT(*) FROM probe` (via a new conn or the same conn after block) returns 1.
- **no-pending-tx**: `conn.in_transaction` is `False` after the block.

#### Test: transaction-rolls-back-on-exception (covers R10)

**Given**: an empty `probe` table.
**When**: the body raises `ValueError` after inserting a row: `with transaction(...) as conn: conn.execute("INSERT ..."); raise ValueError("boom")`.
**Then**:
- **no-row-persisted**: `SELECT COUNT(*) FROM probe` returns 0.
- **no-pending-tx**: `conn.in_transaction` is `False` after the block.

#### Test: transaction-reraises-original-exception (covers R10)

**Given**: same as above.
**When**: the body raises a specific `ValueError("boom")`.
**Then**:
- **exception-propagates**: the caller catches `ValueError` whose `args == ("boom",)`.
- **exception-type-unwrapped**: the caught exception is not wrapped in any other type.

#### Test: retry-returns-value-no-sleep (covers R17)

**Given**: `fn` returns `"ok"` immediately.
**When**: `_retry_on_locked(fn)` is called with a stubbed `time.sleep` spy.
**Then**:
- **returns-ok**: return value is `"ok"`.
- **no-sleep**: the `sleep` spy was never called.
- **one-call**: `fn` was called exactly once.

#### Test: retry-succeeds-after-lock-retries (covers R12, R13)

**Given**: `fn` raises `OperationalError("database is locked")` on attempts 0 and 1, returns `"ok"` on attempt 2; a stubbed `time.sleep` spy.
**When**: `_retry_on_locked(fn)` is called.
**Then**:
- **returns-ok**: return value is `"ok"`.
- **called-three-times**: `fn` was invoked exactly 3 times.
- **two-sleeps**: `sleep` was called exactly twice.
- **sleep-values**: the sleep arguments, in order, are `0.2` and `0.4`.

#### Test: retry-exhausts-reraises-lock-error (covers R12, R16)

**Given**: `fn` always raises `OperationalError("database is locked")`; stubbed sleep.
**When**: `_retry_on_locked(fn)` is called.
**Then**:
- **raises-operational**: an `sqlite3.OperationalError` is raised.
- **message-contains-locked**: the exception's message contains `"locked"`.
- **five-calls**: `fn` was invoked exactly 5 times.
- **four-sleeps**: `sleep` was called exactly 4 times with args `0.2, 0.4, 0.6, 0.8`.

#### Test: retry-passes-through-non-lock-operational (covers R14)

**Given**: `fn` raises `OperationalError("no such table: foo")` on first call.
**When**: `_retry_on_locked(fn)` is called.
**Then**:
- **raises-operational**: an `sqlite3.OperationalError` is raised.
- **one-call**: `fn` invoked exactly once.
- **no-sleep**: `sleep` not called.

#### Test: retry-passes-through-non-operational (covers R15)

**Given**: `fn` raises `sqlite3.IntegrityError("UNIQUE constraint failed")`.
**When**: `_retry_on_locked(fn)` is called.
**Then**:
- **raises-integrity**: `IntegrityError` is raised, not caught.
- **one-call**: `fn` invoked exactly once.
- **no-sleep**: `sleep` not called.

### Edge Cases

#### Test: retry-linear-backoff-schedule (covers R13)

**Given**: `fn` always raises lock errors; stubbed `time.sleep`.
**When**: `_retry_on_locked(fn, max_retries=5, delay=0.2)` runs to exhaustion.
**Then**:
- **sleep-sequence**: the recorded `sleep` arguments, in order, are exactly `[0.2, 0.4, 0.6, 0.8]` — linear, not exponential.

#### Test: concurrent-first-get-db-migrates-once (covers R7, R8)

**Given**: a fresh `db_path` not in `_migrated_dbs`; a spy on `_ensure_schema`; two threads ready to invoke `get_db(project_dir)` simultaneously (started via a barrier).
**When**: both threads call `get_db` at the same time.
**Then**:
- **ensure-called-once**: `_ensure_schema` was invoked exactly once.
- **both-got-conns**: each thread received a valid `sqlite3.Connection` instance.
- **conns-distinct**: the two threads' connections are different objects.
- **migrated-flag-set**: `db_path` is in `_migrated_dbs` after both return.

#### Test: pragmas-not-reapplied-on-cached-fetch (covers R4)

**Given**: a spy wrapping `conn.execute` on a fresh connection after first `get_db`.
**When**: `get_db(project_dir)` is called a second time from the same thread.
**Then**:
- **no-new-pragma-calls**: the spy records zero new `PRAGMA ...` executions during the second call.

#### Test: rows-are-addressable-by-name (covers R6)

**Given**: a table `probe(id INTEGER, name TEXT)` with one row `(1, 'alice')`.
**When**: `conn.execute("SELECT id, name FROM probe").fetchone()` returns a row `r`.
**Then**:
- **by-index**: `r[0] == 1` and `r[1] == 'alice'`.
- **by-name**: `r["id"] == 1` and `r["name"] == 'alice'`.

#### Test: wal-allows-concurrent-read-during-write (covers R18)

**Given**: two connections (one per thread) to the same `db_path` in WAL mode; a populated `probe` table.
**When**: thread A opens a write transaction and inserts a row but does not commit; thread B runs `SELECT COUNT(*) FROM probe` concurrently.
**Then**:
- **read-not-blocked**: thread B's `SELECT` returns a value (the pre-write count) within the busy_timeout window rather than raising locked.
- **write-uncommitted-invisible**: thread B sees the pre-write count, not the inserted row.

#### Test: transaction-only-rolls-back-on-propagated-exception (covers R10)

**Given**: a transaction body that internally catches and suppresses an error after a write: `with transaction(...) as conn: try: conn.execute("INSERT ..."); raise RuntimeError except RuntimeError: pass`.
**When**: the `with` block exits cleanly (no exception propagating out).
**Then**:
- **committed**: the inserted row is persisted.
- **no-rollback-triggered**: a rollback spy records zero calls.

#### Test: nested-transactions-share-connection (covers R11)

**Given**: an empty `probe` table.
**When**: `with transaction(pd) as outer: outer.execute("INSERT (1)"); with transaction(pd) as inner: inner.execute("INSERT (2)")`.
**Then**:
- **same-conn**: `outer is inner` is True.
- **both-rows-persisted**: after both blocks exit cleanly, `SELECT COUNT(*) FROM probe` returns 2.
- **inner-commit-flushes-outer**: after the inner block but before the outer exits, a separate reader conn sees both rows (inner `commit()` flushed the outer's work too).

#### Test: busy-timeout-configured-60s (covers R19)

**Given**: a freshly opened conn via `get_db`.
**When**: querying `PRAGMA busy_timeout`.
**Then**:
- **value-60000**: returns `60000`.

#### Test: foreign-keys-enforced (covers R4)

**Given**: a parent/child schema with `FOREIGN KEY(parent_id) REFERENCES parent(id)`; one parent row and one child row referencing it.
**When**: attempting `DELETE FROM parent WHERE id = <referenced_id>`.
**Then**:
- **raises-integrity**: `sqlite3.IntegrityError` is raised with message containing "FOREIGN KEY".
- **parent-not-deleted**: parent row still exists after the failed delete.

---

## Non-Goals

- Defining schema migration steps (handled by `_ensure_schema`, separate spec).
- Cross-process / multi-host coordination — single-process engine assumption.
- Retrying at the DAO/API layer above `_retry_on_locked` — callers may add their own retries but that is out of scope here.
- Automatic cleanup of connections whose owning thread has died — see OQ-2.
- Safe concurrent writes on a single shared connection — this helper does NOT provide that; callers must avoid sharing a conn across threads for concurrent writes.
- Read-only / in-memory DB modes.

---

## Open Questions

**OQ-1 — Concurrent writes on the same connection object.** `check_same_thread=False` lets any thread call `.execute()` on a connection created by another thread, but the underlying SQLite connection object is NOT thread-safe for simultaneous writes. Today, the pool keys by thread ident so each thread gets its own conn — but if a caller stashes a conn reference and hands it to another thread, simultaneous writes are possible. Expected behavior: undefined (implementation-dependent; may corrupt state, may raise, may silently interleave). Decision needed: do we document "never share a connection across threads" as a hard rule, or add a per-conn write lock?

**OQ-2 — Connections abandoned by dead threads.** If a worker thread terminates without calling `close_db`, its entry persists in `_connections` indefinitely. The thread ident will be reused by the OS for future threads, potentially colliding. Expected behavior: undefined. Options: (a) accept the leak (small, bounded by live-thread count over process lifetime); (b) use `threading.local` for per-thread storage so dict entries GC with the thread; (c) add an explicit `close_db_for_current_thread()` helper.

**OQ-3 — Retry exhaustion at 5 × 0.2s ≈ 1-2s total. Is that the final contract?** Combined with `busy_timeout=60000` at the SQLite layer, the effective ceiling before a lock error reaches an API caller is ~60s × 5 = 300s of wall time worst-case. That is either "already enough, callers shouldn't retry" OR "deceptive — callers should add their own retry loop because occasional minute-long stalls are acceptable but a raised exception isn't". Decision needed: clarify in the caller-facing contract.

**OQ-4 — `close_db` called while another thread holds that conn.** `close_db` iterates all thread_keys for the `db_path` and calls `.close()` on each, including one currently being used mid-query by another live thread. SQLite's behavior under close-during-use is undefined. Expected: undefined (may corrupt, may raise on the other thread's next `.execute`).

**OQ-5 — `close_db` prefix match is too loose.** Current code: `[k for k in _connections if k.startswith(db_path)]`. If `db_path == "/a/project.db"` and another memoized key is for `/a/project.db-wal-sidecar:123`, it would match accidentally. A tighter match would be `k.startswith(f"{db_path}:")`. Decision needed: tighten the match, or confirm loose prefix is intentional.

**OQ-6 — `_retry_on_locked` substring matcher.** Matching `"locked" in str(e)` is heuristic. SQLite error messages include `"database is locked"` and `"database table is locked"` which both match; but a future SQLite version or a localized build could phrase it differently. Decision needed: match on `sqlite3.OperationalError` error code (e.g., `SQLITE_BUSY` / `SQLITE_LOCKED`) instead of substring?

**OQ-7 — `transaction` only accepts `project_dir`, not `db_path`.** If a caller is working on a session working-copy DB, they must call `get_db(project_dir, db_path=session_path)` directly and manage `commit`/`rollback` by hand. Should `transaction` accept an optional `db_path` for symmetry?

**OQ-8 — PRAGMA order matters?** PRAGMAs are executed before `_ensure_schema`. Should any PRAGMA be deferred (e.g., `foreign_keys=ON` during schema creation could cause migration-order issues with FK-carrying tables)? Current behavior: all four applied before migration.

---

## Related Artifacts

- Source implementation: `/home/prmichaelsen/.acp/projects/scenecraft-engine/src/scenecraft/db.py` lines 1–100
- Related specs (future / out-of-scope here):
  - Schema migration spec (`_ensure_schema` contract)
  - Per-table DAO specs (beatmap, light_show, chat, etc.)
  - Plugin sidecar table spec (plugin-owned `<plugin_id>__<table>` naming)
- Related patterns: "plugins own their own sidecar tables"; "SceneCraft project DB location"; "plugins can't alter core schema"
