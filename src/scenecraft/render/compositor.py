"""Per-frame compositor: render_frame_at(Schedule, t) -> np.ndarray.

Extracted from narrative.py's assemble_final so that individual frames can be
rendered on demand (for preview / scrub) and so assemble_final can be
expressed as a thin loop over this function.

All helpers mirror the original closures in assemble_final; behavior is
intended to be pixel-identical to the old inline implementation.
"""

from __future__ import annotations

import math
from typing import Any

from scenecraft.render.narrative import _blend_frames, _evaluate_curve, _log
from scenecraft.render.schedule import Schedule


def _effect_category(effect: str) -> str:
    if effect in ("zoom_pulse", "zoom_bounce", "zoom"):
        return "zoom"
    if effect in ("shake_x", "shake_y", "shake"):
        return "shake"
    if effect in ("glow_swell", "glow"):
        return "glow"
    if effect in ("echo", "echo_pulse"):
        return "echo"
    if effect in ("contrast_pop",):
        return "pulse"
    return effect


def _is_suppressed(suppressions: list[dict], t: float, effect: str, is_layered: bool = False) -> bool:
    category = _effect_category(effect)
    for sup in suppressions:
        if sup["from"] <= t <= sup["to"]:
            if is_layered:
                layer_types = sup.get("layerEffectTypes")
                if not layer_types:
                    continue
                if category in layer_types or effect in layer_types:
                    return True
            else:
                et = sup.get("effectTypes")
                if et is None:
                    return True
                if category in et or effect in et:
                    return True
    return False


def _get_event_intensity(t: float, event: dict) -> float:
    event_time = event["time"]
    duration = event.get("duration", 0.2)
    sustain = event.get("sustain") or 0.0
    intensity = event.get("intensity", 0.5)
    dt = t - event_time
    if dt < 0:
        return 0.0
    attack = min(0.04, duration * 0.2)
    release = duration - attack
    if sustain > 0:
        if dt < attack:
            return intensity * (dt / attack)
        elif dt < attack + sustain:
            return intensity
        elif dt < attack + sustain + release:
            return intensity * (1.0 - (dt - attack - sustain) / release)
        return 0.0
    else:
        if dt < attack:
            return intensity * (dt / attack)
        elif dt < attack + release:
            return intensity * (1.0 - (dt - attack) / release)
        return 0.0


def _apply_frame_effects(
    frame,
    t: float,
    w: int,
    h: int,
    effect_events: list[dict],
    suppressions: list[dict],
):
    import cv2
    import numpy as np

    zoom_amount = 0.0
    zoom_bounce_active = False
    shake_x_val = 0
    shake_y_val = 0
    bright_alpha = 1.0
    bright_beta = 0
    contrast_amount = 0.0
    glow_amount = 0.0

    # Check zoom_bounce first
    for event in effect_events:
        et = event["time"]
        max_dur = event.get("duration", 0.2) + (event.get("sustain") or 0.0) + 0.5
        if et > t + 0.1:
            break
        if et + max_dur < t:
            continue
        if event["effect"] == "zoom_bounce" and _get_event_intensity(t, event) > 0.05:
            zoom_bounce_active = True
            break

    for event in effect_events:
        et = event["time"]
        max_dur = event.get("duration", 0.2) + (event.get("sustain") or 0.0) + 0.5
        if et > t + 0.1:
            break
        if et + max_dur < t:
            continue
        ei = _get_event_intensity(t, event)
        if ei < 0.01:
            continue
        if _is_suppressed(suppressions, et, event["effect"], event.get("is_layered", False)):
            continue

        effect = event["effect"]
        if effect == "zoom_pulse":
            if not zoom_bounce_active:
                zoom_amount = max(zoom_amount, 0.12 * ei)
        elif effect == "zoom_bounce":
            zoom_amount = max(zoom_amount, 0.20 * ei)
        elif effect == "shake_x":
            shake_x_val += int(8 * ei * math.sin(t * 47))
        elif effect == "shake_y":
            shake_y_val += int(5 * ei * math.cos(t * 53))
        elif effect == "flash":
            contrast_amount = max(contrast_amount, 0.4 * ei)
        elif effect == "hard_cut":
            bright_alpha = max(bright_alpha, 1.0 + 0.8 * ei)
            bright_beta = max(bright_beta, int(50 * ei))
        elif effect == "contrast_pop":
            contrast_amount = max(contrast_amount, 0.4 * ei)
        elif effect == "glow_swell":
            glow_amount = max(glow_amount, 0.3 * ei)

    if zoom_amount > 0.001:
        zoom = 1.0 + zoom_amount
        new_h, new_w = int(h * zoom), int(w * zoom)
        zoomed = cv2.resize(frame, (new_w, new_h), interpolation=cv2.INTER_LINEAR)
        top = (new_h - h) // 2
        left = (new_w - w) // 2
        frame = zoomed[top:top + h, left:left + w]
    if abs(shake_x_val) > 0 or abs(shake_y_val) > 0:
        M = np.float32([[1, 0, shake_x_val], [0, 1, shake_y_val]])
        frame = cv2.warpAffine(frame, M, (w, h), borderMode=cv2.BORDER_REFLECT)
    if bright_alpha != 1.0 or bright_beta != 0:
        frame = cv2.convertScaleAbs(frame, alpha=bright_alpha, beta=bright_beta)
    if contrast_amount > 0.01:
        contrast = 1.0 + contrast_amount
        mean = np.mean(frame)
        frame = cv2.convertScaleAbs(frame, alpha=contrast, beta=int(mean * (1 - contrast)))
    if glow_amount > 0.01:
        blurred = cv2.GaussianBlur(frame, (0, 0), 8)
        frame = cv2.addWeighted(frame, 1.0 - glow_amount, blurred, glow_amount, 0)
    return frame


def _apply_color_grading(frame, clip: dict, progress: float):
    """Apply per-clip color curves (red, green, blue, black, saturation, hue_shift, invert, brightness, contrast, exposure)."""
    import cv2
    import numpy as np

    f = frame.astype(np.float32) / 255.0

    # RGB channel multipliers (OpenCV is BGR)
    for ch, curve_key in enumerate(("blue_curve", "green_curve", "red_curve")):
        curve = clip.get(curve_key)
        if curve:
            f[:, :, ch] *= _evaluate_curve(curve, progress)

    # Black fade
    black_curve = clip.get("black_curve")
    if black_curve:
        f *= (1.0 - _evaluate_curve(black_curve, progress))

    # Saturation
    sat_curve = clip.get("saturation_curve")
    if sat_curve:
        sat = _evaluate_curve(sat_curve, progress)
        if abs(sat - 1.0) > 0.001:
            gray = np.mean(f, axis=2, keepdims=True)
            f = gray + sat * (f - gray)

    # Hue shift
    hue_curve = clip.get("hue_shift_curve")
    if hue_curve:
        shift = _evaluate_curve(hue_curve, progress)
        if shift > 0.001:
            hsv = cv2.cvtColor(np.clip(f, 0, 1).astype(np.float32), cv2.COLOR_BGR2HSV)
            hsv[:, :, 0] = (hsv[:, :, 0] / 360.0 + shift) % 1.0 * 360.0
            f = cv2.cvtColor(hsv, cv2.COLOR_HSV2BGR)

    # Invert (from curve or effect)
    inv = 0.0
    inv_curve = clip.get("invert_curve")
    if inv_curve:
        inv = _evaluate_curve(inv_curve, progress)
    if clip.get("_effect_invert"):
        inv = max(inv, clip["_effect_invert"])
    if inv > 0.001:
        f = f * (1.0 - inv) + (1.0 - f) * inv

    # Brightness (offset)
    bright_curve = clip.get("brightness_curve")
    if bright_curve:
        bright = _evaluate_curve(bright_curve, progress)
        if abs(bright) > 0.001:
            f += bright

    # Contrast (scale around 0.5)
    con_curve = clip.get("contrast_curve")
    if con_curve:
        con = _evaluate_curve(con_curve, progress)
        if abs(con - 1.0) > 0.001:
            f = (f - 0.5) * con + 0.5

    # Exposure (2^stops)
    exp_curve = clip.get("exposure_curve")
    if exp_curve:
        exp_val = _evaluate_curve(exp_curve, progress)
        if abs(exp_val) > 0.001:
            f *= (2.0 ** exp_val)

    return np.clip(f * 255, 0, 255).astype(np.uint8)


def _read_overlay_frame(oclip: dict, progress: float, ow: int, oh: int):
    """Read a frame from an overlay clip at the given progress (0-1), with remap support.

    Mutates `oclip` to cache a VideoCapture / decoded still across calls.
    """
    import cv2

    p = progress
    if oclip.get("remap_method") == "curve" and oclip.get("curve_points"):
        p = _evaluate_curve(oclip["curve_points"], p)

    frame = None
    if oclip.get("video"):
        if "_cap" not in oclip:
            oclip["_cap"] = cv2.VideoCapture(oclip["video"])
            oclip["_nframes"] = int(oclip["_cap"].get(cv2.CAP_PROP_FRAME_COUNT))
        cap = oclip["_cap"]
        n = oclip["_nframes"]
        if n > 0:
            idx = min(int(p * n), n - 1)
            cap.set(cv2.CAP_PROP_POS_FRAMES, idx)
            ret, f = cap.read()
            if ret:
                frame = cv2.resize(f, (ow, oh), interpolation=cv2.INTER_LINEAR)
    elif oclip.get("still"):
        if "_img" not in oclip:
            oclip["_img"] = cv2.imread(oclip["still"])
            if oclip["_img"] is not None:
                oclip["_img"] = cv2.resize(oclip["_img"], (ow, oh), interpolation=cv2.INTER_LINEAR)
        frame = oclip["_img"]
    return frame


def _apply_transform(img, clip_data: dict, progress: float = 0):
    import cv2
    import numpy as np

    tx_curve = clip_data.get("transform_x_curve")
    ty_curve = clip_data.get("transform_y_curve")
    tz_curve = clip_data.get("transform_z_curve")
    tx = _evaluate_curve(tx_curve, progress) if tx_curve else (clip_data.get("transform_x") or 0)
    ty = _evaluate_curve(ty_curve, progress) if ty_curve else (clip_data.get("transform_y") or 0)
    scale = _evaluate_curve(tz_curve, progress) if tz_curve else 1.0
    anchor_x = clip_data.get("anchor_x") or 0.5
    anchor_y = clip_data.get("anchor_y") or 0.5
    h, w = img.shape[:2]
    if abs(scale - 1.0) > 0.001:
        ax, ay = int(anchor_x * w), int(anchor_y * h)
        new_w, new_h = int(w * scale), int(h * scale)
        if new_w > 0 and new_h > 0:
            scaled = cv2.resize(img, (new_w, new_h), interpolation=cv2.INTER_LINEAR)
            x0 = int(anchor_x * new_w) - ax
            y0 = int(anchor_y * new_h) - ay
            if scale > 1.0:
                img = scaled[y0:y0 + h, x0:x0 + w]
            else:
                result = np.zeros_like(img)
                paste_x = max(0, -x0)
                paste_y = max(0, -y0)
                src_x = max(0, x0)
                src_y = max(0, y0)
                copy_w = min(new_w - src_x, w - paste_x)
                copy_h = min(new_h - src_y, h - paste_y)
                if copy_w > 0 and copy_h > 0:
                    result[paste_y:paste_y + copy_h, paste_x:paste_x + copy_w] = \
                        scaled[src_y:src_y + copy_h, src_x:src_x + copy_w]
                img = result
    is_adjustment = clip_data.get("is_adjustment", False)
    if tx or ty:
        dx = int(tx * w)
        dy = int((-ty if not is_adjustment else ty) * h)
        M = np.float32([[1, 0, dx], [0, 1, dy]])
        img = cv2.warpAffine(img, M, (w, h), borderMode=cv2.BORDER_CONSTANT, borderValue=(0, 0, 0))
    return img


def _apply_radial_mask(img, clip_data: dict):
    mask_r = clip_data.get("mask_radius")
    if mask_r is not None and mask_r < 1.0:
        import numpy as np
        h, w = img.shape[:2]
        cx = clip_data.get("mask_center_x", 0.5) * w
        cy = clip_data.get("mask_center_y", 0.5) * h
        feather = clip_data.get("mask_feather", 0.0)
        diag = (w ** 2 + h ** 2) ** 0.5
        Y, X = np.ogrid[:h, :w]
        dist = np.sqrt((X - cx) ** 2 + (Y - cy) ** 2) / diag
        inner = mask_r * (1.0 - feather)
        mask = np.clip(1.0 - (dist - inner) / max(mask_r - inner, 0.001), 0, 1).astype(np.float32)
        mask = mask[:, :, np.newaxis]
        img = (img.astype(np.float32) * mask).astype(np.uint8)
    return img


def _process_overlay_clip(oclip: dict, t: float, progress: float, ow: int, oh: int):
    """Read + fully process an overlay clip frame: source read → color grading → transform → mask.

    Returns (frame, opacity, blend_mode) or (None, 0, "normal") if no frame.
    """
    frame = _read_overlay_frame(oclip, progress, ow, oh)
    if frame is None:
        return None, 0, "normal"

    # Clip opacity
    opacity = 1.0
    if oclip.get("opacity_curve"):
        opacity = _evaluate_curve(oclip["opacity_curve"], progress)
    elif oclip.get("opacity") is not None:
        opacity = oclip["opacity"]

    blend = oclip.get("blend_mode") or "normal"

    # Effects (strobe, invert)
    clip_invert = 0.0
    for efx in oclip.get("effects", []):
        if not efx.get("enabled", True):
            continue
        if efx["type"] == "strobe":
            period = efx["params"].get("period", 1.0 / efx["params"].get("frequency", 8))
            duty = efx["params"].get("duty", 0.5)
            elapsed = t - oclip["from_ts"]
            if (elapsed / period) % 1 > duty:
                opacity = 0
        elif efx["type"] == "invert":
            clip_invert = efx["params"].get("amount", 1.0)
    if clip_invert > 0 and not oclip.get("invert_curve"):
        oclip["_effect_invert"] = clip_invert

    # Color grading
    has_curves = any(oclip.get(k) for k in (
        "red_curve", "green_curve", "blue_curve", "black_curve",
        "saturation_curve", "hue_shift_curve", "invert_curve",
        "brightness_curve", "contrast_curve", "exposure_curve",
    ))
    if has_curves:
        frame = _apply_color_grading(frame, oclip, progress)

    frame = _apply_transform(frame, oclip, progress)
    frame = _apply_radial_mask(frame, oclip)
    return frame, opacity, blend


def _composite_overlays(
    base_frame,
    t: float,
    ow: int,
    oh: int,
    overlay_tracks: list[dict],
    fps: float,
    XFADE_FRAMES: int,
    overlay_prev: dict,
):
    """Composite overlay tracks onto base frame at timeline time t, with crossfade at clip boundaries.

    `overlay_prev` is a mutable dict keyed by track index; used for crossfade
    continuity across frames (callers should preserve this across render_frame_at
    invocations for parity with the old single-loop version).
    """
    import cv2

    result = base_frame
    for ti, otrack in enumerate(overlay_tracks):
        track_opacity = otrack["opacity"]
        track_blend = otrack["blend_mode"]
        clips = otrack["clips"]

        active_idx = -1
        matched_clip = None
        raw_progress = 0.0
        progress = 0.0
        for ci, oclip in enumerate(clips):
            if oclip["from_ts"] <= t < oclip["to_ts"]:
                active_idx = ci
                matched_clip = oclip
                clip_dur = oclip["to_ts"] - oclip["from_ts"]
                raw_progress = (t - oclip["from_ts"]) / clip_dur if clip_dur > 0 else 0

                seg_frames = round(clip_dur * fps)
                eff_xfade_ov = min(XFADE_FRAMES, max(2, seg_frames // 4))
                eff_half_xfade_ov = (eff_xfade_ov / 2) / fps
                ext = min(eff_half_xfade_ov / clip_dur, 0.2) if clip_dur > 0 else 0
                progress = ext + raw_progress * (1.0 - 2 * ext)
                progress = max(0.0, min(0.999, progress))
                break

        if matched_clip is None:
            overlay_prev.pop(ti, None)
            continue

        if matched_clip.get("is_adjustment"):
            result = _apply_color_grading(result, matched_clip, raw_progress)
            result = _apply_radial_mask(result, matched_clip)
            continue

        frame, effect_opacity, effect_blend = _process_overlay_clip(matched_clip, t, progress, ow, oh)
        if frame is None:
            continue

        clip_blend = effect_blend if matched_clip.get("blend_mode") else track_blend
        clip_opacity = effect_opacity
        if (
            clip_opacity >= 1.0
            and matched_clip.get("opacity") is None
            and not matched_clip.get("opacity_curve")
        ):
            clip_opacity = track_opacity

        # Crossfade at clip boundaries
        clip_dur = matched_clip["to_ts"] - matched_clip["from_ts"]
        seg_frames = round(clip_dur * fps)
        eff_xfade = min(XFADE_FRAMES, max(2, seg_frames // 4))
        eff_half_xfade_s = (eff_xfade / 2) / fps

        # Start of clip
        if active_idx > 0 and (t - matched_clip["from_ts"]) < eff_half_xfade_s:
            prev_clip = clips[active_idx - 1]
            if abs(prev_clip["to_ts"] - matched_clip["from_ts"]) < 0.1:
                prev_dur = prev_clip["to_ts"] - prev_clip["from_ts"]
                prev_raw = (t - prev_clip["from_ts"]) / prev_dur if prev_dur > 0 else 0
                prev_seg_frames = round(prev_dur * fps)
                prev_eff_xfade = min(XFADE_FRAMES, max(2, prev_seg_frames // 4))
                prev_eff_half = (prev_eff_xfade / 2) / fps
                prev_ext = min(prev_eff_half / prev_dur, 0.2) if prev_dur > 0 else 0
                prev_progress = prev_ext + min(prev_raw, 1.0) * (1.0 - 2 * prev_ext)
                prev_progress = max(0.0, min(0.999, prev_progress))
                prev_frame, _, _ = _process_overlay_clip(prev_clip, t, prev_progress, ow, oh)
                if prev_frame is not None:
                    blend_t = (t - matched_clip["from_ts"]) / eff_half_xfade_s
                    alpha = 0.5 + blend_t * 0.5
                    frame = cv2.addWeighted(prev_frame, 1.0 - alpha, frame, alpha, 0)

        # End of clip
        if active_idx < len(clips) - 1 and (matched_clip["to_ts"] - t) < eff_half_xfade_s:
            next_clip = clips[active_idx + 1]
            if abs(next_clip["from_ts"] - matched_clip["to_ts"]) < 0.1:
                next_dur = next_clip["to_ts"] - next_clip["from_ts"]
                next_raw = (t - next_clip["from_ts"]) / next_dur if next_dur > 0 else 0
                next_seg_frames = round(next_dur * fps)
                next_eff_xfade = min(XFADE_FRAMES, max(2, next_seg_frames // 4))
                next_eff_half = (next_eff_xfade / 2) / fps
                next_ext = min(next_eff_half / next_dur, 0.2) if next_dur > 0 else 0
                next_progress = next_ext + max(0, next_raw) * (1.0 - 2 * next_ext)
                next_progress = max(0.0, min(0.999, next_progress))
                next_frame, _, _ = _process_overlay_clip(next_clip, t, next_progress, ow, oh)
                if next_frame is not None:
                    blend_t = (matched_clip["to_ts"] - t) / eff_half_xfade_s
                    alpha = 0.5 + blend_t * 0.5
                    frame = cv2.addWeighted(frame, alpha, next_frame, 1.0 - alpha, 0)

        overlay_prev[ti] = {"clip_idx": active_idx, "frame": frame.copy()}

        result = _blend_frames(result, frame, clip_blend, clip_opacity)

        # Release handles for passed clips
        for oclip in clips:
            if oclip["to_ts"] < t - 1.0 and "_cap" in oclip:
                oclip["_cap"].release()
                del oclip["_cap"]
            if oclip["to_ts"] < t - 1.0 and "_img" in oclip:
                del oclip["_img"]
    return result


def _ensure_loaded(
    seg_idx: int,
    segments: list[dict],
    loaded_segs: set,
    w: int,
    h: int,
    preview: bool,
) -> None:
    """Load segment frames into memory, evicting distant segments (keep ±1 for crossfade)."""
    import cv2

    seg = segments[seg_idx]
    if seg.get("_loaded") or seg["is_still"]:
        return
    keep = {seg_idx, seg_idx - 1, seg_idx + 1}
    for old_idx in list(loaded_segs):
        if old_idx not in keep and 0 <= old_idx < len(segments):
            old = segments[old_idx]
            if not old["is_still"]:
                old["_frames"] = None
                old["_loaded"] = False
                loaded_segs.discard(old_idx)
    cap = cv2.VideoCapture(seg["source"])
    frames = []
    while True:
        ret, frame = cap.read()
        if not ret:
            break
        fh, fw = frame.shape[:2]
        if fw != w or fh != h:
            frame = cv2.resize(
                frame, (w, h),
                interpolation=cv2.INTER_AREA if preview else cv2.INTER_LINEAR,
            )
        elif preview:
            frame = cv2.resize(frame, (w, h), interpolation=cv2.INTER_AREA)
        frames.append(frame)
    cap.release()
    seg["_frames"] = frames
    seg["_n"] = len(frames)
    seg["_loaded"] = True
    loaded_segs.add(seg_idx)


def _find_segment(segments: list[dict], t: float) -> int:
    """Find segment index active at time t (binary search)."""
    lo, hi = 0, len(segments) - 1
    while lo <= hi:
        mid = (lo + hi) // 2
        if segments[mid]["to_ts"] <= t:
            lo = mid + 1
        elif segments[mid]["from_ts"] > t:
            hi = mid - 1
        else:
            return mid
    return -1


def _get_frame_at(
    seg_idx: int,
    progress: float,
    segments: list[dict],
    loaded_segs: set,
    w: int,
    h: int,
    preview: bool,
):
    """Get source frame from segment at given progress (0-1), with remap."""
    import numpy as np

    seg = segments[seg_idx]
    _ensure_loaded(seg_idx, segments, loaded_segs, w, h, preview)
    use_curve = seg["remap_method"] == "curve" and seg.get("curve_points")
    p = progress
    if use_curve:
        p = _evaluate_curve(seg["curve_points"], p)
    n = seg["_n"]
    if n == 0:
        return np.zeros((h, w, 3), dtype=np.uint8)
    idx = min(int(p * n), n - 1)
    return seg["_frames"][idx]


def _prime_segments(segments: list[dict], w: int, h: int, preview: bool) -> None:
    """First-time preparation of segment state: load stills eagerly, stub video segments.

    Idempotent — safe to call repeatedly; only segments without the `_n` key
    are initialized.
    """
    import cv2

    for seg in segments:
        if "_n" in seg:
            continue
        if seg["is_still"]:
            img = cv2.imread(seg["source"])
            if img is not None:
                img = cv2.resize(img, (w, h), interpolation=cv2.INTER_LINEAR)
            seg["_frames"] = [img] if img is not None else []
            seg["_n"] = len(seg["_frames"])
        else:
            cap = cv2.VideoCapture(seg["source"])
            seg["_n"] = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
            cap.release()
            seg["_frames"] = None
            seg["_loaded"] = False


def render_frame_at(
    schedule: Schedule,
    t: float,
    *,
    frame_cache: dict | None = None,
) -> Any:
    """Render a single composited BGR frame at time t.

    Produces the same pixels as the corresponding frame of the old
    assemble_final loop at the same `(project_dir, t)`.

    `frame_cache` holds per-call mutable state (loaded segment indices,
    overlay crossfade memory). Passing the same dict across sequential
    calls enables the original loop's cache reuse; passing None creates a
    fresh one.
    """
    import cv2
    import numpy as np

    if frame_cache is None:
        frame_cache = {}

    segments = schedule.segments
    overlay_tracks = schedule.overlay_tracks
    effect_events = schedule.effect_events
    suppressions = schedule.suppressions
    fps = schedule.fps
    XFADE_FRAMES = schedule.crossfade_frames
    w = schedule.width
    h = schedule.height
    preview = schedule.preview

    loaded_segs = frame_cache.setdefault("loaded_segs", set())
    overlay_prev = frame_cache.setdefault("overlay_prev", {})
    if not frame_cache.get("_segments_primed"):
        _prime_segments(segments, w, h, preview)
        frame_cache["_segments_primed"] = True

    black_frame = np.zeros((h, w, 3), dtype=np.uint8)

    seg_idx = _find_segment(segments, t)
    if seg_idx < 0:
        frame = black_frame.copy()
    else:
        seg = segments[seg_idx]
        seg_dur = seg["to_ts"] - seg["from_ts"]

        seg_frames = round(seg_dur * fps)
        eff_xfade = min(XFADE_FRAMES, max(2, seg_frames // 4))
        eff_half_xfade = (eff_xfade / 2) / fps

        ext = min(eff_half_xfade / seg_dur, 0.2) if seg_dur > 0 else 0
        raw_progress = (t - seg["from_ts"]) / seg_dur if seg_dur > 0 else 0
        progress = ext + raw_progress * (1.0 - 2 * ext)
        progress = max(0.0, min(0.999, progress))
        frame = _get_frame_at(seg_idx, progress, segments, loaded_segs, w, h, preview)

        # Crossfade at segment boundaries — start
        if seg_idx > 0 and (t - seg["from_ts"]) < eff_half_xfade:
            prev_seg = segments[seg_idx - 1]
            if prev_seg["to_ts"] == seg["from_ts"] and prev_seg["_n"] > 0:
                blend_t = (t - seg["from_ts"]) / eff_half_xfade
                alpha = 0.5 + blend_t * 0.5
                prev_dur = prev_seg["to_ts"] - prev_seg["from_ts"]
                prev_ext = min(eff_half_xfade / prev_dur, 0.2) if prev_dur > 0 else 0
                prev_raw = (t - prev_seg["from_ts"]) / prev_dur if prev_dur > 0 else 0
                prev_progress = prev_ext + prev_raw * (1.0 - 2 * prev_ext)
                prev_progress = max(0.0, min(0.999, prev_progress))
                prev_frame = _get_frame_at(seg_idx - 1, prev_progress, segments, loaded_segs, w, h, preview)
                frame = cv2.addWeighted(prev_frame, 1.0 - alpha, frame, alpha, 0)

        # Crossfade at segment boundaries — end
        if seg_idx < len(segments) - 1 and (seg["to_ts"] - t) < eff_half_xfade:
            next_seg = segments[seg_idx + 1]
            if next_seg["from_ts"] == seg["to_ts"] and next_seg["_n"] > 0:
                blend_t = (seg["to_ts"] - t) / eff_half_xfade
                alpha = 0.5 + blend_t * 0.5
                next_dur = next_seg["to_ts"] - next_seg["from_ts"]
                next_ext = min(eff_half_xfade / next_dur, 0.2) if next_dur > 0 else 0
                next_raw = (t - next_seg["from_ts"]) / next_dur if next_dur > 0 else 0
                next_progress = next_ext + next_raw * (1.0 - 2 * next_ext)
                next_progress = max(0.0, min(0.999, next_progress))
                next_frame = _get_frame_at(seg_idx + 1, next_progress, segments, loaded_segs, w, h, preview)
                frame = cv2.addWeighted(frame, alpha, next_frame, 1.0 - alpha, 0)

        # Base track opacity curve (fade to/from black)
        if seg.get("opacity_curve"):
            opacity = _evaluate_curve(seg["opacity_curve"], raw_progress)
            opacity = max(0.0, min(1.0, opacity))
            if opacity < 0.999:
                frame = cv2.convertScaleAbs(frame, alpha=opacity, beta=0)

        # Color grading
        has_curves = any(seg.get(k) for k in (
            "red_curve", "green_curve", "blue_curve", "black_curve",
            "saturation_curve", "hue_shift_curve", "invert_curve",
            "brightness_curve", "contrast_curve", "exposure_curve",
        ))
        if has_curves:
            frame = _apply_color_grading(frame, seg, raw_progress)

        # Per-transition effects (strobe etc.)
        for efx in seg.get("effects", []):
            if not efx.get("enabled", True):
                continue
            if efx["type"] == "strobe":
                freq = efx["params"].get("frequency", 8)
                duty = efx["params"].get("duty", 0.5)
                if (progress * freq) % 1 > duty:
                    frame = np.zeros_like(frame)

        # Base track transform (X/Y/Z)
        if any(seg.get(k) for k in (
            "transform_x", "transform_y",
            "transform_x_curve", "transform_y_curve", "transform_z_curve",
        )):
            frame = _apply_transform(frame, seg, raw_progress)

    # Composite overlays first, then beat-synced effects
    frame = _composite_overlays(
        frame, t, w, h, overlay_tracks, fps, XFADE_FRAMES, overlay_prev,
    )
    frame = _apply_frame_effects(frame, t, w, h, effect_events, suppressions)
    return frame
