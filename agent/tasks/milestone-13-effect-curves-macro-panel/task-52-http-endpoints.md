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

### 2. Validation

- `points` array: clamp values to [0, 1]; sort by time ascending; dedupe exact-time duplicates (keep last)
- `interpolation`: one of `bezier`, `linear`, `step`
- `bus_type`: one of `reverb`, `delay`, `echo`
- `effect_type`: must be in the registry (hardcoded list of 17 type strings — mirrors frontend registry)

On clamp, log a warning; still return 200 (per spec test `curve-point-values-out-of-range-clamped`).

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
- POST a curve with out-of-range points, verify clamped + warning logged
- DELETE track-effect cascades to curves
- Invalidation called with correct ranges (mock `invalidate_frames_for_mutation`)
- Listing effects for a track returns them in `order_index` order
- Invalid `effect_type` returns 400

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
