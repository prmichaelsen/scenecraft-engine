# Task 78: Engine Cache Invalidation Regression Tests

**Milestone**: [M18 — Engine Regression Test Suite](../../milestones/milestone-18-engine-regression-test-suite.md)
**Spec**: [`local.engine-cache-invalidation`](../../specs/local.engine-cache-invalidation.md)
**Design Reference**: [`local.engine-cache-invalidation`](../../specs/local.engine-cache-invalidation.md)
**Estimated Time**: 9 hours
**Dependencies**: task-70
**Status**: Not Started
**Repository**: `scenecraft-engine`

---

## Objective

Write comprehensive unit AND e2e tests. E2E coverage MUST match unit coverage in breadth — every requirement's observable effect has an HTTP/WS-level test. Lock in: when each cache (render-frame L1, fragment cache, mix-analysis) invalidates, how range-invalidation composes, and the `encoder_generation` bump semantics. Target-state tests use `@pytest.mark.xfail(reason="target-state; awaits M16 FastAPI refactor", strict=False)`.

---

## Context

Cache invalidation touches three caches (render-frame L1, fragment cache, mix analysis). This spec defines when each invalidates, how range-invalidation composes, and the encoder_generation bump semantics. Builds on task-70 fixtures.

---

## Steps

### 1. Read the spec fully

Read `agent/specs/local.engine-cache-invalidation.md`. Note every `Rn`, Behavior Table row, test name.

### 2. Create the test file

Create `tests/specs/test_engine_cache_invalidation.py`.

### 3. Translate requirements into pytest functions

Typical patterns:

- Full invalidation — mutate a project-level setting; assert all three caches evict.
- Range invalidation — mutate a clip covering `[t0, t1]`; assert only frames + fragments in that range evict; other ranges intact.
- Fragment cache composition — invalidating a range that partially overlaps fragment boundaries evicts whole fragments.
- Encoder generation bump — settings change → `encoder_generation` increments → worker rebuilds encoder.
- Mix-analysis cache invalidation — mutate track effect → analysis cache invalidates by mix_graph_hash.
- Return shape — `cache_invalidation` returns `(frames, fragments)` tuple.

Target-ideal behaviors (e.g., partial-fragment eviction, LRU on mix cache) → `xfail`.

### 4. Cover every Behavior Table row

### 5. E2E coverage checklist (comprehensive)

Cache invalidation is observable via `/render-cache/stats` + subsequent cache-miss latency. E2E MUST exercise every cache (render-frame L1, fragment cache, mix-analysis) and every trigger.

Scenarios:

- Clip mutation evicts render-frame + fragment caches over the clip's time range — `GET /render-cache/stats` shows eviction count
- Keyframe mutation evicts caches covering the adjacent transitions' time ranges
- Transition mutation evicts over `[from_kf, to_kf]` range
- Project-level setting mutation (e.g., resolution) → all caches evict + encoder_generation bumps
- Range composition: mutate two overlapping ranges; assert union evicted, no gaps
- Partial-overlap: mutation spans fragment boundary → whole fragments evicted
- Mix-graph mutation (add effect) → mix-analysis cache invalidated by mix_graph_hash; subsequent analyze_master_bus misses
- WS: cache-invalidation emits a `render_state_changed` delta event (if spec says so); subscriber verifies
- Return-shape invariant for `/render-cache/stats`: `(frames, fragments)` tuple-shaped JSON
- Target-state xfails: partial-fragment eviction, LRU eviction on mix cache
- After eviction, re-GET `/render-frame` at the evicted t → cache miss (observable via timing OR stats diff); result byte-identical to pre-mutation frame for an unchanged clip region

Each e2e test annotated `(covers Rn, row #N)`.

```python
# === E2E ===

class TestEndToEnd:
    """Comprehensive e2e — cache invalidation observable via stats, timing, and WS events."""
    # ... tests per checklist
```

### 6. Run + verify + commit

```bash
pytest tests/specs/test_engine_cache_invalidation.py -v
git add tests/specs/test_engine_cache_invalidation.py
git commit -m "test(M18-78): engine-cache-invalidation regression tests — <N> unit + <M> e2e"
```

---

## Verification

- [ ] Test file exists
- [ ] Every `Rn` has ≥1 covering test
- [ ] Every Behavior Table row covered
- [ ] Target-state tests use `xfail(..., strict=False)`
- [ ] E2E section present with comprehensive cache + WS coverage
- [ ] Every spec requirement has ≥1 e2e test (not just unit)
- [ ] `pytest ... -v` passes

---

## Key Design Decisions

| Decision | Choice | Rationale |
|---|---|---|
| Test tuple return shape explicitly | Yes | `(frames, fragments)` shape is load-bearing for callers. |
| Range-composition tests | Yes | Partial-overlap is where bugs hide. |

---

## Notes

- Cache stats endpoints exist today — use them for e2e verification.
- Keep mutations small (single clip edit) for deterministic range boundaries.
