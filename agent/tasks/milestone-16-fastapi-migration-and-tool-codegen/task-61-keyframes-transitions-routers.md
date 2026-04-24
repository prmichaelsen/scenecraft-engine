# Task 61: Keyframes + transitions routers

**Milestone**: [M16 — FastAPI Migration + Tool Codegen](../../milestones/milestone-16-fastapi-migration-and-tool-codegen.md)
**Spec**: [`local.fastapi-migration`](../../specs/local.fastapi-migration.md) — R4, R6–R8, R18, R47
**Estimated Time**: 6–8 hours
**Dependencies**: T57, T58, T59
**Status**: Not Started

---

## Objective

Port the keyframe and transition mutation routes — the structural-mutation hot path. These are the routes that exercise the T59 per-project lock and the post-mutation timeline validator. Roughly 40 routes across two routers.

---

## TDD Plan

Capture legacy parity fixtures for each route. Write the parity tests (they fail — no routes). Port routes incrementally, starting with the structural ones (so T59's `project_lock` dependency is exercised first). Verify the structural-lock tests from T59 still pass after these routes are real (not just the test-harness routes). Remove T59's `/api/test-harness/` routes from the production app once the real routes prove the pattern.

---

## Steps

### 1. Capture parity fixtures

Extend `scripts/capture_parity_fixtures.py` to cover the routes listed below. Fixtures land in `tests/fixtures/parity/keyframes_*.json` and `.../transitions_*.json`.

### 2. Pydantic models

Create `src/scenecraft/api/models/keyframes.py` and `.../transitions.py`. Every model uses `extra="ignore"`. Cover:

**Keyframes**:
- `SelectKeyframesBody`, `SelectSlotKeyframesBody`, `UpdateTimestampBody`, `AddKeyframeBody`, `DuplicateKeyframeBody`, `DeleteKeyframeBody`, `BatchDeleteKeyframesBody`, `RestoreKeyframeBody`, `BatchSetBaseImageBody`, `SetBaseImageBody`, `UnlinkKeyframeBody`, `GenerateKeyframeVariationsBody`, `EscalateKeyframeBody`, `UpdateKeyframeLabelBody`, `UpdateKeyframeStyleBody`, `AssignKeyframeImageBody`, `UpdateKeyframeBody`, `SuggestKeyframePromptsBody`, `EnhanceKeyframePromptBody`, `GenerateKeyframeCandidatesBody`, `GenerateSlotKeyframeCandidatesBody`, `PasteGroupBody`, `InsertPoolItemBody`, `ExtendVideoBody` (if not already in projects), `UpdatePromptBody`.

**Transitions**:
- `SelectTransitionsBody`, `UpdateTransitionTrimBody`, `ClipTrimEdgeBody`, `MoveTransitionsBody`, `DeleteTransitionBody`, `RestoreTransitionBody`, `UpdateTransitionActionBody`, `UpdateTransitionRemapBody`, `GenerateTransitionActionBody`, `EnhanceTransitionActionBody`, `UpdateTransitionStyleBody`, `UpdateTransitionLabelBody`, `UpdateTransitionBody`, `CopyTransitionStyleBody`, `DuplicateTransitionVideoBody`, `SplitTransitionBody`, `LinkAudioBody`, `GenerateTransitionCandidatesBody`, `TransitionEffectAddBody`, `TransitionEffectUpdateBody`, `TransitionEffectDeleteBody`.

### 3. Routers

#### `routers/keyframes.py`

Apply `dependencies=[Depends(project_lock)]` to the **structural** subset (per T59's `STRUCTURAL_ROUTES` set) AND to handlers that were previously inside the `_use_lock` branch in `api_server.py::do_POST`.

- `POST /api/projects/{name}/select-keyframes` → `select_keyframes`
- `POST /api/projects/{name}/select-slot-keyframes` → `select_slot_keyframes`
- `POST /api/projects/{name}/update-timestamp` → `update_timestamp`
- `POST /api/projects/{name}/update-prompt` → `update_prompt`
- `POST /api/projects/{name}/add-keyframe` → `add_keyframe` 🔒
- `POST /api/projects/{name}/duplicate-keyframe` → `duplicate_keyframe` 🔒
- `POST /api/projects/{name}/paste-group` → `paste_group` 🔒
- `POST /api/projects/{name}/delete-keyframe` → `delete_keyframe` 🔒
- `POST /api/projects/{name}/batch-delete-keyframes` → `batch_delete_keyframes` 🔒
- `POST /api/projects/{name}/restore-keyframe` → `restore_keyframe` 🔒
- `POST /api/projects/{name}/batch-set-base-image` → `batch_set_base_image`
- `POST /api/projects/{name}/set-base-image` → `set_base_image`
- `POST /api/projects/{name}/unlink-keyframe` → `unlink_keyframe`
- `POST /api/projects/{name}/generate-keyframe-variations` → `generate_keyframe_variations`
- `POST /api/projects/{name}/escalate-keyframe` → `escalate_keyframe`
- `POST /api/projects/{name}/update-keyframe-label` → `update_keyframe_label`
- `POST /api/projects/{name}/update-keyframe-style` → `update_keyframe_style`
- `POST /api/projects/{name}/assign-keyframe-image` → `assign_keyframe_image`
- `POST /api/projects/{name}/generate-keyframe-candidates` → `generate_keyframe_candidates`
- `POST /api/projects/{name}/generate-slot-keyframe-candidates` → `generate_slot_keyframe_candidates`
- `POST /api/projects/{name}/insert-pool-item` → `insert_pool_item` 🔒
- `POST /api/projects/{name}/update-keyframe` → `update_keyframe`
- `POST /api/projects/{name}/suggest-keyframe-prompts` → `suggest_keyframe_prompts`
- `POST /api/projects/{name}/enhance-keyframe-prompt` → `enhance_keyframe_prompt`

#### `routers/transitions.py`

- `POST /api/projects/{name}/select-transitions` → `select_transitions`
- `POST /api/projects/{name}/update-transition-trim` → `update_transition_trim`
- `POST /api/projects/{name}/clip-trim-edge` → `clip_trim_edge`
- `POST /api/projects/{name}/move-transitions` → `move_transitions`
- `POST /api/projects/{name}/delete-transition` → `delete_transition` 🔒
- `POST /api/projects/{name}/restore-transition` → `restore_transition` 🔒
- `POST /api/projects/{name}/split-transition` → `split_transition` 🔒
- `POST /api/projects/{name}/update-transition-action` → `update_transition_action`
- `POST /api/projects/{name}/update-transition-remap` → `update_transition_remap`
- `POST /api/projects/{name}/generate-transition-action` → `generate_transition_action`
- `POST /api/projects/{name}/enhance-transition-action` → `enhance_transition_action`
- `POST /api/projects/{name}/update-transition-style` → `update_transition_style`
- `POST /api/projects/{name}/update-transition-label` → `update_transition_label`
- `POST /api/projects/{name}/update-transition` → `update_transition`
- `POST /api/projects/{name}/copy-transition-style` → `copy_transition_style`
- `POST /api/projects/{name}/duplicate-transition-video` → `duplicate_transition_video`
- `POST /api/projects/{name}/transitions/{tr_id}/link-audio` → `link_transition_audio`
- `POST /api/projects/{name}/generate-transition-candidates` → `generate_transition_candidates`
- `POST /api/projects/{name}/transition-effects/add` → `add_transition_effect`
- `POST /api/projects/{name}/transition-effects/update` → `update_transition_effect`
- `POST /api/projects/{name}/transition-effects/delete` → `delete_transition_effect`

🔒 = `dependencies=[Depends(project_lock)]`

### 4. OperationId alignment for chat tools

These operation IDs are **load-bearing** for T67 (tool annotation). The chat tool names map as follows — MUST match:

| Chat tool name | operationId |
|---|---|
| `update_keyframe_prompt` | `update_prompt` — rename one to match; prefer keeping `update_keyframe_prompt` since that's the tool name (rename the operationId). Actually the route is `/api/projects/{name}/update-prompt` which applies to keyframes. Rename operationId → `update_keyframe_prompt`. |
| `update_keyframe_timestamp` | `update_timestamp` → rename to `update_keyframe_timestamp` (route is keyframe-scoped). |
| `update_curve` | not in this task (transitions curve — handled by `update_transition` or similar; audit and align) |
| `update_transform_curve` | not in this task (transitions transform curve — audit) |
| `delete_keyframe` | `delete_keyframe` ✓ |
| `delete_transition` | `delete_transition` ✓ |
| `batch_delete_keyframes` | `batch_delete_keyframes` ✓ |
| `batch_delete_transitions` | NOT an existing REST route! Today's `chat.py::_exec_batch_delete_transitions` calls `db.*` directly with no HTTP equivalent. **Add** `POST /api/projects/{name}/batch-delete-transitions` → `batch_delete_transitions` in this task. 🔒 |
| `add_keyframe` | `add_keyframe` ✓ |
| `update_keyframe` | `update_keyframe` ✓ |
| `update_transition` | `update_transition` ✓ |
| `split_transition` | `split_transition` ✓ |
| `assign_keyframe_image` | `assign_keyframe_image` ✓ |
| `generate_keyframe_candidates` | `generate_keyframe_candidates` ✓ |
| `generate_transition_candidates` | `generate_transition_candidates` ✓ |

Flag any other chat tool in `chat.py::TOOLS` that doesn't have a matching route and **add the route** (thin wrapper over the existing `_exec_*`'s db call) OR defer to a follow-up task. Record the list in the task PR.

### 5. Tests to Pass

Extend the parity crawl tests. Specifically name:

- `get_route_parity` (keyframes/transitions slice, via the parity fixtures)
- `post_route_parity` (keyframes/transitions slice)
- `delete_route_parity` — not relevant here; keyframes/transitions use POST-with-delete-intent. Skip.
- `structural_lock_serializes` — already passing from T59 via test harness; verify it still passes after switching to real `POST /add-keyframe` on a live project.
- `structural_lock_is_per_project` — same.

---

## Verification

- [ ] All keyframe routes registered with correct operationIds (chat-tool-aligned where applicable)
- [ ] All transition routes registered with correct operationIds
- [ ] Structural routes have `Depends(project_lock)`
- [ ] `batch_delete_transitions` route **added** (was previously chat-only)
- [ ] Any chat-tool-to-route gaps documented in PR description
- [ ] Parity fixtures pass for every ported route
- [ ] T59's structural lock tests still pass with real routes (test-harness routes can be removed now)
- [ ] No business logic rewritten — all handlers are thin wrappers
- [ ] Timeline validator runs after every structural mutation (verified by a smoke test here, since T59 already proved the mechanism)

---

## Tests Covered

`get-route-parity` (keyframes/transitions slice), `post-route-parity` (keyframes/transitions slice), `structural-lock-serializes` (re-run with real routes), `structural-lock-is-per-project` (re-run).

---

## Notes

- `GET /api/projects/{name}/keyframes` lives in T60's `projects` router (read).
- `GET /api/projects/{name}/unselected-candidates`, `GET .../video-candidates`: these are candidate reads — they land in T63 with the rendering/pool/candidates cluster.
- Route-level structural audit: cross-check `STRUCTURAL_ROUTES` from T59 against the route set here. Any addition triggers a T59 update.
- Some "candidate generation" routes (`generate-keyframe-variations`, etc.) call into LLM providers and can take minutes. Verify `TestClient` timeouts are generous enough, or mock the provider.
