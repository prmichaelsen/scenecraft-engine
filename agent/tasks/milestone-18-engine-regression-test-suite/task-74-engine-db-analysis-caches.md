# Task 74: Engine DB Analysis Caches Regression Tests

**Milestone**: [M18 — Engine Regression Test Suite](../../milestones/milestone-18-engine-regression-test-suite.md)
**Spec**: [`local.engine-db-analysis-caches`](../../specs/local.engine-db-analysis-caches.md)
**Design Reference**: [`local.engine-db-analysis-caches`](../../specs/local.engine-db-analysis-caches.md)
**Estimated Time**: 4-6 hours
**Dependencies**: task-70, task-71
**Status**: Not Started
**Repository**: `scenecraft-engine`

---

## Objective

Write unit tests for `local.engine-db-analysis-caches.md`. Lock in the schema + DAO contract for `mix_analysis_runs`, `mix_datapoints`, `mix_sections`, `mix_scalars` — the content-addressable cache behind analyze_master_bus. Target-state tests use `@pytest.mark.xfail(reason="target-state; awaits M16 FastAPI refactor", strict=False)`.

---

## Context

Analysis caches are the content-addressable layer behind analyze_master_bus and friends. Cache hits are the difference between milliseconds and minutes. Any refactor must preserve the mix_graph_hash + composite-key invariants. Builds on task-70/71 fixtures.

---

## Steps

### 1. Read the spec fully

Read `agent/specs/local.engine-db-analysis-caches.md` end-to-end. Note every `Rn`, Behavior Table row, test name.

### 2. Create the test file

Create `tests/specs/test_engine_db_analysis_caches.py`.

### 3. Translate requirements into pytest functions

Typical patterns:

- Schema tests for all 4 tables.
- Composite-key invariants — `(mix_graph_hash, analyzer, params_hash)` uniqueness; attempt dup insert, assert IntegrityError.
- Cache-hit semantics — insert a run; query by the composite key; assert the matching row is returned without re-running the analyzer.
- Cache-miss semantics — query with a novel key; assert empty result (DAO returns None / empty).
- Datapoint ordering — insert datapoints out of order; read back time-sorted.
- Section + scalar round-trip — structured JSON payloads persist without lossy encoding.
- Orphan behavior on run deletion — delete a run; assert datapoints/sections/scalars are either cascaded or preserved per spec.

Target-ideal behaviors (e.g., partial-result cleanup, TTL-based eviction, size bounds) → `xfail`.

### 4. Cover every Behavior Table row

### 5. No e2e section

```python
# NOTE: no e2e — local.engine-db-analysis-caches.md is a DB-layer spec; no HTTP/WS surface. Cross-layer e2e for analyze_master_bus is covered by task-82.
```

### 6. Run + verify + commit

```bash
pytest tests/specs/test_engine_db_analysis_caches.py -v
git add tests/specs/test_engine_db_analysis_caches.py
git commit -m "test(M18-74): engine-db-analysis-caches regression tests — <N> unit"
```

---

## Verification

- [ ] Test file exists
- [ ] Every `Rn` has ≥1 covering test
- [ ] Every Behavior Table row covered
- [ ] Target-state tests use `xfail(..., strict=False)`
- [ ] `# NOTE: no e2e` comment at bottom
- [ ] `pytest ... -v` passes
- [ ] Collect count matches spec

---

## Key Design Decisions

| Decision | Choice | Rationale |
|---|---|---|
| Exercise composite-key uniqueness explicitly | Yes | Cache correctness depends on it. |
| Test datapoint ordering | Yes | Downstream consumers assume sorted reads. |

---

## Notes

- Keep payloads small in tests (a handful of datapoints) — the cache contract is structural, not volumetric.
- Analyzer + params_hash stringification is brittle; test identity, not byte equality of serialized JSON.
