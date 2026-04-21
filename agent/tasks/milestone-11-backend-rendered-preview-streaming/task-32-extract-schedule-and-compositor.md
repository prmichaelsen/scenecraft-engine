# Task 32: Extract `build_schedule` + `render_frame_at` + parity tests

**Milestone**: [M11 - Backend-Rendered Preview Streaming](../../milestones/milestone-11-backend-rendered-preview-streaming.md)
**Design Reference**: [backend-rendered-preview-streaming](../../design/local.backend-rendered-preview-streaming.md)
**Estimated Time**: 2-3 days
**Dependencies**: None
**Status**: Completed
**Completed Date**: 2026-04-20
**Actual Commits**: `0808a0e`, `9948f07`

---

## Objective

Decompose the monolithic `assemble_final()` in `render/narrative.py` into a reusable frame-rendering primitive so the scrub endpoint and MSE playback can share the same compositor code path as the final export.

---

## Context

`assemble_final` was ~1060 lines that interleaved schedule-building, per-frame rendering, and MP4 muxing. The scrub and playback endpoints need random-access rendering, which requires the per-frame body to be callable standalone. This task extracts the pieces and proves parity.

---

## Steps

### 1. Create `render/schedule.py`
- `Schedule` dataclass holding segments, overlay_tracks, effect_events, suppressions, meta, fps, width, height, duration_seconds, crossfade_frames, work_dir, audio_path, preview
- `build_schedule(project_dir, max_time, crossfade_frames, preview) -> Schedule` — reads DB directly (no YAML)

### 2. Create `render/compositor.py`
- `render_frame_at(schedule, t, *, frame_cache=None) -> np.ndarray`
- Includes all per-frame helpers: `_apply_color_grading`, `_apply_frame_effects`, `_apply_transform`, `_apply_radial_mask`, `_composite_overlays`, `_ensure_loaded`, `_find_segment`, `_get_frame_at`
- Accepts optional `frame_cache` dict for mutable state (loaded segments) to be shared across calls

### 3. Slim `render/narrative.py:assemble_final`
- Becomes: `build_schedule()` → `cv2.VideoWriter` → loop `render_frame_at()` → `_mux_audio`
- Target: under 100 lines for the function body

### 4. Parity tests in `tests/test_compositor_parity.py`
- `test_random_access_matches_sequential` — render in reverse order, compare pixel-for-pixel
- `test_cold_cache_matches_warm_cache` — fresh `frame_cache` vs. shared `frame_cache`
- `test_repeated_render_is_stable` — same `(schedule, t, cache)` returns identical bytes every call
- Fixture: synthesize a gradient video, build a 2-keyframe + 1-transition project

---

## Verification

- [x] Schedule + compositor modules import cleanly (no YAML deps)
- [x] All three parity tests pass on clean checkout
- [x] `assemble_final` produces byte-identical output vs. pre-refactor (manual spot-check via existing render tests)
- [x] No regression in existing test suite
- [x] Signature: `assemble_final(project_dir, output_path, max_time, crossfade_frames)` — renamed from `yaml_path`

---

## Expected Output

### Files Created
- `src/scenecraft/render/schedule.py`
- `src/scenecraft/render/compositor.py`
- `tests/test_compositor_parity.py`

### Files Modified
- `src/scenecraft/render/narrative.py` — `assemble_final` slimmed, `load_narrative` rewritten as DB wrapper

---

**Status**: Completed
