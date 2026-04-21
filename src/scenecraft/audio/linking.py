"""Create a linked audio clip from a transition's selected video (M9 task-84).

Given an existing transition with a selected video file:
  1. Probe + extract the audio stream to audio_staging/
  2. Compute timeline [start_time, end_time] from the from_kf/to_kf timestamps
  3. Route to the paired audio track by slot (video z_order ↔ audio
     display_order), bumping on time-range overlap, creating a track if
     needed
  4. Create the audio_clips row and audio_clip_links row (offset=0)

Idempotent: if the transition already has an audio_clip_link, this returns
the existing info without re-extracting.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path


def _log(msg: str):
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] [audio.linking] {msg}", file=sys.stderr, flush=True)


def _parse_ts(ts) -> float:
    if isinstance(ts, (int, float)):
        return float(ts)
    if not ts:
        return 0.0
    parts = str(ts).split(":")
    try:
        if len(parts) == 1:
            return float(parts[0])
        if len(parts) == 2:
            return float(parts[0]) * 60 + float(parts[1])
        if len(parts) == 3:
            return float(parts[0]) * 3600 + float(parts[1]) * 60 + float(parts[2])
    except ValueError:
        return 0.0
    return 0.0


def _selected_video_path(project_dir: Path, transition_id: str, selected_json: str | None) -> Path | None:
    """Best-effort resolve the selected video file for a transition."""
    base = project_dir / "selected_transitions"
    candidates = [f"{transition_id}_slot_0.mp4", f"{transition_id}.mp4"]
    try:
        sel = json.loads(selected_json) if selected_json else None
        if isinstance(sel, list) and sel and sel[0] is not None:
            candidates.insert(0, f"{transition_id}_slot_{int(sel[0])}.mp4")
    except (json.JSONDecodeError, ValueError, TypeError):
        pass
    for name in candidates:
        p = base / name
        if p.exists() and p.stat().st_size > 0:
            return p
    return None


def link_audio_for_transition(
    project_dir: Path,
    transition_id: str,
    force: bool = False,
) -> dict:
    """Create a linked audio clip for the given transition.

    Args:
        project_dir: Project root containing project.db
        transition_id: Transition id to link audio for
        force: If True, re-link even if a link already exists for this transition
               (does NOT delete existing clips — creates an additional clip).

    Returns a status dict: {
      "status": "linked" | "exists" | "skipped" | "error",
      "transition_id": str,
      "audio_clip_id": str | None,
      "audio_track_id": str | None,
      "audio_track_created": bool,
      "reason": str | None,
    }
    """
    from scenecraft import db as dbmod
    from scenecraft.audio.extract import probe_audio_stream, extract_audio
    from scenecraft.audio.routing import resolve_audio_track_for_insert

    result = {
        "status": "skipped",
        "transition_id": transition_id,
        "audio_clip_id": None,
        "audio_track_id": None,
        "audio_track_created": False,
        "reason": None,
    }

    # Idempotency: skip if already linked (unless forced)
    existing_links = dbmod.get_audio_clip_links_for_transition(project_dir, transition_id)
    if existing_links and not force:
        result["status"] = "exists"
        result["audio_clip_id"] = existing_links[0]["audio_clip_id"]
        result["reason"] = "link already exists"
        return result

    # Load the transition + its keyframes to get timeline span and video track
    tr = dbmod.get_transition(project_dir, transition_id)
    if not tr:
        result["status"] = "error"
        result["reason"] = f"transition not found: {transition_id}"
        return result

    from_kf = dbmod.get_keyframe(project_dir, tr.get("from", ""))
    to_kf = dbmod.get_keyframe(project_dir, tr.get("to", ""))
    if not from_kf or not to_kf:
        result["status"] = "error"
        result["reason"] = "missing boundary keyframes"
        return result

    start_time = _parse_ts(from_kf.get("timestamp"))
    end_time = _parse_ts(to_kf.get("timestamp"))
    if end_time <= start_time:
        result["status"] = "skipped"
        result["reason"] = f"degenerate range [{start_time}..{end_time}]"
        return result

    # Resolve video file
    video = _selected_video_path(project_dir, transition_id, json.dumps(tr.get("selected")) if tr.get("selected") is not None else None)
    if video is None:
        result["status"] = "skipped"
        result["reason"] = "no selected video file found"
        return result

    # Probe audio stream
    info = probe_audio_stream(video)
    if info is None:
        result["status"] = "skipped"
        result["reason"] = "no audio stream"
        return result

    # Extract (idempotent — hashes source + mtime)
    try:
        extracted = extract_audio(video, project_dir)
    except RuntimeError as e:
        result["status"] = "error"
        result["reason"] = f"extract failed: {e}"
        return result
    if extracted is None:
        result["status"] = "skipped"
        result["reason"] = "extract returned None"
        return result

    # Resolve video track z_order → audio slot
    video_tracks = dbmod.get_tracks(project_dir) if hasattr(dbmod, "get_tracks") else []
    video_track_z = 0
    for t in video_tracks:
        if t.get("id") == tr.get("track_id"):
            video_track_z = int(t.get("z_order", 0) or 0)
            break

    # Route
    audio_track_id, created = resolve_audio_track_for_insert(
        project_dir, video_track_z, start_time, end_time
    )

    # Create clip + link
    clip_id = dbmod.generate_id("audio_clip")
    dbmod.add_audio_clip(project_dir, {
        "id": clip_id,
        "track_id": audio_track_id,
        "source_path": str(extracted.relative_to(project_dir)),
        "start_time": start_time,
        "end_time": end_time,
        "source_offset": 0.0,
    })
    dbmod.add_audio_clip_link(project_dir, clip_id, transition_id, offset=0.0)

    result["status"] = "linked"
    result["audio_clip_id"] = clip_id
    result["audio_track_id"] = audio_track_id
    result["audio_track_created"] = created
    _log(f"linked {transition_id} → audio_clip={clip_id} on track={audio_track_id} (created={created})")
    return result
