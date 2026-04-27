# Spec: Engine Render Pipeline

> **Agent Directive**: This spec is the black-box contract for the offline render pipeline: schedule build, per-frame composition, transform application, and final assembly (video write + audio mux). Treat the Behavior Table as the reviewer's proofing surface; every `undefined` row links to an Open Question. TDD is mechanical from the Tests section.

**Namespace**: local
**Version**: 1.0.0
**Created**: 2026-04-27
**Last Updated**: 2026-04-27
**Status**: Draft
**Compatibility**: scenecraft-engine (post-M9 mixdown, pre-M18 provider-typed)

---

**Purpose**: Define the observable, end-to-end behavior of the three-tier render pipeline — Schedule Build → Per-Frame Composition → Final Assembly — plus the non-fatal cache-invalidation adjunct that keeps preview caches coherent across edits. Generation of segment videos/stills, individual effect types, and provider integration are specified elsewhere.

**Source**: `--from-draft` — user-supplied task description + direct read of `scenecraft-engine/src/scenecraft/render/{narrative.py, schedule.py, compositor.py, cache_invalidation.py}` and `agent/reports/audit-2-architectural-deep-dive.md §1D`.

---

## Scope

**In-scope**:
- `build_schedule(project_dir, max_time, crossfade_frames, preview) -> Schedule` — reads project.db, produces an immutable-ish Schedule dataclass covering base-track segments (deduped, sorted), overlay tracks (layered, sorted by zOrder, respecting mute/solo/hidden), effect events (from audio_intelligence intel + user effects, sorted, hard_cut stripped), suppressions, fps/width/height/duration, audio path, crossfade-frames resolution, and preview halving of output resolution.
- `render_frame_at(schedule, t, *, frame_cache, scrub, prefer_proxy) -> BGR ndarray` — deterministic per-frame composite covering: base-segment lookup + time-remap (linear or curve) + crossfade at segment boundaries + base opacity curve + color grading + per-transition effects + base transform, then layered overlay composition with its own crossfades + color grading + transform + radial mask + blend-mode composite, then global frame effects driven by `effect_events`.
- `_apply_transform(img, clip_data, progress)` — scale X / scale Y about an anchor (`cv2.resize` + zero-padded crop/pad), then X/Y translation (`cv2.warpAffine` with `BORDER_CONSTANT` value `(0,0,0)`). The Y translation is sign-flipped for non-adjustment clips so positive `ty` moves the image up (matching frontend convention); adjustment clips preserve sign.
- `assemble_final(project_dir, output_path, max_time, crossfade_frames) -> str` — thin loop driving `render_frame_at` via `cv2.VideoWriter` (fourcc `mp4v`) into `{output_path}.tmp.mp4`, then ffmpeg re-encode + audio mux (multi-track mixdown if available, else schedule.audio_path) via a single `ffmpeg -i tmp -i audio -c:v libx264 -c:a aac -shortest` subprocess, then unlink tmp.
- `invalidate_frames_for_mutation(project_dir, ranges)` — non-fatal invalidation of L1 frame cache + fMP4 fragment cache + RenderCoordinator schedule-rebuild hint; returns `(frames_dropped, fragments_dropped)` and **never raises**.

**Out of scope** (specced elsewhere):
- Keyframe / transition / slot / video generation (Imagen, Veo, Kling, Runway) — see engine-generation-pipelines.
- Individual effect definitions and intensity envelopes beyond the dispatch shape in `_apply_frame_effects` — see scenecraft effects specs.
- Provider integration and spend tracking — see engine-providers + engine-spend-ledger.
- Proxy generation + chunked-proxy manifests — see engine-proxy-pipeline.
- Playback-worker / fragment-cache / preview WS — see engine-preview-playback.

---

## Requirements

### Schedule Build

- **R1** `build_schedule` reads `project.db` via `load_project_data(project_dir)`; no YAML is read.
- **R2** Base-track segments are drawn from transitions on `track_id == "track_1"` with `deleted_at IS NULL`; for each, `from_ts = parse(from_kf.timestamp)`, `to_ts = parse(to_kf.timestamp)`.
- **R3** A segment with `to_ts <= from_ts` is dropped.
- **R4** `max_time` clamps: segments starting at or after `max_time` are dropped; segments whose `to_ts > max_time` have `to_ts = max_time`.
- **R5** Each base segment prefers `selected_transitions/{tr_id}_slot_0.mp4` (`is_still=False`); falls back to `selected_keyframes/{from_id}.png` (`is_still=True`); if neither exists the transition contributes no segment.
- **R6** Segments are sorted by `(from_ts, -duration)` and deduplicated: a segment whose `from_ts` falls inside the previous segment's `[from_ts, to_ts)` is dropped. Longer segments win ties on the same `from_ts` by sort order.
- **R7** Base segments carry: `remap_method` (`"linear"` or `"curve"`), `curve_points`, `effects` (from `get_transition_effects`), `opacity_curve`, all 10 color-grading curves (red/green/blue/black/saturation/hue_shift/invert/brightness/contrast/exposure), and full transform data (`transform_x/y`, `transform_x_curve`, `transform_y_curve`, `transform_scale_x_curve`, `transform_scale_y_curve`, `anchor_x/y`, `is_adjustment`). Curve fields are JSON-parsed from string form.
- **R8** Overlay tracks are loaded from `get_tracks`, sorted ascending by `z_order`, base track (`tracks[0]`) is excluded from the overlay loop. A track is skipped if `muted=True` or `hidden=True`; solo logic: if any track has `solo=True`, all non-solo tracks are implicitly muted.
- **R9** Per overlay track, each transition produces a clip with video path `selected_transitions/{tr_id}_slot_0.mp4` when `selected ∉ {None, 0, "null"}` and the file exists; otherwise falls back to still `selected_keyframes/{from}.png`; if neither exists the clip is dropped. Hidden transitions are dropped.
- **R10** Overlay clips also include "hold stills" for keyframes that have no outgoing transition on that track; hold duration runs from the keyframe timestamp to the next keyframe on the same track, or `+1.0s` if there is no next keyframe.
- **R11** Overlay clips within a track are sorted by `from_ts` after collection.
- **R12** Output `fps` comes from `meta.fps` (default 24). Output width/height come from the first non-still base segment via `cv2.VideoCapture`; default `1920×1080` if no video segment exists. When `preview=True`, width and height are halved (`w//2`, `h//2`).
- **R13** `crossfade_frames` resolution order: CLI argument → `meta.crossfade_frames` → `8`.
- **R14** Effect events: if `meta._intel_path` is set, or an `audio_intelligence*.json` file exists in `project_dir`, onsets + `layer3_rules` are loaded and expanded via `_apply_rules_client`; user effects from `get_effects` are appended; result is sorted by `time`; `effect == "hard_cut"` rows are stripped.
- **R15** Suppressions come from `get_suppressions`.
- **R16** `audio_path = meta._audio_resolved or ""`.
- **R17** `duration_seconds = segments[-1].to_ts` or `0.0` if empty.

### Per-Frame Composition

- **R18** `render_frame_at(schedule, t)` returns a BGR `numpy.ndarray` of shape `(schedule.height, schedule.width, 3)`, dtype `uint8`.
- **R19** If no base segment covers `t`, the returned frame starts as solid black `(0,0,0)`; overlays and frame effects are then applied normally.
- **R20** The active base segment is found by binary search over `segments` sorted by `from_ts`.
- **R21** Base-segment time-remap: `remap_method == "curve"` evaluates `curve_points` (clamped piecewise linear) against raw linear progress; otherwise progress is linear.
- **R22** Progress is edge-compressed: `progress = ext + raw_progress * (1 - 2*ext)` where `ext = min(eff_half_xfade/seg_dur, 0.2)`, and `eff_xfade = min(XFADE_FRAMES, max(2, seg_frames//4))`. Final progress is clamped to `[0.0, 0.999]`.
- **R23** Segment-boundary crossfade: if `t - seg.from_ts < eff_half_xfade` and the previous segment's `to_ts == seg.from_ts` and previous `_n > 0`, the previous frame is alpha-blended in with `alpha = 0.5 + (t - seg.from_ts)/eff_half_xfade * 0.5`. Symmetric logic at segment end.
- **R24** Base opacity curve, if present, scales the frame via `cv2.convertScaleAbs(frame, alpha=opacity, beta=0)` when `opacity < 0.999`; opacity is clamped to `[0,1]`.
- **R25** Color grading applies red/green/blue channel multipliers (BGR-indexed), black fade, saturation (about per-pixel mean gray), hue shift (via HSV round-trip), invert (curve and/or effect-driven), brightness offset, contrast about 0.5, and exposure `2^stops`, in that order. Final result is clipped to `[0, 255]` and cast back to `uint8`.
- **R26** Per-transition `strobe` effect blacks out the frame when `(progress * freq) % 1 > duty`.
- **R27** Base transform (R38–R41) is applied when any transform field on the segment is truthy.
- **R28** Overlay composition iterates tracks in `overlay_tracks` order (already zOrder-sorted). For each track, the active clip is the first clip whose `[from_ts, to_ts)` contains `t`. If none, the per-track `overlay_prev` entry is cleared and the track is skipped.
- **R29** Adjustment overlay clips (`is_adjustment=True`) apply color grading + radial mask to the current composited result without drawing their own pixels.
- **R30** Non-adjustment overlay clips read a frame (video via cached `cv2.VideoCapture` + frame-index seek; still via cached `cv2.imread`), resize to `(ow, oh)` with `INTER_LINEAR`, apply optional strobe (opacity→0) / invert, color grading, transform, radial mask, and composite onto the running result via `_blend_frames` using `blend_mode ∈ {normal, multiply, screen, overlay, difference, add}`.
- **R31** Overlay clip opacity resolution: `opacity_curve` > scalar `opacity` > track `base_opacity` (only when clip had no explicit opacity AND effective opacity ≥ 1.0).
- **R32** Overlay clip-boundary crossfade: when the active clip's neighbor abuts within 0.1s and the playhead is within `eff_half_xfade_s` of a boundary, the neighbor clip is processed through the same pipeline and blended via `cv2.addWeighted`.
- **R33** After all overlays, `_apply_frame_effects` is called: it walks `effect_events` with early termination (`et > t + 0.1` breaks), accumulates zoom/shake/brightness/contrast/glow intensities, and applies zoom (`cv2.resize` + crop), shake (`cv2.warpAffine`, `BORDER_REFLECT`), brightness/contrast (`cv2.convertScaleAbs`), and glow (Gaussian blur + weighted add), skipping events matched by `suppressions`.
- **R34** `frame_cache` is mutable across calls and carries: `loaded_segs`, `overlay_prev`, `_segments_primed`, optional `stream_caps`, optional `_timing`. Passing `None` creates a fresh dict and disables cross-call reuse (but still works).
- **R35** `scrub=True` uses per-call open/seek/close for video segments (O(1) memory). `stream_caps` dict keeps per-segment VideoCaptures open with a cursor. Default (no scrub, no stream_caps) batch-loads all frames of the active base segment into RAM via `_ensure_loaded`, keeping at most ±1 neighboring segment.
- **R36** Determinism: for a given `(schedule, t)` with cold caches and no proxies, `render_frame_at` is pixel-identical across calls.
- **R37** Time values outside `[0, schedule.duration_seconds]` are legal inputs; `t < 0` or `t >= duration_seconds` produce a frame with no active base segment (R19 applies).

### Transform Application

- **R38** `_apply_transform(img, clip_data, progress)` applies scale-then-translate in that order, in-place on a copy of `img` dimensions `(h, w)`.
- **R39** Scale: `scale_x = evaluate(transform_scale_x_curve, progress)` (default 1.0 when curve absent); same for `scale_y`. When both are within `0.001` of `1.0`, the scale branch is skipped. `anchor_x`, `anchor_y` default to `0.5`.
- **R40** Scale implementation: `cv2.resize(img, (int(w*scale_x), int(h*scale_y)), INTER_LINEAR)` followed by an anchor-aligned crop/pad into a zero-initialized output of original `(h, w)`. When `new_w == 0` or `new_h == 0`, the scale branch is a no-op (image passes through unchanged).
- **R41** Translate: `tx = evaluate(transform_x_curve, progress)` or scalar `transform_x` or 0; same for `ty`. When `tx` and `ty` are both 0/None the translate branch is skipped. Pixel shift: `dx = int(tx * w)`, `dy = int((-ty if not is_adjustment else ty) * h)`. Applied via `cv2.warpAffine(img, [[1,0,dx],[0,1,dy]], (w, h), BORDER_CONSTANT, borderValue=(0,0,0))`.

### Final Assembly

- **R42** `assemble_final(project_dir, output_path, max_time, crossfade_frames)`:
  1. Derives `preview = output_path.endswith("_preview.mp4")`.
  2. Calls `build_schedule(project_dir, max_time, crossfade_frames, preview)`.
  3. Computes `total_output_frames = round(duration_seconds * fps)`.
  4. Opens `cv2.VideoWriter("{output_path}.tmp.mp4", fourcc=mp4v, fps, (width, height))`.
  5. Loops `frame_num in range(total_output_frames)`, writing `render_frame_at(schedule, frame_num/fps, frame_cache=...)` into the writer. `frame_cache` is a single persistent dict across the loop.
  6. Releases the writer.
  7. Attempts multi-track mixdown via `render_project_audio(project_dir, duration_seconds, audio_staging/_mixdown.wav)`; on any exception, logs and falls back to `schedule.audio_path`.
  8. Calls `_mux_audio(tmp, output_path, audio_to_mux, preview)` which runs a single ffmpeg re-encode (`libx264` + `aac`, `-shortest`, preset `ultrafast crf 28` for preview else `fast crf 18`), then unlinks the tmp file.
  9. Returns `output_path`.
- **R43** Progress logs are emitted every 1000 frames and on the final frame.
- **R44** `assemble_final` preflights `shutil.which("ffmpeg")` at function entry; missing → `MissingDependencyError("ffmpeg not found; install via: <platform-specific hint>")` raised before any schedule build or writer open. (Closes OQ-3; transitional: current code does not preflight.)
- **R52** `cv2.VideoWriter.write(frame)` return value MUST be checked; on `False` raise `RenderError("VideoWriter rejected frame at t=<time>")`. Mux is NOT attempted on partial `.tmp.mp4`. (Closes OQ-2.)
- **R53** `assemble_final` wraps the entire "open VideoWriter → loop → release → mux" flow in `try/finally`; the `finally` block unlinks `{output_path}.tmp.mp4` if it exists, ensuring no orphan `.tmp.mp4` persists on any exit path (crash or success). (Closes OQ-4.)
- **R54** `_evaluate_curve` clamps `x` to `[0, 1]` as a contractual guarantee (not implementation detail). Callers may pass any float; the curve evaluator treats `x < 0` as `x = 0` and `x > 1` as `x = 1`. (Closes OQ-5.)
- **R55** `_apply_transform` with `scale_x == 0` or `scale_y == 0` returns a black frame of target `(h, w)` dimensions (NumPy zeros, uint8). The current short-circuit-to-identity behavior is a bug to be fixed in the refactor; transitional behavior returns the original image. (Closes OQ-6.)
- **R56** `cv2.resize` interpolation choice is direction-based: `INTER_AREA` when downscaling (new_w*new_h < old_w*old_h), `INTER_LINEAR` when upscaling or identity. Applied uniformly across all `cv2.resize` call sites (transform, overlay read, base-segment fit, frame-effect zoom, preview halving). (Closes OQ-7.)
- **R57** `assemble_final` with `duration_seconds == 0` (no segments / zero-duration schedule) short-circuits before opening `cv2.VideoWriter`: returns success with no output file written. `total_output_frames == 0` path MUST NOT open a writer. (Closes OQ-8.)
- **R58 (INV-1 single-writer)**: `assemble_final` holds the schedule snapshot built at function entry for the entire render loop. Coordinator invalidations / schedule rebuilds that fire mid-render do NOT abort the in-flight render; the render completes with snapshot-at-start semantics. No internal mutex is held across the render loop; negative-assertion test covers this. (Closes OQ-1; cross-ref cache-invalidation R31.)
- **R59 (INV-7 per-working-copy cache partitioning)**: The `frame_cache` dict, `fragment_cache` entries, and `RenderCoordinator` worker state are keyed by `working_copy` (session_id / working_copy_db_path), NOT by `project_dir`. Renders initiated from different working copies for the same project operate on isolated caches. Target state; current `project_dir`-keyed structures transitional. Cross-ref cache-invalidation R27.

### Cache Invalidation

- **R45** `invalidate_frames_for_mutation(project_dir, ranges)` returns `(frames_dropped, fragments_dropped)`.
- **R46** `ranges is None` → wholesale invalidation (`invalidate_project` on both caches, **no** background requeue).
- **R47** `ranges == []` or all pairs invalid (`b < a`) → treated as wholesale invalidation (empty materialized list becomes `None`).
- **R48** `ranges` with at least one valid pair → `invalidate_ranges` on both caches **and** `RenderCoordinator.invalidate_ranges_in_background(project_dir, ranges)` nudge.
- **R49** `RenderCoordinator.invalidate_project(project_dir)` is called unconditionally for any non-no-op invocation (so a live playback worker rebuilds its schedule on its next cycle).
- **R50** The function **never raises**: each of the three steps (frame cache, fragment cache, coordinator) is wrapped in its own broad `try/except` that swallows all exceptions. A failure in one step does not prevent the other steps from running.
- **R51** When the frame cache or fragment cache modules cannot be imported / are unavailable, the corresponding `*_dropped` count is `0`.

---

## Interfaces / Data Shapes

```python
@dataclass
class Schedule:
    segments: list[dict]           # base track — see R7
    overlay_tracks: list[dict]     # {"blend_mode", "opacity", "clips": [clip_dict, ...]}
    effect_events: list[dict]      # {"time", "duration", "effect", "intensity", "sustain", "stem_source", ...}
    suppressions: list[dict]       # {"from", "to", "effectTypes"?, "layerEffectTypes"?}
    meta: dict                     # project meta
    fps: float
    width: int                     # halved if preview
    height: int                    # halved if preview
    duration_seconds: float        # 0.0 if no segments
    crossfade_frames: int
    work_dir: Path
    audio_path: str                # "" if unresolved
    preview: bool
    overlay_clips: list[dict]      # flat alias of all overlay clips
```

Base segment dict (required keys):
```
from_ts: float, to_ts: float, source: str, is_still: bool,
remap_method: "linear"|"curve", curve_points: list[[x,y]] | None,
effects: list[dict], opacity_curve: list[[x,y]] | None,
# optional color-grade curves + transform fields as in R7
```

`render_frame_at` signature:
```python
def render_frame_at(
    schedule: Schedule,
    t: float,
    *,
    frame_cache: dict | None = None,
    scrub: bool = False,
    prefer_proxy: bool = False,
) -> "numpy.ndarray":  # (H, W, 3) uint8 BGR
```

`invalidate_frames_for_mutation`:
```python
def invalidate_frames_for_mutation(
    project_dir: Path,
    ranges: Iterable[tuple[float, float]] | None = None,
) -> tuple[int, int]:  # (frames_dropped, fragments_dropped); NEVER raises
```

---

## Behavior Table

| # | Scenario | Expected Behavior | Tests |
|---|----------|-------------------|-------|
| 1 | `build_schedule` on project with 3 sequential track_1 transitions, selected videos present | Returns Schedule with 3 base segments in timeline order, matching durations | `schedule-basic-three-segments` |
| 2 | `build_schedule` sees two transitions whose time ranges overlap | Later-starting or shorter overlapping segment is dropped; first/longest wins | `schedule-dedup-overlapping-segments` |
| 3 | `build_schedule` with `max_time=T` where T cuts through a segment | That segment's `to_ts` is clamped to T; later segments excluded | `schedule-clamps-to-max-time` |
| 4 | `build_schedule` with no selected video for a transition but keyframe PNG exists | Falls back to `is_still=True` segment pointing at keyframe PNG | `schedule-falls-back-to-still` |
| 5 | `build_schedule` with neither selected video nor keyframe image | Transition contributes no segment; no error | `schedule-drops-missing-media` |
| 6 | `build_schedule` with `preview=True` | Output width/height are halved; preview flag propagates | `schedule-preview-halves-resolution` |
| 7 | `build_schedule` with `meta.crossfade_frames=4` and CLI arg=12 | Uses 12 (CLI beats meta) | `schedule-crossfade-resolution-order` |
| 8 | `build_schedule` loads overlays with mute/solo/hidden mix | Muted + hidden dropped; any `solo=True` mutes non-solo tracks | `schedule-overlay-mute-solo-hidden` |
| 9 | `build_schedule` with no video segments (only stills) | Width/height default to `1920×1080` | `schedule-defaults-resolution` |
| 10 | `render_frame_at(schedule, t)` with t outside any segment | Returns black frame plus overlays/effects at t | `render-t-outside-segments-black` |
| 11 | `render_frame_at` at segment boundary within crossfade window | Output is alpha-blend of prev and current frames | `render-segment-boundary-crossfade` |
| 12 | `render_frame_at` with `remap_method="curve"` | Source frame index derived from evaluated curve, not linear progress | `render-curve-remap` |
| 13 | `render_frame_at` with `opacity_curve` evaluated to 0 | Frame is fully black (pre-overlay/effects) | `render-opacity-zero-blackens-base` |
| 14 | `render_frame_at` with strobe effect where `(progress*freq) % 1 > duty` | Base frame is black this tick | `render-strobe-blacks-base` |
| 15 | `render_frame_at` with active overlay clip, normal blend, opacity 1.0 | Overlay pixels fully replace underlying region | `render-overlay-normal-full-opacity` |
| 16 | `render_frame_at` with adjustment overlay clip | Applies color grading + mask to result; no new pixels drawn | `render-adjustment-overlay-no-draw` |
| 17 | `render_frame_at` called twice with same `(schedule, t)`, no proxies, fresh caches | Byte-identical output arrays | `render-deterministic-same-inputs` |
| 18 | `render_frame_at(scrub=True)` | Uses per-call open/seek/close; no entries linger in `loaded_segs` | `render-scrub-uses-seek-close` |
| 19 | `_apply_transform` with no curves and no scalars | Image is returned unchanged (within rounding) | `transform-noop` |
| 20 | `_apply_transform` with `scale_x=0.5, scale_y=0.5` | Output has scaled image letterbox/pillarbox with zero borders, same `(h,w)` | `transform-scale-half-keeps-dims` |
| 21 | `_apply_transform` with `tx=0.25, ty=0, is_adjustment=False` | Image translated right by `int(0.25*w)`; revealed region is black | `transform-translate-right-black-pad` |
| 22 | `_apply_transform` with `ty=0.25, is_adjustment=False` | Image translated UP by `int(0.25*h)` (sign-flipped) | `transform-ty-sign-flips-non-adjustment` |
| 23 | `_apply_transform` with `ty=0.25, is_adjustment=True` | Image translated DOWN by `int(0.25*h)` (sign preserved) | `transform-ty-preserved-adjustment` |
| 24 | `assemble_final` happy path | Produces mp4 at `output_path` with video+audio; removes tmp file | `assemble-happy-path-produces-mp4` |
| 25 | `assemble_final` with `output_path` ending `_preview.mp4` | Uses `preview=True`, ultrafast/crf28 encode, halved resolution | `assemble-preview-path-triggers-preview` |
| 26 | `assemble_final` when multi-track mixdown raises | Logs, falls back to `schedule.audio_path`, still produces a valid mp4 | `assemble-mixdown-failure-falls-back` |
| 27 | `invalidate_frames_for_mutation(p, None)` | Drops all cached frames+fragments for project; no bg requeue; returns counts | `invalidate-wholesale-no-bg-requeue` |
| 28 | `invalidate_frames_for_mutation(p, [(10.0, 20.0)])` | Drops frames+fragments in range; coordinator gets bg-requeue hint | `invalidate-range-triggers-bg-requeue` |
| 29 | `invalidate_frames_for_mutation(p, [])` | Treated as wholesale (empty list → None) | `invalidate-empty-list-is-wholesale` |
| 30 | `invalidate_frames_for_mutation` when frame_cache import fails | Returns `(0, fragments_dropped)`; no raise | `invalidate-frame-cache-import-fails` |
| 31 | `invalidate_frames_for_mutation` when RenderCoordinator raises | Counts still returned correctly; no raise | `invalidate-coordinator-failure-swallowed` |
| 32 | `invalidate_frames_for_mutation` with a malformed range `(20, 10)` | Dropped from range list; remaining valid ranges processed | `invalidate-drops-invalid-range` |
| 33 | `render_frame_at` on schedule with overlay track `zOrder` out of order in DB | Rendered composite respects ascending zOrder regardless of DB insert order | `render-overlay-zorder-respected` |
| 34 | Schedule rebuild mid-render (coordinator flips project dirty while `assemble_final` loop is running) | Render holds t=0 schedule snapshot; completes with snapshot-at-start semantics; coord signals do not abort (R58) | `assemble-snapshot-immune-to-mid-render-invalidate`, `assemble-no-lock-across-render-loop` |
| 35 | `cv2.VideoWriter.write` returns False mid-frame | Raise `RenderError("VideoWriter rejected frame at t=<time>")`; do NOT proceed to mux; tmp cleaned up via R53 finally (R52) | `assemble-videowriter-false-raises-rendererror` |
| 36 | `ffmpeg` missing from PATH | `MissingDependencyError("ffmpeg not found; install via: ...")` at function entry before any work (R44) | `assemble-ffmpeg-missing-preflight-error` |
| 37 | `assemble_final` crashes between `out.release()` and `_mux_audio` completion | `try/finally` unlinks `.tmp.mp4` on any exit path; no orphan persists (R53) | `assemble-tmp-cleanup-on-crash-via-finally` |
| 38 | Transform curve evaluation queried with `x > 1.0` (progress outside [0,1]) | `_evaluate_curve` clamps to `[0, 1]` as contract (R54) | `evaluate-curve-clamps-x-to-0-1` |
| 39 | `_apply_transform` with `scale_x=0.0` or `scale_y=0.0` | Returns black frame of target `(h, w)` dims, dtype uint8 (R55). Current identity-return transitional. | `transform-scale-zero-returns-black-frame` |
| 40 | `cv2.resize` interpolation choice | Direction-based: `INTER_AREA` for downscale, `INTER_LINEAR` for upscale / identity; uniform across all call sites (R56) | `resize-interpolation-by-direction` |
| 41 | Zero-duration schedule (`duration_seconds == 0`) | Short-circuit before opening VideoWriter; return success with no output file (R57) | `assemble-zero-duration-short-circuits` |

---

## Behavior

### Schedule Build — Step by Step

1. Load `data = load_project_data(project_dir)`; `meta = data["meta"]`.
2. Resolve `fps`, `crossfade_frames`, `intel_path` (CLI/meta/auto-glob).
3. Load effect events: parse intel onsets+rules via `_apply_rules_client`; append user effects; load suppressions; sort events by `time`; strip `hard_cut`.
4. Load base-track transitions (`track_id=="track_1"`, not deleted) + keyframes map.
5. For each transition: parse timestamps, clamp vs `max_time`, parse curves, build segment dict preferring selected video else still fallback (R5, R7).
6. Sort segments `(from_ts, -duration)`; dedup overlapping (R6).
7. Read first video segment for `(w, h)`; else default `1920×1080`.
8. Load overlay tracks (R8–R11).
9. If `preview`, halve `(w, h)`.
10. Compute `duration_seconds`.
11. Resolve `audio_path`.
12. Flatten `overlay_clips` alias.
13. Return `Schedule(...)`.

### Per-Frame Composition — Step by Step

1. Initialize `frame_cache` dict + derive `stream_caps`, `timing`.
2. Prime segments (eager `cv2.imread` for stills, stub `_n` + `_fps_source` for videos) once per cache (R34).
3. Binary-search active base segment (R20); if none, start with black frame.
4. Compute `eff_xfade`, `eff_half_xfade`, `ext`, `raw_progress`, clamped `progress` (R22).
5. Fetch base frame from the chosen read path (batch / scrub / stream) (R35).
6. Apply segment-start and segment-end boundary crossfades (R23).
7. Apply base opacity curve (R24).
8. Apply color grading (R25).
9. Apply per-transition effects (R26).
10. Apply base transform (R27, R38–R41).
11. Composite overlay tracks in zOrder, each with its own clip-boundary crossfade, color grading, transform, mask, blend (R28–R32).
12. Apply global frame effects (R33).
13. Record timing deltas if `_timing` present.
14. Return frame.

### Final Assembly — Step by Step

1. Derive `preview` flag from `output_path` suffix.
2. Build schedule.
3. Open `cv2.VideoWriter` on `{output_path}.tmp.mp4`.
4. Loop frames, writing each composited frame; log every 1000 + last.
5. Release writer.
6. Try multi-track mixdown; on failure, fall back to `schedule.audio_path`.
7. `_mux_audio`: single ffmpeg invocation combining `tmp.mp4` video + chosen audio into `output_path`; unlink tmp.
8. Return `output_path`.

### Cache Invalidation — Step by Step

1. Materialize `ranges` → filter `b >= a`; empty → `None` (R47).
2. Frame cache: `invalidate_project` if None else `invalidate_ranges`; swallow exceptions (R50/R51).
3. Fragment cache: same; swallow exceptions.
4. Coordinator: `invalidate_project` always; `invalidate_ranges_in_background` only when `range_list is not None` (R46/R48); swallow exceptions.
5. Return `(frames_dropped, fragments_dropped)`.

---

## Acceptance Criteria

- [ ] `build_schedule` reads only from `project.db` + filesystem under `project_dir` (no YAML read).
- [ ] Overlapping base segments are deduped deterministically (first sort-winner kept).
- [ ] `max_time` truncates the schedule's tail segment and drops strictly-later ones.
- [ ] Preview flag halves output resolution.
- [ ] Effect events are sorted and `hard_cut`-free.
- [ ] `render_frame_at` is pure over `(schedule, t)` modulo `frame_cache` (same inputs ⇒ same pixels with cold cache).
- [ ] Out-of-range `t` yields a black base that overlays/effects can still paint onto.
- [ ] Segment-boundary crossfade window reaches 50%→100% base over `eff_half_xfade` seconds.
- [ ] Overlay track order is strictly zOrder ascending after build.
- [ ] `_apply_transform` sign convention for `ty` depends on `is_adjustment`.
- [ ] `assemble_final` writes one mp4 per invocation, muxes audio in a single ffmpeg call, and deletes the tmp file on the happy path.
- [ ] Preview output path suffix `_preview.mp4` activates preview encode options + schedule preview flag.
- [ ] Multi-track mixdown failure is non-fatal.
- [ ] `invalidate_frames_for_mutation` never raises and returns `(int, int)` for every input.
- [ ] `ranges=None` and `ranges=[]` both behave as wholesale invalidation; only wholesale skips the background requeue.

---

## Tests

### Base Cases

#### Test: schedule-basic-three-segments (covers R1, R2, R5, R7)

**Given**:
- Project with three track_1 transitions at `(0:00 → 0:10)`, `(0:10 → 0:20)`, `(0:20 → 0:30)`, each with a selected `.mp4` on disk.

**When**: `build_schedule(project_dir)` is called with default args.

**Then**:
- **segment-count**: `len(schedule.segments) == 3`.
- **segment-order**: `[s.from_ts for s in schedule.segments] == [0.0, 10.0, 20.0]`.
- **segment-sources**: each segment `source` points at the selected mp4.
- **is-still-false**: every segment has `is_still == False`.

#### Test: schedule-dedup-overlapping-segments (covers R3, R6)

**Given**: Two track_1 transitions spanning `(5.0 → 15.0)` and `(10.0 → 12.0)`.

**When**: `build_schedule(project_dir)`.

**Then**:
- **keeps-longer**: only the `(5.0 → 15.0)` segment remains.
- **drops-shorter**: no segment with `from_ts == 10.0`.

#### Test: schedule-clamps-to-max-time (covers R4)

**Given**: Segments `(0→10)`, `(10→20)`, `(20→30)`; `max_time=15.0`.

**When**: `build_schedule(project_dir, max_time=15.0)`.

**Then**:
- **tail-clamped**: the `(10→20)` segment has `to_ts == 15.0`.
- **later-dropped**: no segment with `from_ts >= 15.0`.

#### Test: schedule-falls-back-to-still (covers R5)

**Given**: Transition with no `selected_transitions/*.mp4` file but `selected_keyframes/{from_id}.png` exists.

**When**: `build_schedule`.

**Then**:
- **is-still-true**: segment `is_still == True`.
- **source-is-png**: segment `source` ends in `.png`.

#### Test: schedule-drops-missing-media (covers R5)

**Given**: Transition with neither selected mp4 nor keyframe png.

**When**: `build_schedule`.

**Then**:
- **no-segment**: schedule contains no segment for that transition.
- **no-exception**: call returns normally.

#### Test: schedule-preview-halves-resolution (covers R12)

**Given**: Project whose first video segment is `1920×1080`.

**When**: `build_schedule(project_dir, preview=True)`.

**Then**:
- **width-halved**: `schedule.width == 960`.
- **height-halved**: `schedule.height == 540`.
- **preview-flag**: `schedule.preview is True`.

#### Test: schedule-crossfade-resolution-order (covers R13)

**Given**: `meta.crossfade_frames == 4`.

**When**: `build_schedule(project_dir, crossfade_frames=12)`.

**Then**:
- **cli-wins**: `schedule.crossfade_frames == 12`.

#### Test: schedule-overlay-mute-solo-hidden (covers R8)

**Given**: Tracks: `track_1` (base), `track_2` muted, `track_3` solo, `track_4` normal.

**When**: `build_schedule`.

**Then**:
- **muted-excluded**: no overlay track with `id=="track_2"`.
- **non-solo-excluded**: no overlay track with `id=="track_4"` (solo active on track_3).
- **solo-included**: overlay tracks contains `track_3`.

#### Test: schedule-defaults-resolution (covers R12)

**Given**: Project has only still segments (no selected videos).

**When**: `build_schedule`.

**Then**:
- **default-width**: `schedule.width == 1920`.
- **default-height**: `schedule.height == 1080`.

#### Test: render-t-outside-segments-black (covers R18, R19, R37)

**Given**: Schedule with one segment `(0 → 10)`; no overlays; no effects.

**When**: `render_frame_at(schedule, 20.0)`.

**Then**:
- **shape**: frame.shape == `(schedule.height, schedule.width, 3)`.
- **all-black**: `frame.sum() == 0`.

#### Test: render-segment-boundary-crossfade (covers R23)

**Given**: Two abutting video segments `(0→5)` and `(5→10)`, distinct solid colors.

**When**: `render_frame_at(schedule, t)` with `t = 5.0 - (eff_half_xfade/2)`.

**Then**:
- **pixel-is-blend**: returned pixel mean value lies strictly between the two solid colors' means.
- **not-equal-prev**: output is not byte-identical to the previous segment's frame.
- **not-equal-current**: output is not byte-identical to the current segment's frame.

#### Test: render-curve-remap (covers R21)

**Given**: Segment with `remap_method="curve"` and `curve_points=[[0,0],[0.5,1],[1,1]]` (fast then hold).

**When**: `render_frame_at` at `t = seg.from_ts + 0.75 * seg_dur`.

**Then**:
- **src-frame-at-end**: the sampled source frame index corresponds to the last source frame (not the 75% one).

#### Test: render-opacity-zero-blackens-base (covers R24)

**Given**: Segment with `opacity_curve` that evaluates to 0 at the queried progress.

**When**: `render_frame_at` at that t, no overlays, no effects.

**Then**:
- **all-black**: `frame.sum() == 0`.

#### Test: render-strobe-blacks-base (covers R26)

**Given**: Segment with a `strobe` effect enabled, freq=8, duty=0.1; t chosen so the duty phase is "off".

**When**: `render_frame_at` at that t.

**Then**:
- **black**: `frame.sum() == 0`.

#### Test: render-overlay-normal-full-opacity (covers R28, R30, R31)

**Given**: Base solid red; overlay clip with solid green still, `blend_mode="normal"`, opacity=1.0, spanning `t`.

**When**: `render_frame_at(schedule, t)`.

**Then**:
- **green-replaces-red**: frame is solid green.

#### Test: render-adjustment-overlay-no-draw (covers R29)

**Given**: Base solid white; overlay clip `is_adjustment=True` with `invert_curve → 1.0`.

**When**: `render_frame_at(schedule, t)`.

**Then**:
- **inverted-base**: frame is solid black (255→0).
- **no-overlay-pixels**: no new drawn pixels from overlay source (overlay still never loaded).

#### Test: render-deterministic-same-inputs (covers R36)

**Given**: Fresh schedule, fresh empty `frame_cache`, same `t`.

**When**: `render_frame_at` is called twice in sequence, each with its own fresh `frame_cache`.

**Then**:
- **byte-equal**: `numpy.array_equal(a, b)` is true.

#### Test: render-scrub-uses-seek-close (covers R35)

**Given**: Schedule with a video segment covering `t`.

**When**: `render_frame_at(schedule, t, scrub=True)`.

**Then**:
- **frame-returned**: returns a valid frame.
- **no-loaded-segs**: `frame_cache["loaded_segs"]` is empty (no batch-load occurred).

#### Test: transform-noop (covers R38, R39, R41)

**Given**: Clip data with no scale/translate fields and no curves.

**When**: `_apply_transform(img, clip_data, 0.5)`.

**Then**:
- **identity**: output is byte-equal to input.

#### Test: transform-scale-half-keeps-dims (covers R40)

**Given**: `scale_x=0.5, scale_y=0.5`, anchor `(0.5, 0.5)`; solid-color image.

**When**: `_apply_transform`.

**Then**:
- **same-shape**: output.shape == input.shape.
- **center-has-color**: central region contains original color.
- **borders-black**: corners are `(0,0,0)` (zero-padded).

#### Test: transform-translate-right-black-pad (covers R41)

**Given**: `tx=0.25, ty=0, is_adjustment=False`; solid-color image.

**When**: `_apply_transform`.

**Then**:
- **left-column-black**: leftmost `int(0.25*w)` columns are zero.
- **rest-is-color**: remaining columns preserve source color.

#### Test: transform-ty-sign-flips-non-adjustment (covers R41)

**Given**: `ty=0.25, is_adjustment=False`; a top-half-red / bottom-half-blue image.

**When**: `_apply_transform`.

**Then**:
- **image-moved-up**: a pixel originally blue at row `h//2 + 1` is now red at a lower row (image translated upward).

#### Test: transform-ty-preserved-adjustment (covers R41)

**Given**: Same image; `ty=0.25, is_adjustment=True`.

**When**: `_apply_transform`.

**Then**:
- **image-moved-down**: a pixel originally red at row `h//2 - 1` is now red at a higher row (translated downward — sign preserved).

#### Test: assemble-happy-path-produces-mp4 (covers R42)

**Given**: Project with a complete schedule (≥1 segment, audio file present, ffmpeg+ffprobe on PATH).

**When**: `assemble_final(project_dir, "out.mp4")`.

**Then**:
- **file-exists**: `out.mp4` exists.
- **has-video-stream**: ffprobe reports a video stream.
- **has-audio-stream**: ffprobe reports an aac audio stream.
- **tmp-gone**: `out.mp4.tmp.mp4` does not exist.
- **returns-path**: return value is `"out.mp4"`.

#### Test: assemble-preview-path-triggers-preview (covers R42)

**Given**: Project with `1920×1080` base video.

**When**: `assemble_final(project_dir, "out_preview.mp4")`.

**Then**:
- **preview-resolution**: ffprobe reports the output as `960×540`.
- **ultrafast-preset**: encoder preset used is `ultrafast` (observable via libx264 encoder metadata or pass-through log).

#### Test: assemble-mixdown-failure-falls-back (covers R42)

**Given**: Project whose `render_project_audio` raises (e.g., no audio tracks but legacy `audio_path` exists).

**When**: `assemble_final(project_dir, "out.mp4")`.

**Then**:
- **mp4-produced**: `out.mp4` exists with audio.
- **audio-matches-legacy**: audio duration ≈ duration of `schedule.audio_path`.
- **no-raise**: call returned normally.

#### Test: invalidate-wholesale-no-bg-requeue (covers R46, R49)

**Given**: Project with 10 cached frames + 3 fragments; active playback worker.

**When**: `invalidate_frames_for_mutation(project_dir, None)`.

**Then**:
- **frames-dropped**: returned `frames_dropped == 10`.
- **fragments-dropped**: returned `fragments_dropped == 3`.
- **coordinator-notified**: `RenderCoordinator.invalidate_project` was called once.
- **no-bg-requeue**: `invalidate_ranges_in_background` was NOT called.

#### Test: invalidate-range-triggers-bg-requeue (covers R48, R49)

**Given**: Same project as above.

**When**: `invalidate_frames_for_mutation(project_dir, [(5.0, 9.0)])`.

**Then**:
- **coordinator-project**: `invalidate_project` called.
- **coordinator-bg-requeue**: `invalidate_ranges_in_background(project_dir, [(5.0, 9.0)])` called exactly once.
- **returns-counts**: return is `(int, int)`.

#### Test: invalidate-empty-list-is-wholesale (covers R47)

**Given**: Project with some cached entries.

**When**: `invalidate_frames_for_mutation(project_dir, [])`.

**Then**:
- **wholesale-drop**: `invalidate_project` called on both caches.
- **no-bg-requeue**: `invalidate_ranges_in_background` NOT called.

### Edge Cases

#### Test: render-overlay-zorder-respected (covers R28)

**Given**: Project DB returns tracks in order `[base, t_hi(zOrder=5), t_lo(zOrder=2)]`; two overlays paint distinct solid colors at the same t.

**When**: `render_frame_at(schedule, t)`.

**Then**:
- **lo-then-hi**: output shows `t_hi`'s color (painted last), regardless of DB insert order.

#### Test: invalidate-frame-cache-import-fails (covers R50, R51)

**Given**: `scenecraft.render.frame_cache` import raises `ImportError`.

**When**: `invalidate_frames_for_mutation(project_dir, None)`.

**Then**:
- **frames-zero**: returned `frames_dropped == 0`.
- **fragments-tried**: fragment cache invalidation still attempted.
- **no-raise**: call returns normally.

#### Test: invalidate-coordinator-failure-swallowed (covers R50)

**Given**: `RenderCoordinator.invalidate_project` raises.

**When**: `invalidate_frames_for_mutation(project_dir, None)`.

**Then**:
- **returns-counts**: `(frames_dropped, fragments_dropped)` tuple returned.
- **no-raise**: call returns normally.

#### Test: invalidate-drops-invalid-range (covers R47, R48)

**Given**: `ranges = [(20.0, 10.0), (30.0, 40.0)]`.

**When**: `invalidate_frames_for_mutation(project_dir, ranges)`.

**Then**:
- **invalid-dropped**: only `(30.0, 40.0)` is passed to `invalidate_ranges`.
- **bg-requeue-called**: coordinator bg-requeue gets `[(30.0, 40.0)]`.

#### Test: invalidate-never-raises-under-arbitrary-failure (covers R50)

**Given**: All three subsystems (frame cache, fragment cache, coordinator) each raise independently.

**When**: `invalidate_frames_for_mutation(project_dir, [(0.0, 1.0)])`.

**Then**:
- **returns-zero-zero**: returns `(0, 0)`.
- **no-raise**: no exception surfaces to caller.

#### Test: render-concurrency-same-schedule-two-threads (covers R36)

**Given**: Same schedule, two threads each calling `render_frame_at(schedule, t, frame_cache=own_dict)` with independent `frame_cache` dicts.

**When**: Both threads render the same `t` concurrently.

**Then**:
- **outputs-equal**: both output frames are byte-equal.
- **no-raise**: neither thread raises.

> Note: This is a weak concurrency contract — it only asserts safety with **separate** `frame_cache` dicts. Sharing a `frame_cache` across threads is explicitly unsupported (see Non-Goals).

#### Test: assemble-zero-duration-short-circuits (covers R57)

**Given**: Project with no base segments (`duration_seconds == 0`).

**When**: `assemble_final(project_dir, "out.mp4")`.

**Then**:
- **no-writer-opened**: `cv2.VideoWriter` is NOT instantiated (observable via mock / spy).
- **no-ffmpeg-call**: no ffmpeg subprocess spawned.
- **no-output-file**: `out.mp4` does not exist on disk.
- **returns-success**: function returns without raising (return value may be `output_path` or `None`; spec leaves exact return value for product).

#### Test: assemble-snapshot-immune-to-mid-render-invalidate (covers R58)

**Given**: An `assemble_final` loop is rendering; a concurrent coroutine calls `invalidate_frames_for_mutation(project_dir, None)` halfway through.

**When**: The render loop continues.

**Then**:
- **render-completes**: output mp4 is produced with all `total_output_frames` frames.
- **snapshot-frames-used**: frames reflect the t=0 schedule snapshot, not any post-invalidate DB state.
- **no-abort-signal**: no exception raised by the render loop from coordinator dirty state.

#### Test: assemble-no-lock-across-render-loop (negative — INV-1, R58)

**Given**: Inspection of `assemble_final` source.

**When**: Inspected.

**Then**:
- **no-threading-lock**: no `threading.Lock` / `asyncio.Lock` acquired across the render loop.
- **no-per-project-mutex**: no global `_render_locks[project_dir]` pattern.

#### Test: assemble-videowriter-false-raises-rendererror (covers R52)

**Given**: `cv2.VideoWriter.write` is patched to return `False` on the 5th frame.

**When**: `assemble_final` runs.

**Then**:
- **raises-rendererror**: `RenderError` raised with message containing "VideoWriter rejected frame" and the time.
- **no-mux-attempted**: ffmpeg mux subprocess is NOT spawned.
- **tmp-cleaned**: `.tmp.mp4` does not exist on disk after the raise (via R53 finally).

#### Test: assemble-ffmpeg-missing-preflight-error (covers R44)

**Given**: `shutil.which("ffmpeg")` returns `None`.

**When**: `assemble_final(project_dir, "out.mp4")` called.

**Then**:
- **raises-missing-dep**: `MissingDependencyError` raised at entry with message "ffmpeg not found".
- **no-schedule-built**: `build_schedule` was NOT invoked.
- **no-writer-opened**: `cv2.VideoWriter` NOT instantiated.

#### Test: assemble-tmp-cleanup-on-crash-via-finally (covers R53)

**Given**: `_mux_audio` raises `RuntimeError("ffmpeg failed")` mid-mux; a `.tmp.mp4` exists on disk at that point.

**When**: `assemble_final` runs.

**Then**:
- **exception-propagates**: the `RuntimeError` reaches the caller.
- **tmp-removed**: `.tmp.mp4` does NOT exist on disk (finally block unlinked it).

#### Test: evaluate-curve-clamps-x-to-0-1 (covers R54)

**Given**: Curve `[[0, 0], [0.5, 0.5], [1, 1]]`.

**When**: `_evaluate_curve(curve, x)` called with x values `-0.5, 0.0, 1.0, 1.5`.

**Then**:
- **negative-clamped**: `_evaluate_curve(curve, -0.5) == 0.0`.
- **over-one-clamped**: `_evaluate_curve(curve, 1.5) == 1.0`.
- **boundary-identity**: `_evaluate_curve(curve, 0.0) == 0.0` and `(..., 1.0) == 1.0`.

#### Test: transform-scale-zero-returns-black-frame (covers R55)

**Given**: Solid-red image, clip_data with `scale_x=0.0, scale_y=1.0`.

**When**: `_apply_transform(img, clip_data, 0.5)`.

**Then**:
- **returns-black**: output is all zeros, dtype uint8.
- **same-shape**: output.shape == input.shape.

#### Test: resize-interpolation-by-direction (covers R56)

**Given**: Spy on `cv2.resize` calls.

**When**: Renders involving both downscale (e.g., overlay source 4K → frame 1080p) and upscale (e.g., scale=1.5) occur.

**Then**:
- **downscale-uses-area**: `cv2.resize` invocations where target pixel count < source pixel count pass `interpolation=cv2.INTER_AREA`.
- **upscale-uses-linear**: invocations where target > source pass `interpolation=cv2.INTER_LINEAR`.
- **identity-uses-linear**: invocations where target == source pass `INTER_LINEAR`.

#### Test: schedule-intel-file-auto-discovered (covers R14)

**Given**: `meta._intel_path` is unset, but `project_dir/audio_intelligence_v2.json` exists.

**When**: `build_schedule(project_dir)`.

**Then**:
- **events-loaded**: `len(schedule.effect_events) > 0`.
- **no-hard-cut**: no event has `effect == "hard_cut"`.
- **sorted**: `effect_events` is sorted by `time` ascending.

---

## Non-Goals

- Generating selected transition videos / keyframe PNGs — handled upstream by generation pipelines.
- Enforcing schema for effect types beyond what `_apply_frame_effects` dispatches.
- Thread-safe sharing of a single `frame_cache` dict across concurrent renderers. Callers give each thread its own cache.
- Validating that `ffmpeg` / `ffprobe` exist before invoking subprocess; the pipeline assumes a correctly-provisioned host.
- GPU-accelerated rendering.
- Re-building schedule on the fly mid-render when the project DB changes under the loop. The render is a point-in-time snapshot; live edits must re-trigger `assemble_final`.
- Recovering or cleaning up orphaned `{output_path}.tmp.mp4` files from prior crashes.

---

## Open Questions

### Resolved

- **OQ-1 (schedule rebuild mid-render)**: **Resolved 2026-04-27**. Snapshot-at-start semantics (R58). Closed under INV-1 single-writer; render holds schedule from t=0; coord signals do not abort. Cross-ref cache-invalidation OQ-4 / R31. Negative-assertion test `assemble-no-lock-across-render-loop`.
- **OQ-2 (cv2.VideoWriter failure unchecked)**: **Resolved 2026-04-27**. Check return; raise `RenderError` on False; no mux on partial tmp (R52).
- **OQ-3 (ffmpeg missing from PATH)**: **Resolved 2026-04-27**. Preflight `shutil.which("ffmpeg")`; raise `MissingDependencyError` before any work (R44).
- **OQ-4 (orphaned .tmp.mp4 on crash)**: **Resolved 2026-04-27**. `try/finally` cleanup (R53).
- **OQ-5 (curve x outside [0,1])**: **Resolved 2026-04-27 as codified**. `_evaluate_curve` clamps as contract (R54).
- **OQ-6 (scale=0 short-circuits to identity)**: **Resolved 2026-04-27**. Returns black frame of target dims (R55); current identity-return is a bug fixed in refactor.
- **OQ-7 (INTER_LINEAR vs INTER_AREA inconsistency)**: **Resolved 2026-04-27**. Direction-based: INTER_AREA for downscale, INTER_LINEAR for upscale/identity (R56).
- **OQ-8 (zero-duration schedule)**: **Resolved 2026-04-27**. Short-circuit before opening VideoWriter; return success with no output file (R57).
- **INV-7 (per-working-copy cache partitioning)**: Codified via R59; cross-ref cache-invalidation R27.

### Open (none remaining)

- **OQ-1 — Schedule rebuild mid-render / coordinator invalidation race**: If a mutating endpoint fires `invalidate_frames_for_mutation` while an `assemble_final` loop is running, `RenderCoordinator.invalidate_project` flips the project's dirty bit. `assemble_final` itself doesn't poll the coordinator; it holds the schedule it built at t=0 of the loop and finishes with stale data. Do we (a) accept stale-output-for-this-run as the contract, (b) abort the render on coordinator dirty, or (c) rebuild + restart? Current code does (a) implicitly. Behavior Table row 34.
- **OQ-2 — `cv2.VideoWriter` failure mid-frame**: `out.write(frame)` returns `False` on failure, which is never checked. If the writer fails partway, we still call `out.release()` and proceed to ffmpeg mux on a partial/truncated `.tmp.mp4`. Do we detect + fail fast, or treat writer returns as advisory? Behavior Table row 35.
- **OQ-3 — ffmpeg / ffprobe missing from PATH**: `subprocess.run([... check=True])` raises `FileNotFoundError` (or `CalledProcessError`); `assemble_final` does not catch it. Do we (a) propagate (current), (b) pre-flight check with a friendly error, or (c) fall back to a pure-Python writer? Behavior Table row 36.
- **OQ-4 — Orphaned `.tmp.mp4` on crash**: If `_mux_audio` raises (ffmpeg missing, audio file invalid, disk full) the code path never reaches `Path(tmp_path).unlink(...)`, leaving the tmp next to the (non-existent) final output. No cleanup hook exists. Do we wrap mux in try/finally, or accept orphan files? Behavior Table row 37.
- **OQ-5 — Transform curve evaluation with `x > 1.0`**: `_evaluate_curve` clamps `p` to `[0, 1]` internally. But callers multiply and add (e.g., `ext + raw_progress * (1 - 2*ext)`), which stays in-range. Progress is also clamped to `[0, 0.999]` before reaching `_apply_transform`. Is the `[0,1]` contract on `_evaluate_curve` part of the spec or an implementation detail subject to change? Behavior Table row 38.
- **OQ-6 — `_apply_transform` with scale=0**: When `scale_x=0` or `scale_y=0`, `new_w` or `new_h` becomes 0 and `cv2.resize` would error; the code short-circuits (`if new_w > 0 and new_h > 0`), returning the image **unchanged**. Intuitively scale=0 should yield a black frame. Which is the contract? Behavior Table row 39.
- **OQ-7 — `cv2.resize` interpolation choice**: The code mixes `INTER_LINEAR` (transform, overlay read, frame-effects zoom) with `INTER_AREA` (preview downscale in `_ensure_loaded` + base-segment fit). No spec rationale is captured; pixel outputs differ measurably between the two. Is this intentional or incidental? Behavior Table row 40.
- **OQ-8 — Zero-duration schedule**: `total_output_frames == 0`; the loop runs zero times, `cv2.VideoWriter` closes an empty container, ffmpeg mux receives an empty video. Does `assemble_final` return success with a 0-frame mp4, raise, or short-circuit before opening the writer? Edge test `assemble-zero-duration-schedule`.

---

## Related Artifacts

- **Audit**: `agent/reports/audit-2-architectural-deep-dive.md` §1D (Render Pipeline, units 6–10).
- **Sibling specs** (to be authored): `local.engine-generation-pipelines`, `local.engine-proxy-pipeline`, `local.engine-preview-playback`, `local.engine-providers`.
- **Upstream sources**:
  - `scenecraft-engine/src/scenecraft/render/narrative.py` (`assemble_final`, `_blend_frames`, `_evaluate_curve`, `_mux_audio`).
  - `scenecraft-engine/src/scenecraft/render/schedule.py` (`build_schedule`, `Schedule` dataclass).
  - `scenecraft-engine/src/scenecraft/render/compositor.py` (`render_frame_at`, `_apply_transform`, `_apply_color_grading`, `_apply_frame_effects`, `_composite_overlays`, `_get_frame_at`, `_ensure_loaded`, `_prime_segments`).
  - `scenecraft-engine/src/scenecraft/render/cache_invalidation.py` (`invalidate_frames_for_mutation`).

---

## Notes

- The pipeline is a **point-in-time snapshot renderer**: once `build_schedule` returns, the render is deterministic for that snapshot. Any mid-render mutation to `project.db` is invisible to the running loop.
- The three cache-invalidation layers (L1 frame, fMP4 fragment, coordinator) are intentionally decoupled by broad `try/except` — a failure in any one must not nil the write path that called the invalidator. This is a design invariant, not an accident; preserve it.
- The pipeline mirrors a WebGL/canvas frontend compositor. `_blend_frames` modes and `_apply_color_grading` curve semantics are chosen to match the browser preview exactly. Divergence between this backend and the frontend preview is a bug, and any tests added here should be mirrored on the frontend side.
- `preview=True` is an output-fidelity knob, not a behavior knob: resolution halves, interpolation flips to `INTER_AREA` in the base-segment fit path, and the encoder switches to `ultrafast crf 28`. Overlay composition, crossfade math, and effect intensities are identical.

---

**Namespace**: local
**Spec**: engine-render-pipeline
**Version**: 1.0.0
**Created**: 2026-04-27
**Last Updated**: 2026-04-27
**Status**: Draft
