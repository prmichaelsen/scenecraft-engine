# Task 72: Engine DB Effects + Curves Regression Tests

**Milestone**: [M18 — Engine Regression Test Suite](../../milestones/milestone-18-engine-regression-test-suite.md)
**Spec**: [`local.engine-db-effects-and-curves`](../../specs/local.engine-db-effects-and-curves.md)
**Design Reference**: [`local.engine-db-effects-and-curves`](../../specs/local.engine-db-effects-and-curves.md)
**Estimated Time**: 6 hours
**Dependencies**: task-70, task-71
**Status**: Not Started
**Repository**: `scenecraft-engine`

---

## Objective

Write comprehensive unit AND e2e tests. E2E coverage MUST match unit coverage in breadth — every requirement's observable effect has an HTTP/WS-level test. Lock in the schema + DAO contract for `track_effects`, `effect_params`, `effect_curves`, `volume_curves`, and `macro_params` — the automation-state layer M13 sits on top of. Target-state tests use `@pytest.mark.xfail(reason="target-state; awaits M16 FastAPI refactor", strict=False)`.

---

## Context

Effects + curves tables are how automation state is persisted. The M13 registry sits on top of them. A refactor that mishandles curve-row ordering or orphan-cleanup breaks playback silently. Builds on task-70/71 fixtures.

---

## Steps

### 1. Read the spec fully

Read `agent/specs/local.engine-db-effects-and-curves.md` end-to-end. Note every `Rn`, every Behavior Table row, every test name.

### 2. Create the test file

Create `tests/specs/test_engine_db_effects_and_curves.py`.

```python
"""
Regression tests for local.engine-db-effects-and-curves.md.
"""
import pytest

from scenecraft import db as scdb
```

### 3. Translate requirements into pytest functions

Typical patterns for this spec:

- Schema tests (as in task-71) for each of the 5 tables.
- CRUD round-trip tests for `add_track_effect`, `update_effect_param`, `add_curve_point`, etc. — assert persistence + readback shapes.
- Curve-row ordering — insert points out of order; assert reads return time-sorted.
- Orphan cleanup — delete a track; assert all its effects + curves cascade.
- FK integrity — attempt to insert a curve point referencing a non-existent effect_param; assert IntegrityError.
- Bezier control-point round-trip — store `(t, v, cp_in_t, cp_in_v, cp_out_t, cp_out_v)`; read back byte-identical.
- Macro param linkage — macro → curve binding persists and unbind cleans up.

Target-ideal behaviors (e.g., strict ordering indices that aren't enforced yet, composite unique constraints) get `xfail`.

### 4. Cover every Behavior Table row

Walk the Behavior Table. No silent omissions.

### 5. E2E coverage checklist (comprehensive)

The effects/curves DAL is thinly exposed via REST under track-effects + effect-curves routes. E2E MUST hit the live HTTP surface for every requirement with an observable effect. Walk the spec's Behavior Table — every row that mutates or reads state becomes an e2e test.

Endpoints to exercise:

- `POST /api/projects/:name/tracks/:id/effects` — add track effect; assert `GET` round-trip and z_order / ordering contract
- `PATCH /api/projects/:name/effects/:id` — update effect params; assert persistence
- `DELETE /api/projects/:name/effects/:id` — assert cascade cleanup of effect_params + effect_curves via subsequent GET (SELECT COUNT through the API, not direct DAO)
- `POST /api/projects/:name/effect-curves` (or nested POST under effect-params) — add curve point; assert read ordering by time
- `PATCH .../effect-curves/:id` — bezier control-point round-trip (byte-identical scalar fields)
- `DELETE .../tracks/:id` — cascade to effects + curves observable via subsequent GET
- `POST .../macro-params` + bind to curve — macro → curve linkage persists; unbind cleans up (via HTTP)
- FK integrity — POST a curve point referencing a missing effect_param → 400 error envelope
- Volume-curve CRUD via track PATCH — roundtrip + ordering
- Target-state xfails: composite unique constraint violations, strict ordering indices → HTTP xfail

Each e2e test annotated `(covers Rn, row #N)`.

```python
# === E2E ===

class TestEndToEnd:
    """Comprehensive e2e — every requirement's HTTP-observable effect has a test."""
    # ... tests per checklist above
```

### 6. Run + verify + commit

```bash
pytest tests/specs/test_engine_db_effects_and_curves.py -v
pytest --collect-only tests/specs/test_engine_db_effects_and_curves.py | wc -l
git add tests/specs/test_engine_db_effects_and_curves.py
git commit -m "test(M18-72): engine-db-effects-and-curves regression tests — <N> unit"
```

---

## Verification

- [ ] Test file exists
- [ ] Every `Rn` has ≥1 covering test
- [ ] Every Behavior Table row covered
- [ ] Target-state tests use `xfail(..., strict=False)`
- [ ] E2E section present with comprehensive HTTP coverage
- [ ] Every spec requirement has ≥1 e2e test (not just unit)
- [ ] `pytest ... -v` passes
- [ ] Collect count matches spec

---

## Key Design Decisions

| Decision | Choice | Rationale |
|---|---|---|
| Round-trip + introspection both | Yes | Schema correctness + DAO correctness are both load-bearing. |
| xfail target-ideal uniqueness constraints | Yes | Visible flip signal when M16 tightens the schema. |

---

## Notes

- Curve point ordering is subtle — test both insertion order and read order.
- Orphan-cleanup tests should assert via `SELECT COUNT(*)` not via DAO calls (DAO could have bugs hiding the orphan).
