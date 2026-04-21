"""Slot routing for linked-audio inserts (M9 task-84).

The rule: video track z_order=N pairs with audio track display_order=N.
Drop a transition on video track z=N → linked audio lands on audio track N,
unless a clip on track N overlaps the insert time-range; in that case
bump to the next-higher slot (creating a new audio track if none exists).

"Occupied" is defined by time-range overlap with the insert window, not
mere presence of any clip on the track.
"""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path


def _log(msg: str):
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] [audio.routing] {msg}", file=sys.stderr, flush=True)


def _overlaps_range(clips: list[dict], start_time: float, end_time: float) -> bool:
    """Return True if any clip on the track overlaps [start_time, end_time)."""
    eps = 1e-6
    for c in clips:
        if c.get("deleted_at"):
            continue
        cs = float(c.get("start_time", 0))
        ce = float(c.get("end_time", 0))
        # Half-open overlap test: clips touching end-to-end are NOT overlapping
        if ce - eps > start_time and cs + eps < end_time:
            return True
    return False


def resolve_audio_track_for_insert(
    project_dir: Path,
    video_track_z: int,
    insert_start: float,
    insert_end: float,
) -> tuple[str, bool]:
    """Pick an audio track ID for the insert, creating one if needed.

    Returns (audio_track_id, created). `created=True` means a new track was
    added to the DB; the caller may want to broadcast / refresh the frontend.
    """
    from scenecraft import db as dbmod

    tracks = dbmod.get_audio_tracks(project_dir)  # already sorted by display_order
    tracks_by_slot = {t["display_order"]: t for t in tracks}

    # Start at the paired slot; if a clip overlaps the insert window, bump
    target_slot = video_track_z
    while True:
        target = tracks_by_slot.get(target_slot)
        if target is None:
            # No track at this slot — create one and use it
            new_id = dbmod.generate_id("audio_track")
            dbmod.add_audio_track(project_dir, {
                "id": new_id,
                "name": f"Audio Track {target_slot + 1}",
                "display_order": target_slot,
            })
            _log(f"created audio track slot={target_slot} id={new_id}")
            return new_id, True

        clips = dbmod.get_audio_clips(project_dir, target["id"])
        if not _overlaps_range(clips, insert_start, insert_end):
            return target["id"], False

        # Occupied at this slot — bump to next higher slot
        # Find next existing slot > target_slot, or just increment
        higher_slots = [s for s in tracks_by_slot if s > target_slot]
        target_slot = min(higher_slots) if higher_slots else target_slot + 1
