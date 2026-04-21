# Task 38: Range-based preview frame cache invalidation

**Milestone**: None (cross-cutting infrastructure)
**Design Reference**: None (extension of `local.backend-rendered-preview-streaming.md` §L1 cache; see Step 1)
**Estimated Time**: 3-4 days
**Dependencies**: Task 37 (unified WebSocket) — required for the frontend push-invalidation subtask; backend-only subtask can ship before 37
**Status**: Not Started
**Repositories**: `scenecraft-engine` (backend) + `scenecraft` (frontend)

---

## Objective

Replace the current "any DB write invalidates the entire project's preview cache" policy with explicit per-operation range-based invalidation. Introduce a frontend bitmap cache that mirrors the backend cache, kept in sync by server-pushed `cache.invalidate { range }` messages. Scrubbing across previously-visited frames becomes a local paint, not a network round-trip.

---

## Context

Today `FrameCache._key()` embeds `max(db.mtime, db-wal.mtime)` so any DB write — even an unrelated one, like updating a keyframe on a different track — bumps the cache key for the whole project. After every edit the next ~500 scrub requests all miss. The backend renders and re-encodes frames that are pixel-identical to the ones it just evicted.

The right model:

1. **Keep cache across edits.** Cache key drops mtime; entries survive unrelated writes.
2. **Invalidate exactly what changed.** Every mutating endpoint knows its effect on the timeline's time range. `/update-transition` with a trim from `[10..15]` → `[10..13]` needs to drop cached frames in `[10..15]` (old) only — frames outside that span are still correct.
3. **Push to client.** Once the server knows the invalidated range, it tells every client watching the project so they can drop matching keys from their own frame bitmap cache. Frontend caches the visited frames; refetches only the ones the server invalidated.

The client cache is what makes scrub feel instant on material you've already looked at — revisiting a known `t` is just `ctx.drawImage(bmp, 0, 0)`, no fetch.

---

## Steps

### 1. Design doc

Write `agent/design/local.range-invalidation.md` covering:

- Cache key shape (drop mtime, keep `(project, t_ms, quality)`)
- Invalidation-range catalog — one row per mutating endpoint, mapping `(endpoint, mutation)` → `affected_range(payload) → [(t_start, t_end), ...]`
- Server-push protocol: message shape `{ type: "cache.invalidate", project, ranges: [[t0, t1], ...] }` (rides on the unified WS from task 37; if that's not landed yet, use a temporary `/ws/cache/:project` handler)
- Frontend cache spec: max entries, LRU eviction, mtime unaffected (server push is authoritative)
- Under-invalidation detection strategy (pixel-diff test harness — render a known frame at time T, mutate, render again, diff — see Testing)
- Fallback: how to drop *all* cached entries in the rare case an endpoint can't compute a tight range (catalog gap, new endpoint added without hook)

### 2. Backend: cache key + range API

In `src/scenecraft/render/frame_cache.py`:

- Drop mtime from `CacheKey` → `(str(project_dir), int(round(t * 1000)), quality)`
- Drop the `_key()` mtime lookup branch
- Add `FrameCache.invalidate_range(project_dir, t_start, t_end) -> int` — O(N) scan, drop entries whose `t_ms` falls in `[t_start * 1000, t_end * 1000]`. Return count.
- Keep `invalidate_project` as the escape hatch
- Tests: covers range hits/misses, boundary inclusivity, multiple ranges, empty range

### 3. Backend: invalidation catalog

Audit every route in `src/scenecraft/api/` that mutates preview-relevant state. For each, compute the affected range and call `global_cache.invalidate_range(...)` after the DB write commits. Starting catalog (refine during implementation):

| Endpoint | Affected range |
|---|---|
| `POST /transitions` (add) | `[from, to]` |
| `POST /transitions/:id` (update — trim, style, curves, blend, opacity, anchors, mask) | `[old_from..old_to] ∪ [new_from..new_to]` |
| `POST /delete-transition` | `[from, to]` |
| `POST /restore-transition` | `[from, to]` |
| `POST /split-transition` | `[from, to]` (original) ∪ both halves |
| `POST /move-transitions` | union of source clip ranges + target clip ranges + overlap-resolution ranges (consume/trim-left/right/split — see M10-T95/T96 logic) |
| `POST /paste-group` | union of pasted clip ranges |
| `POST /insert-pool-item` | landing transition's `[from, to]` |
| `POST /keyframes` (add/update/delete) | adjacent transition(s)' time span (since keyframes bound transitions) |
| `POST /batch-delete-keyframes` | union of affected transition ranges |
| `POST /tracks/:id` (blend_mode, base_opacity, enabled toggle) | full track time range |
| `POST /undo`, `POST /redo` | conservative: use `invalidate_project` (or track per-op undo range; see design) |

Each handler gets 2-5 added lines. A test per endpoint verifies the right range was invalidated (mock the cache).

### 4. Backend: push to clients

- Per-project subscriber list maintained in a module-global dict `{project_dir: set(ws_connections)}`
- After `invalidate_range(...)` writes to cache, also broadcast `{type: "cache.invalidate", project, ranges: [[t_start, t_end]]}` to subscribers of that project
- If task 37 is landed: dispatch through unified WS
- If not: temporary `/ws/cache/:project` endpoint with dead-simple pubsub, deprecated once 37 ships

### 5. Frontend: bitmap cache

In `scenecraft/src/components/editor/PreviewViewport.tsx`:

- Add `Map<number /* t_ms */, ImageBitmap>` (or pair per-quality maps if multiple scrub qualities become common)
- Capacity: 500 entries, LRU (mirror backend)
- On scrub paint: look up `t_ms`, paint if present, otherwise fire `fetchScrubFrame` and cache on resolve
- Latest-t paint gate still wins (no out-of-order paints)

### 6. Frontend: subscribe + apply invalidations

- On `PreviewViewport` mount, subscribe to `cache.invalidate` for this project (via unified WS if present, else temp endpoint)
- On invalidation message: drop keys in `[t0 * 1000, t1 * 1000]` from the local map. Do NOT prefetch — the next scrub visit re-fetches naturally.
- On unmount: unsubscribe

### 7. Delete obsolete behavior

- Drop the mtime-observing code paths in `frame_cache.py` and `api_server.py`'s `Cache-Control` logic (the `no-store` stays, since we don't want browser-HTTP-cache stale hits)
- Remove any dead `invalidate_project` callers that can now use ranges

### 8. Under-invalidation regression harness

Write `tests/test_cache_invalidation_parity.py`:

- For each mutating endpoint in the catalog, set up a project, render frame at time T inside the expected affected range, mutate, render again, diff the two frames
- Assert: frames are different (mutation actually changed output) AND cache was invalidated at T (so the second render hit the renderer, not the cache)
- Also: render frame at time T' *outside* the expected range, mutate, render again, assert the cached frame was served (no pixel diff, cache hit counter went up)

This catches catalog gaps — when we forget to invalidate for a new endpoint or mis-compute a range.

---

## Verification

- [ ] Editing one keyframe / transition / track does NOT wholesale-invalidate the project's frame cache (hit rate visible via `GET /api/render-cache/stats` stays high after an edit that's narrow in time)
- [ ] Scrubbing across previously-visited frames paints instantly (no network request, confirmed via devtools network tab)
- [ ] Editing a clip at time T causes cached frames in `[T.from..T.to]` to drop on *both* backend and frontend; frames outside that range survive
- [ ] Cross-tab: editing in one browser tab invalidates the frame cache in another tab watching the same project
- [ ] Regression harness passes for every catalogued endpoint
- [ ] New mutating endpoints added post-task fail the harness until they add invalidation — i.e., harness has a catch-all "all mutating endpoints must be in the catalog" check
- [ ] `GET /api/render-cache/stats` reports hit rate > 80% during a typical editing session

---

## Key Design Decisions

### Model

| Decision | Choice | Rationale |
|---|---|---|
| Invalidation granularity | Per-range, per-endpoint | Current mtime-based wholesale invalidation makes the cache useless after any edit. Range-based invalidation costs per-endpoint plumbing but gives near-100% hit rate across edits. |
| Cache key | Drop mtime, keep (project, t_ms, quality) | mtime was a proxy for "the project changed." Range invalidation makes that proxy obsolete. |
| Client-side cache | Yes, mirror of backend | Eliminates LAN round-trip for revisited frames. Without it, the backend cache benefit is bounded by network latency per scrub tick. |
| Client cache sync | Server-push only (no polling) | Invalidation is the server's authoritative knowledge; polling is wasteful and lossy. Rides on unified WS. |
| Under-invalidation risk | Accept, mitigate with test harness | A catalog miss produces stale frames (subtle user-visible bug). Harness renders before/after every mutation and diffs. |

### Scope

| Decision | Choice | Rationale |
|---|---|---|
| Ordering vs task 37 | Frontend push-invalidation depends on 37 | Can ship backend half first, but client cache without push means stale frames across tabs. Better to land 37 and then this task end-to-end. |
| Backwards compat | None | Greenfield; switch cache semantics cleanly. |

---

## Notes

- Conservative fallback: when a mutation's range is hard to compute (e.g. complex curve remap), call `invalidate_project` — still better than today because unrelated writes stop triggering it.
- Watch for ordering bugs: invalidate AFTER the DB commit, not before, or you race with concurrent reads.
- Undo/redo is the trickiest case: per-op it should invalidate only the ranges the undone op touched. Carry the range forward in the undo log (cheap — just two floats per op).
- Don't optimize the O(N) range scan prematurely. At N=500 it's sub-millisecond. If the cache grows to 50k entries later, switch to an interval tree.

---

**Repositories**: `scenecraft-engine` + `scenecraft`
**Estimated Completion Date**: TBD
