# Task 83: Engine Chat Pipeline Regression Tests

**Milestone**: [M18 — Engine Regression Test Suite](../../milestones/milestone-18-engine-regression-test-suite.md)
**Spec**: [`local.engine-chat-pipeline`](../../specs/local.engine-chat-pipeline.md)
**Design Reference**: [`local.engine-chat-pipeline`](../../specs/local.engine-chat-pipeline.md)
**Estimated Time**: 6-10 hours
**Dependencies**: task-70, task-79
**Status**: Not Started
**Repository**: `scenecraft-engine`

---

## Objective

Write unit + e2e tests for `local.engine-chat-pipeline.md`. Lock in: tool dispatch, streaming event order, undo-group atomicity, error propagation, and WS event shapes. Target-state tests use `@pytest.mark.xfail(reason="target-state; awaits M16 FastAPI refactor", strict=False)`.

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

### 5. Add e2e section

```python
# === E2E ===

class TestEndToEnd:
    def test_chat_tool_call_round_trip(self, engine_server):
        """covers Rn (e2e)"""
        # Send a chat message invoking a tool (via stubbed LLM provider).
        # Assert tool executes, result persists, WS event stream well-formed.

    def test_chat_undo_atomic(self, engine_server):
        """covers Rn (e2e)"""
        # Tool that fails mid-way; assert DB state fully rolled back.
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
- [ ] E2E section present
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
