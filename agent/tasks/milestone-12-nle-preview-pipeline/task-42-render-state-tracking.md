# Task 42: Render-state tracking

**Milestone**: [M12 - NLE-Style Preview Rendering Pipeline](../../milestones/milestone-12-nle-preview-pipeline.md)
**Design Reference**: None (design captured in the milestone doc)
**Estimated Time**: 1 day
**Dependencies**: Task 40 (fragment cache), Task 41 (background renderer)
**Status**: Not Started
**Repository**: `scenecraft-engine`

---

## Objective

Expose the current render state of every timeline bucket as a queryable API + live push, so the frontend can render the NLE-style colored state bar. State derived from fragment cache + background renderer's queue — no new source of truth.

---

## Context

Tasks 40 and 41 produce the primitives (cached / rendering / invalidated). This task surfaces them as a coherent view for the UI.

State machine per bucket:
- `unrendered` — no cache entry, not currently rendering
- `rendering` — background or playback worker is currently producing this bucket
- `cached` — fragment cache has a fresh entry
- `stale` — had a cache entry, invalidated by an edit (task-38), not yet re-rendered

---

## Steps

### 1. `RenderStateView` aggregator

`src/scenecraft/render/render_state.py`:

```python
BucketState = Literal["unrendered", "rendering", "cached", "stale"]

@dataclass
class BucketEntry:
    t_start: float
    t_end: float
    state: BucketState
    updated_at: float  # monotonic timestamp, last transition

class RenderStateView:
    """Project-scoped. Derives bucket states from fragment cache + background renderer."""

    def __init__(self, project_dir: Path, schedule: Schedule,
                 fragment_cache: FragmentCache, background: BackgroundRenderer):
        ...

    def snapshot(self) -> list[BucketEntry]:
        """Full project state, one entry per FRAGMENT_SECONDS bucket."""

    def get(self, t: float) -> BucketState:
        """State of the bucket containing `t`."""

    def subscribe(self, listener: Callable[[list[BucketEntry]], None]) -> Callable[[], None]:
        """Delta subscription — called with changed buckets since last call.
        Returns an unsubscribe function."""
```

### 2. Bucket-change event stream

- Background renderer calls `render_state.bucket_changed(t_start, t_end, new_state)` on every transition
- Fragment cache calls it on put/evict/invalidate
- RenderStateView aggregates and deduplicates within a coalescing window (~100ms) before emitting

### 3. HTTP snapshot endpoint

`GET /api/projects/:name/render-state` →
```json
{
  "bucket_seconds": 2.0,
  "duration_seconds": 8678.17,
  "buckets": [
    {"t_start": 0.0, "t_end": 2.0, "state": "cached"},
    {"t_start": 2.0, "t_end": 4.0, "state": "rendering"},
    {"t_start": 4.0, "t_end": 6.0, "state": "unrendered"},
    ...
  ]
}
```

On a 2.4h project with 2s buckets, that's ~4340 entries. At ~30 bytes each → ~130KB payload. Acceptable for the initial snapshot. Compressed over HTTP (gzip) it's much smaller.

### 4. WS push updates (requires task-37)

Message type: `render-state.update`:
```json
{
  "type": "render-state.update",
  "project": "oktoberfest_show_01",
  "changes": [
    {"t_start": 4.0, "t_end": 6.0, "state": "rendering"},
    {"t_start": 2.0, "t_end": 4.0, "state": "cached"}
  ]
}
```

Only the changed buckets, not the full snapshot. Client applies deltas to its local state map.

Integration with task-37's unified WS: register a dispatcher for `render-state.subscribe` / `render-state.unsubscribe` messages per project. On subscribe, send current snapshot inline; stream deltas thereafter.

### 5. Performance considerations

- Snapshot generation is O(N buckets) — for a 2.4h project at 2s buckets that's ~4340 buckets. In-memory operation, sub-millisecond.
- Bucket state changes should coalesce — rendering → cached transition often happens quickly; emit both transitions batched within a ~100ms window

### 6. Tests

`tests/test_render_state.py`:
- Snapshot returns one entry per bucket across full duration
- Cache put → bucket transitions to `cached`
- Cache evict/invalidate → `stale` (if evicted because of mutation) or `unrendered` (if evicted by LRU)
- Background render start → `rendering`
- Subscriber receives only changed buckets on subsequent calls

---

## Verification

- [ ] `GET /api/projects/:name/render-state` returns correct bucket-level state for a project with mixed cached + unrendered regions
- [ ] After editing a keyframe in the middle of the project: affected buckets' state in snapshot changes to `stale` within ~100ms
- [ ] Background renderer running: buckets visibly progress from `unrendered` → `rendering` → `cached` over time
- [ ] WS subscribers receive only deltas, not full snapshots, after initial
- [ ] Snapshot generation for a 2.4h project completes in under 10ms
- [ ] State is accurate across multiple concurrent WS subscribers

---

## Key Design Decisions

### Model

| Decision | Choice | Rationale |
|---|---|---|
| State source | Derived from fragment cache + background renderer | Single source of truth, no new state to sync |
| Bucket size | = FRAGMENT_SECONDS (2s) | Matches fragment cache granularity; 1:1 mapping |
| Snapshot on HTTP + deltas on WS | Both | HTTP snapshot for initial load/reconnect; WS deltas for live updates |
| Change coalescing | ~100ms window | Prevents WS spam when cache populates rapidly |
| Stale distinguished from unrendered | Yes | UI shows dark-red-striped for stale; user knows "I edited this" |

---

## Notes

- Future: per-quality dimension (cached at what CRF?). Skipping for now — one quality preset for preview.
- Thread safety: RenderStateView's snapshot method grabs locks on fragment cache + background renderer briefly. Short enough to not contend.
- Memory: the full bucket list is regenerated from current state each snapshot — we don't store a persistent copy. Simplifies invalidation.
