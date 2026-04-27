# Task 87: Engine REST API Dispatcher Regression Tests

**Milestone**: [M18 — Engine Regression Test Suite](../../milestones/milestone-18-engine-regression-test-suite.md)
**Spec**: [`local.engine-rest-api-dispatcher`](../../specs/local.engine-rest-api-dispatcher.md)
**Design Reference**: [`local.engine-rest-api-dispatcher`](../../specs/local.engine-rest-api-dispatcher.md)
**Estimated Time**: 16 hours
**Dependencies**: task-70, task-84, task-85
**Status**: Not Started
**Repository**: `scenecraft-engine`

---

## Objective

Write comprehensive unit AND e2e tests. E2E coverage MUST match unit coverage in breadth — every requirement's observable effect has an HTTP/WS-level test. Unit tests may mock; e2e MUST NOT. Lock in every invariant the M16 replacement must preserve: 164 routes + path/method parity, auth ordering, plugin catch-all last, unknown-route 404, error-envelope shape, CORS on every response, structural-lock serialization. Target-state tests use `@pytest.mark.xfail(reason="target-state; awaits M16 FastAPI refactor", strict=False)`.

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

### 5. E2E coverage checklist (comprehensive — primary for this spec)

Dispatcher behavior only exists end-to-end. E2E MUST cover every of the 164 documented routes + every dispatcher invariant.

Scenarios:

- **Route parity crawl**: every GET route in the spec's enumerated list resolves with status != 404 for valid inputs; every POST/PATCH/DELETE route returns != 404 on OPTIONS preflight
- **Method parity**: for each route, wrong method → 405 per spec
- **Auth ordering**: for an authenticated route — bearer token, session cookie, anonymous each tested; correct precedence
- **Unauthenticated**: `GET /api/projects` without auth → 401 + legacy envelope
- **Unknown route**: `GET /api/doesnt-exist` → 404 + `{"error": "NOT_FOUND", ...}` envelope
- **Plugin catch-all ordering**: built-in `/api/...` wins over plugin path collision (register a plugin that tries to claim a core path; fire request; assert core response)
- **Plugin catch-all reachable**: `/plugin/<id>/...` routes reach plugin handler (not built-in)
- **CORS on every response**: sample 20+ routes (including error paths); every response has `Access-Control-Allow-Origin`
- **OPTIONS preflight**: CORS preflight returns correct headers
- **Error envelope shape**: every 4xx + 5xx returns `{"error": CODE, "message": ...}` — sample across routes
- **Structural lock**: fire two concurrent structural mutations (POST keyframe, POST audio_track) on same project → serialize (observable via timing OR via a racy-test fixture)
- **Structural lock doesn't block reads**: fire a long GET concurrent with a structural mutation; both succeed
- **Timeline validator post-hook**: after a structural mutation, validator runs; deliberate invalid state → validator failure emitted as WS event, not fatal
- **Content-Type negotiation**: POST with wrong content-type → 415 or parsed defensively per spec
- **Malformed JSON body**: → 400 + envelope
- **Request size limit**: body > limit → 413 per spec
- **Slow-client handling**: partial body read → timeout + envelope
- **WS upgrade on dispatcher**: if WS is mounted on same port, upgrade succeeds on the right path
- Target-state xfails: 422 vs 400 for validation errors (FastAPI style), HEAD support on all GET routes, OpenAPI `/docs` endpoint

Each e2e test annotated `(covers Rn, row #N)`.

```python
# === E2E ===

class TestEndToEnd:
    """Comprehensive e2e — full 164-route parity, auth, CORS, error envelopes, structural lock."""
    # ... tests per checklist
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
- [ ] E2E section present with comprehensive 164-route + auth + CORS + envelope + lock coverage
- [ ] Every spec requirement has ≥1 e2e test (not just unit)
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
