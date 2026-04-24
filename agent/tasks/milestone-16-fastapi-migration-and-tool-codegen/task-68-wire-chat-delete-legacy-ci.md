# Task 68: Wire chat.py + delete 32 legacy constants + snapshot + golden + parity + CI

**Milestone**: [M16 — FastAPI Migration + Tool Codegen](../../milestones/milestone-16-fastapi-migration-and-tool-codegen.md)
**Spec**: [`local.openapi-tool-codegen`](../../specs/local.openapi-tool-codegen.md) — R17, R18, R22–R24, R30
**Estimated Time**: 4–6 hours
**Dependencies**: T65, T66, T67
**Status**: Not Started

---

## Objective

Ship it. Run the codegen, write `src/scenecraft/chat_tools_generated.py`, wire it into `chat.py`, delete the 32 legacy tool-dict constants, update `_is_destructive` to consult `DESTRUCTIVE_TOOLS`, commit the snapshot + golden fixtures, and add a CI job that fails on codegen drift. End state: the chat tool surface is entirely generated; any new tool requires an `x-tool-description` on a FastAPI route, nothing else.

---

## TDD Plan

1. Run `python scripts/gen_chat_tools.py --out src/scenecraft/chat_tools_generated.py --golden tests/fixtures/generated_tools.golden.json` — produces real generated artifacts.
2. Run `python -c "from scenecraft.chat_tools_generated import TOOLS; print(len(TOOLS))"` — expect 32.
3. Write `tests/test_openapi_contract.py` + `tests/test_generated_tools_parity.py` — they pass at this point because fixtures match live output.
4. Edit `chat.py`: swap TOOLS, delete the 32 constants, rewire `_is_destructive`.
5. Run full `pytest tests/` — chat tests still pass.
6. Commit snapshot (`tests/fixtures/openapi.snapshot.json`).
7. Wire CI job.

---

## Steps

### 1. Run the codegen

```
python scripts/gen_chat_tools.py \
    --out src/scenecraft/chat_tools_generated.py \
    --golden tests/fixtures/generated_tools.golden.json
```

Inspect the generated module manually: 32 tools, sorted by name, correct destructive set, correct operations map. Commit.

### 2. Capture the OpenAPI snapshot

```
python - <<'EOF'
import json
from scenecraft.api.app import app
spec = app.openapi()
# Normalization: strip volatile fields (FastAPI version, generator-added annotations)
# Keep: paths, components, operationIds, x-* extensions, parameters, schemas
with open("tests/fixtures/openapi.snapshot.json", "w") as f:
    json.dump(spec, f, indent=2, sort_keys=True)
EOF
```

Commit.

### 3. Rewire `chat.py`

```python
# Before:
TOOLS: list[dict] = [
    SQL_QUERY_TOOL,
    UPDATE_KEYFRAME_PROMPT_TOOL,
    ...  # 30 more entries
]

# After:
from scenecraft.chat_tools_generated import TOOLS, OPERATIONS, DESTRUCTIVE_TOOLS
```

Delete the 32 `*_TOOL: dict = {...}` constants from `chat.py`. `git grep "SQL_QUERY_TOOL = \|UPDATE_KEYFRAME_PROMPT_TOOL = \|..."` — expect zero matches after.

Update `_is_destructive`:

```python
def _is_destructive(tool_name: str) -> bool:
    if tool_name in DESTRUCTIVE_TOOLS:
        return True
    # Safety-net fallback (kept per spec R20):
    name = tool_name.lower()
    return any(p in name for p in _DESTRUCTIVE_TOOL_PATTERNS)
```

Leave `_DESTRUCTIVE_TOOL_PATTERNS` in place as the safety net. Do NOT delete it.

### 4. Tests

Create `tests/test_openapi_contract.py`:

```python
import json
from pathlib import Path
from scenecraft.api.app import app

SNAPSHOT = Path(__file__).parent / "fixtures" / "openapi.snapshot.json"

def _normalize(spec: dict) -> dict:
    # Strip volatile fields: FastAPI version, description whitespace, etc.
    # Keep everything semantic.
    ...

def test_openapi_snapshot_matches():
    live = _normalize(app.openapi())
    snap = _normalize(json.loads(SNAPSHOT.read_text()))
    assert live == snap, (
        "OpenAPI spec drift. Regenerate with:\n"
        "  python scripts/capture_openapi_snapshot.py"
    )
```

Create `tests/test_generated_tools_parity.py`:

```python
import json
from pathlib import Path
from scenecraft.chat_tools_generated import TOOLS, DESTRUCTIVE_TOOLS

GOLDEN = Path(__file__).parent / "fixtures" / "generated_tools.golden.json"
LEGACY = Path(__file__).parent / "fixtures" / "legacy_tool_schemas.json"

def test_matches_golden():
    golden = json.loads(GOLDEN.read_text())
    assert {t["name"] for t in TOOLS} == {t["name"] for t in golden["tools"]}
    assert set(DESTRUCTIVE_TOOLS) == set(golden["destructive"])
    # Full per-tool comparison:
    by_name_live = {t["name"]: t for t in TOOLS}
    by_name_golden = {t["name"]: t for t in golden["tools"]}
    for name, live in by_name_live.items():
        assert live == by_name_golden[name], f"drift in tool {name}"

def test_legacy_schemas_preserved():
    """Every schema from pre-migration chat.py::TOOLS is semantically preserved."""
    legacy = json.loads(LEGACY.read_text())["tools"]
    by_name_legacy = {t["name"]: t for t in legacy}
    by_name_live = {t["name"]: t for t in TOOLS}
    for name, legacy_tool in by_name_legacy.items():
        assert name in by_name_live, f"missing tool {name}"
        live_schema = by_name_live[name]["input_schema"]
        legacy_schema = legacy_tool["input_schema"]
        # Required fields MUST match exactly
        assert set(live_schema.get("required", [])) == set(legacy_schema.get("required", [])), (
            f"{name}: required mismatch"
        )
        # Property names and types MUST match
        live_props = live_schema.get("properties", {})
        legacy_props = legacy_schema.get("properties", {})
        assert set(live_props.keys()) >= set(legacy_props.keys()), (
            f"{name}: missing property"
        )
        for prop_name, legacy_prop in legacy_props.items():
            live_prop = live_props[prop_name]
            assert live_prop.get("type") == legacy_prop.get("type"), (
                f"{name}.{prop_name}: type mismatch"
            )
            if "enum" in legacy_prop:
                assert live_prop.get("enum") == legacy_prop["enum"], (
                    f"{name}.{prop_name}: enum mismatch"
                )
            if "default" in legacy_prop:
                assert live_prop.get("default") == legacy_prop["default"], (
                    f"{name}.{prop_name}: default mismatch"
                )
        # Description presence (content can differ; presence is mandatory)
        for prop_name in live_props:
            assert "description" in live_props[prop_name], (
                f"{name}.{prop_name}: missing description"
            )
```

### 5. `chat_imports_generated` test

Create `tests/test_chat_wiring.py`:

```python
def test_chat_tools_come_from_generated():
    import scenecraft.chat
    import scenecraft.chat_tools_generated
    assert scenecraft.chat.TOOLS is scenecraft.chat_tools_generated.TOOLS

def test_legacy_constants_deleted():
    import scenecraft.chat
    for name in ("SQL_QUERY_TOOL", "UPDATE_KEYFRAME_PROMPT_TOOL", "DELETE_KEYFRAME_TOOL"):
        assert not hasattr(scenecraft.chat, name), (
            f"legacy constant {name} not deleted from chat.py"
        )
```

### 6. CI job — `tools-up-to-date`

Add a CI step (wherever CI lives; likely `.github/workflows/` or similar — audit the project's CI config):

```yaml
- name: Codegen up-to-date
  run: python scripts/gen_chat_tools.py --check
```

On drift: exit 1 with diff + regeneration command in the output.

### 7. Snapshot-drift test

Extend `tests/test_openapi_contract.py` with:

```python
def test_snapshot_test_flags_drift():
    """If a developer removes an operation without regenerating, the snapshot test fails."""
    # This is a meta-test; reproduced by copying the snapshot, mutating it (remove one op),
    # and asserting the assertion raises.
    import copy
    snap = json.loads(SNAPSHOT.read_text())
    mutated = copy.deepcopy(snap)
    # Remove an arbitrary operation
    first_path = next(iter(mutated["paths"]))
    mutated["paths"].pop(first_path)
    live = _normalize(app.openapi())
    assert _normalize(mutated) != live, "mutated snapshot should differ from live"
```

### 8. `incremental_add_minimal_diff` test

```python
def test_adding_a_new_tool_produces_bounded_diff():
    """When a new tool is added, the generated file diff contains exactly the new entries."""
    # Setup: capture current generated file.
    # Action: monkey-patch app to add one synthetic x-tool-true operation.
    # Assert: diff adds exactly one tool entry in TOOLS + one entry in OPERATIONS, nothing else.
    ...
```

This is nice-to-have; if it's tricky to set up, defer.

---

## Verification

- [ ] `src/scenecraft/chat_tools_generated.py` committed; contains 32 tools
- [ ] `tests/fixtures/openapi.snapshot.json` committed
- [ ] `tests/fixtures/generated_tools.golden.json` committed
- [ ] `tests/fixtures/legacy_tool_schemas.json` committed (from T67)
- [ ] `chat.py::TOOLS` imports from `chat_tools_generated`
- [ ] `git grep 'SQL_QUERY_TOOL = \|_TOOL = {' src/` returns zero results
- [ ] `_is_destructive` consults `DESTRUCTIVE_TOOLS` first
- [ ] All 6 test cases pass: `test_openapi_snapshot_matches`, `test_matches_golden`, `test_legacy_schemas_preserved`, `test_chat_tools_come_from_generated`, `test_legacy_constants_deleted`, `test_snapshot_test_flags_drift`
- [ ] CI `tools-up-to-date` job passes on the fresh tree; fails if `--check` fails
- [ ] End-to-end chat smoke test: run the chat agent against a live project, invoke at least 3 tools (`add_audio_track`, `sql_query`, `delete_keyframe`), verify behavior matches pre-migration baseline

---

## Tests Covered

`chat-imports-generated`, `legacy-constants-deleted`, `openapi-snapshot-matches`, `snapshot-test-flags-drift`, `ci-tools-up-to-date-job`, `legacy-schemas-preserved` (full parity), `32-legacy-tools-preserved` (full), (optionally) `incremental-add-minimal-diff`.

---

## Notes

- This task closes M16. After merge, the milestone's deliverables are all shipped.
- Smoke-test the chat agent end-to-end against a real project before declaring done. Unit tests don't catch "the LLM now sees different schemas and behaves differently."
- If the smoke test reveals the LLM confused by a generated description vs its legacy counterpart, the fix is a prose tweak to the `x-tool-description` on the route — NOT editing the generated module.
- OQ-1 (chat tool execution path) remains open. The recommendation (in-process + parity test) is compatible with everything here; if it's chosen, a separate follow-up task adds the parity test. Not blocking for M16.
- After CI hooks in, any future chat-tool work becomes: (1) add/modify a FastAPI route, (2) set `openapi_extra`, (3) run `gen_chat_tools.py`, (4) commit generated file alongside the route change. Three extra steps. Worth the trade.
