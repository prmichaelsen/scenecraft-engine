# Task 62: Audio routers

**Milestone**: [M16 — FastAPI Migration + Tool Codegen](../../milestones/milestone-16-fastapi-migration-and-tool-codegen.md)
**Spec**: [`local.fastapi-migration`](../../specs/local.fastapi-migration.md) — R4, R6–R8, R47, R51
**Estimated Time**: 8 hours
**Dependencies**: T57, T58, T59
**Status**: Not Started

---

## Objective

Port the audio surface: tracks, clips, effect chains (M13 plumbing), effect-curve automation, send buses, track sends, master-bus effects (M15 plumbing), frequency labels, mix-render upload (M15), audio intelligence stubs, audio isolations. Roughly 40 routes — the highest-density cluster. Several of these map to existing chat tools and MUST have their `operationId`s aligned.

---

## TDD Plan

Capture parity fixtures for every route. Write table-driven parity tests. Port routers in dependency order: tracks → clips → effects → curves → send-buses → master-bus → frequency-labels → mix-render → isolations/intelligence.

---

## Steps

### 1. Pydantic models (`src/scenecraft/api/models/audio.py`)

- **Tracks**: `AddAudioTrackBody`, `UpdateAudioTrackBody`, `DeleteAudioTrackBody`, `ReorderAudioTracksBody`
- **Clips**: `AddAudioClipBody`, `AddAudioClipFromPoolBody`, `UpdateAudioClipBody`, `DeleteAudioClipBody`, `AudioClipsBatchOpsBody`, `AudioClipAlignDetectBody`
- **Effects (M13)**: `TrackEffectCreateBody`, `TrackEffectUpdateBody`, `EffectCurveCreateBody`, `EffectCurveUpdateBody`, `EffectCurveBatchUpdateBody`, `SendBusCreateBody`, `SendBusUpdateBody`, `TrackSendUpsertBody`, `FrequencyLabelCreateBody`
- **Master bus (M15)**: `AddMasterBusEffectBody`, `RemoveMasterBusEffectBody`
- **Mix render (M15)**: `MixRenderUploadBody` (multipart + JSON meta)
- **Intelligence / rules**: `UpdateRulesBody` (stub), `ReapplyRulesBody` (stub)

Every model `extra="ignore"`.

### 2. Routers

#### `routers/audio_tracks.py`

- `GET /api/projects/{name}/tracks` → `list_tracks`
- `GET /api/projects/{name}/audio-tracks` → `list_audio_tracks`
- `POST /api/projects/{name}/tracks/add` → `add_track`
- `POST /api/projects/{name}/tracks/update` → `update_track`
- `POST /api/projects/{name}/tracks/delete` → `delete_track`
- `POST /api/projects/{name}/tracks/reorder` → `reorder_tracks`
- `POST /api/projects/{name}/audio-tracks/add` → **`operation_id="add_audio_track"`** 🔧 (chat tool)
- `POST /api/projects/{name}/audio-tracks/update` → `update_audio_track`
- `POST /api/projects/{name}/audio-tracks/delete` → `delete_audio_track`
- `POST /api/projects/{name}/audio-tracks/reorder` → `reorder_audio_tracks`

#### `routers/audio_clips.py`

- `GET /api/projects/{name}/audio-clips` → `list_audio_clips`
- `GET /api/projects/{name}/audio-clips/{id}/peaks` → `get_audio_clip_peaks`
- `POST /api/projects/{name}/audio-clips/add` → `add_audio_clip_core`
- `POST /api/projects/{name}/audio-clips/add-from-pool` → **`operation_id="add_audio_clip"`** 🔧 (chat tool; matches existing `_exec_add_audio_clip` which adds-from-pool)
- `POST /api/projects/{name}/audio-clips/update` → `update_audio_clip`
- `POST /api/projects/{name}/audio-clips/delete` → `delete_audio_clip`
- `POST /api/projects/{name}/audio-clips/batch-ops` → **`operation_id="apply_mix_plan"`** 🔧 (chat tool alignment — `apply_mix_plan` today calls `batch_ops` internally)
- `POST /api/projects/{name}/audio-clips/align-detect` → `align_audio_clips`

#### `routers/effect_curves.py` (M13)

- `POST /api/projects/{name}/track-effects` → **`operation_id="add_audio_effect"`** 🔧 (chat tool)
- `GET /api/projects/{name}/track-effects` → `list_track_effects`
- `POST /api/projects/{name}/track-effects/{effect_id}` → `update_track_effect`
- `DELETE /api/projects/{name}/track-effects/{effect_id}` → `delete_track_effect`
- `POST /api/projects/{name}/effect-curves` → `create_effect_curve`
- `POST /api/projects/{name}/effect-curves/batch` → **`operation_id="update_effect_param_curve"`** 🔧 (chat tool; matches `_exec_update_effect_param_curve` which does a batch upsert of curve points)
- `POST /api/projects/{name}/effect-curves/{curve_id}` → `update_effect_curve`
- `DELETE /api/projects/{name}/effect-curves/{curve_id}` → `delete_effect_curve`
- `POST /api/projects/{name}/send-buses` → `create_send_bus`
- `GET /api/projects/{name}/send-buses` → `list_send_buses`
- `POST /api/projects/{name}/send-buses/{bus_id}` → `update_send_bus`
- `DELETE /api/projects/{name}/send-buses/{bus_id}` → `delete_send_bus`
- `POST /api/projects/{name}/track-sends` → `upsert_track_send`
- `POST /api/projects/{name}/frequency-labels` → `create_frequency_label`

#### `routers/master_bus.py` (M15)

- `GET /api/projects/{name}/master-bus-effects` → `list_master_bus_effects`
- `POST /api/projects/{name}/master-bus-effects/add` → **`operation_id="add_master_bus_effect"`** 🔧 (chat tool)
- `POST /api/projects/{name}/master-bus-effects/remove` → **`operation_id="remove_master_bus_effect"`** 🔧 (chat tool)

#### `routers/mix_render.py` (M15)

- `POST /api/projects/{name}/mix-render-upload` → `mix_render_upload` (multipart — uses `python-multipart`)
- (chat tool `analyze_master_bus` maps to this endpoint + a downstream WS round-trip; tag in T67)

#### `routers/audio_intelligence.py` (stubs)

- `GET /api/projects/{name}/audio-intelligence` → `get_audio_intelligence` (returns empty per legacy stub)
- `POST /api/projects/{name}/update-rules` → `update_rules_stub`
- `POST /api/projects/{name}/reapply-rules` → `reapply_rules_stub`
- `GET /api/projects/{name}/audio-isolations` → `list_audio_isolations`

#### Volume curve — chat tool alignment

`update_volume_curve` (chat tool) today calls `db.update_volume_curve` directly. The existing REST surface doesn't have an exact match — volume curves are persisted via audio-track updates today. Add `POST /api/projects/{name}/audio-tracks/{track_id}/volume-curve` → **`operation_id="update_volume_curve"`** 🔧 to give the chat tool a matching route. Document in PR.

#### `generate_dsp` / `generate_descriptions` — chat tools with no REST equivalent

These two chat tools (`generate_dsp`, `generate_descriptions`) perform LLM-backed analysis. They have no REST route today. For T67 annotation, they need an operationId. Add:

- `POST /api/projects/{name}/dsp/generate` → **`operation_id="generate_dsp"`** 🔧
- `POST /api/projects/{name}/descriptions/generate` → **`operation_id="generate_descriptions"`** 🔧

Each is a thin wrapper over the existing `_exec_generate_dsp` / `_exec_generate_descriptions` logic (relocate the body out of `chat.py::_exec_*` and into a service module, or just call the function).

🔧 = load-bearing operationId alignment for T67.

### 3. Tests to Pass

- `get_route_parity` (audio slice) — via parity fixtures
- `post_route_parity` (audio slice)
- `delete_route_parity` (audio slice)
- `delete_idempotent_parity` — specifically cover `DELETE /track-effects/{id}` with a non-existent id; expect 200 empty (M13 semantics per spec R6 / Behavior Table row 4)
- `multipart_upload_parity` — `mix-render-upload` accepting a small WAV via multipart; verify DB and file-side effects identical to legacy

### 4. Verification of chat-tool-to-route map

At the end of this task, the following chat tools should each have a matching FastAPI operation:

- `add_audio_track`, `add_audio_clip`, `update_volume_curve`, `add_audio_effect`, `update_effect_param_curve`, `add_master_bus_effect`, `remove_master_bus_effect`, `apply_mix_plan`, `generate_dsp`, `generate_descriptions`.

`analyze_master_bus` maps to the `mix_render_upload` flow + downstream async analysis; tag via `x-tool-name` override in T67.

---

## Verification

- [ ] All ~40 audio routes registered with correct operationIds
- [ ] Chat-tool-aligned operationIds match the table above
- [ ] New routes added: `batch-delete-transitions` (T61), `volume-curve`, `dsp/generate`, `descriptions/generate`
- [ ] Multipart `mix-render-upload` works end-to-end
- [ ] Idempotent DELETEs return 200 empty (M13 spec compliance)
- [ ] Parity fixtures pass for every route
- [ ] No business logic rewritten — handlers are thin wrappers over `db.*`/`audio_intelligence.*`
- [ ] Chat-tool-to-route map documented in PR description

---

## Tests Covered

`get-route-parity` (audio slice), `post-route-parity` (audio slice), `delete-route-parity` (audio slice), `delete-idempotent-parity`, `multipart-upload-parity`.

---

## Notes

- The chat-tool-to-route alignment is the single most error-prone part of this task. Audit twice. T67 depends on these operationIds being exactly the chat tool names.
- Some M13/M15 routes may already have their own tests in `tests/test_effect_curves_api.py`, `tests/test_master_bus_effects.py`, etc. After migration, those tests must still pass unchanged (verified in T65's suite-green criterion).
- `apply_mix_plan` is destructive-ish (batches many ops) — tag `x-destructive: true` in T67.
- Consider whether `batch-ops` should actually be a separate operation from `apply_mix_plan`. Today's `_exec_apply_mix_plan` wraps multiple calls; the REST `/audio-clips/batch-ops` route may not be the exact shape. If it's not, add a NEW route `/audio-clips/apply-mix-plan` → `apply_mix_plan` and leave `/audio-clips/batch-ops` → `audio_clips_batch_ops` as a separate op. Decide during implementation; document in PR.
