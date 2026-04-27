# Task 74: Engine DB Analysis Caches Regression Tests

**Milestone**: [M18 — Engine Regression Test Suite](../../milestones/milestone-18-engine-regression-test-suite.md)
**Spec**: [`local.engine-db-analysis-caches`](../../specs/local.engine-db-analysis-caches.md)
**Design Reference**: [`local.engine-db-analysis-caches`](../../specs/local.engine-db-analysis-caches.md)
**Estimated Time**: 9 hours
**Dependencies**: task-70, task-71
**Status**: Not Started
**Repository**: `scenecraft-engine`

---

## Objective

Write comprehensive unit AND e2e tests. E2E coverage MUST match unit coverage in breadth — every requirement's observable effect has an HTTP/WS-level test. Lock in the schema + DAO contract for `mix_analysis_runs`, `mix_datapoints`, `mix_sections`, `mix_scalars` — the content-addressable cache behind analyze_master_bus. Target-state tests use `@pytest.mark.xfail(reason="target-state; awaits M16 FastAPI refactor", strict=False)`.

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

### 5. E2E coverage checklist (comprehensive)

Each cache table is hit by a specific endpoint. E2E MUST verify cache-hit via either (a) second-call timing ratio or (b) direct inspection of the stored DB row count through a subsequent GET / admin endpoint — NOT just DAO-level assertions.

Endpoints to exercise:

- `POST /api/projects/:name/bounce` — write creates audio_bounces row; second identical POST hits cache (timing ratio OR row count unchanged)
- `POST /api/projects/:name/analyze-master-bus` — writes mix_analysis_runs + mix_datapoints + mix_sections + mix_scalars; second identical POST hits cache
- `GET /api/projects/:name/audio-clips/:id/peaks` — peaks cache hit/miss observable via timing + row count
- `POST .../generate-dsp` — DSP cache creates row; re-POST cache-hits
- `POST .../generate-descriptions` — description cache creates row; re-POST cache-hits
- Cache-miss semantics: novel mix_graph_hash → new row; different analyzer → different row; different params_hash → different row
- Composite-key uniqueness: fire concurrent identical POSTs → exactly one cache row
- Datapoint ordering: GET returns time-sorted data
- Orphan behavior on run deletion: DELETE a run via admin endpoint (if exposed); datapoints/sections/scalars cascade or preserve per spec
- WS: `analyze_master_bus` emits completion WS event on first call, cache-hit event on subsequent

Each e2e test annotated `(covers Rn, row #N)`.

```python
# === E2E ===

class TestEndToEnd:
    """Comprehensive e2e — cache-hit observable via row count + WS events."""
    # ... tests per checklist
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
- [ ] E2E section present with comprehensive HTTP + WS coverage
- [ ] Every spec requirement has ≥1 e2e test (not just unit)
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
