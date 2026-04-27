# Task 85: Engine Server Bootstrap Regression Tests

**Milestone**: [M18 — Engine Regression Test Suite](../../milestones/milestone-18-engine-regression-test-suite.md)
**Spec**: [`local.engine-server-bootstrap`](../../specs/local.engine-server-bootstrap.md)
**Design Reference**: [`local.engine-server-bootstrap`](../../specs/local.engine-server-bootstrap.md)
**Estimated Time**: 6-8 hours
**Dependencies**: task-70
**Status**: Not Started
**Repository**: `scenecraft-engine`

---

## Objective

Write unit + e2e tests for `local.engine-server-bootstrap.md`. Lock in the startup sequence: CLI entry → config load → DB open → plugin load → route registration → signal handling → graceful shutdown. Target-state tests use `@pytest.mark.xfail(reason="target-state; awaits M16 FastAPI refactor", strict=False)`.

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

### 5. Add e2e section

```python
# === E2E ===

class TestEndToEnd:
    def test_engine_boots_and_serves_config(self, tmp_path):
        """covers Rn (e2e)"""
        # Boot the server via the real bootstrap path (subprocess or in-process runner).
        # GET /api/config; assert 200.
        # Send SIGTERM; assert clean shutdown within timeout.
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
- [ ] E2E section present
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
