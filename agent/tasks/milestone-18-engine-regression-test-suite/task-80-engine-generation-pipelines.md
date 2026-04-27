# Task 80: Engine Generation Pipelines Regression Tests

**Milestone**: [M18 — Engine Regression Test Suite](../../milestones/milestone-18-engine-regression-test-suite.md)
**Spec**: [`local.engine-generation-pipelines`](../../specs/local.engine-generation-pipelines.md)
**Design Reference**: [`local.engine-generation-pipelines`](../../specs/local.engine-generation-pipelines.md)
**Estimated Time**: 6-10 hours
**Dependencies**: task-70, task-79
**Status**: Not Started
**Repository**: `scenecraft-engine`

---

## Objective

Write unit + e2e tests for `local.engine-generation-pipelines.md`. Lock in: provider dispatch, candidate-pattern output (candidate on the existing entity, not a new sibling), job tracking, WS broadcast, and the "generation survives WS disconnect" invariant. Target-state tests use `@pytest.mark.xfail(reason="target-state; awaits M16 FastAPI refactor", strict=False)`.

---

## Context

Generation pipelines (Imagen, Veo, Kling, Musicful, plus the candidate pattern) coordinate provider calls, job tracking, and the 'survives WS disconnect' invariant. This spec locks the pipeline → candidate → WS-broadcast sequence. Builds on task-70 + task-79 fixtures.

---

## Steps

### 1. Read the spec fully

Read `agent/specs/local.engine-generation-pipelines.md`. Note every `Rn`, Behavior Table row, test name.

### 2. Create the test file

Create `tests/specs/test_engine_generation_pipelines.py`.

### 3. Translate requirements into pytest functions

Typical patterns:

- Candidate pattern — run generation with a stubbed provider; assert output is a candidate on the existing entity, not a new sibling track.
- Job tracking — submit → poll → success/fail; state transitions match spec.
- WS broadcast — stub a WS subscriber; assert generation events fire in order.
- Survives disconnect — drop WS mid-generation; assert the job completes and the candidate lands; reconnect + replay gets the final state.
- Multiple generators — Imagen, Veo, Kling, Musicful — each dispatches to the right provider.
- Error handling — provider raises → job marked failed → WS emits failure event with legacy envelope.

Target-ideal behaviors (e.g., cancel API, priority queue, concurrent job limits) → `xfail`.

### 4. Cover every Behavior Table row

### 5. Add e2e section

```python
# === E2E ===

class TestEndToEnd:
    def test_generation_candidate_lands_on_entity(self, engine_server):
        """covers Rn (e2e)"""
        # POST a generation job with a stubbed provider; assert candidate appears on the correct entity.

    def test_generation_survives_ws_disconnect(self, engine_server):
        """covers Rn (e2e)"""
        # Connect WS; submit job; disconnect; reconnect; assert final state reachable.
```

### 6. Run + verify + commit

```bash
pytest tests/specs/test_engine_generation_pipelines.py -v
git add tests/specs/test_engine_generation_pipelines.py
git commit -m "test(M18-80): engine-generation-pipelines regression tests — <N> unit + <M> e2e"
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
| Stub providers always | Yes | Real provider calls are non-deterministic + cost money. |
| Test "survives disconnect" explicitly | Yes | Memory'd as a load-bearing invariant. |
| Candidate vs sibling — explicit assertion | Yes | Memory'd pattern; regression risk. |

---

## Notes

- Stubbed providers should respond synchronously with fixture payloads.
- WS subscriber can be an in-process fake; no real websocket needed unless the spec says otherwise.
