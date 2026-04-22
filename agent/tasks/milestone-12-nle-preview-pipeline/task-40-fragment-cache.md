# Task 40: Backend fMP4 fragment cache

**Milestone**: [M12 - NLE-Style Preview Rendering Pipeline](../../milestones/milestone-12-nle-preview-pipeline.md)
**Design Reference**: None (design captured in the milestone doc)
**Estimated Time**: 1 day
**Dependencies**: None strictly; integrates with task-38 (range invalidation) when that lands
**Status**: Not Started
**Repository**: `scenecraft-engine`

---

## Objective

Cache encoded fMP4 media fragments on the backend keyed by `(project, t_ms_bucket, quality)`. Playback serves cached bytes directly — no re-render, no re-encode. Makes replaying a previously-played range effectively free.

---

## Context

Currently every fragment served to a WS client is produced fresh: render 48 frames → encode via ffmpeg → send. Even if the user pauses and plays the same region again, the worker re-renders everything.

The existing `FrameCache` stores JPEGs for the scrub path. This task adds a parallel cache for fMP4 fragments on the playback path.

---

## Steps

### 1. New `FragmentCache` module

`src/scenecraft/render/fragment_cache.py`:

```python
@dataclass
class _FragmentEntry:
    bytes: bytes
    duration_ms: int   # fragment represents this much content
    init_fingerprint: bytes  # first 8 bytes of init segment, for tying cached fragments to an encoder generation

CacheKey = Tuple[str, int, int]  # (project_dir_str, t0_ms_bucket, encoder_generation)

class FragmentCache:
    def __init__(self, max_fragments: int = 200, max_bytes: int = 500 * 1024 * 1024): ...
    def get(self, project_dir, t0, encoder_gen) -> bytes | None: ...
    def put(self, project_dir, t0, encoder_gen, bytes, duration_ms): ...
    def invalidate_project(self, project_dir) -> int: ...
    def invalidate_range(self, project_dir, t_start, t_end) -> int: ...
    def stats(self) -> dict: ...
```

Cache key includes an `encoder_generation` counter so a new MediaSource session (different init segment = different SPS/PPS) doesn't serve cross-generation fragments to a client that hasn't seen the matching init.

### 2. Bucket semantics

- `t0_ms_bucket = round(t0 * 1000)` (no rounding to fragment boundaries — t0 is already aligned because fragments are produced at FRAGMENT_SECONDS intervals)
- Matches the existing pattern from `FrameCache`

### 3. RenderWorker integration

In `preview_worker.py`:

- Bump `encoder_generation: int` on each `FragmentEncoder` rebuild (currently happens on seek — though recently removed; reintroduce if needed for cache key freshness)
- Before rendering a fragment, check `fragment_cache.get(project, t0, encoder_gen)`
  - Hit: put cached bytes directly onto the queue, skip render + encode entirely
  - Miss: render + encode as today, then `fragment_cache.put(...)` after encode returns
- Log hit/miss ratio per fragment

### 4. Wire range invalidation (joins with task-38)

When task-38 calls `invalidate_frames_for_mutation(project_dir, ranges)`, also call `fragment_cache.invalidate_ranges(project_dir, ranges)`. Fragment cache evicts entries whose `t0_ms_bucket` falls inside any affected range.

### 5. Cache stats endpoint

`GET /api/render-cache/stats` — extend existing endpoint to include fragment cache stats alongside frame cache.

Response shape:
```json
{
  "frame_cache": { "frames": N, "bytes": N, "hits": N, "misses": N, ... },
  "fragment_cache": { "fragments": N, "bytes": N, "hits": N, "misses": N, "max_fragments": N, "max_bytes": N }
}
```

### 6. Tests

`tests/test_fragment_cache.py`:
- get/put round trip
- LRU eviction by count
- LRU eviction by bytes
- invalidate_project drops all project entries
- invalidate_range drops only overlapping entries
- invalidate_ranges with multiple ranges in one call

---

## Verification

- [ ] First playback of a range: cache MISS per fragment (log reports misses)
- [ ] Replay of the same range: cache HIT per fragment (render + encode skipped — log shows sub-10ms fragment turnaround)
- [ ] `GET /api/render-cache/stats` reports fragment cache stats
- [ ] `invalidate_range(project, 5.0, 10.0)` drops fragments with t0 in [5.0, 10.0] and keeps others
- [ ] Total fragment cache bytes stays under `max_bytes` cap (500MB default)
- [ ] On encoder rebuild (seek / project reload), old fragments don't serve to new MediaSource (generation bump prevents cross-gen serving)

---

## Key Design Decisions

### Model

| Decision | Choice | Rationale |
|---|---|---|
| Cache level | In-memory LRU | Mirrors `FrameCache` pattern; IndexedDB / disk is a later optimization |
| Key includes encoder_generation | Yes | Prevents serving fragments with stale SPS/PPS to a new MediaSource |
| Cache size cap | 500MB / 200 fragments | ~5 hours of content at ~2.5MB/fragment for 2s at 540p preview quality |
| Invalidation | Project-wide + range-based (task-38) | Same semantics as FrameCache; code reuse |

---

## Notes

- Disk-backed fragment cache is a future extension — would survive server restart. Not here.
- Fragment cache hit-rate will show up dramatically once the background render worker (task-41) starts filling it proactively.
- If the encoder rebuilds mid-playback (seek), the new gen's cache starts empty and fills as playback continues.
