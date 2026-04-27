# Task 77: Engine MCP Bridge Regression Tests

**Milestone**: [M18 — Engine Regression Test Suite](../../milestones/milestone-18-engine-regression-test-suite.md)
**Spec**: [`local.engine-mcp-bridge`](../../specs/local.engine-mcp-bridge.md)
**Design Reference**: [`local.engine-mcp-bridge`](../../specs/local.engine-mcp-bridge.md)
**Estimated Time**: 9 hours
**Dependencies**: task-70, task-76
**Status**: Not Started
**Repository**: `scenecraft-engine`

---

## Objective

Write comprehensive unit AND e2e tests. E2E coverage MUST match unit coverage in breadth — every requirement's observable effect has an HTTP/WS-level test. Lock in the MCP bridge's tool-dispatch parity with the direct-call path, auth preservation through the bridge, and request/response envelope. Target-state tests use `@pytest.mark.xfail(reason="target-state; awaits M16 FastAPI refactor", strict=False)`.

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

### 5. E2E coverage checklist (comprehensive)

MCP bridge is itself a transport. E2E MUST exercise every documented MCP surface (stdio or SSE) against the live engine.

Scenarios:

- MCP `tools/list` → every registered chat tool present, names verbatim
- MCP `tools/call <name>` for each tool category (creation, read, mutation, destructive) → parity with direct `POST /api/chat` tool invocation (same DB state, same WS events)
- Unauthenticated MCP call → error envelope; authenticated (bearer / cookie) → success
- Auth preservation across bridge: bearer in MCP request propagates to engine auth check
- Error envelope: DAO raises → MCP returns `{error, message}` with legacy shape
- Stream events: tool emits multiple WS events → MCP surfaces each (ordered)
- Destructive flag honored: destructive tool via MCP without confirmation → rejected per spec
- Concurrent MCP + REST calls on same project: MCP request doesn't bypass structural lock
- Target-state xfails: request tracing IDs, per-tool timeouts, streaming progress events over MCP
- Malformed MCP payload → legible error, no crash
- Round-trip via MCP for undo/redo: mutate via MCP, undo via REST, verify MCP reads reflect the rollback

Each e2e test annotated `(covers Rn, row #N)`.

```python
# === E2E ===

class TestEndToEnd:
    """Comprehensive e2e — MCP parity, auth, error envelope, streaming, concurrency."""
    # ... tests per checklist
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
- [ ] E2E section present with comprehensive MCP + parity coverage
- [ ] Every spec requirement has ≥1 e2e test (not just unit)
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
