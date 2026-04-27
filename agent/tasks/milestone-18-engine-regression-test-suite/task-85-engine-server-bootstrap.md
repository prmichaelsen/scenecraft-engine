# Task 85: Engine Server Bootstrap Regression Tests

**Milestone**: [M18 — Engine Regression Test Suite](../../milestones/milestone-18-engine-regression-test-suite.md)
**Spec**: [`local.engine-server-bootstrap`](../../specs/local.engine-server-bootstrap.md)
**Design Reference**: [`local.engine-server-bootstrap`](../../specs/local.engine-server-bootstrap.md)
**Estimated Time**: 12 hours
**Dependencies**: task-70
**Status**: Not Started
**Repository**: `scenecraft-engine`

---

## Objective

Write comprehensive unit AND e2e tests. E2E coverage MUST match unit coverage in breadth — every requirement's observable effect has an HTTP/WS-level test. Unit tests may mock; e2e MUST NOT. Lock in the startup sequence: CLI entry → config load → DB open → plugin load → route registration → signal handling → graceful shutdown. Target-state tests use `@pytest.mark.xfail(reason="target-state; awaits M16 FastAPI refactor", strict=False)`.

---

## Context

Server bootstrap is how the engine comes up — CLI entry, config load, DB open, plugin load, route registration, signal handling. A refactor that breaks bootstrap breaks every test downstream. This spec locks the startup sequence. Builds on task-70 fixtures.

---

## Steps

### 1. Read the spec fully

Read `agent/specs/local.engine-server-bootstrap.md`. Note every `Rn`, Behavior Table row, test name.

### 2. Create the test file

Create `tests/specs/test_engine_server_bootstrap.py`.

### 3. Translate requirements into pytest functions

Typical patterns:

- Bootstrap order — mock each phase; assert call order (config → DB → plugins → routes).
- Missing project dir — error on startup with descriptive message.
- Malformed config — error, don't crash.
- Plugin load failure — one plugin's load error doesn't block others.
- Signal handling — SIGTERM triggers graceful shutdown (close DB, finalize writes).
- Port binding — bind failure raises a legible error.
- Reentrant startup — starting twice in the same process is rejected or idempotent per spec.

Target-ideal behaviors (e.g., structured startup events, readiness probe) → `xfail`.

### 4. Cover every Behavior Table row

### 5. E2E coverage checklist (comprehensive)

Bootstrap IS the live engine coming up. E2E MUST exercise every boot phase through subprocess + HTTP / signal.

Scenarios (each via subprocess for realism):

- Happy path: `scenecraft start` in subprocess → `GET /api/config` → 200 → `SIGTERM` → clean exit within 5s
- `GET /api/health` (readiness) → 200 after boot
- Missing project dir: boot with nonexistent --work-dir → legible stderr error, exit code ≠ 0
- Malformed config.yaml: boot fails with legible error, doesn't crash
- Plugin load failure: one plugin has bad manifest; boot continues; `GET /api/plugins` shows good plugins loaded, bad one flagged
- Port binding: bind a port, start second engine on same port → exit code ≠ 0 with clear error
- `GET /api/version` returns build info per spec
- Signal handling: SIGTERM → WS connections close gracefully; in-flight requests complete; exit code 0
- Signal handling: SIGINT → same
- Reentrant startup: invoke `run_server` twice in same process → rejected or idempotent per spec
- Plugin sidecar tables created on boot — observable via `GET /api/plugins/<id>/tables` or admin endpoint
- CORS middleware active on first request post-boot
- Auth middleware active on first request post-boot
- WS server on :8891 reachable immediately after boot
- Structural lock initialized per project — concurrent structural mutations serialize
- Target-state xfails: structured startup events via WS, `/api/ready` distinguishing readiness from liveness

Each e2e test annotated `(covers Rn, row #N)`.

```python
# === E2E ===

class TestEndToEnd:
    """Comprehensive e2e — subprocess bootstrap + HTTP + signals."""
    # ... tests per checklist
```

### 6. Run + verify + commit

```bash
pytest tests/specs/test_engine_server_bootstrap.py -v
git add tests/specs/test_engine_server_bootstrap.py
git commit -m "test(M18-85): engine-server-bootstrap regression tests — <N> unit + <M> e2e"
```

---

## Verification

- [ ] Test file exists
- [ ] Every `Rn` has ≥1 covering test
- [ ] Every Behavior Table row covered
- [ ] Target-state tests use `xfail(..., strict=False)`
- [ ] E2E section present with comprehensive subprocess-boot + HTTP + signal coverage
- [ ] Every spec requirement has ≥1 e2e test (not just unit)
- [ ] `pytest ... -v` passes

---

## Key Design Decisions

| Decision | Choice | Rationale |
|---|---|---|
| Real bootstrap in e2e | Yes | TestClient bypasses startup; we need the real path. |
| Subprocess for signal tests | Yes | In-process signal testing is fragile. |

---

## Notes

- Use a non-default port in subprocess tests to avoid conflicts.
- SIGTERM timeout should be bounded (<5s); CI stalls are worse than missed coverage.
