# Task 76: Engine Plugin Loading + Lifecycle Regression Tests

**Milestone**: [M18 — Engine Regression Test Suite](../../milestones/milestone-18-engine-regression-test-suite.md)
**Spec**: [`local.engine-plugin-loading-lifecycle`](../../specs/local.engine-plugin-loading-lifecycle.md)
**Design Reference**: [`local.engine-plugin-loading-lifecycle`](../../specs/local.engine-plugin-loading-lifecycle.md)
**Estimated Time**: 4-6 hours
**Dependencies**: task-70
**Status**: Not Started
**Repository**: `scenecraft-engine`

---

## Objective

Write unit + e2e tests for `local.engine-plugin-loading-lifecycle.md`. Lock in: `plugin.yaml` manifest loading, plugin registration, sidecar-table creation (`<plugin_id>__<table>` prefix), route registration ordering (built-ins before plugin catch-all), and plugin lifecycle hooks. Target-state tests use `@pytest.mark.xfail(reason="target-state; awaits M16 FastAPI refactor", strict=False)`.

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

### 5. Add e2e section

```python
# === E2E ===

class TestEndToEnd:
    """E2E — boot engine with a fake plugin on disk."""

    def test_plugin_routes_reachable(self, engine_server, tmp_path):
        """covers Rn (e2e)"""
        # Scaffold a minimal plugin on disk (plugin.yaml + handler.py) before server boot.
        # Hit /plugin/<id>/<route> via httpx; assert 200 and expected body.

    def test_plugin_sidecar_tables_exist(self, engine_server, tmp_path):
        """covers Rn (e2e)"""
        # After boot, PRAGMA table_info for each declared sidecar table.
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
- [ ] E2E section present
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
