# Task 66: `scripts/gen_chat_tools.py` — spec walk, schema derivation, deterministic emit, `--check`

**Milestone**: [M16 — FastAPI Migration + Tool Codegen](../../milestones/milestone-16-fastapi-migration-and-tool-codegen.md)
**Spec**: [`local.openapi-tool-codegen`](../../specs/local.openapi-tool-codegen.md) — R1–R16, R25–R29
**Estimated Time**: 4–6 hours
**Dependencies**: T65
**Status**: Not Started

---

## Objective

Build the codegen pipeline. Read `openapi.json`, filter operations marked `x-tool: true`, derive Anthropic-compatible tool schemas, validate rigorously, and emit a deterministic Python module. This task produces the machinery; T67 authors the route annotations; T68 wires it up in `chat.py`.

---

## TDD Plan

The codegen is pure: input spec → output file. All tests use synthetic minimal OpenAPI specs (not the live scenecraft app). Write the 23 tests below against an empty codegen module. They fail. Implement until they all pass. The generator is exercised end-to-end against the real scenecraft `openapi.json` only in T68 (the parity + snapshot tests).

---

## Steps

### 1. Script skeleton

`scripts/gen_chat_tools.py`:

```python
"""
Generate src/scenecraft/chat_tools_generated.py from openapi.json.

Usage:
    python scripts/gen_chat_tools.py [--spec PATH] [--out PATH] [--check] [--golden PATH]

Flags:
    --spec    Path to openapi.json. Default: call app.openapi() in-process.
    --out     Destination file. Default: src/scenecraft/chat_tools_generated.py.
    --check   Do not write; exit 1 if output would differ from --out.
    --golden  Also write tests/fixtures/generated_tools.golden.json.
"""

import argparse, json, sys
from pathlib import Path
from dataclasses import dataclass

class ToolSpecError(Exception): pass

def load_spec(path: Path | None) -> dict: ...
def walk_operations(spec: dict) -> list[dict]: ...
def derive_input_schema(op: dict, spec: dict) -> dict: ...
def build_tool(op: dict, spec: dict) -> dict: ...
def build_operation_meta(op: dict, spec: dict) -> dict: ...
def render_module(tools: list[dict], ops: dict, destructive: set[str]) -> str: ...
def render_golden(tools: list[dict], destructive: set[str]) -> str: ...
def main(argv: list[str]) -> int: ...

if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
```

### 2. `load_spec`

- If `--spec` given: `json.load(path)`.
- Else: import `scenecraft.api.app.app`, call `app.openapi()`.

### 3. `walk_operations`

Yields `{operation_id, method, path, x_tool, x_tool_name, x_tool_description, x_destructive, parameters, request_body, responses}` for every path + method. Skip where `x_tool` is not `True`.

### 4. `derive_input_schema`

Per spec R9–R15:

1. Start with `{"type": "object", "properties": {}, "required": []}`.
2. **Path parameters**: for each `parameter` with `in: path`:
   - `properties[name] = {"type": parameter.schema.type, "description": parameter.description or ""}`
   - Always required.
   - If a body field has the same name, log warning and skip that body field (R10).
3. **Query parameters**: for each `parameter` with `in: query`:
   - `properties[name] = {...same...}`
   - Required iff `parameter.required == True`.
4. **Request body** (`requestBody.content.application/json.schema`):
   - Resolve `$ref` against `components.schemas`; inline recursively (R9).
   - If top-level `allOf`: merge all members; merge `properties` and `required` (R11).
   - If top-level `anyOf` / `oneOf`: raise `ToolSpecError` (R11).
   - Otherwise merge the body schema's `properties` and `required` into the top-level schema.
5. **Preserve** `enum`, `default`, `minimum`, `maximum`, `minLength`, `maxLength`, `pattern`, `format`, `description` on every property (R13, R14, R15).
6. Return the composed schema.

### 5. `build_tool` / `build_operation_meta`

- Tool name: `x_tool_name or operationId` (R7).
- Validate name against `^[a-z][a-z0-9_]{0,63}$`; raise `ToolSpecError` on failure (R7, R18).
- Description: `x_tool_description` required; raise `ToolSpecError` if missing (R3, R26).
- Build `{name, description, input_schema}`.
- Build `OperationMeta(tool_name, method, path, path_params, query_params, body_fields, destructive)`.

### 6. `render_module`

Deterministic output (R6):
- Sort tools by `name` (lexicographic).
- Sort property keys: `type` → `description` → `properties` → `required` → remaining alphabetical.
- JSON-dump with `indent=4`, sorted keys at every level (apply after the custom ordering hook above).
- Header comment (R29):
  ```
  # GENERATED — do not edit; run `python scripts/gen_chat_tools.py`.
  # Source: openapi.json emitted by scenecraft.api.app
  # Generated at: {iso_utc}
  ```
- Normalize the timestamp: either use a fixed placeholder or drop the line when comparing in `--check` mode. **Recommendation**: include the timestamp for human reference, and strip the line when diffing in `--check`.

### 7. `render_golden`

Emit `tests/fixtures/generated_tools.golden.json`:
```json
{
  "tools": [ ...the TOOLS list... ],
  "destructive": [ ...sorted DESTRUCTIVE_TOOLS... ]
}
```

### 8. Name-collision check (R27)

After building the full tool list, assert `len({t.name for t in tools}) == len(tools)`. On collision, raise `ToolSpecError` naming both operation IDs.

### 9. `--check` mode

Generate output; compare against on-disk `--out`; exit 1 with a unified diff (strip the timestamp line from both sides before diffing) if they differ.

### 10. Tests to Pass

Create `tests/test_gen_chat_tools.py` with synthetic specs. Each test builds a minimal `spec` dict, runs the generator, inspects the output.

- `happy_path_emits_tool` — R1, R3, R7, R9
- `unannotated_route_skipped` — R1
- `missing_description_errors` — R3, R26
- `name_collision_errors` — R27
- `ref_resolved_inline` — R9
- `allof_flattened` — R11
- `polymorphic_body_errors` — R11
- `path_body_collision_path_wins` — R10
- `query_params_merged` — R9
- `empty_input_schema_ok` — R9 (route with only a required `name` path param)
- `codegen_deterministic` — run twice, assert byte-identical (after stripping timestamps)
- `check_mode_detects_drift` — point `--out` at a stale file, assert exit 1 and diff in stderr
- `check_mode_silent_when_fresh` — run against fresh file, assert exit 0, empty stderr
- `module_imports_cleanly` — write the output to a tmpdir, `importlib.import_module(...)`, inspect `TOOLS`/`OPERATIONS`/`DESTRUCTIVE_TOOLS`
- `anthropic_tool_shape_valid` — each tool dict has exactly `name`/`description`/`input_schema`; no extra top-level keys
- `invalid_tool_name_errors` — `operationId: "Add-Audio-Track"`; expect `ToolSpecError`
- `empty_tool_set_is_fine` — spec with no `x-tool` annotations; `TOOLS == []`, exit 0
- `enum_preserved` — body field with `enum: ["a", "b"]`; assert preserved
- `default_preserved` — body field with `default: 100`; assert preserved
- `description_preserved_or_empty` — two fields, one with description one without; assert first preserved, second is `""`
- `tool_name_override_wins` — `operationId: "foo_bar"`, `x-tool-name: "different"`; `TOOLS[0].name == "different"`; `OPERATIONS["different"]` exists, `OPERATIONS["foo_bar"]` does not
- `unresolvable_ref_errors` — body references `#/components/schemas/Missing`; expect `ToolSpecError` mentioning the ref
- `operation_meta_has_templated_path` — route `/api/projects/{name}/foo`; `OperationMeta.path == "/api/projects/{name}/foo"`, `path_params == ("name",)`

### 11. `pyproject.toml` script entry

```toml
[project.scripts]
scenecraft-gen-tools = "scripts.gen_chat_tools:main"
```

(Or configure `scripts/` as a package if needed.)

---

## Verification

- [ ] `scripts/gen_chat_tools.py` runs end-to-end against a synthetic spec
- [ ] All 23 tests in `tests/test_gen_chat_tools.py` pass
- [ ] Running the generator twice produces byte-identical output (after timestamp normalization)
- [ ] `--check` mode exits 0 on fresh output, 1 on drift
- [ ] `--golden` flag writes a valid JSON fixture
- [ ] Generated module imports cleanly in a sandbox
- [ ] All `ToolSpecError` paths have clear error messages naming the offending operation

---

## Tests Covered

`happy-path-emits-tool`, `unannotated-route-skipped`, `missing-description-errors`, `name-collision-errors`, `ref-resolved-inline`, `allof-flattened`, `polymorphic-body-errors`, `path-body-collision-path-wins`, `query-params-merged`, `empty-input-schema-ok`, `codegen-deterministic`, `check-mode-detects-drift`, `check-mode-silent-when-fresh`, `module-imports-cleanly`, `anthropic-tool-shape-valid`, `invalid-tool-name-errors`, `empty-tool-set-is-fine`, `enum-preserved`, `default-preserved`, `description-preserved-or-empty`, `tool-name-override-wins`, `unresolvable-ref-errors`, `operation-meta-has-templated-path`.

---

## Notes

- Keep the generator's `ToolSpecError` messages specific: name the operation ID, the field, the regex (for name-validation), etc. A cryptic error during codegen becomes a cryptic CI failure.
- Anthropic's tool-definition JSON Schema is published; include a local copy in `tests/fixtures/anthropic_tool_schema.json` and validate with `jsonschema.validate` in `anthropic_tool_shape_valid`.
- The `type → description → properties → required → alphabetical` property ordering is a stylistic choice that makes diffs readable. If adopted, document it in the generator source. Anthropic doesn't care about ordering; it's purely for human reviewers.
- No chat.py wiring in this task — T68 does that. This task ends with a script that emits a file; whether the file is wired to `chat.py` is T68's concern.
