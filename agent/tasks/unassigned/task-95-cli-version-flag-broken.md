# Task 95: `scenecraft --version` Crashes (legacy package_name)

**Milestone**: unassigned
**Spec**: [`local.engine-cli-admin-commands`](../../specs/local.engine-cli-admin-commands.md) — behavior-row 39
**Estimated Time**: 15 minutes
**Status**: Filed (M18-86 regression discovery)
**Repository**: `scenecraft-engine`

---

## Bug

`scenecraft --version` exits 1 with:

```
RuntimeError: 'davinci-beat-lab' is not installed. Try passing 'package_name' instead.
```

Source: `src/scenecraft/cli.py:25`

```python
@click.version_option(package_name="davinci-beat-lab")
```

The wheel was renamed to `scenecraft-engine` (per `pyproject.toml`), but the
Click `version_option` still references the old package_name. `importlib.metadata`
can no longer find `davinci-beat-lab` in the installed dist-info, so every
`scenecraft --version` invocation explodes.

## Spec contract violated

- Behavior table row 39 — `version-option-prints-version` (R3): "Prints the
  package version via `click.version_option(package_name="davinci-beat-lab")`"
- Acceptance criterion: every admin command exits 0 on success.

The spec acknowledges the legacy `package_name` but assumes `davinci-beat-lab`
is still discoverable. After the rename, it isn't.

## Repro

```bash
.venv/bin/scenecraft --version
# RuntimeError: 'davinci-beat-lab' is not installed.
```

## Fix

Either:
1. Update `package_name="scenecraft-engine"` (matches `[project].name`), OR
2. Drop `package_name` entirely and let Click derive it from the installed dist
   that owns `scenecraft.cli`.

Then update the spec's `version-package-name-is-legacy` test row + R3 wording
since the legacy quirk is gone.

## Test

`tests/specs/test_engine_cli_admin_commands.py::TestEntryPoints::test_version_option_prints_version`
is xfailed with `strict=False` referencing this task. Flip to passing once
fixed.
