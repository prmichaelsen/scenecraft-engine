# Task 87: Engine REST API Dispatcher Regression Tests

**Milestone**: [M18 — Engine Regression Test Suite](../../milestones/milestone-18-engine-regression-test-suite.md)
**Spec**: [`local.engine-rest-api-dispatcher`](../../specs/local.engine-rest-api-dispatcher.md)
**Design Reference**: [`local.engine-rest-api-dispatcher`](../../specs/local.engine-rest-api-dispatcher.md)
**Estimated Time**: 8-10 hours
**Dependencies**: task-70, task-84, task-85
**Status**: Not Started
**Repository**: `scenecraft-engine`

---

## Objective

Write unit + e2e tests for `local.engine-rest-api-dispatcher.md`. Lock in every invariant the M16 replacement must preserve: 164 routes + path/method parity, auth ordering, plugin catch-all last, unknown-route 404, error-envelope shape, CORS on every response, structural-lock serialization. Target-state tests use `@pytest.mark.xfail(reason="target-state; awaits M16 FastAPI refactor", strict=False)`.

**This is M18's highest-stakes task — literally the contract M16 is replacing.**

---

## Context

The REST dispatcher is literally what M16 is replacing. This spec locks every invariant the replacement must preserve: 164 routes, path+method parity, auth ordering, plugin catch-all last, unknown-route 404, error-envelope shape. Builds on task-70 + task-84 + task-85 fixtures.

---

## Steps

### 1. Read the spec fully

Read `agent/specs/local.engine-rest-api-dispatcher.md`. Note every `Rn`, Behavior Table row, test name.

### 2. Create the test file

Create `tests/specs/test_engine_rest_api_dispatcher.py`.

### 3. Translate requirements into pytest functions

Typical patterns:

- Route parity — every route in the spec's enumerated list resolves (status != 404 for valid inputs).
- Method parity — correct method accepted; wrong method → 405 (per spec).
- Auth ordering — bearer then cookie then anonymous; assert each.
- Unknown route — `/api/doesnt-exist` → 404 with legacy envelope `{"error": "NOT_FOUND", ...}`.
- Plugin catch-all — built-in route beats plugin catch-all even when plugin registers the same path.
- CORS on every response — all responses have allow-origin header.
- Error envelope — every error path returns `{"error": CODE, "message": ...}`.
- Structural lock serialization — two concurrent structural mutations serialize (per-project lock).
- Timeline validator post-hook — after a structural mutation, validator runs; exception is non-fatal but WS-broadcast.

Target-ideal behaviors (e.g., 422 vs 400 for new endpoints, HEAD on /render-frame) → `xfail`.

### 4. Cover every Behavior Table row

### 5. Add e2e section (primary for this spec)

```python
# === E2E ===

class TestEndToEnd:
    """E2E is THE point for this spec — dispatcher behavior only exists end-to-end."""

    def test_get_route_parity_crawl(self, engine_server):
        """covers Rn (e2e)"""
        # Crawl every GET route from the spec's list; assert none return 404.

    def test_unknown_route_returns_envelope(self, engine_server):
        """covers Rn (e2e)"""

    def test_plugin_catchall_loses_to_builtin(self, engine_server):
        """covers Rn (e2e)"""

    def test_structural_lock_serializes(self, engine_server):
        """covers Rn (e2e)"""
        # Fire two concurrent structural mutations; assert serial execution.

    def test_cors_on_every_response(self, engine_server):
        """covers Rn (e2e)"""
        # Sample 20 routes; assert allow-origin header on each.
```

### 6. Run + verify + commit

```bash
pytest tests/specs/test_engine_rest_api_dispatcher.py -v
git add tests/specs/test_engine_rest_api_dispatcher.py
git commit -m "test(M18-87): engine-rest-api-dispatcher regression tests — <N> unit + <M> e2e"
```

---

## Verification

- [ ] Test file exists
- [ ] Every `Rn` has ≥1 covering test
- [ ] Every Behavior Table row covered
- [ ] Target-state tests use `xfail(..., strict=False)`
- [ ] E2E section present (primary for this spec)
- [ ] Route-parity crawl covers every route in the spec's list
- [ ] `pytest ... -v` passes

---

## Key Design Decisions

| Decision | Choice | Rationale |
|---|---|---|
| Crawl every route for parity | Yes | M16 must preserve all 164; crawl is cheap insurance. |
| Structural-lock tested concurrently | Yes | Race conditions hide in single-threaded tests. |
| Plugin-catchall ordering explicit | Yes | Load order is the contract. |

---

## Notes

- This is the final M18 task. When it lands, the full safety net is in place.
- If the route crawl discovers dead routes (listed in spec but unreachable), fix the spec or the engine — don't xfail a dead-route test with a generic reason.
- Concurrent-mutation tests should bound wall-time (e.g., 2s) to avoid CI stalls.
