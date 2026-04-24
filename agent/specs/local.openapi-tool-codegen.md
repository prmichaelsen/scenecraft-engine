# Spec: OpenAPI-Driven Chat Tool Codegen

**Namespace**: local
**Version**: 1.0.0
**Created**: 2026-04-24
**Last Updated**: 2026-04-24
**Status**: Ready for Proofing

---

**Purpose**: Implementation-ready contract for a codegen pipeline that reads the FastAPI-emitted `openapi.json`, filters operations marked with the `x-tool: true` extension, and emits a Python module `chat_tools_generated.py` containing an Anthropic-compatible `TOOLS: list[dict]` registry. The generated registry replaces ~1000 LOC of hand-written tool schemas in `chat.py`. OpenAPI becomes the single authoring surface for tool schemas; drift between REST and chat tools becomes impossible by construction.
**Source**: Interactive — decisions captured from chat on 2026-04-24 (see Key Design Decisions).

---

## Scope

### In-Scope

- A codegen script `scripts/gen_chat_tools.py` that reads `openapi.json` and emits `src/scenecraft/chat_tools_generated.py`
- OpenAPI extensions: `x-tool`, `x-tool-description`, `x-tool-name`, `x-destructive`
- FastAPI route annotations (via `openapi_extra=`) to declare which operations are chat tools and to carry the LLM-facing description
- A generated module containing:
  - `TOOLS: list[dict]` — Anthropic-compatible tool definitions
  - `OPERATIONS: dict[str, OperationMeta]` — operation-id → HTTP method, path, request-schema source pointer
  - Header comment `# GENERATED — do not edit; run scripts/gen_chat_tools.py`
- Migration of all 32 current chat tools in `chat.py::TOOLS` to generated equivalents
- `chat.py::TOOLS` is replaced by `from scenecraft.chat_tools_generated import TOOLS`
- Snapshot test `tests/test_openapi_contract.py` that compares live `openapi.json` to `tests/fixtures/openapi.snapshot.json`
- Golden test `tests/test_generated_tools_parity.py` that compares `chat_tools_generated.TOOLS` to a committed golden fixture containing the expected shape for all tools
- A pre-commit / CI check (optional, deferred to implementation) that ensures `chat_tools_generated.py` is up-to-date with `openapi.json`

### Out-of-Scope (Non-Goals)

- FastAPI migration itself — covered by `agent/specs/local.fastapi-migration.md` (hard dependency)
- Changing how chat tools *execute*. This spec covers schema generation, not execution routing. Execution-path decision is Open Question **OQ-1**.
- MCP server schema generation. The MCP bridge (`mcp_bridge.py`) is out of scope for this spec. A future follow-up can reuse the same `OPERATIONS` registry.
- Emitting typed TypeScript clients from `openapi.json`. Out of scope.
- Auto-generating `_exec_*` stubs in `chat.py`. The `_exec_*` functions stay hand-authored for now.
- LLM-facing description authoring tool / editor UI. Descriptions are authored in Python source (route decorators).
- Tool output schemas. Anthropic's tool spec today does not include output schemas; we do not emit them. If Anthropic adds output schemas, they can be derived from `responses.200.content.application/json.schema`.
- Removing `chat.py`'s `_DESTRUCTIVE_TOOL_PATTERNS` string-matching fallback until every destructive tool is explicitly marked `x-destructive: true`.

---

## Requirements

### OpenAPI Extensions

- **R1**: Three vendor extensions are supported, authored via `openapi_extra={...}` on FastAPI route decorators:
  - `x-tool: boolean` — if `true`, the operation is a chat tool and appears in `TOOLS`. Default: `false`.
  - `x-tool-description: string` — LLM-facing description (required if `x-tool: true`). Distinct from `summary` (one-line) and `description` (Markdown docs). The `x-tool-description` is Anthropic-style: concrete about inputs, outputs, side effects, and when to use the tool.
  - `x-tool-name: string` — optional override for the chat tool name. Default: the operation's `operationId`.
  - `x-destructive: boolean` — optional. If `true`, the tool is gated by the destructive-confirmation flow in `chat.py`. Default inferred from name (see R8).

- **R2**: Extensions MUST live on the path-operation object in `openapi.json` (under `paths./foo.post.x-tool`), NOT in `components`. They are therefore trivially addressable by `(method, path)`.

- **R3**: An operation with `x-tool: true` MUST also have `x-tool-description`. The codegen raises an explicit error if `x-tool-description` is missing or empty.

### Codegen Script

- **R4**: `scripts/gen_chat_tools.py` accepts:
  - `--spec <path>` — path to `openapi.json`. Default: hit `http://localhost:<port>/openapi.json` if the server is running, else regenerate in-process via `scenecraft.api.app.app.openapi()`.
  - `--out <path>` — path to write the generated module. Default: `src/scenecraft/chat_tools_generated.py`.
  - `--check` — do not write; exit non-zero if the generated output would differ from the current file.
  - `--golden <path>` — also write the golden fixture for the parity test. Default: `tests/fixtures/generated_tools.golden.json`.
- **R5**: The script is runnable via `python -m scripts.gen_chat_tools` AND as `python scripts/gen_chat_tools.py`. A `pyproject.toml` script entry `scenecraft-gen-tools` is defined.
- **R6**: Codegen is deterministic: running it twice with the same input MUST produce byte-identical output. Operations appear in `TOOLS` sorted by tool name (lexicographic). JSON schemas within each tool have keys in a stable order (alphabetical at every level, except `type` first, then `description`, then `properties`, then `required`, then everything else).

### Tool Emission

- **R7**: For each operation with `x-tool: true`, the generator emits a dict with:
  - `name` — `x-tool-name` if set, else `operationId`. Validated to match `^[a-z][a-z0-9_]{0,63}$` (Anthropic's name constraint).
  - `description` — `x-tool-description` verbatim.
  - `input_schema` — JSON Schema object (see R9).

- **R8**: If `x-destructive: true`, the generator ALSO emits the tool name into a module-level `DESTRUCTIVE_TOOLS: frozenset[str]` set. `chat.py::_is_destructive` is updated to use this set as an authoritative source; the existing substring fallback remains as a safety net but is no longer load-bearing.

### Input Schema Derivation

- **R9**: `input_schema` is a JSON Schema `object` with `properties` merging three sources, in this order:
  1. **Path parameters** (from the operation's `parameters` with `in: path`): always required; type from parameter schema; description from parameter description.
  2. **Query parameters** (from `parameters` with `in: query`): required iff `parameter.required: true`; type from parameter schema; description from parameter description.
  3. **Request body** (from `requestBody.content.application/json.schema`): properties merged in at the top level; `required` fields added to the top-level required list. `$ref`s are resolved against the spec's `components.schemas` and inlined (to keep the tool schema self-contained — Anthropic's tool schema does not support `$ref`).

- **R10**: If a path parameter and a body field have the same name, the path parameter wins and the body field is dropped from the schema. (This is a name collision; it should be caught at authoring time. Log a warning during codegen.)

- **R11**: Request-body schemas that use `allOf`, `anyOf`, or `oneOf` are flattened where possible (e.g., `allOf` becomes property merge). `anyOf`/`oneOf` at the top level are not supported — the codegen raises an error naming the operation. This is a conservative choice; today's chat tools don't use polymorphic request bodies.

- **R12**: `additionalProperties: false` is NOT added unless the source schema declares it. Anthropic's tool schema is permissive about extras by default; matching that.

- **R13**: `default` values from the spec are preserved on properties.

- **R14**: `enum`, `minimum`, `maximum`, `minLength`, `maxLength`, `pattern`, `format` — preserved when present on the source schema.

- **R15**: `description` on each property — preserved; falls back to empty string if missing, never omitted (Anthropic's tool docs recommend per-property descriptions).

### Operation Registry

- **R16**: The generated module ALSO emits:
  ```python
  OPERATIONS: dict[str, OperationMeta] = { ... }
  ```
  where `OperationMeta` is a typed-dict (or dataclass) with:
  - `tool_name: str`
  - `method: str`
  - `path: str`
  - `path_params: tuple[str, ...]`
  - `query_params: tuple[str, ...]`
  - `body_fields: tuple[str, ...]`  # top-level required + optional body fields
  - `destructive: bool`
  This registry lets a future execution layer (see OQ-1) route a tool call to the right FastAPI operation without re-parsing `openapi.json`.

### chat.py Migration

- **R17**: `chat.py`'s `TOOLS: list[dict]` constant is replaced by `from scenecraft.chat_tools_generated import TOOLS`.
- **R18**: The 32 hand-authored tool-dict constants in `chat.py` (`SQL_QUERY_TOOL`, `UPDATE_KEYFRAME_PROMPT_TOOL`, …, `ANALYZE_MASTER_BUS_TOOL`) are **deleted** in the same PR. Their names become `operationId`s on FastAPI routes (see `local.fastapi-migration.md` R7, R47).
- **R19**: The `x-tool-description` for each of the 32 is authored by porting the existing `description` field verbatim from `chat.py`. This is a mechanical copy; reviewer can tune prose later.
- **R20**: `chat.py::_DESTRUCTIVE_TOOL_PATTERNS` is preserved as a FALLBACK, but `chat.py::_is_destructive` is updated to consult `DESTRUCTIVE_TOOLS` first. Every destructive tool today is explicitly tagged `x-destructive: true` in its FastAPI route.
- **R21**: Every tool in the generated `TOOLS` list MUST have the same `name` as the tool it replaces in the pre-migration `chat.py::TOOLS`. Verified by golden-diff test.

### Snapshot & Golden Tests

- **R22**: `tests/test_openapi_contract.py` compares the live `app.openapi()` output against `tests/fixtures/openapi.snapshot.json`. Structural differences (added/removed/renamed operation, changed parameter schema, changed `x-tool-*` fields) fail the test with a specific message. Non-substantive differences (description whitespace, FastAPI version-bumped generator fields) may be ignored via a normalization pass.
- **R23**: `tests/test_generated_tools_parity.py` compares `chat_tools_generated.TOOLS` against `tests/fixtures/generated_tools.golden.json`. Any semantic change to any tool's schema fails the test.
- **R24**: Both fixtures are regenerated via `scripts/gen_chat_tools.py --golden ... --out ...`. A failing snapshot test's error message includes the exact command to regenerate.

### Error Behavior

- **R25**: Running the codegen against a spec with NO tool-marked operations produces an empty `TOOLS = []` and an empty `OPERATIONS = {}` — NOT an error. This is the initial state before any route is marked `x-tool: true`.
- **R26**: Running against a spec where an operation has `x-tool: true` but no `x-tool-description` raises `ToolSpecError` with the operation ID in the message.
- **R27**: Running against a spec with two `x-tool: true` operations that resolve to the same `name` (via collision between `operationId` and `x-tool-name`) raises `ToolSpecError` listing both operation IDs.
- **R28**: Running against a spec where a `$ref` cannot be resolved raises `ToolSpecError` listing the ref and the operation ID.

### Generated Module Contents

- **R29**: `src/scenecraft/chat_tools_generated.py` has exactly this top-level shape (illustrative; real content populated by codegen):
  ```python
  # GENERATED — do not edit; run `python scripts/gen_chat_tools.py`.
  # Source: openapi.json emitted by scenecraft.api.app
  # Generated at: <ISO timestamp> UTC

  from __future__ import annotations
  from dataclasses import dataclass

  @dataclass(frozen=True)
  class OperationMeta:
      tool_name: str
      method: str
      path: str
      path_params: tuple[str, ...]
      query_params: tuple[str, ...]
      body_fields: tuple[str, ...]
      destructive: bool

  TOOLS: list[dict] = [ ... ]
  OPERATIONS: dict[str, OperationMeta] = { ... }
  DESTRUCTIVE_TOOLS: frozenset[str] = frozenset({ ... })
  ```

### CI Hook

- **R30**: A CI job `tools-up-to-date` runs `python scripts/gen_chat_tools.py --check` and fails if the generated file differs from what's committed. The job prints the diff and the regeneration command.

---

## Interfaces / Data Shapes

### FastAPI route annotation (authoring surface)

```python
@router.post(
    "/audio-tracks/add",
    operation_id="add_audio_track",
    summary="Add an audio track to a project",
    response_model=AddAudioTrackResponse,
    openapi_extra={
        "x-tool": True,
        "x-tool-description": (
            "Create a new audio track on the project timeline. The track is appended "
            "after existing tracks unless an explicit display_order is given. Volume "
            "defaults to 1.0 unless an initial_volume is provided, in which case a "
            "constant volume_curve is seeded. Wrapped in a single undo group."
        ),
        "x-destructive": False,
    },
)
async def add_audio_track(...): ...
```

### Generated tool dict (Anthropic-compatible)

```json
{
  "name": "add_audio_track",
  "description": "Create a new audio track on the project timeline. ...",
  "input_schema": {
    "type": "object",
    "properties": {
      "name": {
        "type": "string",
        "description": "Project name (URL path parameter)."
      },
      "display_name": {
        "type": "string",
        "description": "Display name. Auto-generated if omitted."
      },
      "initial_volume": {
        "type": "number",
        "description": "Initial volume in 0..1; seeds a constant volume_curve.",
        "default": 1.0
      }
    },
    "required": ["name"]
  }
}
```

### OperationMeta (execution-side)

```python
@dataclass(frozen=True)
class OperationMeta:
    tool_name: str            # "add_audio_track"
    method: str               # "POST"
    path: str                 # "/api/projects/{name}/audio-tracks/add"
    path_params: tuple[str]   # ("name",)
    query_params: tuple[str]  # ()
    body_fields: tuple[str]   # ("display_name", "initial_volume")
    destructive: bool         # False
```

### Generated file header

```python
# GENERATED — do not edit; run `python scripts/gen_chat_tools.py`.
# Source: openapi.json emitted by scenecraft.api.app
# Generated at: 2026-04-24T17:32:05Z
```

### Snapshot fixture shape (`tests/fixtures/openapi.snapshot.json`)

Identical to `app.openapi()` output, with certain volatile fields normalized (FastAPI version, generator annotations stripped).

### Golden fixture shape (`tests/fixtures/generated_tools.golden.json`)

```json
{
  "tools": [
    { "name": "add_audio_track", "description": "...", "input_schema": { ... } },
    ...
  ],
  "destructive": ["delete_keyframe", "delete_transition", ...]
}
```

---

## Behavior Table

| # | Scenario | Expected Behavior | Tests |
|---|----------|-------------------|-------|
| 1 | Route annotated with `x-tool: true` and `x-tool-description` | Appears in generated `TOOLS` with correct schema | `happy-path-emits-tool` |
| 2 | Route NOT annotated | Does NOT appear in `TOOLS` | `unannotated-route-skipped` |
| 3 | Route with `x-tool: true` but missing `x-tool-description` | Codegen exits non-zero with explicit error | `missing-description-errors` |
| 4 | Two routes resolve to the same tool name | Codegen exits non-zero listing the collision | `name-collision-errors` |
| 5 | Request body has `$ref` to `components.schemas.Foo` | Ref resolved and inlined into `input_schema` | `ref-resolved-inline` |
| 6 | Request body uses `allOf` merge | Flattened into a single property set | `allof-flattened` |
| 7 | Request body uses top-level `anyOf` / `oneOf` | Codegen errors | `polymorphic-body-errors` |
| 8 | Path parameter and body field share a name | Path param wins; body field dropped; warning logged | `path-body-collision-path-wins` |
| 9 | Operation has query params | Query params merged into `input_schema.properties`; required iff `required: true` | `query-params-merged` |
| 10 | Operation has no body, no params | `input_schema` is `{"type": "object", "properties": {}, "required": []}` (or equivalent empty) | `empty-input-schema-ok` |
| 11 | `x-destructive: true` on a tool | Tool name appears in `DESTRUCTIVE_TOOLS` set | `destructive-flag-captured` |
| 12 | `x-destructive` absent | Tool name NOT in `DESTRUCTIVE_TOOLS` | `non-destructive-default` |
| 13 | Codegen run twice in a row | Byte-identical output both times | `codegen-deterministic` |
| 14 | Codegen `--check` against stale file | Exit code 1; diff printed | `check-mode-detects-drift` |
| 15 | Codegen `--check` against up-to-date file | Exit code 0; no output | `check-mode-silent-when-fresh` |
| 16 | Generated file is importable | `from scenecraft.chat_tools_generated import TOOLS` succeeds; `TOOLS` is a list of dicts | `module-imports-cleanly` |
| 17 | Every tool in generated `TOOLS` has `name`, `description`, `input_schema` | Shape passes Anthropic's tool-definition schema | `anthropic-tool-shape-valid` |
| 18 | `name` does not match `^[a-z][a-z0-9_]{0,63}$` | Codegen errors with the offending name | `invalid-tool-name-errors` |
| 19 | All 32 legacy tool names are present in generated `TOOLS` | Golden test passes | `32-legacy-tools-preserved` |
| 20 | Generated `input_schema` for each of the 32 legacy tools matches legacy semantics | Required fields match; property types match | `legacy-schemas-preserved` |
| 21 | `chat.py` imports `TOOLS` from the generated module | `chat.py::TOOLS is chat_tools_generated.TOOLS` | `chat-imports-generated` |
| 22 | The 32 hand-authored tool-dict constants in `chat.py` are deleted | `git grep 'SQL_QUERY_TOOL = '` returns zero hits in `src/` | `legacy-constants-deleted` |
| 23 | OpenAPI snapshot test on live spec matches committed fixture | Snapshot test passes | `openapi-snapshot-matches` |
| 24 | Removing an operation from the app without regenerating | Snapshot test fails with a specific message | `snapshot-test-flags-drift` |
| 25 | CI `tools-up-to-date` job | Passes when committed file is fresh; fails with diff otherwise | `ci-tools-up-to-date-job` |
| 26 | `enum` on a body field | Preserved in generated schema | `enum-preserved` |
| 27 | `default` on a body field | Preserved in generated schema | `default-preserved` |
| 28 | `description` on a property | Preserved; empty string when missing | `description-preserved-or-empty` |
| 29 | Running codegen with no tool-marked ops | `TOOLS = []`; `OPERATIONS = {}`; no error | `empty-tool-set-is-fine` |
| 30 | `$ref` that cannot be resolved | Clear error with ref + operation ID | `unresolvable-ref-errors` |
| 31 | Regeneration after adding `x-tool: true` to a new route | Diff contains exactly the new tool entry | `incremental-add-minimal-diff` |
| 32 | `OperationMeta.path` preserves path templating (`{name}`) | Method/path can be substituted at call time | `operation-meta-has-templated-path` |
| 33 | `x-tool-name` override wins over `operationId` | Generated tool's `name` is the override | `tool-name-override-wins` |
| 34 | `undefined` — chat tool execution path (in-process direct vs ASGI round-trip vs extracted service layer) | `undefined` | → [OQ-1](#open-questions) |
| 35 | `undefined` — output schemas from `responses.200.content` | `undefined` | → [OQ-2](#open-questions) |
| 36 | `undefined` — MCP bridge integration (reuse `OPERATIONS` for MCP server?) | `undefined` | → [OQ-3](#open-questions) |
| 37 | `undefined` — tool description authoring tooling (editor, template, linter) | `undefined` | → [OQ-4](#open-questions) |

---

## Behavior

### Authoring a new chat tool

1. Developer adds `x-tool: true` and `x-tool-description: "..."` to an existing FastAPI route's `openapi_extra`.
2. Developer runs `python scripts/gen_chat_tools.py`.
3. `chat_tools_generated.py` updates; `chat.py::TOOLS` (which imports it) now includes the new tool immediately.
4. Developer commits the generated file alongside the route change. Snapshot + golden fixtures regenerated if needed.

### Running the codegen

1. If a local server is running and `--spec` is omitted, fetch `openapi.json` via HTTP.
2. Otherwise, import `scenecraft.api.app.app` and call `app.openapi()` in-process.
3. Walk every `paths.<path>.<method>` operation in the spec:
   - Skip operations without `x-tool: true`.
   - Extract `operationId`, `x-tool-name`, `x-tool-description`, `x-destructive`.
   - Derive `input_schema` from path params + query params + request body.
   - Build a tool dict `{name, description, input_schema}`.
   - Build an `OperationMeta`.
4. Validate: name format, description presence, no collisions.
5. Sort tools by name; serialize with stable key order.
6. Write `chat_tools_generated.py` (or compare if `--check`).
7. If `--golden <path>` given, also write the golden fixture.

### CI enforcement

1. On every PR, `tools-up-to-date` runs the codegen in `--check` mode.
2. If diff exists, CI fails with a message: `Generated tools drift detected. Run 'python scripts/gen_chat_tools.py' and commit.`
3. `test_openapi_contract.py` runs in the main test suite; if live spec drifts from snapshot, the snapshot must be regenerated.
4. `test_generated_tools_parity.py` runs in the main test suite; if `chat_tools_generated.py` drifts from the golden fixture, the golden must be regenerated.

---

## Acceptance Criteria

- [ ] `scripts/gen_chat_tools.py` exists, runs end-to-end, and produces a `chat_tools_generated.py` that imports cleanly.
- [ ] All 32 pre-existing chat tool names appear in `TOOLS` with identical semantic schemas to their pre-migration `chat.py` counterparts.
- [ ] `chat.py::TOOLS` is a re-export from `chat_tools_generated`; the 32 hand-authored tool-dict constants are deleted.
- [ ] `chat.py::_is_destructive` consults the generated `DESTRUCTIVE_TOOLS` set as primary source; substring fallback remains as safety net.
- [ ] Running `python scripts/gen_chat_tools.py --check` on a fresh checkout exits 0.
- [ ] Modifying an `x-tool-description` in a route without regenerating fails CI.
- [ ] Removing an operation from the app without regenerating fails the snapshot test.
- [ ] `tests/test_openapi_contract.py` compares live spec to `tests/fixtures/openapi.snapshot.json`.
- [ ] `tests/test_generated_tools_parity.py` compares generated `TOOLS` to `tests/fixtures/generated_tools.golden.json`.
- [ ] The codegen is deterministic: running twice produces byte-identical output.
- [ ] OpenAPI extensions (`x-tool`, `x-tool-description`, `x-tool-name`, `x-destructive`) are documented in the spec file itself (this doc) AND in code comments on the first route that uses them.
- [ ] A CI job enforces `tools-up-to-date`.
- [ ] Running the chat agent end-to-end with at least 3 tools (`add_audio_track`, `sql_query`, `delete_keyframe` covering non-destructive, read-only, and destructive) succeeds and matches behavior prior to codegen.

---

## Tests

### Base Cases

The core behavior contract: happy path, primary positive and negative assertions, and the parity guarantees for the 32 legacy tools.

#### Test: happy-path-emits-tool (covers R1, R3, R7, R9)

**Given**:
- A FastAPI route `POST /api/projects/{name}/audio-tracks/add` with `operationId="add_audio_track"` and `openapi_extra={"x-tool": True, "x-tool-description": "D", "x-destructive": False}`
- A request body model `AddAudioTrackBody` with `display_name: str | None`, `initial_volume: float = 1.0`, and path param `name: str`

**When**: Codegen runs.

**Then** (assertions):
- **tool-present**: `TOOLS` contains a dict with `"name": "add_audio_track"`
- **description-matches**: that dict's `description` equals `"D"`
- **schema-has-name**: `input_schema.properties` contains `name` (path param), required
- **schema-has-display-name**: `input_schema.properties` contains `display_name` (body), NOT in required
- **schema-has-initial-volume-default**: `initial_volume` present with `default: 1.0`
- **not-destructive**: `"add_audio_track"` NOT in `DESTRUCTIVE_TOOLS`
- **in-operations**: `OPERATIONS["add_audio_track"].method == "POST"`

#### Test: unannotated-route-skipped (covers R1)

**Given**: A route with no `x-tool` extension.

**When**: Codegen runs.

**Then** (assertions):
- **tool-absent**: `TOOLS` contains no dict with `name` matching that route's `operationId`

#### Test: missing-description-errors (covers R3, R26)

**Given**: A route with `x-tool: true` but no `x-tool-description`.

**When**: Codegen runs.

**Then** (assertions):
- **exit-nonzero**: exit code is non-zero
- **error-mentions-operation-id**: stderr contains the offending `operationId`
- **error-class**: raised exception is `ToolSpecError` (if imported as a module)
- **no-file-written**: `chat_tools_generated.py` was NOT modified

#### Test: name-collision-errors (covers R27)

**Given**: Two routes both resolve to tool name `foo` (one via `operationId="foo"`, the other via `x-tool-name="foo"`).

**When**: Codegen runs.

**Then** (assertions):
- **exit-nonzero**: exit code non-zero
- **error-lists-both**: error message names both operation IDs
- **error-class**: `ToolSpecError`

#### Test: 32-legacy-tools-preserved (covers R21, R19)

**Given**: A fixture listing the 32 tool names that exist in pre-migration `chat.py::TOOLS`.

**When**: Codegen runs against the post-migration app (which has all 32 routes annotated).

**Then** (assertions):
- **all-32-names-present**: every name in the fixture appears in `TOOLS`
- **no-extras**: no additional tools beyond the 32 (unless new tools were intentionally added — fixture must be updated in that case)
- **count-matches**: `len(TOOLS)` equals the fixture count

#### Test: legacy-schemas-preserved (covers R20)

**Given**: Pre-migration schemas for each of the 32 tools, captured in `tests/fixtures/legacy_tool_schemas.json`.

**When**: Codegen runs.

**Then** (assertions per tool, table-driven):
- **required-matches**: generated `input_schema.required` equals legacy `required`
- **property-types-match**: every property's `type` matches (including array item types)
- **enums-match**: any `enum` constraint matches
- **defaults-match**: any `default` matches
- **descriptions-present**: every property has a non-null `description` (content may differ from legacy since we're porting prose, but presence is mandatory)

#### Test: chat-imports-generated (covers R17)

**When**: `import scenecraft.chat` and inspect `scenecraft.chat.TOOLS`.

**Then** (assertions):
- **is-same-object**: `scenecraft.chat.TOOLS is scenecraft.chat_tools_generated.TOOLS`
- **tools-is-list**: `isinstance(scenecraft.chat.TOOLS, list)`

#### Test: legacy-constants-deleted (covers R18, R22)

**When**: `git grep 'SQL_QUERY_TOOL = '` in `src/`.

**Then** (assertions):
- **zero-hits**: no results (constant is deleted)
- **32-constants-all-deleted**: none of the 32 `*_TOOL = {` definitions remain in `chat.py`

#### Test: module-imports-cleanly (covers R29)

**When**: `import scenecraft.chat_tools_generated`.

**Then** (assertions):
- **import-succeeds**: no exception
- **tools-is-list**: `TOOLS` is a `list`
- **operations-is-dict**: `OPERATIONS` is a `dict`
- **destructive-is-frozenset**: `DESTRUCTIVE_TOOLS` is a `frozenset`

#### Test: anthropic-tool-shape-valid (covers R7, R17)

**Given**: Anthropic's tool definition schema (either the published JSON schema or a local copy).

**When**: Validate every dict in `TOOLS` against that schema.

**Then** (assertions):
- **all-pass**: every tool validates cleanly
- **no-extra-top-level-keys**: no tool has keys beyond `name`, `description`, `input_schema` (Anthropic accepts more but we stay minimal)

#### Test: codegen-deterministic (covers R6)

**When**: Run `scripts/gen_chat_tools.py` twice, capturing both outputs.

**Then** (assertions):
- **byte-identical**: outputs are byte-for-byte identical
- **timestamp-line-stable**: either the timestamp line is normalized out before compare, or it is absent from the diff

#### Test: check-mode-detects-drift (covers R4)

**Given**: `chat_tools_generated.py` in the repo does NOT match fresh codegen output (e.g., an operation was added without regeneration).

**When**: `scripts/gen_chat_tools.py --check`.

**Then** (assertions):
- **exit-code-1**: exit code is 1
- **stderr-has-diff**: stderr contains a unified diff or a "drift detected" message
- **regeneration-command-printed**: stderr prints `python scripts/gen_chat_tools.py` or the CLI equivalent

#### Test: check-mode-silent-when-fresh (covers R4)

**Given**: Repo's `chat_tools_generated.py` matches fresh output.

**When**: `--check` runs.

**Then** (assertions):
- **exit-code-0**: 0
- **no-stdout**: stdout empty
- **no-stderr**: stderr empty

#### Test: empty-tool-set-is-fine (covers R25)

**Given**: A test app where no route has `x-tool: true`.

**When**: Codegen runs.

**Then** (assertions):
- **tools-empty**: `TOOLS == []`
- **operations-empty**: `OPERATIONS == {}`
- **destructive-empty**: `DESTRUCTIVE_TOOLS == frozenset()`
- **exit-code-0**: 0

#### Test: query-params-merged (covers R9)

**Given**: A route with query param `limit: int = 100` (optional with default) and path param `name: str`.

**When**: Codegen runs.

**Then** (assertions):
- **has-limit**: `input_schema.properties.limit.type == "integer"`
- **has-default**: `input_schema.properties.limit.default == 100`
- **not-required**: `limit` not in `required`

#### Test: empty-input-schema-ok (covers R9)

**Given**: A route with no params, no body (e.g., `POST /api/projects/{name}/undo` has only a path param).

**When**: Codegen runs (for a hypothetical zero-param route).

**Then** (assertions):
- **schema-is-object**: `input_schema.type == "object"`
- **properties-empty**: `input_schema.properties == {}`
- **required-empty**: `input_schema.required == []`

#### Test: destructive-flag-captured (covers R8, R20)

**Given**: A route with `x-destructive: true`.

**When**: Codegen runs.

**Then** (assertions):
- **name-in-destructive**: tool name is in `DESTRUCTIVE_TOOLS`
- **is-destructive-returns-true**: `chat._is_destructive(name)` returns True

#### Test: non-destructive-default (covers R8)

**Given**: A route with `x-tool: true` but no `x-destructive`.

**When**: Codegen runs.

**Then** (assertions):
- **name-not-in-destructive**: tool name NOT in `DESTRUCTIVE_TOOLS`

#### Test: openapi-snapshot-matches (covers R22)

**When**: Test loads `tests/fixtures/openapi.snapshot.json` and compares to `app.openapi()`.

**Then** (assertions):
- **paths-match**: every path in fixture is in live spec and vice versa
- **operation-ids-match**: for each operation, `operationId` matches
- **x-tool-fields-match**: `x-tool`, `x-tool-description`, `x-tool-name`, `x-destructive` match

### Edge Cases

Boundaries, unusual inputs, concurrency, idempotency, ordering, time-dependent behavior, resource exhaustion.

#### Test: ref-resolved-inline (covers R9)

**Given**: A request body schema uses `$ref: "#/components/schemas/AudioClip"` with a non-trivial nested structure.

**When**: Codegen runs.

**Then** (assertions):
- **ref-inlined**: generated `input_schema` contains the resolved structure; `$ref` string does not appear
- **nested-descriptions-present**: nested properties retain their `description`

#### Test: allof-flattened (covers R11)

**Given**: A request body schema of the form `allOf: [SchemaA, SchemaB]` where both are plain object schemas.

**When**: Codegen runs.

**Then** (assertions):
- **properties-merged**: `input_schema.properties` contains union of A's and B's properties
- **required-merged**: `input_schema.required` contains union of A's and B's required
- **conflict-rule-documented**: if the same property exists in both, A wins (first-listed schema) — documented in codegen source

#### Test: polymorphic-body-errors (covers R11)

**Given**: A request body with top-level `oneOf: [SchemaA, SchemaB]`.

**When**: Codegen runs.

**Then** (assertions):
- **exit-nonzero**: exit code non-zero
- **error-mentions-oneof**: error message mentions `oneOf` / `anyOf` and names the operation
- **error-suggests-refactor**: error message suggests removing the polymorphism or adding a tool-specific adapter

#### Test: path-body-collision-path-wins (covers R10)

**Given**: A route with path param `name` AND a body field also named `name`.

**When**: Codegen runs.

**Then** (assertions):
- **path-wins**: `input_schema.properties.name` reflects the path param's type/description
- **body-field-dropped**: the body field is NOT present
- **warning-logged**: codegen stderr contains a collision warning naming the operation

#### Test: unresolvable-ref-errors (covers R28)

**Given**: A body schema references `#/components/schemas/Missing` that doesn't exist.

**When**: Codegen runs.

**Then** (assertions):
- **exit-nonzero**: exit code non-zero
- **error-mentions-ref**: error message names the ref string
- **error-mentions-operation**: error message names the operation ID

#### Test: invalid-tool-name-errors (covers R7, R18)

**Given**: An `operationId` like `Add-Audio-Track` (PascalCase/hyphens — invalid per Anthropic's name constraint).

**When**: Codegen runs.

**Then** (assertions):
- **exit-nonzero**: exit code non-zero
- **error-names-offending**: stderr names the offending operation ID
- **error-cites-rule**: error message cites the `^[a-z][a-z0-9_]{0,63}$` pattern

#### Test: enum-preserved (covers R14)

**Given**: A body field with `enum: ["opacity", "red", "green", "blue"]`.

**When**: Codegen runs.

**Then** (assertions):
- **enum-present**: generated `input_schema.properties.<field>.enum` equals the source list

#### Test: default-preserved (covers R13)

**Given**: A body field with `default: 100`.

**When**: Codegen runs.

**Then** (assertions):
- **default-present**: generated property has `default: 100`

#### Test: description-preserved-or-empty (covers R15)

**Given**: One field with a description and one without.

**When**: Codegen runs.

**Then** (assertions):
- **present-preserved**: first property's `description` matches source
- **absent-becomes-empty**: second property's `description` is `""` (or null per the project's chosen convention — stable either way)

#### Test: tool-name-override-wins (covers R1)

**Given**: `operationId="foo_bar"` with `x-tool-name="different_name"`.

**When**: Codegen runs.

**Then** (assertions):
- **tool-name-is-override**: `TOOLS[i].name == "different_name"`
- **operation-meta-keyed-by-override**: `OPERATIONS["different_name"]` exists; `OPERATIONS["foo_bar"]` does not

#### Test: incremental-add-minimal-diff (covers R31)

**Given**: Committed `chat_tools_generated.py` before adding a new `x-tool: true` annotation.

**When**: Add `x-tool: true` to one route, run codegen.

**Then** (assertions):
- **diff-scope-bounded**: the diff of `chat_tools_generated.py` adds exactly one tool entry and one `OPERATIONS` entry
- **no-other-changes**: no unrelated lines changed (deterministic ordering enforces this)

#### Test: operation-meta-has-templated-path (covers R16)

**When**: Inspect `OPERATIONS["add_audio_track"].path`.

**Then** (assertions):
- **templated**: value is `"/api/projects/{name}/audio-tracks/add"` (NOT a resolved path)
- **path-params-match**: `path_params == ("name",)`

#### Test: snapshot-test-flags-drift (covers R22, R24)

**Given**: Committed snapshot. An operation is deleted from the app.

**When**: `pytest tests/test_openapi_contract.py` runs.

**Then** (assertions):
- **test-fails**: snapshot comparison fails
- **failure-message-names-operation**: failure message identifies the missing operation
- **failure-message-has-regen-command**: message includes the regeneration CLI

#### Test: ci-tools-up-to-date-job (covers R30)

**Given**: CI pipeline with a job that runs `python scripts/gen_chat_tools.py --check`.

**When**: PR introduces a tool change without regenerating.

**Then** (assertions):
- **ci-fails**: job exit code non-zero
- **ci-log-has-diff**: CI log shows the diff
- **ci-log-has-fix-command**: CI log prints the regeneration command

---

## UI-Structure Test Strategy

No UI in this spec. The tool catalog surface is the chat agent UI (existing), which consumes `TOOLS` unchanged.

---

## Non-Goals

Summarized from Out-of-Scope; restated for proofing clarity:

- FastAPI migration (dependency; separate spec)
- Deciding chat tool execution path (see OQ-1)
- MCP server integration
- TypeScript client codegen
- Auto-generating `_exec_*` implementations
- Tool description authoring UI
- Output-schema generation (Anthropic's tool definitions don't support it)
- Removing `_DESTRUCTIVE_TOOL_PATTERNS` from `chat.py` (stays as a safety net)

---

## Open Questions

1. **OQ-1** — **Chat tool execution path.** Three options, presented with tradeoffs rather than a guess:

   **(a) In-process direct calls (status quo; no change from today)**
   Chat tools keep dispatching through `_exec_<tool_name>(project_dir, input_data) -> dict` functions in `chat.py`. The generated `OPERATIONS` registry is informational only. OpenAPI is a shared **contract** that both the FastAPI handler and the `_exec_*` function conform to; drift is caught by behavior tests, not by structural equivalence.
   - **Pros**: zero added latency; no re-serialization; no double authz; matches today's topology; simplest migration.
   - **Cons**: two execution paths to keep in sync (FastAPI handler + `_exec_*`); if they diverge in behavior, the drift is silent.
   - **Drift mitigation**: a cross-execution parity test that feeds identical inputs through both paths and asserts identical DB deltas for a representative 8–10 tools.

   **(b) In-process ASGI round-trip**
   Chat tools dispatch via `httpx.AsyncClient(app=app)` — the tool call is serialized to an HTTP request, routed through FastAPI's middleware stack, and the response is deserialized. Single execution path.
   - **Pros**: impossible for chat and REST behavior to drift; validation, auth, CORS, error handling all run in one place; matches the "contract" spec exactly.
   - **Cons**: serialization overhead per call (dict → JSON → dict, ~sub-ms for most ops, but compounds in batch ops like `apply_mix_plan`); authz runs twice (chat session already authenticated, now re-validates); error shapes go through the HTTP error envelope (fine, but changes exception flow for tool callers).
   - **Performance impact**: estimated +1–3ms per call; for the hot batch ops (`apply_mix_plan`, `generate_descriptions`) the per-op overhead compounds across dozens of sub-operations.

   **(c) Extracted service layer**
   Refactor `db.py`, `audio_intelligence.py`, etc. into a formal service layer. Both FastAPI handlers and `_exec_*` become one-line wrappers calling `services.add_audio_track(project, spec)`. OpenAPI describes the service surface; HTTP and chat surfaces share implementation at the Python-function level.
   - **Pros**: structurally impossible for HTTP and chat to drift; no serialization; testable in isolation; clean architecture.
   - **Cons**: largest refactor; requires moving logic out of `api_server.py` (FastAPI migration is already doing this — might be an opportunity to piggyback) AND consolidating `chat.py::_exec_*` bodies; the boundary between "service" and "db" is already fuzzy since `_exec_*` today already calls `db.*` directly.
   - **Observation**: option (c) is more or less the **status quo**, cleaned up. Today `_exec_add_audio_track` calls `db.add_audio_track`. A FastAPI handler for `POST /audio-tracks/add` would also call `db.add_audio_track`. So "extract service layer" mostly means "name the shared function more carefully and document the contract."

   **Recommendation**: **(a) with the cross-execution parity test.** (c) is what the code already does, modulo documentation. (b) introduces real per-call overhead for a drift problem that can be covered by tests. Take the win from (c)'s natural shape, codify it with the parity test from (a), and defer (b) to a future milestone if drift actually shows up.

   **Captured as Behavior Table row 34.**

2. **OQ-2** — **Output schemas.** Anthropic's tool definition today does NOT include output schemas. If that changes, the codegen should derive them from `responses.200.content.application/json.schema`. Should we emit output schemas anyway for documentation / MCP reuse, or wait? **Default: wait.** **Captured as row 35.**

3. **OQ-3** — **MCP bridge integration.** `src/scenecraft/mcp_bridge.py` exists and exposes tools to MCP clients. Should the MCP bridge also consume `OPERATIONS` to avoid triple drift (REST / chat / MCP)? Answer depends on how much of `mcp_bridge.py` duplicates `chat.py::TOOLS`. **Captured as row 36.**

4. **OQ-4** — **Description authoring tooling.** `x-tool-description` lives in Python source today. Should we add a linter that enforces style (e.g., "describes inputs, outputs, side effects, when-to-use")? Or a template / snippet? **Default: none.** Ports of the 32 legacy tools are already prose-reviewed; new-tool authors can use existing ones as templates. **Captured as row 37.**

5. **OQ-5** — **Codegen output file location.** Alternative: `src/scenecraft/generated/chat_tools.py` in a `generated/` subpackage to make the "do not edit" boundary more visible. **Default: flat file at `src/scenecraft/chat_tools_generated.py` per R29.** Low-stakes; defer to implementation.

### Resolved / reclassified

- **Chat tool name collisions** — prevented structurally by R27. Not an open question.
- **32-tool migration is mechanical** — R19 says port descriptions verbatim; prose tuning is a follow-up.

---

## Key Design Decisions

Captured from chat on 2026-04-24:

- **Two specs, not one** — this one depends on `local.fastapi-migration.md` but is testable independently once the migration ships.
- **Codegen over runtime generation** — write a file to disk, commit it, test it. Not a `__init__.py` import-time shim that parses `openapi.json` on startup. Commit-time codegen has clearer debugging, zero startup cost, and enables the `--check` CI pattern.
- **Anthropic-compatible tool shape** — the generated `TOOLS` list targets the Anthropic messages API tool schema (`name`, `description`, `input_schema`). MCP can reuse `OPERATIONS` later.
- **OpenAPI is the authoring surface** — developers author tool metadata alongside the route, via `openapi_extra=`. No separate YAML or TOML file.
- **Deterministic output** — stable sort + stable JSON key order means the generated file diffs cleanly. Non-deterministic generators make the `--check` pattern useless.
- **Two fixture tests, not one** — snapshot against `openapi.json` catches spec drift; golden against `TOOLS` catches codegen regressions. They fail in different conditions and point at different fixes.
- **Destructive flag is primary, pattern match is safety net** — `_DESTRUCTIVE_TOOL_PATTERNS` stays in `chat.py` but `_is_destructive` consults `DESTRUCTIVE_TOOLS` first. Belt and suspenders.
- **Execution path deferred** — OQ-1 is the single unresolved decision; the spec is otherwise complete. Recommendation leans toward (a) + parity test.

---

## Related Artifacts

- `agent/specs/local.fastapi-migration.md` — hard dependency (provides `openapi.json` and `operationId` convention)
- `src/scenecraft/chat.py` — current home of `TOOLS` and `_exec_*` dispatchers; will import generated module
- `src/scenecraft/mcp_bridge.py` — separate MCP surface; potential future consumer of `OPERATIONS`
- Anthropic Messages API tool-use documentation — target schema for generated tools
- `tests/fixtures/openapi.snapshot.json` — committed spec snapshot (created during implementation)
- `tests/fixtures/generated_tools.golden.json` — committed tool golden (created during implementation)
- `tests/fixtures/legacy_tool_schemas.json` — pre-migration capture of the 32 tools' schemas for the parity test
