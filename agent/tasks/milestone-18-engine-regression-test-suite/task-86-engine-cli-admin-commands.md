# Task 86: Engine CLI + Admin Commands Regression Tests

**Milestone**: [M18 — Engine Regression Test Suite](../../milestones/milestone-18-engine-regression-test-suite.md)
**Spec**: [`local.engine-cli-admin-commands`](../../specs/local.engine-cli-admin-commands.md)
**Design Reference**: [`local.engine-cli-admin-commands`](../../specs/local.engine-cli-admin-commands.md)
**Estimated Time**: 6-8 hours
**Dependencies**: task-70
**Status**: Not Started
**Repository**: `scenecraft-engine`

---

## Objective

Write unit + e2e tests for `local.engine-cli-admin-commands.md`. Lock in: the `scenecraft` CLI surface — `--help`, `start/stop/status`, project management, chat, admin subcommands, exit codes, and stdout shapes. Target-state tests use `@pytest.mark.xfail(reason="target-state; awaits M16 FastAPI refactor", strict=False)`.

---

## Context

The CLI exposes admin commands (start, stop, status, project management, chat). This spec locks the CLI surface so M16's uvicorn swap doesn't accidentally change the CLI contract users depend on. Builds on task-70 fixtures.

---

## Steps

### 1. Read the spec fully

Read `agent/specs/local.engine-cli-admin-commands.md`. Note every `Rn`, Behavior Table row, test name.

### 2. Create the test file

Create `tests/specs/test_engine_cli_admin_commands.py`.

### 3. Translate requirements into pytest functions

Typical patterns:

- `scenecraft --help` exit code 0, contains expected subcommands.
- Each subcommand has `--help` with expected flags.
- `scenecraft start` binds port, emits ready signal.
- `scenecraft stop` shuts down cleanly.
- `scenecraft status` shows running / not running.
- Project management — `scenecraft project list/create/delete` round-trips.
- Exit codes — success=0, user error=2, server error=1 (per spec).
- Stdout vs stderr split — logs on stderr, structured output on stdout.

Use `click.testing.CliRunner` if CLI uses Click (it does — see `pyproject.toml`).

Target-ideal behaviors (e.g., JSON output mode, shell completion) → `xfail`.

### 4. Cover every Behavior Table row

### 5. Add e2e section

```python
# === E2E ===

class TestEndToEnd:
    def test_cli_help_subprocess(self):
        """covers Rn (e2e via subprocess)"""
        import subprocess
        r = subprocess.run(["scenecraft", "--help"], capture_output=True, text=True)
        assert r.returncode == 0
        assert "Commands:" in r.stdout

    def test_cli_start_stop(self, tmp_path):
        """covers Rn (e2e)"""
        # Spawn scenecraft start as subprocess; poll status; scenecraft stop; assert exit code.
```

### 6. Run + verify + commit

```bash
pytest tests/specs/test_engine_cli_admin_commands.py -v
git add tests/specs/test_engine_cli_admin_commands.py
git commit -m "test(M18-86): engine-cli-admin-commands regression tests — <N> unit + <M> e2e"
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
| Click CliRunner for unit tests | Yes | Fast; deterministic. |
| Subprocess for e2e | Yes | Tests the real entrypoint binding. |
| Exit codes explicitly asserted | Yes | Shell integration depends on them. |

---

## Notes

- Subprocess tests should set `PYTHONPATH` correctly; use `python -m scenecraft.cli` as a fallback if the entrypoint isn't installed.
- Keep stdout shape assertions loose where reasonable (contains-match, not exact-match).
