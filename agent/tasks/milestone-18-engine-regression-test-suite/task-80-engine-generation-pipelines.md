# Task 80: Engine Generation Pipelines Regression Tests

**Milestone**: [M18 — Engine Regression Test Suite](../../milestones/milestone-18-engine-regression-test-suite.md)
**Spec**: [`local.engine-generation-pipelines`](../../specs/local.engine-generation-pipelines.md)
**Design Reference**: [`local.engine-generation-pipelines`](../../specs/local.engine-generation-pipelines.md)
**Estimated Time**: 14 hours
**Dependencies**: task-70, task-79
**Status**: Not Started
**Repository**: `scenecraft-engine`

---

## Objective

Write comprehensive unit AND e2e tests. E2E coverage MUST match unit coverage in breadth — every requirement's observable effect has an HTTP/WS-level test. Unit tests may mock; e2e MUST NOT. Lock in: provider dispatch, candidate-pattern output (candidate on the existing entity, not a new sibling), job tracking, WS broadcast, and the "generation survives WS disconnect" invariant. Target-state tests use `@pytest.mark.xfail(reason="target-state; awaits M16 FastAPI refactor", strict=False)`.

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

### 5. E2E coverage checklist (comprehensive)

Every documented generation endpoint + every documented WS event must have a live-server test with a stubbed provider. Walk the spec's Behavior Table — every row has an HTTP or WS boundary.

Endpoints / WS events:

- `POST /api/projects/:name/generate-keyframes` (Imagen) → candidate lands on keyframe via GET
- `POST .../generate-transitions` (Veo, Kling) → candidate lands on transition
- `POST .../generate-music` (Musicful) → audio_clip candidate (or track) lands
- `POST .../generate-foley` — candidate pattern on audio_clip
- Job tracking via `GET /api/jobs/:id` — state transitions `pending → running → succeeded`/`failed`
- WS: subscriber receives `generation_started`, `generation_progress`, `generation_completed` / `generation_failed` in order
- Survives-disconnect: open WS, submit job, close WS mid-run, reopen WS, assert final state via `GET /api/jobs/:id` + candidate reachable via GET (memory'd load-bearing invariant)
- Error path: provider raises → job marked failed; WS emits failure event; legacy envelope
- Candidate pattern: assert output is a candidate on the existing entity, NOT a new sibling track/keyframe (via entity GET)
- Multiple concurrent jobs: each tracked independently; WS events distinguishable
- Auth enforced on generation endpoints (401 without auth)
- Destructive (if any) gated per spec
- Target-state xfails: cancel API `POST /api/jobs/:id/cancel`, priority queue ordering, concurrent job limits → HTTP 429

Each e2e test annotated `(covers Rn, row #N)`.

```python
# === E2E ===

class TestEndToEnd:
    """Comprehensive e2e — every generation endpoint + WS event with stubbed provider."""
    # ... tests per checklist
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
- [ ] E2E section present with comprehensive endpoint + WS coverage
- [ ] Every spec requirement has ≥1 e2e test (not just unit)
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
