# Task 78: Engine Cache Invalidation Regression Tests

**Milestone**: [M18 — Engine Regression Test Suite](../../milestones/milestone-18-engine-regression-test-suite.md)
**Spec**: [`local.engine-cache-invalidation`](../../specs/local.engine-cache-invalidation.md)
**Design Reference**: [`local.engine-cache-invalidation`](../../specs/local.engine-cache-invalidation.md)
**Estimated Time**: 4-6 hours
**Dependencies**: task-70
**Status**: Not Started
**Repository**: `scenecraft-engine`

---

## Objective

Write unit + e2e tests for `local.engine-cache-invalidation.md`. Lock in: when each cache (render-frame L1, fragment cache, mix-analysis) invalidates, how range-invalidation composes, and the `encoder_generation` bump semantics. Target-state tests use `@pytest.mark.xfail(reason="target-state; awaits M16 FastAPI refactor", strict=False)`.

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

### 5. Add e2e section

```python
# === E2E ===

class TestEndToEnd:
    def test_clip_mutation_evicts_render_cache(self, engine_server):
        """covers Rn (e2e)"""
        # GET /render-frame at t=10s; assert hit stats increment.
        # POST clip update covering t=10s.
        # GET /render-cache/stats; assert eviction reflected.
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
- [ ] E2E section present
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
