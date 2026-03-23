"""Beat map generation — convert analysis results to frame-rate-aware JSON."""

from __future__ import annotations

import json
from pathlib import Path


def time_to_frame(time_sec: float, fps: float) -> int:
    """Convert a timestamp in seconds to the nearest frame number.

    Args:
        time_sec: Time in seconds.
        fps: Frames per second (e.g. 24, 29.97, 30, 60).

    Returns:
        Nearest frame number (0-indexed).
    """
    return round(time_sec * fps)


def create_beat_map(
    analysis: dict,
    fps: float,
    source_file: str,
) -> dict:
    """Convert analysis results to a frame-rate-aware beat map.

    Args:
        analysis: Dict from analyzer.analyze_audio().
        fps: Timeline frame rate.
        source_file: Original audio file path (for metadata).

    Returns:
        Beat map dict ready for JSON serialization.
    """
    beats = [
        {
            "time": b["time"],
            "frame": time_to_frame(b["time"], fps),
            "intensity": b["intensity"],
        }
        for b in analysis["beats"]
    ]

    onsets = [
        {
            "time": o["time"],
            "frame": time_to_frame(o["time"], fps),
            "strength": o["strength"],
        }
        for o in analysis["onsets"]
    ]

    return {
        "version": "1.0",
        "source_file": str(Path(source_file).name),
        "duration": analysis["duration"],
        "tempo": analysis["tempo"],
        "fps": fps,
        "beats": beats,
        "onsets": onsets,
    }


def save_beat_map(beat_map: dict, output_path: str) -> None:
    """Write beat map to a JSON file."""
    with open(output_path, "w") as f:
        json.dump(beat_map, f, indent=2)


def load_beat_map(path: str) -> dict:
    """Load a beat map from a JSON file."""
    with open(path) as f:
        return json.load(f)
