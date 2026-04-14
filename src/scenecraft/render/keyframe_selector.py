"""Smart keyframe selection for EbSynth hybrid rendering."""

from __future__ import annotations


def select_keyframes(
    beat_map: dict,
    total_frames: int,
    fps: float,
    interval: int = 12,
    base_denoise: float = 0.4,
    beat_denoise: float = 0.6,
    section_denoise: float = 0.5,
    section_styles: dict[int, str] | None = None,
    default_style: str = "artistic stylized",
    seed: int = 42,
    min_gap: int = 3,
) -> list[dict]:
    """Select keyframes for EbSynth hybrid rendering.

    Picks frames at regular intervals, beat positions, and section boundaries.
    Each keyframe gets a denoising strength based on its type.

    Args:
        beat_map: Parsed beat map dict.
        total_frames: Total number of extracted video frames.
        fps: Frame rate.
        interval: Base keyframe interval (every Nth frame).
        base_denoise: Denoising for interval keyframes.
        beat_denoise: Denoising for beat keyframes (stronger = more stylized).
        section_denoise: Denoising for section boundary keyframes.
        section_styles: Map of section_index → SD style prompt.
        default_style: Fallback style prompt.
        seed: Random seed for consistency.
        min_gap: Minimum gap between keyframes (dedup window).

    Returns:
        Sorted list of {frame, denoise, prompt, seed, type} dicts.
    """
    beats = beat_map.get("beats", [])
    sections = beat_map.get("sections", [])

    # Build candidate keyframes with priorities (higher = keep when deduping)
    candidates: dict[int, dict] = {}

    def _add(frame: int, denoise: float, kf_type: str, priority: int):
        if frame < 1 or frame > total_frames:
            return
        if frame in candidates and candidates[frame]["_priority"] >= priority:
            return
        sec_idx = _section_for_frame(frame, sections, fps)
        prompt = default_style
        if section_styles and sec_idx is not None and sec_idx in section_styles:
            prompt = section_styles[sec_idx]
        candidates[frame] = {
            "frame": frame,
            "denoise": denoise,
            "prompt": prompt,
            "seed": seed,
            "type": kf_type,
            "_priority": priority,
        }

    # 1. Interval keyframes (lowest priority)
    for f in range(1, total_frames + 1, interval):
        _add(f, base_denoise, "interval", 1)

    # 2. Section boundary keyframes (medium priority)
    for i, sec in enumerate(sections):
        start_time = sec.get("start_time", 0)
        frame = max(1, round(start_time * fps))
        _add(frame, section_denoise, "section_boundary", 2)

    # 3. Beat keyframes (highest priority)
    for beat in beats:
        intensity = beat.get("intensity", 0)
        if intensity < 0.1:
            continue  # skip silent beats
        frame = beat.get("frame", round(beat["time"] * fps))
        if frame < 1 or frame > total_frames:
            continue
        # Scale denoise by intensity
        denoise = base_denoise + (beat_denoise - base_denoise) * intensity
        _add(frame, denoise, "beat", 3)

    # 4. Always include first and last frame
    _add(1, base_denoise, "first", 4)
    _add(total_frames, base_denoise, "last", 4)

    # Sort by frame number
    sorted_kfs = sorted(candidates.values(), key=lambda k: k["frame"])

    # Deduplicate: remove keyframes within min_gap of each other (keep higher priority)
    deduped = []
    for kf in sorted_kfs:
        if deduped and kf["frame"] - deduped[-1]["frame"] < min_gap:
            # Keep the one with higher priority
            if kf["_priority"] > deduped[-1]["_priority"]:
                deduped[-1] = kf
        else:
            deduped.append(kf)

    # Remove internal _priority field
    for kf in deduped:
        del kf["_priority"]

    return deduped


def _section_for_frame(frame: int, sections: list[dict], fps: float) -> int | None:
    """Find which section index a frame belongs to."""
    t = frame / fps
    for i, sec in enumerate(sections):
        if sec.get("start_time", 0) <= t < sec.get("end_time", float("inf")):
            return i
    return None
