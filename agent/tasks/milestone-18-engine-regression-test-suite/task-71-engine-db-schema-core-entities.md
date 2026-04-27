# Task 71: Engine DB Core Entities Schema Regression Tests

**Milestone**: [M18 — Engine Regression Test Suite](../../milestones/milestone-18-engine-regression-test-suite.md)
**Spec**: [`local.engine-db-schema-core-entities`](../../specs/local.engine-db-schema-core-entities.md)
**Design Reference**: [`local.engine-db-schema-core-entities`](../../specs/local.engine-db-schema-core-entities.md)
**Estimated Time**: 3-4 hours
**Dependencies**: task-70
**Status**: Not Started
**Repository**: `scenecraft-engine`

---

## Objective

Write unit tests for `local.engine-db-schema-core-entities.md`. Lock in the current + target-ideal shape of the engine's core entity tables (projects, clips, tracks, keyframes, transitions, pool_segments, audio_tracks, audio_clips) so the M16 refactor can't silently drop a column, change a NULL/NOT NULL invariant, reorder a PK, or change a default. Target-state tests use `@pytest.mark.xfail(reason="target-state; awaits M16 FastAPI refactor", strict=False)`.

---

## Context

Core entity tables are the shape the rest of the engine agrees on. Any refactor touching the DB must not silently drop a column, change a NULL/NOT NULL invariant, or reorder a PK. This spec locks those invariants. Builds on task-70's `project_dir` + `db_conn` fixtures.

---

## Steps

### 1. Read the spec fully

Read `agent/specs/local.engine-db-schema-core-entities.md` end-to-end. Note every `Rn`, every Behavior Table row, and every test name. Use them verbatim.

### 2. Create the test file

Create `tests/specs/test_engine_db_schema_core_entities.py`. Import `db_conn` from the conftest seeded in task-70.

```python
"""
Regression tests for local.engine-db-schema-core-entities.md.
"""
import sqlite3
import pytest

from scenecraft import db as scdb
```

### 3. Translate requirements into pytest functions

For each `Rn` in the spec, write one or more test functions. Typical patterns:

- Table-exists tests — `def test_projects_table_exists(db_conn):` — `"""covers Rn"""` — `PRAGMA table_info('projects')` non-empty.
- Column-set tests — assert the set of `{row['name'] for row in pragma}` matches the spec exactly. Fail loud on extras and omissions.
- NULL/NOT NULL tests — attempt insert with NULL where spec says NOT NULL; assert `sqlite3.IntegrityError`.
- PK tests — `PRAGMA table_info` rows where `pk > 0`; assert order and columns match spec.
- FK tests — `PRAGMA foreign_key_list(<table>)`; assert references match spec.
- Default tests — insert a row with only NOT NULL fields; read back; assert defaults equal spec.
- Unique + index tests — `PRAGMA index_list`; `PRAGMA index_info`; assert indices match spec.

Target-ideal behaviors (columns that "should" be NOT NULL today but aren't due to historical debt, indices that "should" exist) get `@pytest.mark.xfail(reason="target-state; awaits M16 FastAPI refactor", strict=False)`.

### 4. Cover every Behavior Table row

Walk the Behavior Table. Every row maps to ≥1 test. `Tests: —` rows get a descriptive name + `# NOTE:` comment.

### 5. No e2e section

Schema tests are DB-only. At the bottom:

```python
# NOTE: no e2e — local.engine-db-schema-core-entities.md is a DB-layer spec; no HTTP/WS surface.
```

### 6. Run + verify

```bash
pytest tests/specs/test_engine_db_schema_core_entities.py -v
pytest --collect-only tests/specs/test_engine_db_schema_core_entities.py | wc -l
```

### 7. Commit

```
git add tests/specs/test_engine_db_schema_core_entities.py
git commit -m "test(M18-71): engine-db-schema-core-entities regression tests — <N> unit"
```

---

## Verification

- [ ] `tests/specs/test_engine_db_schema_core_entities.py` exists
- [ ] Every `Rn` has ≥1 test with matching `(covers Rn)` docstring
- [ ] Every Behavior Table row covered
- [ ] Target-state tests use `xfail(..., strict=False)`
- [ ] `# NOTE: no e2e` comment at bottom
- [ ] `pytest ... -v` passes
- [ ] `pytest --collect-only ...` matches spec test count

---

## Key Design Decisions

| Decision | Choice | Rationale |
|---|---|---|
| Assert exact column-set equality | Yes | Extras are as dangerous as omissions for refactor parity. |
| PRAGMA-based introspection (not ORM) | Yes | The engine uses raw sqlite3; tests should too. |
| xfail target-ideal NOT NULLs | Yes | Historical columns that "should" be NOT NULL but aren't — flip visible after cleanup. |

---

## Notes

- If the spec lists columns the engine doesn't actually have today, that's a spec bug — escalate.
- Introspection tests should be O(ms) each; keep the whole file fast.
