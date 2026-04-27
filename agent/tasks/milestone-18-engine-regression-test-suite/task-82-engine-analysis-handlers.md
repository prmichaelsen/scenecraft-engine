# Task 82: Engine Analysis Handlers Regression Tests

**Milestone**: [M18 — Engine Regression Test Suite](../../milestones/milestone-18-engine-regression-test-suite.md)
**Spec**: [`local.engine-analysis-handlers`](../../specs/local.engine-analysis-handlers.md)
**Design Reference**: [`local.engine-analysis-handlers`](../../specs/local.engine-analysis-handlers.md)
**Estimated Time**: 12 hours
**Dependencies**: task-70, task-74
**Status**: Not Started
**Repository**: `scenecraft-engine`

---

## Objective

Write comprehensive unit AND e2e tests. E2E coverage MUST match unit coverage in breadth — every requirement's observable effect has an HTTP/WS-level test. Unit tests may mock; e2e MUST NOT. Lock in: handler dispatch, cache-key derivation (mix_graph_hash + analyzer + params_hash), cache hit/miss semantics, WS completion event, and error propagation. Target-state tests use `@pytest.mark.xfail(reason="target-state; awaits M16 FastAPI refactor", strict=False)`.

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

### 5. E2E coverage checklist (comprehensive)

Every analysis handler is a POST endpoint with a WS completion event. E2E MUST exercise each, with real (short) audio fixtures.

Endpoints / WS events:

- `POST /api/projects/:name/analyze-master-bus` — writes rows; second call cache-hits; WS emits `analysis_completed`
- `POST .../analyze-beats` (or similar beat detection) — same hit/miss + WS pattern
- `POST .../analyze-spectral` — spectral features
- `POST .../analyze-loudness` — pyloudnorm path
- Different params_hash → different cache key → re-runs analyzer (observable via timing + new row)
- Different mix_graph_hash → different cache key (mutate track effect, re-request, assert miss)
- Handler dispatch: invalid analyzer name → 400 envelope
- Error propagation: analyzer raises (deliberately short/invalid audio fixture) → WS error event + no cache row
- Auth enforced (401)
- WS event shape: completion event payload shape matches spec
- Cache rows reachable via admin GET (if exposed) or via repeat-POST timing test
- Concurrent POSTs for same analyzer+params: only one runs, other waits or both hit cache per spec

Each e2e test annotated `(covers Rn, row #N)`.

```python
# === E2E ===

class TestEndToEnd:
    """Comprehensive e2e — every analysis handler + cache + WS completion event."""
    # ... tests per checklist
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
- [ ] E2E section present with comprehensive handler + cache + WS coverage
- [ ] Every spec requirement has ≥1 e2e test (not just unit)
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
