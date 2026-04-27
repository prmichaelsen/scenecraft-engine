# Task 73: Engine DB Undo/Redo Regression Tests

**Milestone**: [M18 — Engine Regression Test Suite](../../milestones/milestone-18-engine-regression-test-suite.md)
**Spec**: [`local.engine-db-undo-redo`](../../specs/local.engine-db-undo-redo.md)
**Design Reference**: [`local.engine-db-undo-redo`](../../specs/local.engine-db-undo-redo.md)
**Estimated Time**: 9 hours
**Dependencies**: task-70, task-71
**Status**: Not Started
**Repository**: `scenecraft-engine`

---

## Objective

Write comprehensive unit AND e2e tests. E2E coverage MUST match unit coverage in breadth — every requirement's observable effect has an HTTP/WS-level test. Lock in the trigger-based inverse-SQL capture contract (BEGIN/END groups, nested behavior, idempotence after redo, redo-stack invalidation on new mutation). Target-state tests use `@pytest.mark.xfail(reason="target-state; awaits M16 FastAPI refactor", strict=False)`.

---

## Context

The undo/redo subsystem uses a trigger-based inverse-SQL capture — load-bearing for the chat pipeline's atomicity. This spec locks the trigger contract. Builds on task-70/71 fixtures.

---

## Steps

### 1. Read the spec fully

Read `agent/specs/local.engine-db-undo-redo.md`. Note every `Rn`, Behavior Table row, test name.

### 2. Create the test file

Create `tests/specs/test_engine_db_undo_redo.py`.

### 3. Translate requirements into pytest functions

Typical patterns:

- `def test_undo_begin_opens_group(project_dir):` — `"""covers R1"""` — insert + undo_end; assert one undo group with the insert's inverse SQL.
- Basic roundtrip — insert → undo → row gone; redo → row back.
- Multi-op group — N inserts inside one begin/end; undo reverses all; redo redoes all as a unit.
- Nested groups — behavior per spec (flatten? reject? outer-wins?). Test the spec's chosen semantics.
- Redo-stack invalidation — after a new mutation post-undo, redo stack should be empty.
- Trigger scope — mutations outside undo_begin/end should NOT capture.
- Idempotence — undo → redo → undo produces the same visible state as the first undo.

Target-ideal behaviors (e.g., group sequence IDs, efficient redo-stack pruning, cross-session persistence) → `xfail`.

### 4. Cover every Behavior Table row

### 5. E2E coverage checklist (comprehensive)

Undo/redo is exposed through HTTP. E2E MUST verify round-trip state through the live API, not just trigger internals.

Endpoints to exercise (walk the spec's Behavior Table):

- `POST /api/projects/:name/undo` — undoes the most recent group; subsequent GET verifies original state
- `POST /api/projects/:name/redo` — redoes the last undone group; subsequent GET matches post-mutation state
- Round-trip: mutation (POST keyframe) → undo → GET shows no row → redo → GET shows row back
- Multi-op group: a chat tool that makes N writes inside one begin/end → one undo removes all N (verified via N GETs)
- New mutation invalidates redo stack: undo → new POST → redo → 400/409 error envelope (redo stack empty)
- Undo on empty stack → 400 error envelope
- Undo captures span INSERT, UPDATE, DELETE separately — exercise each via its respective REST mutation and assert correct inverse via GET
- WS broadcast: subscribe; POST mutation; verify WS emits `undo_stack_changed` (or equivalent per spec); POST undo; verify another event
- Undo group sequence IDs observable via GET `/api/projects/:name/undo-state` (if exposed) — xfail if target-ideal only
- Cross-session persistence (target-state): boot, mutate, stop, boot, undo → xfail

Each e2e test annotated `(covers Rn, row #N)`.

```python
# === E2E ===

class TestEndToEnd:
    """Comprehensive e2e — undo/redo through the live HTTP + WS surface."""
    # ... tests per checklist
```

### 6. Run + verify + commit

```bash
pytest tests/specs/test_engine_db_undo_redo.py -v
git add tests/specs/test_engine_db_undo_redo.py
git commit -m "test(M18-73): engine-db-undo-redo regression tests — <N> unit"
```

---

## Verification

- [ ] Test file exists
- [ ] Every `Rn` has ≥1 covering test
- [ ] Every Behavior Table row covered
- [ ] Target-state tests use `xfail(..., strict=False)`
- [ ] E2E section present with comprehensive HTTP + WS coverage
- [ ] Every spec requirement has ≥1 e2e test (not just unit)
- [ ] `pytest ... -v` passes
- [ ] Collect count matches spec

---

## Key Design Decisions

| Decision | Choice | Rationale |
|---|---|---|
| Assert via direct SELECTs, not DAOs | Yes | DAOs could hide trigger bugs. |
| Test trigger idempotence explicitly | Yes | undo → redo → undo is where bugs live. |

---

## Notes

- Trigger tests must exercise INSERT, UPDATE, DELETE separately — inverse SQL shapes differ.
- Nested begin/end behavior is spec-defined; do not assume flatten or reject without checking the spec.
