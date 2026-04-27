# Task 77: Engine MCP Bridge Regression Tests

**Milestone**: [M18 — Engine Regression Test Suite](../../milestones/milestone-18-engine-regression-test-suite.md)
**Spec**: [`local.engine-mcp-bridge`](../../specs/local.engine-mcp-bridge.md)
**Design Reference**: [`local.engine-mcp-bridge`](../../specs/local.engine-mcp-bridge.md)
**Estimated Time**: 4-6 hours
**Dependencies**: task-70, task-76
**Status**: Not Started
**Repository**: `scenecraft-engine`

---

## Objective

Write unit + e2e tests for `local.engine-mcp-bridge.md`. Lock in the MCP bridge's tool-dispatch parity with the direct-call path, auth preservation through the bridge, and request/response envelope. Target-state tests use `@pytest.mark.xfail(reason="target-state; awaits M16 FastAPI refactor", strict=False)`.

---

## Context

The MCP bridge exposes engine capabilities over stdio/SSE for external agents. Parity with the direct-call path (same DAOs, same auth) is the contract. A refactor that accidentally bypasses auth via MCP is a privilege-escalation bug. Builds on task-70 + task-76 fixtures.

---

## Steps

### 1. Read the spec fully

Read `agent/specs/local.engine-mcp-bridge.md`. Note every `Rn`, Behavior Table row, test name.

### 2. Create the test file

Create `tests/specs/test_engine_mcp_bridge.py`.

### 3. Translate requirements into pytest functions

Typical patterns:

- Tool registry exposure — MCP `tools/list` returns every chat tool; names match verbatim.
- Tool dispatch — call one tool via MCP; assert the same DB state change as a direct DAO call.
- Auth passthrough — unauthenticated MCP call rejected; authenticated call succeeds; auth cookie/bearer honored.
- Error envelope — DAO raises → MCP returns an error with the legacy `{error, message}` shape.
- Stream events — if the tool emits WS events directly, the MCP path should surface them.

Target-ideal behaviors (e.g., request tracing IDs, per-tool timeouts, streaming progress) → `xfail`.

### 4. Cover every Behavior Table row

### 5. Add e2e section

```python
# === E2E ===

class TestEndToEnd:
    def test_mcp_tool_round_trip(self, engine_server):
        """covers Rn (e2e)"""
        # Stand up the MCP bridge against the engine; call one tool; assert parity with direct-call.

    def test_mcp_auth_enforced(self, engine_server):
        """covers Rn (e2e)"""
        # Unauthenticated MCP call → error envelope; authenticated → success.
```

### 6. Run + verify + commit

```bash
pytest tests/specs/test_engine_mcp_bridge.py -v
git add tests/specs/test_engine_mcp_bridge.py
git commit -m "test(M18-77): engine-mcp-bridge regression tests — <N> unit + <M> e2e"
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
| Parity test MCP-vs-direct | Yes | The contract is "same result" — test both paths and diff. |
| Auth enforcement tested at the bridge | Yes | Privilege-escalation risk. |

---

## Notes

- If the MCP bridge uses stdio, use the `mcp` test client or subprocess + pipes.
- Keep tool payloads minimal — the bridge is the focus, not the tool itself.
