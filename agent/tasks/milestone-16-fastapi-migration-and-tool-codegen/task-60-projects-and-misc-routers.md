# Task 60: Projects + misc routers

**Milestone**: [M16 — FastAPI Migration + Tool Codegen](../../milestones/milestone-16-fastapi-migration-and-tool-codegen.md)
**Spec**: [`local.fastapi-migration`](../../specs/local.fastapi-migration.md) — R4, R6–R8, R10, R43, R57, R58
**Estimated Time**: 6–8 hours
**Dependencies**: T57, T58
**Status**: Not Started

---

## Objective

Port the **projects-and-meta** half of `api_server.py`: project CRUD, browse/ls, workspace views, narrative, settings, ingredients, watched folders, branches/checkout, markers, prompt-roster, bench, section-settings, deprecated version/git shims, config, and the save-as-still/import/update-meta/extend-video family. Roughly 40 routes.

---

## TDD Plan

Capture the existing response bodies and status codes for each of the 40 routes against the legacy server into `tests/fixtures/parity/projects_<operationid>.json` (a one-off script that hits the legacy server and saves the JSON). Write a table-driven parity test that replays each captured request against the FastAPI app and diffs the response. The parity test fails initially (no routes). Port one router at a time until each fixture passes.

---

## Steps

### 1. Parity fixture capture

Write `scripts/capture_parity_fixtures.py` (one-off, don't commit to long-term tooling):
- For each of the 40 routes in this task, hit the **legacy** server running locally with known inputs.
- Save `{"request": {...}, "response": {"status": int, "headers": {...}, "body": <json|base64>}}` to `tests/fixtures/parity/projects_<op>.json`.
- Commit fixtures to git (they're small — pure JSON).

### 2. Pydantic models

Create `src/scenecraft/api/models/projects.py` with request/response models for:
- `CreateProjectBody` (name, fps, resolution, motionPrompt, defaultTransitionPrompt)
- `UpdateMetaBody`, `UpdatePromptBody`, `UpdateTimestampBody`, `UpdateTransitionTrimBody`, `ClipTrimEdgeBody`, `MoveTransitionsBody`
- `WorkspaceViewBody`, `WatchFolderBody`, `NarrativeBody`, `BranchCreateBody`, `CheckoutBody`
- `SectionSettingsBody`, `SettingsBody`, `IngredientsPromoteBody`/`RemoveBody`/`UpdateBody`
- `BenchCaptureBody`, `BenchAddBody`, `BenchRemoveBody`
- `MarkerAddBody`, `MarkerUpdateBody`, `MarkerRemoveBody`
- `PromptRosterAddBody`, `PromptRosterUpdateBody`, `PromptRosterRemoveBody`
- `SaveAsStillBody`, `ImportBody`

Configure `model_config = ConfigDict(extra="ignore")` on all (matches legacy permissiveness per R10).

### 3. Routers

Create routers; include in `app.py`:

#### `routers/projects.py`
- `GET /api/projects` → `list_projects`
- `POST /api/projects/create` → `create_project`
- `GET /api/browse` → `browse_projects`
- `GET /api/projects/{name}/ls` → `list_project_files`
- `GET /api/projects/{name}/bin` → `get_project_bin`
- `GET /api/projects/{name}/keyframes` → `get_keyframes` (yes this goes here since it's a project read, not a mutation)
- `GET /api/projects/{name}/beats` → `get_project_beats`
- `GET /api/projects/{name}/narrative` → `get_narrative`
- `POST /api/projects/{name}/narrative` → `update_narrative`
- `POST /api/projects/{name}/update-meta` → `update_meta`
- `POST /api/projects/{name}/import` → `import_project`
- `POST /api/projects/{name}/save-as-still` → `save_as_still`
- `POST /api/projects/{name}/extend-video` → `extend_video`
- `GET /api/projects/{name}/watched-folders` → `get_watched_folders`
- `POST /api/projects/{name}/watch-folder` → `watch_folder`
- `POST /api/projects/{name}/unwatch-folder` → `unwatch_folder`
- `GET /api/projects/{name}/branches` → `list_branches`
- `POST /api/projects/{name}/branches` → `create_branch`
- `POST /api/projects/{name}/branches/delete` → `delete_branch`
- `POST /api/projects/{name}/checkout` → `checkout_branch`
- `GET /api/projects/{name}/version/history` → `version_history_deprecated`
- `GET /api/projects/{name}/version/diff` → `version_diff_deprecated`
- `POST /api/projects/{name}/version/commit` → `version_commit_noop`
- `POST /api/projects/{name}/version/checkout` → `version_checkout_noop`
- `POST /api/projects/{name}/version/branch` → `version_branch_noop`
- `POST /api/projects/{name}/version/delete-branch` → `version_delete_branch_noop`

#### `routers/workspace.py`
- `GET /api/projects/{name}/workspace-views` → `list_workspace_views`
- `GET /api/projects/{name}/workspace-views/{view_name}` → `get_workspace_view`
- `POST /api/projects/{name}/workspace-views/{view_name}` → `upsert_workspace_view`
- `POST /api/projects/{name}/workspace-views/{view_name}/delete` → `delete_workspace_view`

#### `routers/settings.py`
- `GET /api/projects/{name}/settings` → `get_settings`
- `POST /api/projects/{name}/settings` → `update_settings`
- `GET /api/projects/{name}/section-settings` → `get_section_settings`
- `POST /api/projects/{name}/section-settings` → `update_section_settings`

#### `routers/ingredients.py`
- `GET /api/projects/{name}/ingredients` → `list_ingredients`
- `POST /api/projects/{name}/ingredients/promote` → `promote_ingredient`
- `POST /api/projects/{name}/ingredients/remove` → `remove_ingredient`
- `POST /api/projects/{name}/ingredients/update` → `update_ingredient`

#### `routers/bench.py`
- `GET /api/projects/{name}/bench` → `get_bench`
- `POST /api/projects/{name}/bench/capture` → `bench_capture`
- `POST /api/projects/{name}/bench/upload` → `bench_upload`
- `POST /api/projects/{name}/bench/add` → `bench_add`
- `POST /api/projects/{name}/bench/remove` → `bench_remove`

#### `routers/markers.py`
- `GET /api/projects/{name}/markers` → `list_markers`
- `POST /api/projects/{name}/markers/add` → `add_marker`
- `POST /api/projects/{name}/markers/update` → `update_marker`
- `POST /api/projects/{name}/markers/remove` → `remove_marker`

#### `routers/prompt_roster.py`
- `GET /api/projects/{name}/prompt-roster` → `get_prompt_roster`
- `POST /api/projects/{name}/prompt-roster/add` → `add_prompt_roster_entry`
- `POST /api/projects/{name}/prompt-roster/update` → `update_prompt_roster_entry`
- `POST /api/projects/{name}/prompt-roster/remove` → `remove_prompt_roster_entry`

#### `routers/config.py`
- `GET /api/config` → `get_config` (move the T57 spike here)
- `POST /api/config` → `update_config`

### 4. Thin handlers

Every handler is a one-liner calling into existing `scenecraft.db.*`, `scenecraft.config.*`, etc. Do NOT reimplement any logic — copy the calls from `api_server.py`'s corresponding `_handle_*` methods verbatim.

### 5. Tests to Pass

- `get_route_parity` (projects slice) — table-driven: for each captured fixture in `tests/fixtures/parity/projects_*.json` whose method is GET, replay and assert status + body match.
- `post_route_parity` (meta/config slice) — same, for POST fixtures.
- `deprecated_noops_preserved` — hit each of the 4 deprecated `/version/*` routes; verify response body matches legacy noop shape exactly.
- `extra_fields_ignored` — POST `/api/projects/create` with `{"name": "p1", "unknown_field": 42}`; expect 200, project created, no error.
- `no_body_post_works` — POST `/api/projects/{name}/narrative` with empty body if legacy accepts that, or with the minimum body required. Table-drive.

---

## Verification

- [ ] All 40 routes are registered with `operationId` set
- [ ] Parity fixtures exist for every route in this task
- [ ] Parity test table runs clean (one row per route, green)
- [ ] Deprecated `/version/*` responses match legacy noop shape byte-for-byte
- [ ] Extra-fields permissiveness confirmed for at least 3 representative routes
- [ ] No business logic was rewritten — every handler is a thin wrapper over existing `scenecraft.db.*`/`scenecraft.config.*` calls
- [ ] Legacy server still runs; both serve in parallel until T65

---

## Tests Covered

`get-route-parity` (projects slice), `post-route-parity` (meta/config slice), `deprecated-noops-preserved`, `extra-fields-ignored`, `no-body-post-works`.

---

## Notes

- `GET /api/projects/{name}/keyframes` lands here (read) but the mutation routes for keyframes are T61. Don't double-port.
- `POST /api/projects/{name}/branches/delete` uses the POST-with-body-for-delete pattern legacy uses. Preserve that shape; do NOT convert to `DELETE /branches/{id}`.
- Stale `/version/*` routes are **deprecated noops**, not deletions. They return the legacy success shape without doing anything. Tests pin that contract.
- If a fixture capture reveals a route that legacy handles but isn't in the spec's list, stop and add it to the spec's Requirements rather than porting silently.
