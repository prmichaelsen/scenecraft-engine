# Spec: Engine Cache Invalidation

**Namespace**: local
**Version**: 1.0.0
**Created**: 2026-04-27
**Last Updated**: 2026-04-27
**Status**: Ready for Proofing

---

**Purpose**: Black-box contract for `scenecraft.render.cache_invalidation.invalidate_frames_for_mutation` — the single chokepoint every mutating REST handler calls after its DB write commits, which drops stale entries from the L1 preview frame cache and the fMP4 fragment cache, signals the `RenderCoordinator` to rebuild its schedule, and (in surgical mode) nudges the background renderer to re-enqueue buckets overlapping the edit — all under a non-fatal exception policy that must never raise into the caller's write path.

**Source**: `src/scenecraft/render/cache_invalidation.py` (full file), collaborators `render/frame_cache.py`, `render/fragment_cache.py`, `render/preview_worker.py`, `render/background_renderer.py`, and observed call-sites in `api_server.py`. Cross-referenced with audit-2 §1D unit 10.

---

## Scope

### In-Scope

- The public function `invalidate_frames_for_mutation(project_dir: Path, ranges: Iterable[tuple[float, float]] | None = None) -> tuple[int, int]`.
- Its four observable side-effects: (1) drop matching `frame_cache` entries, (2) drop matching `fragment_cache` entries, (3) call `RenderCoordinator.invalidate_project` on the project's active worker (if any), (4) call `RenderCoordinator.invalidate_ranges_in_background` (surgical mode only).
- Range normalization (materialization, filtering inverted tuples, empty-list-to-wholesale promotion).
- Non-fatal exception policy — every inner block is wrapped; the function never raises to the endpoint handler.
- Wholesale (ranges=None / effectively empty) vs surgical (explicit non-empty range list) branching, including the deliberate skip of the BG requeue step under wholesale mode.
- Return-value contract `(frames_dropped, fragments_dropped)`.

### Out-of-Scope (Non-Goals)

- **What triggers invalidation** — which endpoints call this, and with what ranges, is the dispatcher / endpoint handler spec's concern, not this one. (Current callers in `api_server.py` are reference context only.)
- Internal eviction algorithms of `frame_cache` / `fragment_cache` (lock shape, LRU policy, byte accounting) — this spec treats them as black boxes with an `invalidate_project` and `invalidate_ranges` interface.
- The `RenderCoordinator` worker lifecycle and `_background_renderer` priority-queue mechanics.
- Correctness of callers' range math (e.g., computing `(t_start, t_end)` for a transition edit). This spec only asserts how this function consumes ranges.
- CDN / HTTP-level caches, browser caches, service-worker caches — only the in-process Python caches are in scope.
- Frontend preview cache or scrub state.
- Metrics / logging emission (current implementation emits none from this function itself).
- Concurrency against other mutations (no lock held across the three side-effects — see Open Questions).

---

## Requirements

### Interface

- **R1**: Target signature is `invalidate_frames_for_mutation(working_copy, ranges: Iterable[tuple[float, float]] | None = None) -> tuple[int, int, bool]` where `working_copy` identifies the per-user working-copy partition (e.g., `session_id` or `working_copy_db_path`). Current code uses `(project_dir, ranges) -> tuple[int, int]`; this is **transitional** and migrates to the target during the per-working-copy cache refactor (see INV-7). Both forms reside in the same function, gated by a type-adapter during transition.
- **R2**: Return value is a 3-tuple `(frames_dropped, fragments_dropped, coordinator_fallback)`. `frames_dropped` and `fragments_dropped` are non-negative integers counting entries evicted from the corresponding cache during this call. `coordinator_fallback` is a bool — `True` iff a wholesale `invalidate_project` fallback was triggered by a BG requeue exception (see R18 / OQ-2). Transitional 2-tuple return `(frames_dropped, fragments_dropped)` is accepted for pre-migration callers.
- **R3**: The function is `@pure side-effect` from the caller's perspective: no exceptions propagate out under any failure mode of any collaborator. The return value remains well-formed even when every collaborator raises.

### Range Normalization

- **R4**: `ranges=None` is interpreted as **wholesale invalidation** of the project.
- **R5**: `ranges=[]` (empty iterable) is interpreted as **wholesale invalidation** of the project (promoted to None-equivalent internally).
- **R6**: A range tuple `(a, b)` with `b < a` is **silently dropped** from the range list. A range `(a, a)` (zero-width) is **kept** (`b >= a`).
- **R7**: If after filtering all supplied ranges were inverted, the remaining list is empty and the call is promoted to wholesale invalidation (same behavior as R5).
- **R8**: The materialized range list is used **identically** by both the frame-cache call and the fragment-cache call (they see the same normalized list).

### Frame Cache

- **R9**: In wholesale mode, `global_cache.invalidate_project(project_dir)` is called exactly once; its return value becomes `frames_dropped`.
- **R10**: In surgical mode, `global_cache.invalidate_ranges(project_dir, range_list)` is called exactly once with the normalized list; its return value becomes `frames_dropped`.
- **R11**: If the frame-cache import or call raises, `frames_dropped` remains `0` and execution continues to the fragment-cache block.

### Fragment Cache

- **R12**: In wholesale mode, `global_fragment_cache.invalidate_project(project_dir)` is called exactly once; its return value becomes `fragments_dropped`.
- **R13**: In surgical mode, `global_fragment_cache.invalidate_ranges(project_dir, range_list)` is called exactly once with the normalized list; its return value becomes `fragments_dropped`.
- **R14**: If the fragment-cache import or call raises, `fragments_dropped` remains `0` and execution continues to the coordinator block.

### Render Coordinator Signaling

- **R15**: `RenderCoordinator.instance().invalidate_project(project_dir)` is called exactly once in both wholesale and surgical modes (if the coordinator block does not raise earlier).
- **R16**: In surgical mode **only**, after `invalidate_project`, `coord.invalidate_ranges_in_background(project_dir, range_list)` is called exactly once with the normalized range list.
- **R17**: In wholesale mode, `invalidate_ranges_in_background` is **not** called — the BG requeue is deliberately skipped (rationale: the schedule rebuild + next play/seek will re-prime the queue cheaply; a full BG requeue would be wasteful).
- **R18**: If any exception is raised anywhere in the coordinator block (import, `instance()` lookup, `invalidate_project`, or `invalidate_ranges_in_background`), it is swallowed. The function still returns normally with the cache counts already computed.
- **R18a (BG requeue fallback)**: If `coord.invalidate_ranges_in_background` raises in surgical mode, the function falls back to `coord.invalidate_project(working_copy)` (wholesale schedule rebuild on the coordinator only — caches were already drained surgically) and sets `coordinator_fallback=True` in the return tuple. The fallback call's own exceptions are swallowed. (Closes OQ-2.)
- **R24 (negative time clip)**: `(t_start, t_end)` tuples are normalized by clipping to `[0, +inf)` at this function's boundary: any negative value is clamped to `0.0`. Caches never observe negative times. Applied before the inversion filter (R6). (Closes OQ-6.)
- **R25 (unknown working_copy / project_dir)**: Silent no-op. Caches return 0 and the coordinator no-ops. No warning logged. (Closes OQ-7.)
- **R26 (large range lists)**: No upper threshold. Caller responsibility for batch size; collaborators iterate linearly. Caller may choose to collapse many ranges into wholesale. (Closes OQ-8.)
- **R27 (per-working-copy cache partitioning — INV-7)**: Frame and fragment caches are keyed by `working_copy` (session_id / working_copy_db_path), NOT by `project_dir`. Each user's working copy has an isolated cache partition; invalidations from user A's working copy MUST NOT affect user B's working copy, even for the same project. Exception: peaks cache remains project-scoped (content-addressed; genuinely shareable). Target state; current `project_dir`-keyed caches are transitional.
- **R28 (no internal lock held across collaborators — INV-1)**: The function holds no mutex across frame → fragment → coord side-effects. Concurrent calls from different working copies on the same project interleave freely and produce correct results because each working copy has its own cache partition (R27). Concurrent calls within the same (user, project) working copy are out of scope (INV-1). (Closes OQ-5.)
- **R29 (active scrub during invalidate)**: Brief re-render at next-visible-frame; no UI flicker. Preview worker re-primes on-demand via cache miss → fresh render. No visible artifact other than a possible frame-latency spike. (Closes OQ-1.)
- **R30 (non-overlapping ranges during scrub)**: No-op on the active fragment; the currently-playing fragment's cache entry is untouched when the invalidated ranges don't overlap its span. (Closes OQ-3.)
- **R31 (wholesale invalidate during active render)**: `assemble_final` holds its schedule snapshot from t=0; coordinator signals do not abort in-flight renders. Render completes with snapshot-at-start semantics. (Closes OQ-4; cross-referenced by render-pipeline OQ-1.)

### Ordering

- **R19**: Side-effects execute in the fixed order: (1) frame-cache drop, (2) fragment-cache drop, (3) coordinator `invalidate_project`, (4) coordinator `invalidate_ranges_in_background` (surgical only).
- **R20**: A failure in step N does not prevent steps N+1..4 from running (each step is independently `try`/`except`'d per block).

### Idempotency & Null Cases

- **R21**: Calling the function when no caches contain entries for `project_dir` returns `(0, 0)` and does not raise, regardless of mode.
- **R22**: Calling the function when no worker exists for `project_dir` still succeeds and returns the cache counts; the coordinator block silently no-ops on the missing worker (the coordinator's own `invalidate_project` returns `False` and `invalidate_ranges_in_background` returns `0`, but neither return value is surfaced by this function).
- **R23**: Repeated calls with identical arguments are safe; the second call returns `(0, 0)` for the caches that were already drained by the first.

---

## Interfaces / Data Shapes

```python
def invalidate_frames_for_mutation(
    project_dir: Path,
    ranges: Iterable[tuple[float, float]] | None = None,
) -> tuple[int, int]:
    """Returns (frames_dropped, fragments_dropped). Never raises."""
```

### Collaborator surface (treated as black box)

- `scenecraft.render.frame_cache.global_cache`
  - `invalidate_project(project_dir: Path) -> int`
  - `invalidate_ranges(project_dir: Path, ranges: list[tuple[float, float]]) -> int`
- `scenecraft.render.fragment_cache.global_fragment_cache`
  - `invalidate_project(project_dir: Path) -> int`
  - `invalidate_ranges(project_dir: Path, ranges: list[tuple[float, float]]) -> int`
- `scenecraft.render.preview_worker.RenderCoordinator`
  - `instance() -> RenderCoordinator`
  - `invalidate_project(project_dir: Path) -> bool`
  - `invalidate_ranges_in_background(project_dir: Path, ranges: list[tuple[float, float]]) -> int`

### Range semantics

- Seconds, float. Closed intervals `[t_start, t_end]`.
- `t_end < t_start` → dropped (R6).
- `t_end == t_start` → kept.
- No upper bound enforced here; the caches clip internally.

---

## Behavior Table

| #  | Scenario                                                                                       | Expected Behavior                                                                                              | Tests |
|----|-----------------------------------------------------------------------------------------------|----------------------------------------------------------------------------------------------------------------|-------|
| 1  | Surgical call, one valid range, all collaborators healthy                                     | Drops matching frame + fragment entries, calls coord.invalidate_project + invalidate_ranges_in_background, returns `(F, G)` | `surgical-happy-path` |
| 2  | Surgical call, multiple valid ranges                                                          | Each collaborator receives the full normalized list in one call; counts are totals across ranges               | `surgical-multiple-ranges` |
| 3  | Wholesale call, `ranges=None`                                                                 | Calls `invalidate_project` on both caches; calls coord.invalidate_project; does NOT call invalidate_ranges_in_background | `wholesale-none` |
| 4  | Wholesale call, `ranges=[]`                                                                   | Empty list promoted to wholesale; same behavior as ranges=None                                                 | `wholesale-empty-list` |
| 5  | All ranges inverted (e.g., `[(5, 3)]`)                                                        | Filtered list is empty → promoted to wholesale                                                                 | `all-inverted-promotes-to-wholesale` |
| 6  | Mixed valid + inverted ranges (e.g., `[(1, 2), (5, 3), (7, 9)]`)                              | Inverted dropped; surgical call proceeds with `[(1,2), (7,9)]`                                                 | `mixed-inverted-filtered` |
| 7  | Zero-width range `(a, a)`                                                                     | Kept; passed through to caches; surgical mode retained                                                         | `zero-width-kept` |
| 8  | Frame cache raises on import or call                                                          | `frames_dropped=0`; fragment cache + coord still run; no exception propagates                                  | `frame-cache-raises-non-fatal` |
| 9  | Fragment cache raises                                                                         | `fragments_dropped=0`; frame cache result preserved; coord still runs; no exception propagates                 | `fragment-cache-raises-non-fatal` |
| 10 | RenderCoordinator.instance() or invalidate_project raises                                     | Both cache drops already applied and returned; no exception propagates                                         | `coord-raises-non-fatal` |
| 11 | `invalidate_ranges_in_background` raises (surgical mode)                                      | Return tuple still reflects cache drops; exception swallowed                                                   | `coord-bg-requeue-raises-non-fatal` |
| 12 | All collaborators raise simultaneously                                                        | Returns `(0, 0)`; no exception propagates                                                                      | `all-collaborators-raise` |
| 13 | No worker exists for `project_dir`                                                            | Coordinator block no-ops silently; cache drops still applied; return tuple valid                               | `no-active-worker` |
| 14 | Caches already empty for `project_dir`                                                        | Returns `(0, 0)`; no exception                                                                                 | `empty-caches-returns-zero` |
| 15 | Repeated identical invocation                                                                 | Second call returns `(0, 0)` (entries already drained); no exception                                           | `idempotent-on-repeat` |
| 16 | Side-effect ordering                                                                          | frame-cache → fragment-cache → coord.invalidate_project → coord.invalidate_ranges_in_background (surgical)     | `ordering-fixed` |
| 17 | Range list materialized once                                                                  | Frame cache and fragment cache receive the identical (equal) list object contents                              | `normalized-list-shared-across-caches` |
| 18 | Wholesale mode skips BG requeue deliberately                                                  | `invalidate_ranges_in_background` is not called when ranges was None or empty                                  | `wholesale-skips-bg-requeue` |
| 19 | Return type is always a 2-tuple of ints, even on full failure                                 | `(0, 0)` on total collaborator failure; always ints, never None                                                | `return-type-invariant` |
| 20 | Generator/iterator passed as `ranges` (e.g., `(r for r in [...])`)                            | Consumed exactly once during normalization; both cache calls see the materialized list                         | `iterator-consumed-once` |
| 21 | Invalidate called while a scrub/seek is in flight for an overlapping time                     | Brief re-render at next-visible-frame; no UI flicker; preview worker re-primes on-demand (R29)                 | `scrub-overlap-re-renders-no-flicker` |
| 22 | BG requeue fails but caches were drained — cache vs DB consistency                            | Fall back to wholesale `coord.invalidate_project`; return tuple `coordinator_fallback=True` (R18a)             | `bg-requeue-raises-wholesale-fallback` |
| 23 | Invalidate with ranges that do not overlap current scrub position                             | No-op on active fragment; the currently-playing fragment's cache entry is untouched (R30)                      | `scrub-non-overlapping-no-op-on-active-fragment` |
| 24 | Wholesale invalidate during an active playback/export render                                  | Render completes with snapshot-at-start semantics; coord signals do not abort in-flight renders (R31)          | `wholesale-during-render-snapshot-semantics` |
| 25 | Concurrent invalidations from two mutating endpoints for the same project                     | Different working copies: interleave freely, isolated per-partition (R27, INV-1). Same (user, project): out of scope (INV-1) | `concurrent-invalidates-different-working-copies-isolated`, `no-internal-lock-across-collaborators` |
| 26 | Negative `t_start` / `t_end` values                                                           | Clipped to `max(0, t)` at function boundary; caches never see negative times (R24)                             | `negative-times-clipped-to-zero` |
| 27 | `working_copy` / `project_dir` that was never opened / non-existent                            | Silent no-op; caches return 0, coord no-ops; no warning logged (R25)                                           | `unknown-target-silent-noop` |
| 28 | Very large range list (e.g., 1000 tuples from a batch edit)                                   | Accepted without threshold; linear iteration; caller responsibility for batch size (R26)                       | `large-range-list-1000-accepted` |
| 29 | Call with per-working-copy partition key (target signature)                                   | Frame + fragment caches scoped to the working copy; another working copy's cache for same project is untouched (R27, INV-7) | `working-copy-cache-partition-isolation` |

---

## Behavior

Step-by-step execution path.

1. **Enter.** Function receives `project_dir` (Path) and `ranges` (iterable of `(float, float)` tuples, or `None`).
2. **Initialize counters.** `frames_dropped = 0`, `fragments_dropped = 0`.
3. **Normalize ranges.**
   - If `ranges is None`, set `range_list = None`.
   - Else materialize `rl = [(a, b) for (a, b) in ranges if b >= a]` (consumes any iterator exactly once).
   - If `rl` is empty, set `range_list = None` (wholesale promotion).
   - Else `range_list = rl`.
4. **Frame cache block.** Inside a `try/except: pass`:
   - Import `global_cache` from `scenecraft.render.frame_cache`.
   - If `range_list is None`, `frames_dropped = global_cache.invalidate_project(project_dir)`.
   - Else `frames_dropped = global_cache.invalidate_ranges(project_dir, range_list)`.
5. **Fragment cache block.** Inside a `try/except: pass`:
   - Import `global_fragment_cache` from `scenecraft.render.fragment_cache`.
   - If `range_list is None`, `fragments_dropped = global_fragment_cache.invalidate_project(project_dir)`.
   - Else `fragments_dropped = global_fragment_cache.invalidate_ranges(project_dir, range_list)`.
6. **Coordinator block.** Inside a single `try/except: pass`:
   - Import `RenderCoordinator` from `scenecraft.render.preview_worker`.
   - `coord = RenderCoordinator.instance()`.
   - `coord.invalidate_project(project_dir)` — signals any live worker to rebuild its schedule on the next fragment cycle.
   - If `range_list is not None`: `coord.invalidate_ranges_in_background(project_dir, range_list)` — nudges the BG renderer to re-enqueue overlapping buckets.
   - If `range_list is None`: **skip** the BG requeue.
7. **Return** `(frames_dropped, fragments_dropped)`.

At no point does any exception propagate out of the function.

---

## Acceptance Criteria

- [ ] Function never raises under any combination of collaborator failures (verified by `all-collaborators-raise`).
- [ ] Wholesale mode triggered by both `ranges=None` and `ranges=[]` and by all-inverted filtered-to-empty lists.
- [ ] Wholesale mode skips `invalidate_ranges_in_background` (verified by `wholesale-skips-bg-requeue`).
- [ ] Surgical mode passes the same normalized list to both caches and to the BG requeue.
- [ ] Inverted tuples (`b < a`) are dropped; zero-width (`b == a`) are kept.
- [ ] Return value is always `tuple[int, int]`, defaulting to `0` for any cache whose block raised.
- [ ] Side-effects occur in the documented order (frame → fragment → coord.invalidate_project → coord.invalidate_ranges_in_background).
- [ ] Each of the three exception-protected blocks (frame, fragment, coord) is independent — a failure in one does not short-circuit the others.
- [ ] Coordinator block's failure does not affect the return value.
- [ ] Every `undefined` row in the Behavior Table is either resolved and moved to a concrete test, or explicitly accepted as `undefined` by the proofing reviewer.

---

## Tests

### Base Cases

The core behavior contract: happy path, wholesale vs surgical branching, non-fatal exception policy, return-value invariants.

#### Test: surgical-happy-path (covers R1, R2, R8, R10, R13, R15, R16, R19)

**Given**:
- A project with cached frames at t=1.0, 2.0, 3.0 and cached fragments at t0=1.0, 2.0.
- A live `RenderCoordinator` worker for the project.
- Collaborators healthy (no exceptions).

**When**: Call `invalidate_frames_for_mutation(project_dir, [(1.5, 2.5)])`.

**Then** (assertions):
- **calls-frame-ranges-once**: `frame_cache.global_cache.invalidate_ranges` is called exactly once with `(project_dir, [(1.5, 2.5)])`.
- **calls-fragment-ranges-once**: `fragment_cache.global_fragment_cache.invalidate_ranges` is called exactly once with `(project_dir, [(1.5, 2.5)])`.
- **calls-coord-invalidate-project**: `RenderCoordinator.instance().invalidate_project` is called exactly once with `project_dir`.
- **calls-coord-bg-requeue**: `RenderCoordinator.instance().invalidate_ranges_in_background` is called exactly once with `(project_dir, [(1.5, 2.5)])`.
- **returns-tuple-of-ints**: Return value is `(frame_count, fragment_count)` where both are the `int` return values of the respective `invalidate_ranges` calls.
- **does-not-call-invalidate-project-on-caches**: Neither cache's `invalidate_project` is called.

#### Test: surgical-multiple-ranges (covers R8, R10, R13)

**Given**: A project with diverse cached frames/fragments.

**When**: Call with `[(1.0, 2.0), (5.0, 6.0), (9.0, 10.0)]`.

**Then**:
- **frame-sees-full-list**: Frame cache receives the full 3-tuple list in one call.
- **fragment-sees-full-list**: Fragment cache receives the full 3-tuple list in one call.
- **bg-requeue-sees-full-list**: `invalidate_ranges_in_background` receives the full 3-tuple list in one call.
- **counts-are-sums**: Return tuple sums drops across all ranges (exact sum equals collaborator return values).

#### Test: wholesale-none (covers R3, R4, R9, R12, R15, R17)

**Given**: Caches contain entries for the project across many times.

**When**: Call `invalidate_frames_for_mutation(project_dir, ranges=None)`.

**Then**:
- **calls-frame-invalidate-project**: `global_cache.invalidate_project(project_dir)` is called exactly once.
- **calls-fragment-invalidate-project**: `global_fragment_cache.invalidate_project(project_dir)` is called exactly once.
- **calls-coord-invalidate-project**: Coordinator's `invalidate_project` is called exactly once.
- **no-bg-requeue**: `invalidate_ranges_in_background` is NOT called.
- **no-ranges-calls**: Neither cache's `invalidate_ranges` method is called.

#### Test: wholesale-empty-list (covers R5, R7)

**Given**: Caches populated.

**When**: Call with `ranges=[]`.

**Then**:
- **promotes-to-wholesale**: Behavior is identical to `ranges=None` (all assertions of `wholesale-none` hold).

#### Test: all-inverted-promotes-to-wholesale (covers R6, R7)

**Given**: Caches populated.

**When**: Call with `ranges=[(5.0, 3.0), (10.0, 8.0)]`.

**Then**:
- **promotes-to-wholesale**: Identical to `wholesale-none`.
- **no-surgical-calls**: Neither `invalidate_ranges` method nor `invalidate_ranges_in_background` is called.

#### Test: mixed-inverted-filtered (covers R6)

**Given**: Caches populated.

**When**: Call with `ranges=[(1.0, 2.0), (5.0, 3.0), (7.0, 9.0)]`.

**Then**:
- **inverted-dropped**: Frame cache `invalidate_ranges` receives exactly `[(1.0, 2.0), (7.0, 9.0)]`.
- **fragment-matches**: Fragment cache `invalidate_ranges` receives exactly `[(1.0, 2.0), (7.0, 9.0)]`.
- **bg-requeue-matches**: `invalidate_ranges_in_background` receives exactly `[(1.0, 2.0), (7.0, 9.0)]`.

#### Test: zero-width-kept (covers R6)

**Given**: Caches populated; fragment at exactly t=4.0.

**When**: Call with `ranges=[(4.0, 4.0)]`.

**Then**:
- **zero-width-passed-through**: Frame cache receives `[(4.0, 4.0)]`.
- **fragment-gets-zero-width**: Fragment cache receives `[(4.0, 4.0)]`.
- **surgical-mode-retained**: `invalidate_ranges_in_background` is called (not skipped).

#### Test: frame-cache-raises-non-fatal (covers R3, R11, R20)

**Given**: `frame_cache.global_cache.invalidate_ranges` raises `RuntimeError`.

**When**: Call `invalidate_frames_for_mutation(project_dir, [(1.0, 2.0)])`.

**Then**:
- **no-propagation**: The call returns normally (no exception).
- **frames-dropped-is-zero**: Return tuple's first element is `0`.
- **fragment-still-runs**: Fragment cache `invalidate_ranges` is still called.
- **coord-still-runs**: Coordinator `invalidate_project` is still called.
- **bg-requeue-still-runs**: `invalidate_ranges_in_background` is still called.

#### Test: fragment-cache-raises-non-fatal (covers R3, R14, R20)

**Given**: Frame cache healthy (returns 3); fragment cache `invalidate_ranges` raises.

**When**: Call with a surgical range.

**Then**:
- **no-propagation**: No exception raised.
- **frames-dropped-preserved**: Return tuple's first element equals frame cache's return (3).
- **fragments-dropped-is-zero**: Return tuple's second element is `0`.
- **coord-still-runs**: Coordinator calls still occur.

#### Test: coord-raises-non-fatal (covers R3, R18, R20)

**Given**: Both caches healthy; `RenderCoordinator.instance()` raises.

**When**: Call with a surgical range.

**Then**:
- **no-propagation**: No exception.
- **cache-drops-preserved**: Return tuple reflects both caches' actual return values.
- **no-bg-requeue**: `invalidate_ranges_in_background` is not called (coord block errored first).

#### Test: coord-bg-requeue-raises-non-fatal (covers R3, R18)

**Given**: Caches healthy; `coord.invalidate_project` succeeds; `coord.invalidate_ranges_in_background` raises.

**When**: Call with a surgical range.

**Then**:
- **no-propagation**: No exception.
- **cache-drops-preserved**: Return tuple correct.
- **invalidate-project-was-called**: `coord.invalidate_project` was still called (it ran before the failure).

#### Test: all-collaborators-raise (covers R3, R11, R14, R18, R19)

**Given**: Every collaborator (both caches, coordinator) raises.

**When**: Call with any arguments.

**Then**:
- **returns-zero-zero**: Return value is `(0, 0)`.
- **no-propagation**: No exception raised.

#### Test: no-active-worker (covers R22)

**Given**: Caches have entries for the project; no `RenderCoordinator` worker exists for `project_dir`.

**When**: Call with a surgical range.

**Then**:
- **cache-drops-happen**: Return tuple reflects cache drops.
- **no-exception**: No exception raised.
- **coord-calls-silent-noop**: Coordinator's `invalidate_project` returns `False` and `invalidate_ranges_in_background` returns `0`, but neither value is surfaced.

#### Test: empty-caches-returns-zero (covers R21)

**Given**: Caches are empty for `project_dir`; coordinator healthy.

**When**: Call with any arguments (surgical or wholesale).

**Then**:
- **returns-zero-zero**: Return value is `(0, 0)`.
- **no-exception**: No exception raised.

#### Test: return-type-invariant (covers R2, R3)

**Given**: Any combination of collaborator states.

**When**: Call the function.

**Then**:
- **is-tuple-of-ints**: Return value is always `tuple[int, int]`, never `None`, never a single int, never mixed types.
- **non-negative**: Both elements are `>= 0`.

### Edge Cases

Boundaries, iterator consumption, ordering, idempotency, and explicitly-flagged `undefined` scenarios carried as Open Questions.

#### Test: ordering-fixed (covers R19, R20)

**Given**: All collaborators instrumented to record call order.

**When**: Call with a surgical range.

**Then**:
- **order-sequence**: Calls occur in the order: `frame_cache.invalidate_ranges`, `fragment_cache.invalidate_ranges`, `coord.invalidate_project`, `coord.invalidate_ranges_in_background`.
- **order-holds-on-wholesale**: For wholesale calls, order is: `frame_cache.invalidate_project`, `fragment_cache.invalidate_project`, `coord.invalidate_project` (and BG requeue absent).

#### Test: normalized-list-shared-across-caches (covers R8, R17)

**Given**: A generator expression passed as `ranges` that yields `[(1.0, 2.0), (3.0, 4.0)]`.

**When**: Call the function.

**Then**:
- **frame-cache-receives-materialized-list**: Frame cache's `invalidate_ranges` is called with a list (not an iterator), equal to `[(1.0, 2.0), (3.0, 4.0)]`.
- **fragment-cache-receives-equal-list**: Fragment cache receives a list equal to the same contents.
- **bg-requeue-receives-equal-list**: Coordinator BG requeue receives a list equal to the same contents.

#### Test: iterator-consumed-once (covers R8)

**Given**: A generator that raises `StopIteration` on second consumption (i.e., a standard generator).

**When**: Call the function with that generator as `ranges`.

**Then**:
- **no-exception-from-reuse**: Function does not raise.
- **all-collaborators-see-same-contents**: Frame, fragment, and BG requeue each receive the same materialized list (proving the iterator was consumed exactly once into a list that was reused).

#### Test: wholesale-skips-bg-requeue (covers R17)

**Given**: Caches populated; coordinator healthy.

**When**: Call with `ranges=None`.

**Then**:
- **bg-requeue-not-called**: `coord.invalidate_ranges_in_background` is NOT called.
- **invalidate-project-called**: `coord.invalidate_project` IS called.

#### Test: scrub-overlap-re-renders-no-flicker (covers R29)

**Given**: A preview worker is actively serving a frame for t=2.0; caches contain the frame at t=2.0.

**When**: `invalidate_frames_for_mutation(target, [(1.5, 2.5)])` fires.

**Then**:
- **cache-entry-removed**: The t=2.0 frame is evicted from `frame_cache`.
- **next-visible-frame-re-renders**: The next scrub request at t=2.0 triggers a fresh render (cache miss).
- **no-ui-flicker-contract**: The spec documents the preview worker as responsible for re-priming; no mid-stream abort is signaled.

#### Test: bg-requeue-raises-wholesale-fallback (covers R18a, OQ-2)

**Given**: Surgical range; caches healthy; `coord.invalidate_project` succeeds on first call; `coord.invalidate_ranges_in_background` raises.

**When**: Handler runs.

**Then**:
- **fallback-invoked**: `coord.invalidate_project` is called a second time as the wholesale fallback (or equivalent fallback call).
- **return-fallback-true**: Return tuple's `coordinator_fallback` field is `True`.
- **no-propagation**: No exception propagates.
- **cache-drops-preserved**: Surgical frame/fragment counts intact.

#### Test: scrub-non-overlapping-no-op-on-active-fragment (covers R30)

**Given**: Active scrub at t=2.0; cached fragment span [1.0, 3.0]; invalidation ranges `[(10.0, 12.0)]` (non-overlapping).

**When**: `invalidate_frames_for_mutation` fires.

**Then**:
- **active-fragment-untouched**: The cached fragment for [1.0, 3.0] is NOT evicted.
- **no-scrub-impact**: No evidence of re-render triggered for the active scrub.

#### Test: wholesale-during-render-snapshot-semantics (covers R31)

**Given**: A render (`assemble_final`) is actively streaming frames from a schedule snapshot taken at t=0; wholesale invalidate fires mid-render.

**When**: The render continues.

**Then**:
- **render-completes**: Render reaches end of schedule without abort.
- **uses-snapshot-schedule**: Output frames derive from the t=0 schedule snapshot (not from post-invalidate DB state).
- **no-render-abort-signal**: Coordinator does not signal an abort to the in-flight render.

#### Test: concurrent-invalidates-different-working-copies-isolated (covers R27, R28, INV-1)

**Given**: Two working copies A and B both have cached entries for the same project; both fire invalidate simultaneously (different partition keys).

**When**: Both complete.

**Then**:
- **a-partition-drained**: A's cache partition is empty for the project.
- **b-partition-untouched-by-a**: B's cache partition still contains B's entries (isolation).
- **no-cross-contamination**: Neither call evicts the other working copy's entries.

#### Test: no-internal-lock-across-collaborators (negative — INV-1)

**Given**: Inspection of `invalidate_frames_for_mutation` source.

**When**: Inspected.

**Then**:
- **no-threading-lock**: No `threading.Lock`, `asyncio.Lock`, or file lock is acquired across the three side-effects.
- **no-per-project-mutex**: No module-level `_project_locks: dict[Path, Lock]` pattern.

#### Test: negative-times-clipped-to-zero (covers R24)

**Given**: `ranges=[(-1.5, 2.0), (3.0, -0.5), (-2.0, -1.0)]`.

**When**: Handler runs.

**Then**:
- **first-clipped**: Frame cache receives `(0.0, 2.0)` for the first tuple.
- **inversion-after-clip**: `(3.0, -0.5)` becomes `(3.0, 0.0)` which is inverted and DROPPED.
- **all-negative-after-clip**: `(-2.0, -1.0)` becomes `(0.0, 0.0)` — kept (zero-width).
- **no-negative-values-in-collaborator-args**: Inspecting calls, no negative float ever reaches either cache.

#### Test: unknown-target-silent-noop (covers R25)

**Given**: A `project_dir` / `working_copy` that was never opened.

**When**: Handler runs with any arguments.

**Then**:
- **returns-zero-counts**: Frame + fragment counts are `0`.
- **no-warning-logged**: No logging emitted by this function.
- **no-exception**: No exception raised.

#### Test: large-range-list-1000-accepted (covers R26)

**Given**: `ranges` = 1000 distinct non-overlapping tuples.

**When**: Handler runs.

**Then**:
- **frame-receives-1000**: Frame cache `invalidate_ranges` receives a list of length 1000.
- **fragment-receives-1000**: Fragment cache receives a list of length 1000.
- **bg-requeue-receives-1000**: Coordinator BG requeue receives a list of length 1000.
- **no-exception-from-size**: No size-related rejection.

#### Test: working-copy-cache-partition-isolation (covers R27, INV-7)

**Given**: Target signature in use; working copy A and working copy B both have entries cached for the same project_dir under different partition keys.

**When**: `invalidate_frames_for_mutation(working_copy_A, [(1.0, 2.0)])`.

**Then**:
- **a-entries-evicted**: A's cache entries in [1.0, 2.0] are gone.
- **b-entries-untouched**: B's cache entries in [1.0, 2.0] for the same project_dir remain.
- **peaks-cache-unchanged**: Peaks cache (project-scoped by design) is untouched (not under this function's scope, but test asserts no collateral eviction).

#### Test: idempotent-on-repeat (covers R23)

**Given**: Caches populated; call once with `[(1.0, 2.0)]` (drains matching entries).

**When**: Call again with `[(1.0, 2.0)]`.

**Then**:
- **second-call-returns-zero-zero**: Second return is `(0, 0)` (caches already drained).
- **no-exception**: No exception on second call.
- **coord-still-signaled**: `coord.invalidate_project` still called on second call (this is correct — the signal is idempotent-safe).

---

## Non-Goals

- Validating caller-supplied ranges against project duration or against known cached extents (no rejection of "nonsensical" ranges — the caches clip internally).
- Atomicity across cache + coordinator steps (no lock spans the three side-effects; interleaving with concurrent mutations is not addressed by this function — see OQ-5).
- Reporting which specific cache keys were evicted (only aggregate counts).
- Reporting coordinator signaling success in the return value (it is intentionally dropped).
- Emitting telemetry, metrics, or logs from this function (current implementation emits none; any future logging is a separate concern).
- Integration with HTTP-layer / CDN / frontend caches.
- Providing a sync-vs-async variant; function is synchronous and fast by design (short locks inside collaborators).

---

## Open Questions

### Resolved

- **OQ-1 (invalidate during active scrub — flicker?)**: **Resolved 2026-04-27 as codified**. Brief re-render at next-visible-frame; no UI flicker; preview worker re-primes on-demand (R29).
- **OQ-2 (BG requeue silent failure — cache/DB drift)**: **Resolved 2026-04-27**. Fall back to wholesale `coord.invalidate_project` on BG requeue exception; return tuple gains `coordinator_fallback: bool` third field (R18a, R2).
- **OQ-3 (non-overlapping ranges during scrub)**: **Resolved 2026-04-27 as codified**. No-op on active fragment (R30).
- **OQ-4 (wholesale invalidate during active render)**: **Resolved 2026-04-27 as codified**. Render holds schedule snapshot from t=0 (R31). Cross-ref render-pipeline OQ-1.
- **OQ-5 (concurrent invalidations)**: **Resolved 2026-04-27**. Closed under INV-1 single-writer per (user, project). Different working copies interleave safely per R27 partition isolation; same (user, project) out of scope. Negative-assertion test `no-internal-lock-across-collaborators`.
- **OQ-6 (negative time values)**: **Resolved 2026-04-27**. Clip to `max(0, t)` at function boundary (R24).
- **OQ-7 (unknown project_dir)**: **Resolved 2026-04-27 as codified**. Silent no-op (R25).
- **OQ-8 (very large range lists)**: **Resolved 2026-04-27 as codified**. Accepted without threshold (R26).
- **INV-7 (per-working-copy cache partitioning)**: Codified via R27, R28, R1 target signature migration. Current `project_dir`-keyed caches transitional; migration to `working_copy` keying tracked as implementation follow-up.

### Open (none remaining)

- **OQ-1 (Behavior Table #21)**: **Invalidate during active scrub — does preview flicker?** When a scrub/seek request is mid-flight (HTTP handler currently computing or streaming a frame for an overlapping time) and `invalidate_frames_for_mutation` fires for the same range, what does the user observe? Possible answers: (a) scrub completes with stale pixels (no visible flicker until next scrub); (b) scrub completes with fresh pixels because re-render is triggered by the schedule rebuild mid-stream; (c) scrub fails / returns an error; (d) brief visible flicker as cache misses force a re-render. Not determinable from this function's code alone — depends on preview-worker scrub orchestration. Needs a design decision or an integration-level spec.
- **OQ-2 (Behavior Table #22)**: **BG requeue failure — cache vs DB drift.** If `invalidate_ranges_in_background` raises (swallowed by R18), the caches have been drained but the BG renderer queue has NOT been updated. Is the cache now inconsistent with the DB? Technically no: the cache is simply emptier than the DB; subsequent play/seek will re-render from the (already-correct) DB state on demand. But is this acceptable? Or should we fall back to a wholesale `invalidate_project` on the coordinator? Needs confirmation from the preview-worker owner.
- **OQ-3 (Behavior Table #23)**: **Invalidate with non-overlapping ranges during scrub.** If the edit's range does not overlap the current scrub position, is there any observable effect on the active scrub? Expected answer: none — the currently-playing fragment's cache entry is untouched. Needs confirmation plus a test that asserts no-op on the active fragment.
- **OQ-4 (Behavior Table #24)**: **Wholesale invalidate during active render.** If a bounce/export render is running and a wholesale invalidate fires (e.g., undo/redo), does the render abort, produce corrupt output, produce correct output slowly, or ignore the signal? The coordinator's `invalidate_project` signals schedule rebuild — export may or may not listen. Needs a decision from the bounce-pipeline owner.
- **OQ-5**: **Concurrent invalidations.** Two mutating endpoints commit DB writes for the same project within milliseconds of each other and both call `invalidate_frames_for_mutation`. Under the GIL the two calls serialize, but the `RenderCoordinator.instance()` lookup and worker state could interleave with a seek from a third thread. Is a higher-level mutex needed (e.g., per-project invalidation serializer)? Current design: no — collaborators are individually thread-safe and the signal is idempotent. Needs confirmation.
- **OQ-6**: **Negative time values.** Does a `(t_start, t_end)` tuple with negative floats raise inside the caches, or clip to 0, or silently drop? Caches accept them (math works), but semantics unclear. Define: clip to `max(0, t)` at this function's layer? Or pass through?
- **OQ-7**: **Unknown project_dir.** If `project_dir` is a Path that was never opened (typo, deleted project), caches return 0 and the coordinator no-ops. No error. Is this correct, or should the function log a warning? Current policy: silent. Needs confirmation.
- **OQ-8**: **Very large range lists.** A batch edit could produce hundreds of ranges (e.g., "delete all muted clips"). Is there a size threshold above which the caller should escalate to wholesale? Currently no threshold — caches iterate linearly. Not a correctness concern, but potentially a latency concern inside locks.

---

## Related Artifacts

- Source file: `src/scenecraft/render/cache_invalidation.py`
- Collaborators:
  - `src/scenecraft/render/frame_cache.py` — `global_cache`, `invalidate_project`, `invalidate_ranges`
  - `src/scenecraft/render/fragment_cache.py` — `global_fragment_cache`, `invalidate_project`, `invalidate_ranges`
  - `src/scenecraft/render/preview_worker.py` — `RenderCoordinator.instance`, `invalidate_project`, `invalidate_ranges_in_background`
  - `src/scenecraft/render/background_renderer.py` — `invalidate_range` (called by coordinator's BG requeue path)
- Callers (reference only — out of scope for this spec):
  - `src/scenecraft/api_server.py:3336` — transition-trim update (surgical, union of old + new spans)
  - `src/scenecraft/api_server.py:9836` — M13 track-wide invalidation (surgical, clip min/max span)
  - `src/scenecraft/api_server.py:9850` — M13 curve-range invalidation (surgical, ±0.5s margin)
  - `src/scenecraft/api_server.py:9872` — M13 project-wide invalidation (wholesale)
- Audit reference: `agent/reports/audit-2-architectural-deep-dive.md` §1D Render Pipeline, unit 10.
- Related spec (future): the mutating-endpoint dispatcher spec, which will define WHO calls this function and with WHAT ranges. This spec's Non-Goals explicitly defers that.

---

**Namespace**: local
**Spec**: engine-cache-invalidation
**Version**: 1.0.0
**Created**: 2026-04-27
**Status**: Ready for Proofing
