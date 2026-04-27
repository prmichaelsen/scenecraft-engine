# Task 75: Engine Migrations Framework Regression Tests

**Milestone**: [M18 — Engine Regression Test Suite](../../milestones/milestone-18-engine-regression-test-suite.md)
**Spec**: [`local.engine-migrations-framework`](../../specs/local.engine-migrations-framework.md)
**Design Reference**: [`local.engine-migrations-framework`](../../specs/local.engine-migrations-framework.md)
**Estimated Time**: 4-6 hours
**Dependencies**: task-70
**Status**: Not Started
**Repository**: `scenecraft-engine`

---

## Objective

Write unit + minimal e2e tests for `local.engine-migrations-framework.md`. Lock in the forward-only migration order, PRAGMA-detected in-place column adds, idempotence on re-run, and schema-version tracking. Target-state tests use `@pytest.mark.xfail(reason="target-state; awaits M16 FastAPI refactor", strict=False)`.

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

### 5. Add e2e section

```python
# === E2E ===

class TestEndToEnd:
    """Minimal e2e — boot the engine against a pre-migration DB fixture and assert schema converges."""

    def test_engine_boots_against_pre_migration_db(self, tmp_path):
        """covers Rn (e2e)"""
        # Copy a fixture DB from tests/specs/fixtures/pre-migration.db into tmp_path.
        # Call scenecraft.db.get_db(tmp_path). Assert no error.
        # Assert PRAGMA user_version or schema-version table reflects the latest.
        ...
```

Keep the e2e small — one scenario.

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
- [ ] E2E section present
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
