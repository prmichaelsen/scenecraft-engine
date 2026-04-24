# Task 67: Annotate 32 existing routes with `x-tool` / `x-tool-description` / `x-destructive`

**Milestone**: [M16 — FastAPI Migration + Tool Codegen](../../milestones/milestone-16-fastapi-migration-and-tool-codegen.md)
**Spec**: [`local.openapi-tool-codegen`](../../specs/local.openapi-tool-codegen.md) — R7, R8, R19, R20, R21
**Estimated Time**: 3–4 hours
**Dependencies**: T65, T66
**Status**: Not Started

---

## Objective

Mechanical port. Walk the existing `chat.py::TOOLS` list and tag each tool's matching FastAPI route with `openapi_extra={"x-tool": True, "x-tool-description": "...", "x-destructive": True/False}`. Descriptions are ported **verbatim** from `chat.py`; prose tuning is a follow-up, not this task's scope. At the end of this task, running `python scripts/gen_chat_tools.py --out /tmp/test.py` produces a module with exactly 32 tools matching pre-migration semantics.

---

## TDD Plan

Before this task, capture pre-migration schemas for the 32 tools (the **legacy** schemas from `chat.py::TOOLS`) into `tests/fixtures/legacy_tool_schemas.json`. This is the source of truth for the parity test. Write the parity test in T68 (since T68 also wires chat.py). For this task, the main test is "codegen produces exactly 32 tools" and "each has the expected name and destructive flag."

---

## Steps

### 1. Capture `tests/fixtures/legacy_tool_schemas.json`

Write `scripts/capture_legacy_tool_schemas.py` (one-off, run **against pre-migration `chat.py`**):

```python
# Pre-migration capture — run before T68 removes the legacy constants.
import json, sys
from scenecraft.chat import TOOLS
out = []
for t in TOOLS:
    out.append({
        "name": t["name"],
        "description": t["description"],
        "input_schema": t["input_schema"],
    })
json.dump({"tools": out}, sys.stdout, indent=2)
```

Commit `tests/fixtures/legacy_tool_schemas.json`. This fixture MUST be captured **before** T68 deletes the constants.

### 2. Authoring the 32 annotations

For each of the 32 tools in `chat.py::TOOLS`, locate the matching FastAPI route (operationId == tool name, per T61/T62/T64 alignment). On that route's decorator, set:

```python
openapi_extra={
    "x-tool": True,
    "x-tool-description": <verbatim description from chat.py>,
    "x-destructive": <True if in _DESTRUCTIVE_TOOL_PATTERNS match else False>,
}
```

Do it one at a time; commit per-tool if granular diffs are useful, otherwise batch in logical clusters.

### Destructive flag determination

Today's `_DESTRUCTIVE_TOOL_PATTERNS` does substring matching: `delete`, `remove`, `destroy`, `drop`, `publish`, `retract`, `revise`, `moderate`, `restore_checkpoint`, `batch_delete`, `generate_`, `isolate_`. Apply this to each name. Additionally, mark `apply_mix_plan` as destructive (it batches mutations and is not trivially undoable without a checkpoint).

### The 32 tools + their routes + destructive flag

| # | Chat tool | Route operationId | Route | Destructive |
|---|---|---|---|---|
| 1 | `sql_query` | `sql_query` | `POST /api/projects/{name}/sql/query` (added in T64) | false |
| 2 | `update_keyframe_prompt` | `update_keyframe_prompt` | `POST /api/projects/{name}/update-prompt` (aliased in T61) | false |
| 3 | `update_keyframe_timestamp` | `update_keyframe_timestamp` | `POST /api/projects/{name}/update-timestamp` (aliased in T61) | false |
| 4 | `update_curve` | `update_curve` | `POST /api/projects/{name}/update-transition-remap` or similar — **audit** | false |
| 5 | `update_transform_curve` | `update_transform_curve` | audit per T61 | false |
| 6 | `delete_keyframe` | `delete_keyframe` | `POST /api/projects/{name}/delete-keyframe` | **true** |
| 7 | `delete_transition` | `delete_transition` | `POST /api/projects/{name}/delete-transition` | **true** |
| 8 | `batch_delete_keyframes` | `batch_delete_keyframes` | `POST /api/projects/{name}/batch-delete-keyframes` | **true** |
| 9 | `batch_delete_transitions` | `batch_delete_transitions` | `POST /api/projects/{name}/batch-delete-transitions` (added in T61) | **true** |
| 10 | `add_keyframe` | `add_keyframe` | `POST /api/projects/{name}/add-keyframe` | false |
| 11 | `update_keyframe` | `update_keyframe` | `POST /api/projects/{name}/update-keyframe` | false |
| 12 | `update_transition` | `update_transition` | `POST /api/projects/{name}/update-transition` | false |
| 13 | `split_transition` | `split_transition` | `POST /api/projects/{name}/split-transition` | false |
| 14 | `assign_keyframe_image` | `assign_keyframe_image` | `POST /api/projects/{name}/assign-keyframe-image` | false |
| 15 | `assign_pool_video` | `assign_pool_video` | `POST /api/projects/{name}/assign-pool-video` | false |
| 16 | `checkpoint` | `checkpoint` | `POST /api/projects/{name}/checkpoint` | false |
| 17 | `list_checkpoints` | `list_checkpoints` | `GET /api/projects/{name}/checkpoints` | false |
| 18 | `restore_checkpoint` | `restore_checkpoint` | `POST /api/projects/{name}/checkpoint/restore` | **true** (restore_checkpoint is in patterns) |
| 19 | `generate_keyframe_candidates` | `generate_keyframe_candidates` | `POST /api/projects/{name}/generate-keyframe-candidates` | **true** (`generate_` pattern) |
| 20 | `generate_transition_candidates` | `generate_transition_candidates` | `POST /api/projects/{name}/generate-transition-candidates` | **true** |
| 21 | `isolate_vocals__run` | `isolate_vocals__run` | plugin route (`POST /api/projects/{name}/plugins/isolate_vocals/run`) — tag in plugin manifest | **true** (`isolate_` pattern) |
| 22 | `add_audio_track` | `add_audio_track` | `POST /api/projects/{name}/audio-tracks/add` | false |
| 23 | `add_audio_clip` | `add_audio_clip` | `POST /api/projects/{name}/audio-clips/add-from-pool` | false |
| 24 | `update_volume_curve` | `update_volume_curve` | `POST /api/projects/{name}/audio-tracks/{track_id}/volume-curve` (added in T62) | false |
| 25 | `generate_dsp` | `generate_dsp` | `POST /api/projects/{name}/dsp/generate` (added in T62) | **true** (`generate_` pattern) |
| 26 | `add_audio_effect` | `add_audio_effect` | `POST /api/projects/{name}/track-effects` | false |
| 27 | `add_master_bus_effect` | `add_master_bus_effect` | `POST /api/projects/{name}/master-bus-effects/add` | false |
| 28 | `remove_master_bus_effect` | `remove_master_bus_effect` | `POST /api/projects/{name}/master-bus-effects/remove` | **true** |
| 29 | `update_effect_param_curve` | `update_effect_param_curve` | `POST /api/projects/{name}/effect-curves/batch` | false |
| 30 | `generate_descriptions` | `generate_descriptions` | `POST /api/projects/{name}/descriptions/generate` (added in T62) | **true** (`generate_` pattern) |
| 31 | `apply_mix_plan` | `apply_mix_plan` | `POST /api/projects/{name}/audio-clips/apply-mix-plan` (added in T62) | **true** (batch mutation) |
| 32 | `analyze_master_bus` | `analyze_master_bus` | `POST /api/projects/{name}/master-bus/analyze` (audit: does this route exist post-T62, or is `mix-render-upload` the entry?) | false |

Any "audit" marker above → during this task, look at the tool's `_exec_*` body and confirm which FastAPI route matches. If none matches, surface as a blocker (T61/T62 should have covered it).

### 3. Plugin tool (`isolate_vocals__run`) annotation

Plugin routes don't go through the `openapi_extra=` decorator. Instead, the plugin itself declares its tool metadata in its manifest or registration. Extend the plugin API to let plugins contribute `x-tool-description` + `x-destructive`:

```python
# plugin_host.py or plugin_manifest.py
class PluginToolSpec(TypedDict):
    operation_id: str
    tool_description: str
    destructive: bool
    # ...
```

At app build time, for each registered plugin tool, inject an OpenAPI path with the operation + `x-tool: True` + description. (Plugin routes are `include_in_schema=False` today; we need a path with schema for the codegen to see it.)

Alternative: expose plugin tools via a separate registry the codegen consults, bypassing OpenAPI for plugins. Decide during implementation; prefer the OpenAPI path for consistency.

### 4. Validation

Run `python scripts/gen_chat_tools.py --spec <live_openapi.json> --out /tmp/test_generated.py`. Inspect:
- Exactly 32 tools in `TOOLS`.
- Each of the 32 names present.
- Destructive flag matches the table.
- No `ToolSpecError` raised.

### 5. Tests to Pass

- `chat_tool_operation_ids_match` — spec crawl confirms every chat-tool name has a matching operationId.
- `32_legacy_tools_preserved` — codegen produces `len(TOOLS) == 32`; every legacy name present.
- `legacy_schemas_preserved` — for each of the 32, compare the generated `input_schema` against `tests/fixtures/legacy_tool_schemas.json`: required fields match, property types match, enums match, defaults match. (See T68 for the full parity test — this is an early-integration check.)
- `destructive_flag_captured` — for each destructive tool in the table, assert `DESTRUCTIVE_TOOLS` contains it.
- `non_destructive_default` — for each non-destructive tool, assert NOT in `DESTRUCTIVE_TOOLS`.

---

## Verification

- [ ] `tests/fixtures/legacy_tool_schemas.json` captured BEFORE any chat.py changes
- [ ] All 32 chat tools have FastAPI routes with matching operationIds
- [ ] All 32 routes have `openapi_extra={"x-tool": True, "x-tool-description": ..., "x-destructive": ...}`
- [ ] Plugin tool (`isolate_vocals__run`) is discoverable by the codegen (via whichever registry approach was taken)
- [ ] `python scripts/gen_chat_tools.py` against live openapi.json emits 32 tools
- [ ] Destructive table in this task matches what the codegen emits
- [ ] Descriptions are verbatim from pre-migration `chat.py`; any divergence is flagged in PR review

---

## Tests Covered

`chat-tool-operation-ids-match`, `32-legacy-tools-preserved`, (partial) `legacy-schemas-preserved`, `destructive-flag-captured`, `non-destructive-default`.

---

## Notes

- **Description prose tuning is a follow-up.** Ports are verbatim. The goal is semantic parity first; style polish later. Don't touch more than one concern per PR.
- Plugin annotation is the fiddly part. If it gets too complex, scope down: just cover `isolate_vocals__run` via the OpenAPI path approach and defer a general plugin-tool registry to a future task.
- If any chat tool has no matching FastAPI route after T61/T62/T64 — stop. The route should have been added there. Reopen the earlier task rather than bodging a route here.
- `restore_checkpoint` being destructive is a judgement call. It's in the `_DESTRUCTIVE_TOOL_PATTERNS` list (explicitly named), so preserve that classification.
- After this task, running `chat.py::TOOLS` still works — T68 is where chat.py switches to the generated module.
