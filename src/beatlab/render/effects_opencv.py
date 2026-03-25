"""Beat-synced video effects via OpenCV — fast single-pass processing."""

from __future__ import annotations

import cv2
import math
import sys
import numpy as np
from datetime import datetime
from pathlib import Path


def _log(msg: str) -> None:
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] {msg}", file=sys.stderr, flush=True)


def apply_effects(
    video_path: str,
    output_path: str,
    beat_map: dict,
    effect_plan: object | None = None,
    fps: float | None = None,
    glow: bool = False,
) -> str:
    """Apply beat-synced effects via OpenCV in a single pass.

    Args:
        video_path: Input video path.
        output_path: Output video path.
        beat_map: Parsed beat map dict with beats and sections.
        effect_plan: EffectPlan from AI director (optional).
        fps: Override frame rate.
        glow: Enable glow/bloom effect (slower).

    Returns:
        output_path
    """
    cap = cv2.VideoCapture(video_path)
    video_fps = fps or cap.get(cv2.CAP_PROP_FPS)
    w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))

    _log(f"Applying beat-synced effects (OpenCV): {total_frames} frames, {w}x{h} @ {video_fps}fps")
    if glow:
        _log("  Glow enabled (slower)")

    # Build beat lookup
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
            section_presets[i] = {"presets": sp.presets, "intensity_curve": sp.intensity_curve}
        else:
            section_presets[i] = {"presets": ["zoom_pulse"], "intensity_curve": "linear"}

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

    # Output — write raw frames, pipe through ffmpeg for encoding
    tmp_path = output_path + ".tmp.mp4"
    out = cv2.VideoWriter(tmp_path, cv2.VideoWriter_fourcc(*"mp4v"), video_fps, (w, h))

    import time
    start_time = time.time()
    frame_num = 0

    while True:
        ret, frame = cap.read()
        if not ret:
            break

        t = frame_num / video_fps
        intensity = get_beat_intensity(t)
        sec_idx = get_section(t)
        sp = section_presets.get(sec_idx, {})
        presets = sp.get("presets", [])

        # === ZOOM (zoom_pulse, zoom_bounce) ===
        zoom_amount = 0.0
        if "zoom_pulse" in presets:
            zoom_amount = max(zoom_amount, 0.12 * intensity)
        if "zoom_bounce" in presets:
            zoom_amount = max(zoom_amount, 0.20 * intensity)

        if zoom_amount > 0.001:
            zoom = 1.0 + zoom_amount
            new_h, new_w = int(h * zoom), int(w * zoom)
            zoomed = cv2.resize(frame, (new_w, new_h), interpolation=cv2.INTER_LINEAR)
            top = (new_h - h) // 2
            left = (new_w - w) // 2
            frame = zoomed[top:top+h, left:left+w]

        # === CAMERA SHAKE (shake_x, shake_y) ===
        shake_x, shake_y = 0, 0
        if "shake_x" in presets and intensity > 0.1:
            shake_x = int(8 * intensity * math.sin(t * 47))
        if "shake_y" in presets and intensity > 0.1:
            shake_y = int(5 * intensity * math.cos(t * 53))

        if abs(shake_x) > 0 or abs(shake_y) > 0:
            M = np.float32([[1, 0, shake_x], [0, 1, shake_y]])
            frame = cv2.warpAffine(frame, M, (w, h), borderMode=cv2.BORDER_REFLECT)

        # === BRIGHTNESS FLASH (flash, hard_cut) ===
        bright_alpha = 1.0
        bright_beta = 0
        if "flash" in presets and intensity > 0.05:
            bright_alpha += 0.3 * intensity
            bright_beta = int(30 * intensity)
        if "hard_cut" in presets and intensity > 0.05:
            bright_alpha += 0.8 * intensity
            bright_beta = int(50 * intensity)

        if bright_alpha != 1.0 or bright_beta != 0:
            frame = cv2.convertScaleAbs(frame, alpha=bright_alpha, beta=bright_beta)

        # === CONTRAST POP (contrast_pop) ===
        if "contrast_pop" in presets and intensity > 0.1:
            contrast = 1.0 + 0.4 * intensity
            mean = np.mean(frame)
            frame = cv2.convertScaleAbs(frame, alpha=contrast, beta=int(mean * (1 - contrast)))

        # === GLOW (glow_swell) — optional ===
        if glow and "glow_swell" in presets and intensity > 0.05:
            alpha = 0.3 * intensity
            blurred = cv2.GaussianBlur(frame, (0, 0), 8)
            frame = cv2.addWeighted(frame, 1.0 - alpha, blurred, alpha, 0)

        out.write(frame)
        frame_num += 1

        if frame_num % 1000 == 0:
            elapsed = time.time() - start_time
            fps_actual = frame_num / elapsed
            eta = (total_frames - frame_num) / fps_actual / 60
            _log(f"  [{frame_num}/{total_frames}] {fps_actual:.0f} fps, ETA {eta:.1f}m")

    cap.release()
    out.release()

    elapsed = time.time() - start_time
    _log(f"  Effects applied in {elapsed:.0f}s ({frame_num / elapsed:.0f} fps)")

    # Re-encode with ffmpeg for proper H.264 + mux audio from original
    _log(f"  Re-encoding with H.264...")
    import subprocess
    cmd = [
        "ffmpeg", "-y",
        "-i", tmp_path,
        "-i", video_path,
        "-map", "0:v", "-map", "1:a?",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", "18",
        "-c:a", "copy",
        "-shortest",
        output_path,
    ]
    subprocess.run(cmd, capture_output=True, check=True)
    Path(tmp_path).unlink(missing_ok=True)

    _log(f"  Done: {output_path}")
    return output_path
