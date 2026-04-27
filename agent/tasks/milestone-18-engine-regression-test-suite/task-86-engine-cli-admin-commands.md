# Task 86: Engine CLI + Admin Commands Regression Tests

**Milestone**: [M18 — Engine Regression Test Suite](../../milestones/milestone-18-engine-regression-test-suite.md)
**Spec**: [`local.engine-cli-admin-commands`](../../specs/local.engine-cli-admin-commands.md)
**Design Reference**: [`local.engine-cli-admin-commands`](../../specs/local.engine-cli-admin-commands.md)
**Estimated Time**: 12 hours
**Dependencies**: task-70
**Status**: Not Started
**Repository**: `scenecraft-engine`

---

## Objective

Write comprehensive unit AND e2e tests. E2E coverage MUST match unit coverage in breadth — every requirement's observable effect has an HTTP/WS-level test (or subprocess surface for CLI-specific rows). Unit tests may mock; e2e MUST NOT. Lock in: the `scenecraft` CLI surface — `--help`, `start/stop/status`, project management, chat, admin subcommands, exit codes, and stdout shapes. Target-state tests use `@pytest.mark.xfail(reason="target-state; awaits M16 FastAPI refactor", strict=False)`.

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

### 5. E2E coverage checklist (comprehensive)

CLI is a subprocess surface, but every admin command has an HTTP-observable effect (CLI → engine → DB). E2E MUST exercise each subcommand end-to-end.

Scenarios (subprocess-invoked for realism):

- `scenecraft --help` → exit 0, lists every documented subcommand
- `scenecraft <subcmd> --help` for each subcommand → exit 0, lists documented flags
- `scenecraft start` → server boots → `GET /api/health` succeeds → `scenecraft stop` → exit 0
- `scenecraft status` running → exit 0, stdout contains "running"
- `scenecraft status` not running → exit 0 (or 1 per spec), stdout contains "not running"
- `scenecraft project list` → stdout lists known projects (JSON or text per spec)
- `scenecraft project create <name>` → subsequent `GET /api/projects` shows it; DB initialized
- `scenecraft project delete <name>` → subsequent `GET /api/projects` excludes it
- `scenecraft chat "<prompt>"` → output streams; exit 0 on success
- `scenecraft admin migrate` (if exists) → schema_migrations rows updated
- Exit codes: success=0, user error=2, server error=1 (per spec)
- stdout vs stderr: logs on stderr, structured output on stdout — verify via capture
- `--json` flag (if spec) → machine-parseable output on stdout
- Invalid subcommand → exit 2, helpful message
- Missing required arg → exit 2, helpful message
- Target-state xfails: shell completion, interactive prompts, JSON output mode

Each e2e test annotated `(covers Rn, row #N)`.

```python
# === E2E ===

class TestEndToEnd:
    """Comprehensive e2e — every subcommand via subprocess + HTTP verification."""
    # ... tests per checklist
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
- [ ] E2E section present with comprehensive subcommand + HTTP coverage
- [ ] Every spec requirement has ≥1 e2e test (not just unit)
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
