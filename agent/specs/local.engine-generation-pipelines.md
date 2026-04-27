# Spec: Engine Generation Pipelines (Keyframe + Transition Candidates)

> **🤖 Agent Directive**: This is a specification document. Implementers MUST treat the Requirements, Behavior Table, and Tests sections as the authoritative contract. Do NOT silently resolve items marked `undefined` — surface them to the user.

**Namespace**: local
**Version**: 1.0.0
**Created**: 2026-04-27
**Last Updated**: 2026-04-27
**Status**: Draft (retroactive spec of existing code; contains known-issue requirements)

---

## Purpose

Define the **shared contract** for generating keyframe image candidates (via Imagen) and transition video candidates (via Veo / Runway) in the scenecraft engine — independent of which entry point invokes it (chat tool vs. CLI).

## Source

`--from-draft` (prompt-supplied scope). Primary source files inspected:
- `src/scenecraft/chat_generation.py` — chat-tool-initiated, thread + `JobManager`, pool-registered outputs
- `src/scenecraft/render/narrative.py` — CLI-initiated, synchronous, work-dir files + grid contact sheets (`generate_keyframe_candidates`, `generate_transition_candidates`)
- `src/scenecraft/render/google_video.py` — `GoogleVideoClient` (Imagen + Veo) and `RunwayVideoClient`, used by both paths

Prep context: `acp.spec.md`; `agent/reports/audit-2-architectural-deep-dive.md` §1D (Render Pipeline units 1–4), §3 leaks #1 (provider spend untracked) and #6 (dual generation paths).

---

## Two Codepaths, One Contract

> **IMPORTANT — architectural callout.** The engine currently has **two parallel implementations** of keyframe/transition candidate generation:
>
> 1. **New (chat-tool) path** — `chat_generation.start_keyframe_generation` / `start_transition_generation`. Asynchronous; spawns a daemon thread; reports progress through `JobManager`; registers outputs in the pool DB (`pool_segments`, `tr_candidates`) and directly mutates the DB (`update_keyframe`).
> 2. **Legacy (CLI) path** — `narrative.generate_keyframe_candidates` / `generate_transition_candidates`. Synchronous; writes variants under a work-directory tree (`keyframe_candidates/candidates/section_<id>/v*.png`, `transition_candidates/<tr>/slot_<i>/v*.mp4`); builds grid contact sheets via `make_contact_sheet`; returns via the narrative YAML file.
>
> **Both paths call `GoogleVideoClient` / `RunwayVideoClient` directly, neither routes through `plugin_api.providers`, neither records spend, and both duplicate retry/backoff logic.**
>
> This spec defines the **shared contract**: the observable behavior both paths MUST honor. Where the paths legitimately differ (output sink, progress reporting), the spec calls it out. Where they diverge by accident (retry counts, backoff multipliers, error handling for mid-batch `PromptRejectedError`), the divergence is captured as an **Open Question** or a **known-issue requirement**.

---

## Scope

### In Scope
- Contract for **keyframe candidate generation**: given a keyframe record and its selected source image, produce N styled image variants using the configured image backend.
- Contract for **transition candidate generation**: given a transition record (from_kf → to_kf, N slots, intermediate slot keyframes) and its selected boundary images, produce V × S video clips.
- Multi-slot **chaining invariant**: slot i's start = `from` if i=0 else `<tr_id>_slot_<i-1>.png`; slot i's end = `to` if i = n_slots-1 else `<tr_id>_slot_<i>.png`.
- Pool registration (chat path only): pre-generated UUID → single `INSERT` into `pool_segments` under `_retry_on_locked`.
- Retry/backoff for transient failures.
- Progress / job-state reporting (chat path) and synchronous completion (CLI path).

### Out of Scope
- **Render pipeline composition** — schedule building, compositor, final assembly (separate spec).
- **Provider implementations** — Imagen/Veo/Runway SDK adapters, auth, `_retry_video_generation` internals, 429 handling (separate spec; treat providers as opaque dependencies here).
- **Candidate selection** (`update_keyframe(..., selected=...)`, `apply_transition_selection`) — separate spec.
- **Slot-keyframe extraction** from previously generated transition segments (`resolve_existing_boundary_frames`) — separate spec.
- **Contact-sheet rendering** (`make_contact_sheet`) — treat as a downstream artifact the CLI path emits; its internals are out of scope.
- **Provider-surface unification** — tracked as R10 (DEFERRED).

---

## Requirements

> Requirements apply to **both paths** unless prefixed `[CHAT]` or `[CLI]`.

### Inputs and Preconditions

- **R1**. Keyframe generation MUST refuse to start when the keyframe does not exist in the project DB (chat) or narrative YAML (CLI); it MUST return a structured error (`{error: "keyframe not found: <id>"}`) without spawning a worker or creating files.
- **R2**. Keyframe generation MUST refuse to start when **no source image** is present on disk for the keyframe; structured error, no worker, no files.
- **R3**. Keyframe generation MUST refuse to start when **no prompt** is set on the keyframe and no `prompt_override` was supplied; structured error, no worker, no files.
- **R4**. Transition generation MUST refuse to start when the transition does not exist; structured error.
- **R5**. Transition generation MUST refuse to start when the **start boundary image** (`selected_keyframes/<from>.png`) or **end boundary image** (`selected_keyframes/<to>.png`) is not present on disk; structured error.
- **R6**. [CHAT] Transition generation MUST refuse an explicit `slot_index` that is out of range `[0, n_slots)`; structured error naming the range.
- **R7**. `count` MUST be clamped to `[1, 8]` for keyframe generation and `[1, 4]` for transition generation.

### Generation Semantics

- **R8**. Variant numbering MUST be **append-only**: new variants take indices `existing_count + 1 … existing_count + count` so prior variants are never overwritten. (Keyframe path.)
- **R9**. If an output file already exists at the target path, the worker MUST treat it as a cached success — no provider call, progress still advances.
- **R10**. **(DEFERRED — known issue)** Spend MUST be recorded for every successful image and video generation via `plugin_api.record_spend`. *Current code does not record spend for Imagen, Veo, or Runway; this requirement is flagged DEFERRED pending provider-surface unification. Tests in the Base Cases for spend recording are marked `SKIP-DEFERRED` and MUST remain in the suite.*
- **R11**. Per-attempt retry MUST be bounded: keyframe Imagen = 3 attempts with backoff `5 * tries` seconds; transition Veo = 3 attempts with backoff `10 * tries` seconds (chat path). The third failure MUST surface as a job/task failure rather than silent drop.
- **R12**. The chat transition path MUST use `RunwayVideoClient(model=<runway_model>)` when `meta.video_backend` starts with `runway/`; otherwise `GoogleVideoClient(vertex=True)`.
- **R13**. The chat keyframe path MUST read `meta.image_model` and pass it through as `image_model` to `client.stylize_image`; default `replicate/nano-banana-2`.

### Multi-Slot Chaining (Transition)

- **R14**. For a transition with `n_slots ≥ 1`:
  - slot 0's start image is `selected_keyframes/<from>.png`.
  - slot i's start image (i > 0) is `selected_slot_keyframes/<tr_id>_slot_<i-1>.png`.
  - slot `n_slots - 1`'s end image is `selected_keyframes/<to>.png`.
  - slot i's end image (i < n_slots - 1) is `selected_slot_keyframes/<tr_id>_slot_<i>.png`.
- **R15**. `slot_duration = min(meta.transition_max_seconds, tr.duration_seconds / n_slots)` when `duration_seconds > 0`, else `meta.transition_max_seconds`.
- **R16**. [CHAT] If an intermediate slot-keyframe file is absent at job start, the chat path MUST fall back to the boundary image (`start_img` or `end_img`) rather than aborting the slot — *this matches current behavior and is codified; see Open Questions OQ-2 for whether that is desirable.*
- **R17**. [CLI] If an intermediate slot-keyframe file is absent at job-list construction time, the CLI path MUST skip that slot with a log line and continue the rest of the batch — *matches current behavior.*

### Prompt Construction

- **R18**. Keyframe variant prompt: `<base_prompt>` for v1, `<base_prompt>, variation <v>` for v ≥ 2.
- **R19**. Transition prompt (chat): `"<action>. Camera and motion style: <motion_prompt>"` when `use_global_prompt` is true and a `motionPrompt`/`motion_prompt` meta exists; otherwise just `<action>`. `action` defaults to `"Smooth cinematic transition"` when absent.
- **R20**. Transition prompt (CLI): same pattern using `slot_actions[slot_idx]` when provided, falling back to `tr.action`.

### Output and Persistence

- **R21**. [CHAT] Keyframe outputs MUST be written to `keyframe_candidates/candidates/section_<kf_id>/v<n>.png`. After the batch, `update_keyframe(project_dir, kf_id, candidates=<sorted list of all v*.png rel paths>)` MUST be called.
- **R22**. [CHAT] Transition outputs MUST land at `pool/segments/<uuid>.mp4` in one shot (no post-rename). The UUID MUST be pre-generated before the provider call.
- **R23**. [CHAT] Pool registration MUST be a **single INSERT** into `pool_segments` (id, pool_path, kind='generated', created_by='chat_generation', duration_seconds, created_at) wrapped in `_retry_on_locked`. No UPDATE, no post-hoc rename.
- **R24**. [CHAT] After pool insert, `add_tr_candidate(project_dir, transition_id, slot, pool_segment_id, source='generated')` MUST be called to link the segment to the transition slot.
- **R25**. [CLI] Keyframe outputs MUST be written to `<work_dir>/keyframe_candidates/candidates/section_<kf_id>/v<n>.png`, and a grid contact sheet to `grid.png` in the same directory.
- **R26**. [CLI] Transition outputs MUST be written to `<work_dir>/transition_candidates/<tr_id>/slot_<i>/v<n>.mp4`.

### Concurrency and Job Management

- **R27**. [CHAT] Work MUST run on a **daemon thread** launched via `threading.Thread(..., daemon=True).start()`; the tool call MUST return immediately with `{job_id, keyframe_id|transition_id, count, ...}`.
- **R28**. [CHAT] A `JobManager` job MUST be created *before* the worker thread starts, with `total` equal to the number of provider calls to make (keyframe: `count`; transition: `count * len(slots_to_process)`).
- **R29**. [CHAT] Each successful provider call MUST call `job_manager.update_progress(job_id, completed, label)`; the final successful batch MUST call `job_manager.complete_job(job_id, <summary>)`; any exception escaping the worker MUST call `job_manager.fail_job(job_id, str(e))`.
- **R30**. [CHAT] Within a single job, variants MUST run in a `ThreadPoolExecutor` with `max_workers = count` (keyframe) or `min(count, 4)` (transition).
- **R31**. [CHAT] A keyframe-generation job MUST survive WebSocket disconnect of the chat client (the daemon thread is not tied to any WS connection).
- **R32**. [CLI] Work MUST run synchronously; control returns only after all batch jobs settle.

### Error Propagation

- **R33**. [CHAT] A provider exception that survives R11's retry budget MUST mark the whole job `failed` with the exception string.
- **R34**. [CLI] A `PromptRejectedError` from Veo MUST NOT abort the whole batch; the offending `tr_id` MUST be collected and logged at end-of-run, and other jobs continue.
- **R35**. **Intentional divergence**: R33 and R34 describe **different** error-handling policies between the two paths. This is a known divergence; harmonization is an Open Question (OQ-3).

### Negative Assertions (MUST NOT)

- **R36**. The chat keyframe worker MUST NOT overwrite a pre-existing `v<n>.png` file.
- **R37**. The chat transition worker MUST NOT create `pool_segments` rows until the provider reports success AND the `.mp4` is on disk.
- **R38**. Neither path MUST call `record_spend` today (per R10 DEFERRED) — tests assert the call is absent so the regression is tracked.
- **R39**. Neither path MAY import `scenecraft.db` from a plugin boundary (R9a from audit-2); in-engine use is fine.
- **R40**. Neither path MAY perform a post-generation file rename on the transition output path (see R22).
- **R41**. The chat path MUST NOT throw through the tool return; all errors surface as `{error: "..."}` (pre-flight) or via `job_manager.fail_job` (mid-flight).

---

## Interfaces / Data Shapes

### Chat tool entry points (`chat_generation.py`)

```python
def start_keyframe_generation(
    project_dir: Path,
    project_name: str,
    kf_id: str,
    count: int,                       # clamped to [1, 8]
    prompt_override: str | None = None,
) -> dict:
    # success: {"job_id": str, "keyframe_id": str, "count": int, "backend": str}
    # failure (pre-flight): {"error": str}

def start_transition_generation(
    project_dir: Path,
    project_name: str,
    tr_id: str,
    count: int,                       # clamped to [1, 4]
    slot_index: int | None = None,    # None = all slots
) -> dict:
    # success: {"job_id": str, "transition_id": str, "count": int,
    #           "slots": list[int], "backend": str}
    # failure (pre-flight): {"error": str}
```

### CLI entry points (`narrative.py`)

```python
def generate_keyframe_candidates(
    yaml_path: str,
    vertex: bool = False,
    candidates_per_slot: int | None = None,
    segment_filter: set[str] | None = None,
    use_replicate: bool = False,
    regen: dict[str, set[str]] | None = None,
) -> None

def generate_transition_candidates(
    yaml_path: str,
    vertex: bool = False,
    candidates_per_slot: int | None = None,
    segment_filter: set[str] | None = None,
    slot_filter: set[int] | None = None,
    on_status=None,
    duration_seconds: int | None = None,
) -> None
```

### Job summary (chat path, written via `job_manager.complete_job`)

```jsonc
// Keyframe
{
  "keyframeId": "kf_007",
  "candidates": ["keyframe_candidates/candidates/section_kf_007/v1.png", "..."],
  "added_count": 4,
  "total_candidates": 8
}

// Transition
{
  "transitionId": "tr_003",
  "generated": [
    {"pool_segment_id": "<uuid>", "transition_id": "tr_003",
     "slot": 0, "path": "pool/segments/<uuid>.mp4"}
  ],
  "added_count": 3
}
```

### `pool_segments` row (chat transition path)

```
id                  TEXT    — pre-generated uuid4.hex
pool_path           TEXT    — "pool/segments/<id>.mp4"
kind                TEXT    — "generated"
created_by          TEXT    — "chat_generation"
duration_seconds    REAL    — slot_duration
created_at          TEXT    — ISO 8601 with tz
```

### `tr_candidates` row

Opaque to this spec; constructed via `add_tr_candidate(project_dir, transition_id, slot, pool_segment_id, source='generated')`.

### Provider surface (opaque)

```python
GoogleVideoClient.stylize_image(source, prompt, output, image_model=?) -> str
GoogleVideoClient.generate_video(start_img, end_img, prompt, out, duration_seconds=..., ingredient_paths=..., negative_prompt=..., seed=...)
GoogleVideoClient.generate_video_transition(start_frame_path, end_frame_path, prompt, output_path, duration_seconds, on_status=...)
RunwayVideoClient(...).generate_video(...)  # same shape
```

---

## Behavior Table

| # | Scenario | Expected Behavior | Tests |
|---|----------|-------------------|-------|
| 1 | Chat: keyframe exists, source image + prompt present | Returns `{job_id, keyframe_id, count, backend}`; N variants written to `candidates/section_<kf>/v*.png`; `candidates` list updated on keyframe; `JobManager` job completes | `chat-keyframe-happy-path`, `chat-keyframe-updates-db` |
| 2 | Chat: keyframe unknown | Returns `{error: "keyframe not found: ..."}`, no thread spawned, no job created | `chat-keyframe-missing-record` |
| 3 | Chat: keyframe has no source image | Returns `{error: "no source image..."}`, no thread, no job | `chat-keyframe-missing-source` |
| 4 | Chat: keyframe has no prompt and no override | Returns `{error: "...has no prompt..."}`, no thread, no job | `chat-keyframe-no-prompt` |
| 5 | Chat: `count` clamping | Counts of `0`, `-3`, `50` clamp to `1, 1, 8`; `count` in return reflects clamp | `chat-keyframe-count-clamp` |
| 6 | Chat: variant numbering appends | Call with 2 existing variants + count=2 yields v3, v4 (never overwrites v1/v2) | `chat-keyframe-append-numbering` |
| 7 | Chat: Imagen transient failure retried | First 2 attempts raise; 3rd succeeds; file written; job progresses | `chat-keyframe-retries-transient` |
| 8 | Chat: Imagen fails 3 times | Worker raises; `JobManager.fail_job` called with error string; no DB mutation | `chat-keyframe-retry-exhausted` |
| 9 | Chat: transition happy path (n_slots=1) | Runs `count` Veo calls; N `pool_segments` rows inserted; N `tr_candidates` rows linked; job completes | `chat-transition-happy-single-slot` |
| 10 | Chat: transition with n_slots=3, all slots | Slot 0 uses `from`→`slot_0`; slot 1 uses `slot_0`→`slot_1`; slot 2 uses `slot_1`→`to`; total jobs = count × 3 | `chat-transition-multi-slot-chain` |
| 11 | Chat: transition with explicit `slot_index=1` | Only slot 1 runs; `slots` in return = `[1]`; total jobs = count | `chat-transition-slot-filter` |
| 12 | Chat: transition `slot_index` out of range | Returns `{error: "slot_index ... out of range ..."}` | `chat-transition-slot-out-of-range` |
| 13 | Chat: transition missing start or end boundary image | Returns `{error}` without spawning worker | `chat-transition-missing-boundary-image` |
| 14 | Chat: `video_backend=runway/<model>` | Uses `RunwayVideoClient(model=<model>)` | `chat-transition-runway-backend` |
| 15 | Chat: transition output UUID pre-generated | File lands at `pool/segments/<id>.mp4` in one step; no intermediate name | `chat-transition-no-post-rename` |
| 16 | Chat: pool insert races DB lock | `_retry_on_locked` retries; eventually single INSERT commits | `chat-transition-pool-retry-on-locked` |
| 17 | Chat: job reporting | `JobManager.create_job` called with correct `total`; `update_progress` called once per success; `complete_job` called once at end | `chat-job-progress-reporting` |
| 18 | Chat: client disconnects mid-job | Thread continues; files land; DB mutated; `complete_job` fires on the (now disconnected) job | `chat-job-survives-disconnect` |
| 19 | CLI: keyframe batch, no existing candidates | Writes `v1.png…vN.png` under `candidates/section_<id>/`; builds `grid.png` | `cli-keyframe-happy-path` |
| 20 | CLI: keyframe batch with `regen={"kf": {"v2"}}` | Deletes `v2.png` only; regenerates `v2.png`; rebuilds grid | `cli-keyframe-regen-specific` |
| 21 | CLI: keyframe with `_existing_keyframe_resolved` | Copies into `selected_keyframes/<id>.png`; skips generation | `cli-keyframe-existing-shortcut` |
| 22 | CLI: transition `PromptRejectedError` mid-batch | Offending `tr_id` collected; other jobs continue; end-of-run log enumerates rejections | `cli-transition-prompt-rejected-continues` |
| 23 | CLI: transition with `_existing_segment_resolved` | Copies existing segments to `selected_transitions/<tr>_slot_<i>.mp4`; skips generation | `cli-transition-existing-segments` |
| 24 | Output file already exists | Worker skips provider call; treats as cached success; progress still advances | `both-paths-cached-output-skipped` |
| 25 | Spend recording **(DEFERRED / R10)** | `record_spend` is NOT called today; test asserts absence to track regression | `record-spend-not-invoked-deferred` |
| 26 | Direct `scenecraft.db` imports from plugin boundary | Not present | `no-plugin-db-import` (static) |
| 27 | Partial slot success (slot 0 succeeds, slot 1 fails) — should job complete partial or fail whole? | `undefined` | → [OQ-1](#open-questions) |
| 28 | Intermediate slot-keyframe file disappears **mid-generation** (between job start and slot worker call) | `undefined` | → [OQ-2](#open-questions) |
| 29 | `PromptRejectedError` on chat transition path (chat has no CLI-like "collect + continue" logic) | `undefined` | → [OQ-3](#open-questions) |
| 30 | Veo reports success but downloaded `.mp4` is 0 bytes | `undefined` | → [OQ-4](#open-questions) |
| 31 | Two concurrent `start_keyframe_generation` calls for the same `kf_id` | `undefined` | → [OQ-5](#open-questions) |

---

## Behavior (step-by-step)

### Chat keyframe (`start_keyframe_generation`)

1. Clamp `count` to `[1, 8]`.
2. Load keyframe record; error if missing (R1).
3. Resolve source path: `kf.source` or `selected_keyframes/<id>.png`; error if missing (R2).
4. Resolve prompt: `prompt_override or kf.prompt`; error if empty (R3).
5. Ensure `candidates/section_<id>/` exists; compute `existing_count` from highest `v*.png`.
6. Read `meta.image_backend` and `meta.image_model`.
7. `JobManager.create_job("chat_keyframe_candidates", total=count, meta={...})`.
8. Spawn daemon thread; return `{job_id, keyframe_id, count, backend}` immediately.
9. In the thread:
   - Build variants list `[existing+1 … existing+count]`.
   - For each variant, in a `ThreadPoolExecutor(max_workers=count)`:
     - If output already exists: progress++, label `"v<n> cached"`, return.
     - Else: up to 3 attempts calling `client.stylize_image(src, varied_prompt, out, image_model=...)`; sleep `5 * tries` between failures; re-raise on third.
     - On success: progress++, label `"v<n>"`.
   - Collect all `candidates/section_<id>/v*.png` sorted by variant index (numeric).
   - `update_keyframe(project_dir, kf_id, candidates=<list>)`.
   - `JobManager.complete_job(job_id, {keyframeId, candidates, added_count, total_candidates})`.
10. Any uncaught exception in the thread → `JobManager.fail_job(job_id, str(e))`.

### Chat transition (`start_transition_generation`)

1. Clamp `count` to `[1, 4]`.
2. Load transition record; error if missing (R4).
3. Load meta: `motionPrompt`, `transition_max_seconds` (default 8), `video_backend` (default "vertex").
4. Read `tr.from`, `tr.to`, `tr.slots` (default 1), `tr.duration_seconds`, `tr.action` (default "Smooth cinematic transition"), `tr.use_global_prompt` (default True), `tr.ingredients`, `tr.negativePrompt`, `tr.seed`.
5. Validate `slot_index` bounds (R6).
6. Verify boundary images exist; error if not (R5).
7. Compute prompt per R19 and `slot_duration` per R15.
8. Resolve ingredient paths (filter to existing).
9. Build `slots_to_process = [slot_index]` if given else `range(n_slots)`; `total = count * len(slots_to_process)`.
10. `JobManager.create_job("chat_transition_candidates", total=...)`.
11. Spawn daemon thread; return `{job_id, transition_id, count, slots, backend}`.
12. In the thread:
    - Pick `GoogleVideoClient` or `RunwayVideoClient` per R12.
    - Ensure `pool/segments/` exists.
    - For each `(slot_index, variant_index)` in a `ThreadPoolExecutor(max_workers=min(count,4))`:
      - Pre-generate `seg_id = uuid4().hex`, `out_path = pool/segments/<id>.mp4`.
      - Resolve start/end images per R14; fall back to boundary if intermediate missing (R16).
      - Up to 3 attempts calling `client.generate_video(...)`; sleep `10 * tries` between failures; re-raise on third.
      - Wrap `INSERT INTO pool_segments (...)` in `_retry_on_locked`.
      - `add_tr_candidate(project_dir, tr_id, slot, seg_id, source='generated')`.
      - Progress++.
    - `JobManager.complete_job(job_id, {transitionId, generated, added_count})`.
13. Exception → `JobManager.fail_job(...)`.

### CLI keyframe / transition

Follows `narrative.py` current behavior; see Requirements R17, R20, R25, R26, R32, R34. Out-of-scope items: work-dir layout internals, `load_narrative` schema, contact-sheet rendering.

---

## Acceptance Criteria

- [ ] All Base Case tests pass against the chat path (`chat_generation.py`).
- [ ] All Base Case tests pass against the CLI path (`narrative.py`) where applicable (rows 19–23).
- [ ] The Behavior Table row ordering matches the Tests section (happy → bad → edge → undefined).
- [ ] Every `undefined` row has a matching Open Question (OQ-1…OQ-5).
- [ ] The DEFERRED R10 test (`record-spend-not-invoked-deferred`) is present in the suite as a **tracked regression** — it asserts absence of `record_spend` calls so the day the code is fixed, the test flips and is rewritten.
- [ ] No test silently guesses behavior for OQ-1…OQ-5.

---

## Tests

### Base Cases

#### Test: chat-keyframe-happy-path (covers R1, R2, R3, R7, R8, R9, R13, R21, R27, R28, R29)

**Given**:
- A project with keyframe `kf_042` having `source=selected_keyframes/kf_042.png` (file exists) and `prompt="moody blue studio"`.
- `meta.image_model = "replicate/nano-banana-2"`, `meta.image_backend = "vertex"`.
- No existing candidates under `keyframe_candidates/candidates/section_kf_042/`.
- `GoogleVideoClient.stylize_image` is stubbed to write a 1×1 PNG to the requested output.

**When**: `start_keyframe_generation(project_dir, "proj", "kf_042", count=3)` is called and the spawned thread is joined.

**Then** (assertions):
- **return-shape**: return value contains exactly keys `job_id`, `keyframe_id`, `count`, `backend`; `keyframe_id == "kf_042"`; `count == 3`; `backend == "vertex"`.
- **files-written**: `v1.png`, `v2.png`, `v3.png` exist under `section_kf_042/`.
- **db-candidates-updated**: `get_keyframe(...)["candidates"]` equals the sorted list of the three new relative paths.
- **job-total**: `JobManager.create_job` was called with `total=3`.
- **job-progress-count**: `update_progress` was called exactly 3 times.
- **job-completed**: `complete_job` was called exactly once; no `fail_job` call.
- **prompts**: v1 provider call used `"moody blue studio"`, v2 used `"moody blue studio, variation 2"`, v3 used `"moody blue studio, variation 3"`.
- **image-model-threaded**: every provider call received `image_model="replicate/nano-banana-2"`.

#### Test: chat-keyframe-missing-record (covers R1, R41)

**Given**: `kf_999` does not exist in the project DB.

**When**: `start_keyframe_generation(..., "kf_999", count=2)`.

**Then**:
- **returns-error-dict**: return value is `{"error": "keyframe not found: kf_999"}`.
- **no-job-created**: `JobManager.create_job` was not called.
- **no-thread**: no daemon thread was spawned (stub `threading.Thread` to observe).
- **no-files**: no files were written under `section_kf_999/`.

#### Test: chat-keyframe-missing-source (covers R2, R41)

**Given**: `kf_050` exists, but `selected_keyframes/kf_050.png` is absent and `kf.source` resolves to a missing path.

**When**: `start_keyframe_generation(..., "kf_050", count=1)`.

**Then**:
- **returns-error-dict**: return value error string contains `"no source image"`.
- **no-job-created**: no `create_job` call.
- **no-files**: no output directory mutated.

#### Test: chat-keyframe-no-prompt (covers R3, R41)

**Given**: `kf_060` has `prompt=""` (or missing) and no `prompt_override` is passed.

**When**: `start_keyframe_generation(..., "kf_060", count=1)`.

**Then**:
- **returns-error-dict**: error string contains `"has no prompt"`.
- **no-job-created**: no `create_job` call.

#### Test: chat-keyframe-count-clamp (covers R7)

**Given**: a valid keyframe.

**When**: `start_keyframe_generation` is called with `count` values `0`, `-5`, `50`.

**Then** (one assertion each):
- **zero-clamps-to-one**: return `count == 1`, job `total == 1`.
- **neg-clamps-to-one**: return `count == 1`, job `total == 1`.
- **over-max-clamps-to-eight**: return `count == 8`, job `total == 8`.

#### Test: chat-keyframe-append-numbering (covers R8, R36)

**Given**: `section_kf_007/` already contains `v1.png` and `v2.png`.

**When**: `start_keyframe_generation(..., "kf_007", count=2)` is called and completes.

**Then**:
- **new-variants-are-v3-v4**: files `v3.png` and `v4.png` now exist.
- **existing-untouched**: byte contents of `v1.png` and `v2.png` are unchanged (compare hash).
- **db-candidates-contains-all**: `get_keyframe("kf_007")["candidates"]` lists v1…v4 in numeric order.

#### Test: chat-keyframe-retries-transient (covers R11)

**Given**: `stylize_image` stub raises `RuntimeError("transient")` on calls 1 and 2, succeeds on call 3.

**When**: `start_keyframe_generation(..., count=1)` runs to completion.

**Then**:
- **three-attempts**: provider was called 3 times.
- **file-eventually-written**: output `v<n>.png` exists.
- **job-completed-not-failed**: `complete_job` called; `fail_job` not called.

#### Test: chat-keyframe-retry-exhausted (covers R11, R33)

**Given**: `stylize_image` stub raises on every call.

**When**: `start_keyframe_generation(..., count=1)` runs; wait for thread to finish.

**Then**:
- **three-attempts-made**: provider called exactly 3 times.
- **fail-job-called**: `fail_job` invoked with the exception string; `complete_job` not called.
- **no-db-mutation**: `get_keyframe(...)["candidates"]` is unchanged from pre-call state.

#### Test: chat-keyframe-updates-db (covers R21)

**Given**: Three variants generated successfully.

**When**: thread completes.

**Then**:
- **update-keyframe-called-once**: `update_keyframe` called exactly once, with a `candidates` kwarg.
- **sorted-by-variant-num**: the candidates list is sorted by the numeric suffix, not lexicographically (so `v10` sorts after `v9`).

#### Test: chat-transition-happy-single-slot (covers R4, R5, R7, R9, R12, R22, R23, R24, R27, R29, R30)

**Given**:
- Transition `tr_010` with `from=kf_A`, `to=kf_B`, `slots=1`, `duration_seconds=4`.
- Boundary images present.
- `meta.video_backend = "vertex"`, `meta.transition_max_seconds = 8`.
- `GoogleVideoClient.generate_video` stub writes a tiny `.mp4` to the requested path.

**When**: `start_transition_generation(..., "tr_010", count=2)` completes.

**Then**:
- **return-shape**: `{job_id, transition_id:"tr_010", count:2, slots:[0], backend:"vertex"}`.
- **job-total-2**: `create_job` called with `total=2`.
- **files-in-pool**: 2 `.mp4` files exist under `pool/segments/`, each named `<uuid>.mp4` matching a v4 hex uuid.
- **pool-rows-inserted**: 2 rows in `pool_segments` with `kind='generated'`, `created_by='chat_generation'`, `duration_seconds=4` (or `min(max_seconds, duration/1) == 4`), and `pool_path` = `pool/segments/<id>.mp4`.
- **tr-candidates-linked**: 2 rows inserted via `add_tr_candidate` with `slot=0`, `source='generated'` referencing the new seg ids.
- **complete-job-summary**: `complete_job` summary `added_count == 2` and `generated` has 2 entries.

#### Test: chat-transition-multi-slot-chain (covers R14, R15, R27, R28)

**Given**:
- Transition `tr_020` with `from=kf_A`, `to=kf_B`, `slots=3`, `duration_seconds=9`, `meta.transition_max_seconds=8`.
- Boundary images present.
- Intermediate slot keyframes `tr_020_slot_0.png` and `tr_020_slot_1.png` present under `selected_slot_keyframes/`.

**When**: `start_transition_generation(..., "tr_020", count=1)` completes.

**Then**:
- **slot-0-uses-from-to-slot0**: generate_video for slot 0 called with start=`kf_A.png`, end=`tr_020_slot_0.png`.
- **slot-1-uses-slot0-to-slot1**: generate_video for slot 1 called with start=`tr_020_slot_0.png`, end=`tr_020_slot_1.png`.
- **slot-2-uses-slot1-to-to**: generate_video for slot 2 called with start=`tr_020_slot_1.png`, end=`kf_B.png`.
- **slot-duration**: each call uses `duration_seconds = min(8, 9/3) = 3`.
- **job-total-3**: `create_job` called with `total = 1*3 = 3`.

#### Test: chat-transition-slot-filter (covers R6, R27)

**Given**: `tr_030` with `slots=3`, valid boundaries + intermediates.

**When**: `start_transition_generation(..., "tr_030", count=2, slot_index=1)`.

**Then**:
- **return-slots**: return `slots == [1]`.
- **job-total**: `create_job` called with `total=2`.
- **provider-calls-slot-1-only**: all provider calls used slot 1's start/end images; neither slot 0 nor slot 2 was called.

#### Test: chat-transition-slot-out-of-range (covers R6, R41)

**Given**: `tr_040` with `slots=2`.

**When**: `start_transition_generation(..., "tr_040", count=1, slot_index=5)`.

**Then**:
- **returns-error**: result error contains `"slot_index 5 out of range"` and `"2 slots"`.
- **no-job**: `create_job` not called.

#### Test: chat-transition-missing-boundary-image (covers R5, R41)

**Given**: `tr_050` whose `selected_keyframes/<from>.png` does not exist on disk.

**When**: `start_transition_generation(..., "tr_050", count=1)`.

**Then**:
- **returns-error**: error string contains `"start keyframe image not found"`.
- **no-job-spawned**: no thread, no pool row.

#### Test: chat-transition-runway-backend (covers R12)

**Given**: `meta.video_backend = "runway/veo3.1_fast"`.

**When**: transition generation runs with valid preconditions.

**Then**:
- **runway-client-used**: `RunwayVideoClient(model="veo3.1_fast")` is instantiated (not `GoogleVideoClient`).

#### Test: chat-transition-no-post-rename (covers R22, R40)

**Given**: A transition generation that produces one segment.

**When**: the worker completes.

**Then**:
- **single-final-path**: only one `.mp4` file was created for that variant; its name matches the uuid used for the `pool_segments.id`.
- **no-rename-syscalls**: monitored `os.rename`/`shutil.move` were not called in the worker (spy).

#### Test: chat-transition-pool-retry-on-locked (covers R23)

**Given**: `_retry_on_locked` wraps the insert; the first 2 DB attempts raise `sqlite3.OperationalError("database is locked")`, the 3rd succeeds.

**When**: one transition variant completes.

**Then**:
- **retries-occurred**: insert function was invoked 3 times.
- **row-present**: a single row exists in `pool_segments` (not 3 rows — not duplicated).

#### Test: chat-job-progress-reporting (covers R28, R29)

**Given**: A generation call producing 3 successful outputs.

**When**: the worker completes.

**Then**:
- **create-job-total**: `create_job` called with `total == 3`.
- **update-progress-3-times**: `update_progress(job_id, completed)` called 3 times with `completed` going `1, 2, 3`.
- **complete-called-once**: `complete_job` called exactly once at the end.
- **fail-not-called**: `fail_job` not called.

#### Test: chat-job-survives-disconnect (covers R31)

**Given**: A generation call in progress; the simulated WS client closes its connection while the worker is mid-batch.

**When**: the provider stub finishes all calls.

**Then**:
- **files-still-written**: all N outputs exist on disk.
- **db-still-mutated**: `update_keyframe` / `pool_segments` rows present.
- **complete-job-still-called**: `complete_job` still fired (on the JobManager object, even though no WS client is listening).

#### Test: cli-keyframe-happy-path (covers R25, R32)

**Given**: `narrative.yaml` with 2 keyframes, `meta.candidates_per_slot=3`, `use_replicate=False`.

**When**: `generate_keyframe_candidates(yaml_path, vertex=True)` returns.

**Then**:
- **files-written**: each keyframe's `candidates/section_<id>/v1.png…v3.png` exist.
- **grid-written**: `grid.png` exists in each `section_<id>/` dir.
- **synchronous**: call returned only after all provider stubs were invoked; no background thread outstanding.

#### Test: cli-keyframe-regen-specific (covers R25)

**Given**: `section_kf_A/` has v1–v3; `regen = {"kf_A": {"v2"}}`.

**When**: `generate_keyframe_candidates(..., regen=regen)` runs.

**Then**:
- **v2-deleted-and-regenerated**: the `v2.png` file's mtime is newer than `v1.png`'s and `v3.png`'s.
- **v1-v3-untouched**: byte hashes of `v1.png` and `v3.png` unchanged.
- **grid-rebuilt**: `grid.png` mtime is newer than all v*.png.

#### Test: cli-keyframe-existing-shortcut (covers R25)

**Given**: Keyframe has `_existing_keyframe_resolved` pointing to a real file.

**When**: `generate_keyframe_candidates(...)`.

**Then**:
- **copied-to-selected**: `selected_keyframes/<id>.png` exists and is identical to the existing file.
- **no-provider-call**: `stylize_image` was not called for that keyframe.

#### Test: cli-transition-prompt-rejected-continues (covers R34)

**Given**: 3 transition jobs; Veo stub raises `PromptRejectedError` on the 2nd; others succeed.

**When**: `generate_transition_candidates(...)` returns.

**Then**:
- **two-outputs-written**: jobs 1 and 3 produced `.mp4` files.
- **rejected-set-logged**: rejection log contains the 2nd job's `tr_id`.
- **no-exception-escapes**: the function returns normally (does not raise).

#### Test: cli-transition-existing-segments (covers R26)

**Given**: Transition has `_existing_segment_resolved=["/abs/a.mp4","/abs/b.mp4"]` and `slots=2`.

**When**: `generate_transition_candidates(...)`.

**Then**:
- **slot-files-copied**: `selected_transitions/<tr>_slot_0.mp4` and `<tr>_slot_1.mp4` exist with matching byte hashes.
- **no-provider-calls**: Veo was not invoked for that transition.

#### Test: both-paths-cached-output-skipped (covers R9)

**Given**: Target output files already exist at the expected paths.

**When**: generation runs on both chat and CLI paths.

**Then**:
- **provider-not-called**: `stylize_image` / `generate_video` not called for the cached variants.
- **progress-still-advances** (chat): `update_progress` labels include `"cached"` and counts still increment to `total`.

#### Test: record-spend-not-invoked-deferred (covers R10 — **SKIP-DEFERRED**)

**Given**: An instrumented `plugin_api.record_spend` spy; a happy-path keyframe generation and happy-path transition generation.

**When**: both paths complete.

**Then**:
- **record-spend-zero-calls-today**: spy shows 0 invocations. *This test documents the current broken state; when the leak is fixed, this assertion MUST flip and the test be rewritten to assert `record_spend` IS called with a correct provider/amount/unit.*

#### Test: no-plugin-db-import (covers R39 — static)

**Given**: The set of files under `src/scenecraft/plugins/`.

**When**: AST-scanning plugin files for `import scenecraft.db` or `from scenecraft.db`.

**Then**:
- **no-import-found**: no plugin file imports `scenecraft.db` directly (allowlist from R9a preserved).

### Edge Cases

#### Test: chat-keyframe-prompt-override (covers R3, R18)

**Given**: Keyframe has prompt `"A"`; `prompt_override="B"` is passed.

**When**: 2 variants generated.

**Then**:
- **override-wins**: v1 provider call uses `"B"`, v2 uses `"B, variation 2"`.

#### Test: chat-keyframe-unicode-prompt (covers R18)

**Given**: Prompt `"日本語 — πρώτος"` (multibyte).

**When**: 1 variant generated.

**Then**:
- **prompt-passed-as-utf8**: provider call received the exact string (no mojibake).
- **file-written**: `v<n>.png` exists.

#### Test: chat-transition-count-clamp (covers R7)

**Given**: `count=99` on transition generation.

**When**: call completes.

**Then**:
- **clamped-to-4**: return `count == 4`; `create_job` called with `total = 4 * n_slots_selected`.

#### Test: chat-transition-intermediate-missing-falls-back (covers R16)

**Given**: `tr_080` with `slots=2`; `tr_080_slot_0.png` does NOT exist at job start.

**When**: transition generation runs.

**Then**:
- **slot-0-end-falls-back-to-to**: provider call for slot 0 used `end = selected_keyframes/<to>.png`.
- **slot-1-start-falls-back-to-from**: provider call for slot 1 used `start = selected_keyframes/<from>.png`.
- **no-error**: job does not fail; it completes all slots.

#### Test: chat-transition-ingredient-filtering (covers transition input preparation)

**Given**: `tr.ingredients = ["a.png", "ghost.png", ""]`; only `a.png` exists.

**When**: transition generation.

**Then**:
- **only-existing-passed**: provider received `ingredient_paths=["/project_dir/a.png"]` (empties dropped, missing dropped).

#### Test: chat-transition-no-ingredients-is-none (covers transition input preparation)

**Given**: `tr.ingredients = []` or `None`.

**When**: transition generation.

**Then**:
- **ingredient-paths-is-none**: provider received `ingredient_paths=None`.

#### Test: chat-transition-global-prompt-off (covers R19)

**Given**: `tr.use_global_prompt=False`, `meta.motionPrompt="cinematic"`, `tr.action="swoop"`.

**When**: transition generation.

**Then**:
- **prompt-is-action-only**: provider called with `prompt="swoop"` (motion not appended).

#### Test: chat-transition-no-motion-prompt (covers R19)

**Given**: `meta` has neither `motionPrompt` nor `motion_prompt`; `use_global_prompt=True`.

**When**: transition generation.

**Then**:
- **prompt-is-action-only**: provider called with the bare action string.

#### Test: chat-keyframe-output-exists-cached (covers R9)

**Given**: `v1.png` already exists for `kf_100`.

**When**: `start_keyframe_generation(..., count=2)` where variant indices are `[existing+1, existing+2]` but one pre-existed (contrived pre-seed).

**Then**:
- **cached-skip**: `stylize_image` not called for the pre-existing file.
- **progress-still-3-max**: `update_progress` still fired for the cached slot with a `"cached"` label.

#### Test: chat-keyframe-three-fail-fail-job-string (covers R11, R33)

**Given**: `stylize_image` raises `ValueError("boom")` on every call.

**When**: worker finishes.

**Then**:
- **fail-job-contains-boom**: `fail_job` arg string contains `"boom"`.
- **progress-never-incremented**: `update_progress` not called for this variant.

#### Test: cli-transition-slot-filter (covers R20, R34)

**Given**: Transition with `slots=3`; `slot_filter={0}`.

**When**: `generate_transition_candidates(..., slot_filter={0})`.

**Then**:
- **slot-0-only**: only slot_0 directory gets new `.mp4` files; slot_1 and slot_2 directories are untouched.

#### Test: chat-concurrency-thread-is-daemon (covers R27)

**Given**: Chat tool entry point.

**When**: `start_keyframe_generation` returns.

**Then**:
- **thread-alive-and-daemon**: the spawned thread's `daemon` attribute is True.
- **tool-returns-before-worker-done**: the return happened before any provider call completed (observed via provider stub delay).

#### Test: chat-partial-slot-success (covers OQ-1)

`undefined` — see [Open Questions](#open-questions). Do NOT add an assertion-based test until OQ-1 is resolved.

#### Test: chat-intermediate-slot-key-disappears-mid-job (covers OQ-2)

`undefined` — see [Open Questions](#open-questions).

#### Test: chat-prompt-rejected-error (covers OQ-3)

`undefined` — see [Open Questions](#open-questions).

#### Test: chat-veo-returns-zero-byte-file (covers OQ-4)

`undefined` — see [Open Questions](#open-questions).

#### Test: chat-concurrent-start-same-keyframe (covers OQ-5)

`undefined` — see [Open Questions](#open-questions).

---

## Non-Goals

- **Harmonizing the two paths into one** — that's a future refactor. This spec only defines the shared contract both paths must honor today.
- **Fixing R10 (spend recording)** — intentionally DEFERRED; the test exists as a regression tracker.
- **Replacing `_retry_on_locked` with a transaction queue** — out of scope; treated as an opaque helper.
- **Changing the retry counts / backoff constants** — documenting current behavior, not redesigning.
- **Specifying `make_contact_sheet` / `generate_image_candidates` internals** — out of scope (candidates.py has its own surface).
- **Specifying `GoogleVideoClient._retry_video_generation` / 429 handling** — provider spec, separate.
- **Specifying final video assembly, compositor, schedule, or cache invalidation** — separate render-pipeline spec.
- **Plugin-provider surface design** — whether generation should move behind `plugin_api.providers` is a scenecraft-engine-wide question, not this spec's.

---

## Open Questions

### OQ-1 — Partial slot success: keep partials or roll back?

If transition generation has `slots=3` and slot 1 fails after slot 0 succeeded, what is the desired end state?
- **Option A**: Keep slot 0's `pool_segments` row + `tr_candidates` link; mark job failed; user can retry just the failed slot.
- **Option B**: Roll back all slots on any failure; nothing persists; user retries the whole transition.
- Current code: **Option A-ish** — the `complete_job` line is never reached on exception, but the `pool_segments` + `tr_candidates` inserts already committed for prior successful variants. This is de facto "partial success with failed job marker" and is likely unintentional.

### OQ-2 — Intermediate slot-keyframe disappears mid-generation

The slot-keyframe file existence check happens *inside* `_gen_one`, but races are possible if another actor (Resolve, the user, a concurrent tool) deletes the file after job start. Current code: silently falls back to the boundary image (R16). Is that desired, or should the slot abort?

### OQ-3 — `PromptRejectedError` on the chat path

CLI collects rejections and continues (R34). Chat has no such collector — a `PromptRejectedError` from Veo will blow up the worker after retries (or earlier if the retry loop re-raises it as a regular `Exception`). Should the chat path:
- **A**: Fail the whole job on first rejection.
- **B**: Collect rejections like the CLI, complete other slots, and include `rejected_slots` in the summary.
- **C**: Treat rejection as permanent (don't retry) but still fail the whole job.

### OQ-4 — Veo succeeds but downloaded file is 0 bytes

`generate_video` may return normally while the download lands a truncated or zero-byte `.mp4`. Today the code inserts the `pool_segments` row and links the `tr_candidate` — the broken file is visible to the user. Should we:
- **A**: Stat the file ≥ N bytes before insert; treat 0-byte as a failure (retry).
- **B**: Always insert; leave validation to a downstream task.

### OQ-5 — Concurrent `start_*_generation` for the same entity

Two chat tool calls targeting the same `kf_id` (or `tr_id`) interleave on:
- Keyframe: `existing_count` computed separately → both decide `v3, v4` → collisions possible; `ThreadPoolExecutor` per-job does not coordinate. `update_keyframe` overwrites `candidates` whichever finishes last.
- Transition: each gets its own pre-generated UUIDs → no path collision, but two `JobManager` jobs reference the same `tr_id`, and the progress UI may be confusing.

Desired policy?
- **A**: Lock per-entity; reject second call with `{error: "generation already in progress for <id>"}`.
- **B**: Allow; accept that variant numbering may double-up (files collide on write).
- **C**: Allow transitions (safe via UUID) but reject concurrent keyframes.

---

## Known-Issue Requirements (DEFERRED)

- **R10 (Spend tracking)** — `record_spend` is not called by either path for Imagen / Veo / Runway. Captured as `record-spend-not-invoked-deferred` test; it intentionally asserts **absence** today. When the leak is fixed (via provider-surface unification), the test flips and is rewritten. This mirrors audit-2 §3 leak #1.
- **Dual code paths (audit-2 §3 leak #6)** — not a single requirement, but the whole reason this spec exists; tracked by the "Two Codepaths, One Contract" callout above.

---

## Related Artifacts

- `agent/reports/audit-2-architectural-deep-dive.md` §1D units 1–4; §3 leaks #1 and #6
- `src/scenecraft/chat_generation.py` — chat tool implementation
- `src/scenecraft/render/narrative.py` — `generate_keyframe_candidates`, `generate_transition_candidates`
- `src/scenecraft/render/google_video.py` — `GoogleVideoClient`, `RunwayVideoClient`, `PromptRejectedError`, `_retry_video_generation`
- `src/scenecraft/ws_server.py` — `job_manager` (JobManager used by chat path)
- `src/scenecraft/db.py` — `get_keyframe`, `update_keyframe`, `get_transition`, `add_tr_candidate`, `_retry_on_locked`
- Future specs:
  - `local.render-pipeline-composition` (compositor, schedule, assembly)
  - `local.provider-surface` (plugin_api.providers unification; will retire R10 DEFERRED)

---

**Namespace**: local
**Spec**: engine-generation-pipelines
**Version**: 1.0.0
**Created**: 2026-04-27
**Status**: Draft
