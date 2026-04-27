# Task 70: Engine Connection Pool + Transactions Regression Tests

**Milestone**: [M18 — Engine Regression Test Suite](../../milestones/milestone-18-engine-regression-test-suite.md)
**Spec**: [`local.engine-connection-and-transactions`](../../specs/local.engine-connection-and-transactions.md)
**Design Reference**: [`local.engine-connection-and-transactions`](../../specs/local.engine-connection-and-transactions.md)
**Estimated Time**: 3-4 hours
**Dependencies**: None
**Status**: Not Started
**Repository**: `scenecraft-engine`

---

## Objective

Write unit tests for `local.engine-connection-and-transactions.md`. Lock in the spec's target-ideal contract + current transitional behavior so the M16 FastAPI refactor preserves both. Target-state tests that cannot pass today are marked `@pytest.mark.xfail(reason="target-state; awaits M16 FastAPI refactor", strict=False)`. This task also seeds the shared `tests/specs/conftest.py` fixtures that every subsequent M18 task reuses.

---

## Context

Connection pooling + transactions are the foundation every DAO sits on. The M16 refactor will swap the HTTP layer but must preserve: per-thread memoization, PRAGMA-on-create, WAL mode, busy_timeout=60000, `_retry_on_locked` backoff, `transaction(...)` commit/rollback semantics. This spec defines all of it. Because this task seeds the fixtures used by all of M18, it has no dependencies and must be completed first.

---

## Steps

### 1. Verify pytest is installed

`pyproject.toml` already lists `pytest>=7.0.0` and `pytest-asyncio>=0.21.0` under `[project.optional-dependencies].dev`. If the dev extras aren't installed in your environment: `pip install -e '.[dev]'`.

### 2. Read the spec fully

Read `agent/specs/local.engine-connection-and-transactions.md` end-to-end. Note every `Rn`, every Behavior Table row, and the test names in each row's `Tests` column (or under `### Base Cases` / `### Edge Cases`). Use them verbatim (kebab-case → snake_case) as pytest function names.

### 3. Seed shared fixtures

Create (or confirm):
- `tests/specs/__init__.py` — empty.
- `tests/specs/conftest.py` — shared fixtures for all M18 tasks.

Fixtures to seed in `tests/specs/conftest.py`:

```python
"""Shared fixtures for M18 spec-locked regression tests."""
from __future__ import annotations

import concurrent.futures
import threading
from pathlib import Path

import pytest

from scenecraft import db as scdb


@pytest.fixture
def project_dir(tmp_path: Path) -> Path:
    p = tmp_path / "proj"
    p.mkdir()
    return p


@pytest.fixture
def db_conn(project_dir: Path):
    conn = scdb.get_db(project_dir)
    try:
        yield conn
    finally:
        scdb.close_db(project_dir)


@pytest.fixture
def thread_pool():
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
        yield ex


@pytest.fixture
def engine_server():
    """Stub — overridden by e2e tasks that need a real server.

    Tasks that exercise HTTP/WS surface (T75-T87) install a real fixture
    in their own file or extend conftest.
    """
    pytest.skip("engine_server fixture not installed for this test")
```

Later tasks extend this conftest; do not duplicate.

### 4. Create the test file

Create `tests/specs/test_engine_connection_and_transactions.py`:

```python
"""
Regression tests for local.engine-connection-and-transactions.md.

Every test docstring opens with `(covers Rn, ...)` referencing the spec requirements.
Target-state tests are marked @pytest.mark.xfail(reason="target-state; awaits M16 FastAPI refactor", strict=False).
"""
import threading
import time

import pytest

from scenecraft import db as scdb
```

### 5. Translate requirements into pytest functions

For each `Rn` in the spec, write one or more test functions. Examples:

- `def test_connection_memoized_per_thread(project_dir):` — `"""covers R1, R3"""` — two `get_db(project_dir)` calls on the same thread return the same object.
- `def test_different_thread_gets_different_connection(project_dir, thread_pool):` — `"""covers R1, R3"""`.
- `def test_db_path_defaults_to_project_db(project_dir):` — `"""covers R2"""`.
- `def test_pragmas_applied_on_creation(project_dir):` — `"""covers R4, R5, R6"""` — assert `journal_mode=wal`, `synchronous=NORMAL` (=1), `foreign_keys=1`, `busy_timeout=60000`, `row_factory is sqlite3.Row`.
- `def test_schema_migration_runs_once_per_db_path(project_dir):` — `"""covers R7, R8"""` — monkeypatch `_ensure_schema` and count calls.
- `def test_close_db_closes_and_unmemoizes(project_dir):` — `"""covers R9"""`.
- `def test_transaction_commits_on_clean_exit(project_dir):` — `"""covers R10"""`.
- `def test_transaction_rolls_back_on_exception(project_dir):` — `"""covers R10, R11"""`.
- `def test_retry_on_locked_backoff_matches_spec(project_dir):` — `"""covers retry-semantics"""` — mock `OperationalError("database is locked")`, count attempts, assert delays.

Target-ideal behaviors (if any — e.g., bounded `_migrated_dbs` growth, explicit per-thread cleanup on thread death) get `@pytest.mark.xfail(reason="target-state; awaits M16 FastAPI refactor", strict=False)`.

### 6. Cover every Behavior Table row

Walk the spec's Behavior Table top to bottom. Every row maps to ≥1 test. For rows with `Tests: —`, write a descriptive test name and add a `# NOTE: derived from Behavior Table row <N>; spec did not name this test.` comment.

No silent omissions.

### 7. No e2e section

This is a DB-layer spec. Add a comment at the bottom:

```python
# NOTE: no e2e — local.engine-connection-and-transactions.md is a DB-layer spec; no HTTP/WS surface.
```

### 8. Run

```bash
pytest tests/specs/test_engine_connection_and_transactions.py -v
```

Every test must pass or xfail (yellow). No failures.

### 9. Verify coverage

```bash
pytest --collect-only tests/specs/test_engine_connection_and_transactions.py | wc -l
```

Must be ≥ the sum of `### Base Cases` + `### Edge Cases` in the spec.

```bash
# For each R1..Rn in the spec:
grep -E "covers .*R1\b" tests/specs/test_engine_connection_and_transactions.py
# ...
```

Every `Rn` must match at least one docstring.

### 10. Commit

```
git add tests/specs/__init__.py tests/specs/conftest.py tests/specs/test_engine_connection_and_transactions.py
git commit -m "test(M18-70): engine-connection-and-transactions regression tests — <N> unit"
```

---

## Verification

- [ ] `tests/specs/__init__.py` exists (empty)
- [ ] `tests/specs/conftest.py` exists with `project_dir`, `db_conn`, `thread_pool`, `engine_server` (stub) fixtures
- [ ] `tests/specs/test_engine_connection_and_transactions.py` exists
- [ ] Every `Rn` in the spec has ≥1 test with matching `(covers Rn)` docstring
- [ ] Every Behavior Table row covered (pass, xfail, or explicit `# NOTE` comment)
- [ ] Target-state tests use `@pytest.mark.xfail(reason="target-state; awaits M16 FastAPI refactor", strict=False)`
- [ ] `# NOTE: no e2e — DB-layer spec` comment at bottom of file
- [ ] `pytest tests/specs/test_engine_connection_and_transactions.py -v` passes
- [ ] `pytest --collect-only ... | wc -l` matches spec test count
- [ ] Bugs surfaced (if any) filed or fixed — not silently xfail'd

---

## Key Design Decisions

| Decision | Choice | Rationale |
|---|---|---|
| xfail vs skip | xfail, `strict=False` | Flipping to xpass is a visible refactor signal; `strict=False` lets already-met target rows through quietly. |
| Shared conftest owned by task-70 | Yes | Every subsequent M18 task reuses `project_dir` / `db_conn` / `thread_pool` / `engine_server`. Duplicating fixtures defeats the bidirectional-traceability goal. |
| Test name verbatim from spec | Required | Spec kebab-case names become pytest snake_case names; no creative renaming. |
| No e2e | Correct for DB-layer specs | Explicitly noted at bottom of file. |

---

## Notes

- This is the foundation task. Later tasks (T71-T87) depend on the fixtures seeded here.
- If `get_db` has a signature subtly different from what the spec documents, fix the spec or file an engine bug — don't just xfail with a generic reason.
- Keep thread-safety tests bounded in duration (<1s each); avoid flakiness.
