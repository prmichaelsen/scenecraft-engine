# Task 41: Background render worker

**Milestone**: [M12 - NLE-Style Preview Rendering Pipeline](../../milestones/milestone-12-nle-preview-pipeline.md)
**Design Reference**: None (design captured in the milestone doc)
**Estimated Time**: 2 days
**Dependencies**: Task 39 (proxies), Task 40 (fragment cache)
**Status**: Not Started
**Repository**: `scenecraft-engine`

---

## Objective

Proactively render uncached regions of the project in the background so that by the time the user presses play, the fragment cache is already populated. Priority: frames near the playhead first, expanding outward. Preempted by real-time playback and scrub demand so the user-facing paths always win.

---

## Context

With proxies (task-39) and fragment cache (task-40), playback-from-cache is instant. But on a cold cache, the first play of any range still has to render + encode. The background worker eliminates that cold-start penalty by filling the cache during idle time.

This is the "render in the background, show colored bars for state" behavior of every NLE.

---

## Steps

### 1. `BackgroundRenderer` class

`src/scenecraft/render/background_renderer.py`:

```python
class BackgroundRenderer:
    """One per project. Renders uncached fragments in priority order."""

    def __init__(self, project_dir: Path, schedule: Schedule, encoder: FragmentEncoder, fragment_cache: FragmentCache):
        ...

    def update_playhead(self, t: float) -> None:
        """Called by RenderWorker on play/seek. Re-prioritizes queue around new t."""

    def request_range(self, t_start: float, t_end: float, priority: int = 5) -> None:
        """External hint — e.g., user just loaded a new section. Priority 0 = highest."""

    def pause(self) -> None:
        """Stop rendering but keep queue state — resume via update_playhead."""

    def stop(self) -> None:
        """Shut down the worker thread."""

    @property
    def state(self) -> dict[str, str]:
        """Map of t0_bucket → state ('rendering' | 'cached' | 'unrendered' | 'stale').
        Used by task-42's state tracking API."""
```

### 2. Work unit = 1 fragment (aligned to FRAGMENT_SECONDS)

- Project timeline divided into buckets at `FRAGMENT_SECONDS` (2s) boundaries
- Each bucket is one work unit
- Buckets match fragment-cache keys so a rendered bucket fills a cache entry

### 3. Priority model

- Priority = distance from playhead (seconds)
- User explicit `request_range` calls get priority 0-5 (near the visible Timeline range)
- Background sweep starts at playhead ± 2s and radiates outward

### 4. Preemption

- When `preview_worker` needs a fragment for real-time playback that isn't in cache: it renders synchronously itself (existing code path). Background worker sees via the cache that this bucket is now populated and skips it.
- Conversely: when background renders a bucket and puts it in cache, the playback worker can hit cache on that bucket next time.
- Scrub: scrub HTTP path doesn't block on fragment cache — it uses the JPEG scrub cache. No direct interaction with background renderer.

### 5. Thread model

- Single-threaded per project (one BackgroundRenderer per project)
- Uses the existing render pool (task-39 introduced thread-local persistent caps) — BUT, when the playback worker is actively rendering a fragment, background renderer waits on a condition var. Two renderers competing for the pool would thrash cache warmth.
- Option: separate pool for background with fewer threads (e.g., 4 instead of 16). Let playback use the main pool.

### 6. Integration with `RenderCoordinator`

- Each worker gets a `.background_renderer` attribute, created alongside the worker
- `worker.play(t)` → `background_renderer.update_playhead(t)` + `background_renderer.resume_if_idle()`
- `worker.pause()` → `background_renderer.update_playhead(current_t)` (keeps filling around pause point)
- `worker.stop()` → `background_renderer.stop()`

### 7. Range invalidation

When task-38's `invalidate_frames_for_mutation(project, ranges)` fires:
- BackgroundRenderer re-marks the affected buckets as `unrendered` → moves them back into the priority queue
- Optional: prioritize re-render of the range that's currently being played

### 8. Throttling and back-off

- Cap background at N-1 CPU cores (leave one for everything else)
- If fragment cache is near capacity, pause background (don't thrash LRU)
- Idle eviction: if nobody has connected in IDLE_TIMEOUT_S, stop background too

### 9. Tests

`tests/test_background_renderer.py`:
- Priority queue orders buckets by distance from playhead
- Preemption: requesting a specific bucket bumps it to front
- State transitions: unrendered → rendering → cached
- Invalidation: range invalidation flips cached → unrendered
- Cooperates with real playback: playback render doesn't deadlock with background render

---

## Verification

- [ ] On project open + play: first play starts within 2s (background has pre-rendered around t=0)
- [ ] While paused: fragment cache steadily fills around the paused playhead position (verify via `GET /api/render-cache/stats` delta over time)
- [ ] Seeking to an uncached range: background worker reprioritizes; within 1-2s the seek's playback has cached fragments ahead
- [ ] Stopping the worker halts background rendering within 500ms
- [ ] On range invalidation: affected buckets drop from cache, background re-renders them in priority order
- [ ] CPU stays under N-1 cores during background work; doesn't starve playback

---

## Key Design Decisions

### Model

| Decision | Choice | Rationale |
|---|---|---|
| Work granularity | One fMP4 fragment per bucket | Matches cache key; no splitting/merging complexity |
| Priority | Distance from playhead | Intuitive; maps to "render what user is closest to wanting" |
| Preemption | Cooperative (via cache visibility) | Simpler than task cancellation; caps contention by sharing the cache |
| Pool | Separate smaller pool for background | Keeps playback from sharing warm caps with background |
| Invalidation response | Re-mark as unrendered, re-queue | Single mechanism covers edit-driven invalidation |

---

## Notes

- Background render means the user can effectively "scrub the future" — if they pause, wait 30s, the next 30s of playback is pre-rendered and instant.
- The big UX win is hidden in user perception: the timeline feels responsive because playback-from-cache is always instant, and edits only affect small colored regions.
- Future optimization: persist fragment cache to disk on worker stop; restore on start. Would make fresh-session playback near-instant too.
