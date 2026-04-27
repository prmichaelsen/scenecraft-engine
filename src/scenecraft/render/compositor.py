"""Per-frame compositor: render_frame_at(Schedule, t) -> np.ndarray.

Extracted from narrative.py's assemble_final so that individual frames can be
rendered on demand (for preview / scrub) and so assemble_final can be
expressed as a thin loop over this function.

All helpers mirror the original closures in assemble_final; behavior is
intended to be pixel-identical to the old inline implementation.
"""

from __future__ import annotations

import math
import time
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
    # Independent scale X / scale Y (replaced the single uniform transform_z_curve
    # in 0.25.x). Each defaults to 1.0 when its curve is absent. Uniform zoom is
    # still expressible by setting both curves identical; migration from the old
    # z curve does exactly that for pre-existing projects, preserving renders.
    sx_curve = clip_data.get("transform_scale_x_curve")
    sy_curve = clip_data.get("transform_scale_y_curve")
    tx = _evaluate_curve(tx_curve, progress) if tx_curve else (clip_data.get("transform_x") or 0)
    ty = _evaluate_curve(ty_curve, progress) if ty_curve else (clip_data.get("transform_y") or 0)
    scale_x = _evaluate_curve(sx_curve, progress) if sx_curve else 1.0
    scale_y = _evaluate_curve(sy_curve, progress) if sy_curve else 1.0
    anchor_x = clip_data.get("anchor_x") or 0.5
    anchor_y = clip_data.get("anchor_y") or 0.5
    h, w = img.shape[:2]
    if abs(scale_x - 1.0) > 0.001 or abs(scale_y - 1.0) > 0.001:
        ax, ay = int(anchor_x * w), int(anchor_y * h)
        new_w, new_h = int(w * scale_x), int(h * scale_y)
        if new_w > 0 and new_h > 0:
            scaled = cv2.resize(img, (new_w, new_h), interpolation=cv2.INTER_LINEAR)
            x0 = int(anchor_x * new_w) - ax
            y0 = int(anchor_y * new_h) - ay
            # Horizontal branch: crop when enlarged, pad when reduced. Same
            # for vertical. When the two axes scale in opposite directions
            # (x > 1, y < 1, or vice versa) we need mixed logic — do the
            # general pad-or-crop dance that handles both cases safely.
            result = np.zeros_like(img)
            # Source region within `scaled` we want to read from.
            src_x = max(0, x0)
            src_y = max(0, y0)
            # Destination region within `result` (original-size) where it goes.
            paste_x = max(0, -x0)
            paste_y = max(0, -y0)
            # Size to copy — bounded by both source and destination extents.
            copy_w = max(0, min(new_w - src_x, w - paste_x))
            copy_h = max(0, min(new_h - src_y, h - paste_y))
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


def _resolve_source_for_read(
    seg: dict,
    project_dir: "Path | None",
    prefer_proxy: bool,
    t_in_source: float,
) -> tuple[str, float]:
    """Decide which file to decode from, and what time-offset within it
    the seek must subtract.

    Returns `(file_path, t_file_offset)` where:
    - `file_path` is the absolute file path to open with cv2.VideoCapture.
    - `t_file_offset` is the source-time at which THAT file begins. For
      the original source or a single-file proxy this is always 0.0.
      For a chunked proxy chunk it's the chunk's `start` on the source
      timeline — callers compute `idx_in_file = int((t_in_source - offset) * fps)`.

    `t_in_source` is the absolute time within the ORIGINAL source that
    the caller wants to read (0..source_duration). For chunked proxies we
    use it to pick the containing chunk; for single-file paths it's
    ignored by the resolver (the caller already has idx_in_source = idx_in_file).

    Mode ladder (tried in order):
    1. `prefer_proxy=False` or no `project_dir` → original source, offset 0.
    2. Chunked proxy ready (manifest.json exists) → chunk file + chunk.start.
       Biggest win for long sources: the chunk is tiny so the first keyframe
       near `t_in_source` is found fast.
    3. Single-file proxy ready → single proxy path, offset 0.
    4. Neither ready → original source, offset 0; kicks off background
       generation (mode='auto' picks single vs chunked by duration).
    """
    source = seg["source"]
    if not prefer_proxy or project_dir is None:
        return (source, 0.0)
    try:
        from scenecraft.render.proxy_generator import (
            proxy_path_for, proxy_exists,
            chunked_proxy_manifest, chunk_for_time,
            ProxyCoordinator,
        )
    except ImportError:
        return (source, 0.0)

    # Prefer chunked proxy when available — even small wins per-frame
    # (smaller file → faster seek, better page cache) add up across
    # 16-thread playback + scrub bursts.
    manifest = chunked_proxy_manifest(project_dir, source)
    if manifest is not None:
        mapped = chunk_for_time(manifest, t_in_source)
        if mapped is not None:
            chunk_idx, _t_within_chunk = mapped
            chunk = manifest.chunks[chunk_idx]
            chunk_path = (
                _proxy_chunks_dir(project_dir, source) / chunk.file
            )
            if chunk_path.exists():
                return (str(chunk_path), chunk.start)

    if proxy_exists(project_dir, source):
        pp = proxy_path_for(project_dir, source)
        if pp is not None:
            return (str(pp), 0.0)

    # No proxy ready yet — kick off background gen (auto picks mode by
    # duration) and serve the original for this frame.
    try:
        ProxyCoordinator.instance().ensure_proxy(project_dir, source, mode="auto")
    except Exception:
        pass
    return (source, 0.0)


def _proxy_chunks_dir(project_dir: "Path", source_path: str) -> "Path":
    """Thin wrapper that avoids a circular import at module load time."""
    from scenecraft.render.proxy_generator import chunked_proxy_dir_for
    pd = chunked_proxy_dir_for(project_dir, source_path)
    assert pd is not None, "caller must have verified manifest; dir must exist"
    return pd


def _get_frame_at(
    seg_idx: int,
    progress: float,
    segments: list[dict],
    loaded_segs: set,
    w: int,
    h: int,
    preview: bool,
    *,
    scrub: bool = False,
    stream_caps: dict | None = None,
    project_dir: "Path | None" = None,
    prefer_proxy: bool = False,
):
    """Get source frame from segment at given progress (0-1), with remap.

    Three paths:
    - Offline render (default): `_ensure_loaded` batch-decodes every frame of
      the active segment into RAM and indexes. Fast for sequential reads on
      short clips; blows up on multi-hour segments.
    - `scrub=True` (HTTP /render-frame): opens a cv2.VideoCapture, seeks to
      the target frame, reads one, closes. O(1) memory, O(seek) CPU.
    - `stream_caps` provided (playback worker): reuses a long-lived
      VideoCapture per segment across calls, advancing sequentially on
      monotonic idx and seeking only when the cursor jumps. O(1) memory,
      cheap sequential reads. The caller owns the dict lifetime so they
      can release captures on teardown.

    `prefer_proxy=True` routes reads through a 540p proxy of the source
    when available (see proxy_generator). `project_dir` must be supplied
    for proxy resolution.
    """
    import cv2
    import numpy as np

    seg = segments[seg_idx]
    use_curve = seg["remap_method"] == "curve" and seg.get("curve_points")
    p = progress
    if use_curve:
        p = _evaluate_curve(seg["curve_points"], p)
    n = seg["_n"]
    if n == 0:
        return np.zeros((h, w, 3), dtype=np.uint8)
    idx = min(int(p * n), n - 1)

    if seg["is_still"]:
        _ensure_loaded(seg_idx, segments, loaded_segs, w, h, preview)
        return seg["_frames"][idx]

    # Resolve the file to decode from and any source-time offset the
    # chosen file begins at (non-zero only for chunked-proxy chunks).
    src_fps = float(seg.get("_fps_source") or 0.0)
    t_in_source = (idx / src_fps) if src_fps > 0 else 0.0
    effective_source, t_file_offset = _resolve_source_for_read(
        seg, project_dir, prefer_proxy, t_in_source
    )

    # Frame index within the chosen file. For non-chunked (offset=0) this
    # equals the source-frame-index, preserving the task-39 behavior
    # exactly. For chunked proxies we subtract the chunk's starting
    # frame count so the seek lands at the right frame inside the chunk.
    if t_file_offset > 0.0 and src_fps > 0:
        idx_in_file = max(0, int(round((t_in_source - t_file_offset) * src_fps)))
    else:
        idx_in_file = idx

    def _fit(frame):
        if frame is None:
            return np.zeros((h, w, 3), dtype=np.uint8)
        fh, fw = frame.shape[:2]
        if fw != w or fh != h:
            return cv2.resize(
                frame, (w, h),
                interpolation=cv2.INTER_AREA if preview else cv2.INTER_LINEAR,
            )
        if preview:
            return cv2.resize(frame, (w, h), interpolation=cv2.INTER_AREA)
        return frame

    if scrub:
        cap = cv2.VideoCapture(effective_source)
        try:
            cap.set(cv2.CAP_PROP_POS_FRAMES, idx_in_file)
            ret, frame = cap.read()
            return _fit(frame if ret else None)
        finally:
            cap.release()

    if stream_caps is not None:
        # Key the cache by (seg_idx, effective_source) so that switching
        # between proxy variants (single-file → chunked chunk A → chunked
        # chunk B as the playhead advances) transparently opens a new
        # cap instead of reading from a now-stale handle.
        cache_key = (seg_idx, effective_source)
        entry = stream_caps.get(cache_key)
        if entry is None:
            entry = {"cap": cv2.VideoCapture(effective_source), "cursor": -1}
            stream_caps[cache_key] = entry
        cap = entry["cap"]
        cursor = entry["cursor"]
        if idx_in_file != cursor + 1:
            cap.set(cv2.CAP_PROP_POS_FRAMES, idx_in_file)
        ret, frame = cap.read()
        entry["cursor"] = idx_in_file if ret else cursor
        return _fit(frame if ret else None)

    _ensure_loaded(seg_idx, segments, loaded_segs, w, h, preview)
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
            # Source fps is needed by the chunked-proxy path to map
            # source-frame-index ↔ source-time ↔ chunk-local-frame-index.
            # Falls back to 0 if unreadable; chunked path treats <=0 as
            # "can't use chunks, fall through to single-file/original".
            src_fps = float(cap.get(cv2.CAP_PROP_FPS) or 0.0)
            seg["_fps_source"] = src_fps if src_fps > 0 else 0.0
            cap.release()
            seg["_frames"] = None
            seg["_loaded"] = False


# Per-phase timing accumulator for perf audits. When a dict is passed via
# frame_cache["_timing"], each phase adds its elapsed time (seconds).
# Consumers read and reset externally.
def _tick(timing: dict | None, key: str, t0: float) -> None:
    if timing is None:
        return
    timing[key] = timing.get(key, 0.0) + (time.monotonic() - t0)


def render_frame_at(
    schedule: Schedule,
    t: float,
    *,
    frame_cache: dict | None = None,
    scrub: bool = False,
    prefer_proxy: bool = False,
) -> Any:
    """Render a single composited BGR frame at time t.

    Produces the same pixels as the corresponding frame of the old
    assemble_final loop at the same `(project_dir, t)`.

    `frame_cache` holds per-call mutable state (loaded segment indices,
    overlay crossfade memory, and — for long-lived callers like the
    preview worker — a `stream_caps` dict of open VideoCaptures keyed
    by segment index). Passing the same dict across sequential calls
    enables the original loop's cache reuse; passing None creates a
    fresh one.

    `scrub=True` opens/seeks/closes a capture per call (O(1) memory,
    slow per frame). Otherwise, when `frame_cache["stream_caps"]` is
    present, video segments stream through a kept-open capture with a
    cursor (O(1) memory, cheap sequential reads). Absent both flags,
    the legacy batch-load path runs — fine for short clips, fatal on
    multi-hour segments.
    """
    import cv2
    import numpy as np

    if frame_cache is None:
        frame_cache = {}
    stream_caps = frame_cache.get("stream_caps") if not scrub else None
    timing = frame_cache.get("_timing")  # may be None (no instrumentation)

    segments = schedule.segments
    overlay_tracks = schedule.overlay_tracks
    effect_events = schedule.effect_events
    suppressions = schedule.suppressions
    fps = schedule.fps
    XFADE_FRAMES = schedule.crossfade_frames
    w = schedule.width
    h = schedule.height
    preview = schedule.preview
    project_dir = schedule.work_dir

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
        _phase_t = time.monotonic()

        seg_frames = round(seg_dur * fps)
        eff_xfade = min(XFADE_FRAMES, max(2, seg_frames // 4))
        eff_half_xfade = (eff_xfade / 2) / fps

        ext = min(eff_half_xfade / seg_dur, 0.2) if seg_dur > 0 else 0
        raw_progress = (t - seg["from_ts"]) / seg_dur if seg_dur > 0 else 0
        progress = ext + raw_progress * (1.0 - 2 * ext)
        progress = max(0.0, min(0.999, progress))
        frame = _get_frame_at(seg_idx, progress, segments, loaded_segs, w, h, preview, scrub=scrub, stream_caps=stream_caps, project_dir=project_dir, prefer_proxy=prefer_proxy)
        _tick(timing, "base_frame", _phase_t); _phase_t = time.monotonic()

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
                prev_frame = _get_frame_at(seg_idx - 1, prev_progress, segments, loaded_segs, w, h, preview, scrub=scrub, stream_caps=stream_caps, project_dir=project_dir, prefer_proxy=prefer_proxy)
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
                next_frame = _get_frame_at(seg_idx + 1, next_progress, segments, loaded_segs, w, h, preview, scrub=scrub, stream_caps=stream_caps, project_dir=project_dir, prefer_proxy=prefer_proxy)
                frame = cv2.addWeighted(frame, alpha, next_frame, 1.0 - alpha, 0)
        _tick(timing, "crossfade", _phase_t); _phase_t = time.monotonic()

        # Base track opacity curve (fade to/from black)
        if seg.get("opacity_curve"):
            opacity = _evaluate_curve(seg["opacity_curve"], raw_progress)
            opacity = max(0.0, min(1.0, opacity))
            if opacity < 0.999:
                frame = cv2.convertScaleAbs(frame, alpha=opacity, beta=0)
        _tick(timing, "opacity", _phase_t); _phase_t = time.monotonic()

        # Color grading
        has_curves = any(seg.get(k) for k in (
            "red_curve", "green_curve", "blue_curve", "black_curve",
            "saturation_curve", "hue_shift_curve", "invert_curve",
            "brightness_curve", "contrast_curve", "exposure_curve",
        ))
        if has_curves:
            frame = _apply_color_grading(frame, seg, raw_progress)
        _tick(timing, "color_grade", _phase_t); _phase_t = time.monotonic()

        # Per-transition effects (strobe etc.)
        for efx in seg.get("effects", []):
            if not efx.get("enabled", True):
                continue
            if efx["type"] == "strobe":
                freq = efx["params"].get("frequency", 8)
                duty = efx["params"].get("duty", 0.5)
                if (progress * freq) % 1 > duty:
                    frame = np.zeros_like(frame)
        _tick(timing, "effects", _phase_t); _phase_t = time.monotonic()

        # Base track transform (translate X/Y, scale X/Y).
        if any(seg.get(k) for k in (
            "transform_x", "transform_y",
            "transform_x_curve", "transform_y_curve",
            "transform_scale_x_curve", "transform_scale_y_curve",
        )):
            frame = _apply_transform(frame, seg, raw_progress)
        _tick(timing, "transform", _phase_t); _phase_t = time.monotonic()

    # Composite overlays first, then beat-synced effects
    _ov_t = time.monotonic()
    frame = _composite_overlays(
        frame, t, w, h, overlay_tracks, fps, XFADE_FRAMES, overlay_prev,
    )
    _tick(timing, "overlays", _ov_t)
    _fx_t = time.monotonic()
    frame = _apply_frame_effects(frame, t, w, h, effect_events, suppressions)
    _tick(timing, "frame_effects", _fx_t)
    if timing is not None:
        timing["_frames"] = timing.get("_frames", 0) + 1
    return frame
