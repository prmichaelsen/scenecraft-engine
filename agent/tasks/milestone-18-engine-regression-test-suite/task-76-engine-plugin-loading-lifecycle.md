# Task 76: Engine Plugin Loading + Lifecycle Regression Tests

**Milestone**: [M18 — Engine Regression Test Suite](../../milestones/milestone-18-engine-regression-test-suite.md)
**Spec**: [`local.engine-plugin-loading-lifecycle`](../../specs/local.engine-plugin-loading-lifecycle.md)
**Design Reference**: [`local.engine-plugin-loading-lifecycle`](../../specs/local.engine-plugin-loading-lifecycle.md)
**Estimated Time**: 9 hours
**Dependencies**: task-70
**Status**: Not Started
**Repository**: `scenecraft-engine`

---

## Objective

Write comprehensive unit AND e2e tests. E2E coverage MUST match unit coverage in breadth — every requirement's observable effect has an HTTP/WS-level test. Lock in: `plugin.yaml` manifest loading, plugin registration, sidecar-table creation (`<plugin_id>__<table>` prefix), route registration ordering (built-ins before plugin catch-all), and plugin lifecycle hooks. Target-state tests use `@pytest.mark.xfail(reason="target-state; awaits M16 FastAPI refactor", strict=False)`.

---

## Context

The plugin loader reads `plugin.yaml` manifests, registers handlers + sidecar tables, and exposes plugin-owned HTTP routes. The M16 refactor has to keep the catch-all ordering correct (built-ins first, then plugin catch-all). This spec locks plugin lifecycle + sidecar-table invariants. Builds on task-70 fixtures.

---

## Steps

### 1. Read the spec fully

Read `agent/specs/local.engine-plugin-loading-lifecycle.md`. Note every `Rn`, Behavior Table row, test name.

### 2. Create the test file

Create `tests/specs/test_engine_plugin_loading_lifecycle.py`.

### 3. Translate requirements into pytest functions

Typical patterns:

- Manifest parsing — valid `plugin.yaml` loads; malformed yaml raises descriptive error; missing required field raises.
- Plugin ID uniqueness — two plugins with the same ID fail to both register.
- Sidecar table creation — on plugin load, tables named `<plugin_id>__<table>` exist; verify via PRAGMA.
- Table prefix enforcement — a plugin declaring a table named `projects` (no prefix) is rejected.
- Route registration order — load plugin A, then built-ins, then plugin B; assert resolution order (built-ins win over catch-all).
- Lifecycle hooks — `on_load`, `on_unload` fire in order, both get the engine handle.
- Plugin unload — sidecar tables remain (data preserved); routes unregister.

Target-ideal behaviors (e.g., hot-reload, sandboxed exec, version pinning) → `xfail`.

### 4. Cover every Behavior Table row

### 5. E2E coverage checklist (comprehensive)

Plugin loading has a large HTTP surface. E2E MUST exercise every plugin lifecycle + route registration + catch-all ordering behavior through the live server, per the spec's Behavior Table.

Scenarios/endpoints:

- Scaffold minimal plugin (plugin.yaml + handler.py); boot engine; `GET /plugin/<id>/<route>` → 200 + body
- Plugin with multiple routes: all reachable; method parity per route
- Plugin POST route: `POST /plugin/<id>/<route>` → sidecar table row created (verified via subsequent GET)
- Malformed plugin.yaml: boot emits error; other plugins load; `GET /plugin/<bad_id>/...` → 404
- Missing required field in manifest: boot rejects; `GET /api/plugins` omits it
- Duplicate plugin_id: only first registers; second's routes unreachable (404)
- Sidecar table prefix enforcement: plugin declaring unprefixed table → boot rejects; server fails to come up OR plugin marked disabled via `GET /api/plugins`
- Route ordering: built-in `/api/...` wins over plugin catch-all even when plugin declares a colliding path — hit the collision URL, assert built-in response
- Lifecycle hooks `on_load` / `on_unload`: observable via plugin-emitted log line or sidecar-table row
- Plugin unload via admin endpoint (if exposed): sidecar tables preserved; routes unregister → 404
- `GET /api/plugins` list reflects loaded state
- WS: plugin emits WS event; subscriber receives with correct topic
- Target-state xfails: hot-reload, sandboxed exec, version pinning at HTTP level

Each e2e test annotated `(covers Rn, row #N)`.

```python
# === E2E ===

class TestEndToEnd:
    """Comprehensive e2e — plugin routes, sidecar tables, catch-all ordering, lifecycle."""
    # ... tests per checklist
```

### 6. Run + verify + commit

```bash
pytest tests/specs/test_engine_plugin_loading_lifecycle.py -v
git add tests/specs/test_engine_plugin_loading_lifecycle.py
git commit -m "test(M18-76): engine-plugin-loading-lifecycle regression tests — <N> unit + <M> e2e"
```

---

## Verification

- [ ] Test file exists
- [ ] Every `Rn` has ≥1 covering test
- [ ] Every Behavior Table row covered
- [ ] Target-state tests use `xfail(..., strict=False)`
- [ ] E2E section present with comprehensive HTTP coverage
- [ ] Every spec requirement has ≥1 e2e test (not just unit)
- [ ] `pytest ... -v` passes
- [ ] Collect count matches spec

---

## Key Design Decisions

| Decision | Choice | Rationale |
|---|---|---|
| Scaffold fake plugin per-test | Yes | Isolation; no dep on shipped plugins. |
| Test route-ordering via resolution, not introspection | Yes | The contract is resolution order, not list order. |

---

## Notes

- The `<plugin_id>__<table>` prefix is load-bearing (R9a) — test it's enforced, not just documented.
- Plugin unload is spec-defined; do not assume symmetry with load.
