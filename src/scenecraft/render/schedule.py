"""Render schedule: pre-computed state needed to render any frame of a project.

Extracted from narrative.py's assemble_final so that individual frames can be
rendered on demand (for preview / scrub) without duplicating compositor setup.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class Schedule:
    """Result of build_schedule — all state required to render any frame in [0, duration].

    Produced once per session; consumed by render_frame_at() for each frame.
    The dict shapes are preserved from the previous inline code in assemble_final
    so behavior is identical.
    """

    segments: list[dict]                 # base track segment schedule
    overlay_tracks: list[dict]           # overlay track list (each track: {blend_mode, opacity, clips})
    effect_events: list[dict]            # beat effect timeline
    suppressions: list[dict]             # user suppressions
    meta: dict                           # from YAML meta (has _audio_resolved, _intel_path, _work_dir)
    fps: float
    width: int
    height: int
    duration_seconds: float
    crossfade_frames: int
    work_dir: Path
    audio_path: str
    preview: bool
    # Alias kept for callers that want the primary overlay dict list — each
    # clip carries its own state, matching the old inline representation.
    overlay_clips: list[dict] = field(default_factory=list)


def build_schedule(
    project_dir: Path | str,
    max_time: float | None = None,
    crossfade_frames: int | None = None,
    preview: bool = False,
) -> Schedule:
    """Build the render Schedule by reading directly from project.db.

    Produces all state needed for render_frame_at(): base track segments,
    overlay track clips, effect events, suppressions, audio path, output
    dimensions. No YAML, no round-trips.
    """
    import cv2

    from scenecraft.render.narrative import _log
    from scenecraft.db import load_project_data

    work_dir = Path(project_dir)
    data = load_project_data(work_dir)
    meta = data["meta"]
    selected_tr_dir = work_dir / "selected_transitions"
    remapped_dir = work_dir / "remapped"
    remapped_dir.mkdir(parents=True, exist_ok=True)

    def _parse_ts(ts: Any) -> float:
        parts = str(ts).split(":")
        if len(parts) == 2:
            return int(parts[0]) * 60 + float(parts[1])
        return 0.0

    fps = float(meta.get("fps", 24))
    # Crossfade frames: CLI arg > meta.crossfade_frames > default 8
    if crossfade_frames is None:
        crossfade_frames = int(meta.get("crossfade_frames", 8))
    XFADE_FRAMES = crossfade_frames

    # Load effect events if intel_path provided
    intel_path = meta.get("_intel_path")
    effect_events: list[dict] = []
    suppressions: list[dict] = []

    # Try to find intel file automatically
    if not intel_path:
        candidates = sorted(
            work_dir.glob("audio_intelligence*.json"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        if candidates:
            intel_path = str(candidates[0])

    if intel_path:
        import json as _json
        with open(intel_path) as f:
            intel_data = _json.load(f)
        from scenecraft.render.effects_opencv import _apply_rules_client
        onsets: dict[str, dict[str, list]] = {}
        for stem, bands in intel_data.get("layer1", {}).items():
            onsets[stem] = {}
            for band, bdata in bands.items():
                onsets[stem][band] = bdata.get("onsets", [])
        rules = intel_data.get("layer3_rules", [])
        layer1 = intel_data.get("layer1", {})
        effect_events = _apply_rules_client(onsets, rules, layer1=layer1)
        _log(f"Phase 2: Loaded {len(rules)} rules → {len(effect_events)} events")

    # Load user effects and suppressions
    if (work_dir / "project.db").exists():
        from scenecraft.db import get_effects, get_suppressions
        user_effects = get_effects(work_dir)
        suppressions = get_suppressions(work_dir)
        if user_effects:
            for ufx in user_effects:
                effect_events.append({
                    "time": ufx["time"], "duration": ufx["duration"],
                    "effect": ufx["type"], "intensity": ufx["intensity"],
                    "sustain": 0, "stem_source": "user",
                })
            _log(f"  + {len(user_effects)} user effects, {len(suppressions)} suppressions")

    effect_events.sort(key=lambda e: e["time"])
    # Remove hard_cuts
    effect_events = [e for e in effect_events if e.get("effect") != "hard_cut"]

    # Build base track segments from DB.
    segments: list[dict] = []
    from scenecraft.db import (
        get_keyframes as db_get_kfs_base,
        get_transitions as db_get_trs_base,
        get_transition_effects,
    )
    db_trs = [
        tr for tr in db_get_trs_base(work_dir)
        if tr.get("track_id") == "track_1" and not tr.get("deleted_at")
    ]
    db_kfs = {
        kf["id"]: kf for kf in db_get_kfs_base(work_dir)
        if kf.get("track_id", "track_1") == "track_1" and not kf.get("deleted_at")
    }

    import json as _json2

    def _parse_curve(val):
        if isinstance(val, str):
            try:
                return _json2.loads(val)
            except Exception:
                return None
        return val

    for tr in db_trs:
        from_kf = db_kfs.get(tr.get("from_id") or tr.get("from", ""))
        to_kf = db_kfs.get(tr.get("to_id") or tr.get("to", ""))
        if not from_kf or not to_kf:
            continue
        from_ts = _parse_ts(from_kf["timestamp"])
        to_ts = _parse_ts(to_kf["timestamp"])
        if to_ts <= from_ts:
            continue
        if max_time is not None and from_ts >= max_time:
            continue
        if max_time is not None and to_ts > max_time:
            to_ts = max_time

        tr_id = tr["id"]
        selected = selected_tr_dir / f"{tr_id}_slot_0.mp4"
        remap = tr["remap"] if isinstance(tr.get("remap"), dict) else {}

        try:
            tr_effects = get_transition_effects(work_dir, tr_id)
        except Exception:
            tr_effects = []

        opacity_curve = _parse_curve(tr.get("opacity_curve"))

        color_grading = {
            "red_curve": _parse_curve(tr.get("red_curve")),
            "green_curve": _parse_curve(tr.get("green_curve")),
            "blue_curve": _parse_curve(tr.get("blue_curve")),
            "black_curve": _parse_curve(tr.get("black_curve")),
            "saturation_curve": _parse_curve(tr.get("saturation_curve")),
            "hue_shift_curve": _parse_curve(tr.get("hue_shift_curve")),
            "invert_curve": _parse_curve(tr.get("invert_curve")),
            "brightness_curve": _parse_curve(tr.get("brightness_curve")),
            "contrast_curve": _parse_curve(tr.get("contrast_curve")),
            "exposure_curve": _parse_curve(tr.get("exposure_curve")),
        }
        has_grading = any(color_grading.values())

        transform_data = {
            "transform_x": tr.get("transform_x"),
            "transform_y": tr.get("transform_y"),
            "transform_x_curve": _parse_curve(tr.get("transform_x_curve")),
            "transform_y_curve": _parse_curve(tr.get("transform_y_curve")),
            "transform_scale_x_curve": _parse_curve(tr.get("transform_scale_x_curve")),
            "transform_scale_y_curve": _parse_curve(tr.get("transform_scale_y_curve")),
            "anchor_x": tr.get("anchor_x"),
            "anchor_y": tr.get("anchor_y"),
            "is_adjustment": tr.get("is_adjustment", False),
        }
        has_transform = any(
            transform_data.get(k)
            for k in (
                "transform_x", "transform_y",
                "transform_x_curve", "transform_y_curve",
                "transform_scale_x_curve", "transform_scale_y_curve",
            )
        )

        if selected.exists():
            segments.append({
                "from_ts": from_ts, "to_ts": to_ts,
                "source": str(selected), "is_still": False,
                "remap_method": remap.get("method", "linear"),
                "curve_points": remap.get("curve_points"),
                "effects": tr_effects,
                "opacity_curve": opacity_curve,
                **({k: v for k, v in color_grading.items() if v} if has_grading else {}),
                **(transform_data if has_transform else {}),
            })
        else:
            kf_image = work_dir / "selected_keyframes" / f"{tr.get('from_id') or tr.get('from', '')}.png"
            if kf_image.exists():
                segments.append({
                    "from_ts": from_ts, "to_ts": to_ts,
                    "source": str(kf_image), "is_still": True,
                    "remap_method": "linear", "curve_points": None,
                    "effects": tr_effects,
                    "opacity_curve": opacity_curve,
                    **({k: v for k, v in color_grading.items() if v} if has_grading else {}),
                    **(transform_data if has_transform else {}),
                })

    # Sort by from_ts and deduplicate overlaps (keep longest)
    segments.sort(key=lambda s: (s["from_ts"], -(s["to_ts"] - s["from_ts"])))
    deduped = []
    for seg in segments:
        if deduped and seg["from_ts"] < deduped[-1]["to_ts"]:
            continue
        deduped.append(seg)
    segments = deduped
    _log(f"  {len(segments)} base track segments (deduped from DB)")

    # Determine output resolution from first video segment
    w, h = 1920, 1080
    for seg in segments:
        if not seg["is_still"]:
            cap0 = cv2.VideoCapture(seg["source"])
            w = int(cap0.get(cv2.CAP_PROP_FRAME_WIDTH))
            h = int(cap0.get(cv2.CAP_PROP_FRAME_HEIGHT))
            cap0.release()
            break

    # Load overlay tracks from DB for multi-track compositing
    overlay_tracks: list[dict] = []
    from scenecraft.db import (
        get_keyframes as db_get_kfs,
        get_tracks,
        get_transitions as db_get_trs,
    )
    tracks = get_tracks(work_dir)
    # Sort by zOrder ascending — track_1 (zOrder 0) is base, higher zOrder overlays on top
    tracks.sort(key=lambda t: t.get("z_order", 0))
    all_db_kfs = db_get_kfs(work_dir)
    all_db_trs = db_get_trs(work_dir)

    # Effective mute: track.muted OR (anySolo AND NOT this.solo). Matches
    # DAW convention — solo'ing any track implicitly mutes all non-solo'd.
    any_solo = any(t.get("solo", False) for t in tracks)

    for track in tracks[1:]:  # skip base track
        if track.get("muted", False) or track.get("hidden", False):
            continue
        if any_solo and not track.get("solo", False):
            continue
        tid = track["id"]
        blend_mode = track.get("blend_mode", "normal")
        opacity = track.get("base_opacity", 1.0)
        tkfs = sorted(
            [kf for kf in all_db_kfs if kf.get("track_id") == tid and not kf.get("deleted_at")],
            key=lambda k: _parse_ts(k["timestamp"]),
        )
        ttrs = [tr for tr in all_db_trs if tr.get("track_id") == tid and not tr.get("deleted_at")]

        overlay_clips_local = []
        for tr in ttrs:
            if tr.get("hidden"):
                continue
            from_kf = next((k for k in tkfs if k["id"] == tr["from"]), None)
            to_kf = next((k for k in tkfs if k["id"] == tr["to"]), None)
            if not from_kf or not to_kf:
                continue
            ft = _parse_ts(from_kf["timestamp"])
            tt = _parse_ts(to_kf["timestamp"])
            sel = tr.get("selected")
            video_path = work_dir / "selected_transitions" / f"{tr['id']}_slot_0.mp4"
            tr_opacity = tr.get("opacity")
            tr_opacity_curve = tr.get("opacity_curve")
            tr_blend = tr.get("blend_mode") or blend_mode
            tr_effects = get_transition_effects(work_dir, tr["id"])
            clip_data = {
                "from_ts": ft, "to_ts": tt,
                "opacity": tr_opacity, "opacity_curve": tr_opacity_curve,
                "blend_mode": tr_blend, "effects": tr_effects,
                "red_curve": tr.get("red_curve"), "green_curve": tr.get("green_curve"),
                "blue_curve": tr.get("blue_curve"),
                "black_curve": tr.get("black_curve"),
                "saturation_curve": tr.get("saturation_curve"),
                "hue_shift_curve": tr.get("hue_shift_curve"),
                "invert_curve": tr.get("invert_curve"),
                "brightness_curve": tr.get("brightness_curve"),
                "contrast_curve": tr.get("contrast_curve"),
                "exposure_curve": tr.get("exposure_curve"),
                "is_adjustment": tr.get("is_adjustment", False),
                "mask_center_x": tr.get("mask_center_x"), "mask_center_y": tr.get("mask_center_y"),
                "mask_radius": tr.get("mask_radius"), "mask_feather": tr.get("mask_feather"),
                "transform_x": tr.get("transform_x"), "transform_y": tr.get("transform_y"),
                "transform_x_curve": tr.get("transform_x_curve"),
                "transform_y_curve": tr.get("transform_y_curve"),
                "transform_scale_x_curve": tr.get("transform_scale_x_curve"),
                "transform_scale_y_curve": tr.get("transform_scale_y_curve"),
                "remap_method": tr.get("remap", {}).get("method", "linear") if isinstance(tr.get("remap"), dict) else "linear",
                "curve_points": tr.get("remap", {}).get("curve_points") if isinstance(tr.get("remap"), dict) else None,
            }
            if sel and sel not in (0, "null") and video_path.exists():
                clip_data.update({"video": str(video_path), "still": None})
                overlay_clips_local.append(clip_data)
            else:
                kf_img = work_dir / "selected_keyframes" / f"{tr['from']}.png"
                if kf_img.exists():
                    clip_data.update({"video": None, "still": str(kf_img)})
                    overlay_clips_local.append(clip_data)

        # Also add keyframes that have no outgoing transition (hold stills)
        tr_from_ids = {tr["from"] for tr in ttrs}
        for kf in tkfs:
            if kf["id"] not in tr_from_ids:
                kf_img = work_dir / "selected_keyframes" / f"{kf['id']}.png"
                if kf_img.exists():
                    kft = _parse_ts(kf["timestamp"])
                    next_kf = next((k for k in tkfs if _parse_ts(k["timestamp"]) > kft), None)
                    end_t = _parse_ts(next_kf["timestamp"]) if next_kf else kft + 1.0
                    overlay_clips_local.append({
                        "from_ts": kft, "to_ts": end_t,
                        "video": None, "still": str(kf_img),
                    })

        if overlay_clips_local:
            overlay_tracks.append({
                "blend_mode": blend_mode,
                "opacity": opacity,
                "clips": overlay_clips_local,
            })
            _log(
                f"  Overlay track {tid}: {len(overlay_clips_local)} clips, "
                f"blend={blend_mode}, opacity={opacity}"
            )

    # Sort overlay clips by from_ts for crossfade boundary detection
    for otrack in overlay_tracks:
        otrack["clips"].sort(key=lambda c: c["from_ts"])

    # Apply preview halving if requested — must match assemble_final's behaviour.
    if preview:
        w, h = w // 2, h // 2

    # Compute total output duration
    end_time = segments[-1]["to_ts"] if segments else 0.0

    audio_path = meta.get("_audio_resolved") or ""

    # Flatten overlay_clips alias (all clips across all overlay tracks, in order)
    flat_overlay_clips: list[dict] = []
    for otrack in overlay_tracks:
        flat_overlay_clips.extend(otrack["clips"])

    return Schedule(
        segments=segments,
        overlay_tracks=overlay_tracks,
        effect_events=effect_events,
        suppressions=suppressions,
        meta=meta,
        fps=fps,
        width=w,
        height=h,
        duration_seconds=end_time,
        crossfade_frames=XFADE_FRAMES,
        work_dir=work_dir,
        audio_path=audio_path,
        preview=preview,
        overlay_clips=flat_overlay_clips,
    )
