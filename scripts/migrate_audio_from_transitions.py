#!/usr/bin/env python3
"""One-shot migration: populate audio_clips + audio_clip_links from an
existing project's selected transition videos.

For each non-deleted transition that has a selected video, extract its audio
stream (via scenecraft.audio.extract) and create a linked audio clip
anchored to the transition's timeline range.

Usage:
    .venv/bin/python scripts/migrate_audio_from_transitions.py \\
        /mnt/storage/prmichaelsen/.scenecraft/projects/test

Or with --dry-run to preview:
    .venv/bin/python scripts/migrate_audio_from_transitions.py \\
        /mnt/storage/prmichaelsen/.scenecraft/projects/test --dry-run
"""

from __future__ import annotations

import argparse
import json
import sys
import uuid
from pathlib import Path


def _parse_timestamp(ts: str) -> float:
    """Parse 'm:ss', 'm:ss.fff', or 'H:MM:SS(.fff)' into seconds."""
    if not ts:
        return 0.0
    parts = ts.split(":")
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
    # Default convention from the repo: slot_0 for the primary
    candidates = [f"{transition_id}_slot_0.mp4", f"{transition_id}.mp4"]

    # Some projects use selected[0] as slot index; fall through to slot_0 if not present
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


def migrate(project_dir: Path, dry_run: bool = False) -> int:
    from scenecraft import db as dbmod
    from scenecraft.audio.extract import probe_audio_stream, extract_audio

    conn = dbmod.get_db(project_dir)

    # Load transitions with kf timestamps ordered by from_kf timestamp
    rows = conn.execute("""
        SELECT t.id, t.from_kf, t.to_kf, t.duration_seconds, t.selected, t.track_id,
               kf_from.timestamp AS from_ts,
               kf_to.timestamp   AS to_ts
        FROM transitions t
        LEFT JOIN keyframes kf_from ON kf_from.id = t.from_kf
        LEFT JOIN keyframes kf_to   ON kf_to.id = t.to_kf
        WHERE t.deleted_at IS NULL
        ORDER BY kf_from.timestamp
    """).fetchall()

    # Video tracks (z_order defines slot)
    v_tracks = conn.execute("SELECT id, z_order FROM tracks ORDER BY z_order").fetchall()
    v_track_by_id = {r["id"]: r["z_order"] for r in v_tracks}

    # Ensure audio tracks exist for every video z_order we might target
    a_tracks = {r["display_order"]: r["id"] for r in conn.execute("SELECT id, display_order FROM audio_tracks").fetchall()}

    created_audio_tracks: dict[int, str] = {}
    created_audio_clips: list[dict] = []
    skipped: list[str] = []

    for r in rows:
        tr_id = r["id"]
        from_ts = _parse_timestamp(r["from_ts"] or "")
        to_ts = _parse_timestamp(r["to_ts"] or "")
        if to_ts <= from_ts:
            skipped.append(f"{tr_id}: degenerate range {from_ts}..{to_ts}")
            continue

        video = _selected_video_path(project_dir, tr_id, r["selected"])
        if video is None:
            skipped.append(f"{tr_id}: no selected video file found")
            continue

        info = probe_audio_stream(video)
        if info is None:
            skipped.append(f"{tr_id}: no audio stream in {video.name}")
            continue

        # Determine paired audio slot from the transition's video track z_order
        z = v_track_by_id.get(r["track_id"], 0)

        # Ensure audio track at slot z
        audio_track_id = a_tracks.get(z) or created_audio_tracks.get(z)
        if audio_track_id is None:
            audio_track_id = dbmod.generate_id("audio_track")
            created_audio_tracks[z] = audio_track_id
            if not dry_run:
                dbmod.add_audio_track(project_dir, {
                    "id": audio_track_id,
                    "name": f"Audio Track {z + 1}",
                    "display_order": z,
                })

        # Extract audio file
        if dry_run:
            extracted_rel = f"<staged>/{video.stem}.<ext>"
        else:
            extracted = extract_audio(video, project_dir)
            if extracted is None:
                skipped.append(f"{tr_id}: extract returned None (no audio)")
                continue
            # Store as project-relative path
            extracted_rel = str(extracted.relative_to(project_dir))

        # Create audio clip row
        clip_id = dbmod.generate_id("audio_clip")
        clip = {
            "id": clip_id,
            "track_id": audio_track_id,
            "source_path": extracted_rel,
            "start_time": from_ts,
            "end_time": to_ts,
            "source_offset": 0.0,
        }
        if not dry_run:
            dbmod.add_audio_clip(project_dir, clip)
            dbmod.add_audio_clip_link(project_dir, clip_id, tr_id, offset=0.0)
        created_audio_clips.append({**clip, "transition_id": tr_id})

    # Report
    print(f"Project: {project_dir}")
    print(f"{'DRY RUN — no DB writes' if dry_run else 'Migration applied'}")
    print(f"Audio tracks created: {len(created_audio_tracks)}")
    for slot, aid in created_audio_tracks.items():
        print(f"  slot {slot} → {aid}")
    print(f"Audio clips created: {len(created_audio_clips)}")
    for c in created_audio_clips:
        print(f"  {c['id']} linked to {c['transition_id']} @ [{c['start_time']:.2f}..{c['end_time']:.2f}] src={c['source_path']}")
    if skipped:
        print(f"Skipped: {len(skipped)}")
        for s in skipped:
            print(f"  {s}")

    return 0 if not skipped or len(created_audio_clips) > 0 else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("project_dir", type=Path, help="Path to the project directory (contains project.db)")
    parser.add_argument("--dry-run", action="store_true", help="Preview without writing to DB or extracting files")
    args = parser.parse_args()
    if not (args.project_dir / "project.db").exists():
        print(f"ERROR: {args.project_dir} has no project.db", file=sys.stderr)
        return 2
    return migrate(args.project_dir, dry_run=args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
