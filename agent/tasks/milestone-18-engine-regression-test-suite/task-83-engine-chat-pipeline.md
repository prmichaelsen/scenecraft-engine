# Task 83: Engine Chat Pipeline Regression Tests

**Milestone**: [M18 — Engine Regression Test Suite](../../milestones/milestone-18-engine-regression-test-suite.md)
**Spec**: [`local.engine-chat-pipeline`](../../specs/local.engine-chat-pipeline.md)
**Design Reference**: [`local.engine-chat-pipeline`](../../specs/local.engine-chat-pipeline.md)
**Estimated Time**: 14 hours
**Dependencies**: task-70, task-79
**Status**: Not Started
**Repository**: `scenecraft-engine`

---

## Objective

Write comprehensive unit AND e2e tests. E2E coverage MUST match unit coverage in breadth — every requirement's observable effect has an HTTP/WS-level test. Unit tests may mock; e2e MUST NOT. Lock in: tool dispatch, streaming event order, undo-group atomicity, error propagation, and WS event shapes. Target-state tests use `@pytest.mark.xfail(reason="target-state; awaits M16 FastAPI refactor", strict=False)`.

---

## Context

The chat pipeline is the biggest integration surface — tools, streaming, undo groups, WS event ordering, tool execution path. This spec locks the tool-dispatch contract so the M16 refactor (which touches HTTP and potentially tool execution) can't accidentally break streaming order or undo atomicity. Builds on task-70 + task-79 fixtures.

---

## Steps

### 1. Read the spec fully

Read `agent/specs/local.engine-chat-pipeline.md`. Note every `Rn`, Behavior Table row, test name.

### 2. Create the test file

Create `tests/specs/test_engine_chat_pipeline.py`.

### 3. Translate requirements into pytest functions

Typical patterns:

- Tool dispatch — each tool name routes to the right handler.
- Streaming event order — subscribe; submit chat; assert `thinking → tool_use → tool_result → message` order.
- Undo atomicity — tool call wraps in `undo_begin/end`; multi-DAO call either all-commit or all-rollback.
- Error mid-tool — tool raises → rollback → error event → chat continues.
- Destructive flag — `_is_destructive` gating honored.
- Multi-tool turn — sequential tools in one chat turn; order preserved.
- Message persistence — chat message rows persist with correct role + content.

Target-ideal behaviors (e.g., parallel tool calls, per-tool timeouts, streaming cancellation) → `xfail`.

### 4. Cover every Behavior Table row

### 5. E2E coverage checklist (comprehensive)

Chat pipeline is the biggest integration surface. E2E MUST exercise every documented endpoint + every streaming event + every tool category through the live HTTP/WS surface with a stubbed LLM.

Endpoints / WS events:

- `POST /api/chat` streaming response — streaming event order `thinking → tool_use → tool_result → message`
- Each tool category (at least one representative):
  - create (add_keyframe, add_transition, add_audio_track, add_audio_clip) — DB state observable via subsequent GET
  - update (update_keyframe, update_volume_curve, update_effect_param_curve)
  - delete (delete_keyframe, delete_audio_clip) — soft-delete observable
  - batch (batch_delete if present)
  - analysis tools (analyze_master_bus via chat)
  - generation tools (add_audio_effect via chat)
- Multi-tool turn: two sequential tools in one turn; order + state preserved
- Undo atomicity: tool that makes N writes then raises mid-way → all N rolled back (verified via GET); error WS event emitted
- Destructive gating: destructive tool (`_is_destructive`) without confirmation flag → rejected; with flag → executes
- Message persistence: `GET /api/projects/:name/chat-messages` returns role + content + tool_calls
- Auth enforced (401 without cookie/bearer)
- Abort / cancellation via WS disconnect: per spec — job may continue (generation invariant) or halt (chat turn)
- Concurrent chat turns on same project: structural lock serializes tool-use writes
- Stubbed LLM provider emits scripted tool_use + result sequence; no real model call
- Target-state xfails: parallel tool calls, per-tool timeouts, streaming cancellation

Each e2e test annotated `(covers Rn, row #N)`.

```python
# === E2E ===

class TestEndToEnd:
    """Comprehensive e2e — every tool category + streaming order + undo + auth."""
    # ... tests per checklist
```

### 6. Run + verify + commit

```bash
pytest tests/specs/test_engine_chat_pipeline.py -v
git add tests/specs/test_engine_chat_pipeline.py
git commit -m "test(M18-83): engine-chat-pipeline regression tests — <N> unit + <M> e2e"
```

---

## Verification

- [ ] Test file exists
- [ ] Every `Rn` has ≥1 covering test
- [ ] Every Behavior Table row covered
- [ ] Target-state tests use `xfail(..., strict=False)`
- [ ] E2E section present with comprehensive tool + streaming + WS coverage
- [ ] Every spec requirement has ≥1 e2e test (not just unit)
- [ ] `pytest ... -v` passes

---

## Key Design Decisions

| Decision | Choice | Rationale |
|---|---|---|
| Stub LLM provider | Yes | Deterministic; no API calls. |
| Event-order assertion is strict | Yes | Frontend consumers depend on order. |
| Undo atomicity tested via direct SELECTs | Yes | DAOs could hide partial state. |

---

## Notes

- The stubbed LLM should emit a scripted tool_use + result sequence — no real model call.
- Multi-tool tests need care to avoid flakiness from event ordering — use deterministic sleeps only.
