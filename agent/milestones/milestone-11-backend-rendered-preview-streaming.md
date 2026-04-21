# Milestone 11: Backend-Rendered Preview Streaming

**Goal**: Replace the WebGL preview compositor with a Python-based backend renderer that serves scrub frames on demand and streams playback via MSE.
**Duration**: 2-3 weeks (11-18 days across 5 PRs)
**Dependencies**: None (task-31 api_server split is related but can land in parallel)
**Status**: In Progress (2 of 5 tasks completed)

---

## Overview

The WebGL preview shader hits performance/memory ceilings under complex multi-layer compositing — too many overlay tracks, strobe effects, adjustment layers — and has correctness bugs that the final backend export doesn't (strobe desync, adjustment layers, chroma key spill). This milestone unifies preview and export on a single compositor (the existing `narrative.py:assemble_final` code path) and deletes WebGL entirely.

The work breaks into five PRs. The first two — compositor refactor + scrub endpoint + cache — are already done. The remaining three cover playback streaming, the frontend swap, and the WebGL deletion.

See [local.backend-rendered-preview-streaming.md](../design/local.backend-rendered-preview-streaming.md) for the full design, and [clarification-6-backend-rendered-preview-streaming.md](../clarifications/clarification-6-backend-rendered-preview-streaming.md) for the decision record.

---

## Deliverables

### 1. Backend render primitive (✅ completed)
- `src/scenecraft/render/schedule.py` — `Schedule` dataclass + `build_schedule(project_dir)` reading directly from SQLite
- `src/scenecraft/render/compositor.py` — `render_frame_at(schedule, t) -> np.ndarray`
- `src/scenecraft/render/narrative.py` — `assemble_final` slimmed to a loop over `render_frame_at`
- Parity tests in `tests/test_compositor_parity.py`

### 2. Scrub endpoint + cache (✅ completed)
- `GET /api/projects/:name/render-frame?t=X&quality=N` — JPEG bytes
- `src/scenecraft/render/frame_cache.py` — thread-safe LRU (500 frames / 250 MB), mtime-based invalidation (SQLite WAL aware)
- `GET /api/render-cache/stats` — hit/miss telemetry

### 3. MSE playback
- Per-session `RenderWorker` + global `RenderCoordinator` (cpu_count - 1)
- PyAV-based fMP4 fragment encoder
- WebSocket `/api/projects/:name/preview-stream` — action messages in (play/pause/seek), fMP4 binary frames out
- Pre-render buffer (10s ahead of playhead)

### 4. Frontend `<PreviewViewport>`
- `src/components/editor/PreviewViewport.tsx` — video + canvas swap
- `src/hooks/useMSEPlayback.ts`
- `src/hooks/useLatestWinsRequest.ts` — scrub request queue
- `src/lib/preview-client.ts` — fetchScrubFrame, openPreviewStream
- `PreviewPanel` swaps `<BeatEffectPreview>` for `<PreviewViewport>`
- `PreviewContext` shrinks (drops `crossfadeData`, `trackLayers`, `isTransitionLoading`, `updatePreview`)
- Timeline loses its frame-preloading and crossfade-computation pipeline

### 5. WebGL removal + end-to-end validation
- Delete `src/components/editor/BeatEffectPreview.tsx`
- Delete `src/lib/frame-cache.ts` (frontend preloader)
- Delete shader helpers, framebuffer utilities, texture loaders
- Integration tests proving parity between preview and export

---

## Success Criteria

- [x] `render_frame_at` passes parity tests (random-access == sequential, cold == warm, idempotent)
- [x] `/render-frame` endpoint serves JPEGs with correct Content-Type and cache headers
- [x] Frame cache invalidates on any DB write; hit rate > 0 after repeated requests for the same t
- [ ] `/preview-stream` WebSocket produces playable fMP4 fragments accepted by MSE
- [ ] `<PreviewViewport>` renders scrub frames via canvas and playback via `<video>` without flicker on state swap
- [ ] No multi-layer compositing bugs observable on test projects (strobe, adjustment layers, chroma key all match the final export)
- [ ] Frontend has no remaining WebGL code
- [ ] No feature flag — WebGL deletion ships in the same PR as the new viewport

---

## Risks

| Risk | Mitigation |
|---|---|
| Compositor throughput (5-15 fps on CPU at 1080p) bottlenecks real-time playback | Pre-render-ahead buffer; scrub cache absorbs cold regions. Accept stutter on edit invalidation (user confirmed). |
| libx264 / fMP4 muxing quirks in PyAV | Fall back to ffmpeg subprocess if PyAV fMP4 output is finicky. |
| MediaSource SourceBuffer compatibility edge cases across browsers | Test on Chrome/Safari/Firefox. Document known codecs/profiles. |
| Dead code removal on the frontend breaks the download/record feature | `previewRef` handle narrows to `{ getCanvas, getVideo }`; recorder picks based on play state. Tracked in design §Integration. |

---

## Notes

- **Greenfield mode**: no feature flag, no migration shim, no coexist path. `git revert` is the rollback.
- **2/5 tasks already landed** — this milestone is being created retroactively to record the completed work. Remaining tasks are 34, 35, 36.
- **Per-fine-grained invalidation**: listed in the design (§2.3) but out of scope for M11; follow-up work will refine whole-project cache eviction into range-based invalidation.

---

**Status**: In Progress
**Related Documents**:
- [Design: local.backend-rendered-preview-streaming.md](../design/local.backend-rendered-preview-streaming.md)
- [Clarification 6](../clarifications/clarification-6-backend-rendered-preview-streaming.md)
