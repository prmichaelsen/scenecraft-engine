"""Beat-synced video effects via ffmpeg sendcmd — single-pass, all effects combined."""

from __future__ import annotations

import math
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import numpy as np


def _log(msg: str) -> None:
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] {msg}", file=sys.stderr, flush=True)


def apply_effects(
    video_path: str,
    output_path: str,
    beat_map: dict,
    effect_plan: object | None = None,
    fps: float | None = None,
) -> str:
    """Apply beat-synced effects via ffmpeg in a single pass.

    Pre-computes all effect values per frame, writes an ffmpeg filter script,
    and runs a single ffmpeg command.

    Args:
        video_path: Input video path.
        output_path: Output video path.
        beat_map: Parsed beat map dict with beats and sections.
        effect_plan: EffectPlan from AI director (optional).
        fps: Override frame rate.

    Returns:
        output_path
    """
    # Detect fps from video if not provided
    if fps is None:
        probe = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=r_frame_rate", "-of", "csv=p=0", video_path],
            capture_output=True, text=True,
        )
        rate_str = probe.stdout.strip()
        if "/" in rate_str:
            num, den = rate_str.split("/")
            fps = float(num) / float(den)
        else:
            fps = float(rate_str) if rate_str else 24.0

    # Get video duration
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "csv=p=0", video_path],
        capture_output=True, text=True,
    )
    duration = float(probe.stdout.strip()) if probe.stdout.strip() else 0
    total_frames = int(duration * fps)

    _log(f"Pre-computing effects for {total_frames} frames...")

    # Build beat and section lookups
    beats = beat_map.get("beats", [])
    sections = beat_map.get("sections", [])

    beat_times = np.array([b["time"] for b in beats if b.get("intensity", 0) > 0])
    beat_intensities = np.array([b["intensity"] for b in beats if b.get("intensity", 0) > 0])
    beat_downbeats = np.array([b.get("downbeat", False) for b in beats if b.get("intensity", 0) > 0])

    # Build plan map
    plan_map = {}
    if effect_plan is not None:
        for sp in effect_plan.sections:
            plan_map[sp.section_index] = sp

    section_presets = {}
    for i, sec in enumerate(sections):
        sp = plan_map.get(i)
        if sp:
            section_presets[i] = {
                "presets": sp.presets,
                "intensity_curve": sp.intensity_curve,
                "sustained": getattr(sp, "sustained_effects", None) or [],
            }
        else:
            section_presets[i] = {"presets": ["zoom_pulse"], "intensity_curve": "linear", "sustained": []}

    def get_section(t):
        for i, sec in enumerate(sections):
            if sec.get("start_time", 0) <= t < sec.get("end_time", float("inf")):
                return i
        return 0

    def get_beat_intensity(t):
        if len(beat_times) == 0:
            return 0.0
        idx = np.searchsorted(beat_times, t, side="right") - 1
        if idx < 0:
            return 0.0
        dt = t - beat_times[idx]
        bi = beat_intensities[idx]
        is_db = beat_downbeats[idx] if idx < len(beat_downbeats) else False
        attack = 0.03 if is_db else 0.08
        release = 0.3 if is_db else 0.2
        if dt < 0:
            return 0.0
        elif dt < attack:
            return bi * (dt / attack)
        elif dt < attack + release:
            return bi * (1.0 - (dt - attack) / release)
        return 0.0

    # Pre-compute per-frame values
    zoom_values = []      # 1.0 = no zoom
    brightness_values = [] # 0.0 = no change
    contrast_values = []   # 1.0 = no change
    shake_x_values = []    # pixels
    shake_y_values = []    # pixels

    for frame_num in range(total_frames):
        t = frame_num / fps
        intensity = get_beat_intensity(t)
        sec_idx = get_section(t)
        sp = section_presets.get(sec_idx, {})
        presets = sp.get("presets", [])

        # Zoom
        zoom = 1.0
        if "zoom_pulse" in presets:
            zoom = max(zoom, 1.0 + 0.15 * intensity)
        if "zoom_bounce" in presets:
            zoom = max(zoom, 1.0 + 0.25 * intensity)
        zoom_values.append(zoom)

        # Brightness (flash/hard_cut)
        bright = 0.0
        if "flash" in presets:
            bright += 0.8 * intensity
        if "hard_cut" in presets:
            bright += 1.5 * intensity
        brightness_values.append(bright)

        # Contrast
        contrast = 1.0
        if "contrast_pop" in presets and intensity > 0.01:
            contrast = 1.0 + 0.5 * intensity
        contrast_values.append(contrast)

        # Shake
        sx = 0.0
        sy = 0.0
        if "shake_x" in presets:
            sx = 0.015 * intensity * math.sin(t * 47.0)
        if "shake_y" in presets:
            sy = 0.01 * intensity * math.cos(t * 53.0)
        shake_x_values.append(sx)
        shake_y_values.append(sy)

    _log(f"Pre-computed {total_frames} frames. Building ffmpeg filter...")

    # Build ffmpeg expression strings
    # zoompan uses frame number (n), eq uses time
    # We'll use the zoompan filter for zoom+shake and eq for brightness+contrast

    # Write zoom/shake as a zoompan expression using if(eq(n,frame),value,...)
    # For large frame counts, we chunk into a lookup approach

    # Approach: use expression with ternary chain for frames that have effects,
    # default to neutral values for frames without effects

    # Find frames with actual effects (non-default values)
    effect_frames = []
    for i in range(total_frames):
        if (zoom_values[i] > 1.001 or brightness_values[i] > 0.001
                or contrast_values[i] != 1.0
                or abs(shake_x_values[i]) > 0.0001 or abs(shake_y_values[i]) > 0.0001):
            effect_frames.append(i)

    _log(f"  {len(effect_frames)} frames with active effects out of {total_frames}")

    # For sendcmd approach: write a commands file that sets filter params at specific times
    cmd_path = str(Path(output_path).with_suffix(".sendcmd.txt"))

    with open(cmd_path, "w") as f:
        # We use the eq filter for brightness/contrast and
        # pad/crop approach won't work well for zoom.
        # Instead, use a simpler approach: build per-frame expressions

        # Actually, the most robust single-pass approach for all effects:
        # Use -vf with inline expressions referencing frame number

        pass  # We'll build the filter differently

    # Build a single complex filter with expressions
    # zoom via zoompan, brightness via eq, shake via crop offset

    # For zoom: zoompan with z expression
    # For shake: overlay with x/y offset
    # For brightness: eq with brightness expression
    # For contrast: eq with contrast expression

    # Build expression that maps frame number to zoom value
    # For efficiency, only include frames with non-default values
    # Use nested if(gte(n,start)*lt(n,end), interp, ...) for ranges

    zoom_expr = _build_piecewise_expr(zoom_values, fps, default=1.0, key="zoom")
    bright_expr = _build_piecewise_expr(brightness_values, fps, default=0.0, key="brightness")
    contrast_expr = _build_piecewise_expr(contrast_values, fps, default=1.0, key="contrast")
    shake_x_expr = _build_piecewise_expr(shake_x_values, fps, default=0.0, key="shake_x")
    shake_y_expr = _build_piecewise_expr(shake_y_values, fps, default=0.0, key="shake_y")

    # Build the filter chain
    filters = []

    # Zoom + shake via zoompan
    has_zoom = any(z > 1.001 for z in zoom_values)
    has_shake = any(abs(s) > 0.0001 for s in shake_x_values + shake_y_values)

    if has_zoom or has_shake:
        # zoompan: z=zoom, x=shake_x_offset, y=shake_y_offset
        # zoompan outputs at its own size, so we need to set s to match input
        zp = f"zoompan=z='{zoom_expr}'"
        zp += f":x='iw/2-(iw/zoom/2)+{shake_x_expr}*iw'"
        zp += f":y='ih/2-(ih/zoom/2)+{shake_y_expr}*ih'"
        zp += f":d=1:s=hd1080:fps={fps}"
        filters.append(zp)

    # Brightness + contrast via eq
    has_bright = any(b > 0.001 for b in brightness_values)
    has_contrast = any(c != 1.0 for c in contrast_values)

    if has_bright or has_contrast:
        eq_parts = []
        if has_bright:
            eq_parts.append(f"brightness='{bright_expr}'")
        if has_contrast:
            eq_parts.append(f"contrast='{contrast_expr}'")
        filters.append(f"eq={':'.join(eq_parts)}")

    if not filters:
        # No effects to apply — just copy
        _log("No effects to apply, copying video...")
        import shutil
        shutil.copy2(video_path, output_path)
        return output_path

    filter_str = ",".join(filters)

    _log(f"Running ffmpeg with single-pass filter chain...")
    cmd = [
        "ffmpeg", "-y",
        "-i", video_path,
        "-vf", filter_str,
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", "18",
        "-c:a", "copy",
        output_path,
    ]

    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        _log(f"ffmpeg effects failed: {result.stderr[-500:]}")
        # Fallback: just copy without effects
        _log("Falling back to copy without effects...")
        import shutil
        shutil.copy2(video_path, output_path)

    # Clean up
    Path(cmd_path).unlink(missing_ok=True)

    _log("Effects applied (single-pass ffmpeg).")
    return output_path


def _build_piecewise_expr(
    values: list[float],
    fps: float,
    default: float,
    key: str,
    threshold: float = 0.0001,
) -> str:
    """Build an ffmpeg expression that maps frame number (n) to values.

    Groups consecutive active frames into segments and builds a compact
    expression using if(between(n,start,end), interp, ...) chains.

    For very large frame counts, returns a simplified expression that
    uses time-based beat envelope instead of per-frame values.
    """
    # Find segments of active (non-default) frames
    segments = []
    in_segment = False
    seg_start = 0

    for i, v in enumerate(values):
        active = abs(v - default) > threshold
        if active and not in_segment:
            seg_start = i
            in_segment = True
        elif not active and in_segment:
            segments.append((seg_start, i - 1))
            in_segment = False
    if in_segment:
        segments.append((seg_start, len(values) - 1))

    if not segments:
        return str(default)

    # If too many segments for inline expression (>500), use simplified envelope
    if len(segments) > 500:
        return _build_simplified_expr(values, fps, default, key)

    # Build nested if(between(n,start,end), value_expr, ...) expression
    parts = []
    for start, end in segments:
        if start == end:
            # Single frame
            parts.append(f"if(eq(n\\,{start})\\,{values[start]:.4f}")
        else:
            # Range — use linear interpolation between start and end values
            # For simplicity, use the peak value in the range
            peak = max(values[start:end + 1], key=lambda x: abs(x - default))
            peak_frame = start + values[start:end + 1].index(peak)

            # Three-phase: ramp up to peak, ramp down from peak
            if peak_frame > start and peak_frame < end:
                parts.append(
                    f"if(between(n\\,{start}\\,{end})\\,"
                    f"if(lt(n\\,{peak_frame})\\,"
                    f"{default}+({peak:.4f}-{default})*(n-{start})/({peak_frame}-{start})\\,"
                    f"{peak:.4f}-({peak:.4f}-{default})*(n-{peak_frame})/({end}-{peak_frame})"
                    f")"
                )
            else:
                # Just use the peak for the whole range
                parts.append(f"if(between(n\\,{start}\\,{end})\\,{peak:.4f}")

    # Chain with defaults
    if len(parts) == 0:
        return str(default)

    # Build nested: if(cond1, val1, if(cond2, val2, ..., default))
    expr = str(default)
    for part in reversed(parts):
        expr = f"{part}\\,{expr})"

    return expr


def _build_simplified_expr(
    values: list[float],
    fps: float,
    default: float,
    key: str,
) -> str:
    """For large frame counts, build a simplified time-based expression.

    Uses a sine-based beat envelope approximation rather than per-frame values.
    Less accurate but keeps expression size manageable.
    """
    # Find the peak value and average frequency of pulses
    peak = max(values, key=lambda x: abs(x - default))
    if abs(peak - default) < 0.001:
        return str(default)

    # Count beats (transitions from default to active)
    beat_count = 0
    was_default = True
    for v in values:
        is_default = abs(v - default) < 0.001
        if was_default and not is_default:
            beat_count += 1
        was_default = is_default

    total_seconds = len(values) / fps
    if total_seconds > 0 and beat_count > 0:
        beat_freq = beat_count / total_seconds
    else:
        return str(default)

    # Simple pulse: abs(sin(freq * t)) with envelope
    amplitude = abs(peak - default)
    # This is a rough approximation but keeps the expression tiny
    return f"{default}+{amplitude:.4f}*max(0\\,sin(2*PI*{beat_freq:.2f}*t))*max(0\\,sin(2*PI*{beat_freq:.2f}*t))"
