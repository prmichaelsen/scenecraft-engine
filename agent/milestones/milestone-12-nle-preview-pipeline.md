# Milestone 12: NLE-Style Preview Rendering Pipeline

**Goal**: Replace the "try to render 1080p at real-time during playback" model with the industry-standard NLE pattern: proxies for fast decode, background rendering, fragment caching, visible render-state.
**Duration**: 1.5-2 weeks (6-8 days across 5 tasks)
**Dependencies**: Task-37 (unified WS), task-38 (range-based cache invalidation) — both are prereqs for the UI push layer.
**Status**: Not Started

---

## Overview

M11 shipped a functional backend-rendered preview but the "stream every playback frame in real-time from a 1080p source" design has hit a ceiling:

- Base H.264 decode saturates all 16 CPU cores at ~0.75x realtime on a 2.4h 1080p source
- Fragment render cycle ≈ 2.5-2.7s per 2s of content → persistent buffer drain → visible stutter
- No amount of parallelism helps: we're CPU-bound on cv2 decoders, not pipeline-bound

Every pro NLE (Premiere, DaVinci Resolve, Final Cut, Avid) solved this by NOT trying to render at realtime from source. They:

1. Generate **proxies** (low-res copies of source media) to cut decode cost at the root
2. Render preview **in the background** when idle, store encoded fragments
3. Show a **render-state bar** on the timeline (dark-red=unrendered, bright-red=rendering, blue=cached, dark-red-striped=stale)
4. On playback: serve cached fragments instantly. If you hit unrendered territory, the player either pauses, falls back to proxy-at-source-fps, or skips until the next cached region

This milestone brings SceneCraft to that model.

---

## Deliverables

### 1. Proxy generation + proxy-backed compositor read path (Task 39)
- Background worker generates 540p H.264 proxies for every base-track source
- Stored at `{project}/proxies/{source_hash}.mp4` (hash of source path + source mtime)
- Invalidated automatically on source mtime change
- Compositor (`_get_frame_at`) prefers proxy when available; falls back to original
- Export / final render path bypasses proxies (uses originals for quality)
- Proxy resolution configurable (default 540p; also 360p / 720p)

### 2. Backend fMP4 fragment cache (Task 40)
- In-memory cache keyed on `(project, t_ms_bucket, quality)` storing encoded fMP4 media segments
- Integrates with existing range invalidation (task-38)
- Playback serves cached fragments directly from the pump — bypassing render + encode on cache hit
- Cache stats endpoint for observability

### 3. Background render worker (Task 41)
- Proactively renders uncached regions of the timeline when the player is idle or paused
- Priority queue: frames nearest to playhead first, expanding outward
- Preempted by real-time playback fragment demand and by explicit scrub
- Work units are 1-2s time-buckets (aligns with fragment boundaries)
- Throttled to N-1 CPU cores to leave headroom for other work

### 4. Render-state tracking (Task 42)
- Per-bucket state machine: `unrendered` → `rendering` → `cached` → `stale`
- Derived from fragment cache + background worker's in-flight set
- Surfaced via `GET /api/projects/:name/render-state` returning `[{t_start, t_end, state}]`
- Live updates pushed over unified WS (task-37) as `render-state.update` messages

### 5. Timeline render-state UI bar (Task 43)
- Thin colored strip above the playhead ruler in Timeline
- Per-bucket colors:
  - **dark red** (`#7f1d1d` / tailwind `red-900`) — unrendered
  - **bright red** (`#ef4444` / `red-500`) — currently rendering
  - **blue** (`#3b82f6` / `blue-500`) — cached, ready to play
  - **dark red with stripes** — stale (edit invalidated this range; background worker will re-render)
- Subscribes to `render-state.update` WS messages
- Optional tooltip: "cached 2.3s ago", "rendering…", etc.

---

## Success Criteria

- [ ] Proxy generation completes for the oktoberfest_show_01 project in under 15 minutes
- [ ] Playback renders fragments at ≥1.5x realtime (headroom for edits mid-playback)
- [ ] Cold playback from a project with warm proxies + no fragment cache starts within 2s of pressing play
- [ ] Replaying a previously-played range is instant (served from fragment cache)
- [ ] Range edits invalidate only the affected fragments (verified via fragment cache stats delta)
- [ ] Timeline render-state bar updates live as background worker advances
- [ ] Export path unchanged — export renders from originals at full quality, ignores proxies
- [ ] Stutter eliminated at 1080p playback on the standard test machine

---

## Non-goals

- Not touching the scrub JPEG cache (already works, covered by task-38 extensions)
- Not adding GPU-accelerated decode (NVENC / VAAPI) — hardware-dependent, separate effort
- Not rebuilding export pipeline — this milestone is preview-only
- Not reworking frontend state plumbing beyond what the render-state bar needs

---

## Task Ordering

```
task-39 (proxies)         ─┐
                           ├─→ task-41 (background render)
task-40 (fragment cache)  ─┤                 ↓
                           └─→ task-42 (render-state tracking)
                                             ↓
                                         task-43 (UI bar)
                                 (also depends on task-37 for WS push)
```

Task 39 and 40 can ship in either order, independently. 41 depends on both. 42 depends on 40+41. 43 depends on 42 and the unified WS from task-37.

---

## Related

- **Task-37** (unified WebSocket) — prereq for task-43's live push
- **Task-38** (range-based cache invalidation) — integrates with task-40's fragment cache; edits trigger fragment invalidation by range
- **M11** — the existing preview streaming this milestone extends, not replaces
