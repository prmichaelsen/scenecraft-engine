# Task 82: Engine Analysis Handlers Regression Tests

**Milestone**: [M18 — Engine Regression Test Suite](../../milestones/milestone-18-engine-regression-test-suite.md)
**Spec**: [`local.engine-analysis-handlers`](../../specs/local.engine-analysis-handlers.md)
**Design Reference**: [`local.engine-analysis-handlers`](../../specs/local.engine-analysis-handlers.md)
**Estimated Time**: 6-8 hours
**Dependencies**: task-70, task-74
**Status**: Not Started
**Repository**: `scenecraft-engine`

---

## Objective

Write unit + e2e tests for `local.engine-analysis-handlers.md`. Lock in: handler dispatch, cache-key derivation (mix_graph_hash + analyzer + params_hash), cache hit/miss semantics, WS completion event, and error propagation. Target-state tests use `@pytest.mark.xfail(reason="target-state; awaits M16 FastAPI refactor", strict=False)`.

---

## Context

Analysis handlers (analyze_master_bus, beat detection, spectral features, loudness) sit on top of the analysis-caches layer. This spec locks the handler dispatch + cache-key derivation + WS completion-event invariants. Builds on task-70 + task-74 fixtures.

---

## Steps

### 1. Read the spec fully

Read `agent/specs/local.engine-analysis-handlers.md`. Note every `Rn`, Behavior Table row, test name.

### 2. Create the test file

Create `tests/specs/test_engine_analysis_handlers.py`.

### 3. Translate requirements into pytest functions

Typical patterns:

- Handler dispatch — each analyzer name routes to the right function.
- Cache-key derivation — same inputs → same key; different params_hash → different key.
- Cache hit — run once; run again; assert second call doesn't re-invoke the analyzer.
- Cache miss — novel key → analyzer runs → row persisted.
- WS completion event — stub subscriber; assert event fires with the correct payload shape.
- Error propagation — analyzer raises → error event on WS + cache row NOT written.
- mix_graph_hash dependency — different mix graph → different cache key.

Target-ideal behaviors → `xfail`.

### 4. Cover every Behavior Table row

### 5. Add e2e section

```python
# === E2E ===

class TestEndToEnd:
    def test_analyze_master_bus_caches(self, engine_server):
        """covers Rn (e2e)"""
        # Upload a mix; call analyze_master_bus; assert cache row.
        # Call again; assert hit (instant, no recompute).
```

### 6. Run + verify + commit

```bash
pytest tests/specs/test_engine_analysis_handlers.py -v
git add tests/specs/test_engine_analysis_handlers.py
git commit -m "test(M18-82): engine-analysis-handlers regression tests — <N> unit + <M> e2e"
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
| Stub analyzers for unit tests | Yes | Deterministic; fast. |
| Real analyzer in e2e | Yes | End-to-end correctness, not just dispatch. |

---

## Notes

- mix_graph_hash computation is already tested elsewhere (M15) — trust it; focus on handler behavior.
- Keep audio fixtures short (<1s) for fast CI.
