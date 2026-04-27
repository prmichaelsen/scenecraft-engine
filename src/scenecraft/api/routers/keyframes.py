"""Keyframe routes -- M16 T61 / T65 native port (fan-in merge).

25 routes covering the keyframe mutation surface.  ALL are now natively
implemented (calling ``db.*`` directly); zero ``dispatch_legacy`` calls
remain.

Structural routes (add / delete / batch-delete / restore / duplicate /
paste-group / insert-pool-item) gate on ``Depends(project_lock)`` so
the post-handler timeline validator runs and concurrent mutations on the
same project serialize.

Handlers are **sync** (``def``, not ``async def``).  FastAPI offloads
sync handlers to the starlette threadpool so concurrent requests across
different projects don't block each other on the event loop.
"""

from __future__ import annotations

import os
import re as _re
import shutil
import threading
import traceback
from datetime import datetime, timezone
from math import gcd
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Body, Depends, Request
from fastapi.responses import JSONResponse

from scenecraft.api.deps import project_dir, project_lock
from scenecraft.api.errors import ApiError
from scenecraft.api.models.keyframes import (
    AddKeyframeBody,
    AssignKeyframeImageBody,
    BatchDeleteKeyframesBody,
    BatchSetBaseImageBody,
    DeleteKeyframeBody,
    DuplicateKeyframeBody,
    EnhanceKeyframePromptBody,
    EscalateKeyframeBody,
    GenerateKeyframeCandidatesBody,
    GenerateKeyframeVariationsBody,
    GenerateSlotKeyframeCandidatesBody,
    InsertPoolItemBody,
    PasteGroupBody,
    RestoreKeyframeBody,
    SelectKeyframesBody,
    SelectSlotKeyframesBody,
    SetBaseImageBody,
    SuggestKeyframePromptsBody,
    UnlinkKeyframeBody,
    UpdateKeyframeLabelBody,
    UpdateKeyframeStyleBody,
    UpdatePromptBody,
    UpdateTimestampBody,
)
from scenecraft.api.utils import _log, _next_variant, _get_image_backend

router = APIRouter(tags=["keyframes"])


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _work_dir(request: Request) -> Path:
    wd = getattr(request.app.state, "work_dir", None)
    if wd is None:
        raise ApiError("INTERNAL_ERROR", "work_dir not configured", status_code=500)
    return wd


def _parse_ts(ts: Any) -> float:
    """Parse a timestamp string (``"M:SS.ss"``) or number to seconds."""
    parts = str(ts).split(":")
    if len(parts) == 2:
        return int(parts[0]) * 60 + float(parts[1])
    return float(ts) if isinstance(ts, (int, float)) else 0


def _secs_to_ts(s: float) -> str:
    """Convert seconds back to ``"M:SS.ss"`` format."""
    m = int(s) // 60
    sec = s - m * 60
    return f"{m}:{sec:05.2f}"


# ---------------------------------------------------------------------------
# Selection (non-structural) -- NATIVE
# ---------------------------------------------------------------------------


@router.post(
    "/api/projects/{name}/select-keyframes",
    operation_id="select_keyframes",
    dependencies=[Depends(project_dir)],
)
def select_keyframes(
    name: str, request: Request, body: SelectKeyframesBody
) -> dict:
    selections = body.selections
    if not selections:
        raise ApiError("BAD_REQUEST", "Missing 'selections' in body", status_code=400)

    pdir = _work_dir(request) / name
    try:
        from scenecraft.db import update_keyframe

        selected_dir = pdir / "selected_keyframes"
        selected_dir.mkdir(parents=True, exist_ok=True)

        for kf_id, variant in selections.items():
            _log(f"select-keyframes: {kf_id} v{variant}")
            cand_dir = pdir / "keyframe_candidates" / "candidates" / f"section_{kf_id}"
            source = cand_dir / f"v{variant}.png"
            if source.exists():
                shutil.copy2(str(source), str(selected_dir / f"{kf_id}.png"))
                _log(f"  copied {source} -> {selected_dir / f'{kf_id}.png'}")
            else:
                _log(f"  WARNING: candidate not found: {source}")
            update_keyframe(pdir, kf_id, selected=variant)

        return {"success": True, "applied": len(selections)}
    except ApiError:
        raise
    except Exception as e:
        raise ApiError("INTERNAL_ERROR", str(e), status_code=500)


@router.post(
    "/api/projects/{name}/select-slot-keyframes",
    operation_id="select_slot_keyframes",
    dependencies=[Depends(project_dir)],
)
def select_slot_keyframes(
    name: str, request: Request, body: SelectSlotKeyframesBody
) -> dict:
    selections = body.selections
    if not selections:
        raise ApiError("BAD_REQUEST", "Missing 'selections' in body", status_code=400)

    pdir = _work_dir(request) / name
    try:
        _log(f"select-slot-keyframes: {len(selections)} selections")
        selected_dir = pdir / "selected_slot_keyframes"
        selected_dir.mkdir(parents=True, exist_ok=True)
        slot_kf_root = pdir / "slot_keyframe_candidates" / "candidates"

        for slot_key, variant in selections.items():
            source = slot_kf_root / f"section_{slot_key}" / f"v{variant}.png"
            if source.exists():
                shutil.copy2(str(source), str(selected_dir / f"{slot_key}.png"))

        return {"success": True, "applied": len(selections)}
    except ApiError:
        raise
    except Exception as e:
        raise ApiError("INTERNAL_ERROR", str(e), status_code=500)


# ---------------------------------------------------------------------------
# Timestamp / prompt (non-structural) -- NATIVE
# ---------------------------------------------------------------------------


@router.post(
    "/api/projects/{name}/update-timestamp",
    operation_id="update_keyframe_timestamp",
    dependencies=[Depends(project_dir)],
)
def update_timestamp(
    name: str, request: Request, body: UpdateTimestampBody
) -> dict:
    kf_id = body.keyframeId
    new_timestamp = body.newTimestamp if body.newTimestamp is not None else body.timestamp
    if not kf_id or new_timestamp is None:
        raise ApiError("BAD_REQUEST", "Missing 'keyframeId' or 'timestamp' (alias: 'newTimestamp')", status_code=400)

    pdir = _work_dir(request) / name

    from scenecraft.db import undo_begin as _ub
    _ub(pdir, f"Update timestamp {kf_id} to {new_timestamp}")

    try:
        _log(f"update-timestamp: {kf_id} -> {new_timestamp}")
        from scenecraft.db import update_keyframe, get_transitions, update_transition, get_keyframe

        update_keyframe(pdir, kf_id, timestamp=new_timestamp)

        # Update duration_seconds on adjacent transitions
        new_time = _parse_ts(new_timestamp)
        all_trs = get_transitions(pdir)
        for tr in all_trs:
            if tr["from"] == kf_id or tr["to"] == kf_id:
                other_id = tr["to"] if tr["from"] == kf_id else tr["from"]
                other_kf = get_keyframe(pdir, other_id)
                if other_kf:
                    other_time = _parse_ts(other_kf["timestamp"])
                    dur = round(abs(new_time - other_time), 2)
                    update_transition(pdir, tr["id"], duration_seconds=dur)

        return {"success": True, "keyframeId": kf_id, "newTimestamp": new_timestamp}
    except ApiError:
        raise
    except Exception as e:
        raise ApiError("INTERNAL_ERROR", str(e), status_code=500)


@router.post(
    "/api/projects/{name}/update-prompt",
    operation_id="update_keyframe_prompt",
    dependencies=[Depends(project_dir)],
)
def update_prompt(
    name: str, request: Request, body: UpdatePromptBody
) -> dict:
    kf_id = body.keyframeId
    prompt = body.prompt

    pdir = _work_dir(request) / name

    try:
        from scenecraft.db import update_keyframe, get_keyframe
        _log(f"update-prompt: {kf_id} prompt={repr(prompt[:60])}")
        kf = get_keyframe(pdir, kf_id)
        if not kf:
            _log(f"  NOT FOUND: {kf_id}")
            raise ApiError("NOT_FOUND", f"Keyframe {kf_id} not found", status_code=404)

        update_keyframe(pdir, kf_id, prompt=prompt)
        _log(f"  saved prompt for {kf_id}")
        return {"success": True, "keyframeId": kf_id}
    except ApiError:
        raise
    except Exception as e:
        _log(f"  FAILED: {e}")
        raise ApiError("INTERNAL_ERROR", str(e), status_code=500)


# ---------------------------------------------------------------------------
# Structural routes -- guarded by project_lock -- NATIVE
# ---------------------------------------------------------------------------


@router.post(
    "/api/projects/{name}/add-keyframe",
    operation_id="add_keyframe",
    dependencies=[Depends(project_lock), Depends(project_dir)],
)
def add_keyframe(
    name: str, request: Request, body: AddKeyframeBody
) -> dict:
    timestamp = body.timestamp
    ts_seconds = _parse_ts(timestamp)
    if ts_seconds < 0 or ts_seconds > 7200:
        raise ApiError("BAD_REQUEST", f"Invalid timestamp: {timestamp} ({ts_seconds}s)", status_code=400)

    _log(f"add-keyframe: {name} at {timestamp} ({ts_seconds:.2f}s)")

    section = body.section
    prompt = body.prompt
    track_id = body.trackId

    pdir = _work_dir(request) / name

    from scenecraft.db import undo_begin
    undo_begin(pdir, f"Add keyframe at {timestamp}")

    try:
        from scenecraft.db import (
            add_keyframe as db_add_kf,
            get_keyframes as db_get_kfs,
            next_keyframe_id,
            next_transition_id,
            add_transition as db_add_tr,
            update_transition as db_update_tr,
            get_transitions as db_get_trs,
        )

        new_id = next_keyframe_id(pdir)
        new_time = _parse_ts(timestamp)

        new_kf = {
            "id": new_id, "timestamp": timestamp, "section": section,
            "source": f"selected_keyframes/{new_id}.png", "prompt": prompt,
            "candidates": [], "selected": None, "track_id": track_id,
        }
        db_add_kf(pdir, new_kf)

        # Find timeline neighbors on the same track
        all_kfs = [k for k in db_get_kfs(pdir) if k.get("track_id", "track_1") == track_id]
        sorted_kfs = sorted(all_kfs, key=lambda k: _parse_ts(k["timestamp"]))
        new_idx = next((i for i, k in enumerate(sorted_kfs) if k["id"] == new_id), -1)
        prev_kf = sorted_kfs[new_idx - 1] if new_idx > 0 else None
        next_kf = sorted_kfs[new_idx + 1] if new_idx < len(sorted_kfs) - 1 else None

        # Wire transitions
        prev_time = _parse_ts(prev_kf["timestamp"]) if prev_kf else None
        next_time = _parse_ts(next_kf["timestamp"]) if next_kf else None

        old_tr = None
        if prev_kf and next_kf:
            all_trs = db_get_trs(pdir)
            old_tr = next((t for t in all_trs if t["from"] == prev_kf["id"] and t["to"] == next_kf["id"]), None)

        if old_tr:
            dur_before = round(new_time - prev_time, 2)
            db_update_tr(pdir, old_tr["id"], to=new_id, duration_seconds=dur_before,
                         remap={"method": "linear", "target_duration": dur_before})
            _log(f"  Relinked {old_tr['id']}: {prev_kf['id']} -> {new_id} (was -> {next_kf['id']})")

            dur_after = round(next_time - new_time, 2)
            tr2_id = next_transition_id(pdir)
            db_add_tr(pdir, {
                "id": tr2_id, "from": new_id, "to": next_kf["id"],
                "duration_seconds": dur_after, "slots": 1,
                "action": "", "use_global_prompt": False, "selected": None,
                "remap": {"method": "linear", "target_duration": dur_after},
                "track_id": track_id,
            })
        else:
            if prev_kf:
                dur_before = round(new_time - prev_time, 2)
                tr1_id = next_transition_id(pdir)
                db_add_tr(pdir, {
                    "id": tr1_id, "from": prev_kf["id"], "to": new_id,
                    "duration_seconds": dur_before, "slots": 1,
                    "action": "", "use_global_prompt": False, "selected": None,
                    "remap": {"method": "linear", "target_duration": dur_before},
                    "track_id": track_id,
                })
            if next_kf:
                dur_after = round(next_time - new_time, 2)
                tr2_id = next_transition_id(pdir)
                db_add_tr(pdir, {
                    "id": tr2_id, "from": new_id, "to": next_kf["id"],
                    "duration_seconds": dur_after, "slots": 1,
                    "action": "", "use_global_prompt": False, "selected": None,
                    "remap": {"method": "linear", "target_duration": dur_after},
                    "track_id": track_id,
                })
        _log(f"  Wired: {prev_kf['id'] if prev_kf else '(start)'} -> {new_id} -> {next_kf['id'] if next_kf else '(end)'}")
        _log(f"  Created {new_id} at {timestamp}")
        return {"success": True, "keyframe": {"id": new_id, "timestamp": timestamp, "section": section, "prompt": prompt}}
    except ApiError:
        raise
    except Exception as e:
        _log(f"  FAILED: {e}")
        traceback.print_exc()
        raise ApiError("INTERNAL_ERROR", str(e), status_code=500)


@router.post(
    "/api/projects/{name}/duplicate-keyframe",
    operation_id="duplicate_keyframe",
    dependencies=[Depends(project_lock), Depends(project_dir)],
)
def duplicate_keyframe(
    name: str, request: Request, body: DuplicateKeyframeBody
) -> dict:
    source_id = body.keyframeId
    timestamp = body.timestamp

    pdir = _work_dir(request) / name

    from scenecraft.db import undo_begin
    undo_begin(pdir, f"Duplicate keyframe {source_id}")

    try:
        from scenecraft.db import (
            add_keyframe as db_add_kf,
            get_keyframes as db_get_kfs,
            get_keyframe as db_get_kf,
            next_keyframe_id,
            next_transition_id,
            add_transition as db_add_tr,
            delete_transition as db_del_tr,
            get_transitions as db_get_trs,
        )

        source_kf = db_get_kf(pdir, source_id)
        if not source_kf:
            raise ApiError("NOT_FOUND", f"Keyframe {source_id} not found", status_code=404)

        new_id = next_keyframe_id(pdir)
        new_time = _parse_ts(timestamp)

        # Copy candidate files from disk
        src_candidates_dir = pdir / "keyframe_candidates" / "candidates" / f"section_{source_id}"
        dst_candidates_dir = pdir / "keyframe_candidates" / "candidates" / f"section_{new_id}"
        new_candidates = []
        if src_candidates_dir.exists():
            dst_candidates_dir.mkdir(parents=True, exist_ok=True)
            for f in sorted(src_candidates_dir.iterdir()):
                if f.suffix.lower() in ('.png', '.jpg', '.jpeg'):
                    dest = dst_candidates_dir / f.name
                    shutil.copy2(str(f), str(dest))
                    new_candidates.append(f"keyframe_candidates/candidates/section_{new_id}/{f.name}")

        # If no files on disk, use DB candidates (rewrite paths to new id)
        if not new_candidates and source_kf.get("candidates"):
            src_prefix = f"section_{source_id}/"
            dst_prefix = f"section_{new_id}/"
            for cand_path in source_kf["candidates"]:
                src_file = pdir / cand_path
                if src_file.exists():
                    dst_path = cand_path.replace(src_prefix, dst_prefix)
                    dst_file = pdir / dst_path
                    dst_file.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(str(src_file), str(dst_file))
                    new_candidates.append(dst_path)

        # Copy selected keyframe image
        src_selected = pdir / "selected_keyframes" / f"{source_id}.png"
        dst_selected = pdir / "selected_keyframes" / f"{new_id}.png"
        if src_selected.exists():
            shutil.copy2(str(src_selected), str(dst_selected))

        track_id = source_kf.get("track_id", "track_1")
        new_kf = {
            "id": new_id, "timestamp": timestamp,
            "section": source_kf.get("section", ""),
            "source": f"selected_keyframes/{new_id}.png",
            "prompt": source_kf.get("prompt", ""),
            "candidates": new_candidates,
            "selected": source_kf.get("selected"),
            "track_id": track_id,
        }
        db_add_kf(pdir, new_kf)

        # Wire up transitions
        all_kfs = [k for k in db_get_kfs(pdir)
                   if k.get("track_id", "track_1") == track_id and not k.get("deleted_at")]
        sorted_kfs = sorted(all_kfs, key=lambda k: _parse_ts(k["timestamp"]))
        new_idx = next((i for i, k in enumerate(sorted_kfs) if k["id"] == new_id), -1)
        prev_kf = sorted_kfs[new_idx - 1] if new_idx > 0 else None
        next_kf = sorted_kfs[new_idx + 1] if new_idx < len(sorted_kfs) - 1 else None

        all_trs = [t for t in db_get_trs(pdir)
                   if t.get("track_id", "track_1") == track_id and not t.get("deleted_at")]

        # Find and remove ALL transitions that span across the new keyframe's position
        kf_time_map = {k["id"]: _parse_ts(k["timestamp"]) for k in sorted_kfs}
        spanning_trs = []
        for t in all_trs:
            from_time = kf_time_map.get(t["from"])
            to_time = kf_time_map.get(t["to"])
            if from_time is not None and to_time is not None:
                if from_time < new_time < to_time:
                    spanning_trs.append(t)

        old_tr = spanning_trs[0] if spanning_trs else None
        now_iso = datetime.now(timezone.utc).isoformat()
        for t in spanning_trs:
            db_del_tr(pdir, t["id"], now_iso)

        # Check for existing transitions to avoid duplicates
        existing_from_prev = any(t["from"] == prev_kf["id"] and t["to"] == new_id for t in all_trs) if prev_kf else False
        existing_to_next = any(t["from"] == new_id and t["to"] == next_kf["id"] for t in all_trs) if next_kf else False

        prev_time = _parse_ts(prev_kf["timestamp"]) if prev_kf else None
        next_time = _parse_ts(next_kf["timestamp"]) if next_kf else None

        # Build base properties from old spanning transition
        tr_props: dict[str, Any] = {}
        if old_tr:
            for prop in ("action", "use_global_prompt", "blend_mode", "opacity",
                         "opacity_curve", "red_curve", "green_curve", "blue_curve",
                         "black_curve", "saturation_curve", "hue_shift_curve",
                         "invert_curve", "chroma_key", "is_adjustment",
                         "mask_center_x", "mask_center_y", "mask_radius", "mask_feather",
                         "transform_x", "transform_y", "hidden",
                         "label", "label_color", "tags"):
                if old_tr.get(prop) is not None:
                    tr_props[prop] = old_tr[prop]

        if prev_kf and not existing_from_prev:
            dur_before = round(new_time - prev_time, 2)
            if dur_before > 0.05:
                tr1_id = next_transition_id(pdir)
                tr1_data = {
                    "id": tr1_id, "from": prev_kf["id"], "to": new_id,
                    "duration_seconds": dur_before, "slots": 1,
                    "selected": None,
                    "remap": {"method": "linear", "target_duration": dur_before},
                    "track_id": track_id,
                    **tr_props,
                }
                db_add_tr(pdir, tr1_data)

                if old_tr:
                    from scenecraft.db import clone_tr_candidates as _clone_tc, update_transition
                    _clone_tc(pdir, source_transition_id=old_tr["id"],
                              target_transition_id=tr1_id, new_source="cross-tr-copy")

                    old_sel = pdir / "selected_transitions" / f"{old_tr['id']}_slot_0.mp4"
                    if old_sel.exists():
                        new_sel = pdir / "selected_transitions" / f"{tr1_id}_slot_0.mp4"
                        new_sel.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(str(old_sel), str(new_sel))
                        update_transition(pdir, tr1_id, selected=old_tr.get("selected"))

                    from scenecraft.db import get_transition_effects, add_transition_effect
                    for fx in get_transition_effects(pdir, old_tr["id"]):
                        add_transition_effect(pdir, tr1_id, fx["type"], fx.get("params"))

        if next_kf and not existing_to_next:
            dur_after = round(next_time - new_time, 2)
            if dur_after > 0.05:
                tr2_id = next_transition_id(pdir)
                tr2_data = {
                    "id": tr2_id, "from": new_id, "to": next_kf["id"],
                    "duration_seconds": dur_after, "slots": 1,
                    "selected": None,
                    "remap": {"method": "linear", "target_duration": dur_after},
                    "track_id": track_id,
                    **tr_props,
                }
                db_add_tr(pdir, tr2_data)

                if old_tr:
                    from scenecraft.db import clone_tr_candidates as _clone_tc, update_transition
                    _clone_tc(pdir, source_transition_id=old_tr["id"],
                              target_transition_id=tr2_id, new_source="cross-tr-copy")

                    old_sel = pdir / "selected_transitions" / f"{old_tr['id']}_slot_0.mp4"
                    if old_sel.exists():
                        new_sel = pdir / "selected_transitions" / f"{tr2_id}_slot_0.mp4"
                        shutil.copy2(str(old_sel), str(new_sel))
                        update_transition(pdir, tr2_id, selected=old_tr.get("selected"))

                    from scenecraft.db import get_transition_effects, add_transition_effect
                    for fx in get_transition_effects(pdir, old_tr["id"]):
                        add_transition_effect(pdir, tr2_id, fx["type"], fx.get("params"))

        _log(f"  Duplicated {source_id} -> {new_id} at {timestamp} ({len(new_candidates)} candidates copied)")
        return {"success": True, "keyframe": {"id": new_id, "timestamp": timestamp}}
    except ApiError:
        raise
    except Exception as e:
        _log(f"  FAILED: {e}")
        traceback.print_exc()
        raise ApiError("INTERNAL_ERROR", str(e), status_code=500)


@router.post(
    "/api/projects/{name}/paste-group",
    operation_id="paste_group",
    dependencies=[Depends(project_lock), Depends(project_dir)],
)
def paste_group(
    name: str, request: Request, body: PasteGroupBody
) -> dict:
    kf_ids = body.keyframeIds
    audio_clip_ids = body.audioClipIds or []
    target_time_str = body.targetTime
    target_track = body.targetTrackId

    if not kf_ids and not audio_clip_ids:
        raise ApiError("BAD_REQUEST", "Missing content ('keyframeIds' or 'audioClipIds') or 'targetTime'", status_code=400)

    pdir = _work_dir(request) / name

    from scenecraft.db import undo_begin
    undo_begin(pdir, f"Paste {len(kf_ids)} keyframes + {len(audio_clip_ids)} audio clips")

    try:
        from scenecraft.db import (
            get_keyframe as db_get_kf, add_keyframe as db_add_kf,
            get_transitions as db_get_trs, add_transition as db_add_tr,
            next_keyframe_id, next_transition_id, update_transition,
            get_audio_clips as db_get_audio_clips,
            get_keyframes as db_get_kfs_paste,
            clone_tr_candidates as _clone_tc,
            get_transition_effects, add_transition_effect,
            add_audio_clip as db_add_audio_clip, generate_id,
        )

        target_time = _parse_ts(target_time_str)

        # 1. Read source keyframes + audio clips, compute anchor
        src_kfs = []
        for kid in kf_ids:
            kf = db_get_kf(pdir, kid)
            if kf and not kf.get("deleted_at"):
                src_kfs.append(kf)

        src_audio_clips = []
        if audio_clip_ids:
            all_clips = {c["id"]: c for c in db_get_audio_clips(pdir)}
            for cid in audio_clip_ids:
                c = all_clips.get(cid)
                if c:
                    src_audio_clips.append(c)

        if not src_kfs and not src_audio_clips:
            raise ApiError("NOT_FOUND", "No valid keyframes or audio clips found", status_code=404)

        src_kfs.sort(key=lambda k: _parse_ts(k["timestamp"]))
        candidate_mins = []
        if src_kfs:
            candidate_mins.append(_parse_ts(src_kfs[0]["timestamp"]))
        if src_audio_clips:
            candidate_mins.append(min(float(c["start_time"]) for c in src_audio_clips))
        min_time = min(candidate_mins)

        # 2. Create new keyframes with offset times
        id_map: dict[str, str] = {}
        created_kfs = []
        for src in src_kfs:
            offset = _parse_ts(src["timestamp"]) - min_time
            new_time = target_time + offset
            new_ts = _secs_to_ts(new_time)
            new_id = next_keyframe_id(pdir)
            id_map[src["id"]] = new_id

            # Copy candidate files
            src_cand_dir = pdir / "keyframe_candidates" / "candidates" / f"section_{src['id']}"
            dst_cand_dir = pdir / "keyframe_candidates" / "candidates" / f"section_{new_id}"
            new_candidates = []
            if src_cand_dir.exists():
                dst_cand_dir.mkdir(parents=True, exist_ok=True)
                for f in sorted(src_cand_dir.iterdir()):
                    if f.suffix.lower() in ('.png', '.jpg', '.jpeg'):
                        shutil.copy2(str(f), str(dst_cand_dir / f.name))
                        new_candidates.append(f"keyframe_candidates/candidates/section_{new_id}/{f.name}")

            # Copy selected keyframe image
            src_sel = pdir / "selected_keyframes" / f"{src['id']}.png"
            if src_sel.exists():
                dst_sel = pdir / "selected_keyframes" / f"{new_id}.png"
                dst_sel.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(str(src_sel), str(dst_sel))

            db_add_kf(pdir, {
                "id": new_id, "timestamp": new_ts,
                "section": src.get("section", ""),
                "source": f"selected_keyframes/{new_id}.png",
                "prompt": src.get("prompt", ""),
                "candidates": new_candidates,
                "selected": src.get("selected"),
                "track_id": target_track,
                "label": src.get("label", ""),
                "label_color": src.get("label_color", ""),
                "blend_mode": src.get("blend_mode", ""),
                "opacity": src.get("opacity"),
            })
            created_kfs.append({"id": new_id, "timestamp": new_ts})

        # 3. Find transitions between source kfs and duplicate them
        src_kf_set = set(kf_ids)
        all_trs = db_get_trs(pdir)
        internal_trs = [t for t in all_trs
                        if t["from"] in src_kf_set and t["to"] in src_kf_set
                        and not t.get("deleted_at")]

        # Build existing time ranges on target track for overlap check
        all_kfs_paste = {k["id"]: k for k in db_get_kfs_paste(pdir) if not k.get("deleted_at")}
        target_trs = [t for t in all_trs
                      if t.get("track_id") == target_track and not t.get("deleted_at")]
        existing_ranges = []
        for t in target_trs:
            fk = all_kfs_paste.get(t["from"])
            tk = all_kfs_paste.get(t["to"])
            if fk and tk:
                existing_ranges.append((_parse_ts(fk["timestamp"]), _parse_ts(tk["timestamp"])))

        created_trs = []
        for src_tr in internal_trs:
            new_from = id_map.get(src_tr["from"])
            new_to = id_map.get(src_tr["to"])
            if not new_from or not new_to:
                continue

            from_ts = _parse_ts(next((k["timestamp"] for k in created_kfs if k["id"] == new_from), "0"))
            to_ts = _parse_ts(next((k["timestamp"] for k in created_kfs if k["id"] == new_to), "0"))
            if to_ts - from_ts <= 0.05:
                continue

            overlaps = any(ef < to_ts and et > from_ts for ef, et in existing_ranges)
            if overlaps:
                continue

            new_tr_id = next_transition_id(pdir)

            _clone_tc(pdir, source_transition_id=src_tr["id"],
                      target_transition_id=new_tr_id, new_source="cross-tr-copy")

            src_sel = pdir / "selected_transitions" / f"{src_tr['id']}_slot_0.mp4"
            if src_sel.exists():
                dst_sel = pdir / "selected_transitions" / f"{new_tr_id}_slot_0.mp4"
                dst_sel.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(str(src_sel), str(dst_sel))

            db_add_tr(pdir, {
                "id": new_tr_id, "from": new_from, "to": new_to,
                "duration_seconds": src_tr.get("duration_seconds", 0),
                "slots": src_tr.get("slots", 1),
                "action": src_tr.get("action", ""),
                "use_global_prompt": src_tr.get("use_global_prompt", False),
                "selected": src_tr.get("selected"),
                "remap": src_tr.get("remap", {"method": "linear", "target_duration": 0}),
                "track_id": target_track,
                "blend_mode": src_tr.get("blend_mode", ""),
                "opacity": src_tr.get("opacity"),
                "opacity_curve": src_tr.get("opacity_curve"),
                "red_curve": src_tr.get("red_curve"),
                "green_curve": src_tr.get("green_curve"),
                "blue_curve": src_tr.get("blue_curve"),
                "black_curve": src_tr.get("black_curve"),
                "hue_shift_curve": src_tr.get("hue_shift_curve"),
                "saturation_curve": src_tr.get("saturation_curve"),
                "invert_curve": src_tr.get("invert_curve"),
                "brightness_curve": src_tr.get("brightness_curve"),
                "contrast_curve": src_tr.get("contrast_curve"),
                "exposure_curve": src_tr.get("exposure_curve"),
                "chroma_key": src_tr.get("chroma_key"),
                "is_adjustment": src_tr.get("is_adjustment", False),
                "hidden": src_tr.get("hidden", False),
                "mask_center_x": src_tr.get("mask_center_x"),
                "mask_center_y": src_tr.get("mask_center_y"),
                "mask_radius": src_tr.get("mask_radius"),
                "mask_feather": src_tr.get("mask_feather"),
                "transform_x": src_tr.get("transform_x"),
                "transform_y": src_tr.get("transform_y"),
                "transform_x_curve": src_tr.get("transform_x_curve"),
                "transform_y_curve": src_tr.get("transform_y_curve"),
                "transform_z_curve": src_tr.get("transform_z_curve"),
                "anchor_x": src_tr.get("anchor_x"),
                "anchor_y": src_tr.get("anchor_y"),
                "label": src_tr.get("label", ""),
                "label_color": src_tr.get("label_color", ""),
                "tags": src_tr.get("tags", []),
            })

            for fx in get_transition_effects(pdir, src_tr["id"]):
                add_transition_effect(pdir, new_tr_id, fx["type"], fx.get("params"))

            created_trs.append({"id": new_tr_id, "from": new_from, "to": new_to})

        # 4. Clone audio clips
        created_audio_clips = []
        for src_clip in src_audio_clips:
            src_start = float(src_clip["start_time"])
            src_end = float(src_clip["end_time"])
            offset = src_start - min_time
            new_start = target_time + offset
            new_end = new_start + (src_end - src_start)
            new_clip_id = generate_id("audio_clip")
            db_add_audio_clip(pdir, {
                "id": new_clip_id,
                "track_id": src_clip["track_id"],
                "source_path": src_clip["source_path"],
                "start_time": new_start,
                "end_time": new_end,
                "source_offset": src_clip.get("source_offset", 0.0),
                "volume_curve": src_clip.get("volume_curve"),
                "muted": src_clip.get("muted", False),
                "remap": src_clip.get("remap"),
            })
            created_audio_clips.append({"id": new_clip_id, "track_id": src_clip["track_id"], "start_time": new_start, "end_time": new_end})

        _log(f"paste-group: {len(created_kfs)} kfs, {len(created_trs)} trs, {len(created_audio_clips)} clips pasted at {target_time_str} on {target_track}")
        return {
            "success": True,
            "keyframes": created_kfs,
            "transitions": created_trs,
            "audioClips": created_audio_clips,
        }
    except ApiError:
        raise
    except Exception as e:
        _log(f"paste-group FAILED: {e}")
        traceback.print_exc()
        raise ApiError("INTERNAL_ERROR", str(e), status_code=500)


@router.post(
    "/api/projects/{name}/delete-keyframe",
    operation_id="delete_keyframe",
    dependencies=[Depends(project_lock), Depends(project_dir)],
)
def delete_keyframe(
    name: str, request: Request, body: DeleteKeyframeBody
) -> dict:
    kf_id = body.keyframeId

    pdir = _work_dir(request) / name

    from scenecraft.db import undo_begin
    undo_begin(pdir, f"Delete keyframe {kf_id}")

    try:
        from scenecraft.db import (
            get_keyframe, delete_keyframe as db_del_kf,
            get_transitions_involving, delete_transition as db_del_tr,
            get_transition,
        )

        kf = get_keyframe(pdir, kf_id)
        if not kf:
            raise ApiError("NOT_FOUND", f"Keyframe {kf_id} not found", status_code=404)

        now = datetime.now(timezone.utc).isoformat()
        _log(f"[delete-kf] {kf_id}")

        # Soft-delete orphaned transitions, find one with video to inherit
        orphaned = get_transitions_involving(pdir, kf_id)
        inherited_tr_id = None
        for tr in orphaned:
            sel = tr.get("selected")
            if sel is not None and sel != [None]:
                inherited_tr_id = tr["id"]
                break

        for tr in orphaned:
            db_del_tr(pdir, tr["id"], now)

        # Soft-delete the keyframe
        db_del_kf(pdir, kf_id, now)

        # Bridge neighbors SYNCHRONOUSLY before responding
        try:
            from scenecraft.db import (
                get_keyframes as db_get_kfs, get_transitions as db_get_trs,
                next_transition_id, add_transition as db_add_tr,
                clone_tr_candidates as _clone_tc,
                get_transition_effects, add_transition_effect,
            )

            removed_time = _parse_ts(kf["timestamp"])
            kf_track = kf.get("track_id", "track_1")

            all_kfs = [k for k in db_get_kfs(pdir)
                       if k.get("track_id", "track_1") == kf_track and not k.get("deleted_at")]
            sorted_kfs = sorted(all_kfs, key=lambda k: _parse_ts(k["timestamp"]))
            prev_kf = None
            next_kf = None
            for k in sorted_kfs:
                t = _parse_ts(k["timestamp"])
                if t < removed_time:
                    prev_kf = k
                elif t > removed_time and next_kf is None:
                    next_kf = k

            if prev_kf and next_kf:
                active_trs = [t for t in db_get_trs(pdir)
                              if t.get("track_id") == kf_track and not t.get("deleted_at")]
                existing_bridge = next((t for t in active_trs
                                        if t["from"] == prev_kf["id"] and t["to"] == next_kf["id"]), None)

                if not existing_bridge:
                    new_tr_id = next_transition_id(pdir)
                    pt = _parse_ts(prev_kf["timestamp"])
                    nt = _parse_ts(next_kf["timestamp"])
                    dur = round(nt - pt, 2)

                    tr_props: dict[str, Any] = {}
                    selected = None
                    if inherited_tr_id:
                        inh_tr = next((t for t in orphaned if t["id"] == inherited_tr_id), None)
                        if inh_tr:
                            for prop in ("action", "use_global_prompt", "blend_mode", "opacity",
                                         "opacity_curve", "red_curve", "green_curve", "blue_curve",
                                         "black_curve", "saturation_curve", "hue_shift_curve",
                                         "invert_curve", "chroma_key", "is_adjustment",
                                         "label", "label_color", "tags", "hidden"):
                                if inh_tr.get(prop) is not None:
                                    tr_props[prop] = inh_tr[prop]

                            old_sel = pdir / "selected_transitions" / f"{inherited_tr_id}_slot_0.mp4"
                            if old_sel.exists():
                                new_sel = pdir / "selected_transitions" / f"{new_tr_id}_slot_0.mp4"
                                try:
                                    os.link(str(old_sel), str(new_sel))
                                except OSError:
                                    shutil.copy2(str(old_sel), str(new_sel))
                                inh_tr_row = get_transition(pdir, inherited_tr_id)
                                selected = inh_tr_row.get("selected") if inh_tr_row else None
                                _log(f"[delete-kf] {kf_id}: inherited video from {inherited_tr_id}")

                            _clone_tc(pdir, source_transition_id=inherited_tr_id,
                                      target_transition_id=new_tr_id, new_source="cross-tr-copy")

                            for fx in get_transition_effects(pdir, inherited_tr_id):
                                add_transition_effect(pdir, new_tr_id, fx["type"], fx.get("params"))

                    if dur > 0.05:
                        db_add_tr(pdir, {
                            "id": new_tr_id, "from": prev_kf["id"], "to": next_kf["id"],
                            "duration_seconds": dur, "slots": 1,
                            "selected": selected,
                            "remap": {"method": "linear", "target_duration": dur},
                            "track_id": kf_track,
                            **tr_props,
                        })
                        _log(f"[delete-kf] {kf_id}: bridged {prev_kf['id']} -> {next_kf['id']} as {new_tr_id}")
                    else:
                        _log(f"[delete-kf] {kf_id}: skip zero-length bridge ({dur}s)")
                else:
                    _log(f"[delete-kf] {kf_id}: bridge already exists as {existing_bridge['id']}")
        except Exception as e:
            _log(f"[delete-kf] {kf_id}: bridge ERROR {e}")

        return {"success": True, "binned": {"id": kf_id, "deleted_at": now}}

    except ApiError:
        raise
    except Exception as e:
        _log(f"[delete-kf] {kf_id}: ERROR {e}")
        traceback.print_exc()
        raise ApiError("INTERNAL_ERROR", str(e), status_code=500)


@router.post(
    "/api/projects/{name}/batch-delete-keyframes",
    operation_id="batch_delete_keyframes",
    dependencies=[Depends(project_lock), Depends(project_dir)],
)
def batch_delete_keyframes(
    name: str, request: Request, body: BatchDeleteKeyframesBody
) -> dict:
    kf_ids = body.keyframeIds or body.keyframe_ids or []
    if not kf_ids:
        raise ApiError("BAD_REQUEST", "Missing 'keyframeIds'", status_code=400)

    pdir = _work_dir(request) / name

    try:
        from scenecraft.db import (
            get_keyframe, delete_keyframe as db_del_kf, get_keyframes as db_get_kfs,
            get_transitions_involving, delete_transition as db_del_tr,
            next_transition_id, add_transition as db_add_tr, get_transitions as db_get_trs,
            clone_tr_candidates as _clone_tc,
            get_transition_effects, add_transition_effect,
        )

        now = datetime.now(timezone.utc).isoformat()
        deleted = []

        # Collect orphaned transitions with videos BEFORE deleting
        inherited_videos: dict[str, dict] = {}
        for kf_id in kf_ids:
            kf = get_keyframe(pdir, kf_id)
            if not kf:
                continue
            track = kf.get("track_id", "track_1")
            for tr in get_transitions_involving(pdir, kf_id):
                sel = tr.get("selected")
                if sel is not None and sel != [None] and track not in inherited_videos:
                    inherited_videos[track] = tr
                db_del_tr(pdir, tr["id"], now)
            db_del_kf(pdir, kf_id, now)
            deleted.append(kf_id)

        # Bridge gaps PER TRACK
        tracks_affected: set[str] = set()
        for kf_id in kf_ids:
            kf = get_keyframe(pdir, kf_id)
            if kf:
                tracks_affected.add(kf.get("track_id", "track_1"))

        for track in tracks_affected:
            track_kfs = [k for k in db_get_kfs(pdir)
                         if k.get("track_id", "track_1") == track and not k.get("deleted_at")]
            sorted_kfs = sorted(track_kfs, key=lambda k: _parse_ts(k["timestamp"]))

            active_trs = [t for t in db_get_trs(pdir)
                          if t.get("track_id") == track and not t.get("deleted_at")]
            existing_pairs = set((t["from"], t["to"]) for t in active_trs)

            inh_tr = inherited_videos.get(track)

            for i in range(len(sorted_kfs) - 1):
                a = sorted_kfs[i]
                b = sorted_kfs[i + 1]
                if (a["id"], b["id"]) not in existing_pairs:
                    dur = round(_parse_ts(b["timestamp"]) - _parse_ts(a["timestamp"]), 2)
                    if dur <= 0.05:
                        continue

                    tr_id = next_transition_id(pdir)
                    tr_props: dict[str, Any] = {}
                    selected = None

                    if inh_tr:
                        for prop in ("action", "use_global_prompt", "blend_mode", "opacity",
                                     "opacity_curve", "red_curve", "green_curve", "blue_curve",
                                     "black_curve", "saturation_curve", "hue_shift_curve",
                                     "invert_curve", "label", "label_color", "tags", "hidden"):
                            if inh_tr.get(prop) is not None:
                                tr_props[prop] = inh_tr[prop]

                        old_sel = pdir / "selected_transitions" / f"{inh_tr['id']}_slot_0.mp4"
                        if old_sel.exists():
                            new_sel = pdir / "selected_transitions" / f"{tr_id}_slot_0.mp4"
                            try:
                                os.link(str(old_sel), str(new_sel))
                            except OSError:
                                shutil.copy2(str(old_sel), str(new_sel))
                            selected = inh_tr.get("selected")

                        _clone_tc(pdir, source_transition_id=inh_tr["id"],
                                  target_transition_id=tr_id, new_source="cross-tr-copy")

                        for fx in get_transition_effects(pdir, inh_tr["id"]):
                            add_transition_effect(pdir, tr_id, fx["type"], fx.get("params"))

                    db_add_tr(pdir, {
                        "id": tr_id, "from": a["id"], "to": b["id"],
                        "duration_seconds": dur, "slots": 1,
                        "selected": selected,
                        "remap": {"method": "linear", "target_duration": dur},
                        "track_id": track,
                        **tr_props,
                    })

        _log(f"[batch-delete-kf] {name}: deleted {len(deleted)} keyframes")
        return {"success": True, "deleted": deleted}
    except ApiError:
        raise
    except Exception as e:
        _log(f"[batch-delete-kf] ERROR: {e}")
        traceback.print_exc()
        raise ApiError("INTERNAL_ERROR", str(e), status_code=500)


@router.post(
    "/api/projects/{name}/restore-keyframe",
    operation_id="restore_keyframe",
    dependencies=[Depends(project_lock), Depends(project_dir)],
)
def restore_keyframe(
    name: str, request: Request, body: RestoreKeyframeBody
) -> dict:
    kf_id = body.keyframeId
    pdir = _work_dir(request) / name

    try:
        from scenecraft.db import restore_keyframe as db_restore_kf
        _log(f"restore-keyframe: {kf_id}")
        db_restore_kf(pdir, kf_id)
        return {"success": True, "keyframe": {"id": kf_id}}
    except Exception as e:
        raise ApiError("INTERNAL_ERROR", str(e), status_code=500)


@router.post(
    "/api/projects/{name}/insert-pool-item",
    operation_id="insert_pool_item",
    dependencies=[Depends(project_lock), Depends(project_dir)],
)
def insert_pool_item(
    name: str, request: Request, body: InsertPoolItemBody
) -> dict:
    item_type = body.type
    pool_path = body.poolPath
    at_time = body.atTime
    track_id = body.trackId

    if not item_type or not pool_path:
        raise ApiError("BAD_REQUEST", "Missing 'type' or 'poolPath'", status_code=400)

    pdir = _work_dir(request) / name

    try:
        from scenecraft.db import (
            add_keyframe as db_add_kf, get_keyframes as db_get_kfs,
            next_keyframe_id, next_transition_id,
            add_transition as db_add_tr, delete_transition as db_del_tr,
            get_transitions as db_get_trs,
        )

        source = pdir / pool_path
        if not source.exists():
            raise ApiError("NOT_FOUND", f"Pool item not found: {pool_path}", status_code=404)

        _log(f"insert-pool-item: type={item_type} path={pool_path} atTime={at_time}")

        if item_type == "keyframe":
            kf_id = next_keyframe_id(pdir)
            dest = pdir / "selected_keyframes" / f"{kf_id}.png"
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(str(source), str(dest))
            cand_dir = pdir / "keyframe_candidates" / "candidates" / f"section_{kf_id}"
            cand_dir.mkdir(parents=True, exist_ok=True)
            shutil.copy2(str(source), str(cand_dir / "v1.png"))

            db_add_kf(pdir, {
                "id": kf_id, "timestamp": _secs_to_ts(at_time), "section": "",
                "source": pool_path, "prompt": f"Inserted from pool: {source.name}",
                "candidates": [f"keyframe_candidates/candidates/section_{kf_id}/v1.png"],
                "selected": 1,
            })

            # Find neighbors on the same track and split spanning transition
            all_kfs = [k for k in db_get_kfs(pdir)
                       if k.get("track_id", "track_1") == track_id and not k.get("deleted_at")]
            sorted_kfs = sorted(all_kfs, key=lambda k: _parse_ts(k["timestamp"]))
            new_idx = next((i for i, k in enumerate(sorted_kfs) if k["id"] == kf_id), -1)
            prev_kf = sorted_kfs[new_idx - 1] if new_idx > 0 else None
            next_kf = sorted_kfs[new_idx + 1] if new_idx < len(sorted_kfs) - 1 else None

            if prev_kf and next_kf:
                all_trs = [t for t in db_get_trs(pdir)
                           if t.get("track_id", "track_1") == track_id and not t.get("deleted_at")]
                old_tr = next((t for t in all_trs if t["from"] == prev_kf["id"] and t["to"] == next_kf["id"]), None)

                existing_from_prev = any(t["from"] == prev_kf["id"] and t["to"] == kf_id for t in all_trs)
                existing_to_next = any(t["from"] == kf_id and t["to"] == next_kf["id"] for t in all_trs)

                if old_tr:
                    now_iso = datetime.now(timezone.utc).isoformat()
                    db_del_tr(pdir, old_tr["id"], now_iso)

                pt = _parse_ts(prev_kf["timestamp"])
                nt = _parse_ts(next_kf["timestamp"])
                d1, d2 = round(at_time - pt, 2), round(nt - at_time, 2)

                if not existing_from_prev and d1 > 0.05:
                    tr1_id = next_transition_id(pdir)
                    db_add_tr(pdir, {"id": tr1_id, "from": prev_kf["id"], "to": kf_id,
                        "duration_seconds": d1, "slots": 1, "action": "", "use_global_prompt": False,
                        "selected": None, "remap": {"method": "linear", "target_duration": d1},
                        "track_id": track_id})
                if not existing_to_next and d2 > 0.05:
                    tr2_id = next_transition_id(pdir)
                    db_add_tr(pdir, {"id": tr2_id, "from": kf_id, "to": next_kf["id"],
                        "duration_seconds": d2, "slots": 1, "action": "", "use_global_prompt": False,
                        "selected": None, "remap": {"method": "linear", "target_duration": d2},
                        "track_id": track_id})

            return {"success": True, "type": "keyframe", "id": kf_id}
        else:
            raise ApiError("BAD_REQUEST", "Use 'Assign to TR' for video segments", status_code=400)

    except ApiError:
        raise
    except Exception as e:
        traceback.print_exc()
        raise ApiError("INTERNAL_ERROR", str(e), status_code=500)


# ---------------------------------------------------------------------------
# Base image / unlink (non-structural) -- NATIVE
# ---------------------------------------------------------------------------


@router.post(
    "/api/projects/{name}/batch-set-base-image",
    operation_id="batch_set_base_image",
    dependencies=[Depends(project_dir)],
)
def batch_set_base_image(
    name: str, request: Request, body: BatchSetBaseImageBody
) -> dict:
    items = body.items
    if not items:
        raise ApiError("BAD_REQUEST", "Missing 'items' array of {keyframeId, stillName}", status_code=400)

    pdir = _work_dir(request) / name

    try:
        from scenecraft.db import update_keyframe

        dest_dir = pdir / "selected_keyframes"
        dest_dir.mkdir(parents=True, exist_ok=True)

        results = []
        for item in items:
            kf_id = item.get("keyframeId")
            still_name = item.get("stillName")
            if not kf_id or not still_name:
                continue

            source = pdir / "assets" / "stills" / still_name
            if not source.exists():
                source = pdir / "pool" / "keyframes" / still_name
            if not source.exists():
                results.append({"keyframeId": kf_id, "error": f"Still not found: {still_name}"})
                continue

            shutil.copy2(str(source), str(dest_dir / f"{kf_id}.png"))

            cand_dir = pdir / "keyframe_candidates" / "candidates" / f"section_{kf_id}"
            cand_dir.mkdir(parents=True, exist_ok=True)
            existing = _next_variant(cand_dir, ".png") - 1
            v = existing + 1
            shutil.copy2(str(source), str(cand_dir / f"v{v}.png"))
            all_cands = sorted([
                f"keyframe_candidates/candidates/section_{kf_id}/{f.name}"
                for f in cand_dir.glob("v*.png")
            ], key=lambda p: int(p.rsplit("v", 1)[-1].split(".")[0]))

            update_keyframe(pdir, kf_id, source=still_name, selected=v, candidates=all_cands)
            results.append({"keyframeId": kf_id, "success": True})

        _log(f"batch-set-base-image: {len(results)} keyframes updated")
        return {"success": True, "results": results}
    except ApiError:
        raise
    except Exception as e:
        raise ApiError("INTERNAL_ERROR", str(e), status_code=500)


@router.post(
    "/api/projects/{name}/set-base-image",
    operation_id="set_base_image",
    dependencies=[Depends(project_dir)],
)
def set_base_image(
    name: str, request: Request, body: SetBaseImageBody
) -> dict:
    kf_id = body.keyframeId
    still_name = body.stillName

    pdir = _work_dir(request) / name

    try:
        _log(f"set-base-image: {kf_id} from {still_name}")
        source = pdir / "assets" / "stills" / still_name
        if not source.exists():
            source = pdir / "pool" / "keyframes" / still_name
        if not source.exists():
            raise ApiError("NOT_FOUND", f"Still not found: {still_name}", status_code=404)

        dest_dir = pdir / "selected_keyframes"
        dest_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(str(source), str(dest_dir / f"{kf_id}.png"))

        # Add to candidates
        cand_dir = pdir / "keyframe_candidates" / "candidates" / f"section_{kf_id}"
        cand_dir.mkdir(parents=True, exist_ok=True)
        existing = _next_variant(cand_dir, ".png") - 1
        v = existing + 1
        shutil.copy2(str(source), str(cand_dir / f"v{v}.png"))
        all_cands = sorted([
            f"keyframe_candidates/candidates/section_{kf_id}/{f.name}"
            for f in cand_dir.glob("v*.png")
        ], key=lambda p: int(p.rsplit("v", 1)[-1].split(".")[0]))

        from scenecraft.db import update_keyframe
        update_keyframe(pdir, kf_id, source=f"assets/stills/{still_name}", selected=v, candidates=all_cands)

        return {"success": True, "keyframeId": kf_id, "still": still_name}
    except ApiError:
        raise
    except Exception as e:
        raise ApiError("INTERNAL_ERROR", str(e), status_code=500)


@router.post(
    "/api/projects/{name}/unlink-keyframe",
    operation_id="unlink_keyframe",
    dependencies=[Depends(project_dir)],
)
def unlink_keyframe(
    name: str, request: Request, body: UnlinkKeyframeBody
) -> dict:
    kf_id = body.keyframeId
    side = body.side

    pdir = _work_dir(request) / name

    from scenecraft.db import undo_begin
    undo_begin(pdir, f"Unlink keyframe {kf_id}")

    try:
        from scenecraft.db import get_transitions_involving, delete_transition as db_del_tr
        now = datetime.now(timezone.utc).isoformat()

        orphaned = get_transitions_involving(pdir, kf_id)
        deleted = []
        for tr in orphaned:
            if side == "left" and tr["to"] != kf_id:
                continue
            if side == "right" and tr["from"] != kf_id:
                continue
            db_del_tr(pdir, tr["id"], now)
            deleted.append(tr["id"])

        _log(f"unlink-keyframe: {kf_id} side={side} deleted={deleted}")
        return {"success": True, "deleted": deleted}
    except ApiError:
        raise
    except Exception as e:
        raise ApiError("INTERNAL_ERROR", str(e), status_code=500)


# ---------------------------------------------------------------------------
# Escalate / label / style / assign -- NATIVE (from KF-inline agent)
# ---------------------------------------------------------------------------


@router.post(
    "/api/projects/{name}/escalate-keyframe",
    operation_id="escalate_keyframe",
    dependencies=[Depends(project_dir)],
)
def escalate_keyframe(
    name: str,
    body: EscalateKeyframeBody,
    pdir: Path = Depends(project_dir),
) -> dict:
    """Escalate (intensify) a keyframe image via LLM-generated prompts.

    Spawns a background job that:
      1. Sends the source image + escalation prompt to Claude
      2. Generates ``count`` progressively more intense image variants
      3. Updates the DB candidates list after each image
    Returns immediately with the job ID.
    """
    kf_id = body.keyframeId
    count = body.count

    source_img = pdir / "selected_keyframes" / f"{kf_id}.png"
    if not source_img.exists():
        raise ApiError("BAD_REQUEST", f"No source image found for {kf_id}", status_code=400)

    from scenecraft.db import get_keyframe

    kf = get_keyframe(pdir, kf_id)
    kf_prompt = kf.get("prompt", "") if kf else ""

    from scenecraft.ws_server import job_manager

    job_id = job_manager.create_job(
        "escalate_keyframe",
        total=count,
        meta={"keyframeId": kf_id, "project": name},
    )

    def _run_escalate():
        try:
            import base64 as _b64
            import json as _json
            import re as _re_esc
            import traceback as _tb

            from anthropic import Anthropic

            client_llm = Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))

            _log(f"[job {job_id}] Escalating {kf_id} ({count} variants)...")
            job_manager.update_progress(job_id, 0, "Generating escalated prompts with Claude...")

            escalate_instruction = (
                f"Take this keyframe and INTENSIFY it. "
                f"Push every element further -- bolder colors, more dramatic lighting, "
                f"stronger contrast, more extreme angles, more vivid details. "
                f"Don't change the subject or concept, just amplify what's already there.\n\n"
                f"{'Original prompt: ' + kf_prompt[:300] + chr(10) + chr(10) if kf_prompt else ''}"
                f"Generate {count} escalation prompts, each more intense than the last:\n"
                f"1. Moderate escalation -- same scene, pushed 30% more dramatic\n"
                f"2. Heavy escalation -- same scene, pushed to cinematic extremes\n"
                f"{'3. Maximum escalation -- same scene at its absolute visual peak' + chr(10) if count >= 3 else ''}"
                f"{'4. Beyond -- transcendent version, almost abstract in its intensity' + chr(10) if count >= 4 else ''}\n"
                f"Each prompt should be 2-3 sentences with specific visual details. "
                f"Keep the same subject/composition but push every visual property to its extreme.\n\n"
                f'Respond with ONLY a JSON array: ["prompt 1", "prompt 2", ...]'
            )

            # Always send the image so Claude can see what to intensify
            with open(str(source_img), "rb") as _imgf:
                img_b64 = _b64.b64encode(_imgf.read()).decode()
            img_ext = source_img.suffix.lower()
            img_media = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg"}.get(
                img_ext.lstrip("."), "image/png"
            )

            response = client_llm.messages.create(
                model="claude-sonnet-4-20250514",
                max_tokens=4096,
                messages=[
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "image",
                                "source": {
                                    "type": "base64",
                                    "media_type": img_media,
                                    "data": img_b64,
                                },
                            },
                            {"type": "text", "text": escalate_instruction},
                        ],
                    }
                ],
            )
            text = response.content[0].text if response.content else "[]"
            json_match = _re_esc.search(r"\[[\s\S]*\]", text)
            prompts = _json.loads(json_match.group(0)) if json_match else []
            _log(f"[job {job_id}] Got {len(prompts)} escalation prompts")

            from scenecraft.db import get_meta as _get_meta2
            from scenecraft.db import update_keyframe
            from scenecraft.render.google_video import GoogleVideoClient

            img_client = GoogleVideoClient(vertex=True)
            _image_model = _get_meta2(pdir).get("image_model", "replicate/nano-banana-2")
            candidates_dir = pdir / "keyframe_candidates" / "candidates" / f"section_{kf_id}"
            candidates_dir.mkdir(parents=True, exist_ok=True)
            existing = _next_variant(candidates_dir, ".png") - 1

            all_cands = []
            for i, prompt in enumerate(prompts[:count]):
                v = existing + i + 1
                out_path = str(candidates_dir / f"v{v}.png")
                job_manager.update_progress(job_id, i, f"Escalating v{v}: {prompt[:50]}...")
                try:
                    img_client.stylize_image(
                        str(source_img), prompt, out_path, image_model=_image_model
                    )
                    # Update DB after each image so UI can show it immediately
                    all_cands = sorted(
                        [
                            f"keyframe_candidates/candidates/section_{kf_id}/{f.name}"
                            for f in candidates_dir.glob("v*.png")
                        ],
                        key=lambda p: int(p.rsplit("v", 1)[-1].split(".")[0]),
                    )
                    update_keyframe(pdir, kf_id, candidates=all_cands)
                except Exception as e:
                    _log(f"  v{v} failed: {type(e).__name__}: {e}")
                    _tb.print_exc()
                    job_manager.update_progress(job_id, i + 1, f"v{v} failed")

            job_manager.complete_job(
                job_id,
                {"keyframeId": kf_id, "candidates": all_cands, "prompts": prompts[:count]},
            )
        except Exception as e:
            _log(f"[job {job_id}] FAILED: {e}")
            job_manager.fail_job(job_id, str(e))

    threading.Thread(target=_run_escalate, daemon=True).start()
    _log(f"escalate-keyframe: {kf_id} count={count}")
    return {"jobId": job_id, "keyframeId": kf_id}


@router.post(
    "/api/projects/{name}/update-keyframe-label",
    operation_id="update_keyframe_label",
    dependencies=[Depends(project_dir)],
)
def update_keyframe_label(
    name: str,
    body: UpdateKeyframeLabelBody,
    pdir: Path = Depends(project_dir),
) -> dict:
    """Update label and label color for a keyframe."""
    from scenecraft.db import update_keyframe

    kf_id = body.keyframeId
    _log(f"update-keyframe-label: {kf_id} label={body.label!r}")
    update_keyframe(pdir, kf_id, label=body.label, label_color=body.labelColor)
    return {"success": True}


@router.post(
    "/api/projects/{name}/update-keyframe-style",
    operation_id="update_keyframe_style",
    dependencies=[Depends(project_dir)],
)
def update_keyframe_style(
    name: str,
    body: UpdateKeyframeStyleBody,
    pdir: Path = Depends(project_dir),
) -> dict:
    """Update blend mode, opacity, and/or refinement prompt for a keyframe."""
    from scenecraft.db import undo_begin as _ub
    from scenecraft.db import update_keyframe

    _ub(pdir, f"Update keyframe style {body.keyframeId}")
    fields: dict[str, object] = {}
    if body.blendMode is not None:
        fields["blend_mode"] = body.blendMode
    if body.opacity is not None:
        fields["opacity"] = body.opacity
    if body.refinementPrompt is not None:
        fields["refinement_prompt"] = body.refinementPrompt
    kf_id = body.keyframeId
    _log(f"update-keyframe-style: {kf_id} {fields}")
    update_keyframe(pdir, kf_id, **fields)
    return {"success": True}


@router.post(
    "/api/projects/{name}/assign-keyframe-image",
    operation_id="assign_keyframe_image",
    dependencies=[Depends(project_dir)],
)
def assign_keyframe_image(
    name: str,
    body: AssignKeyframeImageBody,
    pdir: Path = Depends(project_dir),
) -> dict:
    """Copy a source image to become the selected keyframe and add as candidate."""
    kf_id = body.keyframeId
    source_path = body.sourcePath

    src = pdir / source_path
    if not src.exists():
        raise ApiError("NOT_FOUND", f"Source not found: {source_path}", status_code=404)

    dst = pdir / "selected_keyframes" / f"{kf_id}.png"
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(str(src), str(dst))

    # Also create as next variant candidate so it appears in the candidates panel
    cand_dir = pdir / "keyframe_candidates" / "candidates" / f"section_{kf_id}"
    cand_dir.mkdir(parents=True, exist_ok=True)
    existing = _next_variant(cand_dir, ".png") - 1
    v = existing + 1
    shutil.copy2(str(src), str(cand_dir / f"v{v}.png"))

    from scenecraft.db import update_keyframe

    all_cands = sorted(
        [
            f"keyframe_candidates/candidates/section_{kf_id}/{f.name}"
            for f in cand_dir.glob("v*.png")
        ],
        key=lambda p: int(p.rsplit("v", 1)[-1].split(".")[0]),
    )
    update_keyframe(pdir, kf_id, selected=v, candidates=all_cands)
    _log(f"assign-keyframe-image: {source_path} -> {kf_id} as v{v} (selected={v})")
    return {"success": True, "selected": v}


# ---------------------------------------------------------------------------
# Generation (non-structural) -- NATIVE
# ---------------------------------------------------------------------------


@router.post(
    "/api/projects/{name}/generate-keyframe-variations",
    operation_id="generate_keyframe_variations",
    dependencies=[Depends(project_dir)],
)
def generate_keyframe_variations(
    name: str,
    body: GenerateKeyframeVariationsBody,
    pdir: Path = Depends(project_dir),
) -> dict:
    """Generate style-variation candidates for a keyframe via LLM + image model.

    Spawns a background job that:
      1. Asks Claude for diverse style transformation prompts
      2. Generates ``count`` images using the image model
      3. Updates the DB candidates list after each image
    Returns immediately with the job ID.
    """
    kf_id = body.keyframeId
    count = body.count

    source_img = pdir / "selected_keyframes" / f"{kf_id}.png"
    if not source_img.exists():
        # Fall back to base stills
        stills_dir = pdir / "assets" / "stills"
        if stills_dir.is_dir():
            for still in sorted(stills_dir.glob("*.png")):
                source_img = still
                break
    if not source_img.exists():
        raise ApiError("BAD_REQUEST", f"No source image found for {kf_id}", status_code=400)

    from scenecraft.db import get_keyframe

    kf = get_keyframe(pdir, kf_id)
    kf_prompt = kf.get("prompt", "") if kf else ""

    from scenecraft.ws_server import job_manager

    job_id = job_manager.create_job(
        "keyframe_variations",
        total=count,
        meta={"keyframeId": kf_id, "project": name},
    )

    def _run_variations():
        try:
            import json as _json
            import re as _re_var

            from anthropic import Anthropic

            api_key = os.environ.get("ANTHROPIC_API_KEY")
            client_llm = Anthropic(api_key=api_key)

            _log(f"[job {job_id}] Generating {count} variation prompts for {kf_id}...")
            job_manager.update_progress(job_id, 0, "Generating prompts with Claude...")

            response = client_llm.messages.create(
                model="claude-sonnet-4-20250514",
                max_tokens=4096,
                messages=[
                    {
                        "role": "user",
                        "content": (
                            f"Generate {count} wildly different style transformation prompts for a keyframe image. "
                            f"Each prompt should create a dramatically different visual world from the same source image.\n\n"
                            f"Original keyframe context: {kf_prompt[:200] if kf_prompt else 'No prompt'}\n\n"
                            f"Create {count} prompts that span the full spectrum:\n"
                            f"- One grounded/realistic transformation (different location, weather, time of day)\n"
                            f"- One surreal/dreamlike (impossible physics, melting reality, dream logic)\n"
                            f"- One cosmic/abstract (celestial energies, particle dissolution, void spaces)\n"
                            f"- One dark/dramatic (gothic, industrial, underwater, fire)\n\n"
                            f"Each prompt should be 2-3 sentences with specific visual details.\n\n"
                            f'Respond with ONLY a JSON array: ["prompt 1", "prompt 2", ...]'
                        ),
                    }
                ],
            )
            text = response.content[0].text if response.content else "[]"
            json_match = _re_var.search(r"\[[\s\S]*\]", text)
            prompts = _json.loads(json_match.group(0)) if json_match else []
            _log(f"[job {job_id}] Got {len(prompts)} prompts")

            # Generate images
            from scenecraft.db import get_meta as _get_meta
            from scenecraft.db import update_keyframe
            from scenecraft.render.google_video import GoogleVideoClient

            img_client = GoogleVideoClient(vertex=True)
            _image_model = _get_meta(pdir).get("image_model", "replicate/nano-banana-2")
            candidates_dir = pdir / "keyframe_candidates" / "candidates" / f"section_{kf_id}"
            candidates_dir.mkdir(parents=True, exist_ok=True)
            existing = _next_variant(candidates_dir, ".png") - 1

            all_cands = []
            for i, prompt in enumerate(prompts[:count]):
                v = existing + i + 1
                out_path = str(candidates_dir / f"v{v}.png")
                job_manager.update_progress(job_id, i, f"Generating v{v}: {prompt[:50]}...")
                try:
                    img_client.stylize_image(
                        str(source_img), prompt, out_path, image_model=_image_model
                    )
                    all_cands = sorted(
                        [
                            f"keyframe_candidates/candidates/section_{kf_id}/{f.name}"
                            for f in candidates_dir.glob("v*.png")
                        ],
                        key=lambda p: int(p.rsplit("v", 1)[-1].split(".")[0]),
                    )
                    update_keyframe(pdir, kf_id, candidates=all_cands)
                except Exception as e:
                    _log(f"  v{v} failed: {e}")
                    job_manager.update_progress(job_id, i + 1, f"v{v} failed")

            job_manager.complete_job(
                job_id,
                {"keyframeId": kf_id, "candidates": all_cands, "prompts": prompts[:count]},
            )
        except Exception as e:
            _log(f"[job {job_id}] FAILED: {e}")
            job_manager.fail_job(job_id, str(e))

    threading.Thread(target=_run_variations, daemon=True).start()
    return {"jobId": job_id, "keyframeId": kf_id}


@router.post(
    "/api/projects/{name}/generate-keyframe-candidates",
    operation_id="generate_keyframe_candidates",
    dependencies=[Depends(project_dir)],
)
def generate_keyframe_candidates(
    name: str, request: Request, body: GenerateKeyframeCandidatesBody
) -> dict:
    kf_id = body.keyframeId
    count = body.count
    refinement_prompt = body.refinementPrompt
    freeform = body.freeform

    _log(f"generate-keyframe-candidates: {name} kf={kf_id} count={count} freeform={freeform} refinement={bool(refinement_prompt)}")
    if not kf_id:
        raise ApiError("BAD_REQUEST", "Missing 'keyframeId'", status_code=400)

    work_dir = _work_dir(request)
    pdir = work_dir / name

    from scenecraft.ws_server import job_manager

    # Freeform: generate from prompt text only, no source image needed
    if freeform:
        from scenecraft.db import get_keyframe, update_keyframe as db_update_kf, get_meta
        kf = get_keyframe(pdir, kf_id)
        if not kf:
            raise ApiError("NOT_FOUND", f"Keyframe {kf_id} not found", status_code=404)
        prompt = kf.get("prompt", "")
        if not prompt:
            raise ApiError("BAD_REQUEST", f"Keyframe {kf_id} has no prompt for freeform generation", status_code=400)

        meta = get_meta(pdir)
        resolution = meta.get("resolution", [1920, 1080])
        if isinstance(resolution, list) and len(resolution) == 2:
            w, h = int(resolution[0]), int(resolution[1])
        else:
            w, h = 1920, 1080
        g = gcd(w, h)
        aspect_ratio = f"{w // g}:{h // g}"
        _log(f"  freeform: resolution={w}x{h} aspect={aspect_ratio}")

        candidates_dir = pdir / "keyframe_candidates" / "candidates" / f"section_{kf_id}"
        candidates_dir.mkdir(parents=True, exist_ok=True)
        existing_count = _next_variant(candidates_dir, ".png") - 1

        job_id = job_manager.create_job("keyframe_candidates", total=count, meta={"keyframeId": kf_id, "project": name})

        img_backend = _get_image_backend(pdir)

        def _run_freeform():
            try:
                from scenecraft.render.google_video import GoogleVideoClient
                client = GoogleVideoClient(vertex=True)
                import time as _time
                for i in range(count):
                    v = existing_count + i + 1
                    out_path = str(candidates_dir / f"v{v}.png")
                    varied = f"{prompt}, variation {v}" if v > 1 else prompt
                    while True:
                        try:
                            client.generate_image(varied, out_path, aspect_ratio=aspect_ratio, image_backend=img_backend)
                            _log(f"  freeform v{v} done")
                            break
                        except Exception as e:
                            _log(f"  freeform v{v} failed: {e} -- retrying in 60s")
                            job_manager.update_progress(job_id, i + 1, f"v{v} failed, retrying in 60s...")
                            _time.sleep(60)
                    job_manager.update_progress(job_id, i + 1, f"v{v} done")

                all_cands = sorted([
                    f"keyframe_candidates/candidates/section_{kf_id}/{f.name}"
                    for f in candidates_dir.glob("v*.png")
                ], key=lambda p: int(p.rsplit("v", 1)[-1].split(".")[0]))
                db_update_kf(pdir, kf_id, candidates=all_cands)
                job_manager.complete_job(job_id, {"keyframeId": kf_id, "candidates": all_cands})
            except Exception as e:
                job_manager.fail_job(job_id, str(e))

        threading.Thread(target=_run_freeform, daemon=True).start()
        return {"jobId": job_id, "keyframeId": kf_id}

    # Refinement: generate from the selected keyframe image with a refinement prompt
    if refinement_prompt:
        source_img = pdir / "selected_keyframes" / f"{kf_id}.png"
        if not source_img.exists():
            raise ApiError("BAD_REQUEST", f"No selected image for {kf_id} to refine", status_code=400)

        candidates_dir = pdir / "keyframe_candidates" / "candidates" / f"section_{kf_id}"
        candidates_dir.mkdir(parents=True, exist_ok=True)
        existing_count = _next_variant(candidates_dir, ".png") - 1

        job_id = job_manager.create_job("keyframe_candidates", total=count, meta={"keyframeId": kf_id, "project": name})

        img_backend = _get_image_backend(pdir)

        def _run_refine():
            try:
                from scenecraft.render.google_video import GoogleVideoClient
                client = GoogleVideoClient(vertex=True)
                import time as _time
                for i in range(count):
                    v = existing_count + i + 1
                    out_path = str(candidates_dir / f"v{v}.png")
                    varied = f"{refinement_prompt}, variation {v}" if v > 1 else refinement_prompt
                    while True:
                        try:
                            client.transform_image(str(source_img), varied, out_path, image_backend=img_backend)
                            break
                        except Exception as e:
                            _log(f"  v{v} failed: {e} -- retrying in 60s")
                            job_manager.update_progress(job_id, i + 1, f"v{v} failed, retrying in 60s...")
                            _time.sleep(60)
                    job_manager.update_progress(job_id, i + 1, f"v{v} done")

                from scenecraft.db import update_keyframe as _upd_kf
                all_cands = sorted([
                    f"keyframe_candidates/candidates/section_{kf_id}/{f.name}"
                    for f in candidates_dir.glob("v*.png")
                ], key=lambda p: int(p.rsplit("v", 1)[-1].split(".")[0]))
                _upd_kf(pdir, kf_id, candidates=all_cands)

                job_manager.complete_job(job_id, {"keyframeId": kf_id, "candidates": all_cands})
            except Exception as e:
                job_manager.fail_job(job_id, str(e))

        threading.Thread(target=_run_refine, daemon=True).start()
        return {"jobId": job_id, "keyframeId": kf_id}

    # Default: stylize from source image using the keyframe prompt
    from scenecraft.db import get_keyframe, update_keyframe as db_update_kf
    kf = get_keyframe(pdir, kf_id)
    if not kf:
        raise ApiError("NOT_FOUND", f"Keyframe {kf_id} not found", status_code=404)

    source = kf.get("source", f"selected_keyframes/{kf_id}.png")
    source_path = pdir / source
    if not source_path.exists():
        source_path = pdir / "selected_keyframes" / f"{kf_id}.png"
    if not source_path.exists():
        raise ApiError("BAD_REQUEST", f"No source image for {kf_id}", status_code=400)

    prompt = kf.get("prompt", "")
    if not prompt:
        raise ApiError("BAD_REQUEST", f"Keyframe {kf_id} has no prompt", status_code=400)

    candidates_dir = pdir / "keyframe_candidates" / "candidates" / f"section_{kf_id}"
    candidates_dir.mkdir(parents=True, exist_ok=True)
    existing_count = _next_variant(candidates_dir, ".png") - 1

    job_id = job_manager.create_job("keyframe_candidates", total=count, meta={"keyframeId": kf_id, "project": name})

    _log(f"  stylize: {kf_id} source={source_path.name} prompt={prompt[:60]!r} count={count} existing={existing_count}")

    # Capture these in closure for thread
    _source_path = source_path
    _prompt = prompt
    _kf_id = kf_id
    _pdir = pdir
    _candidates_dir = candidates_dir
    _existing_count = existing_count

    def _run():
        try:
            from scenecraft.render.google_video import GoogleVideoClient
            from scenecraft.db import get_meta as _get_meta_gen, update_keyframe as _upd_kf2
            from concurrent.futures import ThreadPoolExecutor
            client = GoogleVideoClient(vertex=True)
            _img_model = _get_meta_gen(_pdir).get("image_model", "replicate/nano-banana-2")

            def _gen_one(v):
                import time as _time
                out_path = str(_candidates_dir / f"v{v}.png")
                if Path(out_path).exists():
                    job_manager.update_progress(job_id, v - _existing_count, f"v{v} cached")
                    return
                varied = f"{_prompt}, variation {v}" if v > 1 else _prompt
                while True:
                    try:
                        client.stylize_image(str(_source_path), varied, out_path, image_model=_img_model)
                        _log(f"    {_kf_id} v{v} done")
                        break
                    except Exception as e:
                        _log(f"    {_kf_id} v{v} FAILED: {e} -- retrying in 60s")
                        job_manager.update_progress(job_id, v - _existing_count, f"v{v} failed, retrying in 60s...")
                        _time.sleep(60)
                job_manager.update_progress(job_id, v - _existing_count, f"v{v}")

            variants = list(range(_existing_count + 1, _existing_count + count + 1))
            with ThreadPoolExecutor(max_workers=count) as pool:
                pool.map(_gen_one, variants)

            all_cands = sorted([
                f"keyframe_candidates/candidates/section_{_kf_id}/{f.name}"
                for f in _candidates_dir.glob("v*.png")
            ], key=lambda p: int(p.rsplit("v", 1)[-1].split(".")[0]))
            _upd_kf2(_pdir, _kf_id, candidates=all_cands)

            job_manager.complete_job(job_id, {"keyframeId": _kf_id, "candidates": all_cands})
        except Exception as e:
            job_manager.fail_job(job_id, str(e))

    threading.Thread(target=_run, daemon=True).start()
    return {"jobId": job_id, "keyframeId": kf_id}


@router.post(
    "/api/projects/{name}/generate-slot-keyframe-candidates",
    operation_id="generate_slot_keyframe_candidates",
    dependencies=[Depends(project_dir)],
)
def generate_slot_keyframe_candidates(
    name: str, request: Request, body: GenerateSlotKeyframeCandidatesBody
) -> dict:
    tr_id = body.transitionId

    work_dir = _work_dir(request)
    pdir = work_dir / name

    _log(f"generate-slot-keyframe-candidates: tr_id={tr_id or 'all'}")
    from scenecraft.ws_server import job_manager
    job_id = job_manager.create_job("slot_keyframe_candidates", total=0, meta={"transitionId": tr_id or "all", "project": name})

    def _run():
        try:
            from scenecraft.render.narrative import generate_slot_keyframe_candidates as _gen_slot_kf
            _gen_slot_kf(str(pdir), vertex=False)

            slot_kf_dir = pdir / "slot_keyframe_candidates" / "candidates"
            candidates: dict[str, list[str]] = {}
            if slot_kf_dir.exists():
                for section_dir in sorted(slot_kf_dir.iterdir()):
                    if section_dir.is_dir() and section_dir.name.startswith("section_"):
                        slot_key = section_dir.name.replace("section_", "")
                        images = sorted([
                            f"slot_keyframe_candidates/candidates/{section_dir.name}/{f.name}"
                            for f in section_dir.glob("v*.png")
                        ])
                        if images:
                            candidates[slot_key] = images

            job_manager.complete_job(job_id, {"candidates": candidates})
        except Exception as e:
            job_manager.fail_job(job_id, str(e))

    threading.Thread(target=_run, daemon=True).start()
    return {"jobId": job_id}


@router.post(
    "/api/projects/{name}/suggest-keyframe-prompts",
    operation_id="suggest_keyframe_prompts",
    dependencies=[Depends(project_dir)],
)
def suggest_keyframe_prompts(
    name: str, request: Request, body: SuggestKeyframePromptsBody
) -> dict:
    section_label = body.sectionLabel
    section_content = body.sectionContent
    events = body.events
    base_still = body.baseStillName

    if not events:
        raise ApiError("BAD_REQUEST", "Missing 'events'", status_code=400)

    pdir = _work_dir(request) / name
    _log(f"suggest-keyframe-prompts: {name} section={section_label!r} events={len(events)} still={base_still!r}")

    try:
        import json as _json

        api_key = os.environ.get("ANTHROPIC_API_KEY")
        if not api_key:
            raise ApiError("INTERNAL_ERROR", "ANTHROPIC_API_KEY not set", status_code=500)

        _log(f"  Calling Claude for {len(events)} event prompts...")
        from anthropic import Anthropic
        client = Anthropic(api_key=api_key)

        BATCH_SIZE = 30

        system_prompt = (
            f"You are a visionary art director creating keyframe images for a cinematic music video. "
            f"Each prompt will transform a base photograph (\"{base_still}\") into a vivid scene "
            f"through Imagen style transfer.\n\n"
            f"Your prompts should span the full spectrum from concrete to abstract. Mix freely between:\n\n"
            f"CONCRETE -- tangible places and scenes:\n"
            f"- A mist-shrouded ancient forest with bioluminescent fungi pulsing on twisted bark\n"
            f"- A haunting gothic cathedral where stained glass bleeds liquid color onto stone floors\n"
            f"- An underwater ballroom where jellyfish chandeliers illuminate drowned aristocrats\n\n"
            f"ABSTRACT -- celestial, cosmic, and ethereal:\n"
            f"- Entities of pure light floating in infinite black space, trailing ribbons of golden plasma\n"
            f"- Celestial energies carved into the sky like cracks in reality, violet and amber fire bleeding through\n"
            f"- A figure dissolving into thousands of luminous particles drifting upward like inverse rain\n"
            f"- Geometric mandalas of living crystal rotating in a void of deep indigo, humming with color\n"
            f"- The subject's silhouette filled with a galaxy, stars spilling from their edges like sand\n\n"
            f"Match the prompt style to the musical energy:\n"
            f"- Quiet/intimate -> dreamlike, ethereal, delicate abstractions or whispered landscapes\n"
            f"- Building/rising -> transformative, things becoming other things, reality bending\n"
            f"- Loud/climactic -> explosive cosmic events, overwhelming scale, sensory overload\n"
            f"- Descending/fading -> dissolution, particles scattering, light dimming into beautiful darkness\n\n"
            f"Section: \"{section_label}\"\n"
            f"Musical description:\n{section_content}\n\n"
            f"For each event, write a prompt (2-3 sentences) that:\n"
            f"- Creates a SPECIFIC visual -- whether a real place, an impossible space, or a cosmic abstraction\n"
            f"- Includes concrete visual details even for abstract scenes: what material, what light, what color, what texture\n"
            f"- Varies WILDLY across events -- alternate between grounded and transcendent\n"
            f"- Treats the base image as the subject transformed by or placed within this vision\n\n"
            f"Respond with ONLY a JSON array, no markdown fences: [{{\"eventIndex\": N, \"prompt\": \"...\"}}, ...]"
        )

        all_suggestions = []
        batches = [events[i:i + BATCH_SIZE] for i in range(0, len(events), BATCH_SIZE)]
        _log(f"  Processing {len(batches)} batch(es) of up to {BATCH_SIZE} events each")

        for batch_idx, batch in enumerate(batches):
            event_list = "\n".join(
                f"  {ev.get('_originalIndex', i)}: t={ev.get('time', 0):.2f}s, stem={ev.get('stem_source', '?')}, "
                f"effect={ev.get('effect', '?')}, intensity={ev.get('intensity', 0) * 100:.0f}%"
                for i, ev in enumerate(batch)
            )
            for i, ev in enumerate(batch):
                if "_originalIndex" not in ev:
                    ev["_originalIndex"] = batch_idx * BATCH_SIZE + i

            batch_prompt = f"{system_prompt}\n\nAudio events:\n{event_list}"

            batch_suggestions = None
            for attempt in range(3):
                response = client.messages.create(
                    model="claude-sonnet-4-20250514",
                    max_tokens=16384,
                    messages=[{"role": "user", "content": batch_prompt}],
                )
                text = response.content[0].text if response.content else ""
                _log(f"  Batch {batch_idx + 1}/{len(batches)} attempt {attempt + 1}: len={len(text)}, stop={response.stop_reason}")

                json_match = _re.search(r"\[[\s\S]*\]", text)
                if json_match:
                    try:
                        batch_suggestions = _json.loads(json_match.group(0))
                        break
                    except _json.JSONDecodeError:
                        _log(f"  Batch {batch_idx + 1} attempt {attempt + 1}: JSON parse error")
                else:
                    _log(f"  Batch {batch_idx + 1} attempt {attempt + 1}: no JSON array found")

            if batch_suggestions:
                all_suggestions.extend(batch_suggestions)
            else:
                _log(f"  Batch {batch_idx + 1} failed after retries")

        suggestions = all_suggestions
        if not suggestions:
            raise ApiError("INTERNAL_ERROR", "Failed to parse prompt suggestions after retries", status_code=500)

        _log(f"  Generated {len(suggestions)} prompt suggestions across {len(batches)} batch(es)")

        # Auto-persist suggestions to DB
        try:
            from scenecraft.db import set_meta
            set_meta(pdir, f"section_suggestions:{section_label}", _json.dumps(suggestions))
            if base_still:
                set_meta(pdir, f"section_still:{section_label}", base_still)
        except Exception:
            pass

        return {"suggestions": suggestions}

    except ApiError:
        raise
    except Exception as e:
        _log(f"  suggest-keyframe-prompts error: {e}")
        raise ApiError("INTERNAL_ERROR", str(e), status_code=500)


@router.post(
    "/api/projects/{name}/enhance-keyframe-prompt",
    operation_id="enhance_keyframe_prompt",
    dependencies=[Depends(project_dir)],
)
def enhance_keyframe_prompt(
    name: str, request: Request, body: EnhanceKeyframePromptBody
) -> dict:
    current_prompt = body.prompt
    section_content = body.sectionContent
    event = body.event or {}

    _log(f"enhance-keyframe-prompt: {name} prompt={current_prompt[:60]!r}")

    try:
        api_key = os.environ.get("ANTHROPIC_API_KEY")
        if not api_key:
            raise ApiError("INTERNAL_ERROR", "ANTHROPIC_API_KEY not set", status_code=500)

        from anthropic import Anthropic
        client = Anthropic(api_key=api_key)

        event_context = ""
        if event:
            event_context = (
                f"\n\nAudio event context:\n"
                f"  Time: {event.get('time', 0):.2f}s\n"
                f"  Stem: {event.get('stem_source', '?')}\n"
                f"  Effect: {event.get('effect', '?')}\n"
                f"  Intensity: {event.get('intensity', 0) * 100:.0f}%\n"
            )
            if event.get("rationale"):
                event_context += f"  Rationale: {event['rationale']}\n"

        section_text = f"\n\nMusical context for this section:\n{section_content}\n" if section_content else ""

        prompt_text = (
            "You are a visionary art director enhancing a keyframe image prompt for Imagen style transfer. "
            "Take the user's existing prompt and make it more vivid, specific, and cinematic. "
            "Add details about materials, textures, lighting quality, atmosphere, scale, and spatial depth. "
            "Keep the core scene and intent but make it significantly more descriptive and tangible.\n\n"
            f"Current prompt: \"{current_prompt}\"\n"
            f"{section_text}"
            f"{event_context}\n"
            "Reply with ONLY the enhanced prompt, no preamble or explanation. "
            "Keep it to 2-4 sentences. Describe a CONCRETE, FILMABLE scene."
        )

        response = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=400,
            messages=[{"role": "user", "content": prompt_text}],
        )

        enhanced = response.content[0].text.strip()
        _log(f"  Enhanced prompt: {enhanced[:80]}...")
        return {"success": True, "prompt": enhanced}

    except ApiError:
        raise
    except Exception as e:
        _log(f"  enhance-keyframe-prompt error: {e}")
        raise ApiError("INTERNAL_ERROR", str(e), status_code=500)


# ---------------------------------------------------------------------------
# Catch-all update-keyframe -- chat-tool-only in legacy; added as a REST route
# so the openapi.json surfaces it for codegen (T66) + tool annotation (T67).
# Delegates to ``chat._exec_update_keyframe`` for the exact same field mapping.
# ---------------------------------------------------------------------------


@router.post(
    "/api/projects/{name}/update-keyframe",
    operation_id="update_keyframe",
    dependencies=[Depends(project_dir)],
)
def update_keyframe(
    name: str,
    pdir: Path = Depends(project_dir),
    body: dict = Body(...),
) -> dict:
    """Chat-tool alignment: batch-update arbitrary keyframe fields.

    Body must include ``keyframe_id``; all other accepted keys match
    ``chat._UPDATE_KEYFRAME_FIELDS``. Returns the same payload shape
    the chat tool produces, so tests that go through either path see
    the same response envelope.
    """
    from scenecraft.chat import _exec_update_keyframe

    kf_id = body.get("keyframe_id")
    if not kf_id or not isinstance(kf_id, str):
        raise ApiError("BAD_REQUEST", "Missing 'keyframe_id'", status_code=400)

    result = _exec_update_keyframe(pdir, body)
    if isinstance(result, dict) and "error" in result:
        raise ApiError("BAD_REQUEST", result["error"], status_code=400)
    return result


__all__ = ["router"]
