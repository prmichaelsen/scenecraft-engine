# Task 75: Engine Migrations Framework Regression Tests

**Milestone**: [M18 — Engine Regression Test Suite](../../milestones/milestone-18-engine-regression-test-suite.md)
**Spec**: [`local.engine-migrations-framework`](../../specs/local.engine-migrations-framework.md)
**Design Reference**: [`local.engine-migrations-framework`](../../specs/local.engine-migrations-framework.md)
**Estimated Time**: 9 hours
**Dependencies**: task-70
**Status**: Not Started
**Repository**: `scenecraft-engine`

---

## Objective

Write comprehensive unit AND e2e tests. E2E coverage MUST match unit coverage in breadth — every requirement's observable effect has an HTTP/WS-level test. Lock in the forward-only migration order, PRAGMA-detected in-place column adds, idempotence on re-run, and schema-version tracking. Target-state tests use `@pytest.mark.xfail(reason="target-state; awaits M16 FastAPI refactor", strict=False)`.

---

## Context

The migrations framework is how the engine converges schema across versions. Forward-only ordering, PRAGMA-detected in-place column adds, and idempotence are the invariants. A refactor that breaks idempotence corrupts existing project DBs on upgrade. Builds on task-70's fixtures.

---

## Steps

### 1. Read the spec fully

Read `agent/specs/local.engine-migrations-framework.md`. Note every `Rn`, Behavior Table row, test name.

### 2. Create the test file

Create `tests/specs/test_engine_migrations_framework.py`.

### 3. Translate requirements into pytest functions

Typical patterns:

- Idempotence — run migrations twice; assert no-op on second run (no altered rows, no error, no schema change).
- Forward-only order — inject a migration with a version lower than applied; assert rejection or skip per spec.
- PRAGMA-detected column add — migration ALTER TABLE ADD COLUMN is a no-op if the column already exists; assert no error.
- Schema-version table — assert row is present after init and monotonically increases.
- Fresh DB — run against empty DB; all tables + columns present.
- Partially-migrated DB — simulate a DB missing one recent migration; assert re-run adds only the missing parts.
- Migration isolation — each migration wrapped in a transaction; failure rolls back that migration only.

Target-ideal behaviors (e.g., rollback recording, dry-run mode, checksum-based integrity) → `xfail`.

### 4. Cover every Behavior Table row

### 5. E2E coverage checklist (comprehensive)

Migrations are observable through the live engine boot + subsequent HTTP behavior. E2E MUST verify schema convergence through real server boot and reachable endpoints.

Scenarios to exercise:

- Boot engine against a **clean work_dir** via `engine_server` fixture → GET `/api/admin/schema` (or equivalent) → assert schema_migrations rows present for every shipped migration
- Boot against a **partially-migrated work_dir** (fixture DB missing one migration) → assert only the missing migration runs; PRAGMA reports converged schema via a diagnostic endpoint
- Boot against a **fresh (empty) project DB** → assert full schema bootstrap; subsequent `POST /api/projects/:name/keyframes` succeeds
- Boot against a **fully-migrated DB** → assert idempotent (no migrations run; no altered rows); observable via stable schema_version row
- Boot against a DB with an **unknown future migration version** → assert legible error and graceful bail (no partial writes)
- Forward-only guard: if a future injection attempts a version lower than applied → reject (via admin endpoint if exposed, or boot-time log)
- PRAGMA-detected in-place ALTER idempotence: boot twice; assert no duplicate columns, no errors
- Migration isolation: inject a failing migration fixture; boot; assert rollback; subsequent healthy migration applies after fix
- Legacy NOT NULL `audio_clips.track_id` rebuild path (OQ-8 target) → xfail until `register_migration` + `rebuild_table` land

Each e2e test annotated `(covers Rn, row #N)`.

```python
# === E2E ===

class TestEndToEnd:
    """Comprehensive e2e — boot engine against varied DB fixtures; verify schema convergence via HTTP."""
    # ... tests per checklist
```

Fixture DBs live in `tests/specs/fixtures/` — keep each under 20 KB.

### 6. Run + verify + commit

```bash
pytest tests/specs/test_engine_migrations_framework.py -v
git add tests/specs/test_engine_migrations_framework.py tests/specs/fixtures/
git commit -m "test(M18-75): engine-migrations-framework regression tests — <N> unit + 1 e2e"
```

---

## Verification

- [ ] Test file exists
- [ ] Every `Rn` has ≥1 covering test
- [ ] Every Behavior Table row covered
- [ ] Target-state tests use `xfail(..., strict=False)`
- [ ] E2E section present with comprehensive boot + schema-convergence coverage
- [ ] Every spec requirement has ≥1 e2e test (not just unit)
- [ ] `pytest ... -v` passes
- [ ] Collect count matches spec

---

## Key Design Decisions

| Decision | Choice | Rationale |
|---|---|---|
| Fixture DBs in `tests/specs/fixtures/` | Yes | Shared across M18; small binary files are acceptable. |
| Idempotence tested by re-running, not mocking | Yes | Mocked idempotence is a lie. |

---

## Notes

- If no pre-migration fixture DB exists, create one by snapshotting a fresh `project.db` from the current engine and stripping one migration's effects.
- Keep fixture DBs under 20 KB each.
