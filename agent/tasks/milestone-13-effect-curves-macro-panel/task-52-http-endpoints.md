# Task 52: HTTP endpoints for effect curves

**Milestone**: [M13 - Effect Curves + Macro Panel + Touch-Record](../../milestones/milestone-13-effect-curves-macro-panel.md)
**Spec**: [local.effect-curves-macro-panel](../../specs/local.effect-curves-macro-panel.md) — R51, R52 + every HTTP endpoint in §Interfaces / Data Shapes
**Estimated Time**: 4 hours
**Dependencies**: T45 (schema + DB helpers)
**Status**: Not Started
**Repository**: `scenecraft-engine` (backend) + `scenecraft` (frontend client)

---

## Objective

Expose 11 REST endpoints for CRUD on effect chains, curves, send buses, track sends, and frequency labels. Integrate with `cache_invalidation` (range-based) so edits drop affected preview frames.

---

## Steps

### 1. Endpoints

Add handlers in `src/scenecraft/api/effect_curves.py` (or split across `api/tracks.py`, `api/buses.py` per existing module-split conventions from task-31):

**Effects:**
- `POST /api/projects/:name/track-effects` — create `{track_id, effect_type, static_params, order_index?}`
- `POST /api/projects/:name/track-effects/:id` — update `{order_index?, enabled?, static_params?}`
- `DELETE /api/projects/:name/track-effects/:id` — delete (cascades to curves)

**Curves:**
- `POST /api/projects/:name/effect-curves` — create `{effect_id, param_name, points, interpolation, visible}`
- `POST /api/projects/:name/effect-curves/:id` — update `{points?, interpolation?, visible?}`
- `DELETE /api/projects/:name/effect-curves/:id` — delete

**Buses:**
- `POST /api/projects/:name/send-buses` — create `{bus_type, label, static_params, order_index?}`
- `POST /api/projects/:name/send-buses/:id` — update `{label?, order_index?, static_params?}`
- `DELETE /api/projects/:name/send-buses/:id` — delete (cascades to track_sends)

**Sends:**
- `POST /api/projects/:name/track-sends` — upsert `{track_id, bus_id, level}`

**Frequency labels:**
- `POST /api/projects/:name/frequency-labels` — create `{label, freq_min_hz, freq_max_hz}`
- `DELETE /api/projects/:name/frequency-labels/:id` — delete

### 2. Validation (consolidated per spec R_V1)

Validate inputs BEFORE writing. Each failure returns a specific HTTP status with an error body naming the offending field.

**Input shape validation:**
- `points` array: clamp values to [0, 1]; sort by time ascending; dedupe exact-time duplicates (keep last). Clamping is NOT an error — return 200 with a warning-level log (spec test `curve-point-values-out-of-range-clamped`).
- `interpolation`: one of `bezier`, `linear`, `step`.
- `bus_type`: one of `reverb`, `delay`, `echo`.
- `effect_type`: must be in the R8 registry (17 real types). `__send` is **rejected** at this endpoint with HTTP 400 per R8a — it's a reserved synthetic type usable only via `effect_curves`.

**Reference integrity (R_V1):**
- `POST /track-effects` with unknown `effect_type` → HTTP 400 (spec test `unknown-effect-type-rejected`).
- `POST /effect-curves` with non-existent `effect_id` → HTTP 404 (spec test `animating-static-param-rejected` case b).
- `POST /effect-curves/:id` with non-existent `:id` → HTTP 404.
- `POST /track-sends` with non-existent `track_id` or `bus_id` → HTTP 404.
- `POST /effect-curves` for a non-animatable param (e.g. `character` on drive, `bus_id` on send, `rate` on modulation LFOs, IR-choice on reverb) → HTTP 400 with an error message naming the static param (spec R9 strengthened, test `animating-static-param-rejected` case a).

**Idempotent delete (R_V1):**
- DELETE on a non-existent `track_effects` (or curve / bus / label) is HTTP 200 with empty body, NOT 404 (spec test `delete-nonexistent-effect-idempotent`).

**Order-index collision (R_V1 + R14):**
- `POST /track-effects/:id` with an `order_index` value already held by another effect on the same track triggers an **atomic swap**: the server rewrites all three effects' order_index values within a single SQLite transaction so the final state has no duplicates. The `mixer.chain-rebuilt` event fires exactly once per POST (spec test `order-index-collision-resolved-atomically`). Implementations MUST NOT leave two effects sharing an order_index between statements.

### 3. Cache invalidation hooks

After each write, call `invalidate_frames_for_mutation(project_dir, ranges)`:
- Track-effect add/remove/reorder/enable-toggle: invalidate the full time-range of all clips on that track
- Curve create/update/delete: invalidate `[min(points[].time), max(points[].time)]` (plus a small margin for safety)
- Bus create/update/delete: invalidate project-wide (wholesale)
- Track-send update: invalidate full time-range of that track
- Frequency label ops: no-op (labels are metadata only)

Reuse the helper from `src/scenecraft/render/cache_invalidation.py` (created in task-38 groundwork).

### 4. Frontend client

Update `scenecraft/src/lib/audio-client.ts` (existing file) with typed wrappers:

```ts
export async function postCreateTrackEffect(projectName, body) -> TrackEffect
export async function postUpdateTrackEffect(projectName, id, patch) -> TrackEffect
export async function deleteTrackEffect(projectName, id) -> void

// ... etc for all 11 endpoints
```

Follow the existing `postUpdateAudioClip` / `postUpdateAudioTrack` signature patterns.

### 5. Tests

`tests/test_effect_curves_api.py`:
- POST a track-effect, verify DB row + returned JSON
- POST a curve with out-of-range points, verify clamped + warning logged (spec test `curve-point-values-out-of-range-clamped`)
- DELETE track-effect cascades to curves (spec test `orphan-curve-cleaned-on-effect-delete`)
- Invalidation called with correct ranges (mock `invalidate_frames_for_mutation`)
- Listing effects for a track returns them in `order_index` order
- **R_V1 coverage** (new, per proofing pass):
  - Invalid `effect_type` returns 400 (spec test `unknown-effect-type-rejected`)
  - `__send` POSTed to `/track-effects` returns 400 (R8a)
  - POST `/effect-curves` for non-existent `effect_id` → 404 (spec test `animating-static-param-rejected` case b)
  - POST `/effect-curves` for a static param → 400 naming the param (spec test `animating-static-param-rejected` case a)
  - DELETE on non-existent id → 200 empty body, not 404 (spec test `delete-nonexistent-effect-idempotent`)
  - `order_index` collision → atomic swap in one transaction, exactly one `mixer.chain-rebuilt` event (spec test `order-index-collision-resolved-atomically`)
  - `UNIQUE(effect_id, param_name)` violation: raw duplicate INSERT fails at SQL layer (spec test `effect-curves-unique-constraint`)

---

## Verification

- [ ] All 11 endpoints respond 200 on valid input, 400 on invalid
- [ ] Clamping on out-of-range curve points works + logs warning
- [ ] Cache invalidation called with correct range for each mutation type
- [ ] Frontend client typed correctly; `tsc --noEmit` clean
- [ ] Tests pass

---

## Notes

- Endpoint paths follow the existing `/api/projects/:name/...` convention from the rest of the codebase.
- Auth uses the existing session cookie middleware — no new auth logic.
- No WebSocket push yet for effect-curve edits (could be added in a future task if multi-tab sync becomes a concern). Reads re-fetch on tab focus via existing invalidation patterns.
