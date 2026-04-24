"""Transition routes -- M16 T61 / T65 native port (fan-in merge).

22 routes covering the transition mutation surface, plus one net-new
route (``batch-delete-transitions``) that was previously chat-tool-only.
ALL are now natively implemented; zero ``dispatch_legacy`` calls remain.

Structural routes (delete / restore / split / batch-delete) gate on
``Depends(project_lock)``.

Handlers are sync (``def``, not ``async def``) so the starlette
threadpool runs them -- see ``keyframes.py`` for rationale.

Operation IDs are chat-tool-aligned (T67):
  * ``delete-transition``   -> ``delete_transition``
  * ``split-transition``    -> ``split_transition``
  * ``batch-delete-transitions`` -> ``batch_delete_transitions`` (new REST)
"""

from __future__ import annotations

import json
import shutil
import subprocess as sp
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Body, Depends, Request
from fastapi.responses import JSONResponse

from scenecraft.api.deps import project_dir, project_lock
from scenecraft.api.errors import ApiError
from scenecraft.api.models.transitions import (
    BatchDeleteTransitionsBody,
    ClipTrimEdgeBody,
    CopyTransitionStyleBody,
    DeleteTransitionBody,
    DuplicateTransitionVideoBody,
    EnhanceTransitionActionBody,
    GenerateTransitionActionBody,
    GenerateTransitionCandidatesBody,
    LinkAudioBody,
    MoveTransitionsBody,
    RestoreTransitionBody,
    SelectTransitionsBody,
    SplitTransitionBody,
    TransitionEffectAddBody,
    TransitionEffectDeleteBody,
    TransitionEffectUpdateBody,
    UpdateTransitionActionBody,
    UpdateTransitionLabelBody,
    UpdateTransitionRemapBody,
    UpdateTransitionStyleBody,
    UpdateTransitionTrimBody,
)
from scenecraft.api.utils import _log, _get_video_backend

router = APIRouter(tags=["transitions"])


def _work_dir(request: Request) -> Path:
    wd = getattr(request.app.state, "work_dir", None)
    if wd is None:
        raise ApiError("INTERNAL_ERROR", "work_dir not configured", status_code=500)
    return wd


# ---------------------------------------------------------------------------
# Helpers (shared by multiple handlers)
# ---------------------------------------------------------------------------


def _parse_ts(ts) -> float:
    """Parse ``"M:SS.ff"`` or numeric timestamp to seconds."""
    parts = str(ts).split(":")
    if len(parts) == 2:
        return int(parts[0]) * 60 + float(parts[1])
    return float(ts) if isinstance(ts, (int, float)) else 0.0


def _fmt_ts(seconds: float) -> str:
    """Format seconds back to ``"M:SS.ff"``."""
    s = max(0.0, seconds)
    m = int(s // 60)
    rem = s - m * 60
    return f"{m}:{rem:05.2f}"


# ---------------------------------------------------------------------------
# Selection / trim / move (non-structural) -- NATIVE
# ---------------------------------------------------------------------------


@router.post(
    "/api/projects/{name}/select-transitions",
    operation_id="select_transitions",
    dependencies=[Depends(project_dir)],
)
def select_transitions(
    name: str,
    body: SelectTransitionsBody,
    pdir: Path = Depends(project_dir),
) -> dict:
    """Apply transition video selections."""
    selections = body.selections
    if not selections:
        raise ApiError("BAD_REQUEST", "Missing 'selections' in body", status_code=400)

    _log(f"select-transitions: {len(selections)} selections")
    try:
        import shutil
        import subprocess as _sp
        from scenecraft.db import (
            update_transition,
            get_transition,
            get_pool_segment,
            get_tr_candidates as _db_get_tc,
        )
        selected_dir = pdir / "selected_transitions"
        selected_dir.mkdir(parents=True, exist_ok=True)

        trim_updates = {}

        def _probe_duration(path: Path) -> float | None:
            try:
                r = _sp.run(
                    ["ffprobe", "-v", "error", "-show_entries",
                     "format=duration", "-of", "csv=p=0", str(path)],
                    capture_output=True, text=True, timeout=5,
                )
                return float(r.stdout.strip()) if r.returncode == 0 and r.stdout.strip() else None
            except Exception:
                return None

        def _resolve_to_segment_id(tr_id: str, slot_idx: int, value) -> str | None:
            if value is None:
                return None
            if isinstance(value, int):
                cands = _db_get_tc(pdir, tr_id, slot_idx)
                if 1 <= value <= len(cands):
                    return cands[value - 1]["id"]
                _log(f"  warning: {tr_id} slot_{slot_idx}: legacy rank {value} out of range ({len(cands)} candidates)")
                return None
            return str(value)

        by_tr: dict[str, dict[int, str | None]] = {}
        for key, value in selections.items():
            if "_slot_" in key:
                tr_id, slot_part = key.rsplit("_slot_", 1)
                slot_idx = int(slot_part)
            else:
                tr_id = key
                slot_idx = 0
            seg_id = _resolve_to_segment_id(tr_id, slot_idx, value)
            by_tr.setdefault(tr_id, {})[slot_idx] = seg_id

        for tr_id, slot_updates in by_tr.items():
            tr_row = get_transition(pdir, tr_id) or {}
            n_slots = tr_row.get("slots", 1)

            for slot_idx, seg_id in slot_updates.items():
                dest = selected_dir / f"{tr_id}_slot_{slot_idx}.mp4"
                if seg_id is not None:
                    seg = get_pool_segment(pdir, seg_id)
                    if not seg:
                        _log(f"  warning: pool_segment not found: {seg_id}")
                        continue
                    source = pdir / seg["poolPath"]
                    if source.exists():
                        shutil.copy2(str(source), str(dest))
                    else:
                        _log(f"  warning: pool segment file missing: {source}")
                else:
                    if dest.exists():
                        dest.unlink()

            existing_selected = tr_row.get("selected")
            if isinstance(existing_selected, list):
                current = list(existing_selected)
            elif existing_selected is None or existing_selected == []:
                current = [None] * n_slots
            else:
                current = [existing_selected]
            while len(current) < n_slots:
                current.append(None)
            for slot_idx, seg_id in slot_updates.items():
                if slot_idx < len(current):
                    current[slot_idx] = seg_id
            update_transition(pdir, tr_id, selected=current)

            slot_0_seg_id = slot_updates.get(0)
            if slot_0_seg_id is not None:
                sel_path = selected_dir / f"{tr_id}_slot_0.mp4"
                new_src_dur = None
                seg_full = get_pool_segment(pdir, slot_0_seg_id)
                if seg_full and seg_full.get("durationSeconds"):
                    new_src_dur = seg_full["durationSeconds"]
                elif sel_path.exists():
                    new_src_dur = _probe_duration(sel_path)
                if new_src_dur is not None and new_src_dur > 0:
                    trim_in = tr_row.get("trim_in") or 0
                    trim_out = tr_row.get("trim_out")
                    clamped_trim_out = min(trim_out, new_src_dur) if trim_out is not None else new_src_dur
                    clamped_trim_in = min(trim_in, max(0, new_src_dur - 0.1))
                    update_transition(
                        pdir, tr_id,
                        source_video_duration=new_src_dur,
                        trim_in=clamped_trim_in,
                        trim_out=clamped_trim_out,
                    )
                    trim_updates[tr_id] = {
                        "sourceVideoDuration": new_src_dur,
                        "trimIn": clamped_trim_in,
                        "trimOut": clamped_trim_out,
                        "clamped": (trim_out is not None and trim_out > new_src_dur) or trim_in > (new_src_dur - 0.1),
                    }
                    _log(f"  {tr_id}: source={new_src_dur:.2f}s trim=[{clamped_trim_in:.2f}, {clamped_trim_out:.2f}]")

        auto_link_results: list[dict] = []
        try:
            from scenecraft.audio.linking import link_audio_for_transition
            for tr_id, slot_updates in by_tr.items():
                if 0 not in slot_updates:
                    continue
                new_seg_id = slot_updates[0]
                if new_seg_id is None:
                    continue
                link_result = link_audio_for_transition(pdir, tr_id, replace=True)
                auto_link_results.append(link_result)
                _log(f"  auto-link {tr_id}: {link_result['status']} "
                     f"{('reason=' + str(link_result.get('reason'))) if link_result.get('reason') else ''}".rstrip())
        except Exception as e:
            _log(f"auto-link error (non-fatal): {e}")

        return {
            "success": True,
            "applied": len(selections),
            "trimUpdates": trim_updates,
            "audioLinks": auto_link_results,
        }
    except ApiError:
        raise
    except Exception as e:
        raise ApiError("INTERNAL_ERROR", str(e), status_code=500)


@router.post(
    "/api/projects/{name}/update-transition-trim",
    operation_id="update_transition_trim",
    dependencies=[Depends(project_dir)],
)
def update_transition_trim(
    name: str,
    body: UpdateTransitionTrimBody,
    pdir: Path = Depends(project_dir),
) -> dict:
    """Atomic trim + boundary move."""
    tr_id = body.transitionId
    trim_in = body.trimIn
    trim_out = body.trimOut
    from_ts = body.fromKfTimestamp
    to_ts = body.toKfTimestamp

    try:
        from scenecraft.db import (
            undo_begin as _ub, get_transition, update_transition,
            update_keyframe, get_keyframe, get_transitions as _get_trs,
        )
        _ub(pdir, f"Trim drag on {tr_id}")

        tr = get_transition(pdir, tr_id)
        if not tr:
            raise ApiError("NOT_FOUND", f"Transition not found: {tr_id}", status_code=404)

        old_from_kf = get_keyframe(pdir, tr["from"]) if tr.get("from") else None
        old_to_kf = get_keyframe(pdir, tr["to"]) if tr.get("to") else None
        old_from_t = _parse_ts(old_from_kf["timestamp"]) if old_from_kf else 0.0
        old_to_t = _parse_ts(old_to_kf["timestamp"]) if old_to_kf else 0.0

        trim_updates: dict = {}
        if trim_in is not None:
            trim_updates["trim_in"] = float(trim_in)
        if trim_out is not None:
            trim_updates["trim_out"] = float(trim_out)
        if trim_updates:
            update_transition(pdir, tr_id, **trim_updates)

        kf_updates: list[tuple[str, str]] = []
        if from_ts is not None and tr.get("from"):
            kf_updates.append((tr["from"], from_ts))
        if to_ts is not None and tr.get("to"):
            kf_updates.append((tr["to"], to_ts))

        all_trs = _get_trs(pdir) if kf_updates else []
        for kf_id, new_ts in kf_updates:
            update_keyframe(pdir, kf_id, timestamp=new_ts)
            new_time = _parse_ts(new_ts)
            for adj in all_trs:
                if adj["from"] == kf_id or adj["to"] == kf_id:
                    other_id = adj["to"] if adj["from"] == kf_id else adj["from"]
                    other_kf = get_keyframe(pdir, other_id)
                    if other_kf:
                        other_time = _parse_ts(other_kf["timestamp"])
                        dur = round(abs(new_time - other_time), 2)
                        update_transition(pdir, adj["id"], duration_seconds=dur)

        new_from_t = _parse_ts(from_ts) if from_ts is not None else old_from_t
        new_to_t = _parse_ts(to_ts) if to_ts is not None else old_to_t
        from scenecraft.render.cache_invalidation import invalidate_frames_for_mutation
        invalidate_frames_for_mutation(
            pdir,
            ranges=[
                (min(old_from_t, old_to_t), max(old_from_t, old_to_t)),
                (min(new_from_t, new_to_t), max(new_from_t, new_to_t)),
            ],
        )

        _log(
            f"update-transition-trim: {tr_id} "
            f"trim=[{trim_in},{trim_out}] kfts=[{from_ts},{to_ts}]"
        )
        return {
            "success": True,
            "transitionId": tr_id,
            "trimIn": trim_updates.get("trim_in"),
            "trimOut": trim_updates.get("trim_out"),
        }
    except ApiError:
        raise
    except Exception as e:
        import traceback
        traceback.print_exc()
        raise ApiError("INTERNAL_ERROR", str(e), status_code=500)


@router.post(
    "/api/projects/{name}/clip-trim-edge",
    operation_id="clip_trim_edge",
    dependencies=[Depends(project_dir)],
)
def clip_trim_edge(
    name: str,
    body: ClipTrimEdgeBody,
    pdir: Path = Depends(project_dir),
) -> dict:
    """Left/right clip-edge trim."""
    tr_id = body.transitionId
    edge = body.edge
    new_ts = body.newBoundaryTimestamp
    new_trim = body.newTrim
    mode = body.mode
    if edge not in ("right", "left"):
        raise ApiError("BAD_REQUEST", "edge must be 'right' or 'left'", status_code=400)
    if mode not in ("trim", "ripple"):
        raise ApiError("BAD_REQUEST", "mode must be 'trim' or 'ripple'", status_code=400)

    try:
        from scenecraft.db import (
            undo_begin as _ub, get_transition, update_transition,
            update_keyframe, get_keyframe, get_transitions as _get_trs,
            get_keyframes as _get_kfs,
            add_keyframe, add_transition, next_keyframe_id, next_transition_id,
            delete_transition as db_delete_transition, delete_keyframe as db_delete_keyframe,
        )
        import datetime as _dt

        tr = get_transition(pdir, tr_id)
        if not tr:
            raise ApiError("NOT_FOUND", f"Transition not found: {tr_id}", status_code=404)

        _ub(pdir, f"Clip-trim {edge} drag on {tr_id}")

        from_kf = get_keyframe(pdir, tr.get("from"))
        to_kf = get_keyframe(pdir, tr.get("to"))
        if not from_kf or not to_kf:
            raise ApiError("INTERNAL_ERROR", "Missing boundary keyframes", status_code=500)

        all_trs = _get_trs(pdir)

        new_boundary_time = _parse_ts(new_ts)
        new_trim = float(new_trim)

        if edge == "right":
            old_boundary_kf = to_kf
            old_boundary_time = _parse_ts(to_kf["timestamp"])
            neighbor = next(
                (t for t in all_trs if t.get("from") == old_boundary_kf["id"] and t["id"] != tr_id),
                None,
            )
        else:
            old_boundary_kf = from_kf
            old_boundary_time = _parse_ts(from_kf["timestamp"])
            neighbor = next(
                (t for t in all_trs if t.get("to") == old_boundary_kf["id"] and t["id"] != tr_id),
                None,
            )

        delta = new_boundary_time - old_boundary_time
        shrinking = (edge == "right" and delta < 0) or (edge == "left" and delta > 0)
        extending = (edge == "right" and delta > 0) or (edge == "left" and delta < 0)

        if abs(delta) < 0.001:
            if edge == "right":
                update_transition(pdir, tr_id, trim_out=new_trim)
            else:
                update_transition(pdir, tr_id, trim_in=new_trim)
            return {"success": True, "transitionId": tr_id, "mode": "trim-only"}

        if mode == "ripple":
            all_kfs = _get_kfs(pdir)
            if edge == "right":
                shift = delta
                anchor_time = old_boundary_time
                update_transition(pdir, tr_id, trim_out=new_trim)
            else:
                shift = -delta
                anchor_time = _parse_ts(to_kf["timestamp"])
                update_transition(pdir, tr_id, trim_in=new_trim)

            for kf in all_kfs:
                kf_time = _parse_ts(kf["timestamp"])
                if kf_time >= anchor_time - 0.0005:
                    update_keyframe(pdir, kf["id"], timestamp=_fmt_ts(kf_time + shift))

            from_time_now = _parse_ts(from_kf["timestamp"])
            to_time_now = _parse_ts(to_kf["timestamp"]) + (shift if edge == "left" else 0)
            if edge == "right":
                to_time_now = old_boundary_time + shift
            new_dur = round(abs(to_time_now - from_time_now), 2)
            update_transition(pdir, tr_id, duration_seconds=new_dur)

            _log(f"clip-trim-edge RIPPLE: {tr_id} edge={edge} delta={delta:.3f} shift={shift:.3f}")
            return {
                "success": True, "mode": "ripple",
                "transitionId": tr_id, "shift": shift,
            }

        def _is_empty_tr(t):
            sel = t.get("selected") if t else None
            if sel is None:
                return True
            if isinstance(sel, list):
                return len(sel) == 0 or all(v is None for v in sel)
            return False

        if shrinking and neighbor is not None and _is_empty_tr(neighbor):
            update_keyframe(pdir, old_boundary_kf["id"], timestamp=_fmt_ts(new_boundary_time))

            if edge == "right":
                new_current_dur = round(abs(new_boundary_time - _parse_ts(from_kf["timestamp"])), 2)
                empty_far_kf = get_keyframe(pdir, neighbor.get("to"))
                empty_far_time = _parse_ts(empty_far_kf["timestamp"]) if empty_far_kf else old_boundary_time
                new_empty_dur = round(abs(empty_far_time - new_boundary_time), 2)
                update_transition(pdir, tr_id, trim_out=new_trim, duration_seconds=new_current_dur)
                update_transition(pdir, neighbor["id"], duration_seconds=new_empty_dur)
            else:
                new_current_dur = round(abs(_parse_ts(to_kf["timestamp"]) - new_boundary_time), 2)
                empty_far_kf = get_keyframe(pdir, neighbor.get("from"))
                empty_far_time = _parse_ts(empty_far_kf["timestamp"]) if empty_far_kf else old_boundary_time
                new_empty_dur = round(abs(new_boundary_time - empty_far_time), 2)
                update_transition(pdir, tr_id, trim_in=new_trim, duration_seconds=new_current_dur)
                update_transition(pdir, neighbor["id"], duration_seconds=new_empty_dur)

            sel_video = pdir / "selected_transitions" / f"{tr_id}_slot_0.mp4"
            if sel_video.exists():
                try:
                    import subprocess as _sp
                    import shutil as _sh
                    sel_kf_dir = pdir / "selected_keyframes"
                    sel_kf_dir.mkdir(parents=True, exist_ok=True)
                    kf_img = sel_kf_dir / f"{old_boundary_kf['id']}.png"
                    _sp.run(
                        ["ffmpeg", "-y", "-ss", f"{float(new_trim):.3f}",
                         "-i", str(sel_video), "-vframes", "1", "-q:v", "2", str(kf_img)],
                        capture_output=True, timeout=10,
                    )
                    if kf_img.exists() and kf_img.stat().st_size > 0:
                        cand_dir = pdir / "keyframe_candidates" / "candidates" / f"section_{old_boundary_kf['id']}"
                        cand_dir.mkdir(parents=True, exist_ok=True)
                        _sh.copy2(str(kf_img), str(cand_dir / "v1.png"))
                        update_keyframe(
                            pdir, old_boundary_kf["id"],
                            selected=1,
                            candidates=[f"keyframe_candidates/candidates/section_{old_boundary_kf['id']}/v1.png"],
                        )
                except Exception as _e:
                    _log(f"  re-extract failed: {_e}")

            _log(f"clip-trim-edge SHRINK-EXTEND-EMPTY: {tr_id} edge={edge} "
                 f"moved_kf={old_boundary_kf['id']} empty={neighbor['id']}")
            return {
                "success": True, "mode": "shrink-extend-empty",
                "transitionId": tr_id, "movedKfId": old_boundary_kf["id"], "emptyTrId": neighbor["id"],
            }

        if shrinking:
            new_kf_id = next_keyframe_id(pdir)
            track_id = old_boundary_kf.get("track_id", "track_1")
            add_keyframe(pdir, {
                "id": new_kf_id,
                "timestamp": _fmt_ts(new_boundary_time),
                "track_id": track_id,
                "selected": None,
                "candidates": [],
            })

            empty_tr_id = next_transition_id(pdir)
            if edge == "right":
                new_current_dur = round(abs(new_boundary_time - _parse_ts(from_kf["timestamp"])), 2)
                empty_dur = round(abs(old_boundary_time - new_boundary_time), 2)
                add_transition(pdir, {
                    "id": empty_tr_id,
                    "from": new_kf_id,
                    "to": old_boundary_kf["id"],
                    "duration_seconds": empty_dur,
                    "selected": [None],
                    "track_id": track_id,
                })
                update_transition(
                    pdir, tr_id,
                    **{"to": new_kf_id, "trim_out": new_trim, "duration_seconds": new_current_dur},
                )
            else:
                new_current_dur = round(abs(_parse_ts(to_kf["timestamp"]) - new_boundary_time), 2)
                empty_dur = round(abs(new_boundary_time - old_boundary_time), 2)
                add_transition(pdir, {
                    "id": empty_tr_id,
                    "from": old_boundary_kf["id"],
                    "to": new_kf_id,
                    "duration_seconds": empty_dur,
                    "selected": [None],
                    "track_id": track_id,
                })
                update_transition(
                    pdir, tr_id,
                    **{"from": new_kf_id, "trim_in": new_trim, "duration_seconds": new_current_dur},
                )

            update_keyframe(
                pdir, old_boundary_kf["id"],
                selected=None, candidates=[],
            )

            sel_video = pdir / "selected_transitions" / f"{tr_id}_slot_0.mp4"
            if sel_video.exists():
                try:
                    import subprocess as _sp
                    import shutil as _sh
                    sel_kf_dir = pdir / "selected_keyframes"
                    sel_kf_dir.mkdir(parents=True, exist_ok=True)
                    kf_img = sel_kf_dir / f"{new_kf_id}.png"
                    _sp.run(
                        ["ffmpeg", "-y", "-ss", f"{float(new_trim):.3f}",
                         "-i", str(sel_video), "-vframes", "1", "-q:v", "2", str(kf_img)],
                        capture_output=True, timeout=10,
                    )
                    if kf_img.exists() and kf_img.stat().st_size > 0:
                        cand_dir = pdir / "keyframe_candidates" / "candidates" / f"section_{new_kf_id}"
                        cand_dir.mkdir(parents=True, exist_ok=True)
                        _sh.copy2(str(kf_img), str(cand_dir / "v1.png"))
                        update_keyframe(
                            pdir, new_kf_id,
                            selected=1,
                            candidates=[f"keyframe_candidates/candidates/section_{new_kf_id}/v1.png"],
                        )
                        _log(f"  extracted frame at source_offset={float(new_trim):.2f}s -> {new_kf_id}.png")
                except Exception as _e:
                    _log(f"  frame extraction failed: {_e}")

            _log(f"clip-trim-edge SHRINK: {tr_id} edge={edge} delta={delta:.3f} "
                 f"new_kf={new_kf_id} empty_tr={empty_tr_id}")
            return {
                "success": True, "mode": "shrink-gap-insert",
                "transitionId": tr_id, "newKfId": new_kf_id, "emptyTrId": empty_tr_id,
            }

        # extending
        if neighbor is None:
            update_keyframe(pdir, old_boundary_kf["id"], timestamp=_fmt_ts(new_boundary_time))
            if edge == "right":
                new_dur = round(abs(new_boundary_time - _parse_ts(from_kf["timestamp"])), 2)
                update_transition(pdir, tr_id, trim_out=new_trim, duration_seconds=new_dur)
            else:
                new_dur = round(abs(_parse_ts(to_kf["timestamp"]) - new_boundary_time), 2)
                update_transition(pdir, tr_id, trim_in=new_trim, duration_seconds=new_dur)
            _log(f"clip-trim-edge EXTEND (no neighbor): {tr_id} edge={edge} delta={delta:.3f}")
            return {"success": True, "mode": "extend-no-neighbor", "transitionId": tr_id}

        if edge == "right":
            neighbor_far_kf = get_keyframe(pdir, neighbor.get("to"))
        else:
            neighbor_far_kf = get_keyframe(pdir, neighbor.get("from"))
        neighbor_far_time = _parse_ts(neighbor_far_kf["timestamp"]) if neighbor_far_kf else None
        fully_consuming = (
            neighbor_far_time is not None and (
                (edge == "right" and new_boundary_time >= neighbor_far_time) or
                (edge == "left" and new_boundary_time <= neighbor_far_time)
            )
        )

        if fully_consuming:
            now_iso = _dt.datetime.utcnow().replace(microsecond=0).isoformat() + "Z"
            db_delete_transition(pdir, neighbor["id"], now_iso)
            db_delete_keyframe(pdir, old_boundary_kf["id"], now_iso)
            if edge == "right":
                new_dur = round(abs(neighbor_far_time - _parse_ts(from_kf["timestamp"])), 2)
                update_transition(
                    pdir, tr_id,
                    **{"to": neighbor_far_kf["id"], "trim_out": new_trim, "duration_seconds": new_dur},
                )
            else:
                new_dur = round(abs(_parse_ts(to_kf["timestamp"]) - neighbor_far_time), 2)
                update_transition(
                    pdir, tr_id,
                    **{"from": neighbor_far_kf["id"], "trim_in": new_trim, "duration_seconds": new_dur},
                )
            _log(f"clip-trim-edge EXTEND CONSUME: {tr_id} consumed neighbor={neighbor['id']}")
            return {
                "success": True, "mode": "extend-consume",
                "transitionId": tr_id, "consumedNeighbor": neighbor["id"],
            }

        # Partial extend
        update_keyframe(pdir, old_boundary_kf["id"], timestamp=_fmt_ts(new_boundary_time))

        neighbor_trim_in = neighbor.get("trim_in") or 0.0
        neighbor_trim_out = neighbor.get("trim_out")
        neighbor_src_dur = neighbor.get("source_video_duration")
        if neighbor_trim_out is None:
            neighbor_trim_out = neighbor_src_dur if neighbor_src_dur is not None else 0.0

        if edge == "right":
            old_neighbor_dur = neighbor_far_time - old_boundary_time
            new_neighbor_dur = neighbor_far_time - new_boundary_time
            if old_neighbor_dur > 0:
                neighbor_factor = (neighbor_trim_out - neighbor_trim_in) / old_neighbor_dur
                new_neighbor_trim_in = neighbor_trim_in + (new_boundary_time - old_boundary_time) * neighbor_factor
                update_transition(
                    pdir, neighbor["id"],
                    trim_in=new_neighbor_trim_in,
                    duration_seconds=round(new_neighbor_dur, 2),
                )
            new_current_dur = round(abs(new_boundary_time - _parse_ts(from_kf["timestamp"])), 2)
            update_transition(pdir, tr_id, trim_out=new_trim, duration_seconds=new_current_dur)
        else:
            old_neighbor_dur = old_boundary_time - neighbor_far_time
            new_neighbor_dur = new_boundary_time - neighbor_far_time
            if old_neighbor_dur > 0:
                neighbor_factor = (neighbor_trim_out - neighbor_trim_in) / old_neighbor_dur
                new_neighbor_trim_out = neighbor_trim_out + (new_boundary_time - old_boundary_time) * neighbor_factor
                update_transition(
                    pdir, neighbor["id"],
                    trim_out=new_neighbor_trim_out,
                    duration_seconds=round(new_neighbor_dur, 2),
                )
            new_current_dur = round(abs(_parse_ts(to_kf["timestamp"]) - new_boundary_time), 2)
            update_transition(pdir, tr_id, trim_in=new_trim, duration_seconds=new_current_dur)

        _log(f"clip-trim-edge EXTEND PARTIAL: {tr_id} edge={edge} delta={delta:.3f} neighbor={neighbor['id']}")
        return {
            "success": True, "mode": "extend-partial",
            "transitionId": tr_id, "neighborId": neighbor["id"],
        }
    except ApiError:
        raise
    except Exception as e:
        import traceback
        traceback.print_exc()
        raise ApiError("INTERNAL_ERROR", str(e), status_code=500)


@router.post(
    "/api/projects/{name}/move-transitions",
    operation_id="move_transitions",
    dependencies=[Depends(project_dir)],
)
def move_transitions(
    name: str,
    body: MoveTransitionsBody,
    pdir: Path = Depends(project_dir),
) -> dict:
    """Drag-to-move / copy clips."""
    mode = body.mode
    track_delta = body.trackDelta
    time_delta = body.timeDeltaSeconds
    transition_ids = body.transitionIds
    auto_create_tracks = body.autoCreateTracks

    if mode not in ("move", "copy"):
        raise ApiError("BAD_REQUEST", "mode must be 'move' or 'copy'", status_code=400)
    if not transition_ids:
        raise ApiError("BAD_REQUEST", "transitionIds must be a non-empty list", status_code=400)

    track_delta_i = int(track_delta)
    time_delta_f = float(time_delta)

    try:
        from scenecraft.db import (
            undo_begin as _ub, get_transition, update_transition,
            update_keyframe, get_keyframe, get_transitions as _get_trs,
            add_keyframe, next_keyframe_id, next_transition_id,
            add_transition, get_tracks as _get_tracks, add_track as _add_track,
            generate_id, delete_transition as db_del_tr, delete_keyframe as db_del_kf,
            clone_tr_candidates, get_transition_effects, add_transition_effect,
        )
        import datetime as _dt

        # 1. Fetch track layout
        tracks = _get_tracks(pdir)
        if not tracks:
            raise ApiError("INTERNAL_ERROR", "No tracks in project", status_code=500)
        track_index_by_id = {t["id"]: i for i, t in enumerate(tracks)}

        # 2. Pre-validate
        moved_set = set(transition_ids)
        trs_to_move: list[dict] = []
        overflow_above = 0
        overflow_below = 0

        for tr_id in transition_ids:
            tr = get_transition(pdir, tr_id)
            if not tr:
                raise ApiError("NOT_FOUND", f"Transition not found: {tr_id}", status_code=404)
            if tr.get("deleted_at"):
                raise ApiError("BAD_REQUEST", f"Transition is deleted: {tr_id}", status_code=400)
            fkf = get_keyframe(pdir, tr.get("from"))
            tkf = get_keyframe(pdir, tr.get("to"))
            if not fkf or not tkf:
                raise ApiError("INTERNAL_ERROR", f"Missing boundary kfs for {tr_id}", status_code=500)
            new_from_time = _parse_ts(fkf["timestamp"]) + time_delta_f
            new_to_time = _parse_ts(tkf["timestamp"]) + time_delta_f
            if new_from_time < -0.0005:
                raise ApiError("BAD_REQUEST",
                    f"Move would push {tr_id} past timeline start (new_from={new_from_time:.2f})",
                    status_code=400)
            source_track_id = tr.get("track_id") or "track_1"
            if source_track_id not in track_index_by_id:
                raise ApiError("INTERNAL_ERROR", f"tr {tr_id} on unknown track {source_track_id}", status_code=500)
            source_index = track_index_by_id[source_track_id]
            target_index = source_index + track_delta_i
            if target_index < 0:
                overflow_above = max(overflow_above, -target_index)
            elif target_index >= len(tracks):
                overflow_below = max(overflow_below, target_index - (len(tracks) - 1))
            trs_to_move.append({
                "tr": tr, "from_kf": fkf, "to_kf": tkf,
                "new_from_time": new_from_time, "new_to_time": new_to_time,
                "source_track_id": source_track_id, "source_index": source_index,
                "target_index_raw": target_index,
            })

        if (overflow_above > 0 or overflow_below > 0) and not auto_create_tracks:
            raise ApiError("OUT_OF_RANGE_TRACK",
                f"trackDelta={track_delta_i} would exceed track range "
                f"(need {overflow_above} above, {overflow_below} below) "
                f"and autoCreateTracks=false",
                status_code=400)

        # 3. Undo group
        _ub(pdir, f"Move {len(transition_ids)} tr(s) by {time_delta_f:.2f}s trackDelta={track_delta_i}")

        # 4. Auto-create tracks
        created_track_ids: list[str] = []
        if overflow_above > 0 or overflow_below > 0:
            min_z = min(t["z_order"] for t in tracks)
            max_z = max(t["z_order"] for t in tracks)
            next_ordinal = len(tracks) + 1
            new_above: list[dict] = []
            for i in range(overflow_above):
                tid = generate_id("track")
                z = min_z - (overflow_above - i)
                tr_obj = {"id": tid, "name": f"Track {next_ordinal}", "z_order": z,
                          "blend_mode": "normal", "base_opacity": 1.0, "enabled": True}
                _add_track(pdir, tr_obj)
                new_above.append(tr_obj)
                created_track_ids.append(tid)
                next_ordinal += 1
            new_below: list[dict] = []
            for i in range(overflow_below):
                tid = generate_id("track")
                z = max_z + (i + 1)
                tr_obj = {"id": tid, "name": f"Track {next_ordinal}", "z_order": z,
                          "blend_mode": "normal", "base_opacity": 1.0, "enabled": True}
                _add_track(pdir, tr_obj)
                new_below.append(tr_obj)
                created_track_ids.append(tid)
                next_ordinal += 1
            tracks = new_above + tracks + new_below
            track_index_by_id = {t["id"]: i for i, t in enumerate(tracks)}

        # 5. Resolve target track
        for entry in trs_to_move:
            final_index = entry["target_index_raw"] + overflow_above
            if final_index < 0 or final_index >= len(tracks):
                raise ApiError("INTERNAL_ERROR",
                    f"target_index {final_index} still out of range after auto-create",
                    status_code=500)
            entry["target_track_id"] = tracks[final_index]["id"]

        # 6. Snapshot for boundary classification
        all_active_trs = _get_trs(pdir)

        def is_boundary(kf_id: str) -> bool:
            for other in all_active_trs:
                if other["id"] in moved_set:
                    continue
                if other.get("deleted_at"):
                    continue
                if other.get("from") == kf_id or other.get("to") == kf_id:
                    return True
            return False

        def _copy_selected_transitions_cache(src_tr_id: str, dst_tr_id: str):
            import shutil as _sh
            sel_dir = pdir / "selected_transitions"
            if not sel_dir.exists():
                return
            for src_file in sel_dir.glob(f"{src_tr_id}_slot_*.mp4"):
                suffix = src_file.name[len(src_tr_id):]
                dst_file = sel_dir / f"{dst_tr_id}{suffix}"
                try:
                    _sh.copy2(str(src_file), str(dst_file))
                except Exception as _e:
                    _log(f"  cache copy failed {src_file.name} -> {dst_file.name}: {_e}")

        # 7. Source cleanup spans
        spans_by_src: dict[str, list[tuple[float, float]]] = {}
        if mode == "move":
            for entry in trs_to_move:
                src = entry["source_track_id"]
                a = _parse_ts(entry["from_kf"]["timestamp"])
                b = _parse_ts(entry["to_kf"]["timestamp"])
                spans_by_src.setdefault(src, []).append((a, b))

        def merge_spans(spans):
            if not spans:
                return []
            s = sorted(spans, key=lambda x: x[0])
            merged = [s[0]]
            for a, b in s[1:]:
                la, lb = merged[-1]
                if a <= lb + 1e-4:
                    merged[-1] = (la, max(lb, b))
                else:
                    merged.append((a, b))
            return merged

        merged_by_src = {src: merge_spans(sp) for src, sp in spans_by_src.items()}

        def find_surviving_left(src_track_id, span_from_t):
            for other in all_active_trs:
                if other["id"] in moved_set or other.get("deleted_at"):
                    continue
                if other.get("track_id") != src_track_id:
                    continue
                to_kf_id = other.get("to")
                if not to_kf_id:
                    continue
                to_kf = get_keyframe(pdir, to_kf_id)
                if not to_kf or to_kf.get("track_id") != src_track_id:
                    continue
                if abs(_parse_ts(to_kf["timestamp"]) - span_from_t) < 1e-3:
                    return to_kf_id
            return None

        def find_surviving_right(src_track_id, span_to_t):
            for other in all_active_trs:
                if other["id"] in moved_set or other.get("deleted_at"):
                    continue
                if other.get("track_id") != src_track_id:
                    continue
                from_kf_id = other.get("from")
                if not from_kf_id:
                    continue
                from_kf = get_keyframe(pdir, from_kf_id)
                if not from_kf or from_kf.get("track_id") != src_track_id:
                    continue
                if abs(_parse_ts(from_kf["timestamp"]) - span_to_t) < 1e-3:
                    return from_kf_id
            return None

        # 8. Bridge empty trs
        for src_track_id, merged in merged_by_src.items():
            for span_from_t, span_to_t in merged:
                left_kf_id = find_surviving_left(src_track_id, span_from_t)
                right_kf_id = find_surviving_right(src_track_id, span_to_t)
                if left_kf_id and right_kf_id and left_kf_id != right_kf_id:
                    new_tr_id = next_transition_id(pdir)
                    bridge_dur = round(max(0.0, span_to_t - span_from_t), 2)
                    add_transition(pdir, {
                        "id": new_tr_id, "from": left_kf_id, "to": right_kf_id,
                        "duration_seconds": bridge_dur, "slots": 1, "action": "",
                        "use_global_prompt": False, "selected": [None],
                        "remap": {"method": "linear", "target_duration": bridge_dur},
                        "track_id": src_track_id,
                    })

        # 9. Apply kf writes + tr updates
        interior_updated: set[str] = set()
        boundary_new: dict[tuple[str, str, str], str] = {}
        copy_kf_cache: dict[tuple[str, str], str] = {}
        new_tr_ids: list[str] = []
        source_to_new_tr: dict[str, str] = {}

        def resolve_new_kf(orig_kf, new_time, target_track_id):
            orig_id = orig_kf["id"]
            if is_boundary(orig_id):
                key = (orig_id, target_track_id, _fmt_ts(new_time))
                if key in boundary_new:
                    return boundary_new[key]
                new_kf_id = next_keyframe_id(pdir)
                add_keyframe(pdir, {
                    "id": new_kf_id, "timestamp": _fmt_ts(new_time),
                    "section": orig_kf.get("section", "") or "", "source": "", "prompt": "",
                    "track_id": target_track_id, "selected": None, "candidates": [],
                })
                boundary_new[key] = new_kf_id
                return new_kf_id
            if orig_id not in interior_updated:
                update_keyframe(pdir, orig_id, track_id=target_track_id, timestamp=_fmt_ts(new_time))
                interior_updated.add(orig_id)
            return orig_id

        def resolve_copy_kf(orig_kf, new_time, target_track_id):
            ts_str = _fmt_ts(new_time)
            cache_key = (target_track_id, ts_str)
            if cache_key in copy_kf_cache:
                return copy_kf_cache[cache_key]
            from scenecraft.db import get_keyframes as _get_kfs
            for existing in _get_kfs(pdir):
                if existing.get("deleted_at"):
                    continue
                if existing.get("track_id") != target_track_id:
                    continue
                if abs(_parse_ts(existing["timestamp"]) - new_time) < 1e-4:
                    copy_kf_cache[cache_key] = existing["id"]
                    return existing["id"]
            new_kf_id = next_keyframe_id(pdir)
            add_keyframe(pdir, {
                "id": new_kf_id, "timestamp": ts_str,
                "section": orig_kf.get("section", "") or "", "source": "", "prompt": "",
                "track_id": target_track_id, "selected": None, "candidates": [],
            })
            copy_kf_cache[cache_key] = new_kf_id
            return new_kf_id

        def _build_clone_tr_payload(src_tr, new_id, target_track_id, new_from_kf_id, new_to_kf_id, new_dur):
            return {
                "id": new_id, "from": new_from_kf_id, "to": new_to_kf_id,
                "duration_seconds": new_dur, "track_id": target_track_id,
                "slots": src_tr.get("slots", 1), "action": src_tr.get("action", ""),
                "use_global_prompt": src_tr.get("use_global_prompt", False),
                "selected": src_tr.get("selected"),
                "remap": src_tr.get("remap") or {"method": "linear", "target_duration": new_dur},
                "trim_in": src_tr.get("trim_in"), "trim_out": src_tr.get("trim_out"),
                "source_video_duration": src_tr.get("source_video_duration"),
                "label": src_tr.get("label", ""), "label_color": src_tr.get("label_color", ""),
                "tags": src_tr.get("tags", []), "blend_mode": src_tr.get("blend_mode", ""),
                "opacity": src_tr.get("opacity"), "opacity_curve": src_tr.get("opacity_curve"),
                "red_curve": src_tr.get("red_curve"), "green_curve": src_tr.get("green_curve"),
                "blue_curve": src_tr.get("blue_curve"), "black_curve": src_tr.get("black_curve"),
                "hue_shift_curve": src_tr.get("hue_shift_curve"),
                "saturation_curve": src_tr.get("saturation_curve"),
                "invert_curve": src_tr.get("invert_curve"),
                "is_adjustment": src_tr.get("is_adjustment", False),
                "mask_center_x": src_tr.get("mask_center_x"),
                "mask_center_y": src_tr.get("mask_center_y"),
                "mask_radius": src_tr.get("mask_radius"),
                "mask_feather": src_tr.get("mask_feather"),
                "transform_x": src_tr.get("transform_x"),
                "transform_y": src_tr.get("transform_y"),
                "transform_x_curve": src_tr.get("transform_x_curve"),
                "transform_y_curve": src_tr.get("transform_y_curve"),
                "transform_z_curve": src_tr.get("transform_z_curve"),
                "hidden": src_tr.get("hidden", False),
                "anchor_x": src_tr.get("anchor_x"),
                "anchor_y": src_tr.get("anchor_y"),
            }

        for entry in trs_to_move:
            tr = entry["tr"]
            tr_id = tr["id"]
            target_track_id = entry["target_track_id"]
            new_from_time = entry["new_from_time"]
            new_to_time = entry["new_to_time"]
            fkf = entry["from_kf"]
            tkf = entry["to_kf"]
            new_dur = round(max(0.0, new_to_time - new_from_time), 2)

            if mode == "copy":
                new_from_kf_id = resolve_copy_kf(fkf, new_from_time, target_track_id)
                new_to_kf_id = new_from_kf_id if tkf["id"] == fkf["id"] else resolve_copy_kf(tkf, new_to_time, target_track_id)
                new_tr_id = next_transition_id(pdir)
                payload = _build_clone_tr_payload(tr, new_tr_id, target_track_id, new_from_kf_id, new_to_kf_id, new_dur)
                add_transition(pdir, payload)
                try:
                    clone_tr_candidates(pdir, source_transition_id=tr_id, target_transition_id=new_tr_id, new_source="copy-inherit")
                except Exception as _e:
                    _log(f"  copy: clone_tr_candidates failed: {_e}")
                try:
                    for fx in get_transition_effects(pdir, tr_id):
                        add_transition_effect(pdir, new_tr_id, fx.get("effect_type") or fx.get("type"), fx.get("params"))
                except Exception as _e:
                    _log(f"  copy: effects clone failed: {_e}")
                _copy_selected_transitions_cache(tr_id, new_tr_id)
                entry["new_from_kf_id"] = new_from_kf_id
                entry["new_to_kf_id"] = new_to_kf_id
                entry["new_tr_id"] = new_tr_id
                new_tr_ids.append(new_tr_id)
                source_to_new_tr[tr_id] = new_tr_id
                continue

            # move mode
            new_from_kf_id = resolve_new_kf(fkf, new_from_time, target_track_id)
            new_to_kf_id = new_from_kf_id if tkf["id"] == fkf["id"] else resolve_new_kf(tkf, new_to_time, target_track_id)
            update_fields = {"track_id": target_track_id, "duration_seconds": new_dur}
            update_fields["from"] = new_from_kf_id
            update_fields["to"] = new_to_kf_id
            update_transition(pdir, tr_id, **update_fields)

            if abs(time_delta_f) > 1e-9 and new_from_kf_id != fkf["id"]:
                from scenecraft.db import (
                    get_audio_clip_links_for_transition as _get_links,
                    get_db as _get_db,
                )
                links = _get_links(pdir, tr_id)
                if links:
                    clip_ids = [lk["audio_clip_id"] for lk in links]
                    placeholders = ",".join("?" for _ in clip_ids)
                    conn = _get_db(pdir)
                    conn.execute(
                        f"UPDATE audio_clips "
                        f"SET start_time = start_time + ?, end_time = end_time + ? "
                        f"WHERE id IN ({placeholders}) AND deleted_at IS NULL",
                        [time_delta_f, time_delta_f, *clip_ids],
                    )
                    conn.commit()

            entry["new_from_kf_id"] = new_from_kf_id
            entry["new_to_kf_id"] = new_to_kf_id

        # 10. Overlap resolution
        now_iso = _dt.datetime.now(_dt.timezone.utc).isoformat()
        consumed_ids: list[str] = []
        split_ids: list[str] = []

        def _is_empty_sel(sel):
            if sel is None:
                return True
            if isinstance(sel, list):
                return len(sel) == 0 or all(v is None for v in sel)
            return False

        def _trim_fallback(tr_row, from_time, to_time):
            ti = tr_row.get("trim_in") or 0.0
            to_v = tr_row.get("trim_out")
            if to_v is None:
                to_v = tr_row.get("source_video_duration")
            if to_v is None:
                to_v = max(0.0, to_time - from_time)
            return float(ti), float(to_v)

        def _soft_delete_kf_if_orphan(kf_id):
            if not kf_id:
                return
            active_refs = [
                t for t in _get_trs(pdir)
                if not t.get("deleted_at") and (t.get("from") == kf_id or t.get("to") == kf_id)
            ]
            if not active_refs:
                db_del_kf(pdir, kf_id, now_iso)

        new_tr_ids_set = set(new_tr_ids)

        for entry in trs_to_move:
            dragged_tr_id = entry.get("new_tr_id") if mode == "copy" else entry["tr"]["id"]
            target_track_id = entry["target_track_id"]
            new_from = entry["new_from_time"]
            new_to = entry["new_to_time"]
            new_from_kf_id = entry["new_from_kf_id"]
            new_to_kf_id = entry["new_to_kf_id"]

            if new_to - new_from < 0.0005:
                continue

            all_trs_now = _get_trs(pdir)
            overlaps = []
            for t in all_trs_now:
                if t.get("deleted_at"):
                    continue
                if mode == "copy":
                    if t["id"] in new_tr_ids_set or t["id"] == dragged_tr_id:
                        continue
                else:
                    if t["id"] in moved_set or t["id"] == dragged_tr_id:
                        continue
                if t.get("track_id") != target_track_id:
                    continue
                tfrom_kf = get_keyframe(pdir, t.get("from"))
                tto_kf = get_keyframe(pdir, t.get("to"))
                if not tfrom_kf or not tto_kf:
                    continue
                tf = _parse_ts(tfrom_kf["timestamp"])
                tt = _parse_ts(tto_kf["timestamp"])
                if tt <= new_from + 0.0005 or tf >= new_to - 0.0005:
                    continue
                overlaps.append({"tr": t, "tf": tf, "tt": tt, "from_kf": tfrom_kf, "to_kf": tto_kf})

            case_d = [o for o in overlaps if o["tf"] < new_from - 0.0005 and o["tt"] > new_to + 0.0005]
            if case_d:
                tgt = case_d[0]
                tr_row = tgt["tr"]
                tf, tt = tgt["tf"], tgt["tt"]
                is_empty = _is_empty_sel(tr_row.get("selected"))
                left_id = next_transition_id(pdir)
                right_id = next_transition_id(pdir)
                if is_empty:
                    left_trim_in = left_trim_out = right_trim_in = right_trim_out = None
                else:
                    ti, to_v = _trim_fallback(tr_row, tf, tt)
                    factor = (to_v - ti) / max(1e-6, (tt - tf))
                    left_trim_in = ti
                    left_trim_out = ti + (new_from - tf) * factor
                    right_trim_in = ti + (new_to - tf) * factor
                    right_trim_out = to_v
                left_payload = {
                    "id": left_id, "from": tr_row.get("from"), "to": new_from_kf_id,
                    "duration_seconds": round(new_from - tf, 2),
                    "selected": tr_row.get("selected"), "track_id": target_track_id,
                    "source_video_duration": tr_row.get("source_video_duration"),
                }
                if not is_empty:
                    left_payload["trim_in"] = left_trim_in
                    left_payload["trim_out"] = left_trim_out
                right_payload = {
                    "id": right_id, "from": new_to_kf_id, "to": tr_row.get("to"),
                    "duration_seconds": round(tt - new_to, 2),
                    "selected": tr_row.get("selected"), "track_id": target_track_id,
                    "source_video_duration": tr_row.get("source_video_duration"),
                }
                if not is_empty:
                    right_payload["trim_in"] = right_trim_in
                    right_payload["trim_out"] = right_trim_out
                add_transition(pdir, left_payload)
                add_transition(pdir, right_payload)
                if not is_empty:
                    try:
                        clone_tr_candidates(pdir, source_transition_id=tr_row["id"], target_transition_id=left_id, new_source="split-inherit")
                        clone_tr_candidates(pdir, source_transition_id=tr_row["id"], target_transition_id=right_id, new_source="split-inherit")
                    except Exception as _e:
                        _log(f"  clone_tr_candidates failed: {_e}")
                    try:
                        for fx in get_transition_effects(pdir, tr_row["id"]):
                            add_transition_effect(pdir, left_id, fx.get("effect_type") or fx.get("type"), fx.get("params"))
                            add_transition_effect(pdir, right_id, fx.get("effect_type") or fx.get("type"), fx.get("params"))
                    except Exception as _e:
                        _log(f"  effects clone failed: {_e}")
                    _copy_selected_transitions_cache(tr_row["id"], left_id)
                    _copy_selected_transitions_cache(tr_row["id"], right_id)
                db_del_tr(pdir, tr_row["id"], now_iso)
                split_ids.append(tr_row["id"])
                continue

            for o in overlaps:
                tr_row = o["tr"]
                tf, tt = o["tf"], o["tt"]
                is_empty = _is_empty_sel(tr_row.get("selected"))
                if tf >= new_from - 0.0005 and tt <= new_to + 0.0005:
                    db_del_tr(pdir, tr_row["id"], now_iso)
                    _soft_delete_kf_if_orphan(tr_row.get("from"))
                    _soft_delete_kf_if_orphan(tr_row.get("to"))
                    consumed_ids.append(tr_row["id"])
                    continue
                if tf < new_from - 0.0005 and tt <= new_to + 0.0005:
                    uf = {"to": new_from_kf_id, "duration_seconds": round(new_from - tf, 2)}
                    if not is_empty:
                        ti, to_v = _trim_fallback(tr_row, tf, tt)
                        factor = (to_v - ti) / max(1e-6, (tt - tf))
                        uf["trim_out"] = ti + (new_from - tf) * factor
                    update_transition(pdir, tr_row["id"], **uf)
                    split_ids.append(tr_row["id"])
                    continue
                if tf >= new_from - 0.0005 and tt > new_to + 0.0005:
                    uf = {"from": new_to_kf_id, "duration_seconds": round(tt - new_to, 2)}
                    if not is_empty:
                        ti, to_v = _trim_fallback(tr_row, tf, tt)
                        factor = (to_v - ti) / max(1e-6, (tt - tf))
                        uf["trim_in"] = ti + (new_to - tf) * factor
                    update_transition(pdir, tr_row["id"], **uf)
                    split_ids.append(tr_row["id"])
                    continue

        _log(
            f"move-transitions: {len(transition_ids)} tr(s) by {time_delta_f:.2f}s "
            f"trackDelta={track_delta_i} createdTracks={len(created_track_ids)} "
            f"consumed={len(consumed_ids)} split={len(split_ids)} mode={mode}"
        )
        moved_ids_response = [e["new_tr_id"] for e in trs_to_move] if mode == "copy" else list(transition_ids)
        return {
            "success": True,
            "movedTransitionIds": moved_ids_response,
            "createdTrackIds": created_track_ids,
            "consumedTransitionIds": consumed_ids,
            "splitTransitionIds": split_ids,
        }
    except ApiError:
        raise
    except Exception as e:
        import traceback
        traceback.print_exc()
        raise ApiError("INTERNAL_ERROR", str(e), status_code=500)


# ---------------------------------------------------------------------------
# Structural -- delete / restore / split / batch-delete
# ---------------------------------------------------------------------------


@router.post(
    "/api/projects/{name}/delete-transition",
    operation_id="delete_transition",
    dependencies=[Depends(project_lock), Depends(project_dir)],
)
def delete_transition(
    name: str,
    body: DeleteTransitionBody,
    pdir: Path = Depends(project_dir),
) -> dict:
    """Soft-delete a transition to bin."""
    tr_id = body.transitionId

    from scenecraft.db import undo_begin as _ub
    _ub(pdir, f"Delete transition {tr_id}")

    try:
        _log(f"delete-transition: {tr_id}")
        from scenecraft.db import delete_transition as db_del_tr
        now = datetime.now(timezone.utc).isoformat()
        db_del_tr(pdir, tr_id, now)
        return {"success": True, "binned": {"id": tr_id, "deleted_at": now}}
    except ApiError:
        raise
    except Exception as e:
        raise ApiError("INTERNAL_ERROR", str(e), status_code=500)


@router.post(
    "/api/projects/{name}/restore-transition",
    operation_id="restore_transition",
    dependencies=[Depends(project_lock), Depends(project_dir)],
)
def restore_transition(
    name: str,
    body: RestoreTransitionBody,
    pdir: Path = Depends(project_dir),
) -> dict:
    """Restore a transition from bin."""
    tr_id = body.transitionId

    try:
        from scenecraft.db import restore_transition as db_restore_tr
        _log(f"restore-transition: {tr_id}")
        db_restore_tr(pdir, tr_id)
        return {"success": True, "transition": {"id": tr_id}}
    except ApiError:
        raise
    except Exception as e:
        raise ApiError("INTERNAL_ERROR", str(e), status_code=500)


@router.post(
    "/api/projects/{name}/split-transition",
    operation_id="split_transition",
    dependencies=[Depends(project_lock), Depends(project_dir)],
)
def split_transition(
    name: str,
    body: SplitTransitionBody,
    pdir: Path = Depends(project_dir),
) -> dict:
    """Split a transition at a given time, creating two new transitions and a mid-keyframe."""
    tr_id = body.transitionId
    split_time_raw = body.atTime
    split_time = _parse_ts(split_time_raw)

    try:
        from scenecraft.db import (
            get_transition, get_keyframe, delete_transition as db_del_tr,
            add_keyframe as db_add_kf, add_transition as db_add_tr,
            next_keyframe_id, next_transition_id,
            clone_tr_candidates as _clone_tc,
            update_keyframe as _upd_kf,
        )

        tr = get_transition(pdir, tr_id)
        if not tr:
            raise ApiError("NOT_FOUND", f"Transition {tr_id} not found", status_code=404)

        from_kf = get_keyframe(pdir, tr["from"])
        to_kf = get_keyframe(pdir, tr["to"])
        if not from_kf or not to_kf:
            raise ApiError("BAD_REQUEST", "Keyframes not found", status_code=400)

        from_time = _parse_ts(from_kf["timestamp"])
        to_time = _parse_ts(to_kf["timestamp"])

        if split_time <= from_time or split_time >= to_time:
            raise ApiError("BAD_REQUEST", "Split time must be within transition range", status_code=400)

        _log(f"split-transition: {tr_id} at {split_time:.2f}s (range {from_time:.2f}-{to_time:.2f})")

        split_progress = (split_time - from_time) / (to_time - from_time)
        dur1 = round(split_time - from_time, 2)
        dur2 = round(to_time - split_time, 2)

        orig_trim_in = tr.get("trim_in") or 0
        orig_trim_out = tr.get("trim_out")
        orig_src_dur = tr.get("source_video_duration")
        orig_span = (orig_trim_out - orig_trim_in) if (orig_trim_out is not None) else None
        split_source_offset = (orig_trim_in + split_progress * orig_span) if orig_span else None

        tr_track = tr.get("track_id", "track_1")

        new_kf_id = next_keyframe_id(pdir)
        db_add_kf(pdir, {
            "id": new_kf_id, "timestamp": _fmt_ts(split_time), "section": "",
            "source": f"selected_keyframes/{new_kf_id}.png", "prompt": "",
            "candidates": [], "selected": None, "track_id": tr_track,
        })

        now = datetime.now(timezone.utc).isoformat()
        db_del_tr(pdir, tr_id, now)

        orig_selected = tr.get("selected")
        if isinstance(orig_selected, list):
            orig_selected_seg_id = orig_selected[0] if orig_selected else None
        elif isinstance(orig_selected, str):
            orig_selected_seg_id = orig_selected
        else:
            orig_selected_seg_id = None

        tr1_id = next_transition_id(pdir)
        db_add_tr(pdir, {
            "id": tr1_id, "from": tr["from"], "to": new_kf_id,
            "duration_seconds": dur1, "slots": 1, "action": tr.get("action", ""),
            "use_global_prompt": tr.get("use_global_prompt", False),
            "selected": [orig_selected_seg_id] if orig_selected_seg_id else None,
            "remap": {"method": "linear", "target_duration": dur1},
            "track_id": tr_track,
            "trim_in": orig_trim_in,
            "trim_out": split_source_offset if split_source_offset is not None else orig_trim_out,
            "source_video_duration": orig_src_dur,
        })
        tr2_id = next_transition_id(pdir)
        db_add_tr(pdir, {
            "id": tr2_id, "from": new_kf_id, "to": tr["to"],
            "duration_seconds": dur2, "slots": 1, "action": tr.get("action", ""),
            "use_global_prompt": tr.get("use_global_prompt", False),
            "selected": [orig_selected_seg_id] if orig_selected_seg_id else None,
            "remap": {"method": "linear", "target_duration": dur2},
            "track_id": tr_track,
            "trim_in": split_source_offset if split_source_offset is not None else orig_trim_in,
            "trim_out": orig_trim_out,
            "source_video_duration": orig_src_dur,
        })

        n1 = _clone_tc(pdir, source_transition_id=tr_id,
                        target_transition_id=tr1_id, new_source="split-inherit")
        n2 = _clone_tc(pdir, source_transition_id=tr_id,
                        target_transition_id=tr2_id, new_source="split-inherit")
        _log(f"  Created {new_kf_id}, {tr1_id} ({dur1}s), {tr2_id} ({dur2}s); cloned {n1}/{n2} junction rows")

        sel_video = pdir / "selected_transitions" / f"{tr_id}_slot_0.mp4"
        if sel_video.exists() and split_source_offset is not None:
            sel_dir = pdir / "selected_transitions"
            sel_dir.mkdir(parents=True, exist_ok=True)
            shutil.copy2(str(sel_video), str(sel_dir / f"{tr1_id}_slot_0.mp4"))
            shutil.copy2(str(sel_video), str(sel_dir / f"{tr2_id}_slot_0.mp4"))

            sel_kf_dir = pdir / "selected_keyframes"
            sel_kf_dir.mkdir(parents=True, exist_ok=True)
            sp.run(["ffmpeg", "-y", "-ss", f"{split_source_offset:.3f}", "-i", str(sel_video),
                    "-vframes", "1", "-q:v", "2",
                    str(sel_kf_dir / f"{new_kf_id}.png")], capture_output=True, timeout=10)
            _log(f"  Extracted keyframe frame at source_offset={split_source_offset:.2f}s -> {new_kf_id}.png")
            cand_dir = pdir / "keyframe_candidates" / "candidates" / f"section_{new_kf_id}"
            cand_dir.mkdir(parents=True, exist_ok=True)
            shutil.copy2(str(sel_kf_dir / f"{new_kf_id}.png"), str(cand_dir / "v1.png"))
            _upd_kf(pdir, new_kf_id, selected=1,
                     candidates=[f"keyframe_candidates/candidates/section_{new_kf_id}/v1.png"])

        return {
            "success": True, "keyframeId": new_kf_id,
            "transition1": tr1_id, "transition2": tr2_id,
        }
    except ApiError:
        raise
    except Exception as e:
        _log(f"  FAILED: {e}")
        import traceback
        traceback.print_exc()
        raise ApiError("INTERNAL_ERROR", str(e), status_code=500)


# New REST surface: batch-delete-transitions was chat-only in legacy.
@router.post(
    "/api/projects/{name}/batch-delete-transitions",
    operation_id="batch_delete_transitions",
    dependencies=[Depends(project_lock), Depends(project_dir)],
)
def batch_delete_transitions(
    name: str,
    pdir: Path = Depends(project_dir),
    body: BatchDeleteTransitionsBody = Body(...),
) -> dict:
    """Batch soft-delete transitions -- mirrors ``chat._exec_batch_delete_transitions``."""
    from scenecraft.chat import _exec_batch_delete_transitions

    result = _exec_batch_delete_transitions(pdir, body.model_dump())
    if isinstance(result, dict) and "error" in result:
        raise ApiError("BAD_REQUEST", result["error"], status_code=400)
    return result


# ---------------------------------------------------------------------------
# Action / remap / generate / enhance / style / label (non-structural)
# ---------------------------------------------------------------------------


@router.post(
    "/api/projects/{name}/update-transition-action",
    operation_id="update_transition_action",
    dependencies=[Depends(project_dir)],
)
def update_transition_action(
    name: str,
    body: UpdateTransitionActionBody,
    pdir: Path = Depends(project_dir),
) -> dict:
    """Update a transition's action prompt and related fields."""
    tr_id = body.transitionId
    action = body.action
    use_global = body.useGlobalPrompt

    _log(f"update-transition-action: {name} {tr_id} action={repr(action[:50] if action else None)}")

    try:
        from scenecraft.db import update_transition, get_transition
        tr = get_transition(pdir, tr_id)
        if not tr:
            raise ApiError("NOT_FOUND", f"Transition {tr_id} not found", status_code=404)

        include_section_desc = body.includeSectionDesc
        negative_prompt = body.negativePrompt
        seed = body.seed
        ingredients = body.ingredients
        updates: dict[str, Any] = {}
        if action is not None:
            updates["action"] = action
        if use_global is not None:
            updates["use_global_prompt"] = use_global
        if include_section_desc is not None:
            updates["include_section_desc"] = include_section_desc
        if negative_prompt is not None:
            updates["negative_prompt"] = negative_prompt
        if seed is not None:
            updates["seed"] = seed
        if ingredients is not None:
            updates["ingredients"] = ingredients
        if updates:
            update_transition(pdir, tr_id, **updates)

        return {"success": True}
    except ApiError:
        raise
    except Exception as e:
        raise ApiError("INTERNAL_ERROR", str(e), status_code=500)


@router.post(
    "/api/projects/{name}/update-transition-remap",
    operation_id="update_transition_remap",
    dependencies=[Depends(project_dir)],
)
def update_transition_remap(
    name: str,
    body: UpdateTransitionRemapBody,
    pdir: Path = Depends(project_dir),
) -> dict:
    """Update a transition's remap/duration."""
    tr_id = body.transitionId
    target_duration = body.targetDuration
    method = body.method
    curve_points = body.curvePoints

    try:
        from scenecraft.db import undo_begin as _ub_remap
        _ub_remap(pdir, f"Update transition remap {tr_id}")
        _log(f"update-transition-remap: {tr_id} method={method}")
        from scenecraft.db import get_transition, update_transition
        tr = get_transition(pdir, tr_id)
        if not tr:
            raise ApiError("NOT_FOUND", f"Transition {tr_id} not found", status_code=404)

        remap = tr.get("remap", {"method": "linear", "target_duration": 0})
        if target_duration is not None:
            remap["target_duration"] = target_duration
        if method is not None:
            remap["method"] = method
        if curve_points is not None:
            remap["curve_points"] = curve_points
        elif method == "linear" and "curve_points" in remap:
            del remap["curve_points"]

        update_transition(pdir, tr_id, remap=remap)
        return {"success": True, "transitionId": tr_id, "remap": remap}
    except ApiError:
        raise
    except Exception as e:
        raise ApiError("INTERNAL_ERROR", str(e), status_code=500)


@router.post(
    "/api/projects/{name}/generate-transition-action",
    operation_id="generate_transition_action",
    dependencies=[Depends(project_dir)],
)
def generate_transition_action(
    name: str,
    body: GenerateTransitionActionBody,
    pdir: Path = Depends(project_dir),
) -> dict:
    """LLM-generate action for a single transition."""
    tr_id = body.transitionId
    section_context = body.sectionContext

    _log(f"generate-transition-action: {name} {tr_id} (section context: {'yes' if section_context else 'no'})")

    try:
        import base64
        import os
        from scenecraft.db import get_transition, get_keyframe, get_meta, update_transition

        tr = get_transition(pdir, tr_id)
        if not tr:
            raise ApiError("NOT_FOUND", f"Transition {tr_id} not found", status_code=404)

        from_kf = get_keyframe(pdir, tr["from"])
        to_kf = get_keyframe(pdir, tr["to"])
        if not from_kf or not to_kf:
            raise ApiError("BAD_REQUEST", f"Keyframes {tr['from']} or {tr['to']} not found", status_code=400)
        selected_dir = pdir / "selected_keyframes"
        from_img = selected_dir / f"{tr['from']}.png"
        to_img = selected_dir / f"{tr['to']}.png"

        if not from_img.exists() or not to_img.exists():
            _log(f"  Missing images: from={from_img.exists()} to={to_img.exists()}")
            raise ApiError("BAD_REQUEST",
                f"Selected keyframe images not found -- from:{from_img.exists()} to:{to_img.exists()}",
                status_code=400)

        api_key = os.environ.get("ANTHROPIC_API_KEY")
        if not api_key:
            raise ApiError("INTERNAL_ERROR", "ANTHROPIC_API_KEY not set", status_code=500)

        _log(f"  Calling Claude for {tr_id} ({tr['from']} -> {tr['to']})...")
        from anthropic import Anthropic
        client = Anthropic(api_key=api_key)

        from_b64 = base64.b64encode(from_img.read_bytes()).decode()
        to_b64 = base64.b64encode(to_img.read_bytes()).decode()
        from_ctx = from_kf.get("context") or {}
        to_ctx = to_kf.get("context") or {}
        meta = get_meta(pdir)
        master_prompt = meta.get("prompt", "")
        master_context = f"Overall creative direction: {master_prompt}\n\n" if master_prompt else ""

        n_slots = tr.get("slots", 1)
        selected_slot_kf_dir = pdir / "selected_slot_keyframes"

        section_text = f"\n\nMusical context for this section:\n{section_context}\n" if section_context else ""

        if n_slots <= 1:
            user_content = [
                {"type": "text", "text": f"You are a visual effects director for a music video. {master_context}Describe the ideal visual transition between these two keyframes.{section_text}\n\n"},
                {"type": "text", "text": f"FROM keyframe ({tr['from']}):\n"
                    f"  Timestamp: {from_kf['timestamp']}\n"
                    f"  Mood: {from_ctx.get('mood', 'unknown')}\n"
                    f"  Energy: {from_ctx.get('energy', 'unknown')}\n"
                    f"  Instruments: {', '.join(from_ctx.get('instruments', []))}\n"
                    f"  Visual direction: {from_ctx.get('visual_direction', '')}\n\n"},
                {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": from_b64}},
                {"type": "text", "text": f"\nTO keyframe ({tr['to']}):\n"
                    f"  Timestamp: {to_kf['timestamp']}\n"
                    f"  Mood: {to_ctx.get('mood', 'unknown')}\n"
                    f"  Energy: {to_ctx.get('energy', 'unknown')}\n"
                    f"  Instruments: {', '.join(to_ctx.get('instruments', []))}\n"
                    f"  Visual direction: {to_ctx.get('visual_direction', '')}\n\n"},
                {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": to_b64}},
                {"type": "text", "text": f"\nTransition duration: {tr['duration_seconds']}s.\n\n"
                    "Write a concise cinematic transition description (1-3 sentences) that describes the visual journey "
                    "from the first image to the second, considering the musical context. "
                    "Focus on motion, transformation, and mood shift. "
                    "This will be used as a prompt for Veo video generation.\n\n"
                    "CRITICAL: Do NOT include any text, titles, words, letters, numbers, subtitles, captions, "
                    "or typography in your description. Veo will render any mentioned text literally on screen. "
                    "Describe only visual imagery, motion, color, and light -- never text content.\n\n"
                    "Reply with ONLY the transition description, no preamble."},
            ]

            response = client.messages.create(
                model="claude-sonnet-4-20250514",
                max_tokens=300,
                messages=[{"role": "user", "content": user_content}],
            )

            action = response.content[0].text.strip()
            tr["action"] = action
            _log(f"  Generated action: {action[:80]}...")
        else:
            chain_images = [from_img]
            for s in range(n_slots - 1):
                slot_kf_path = selected_slot_kf_dir / f"{tr_id}_slot_{s}.png"
                chain_images.append(slot_kf_path if slot_kf_path.exists() else None)
            chain_images.append(to_img)

            slot_actions = []
            slot_duration = tr["duration_seconds"] / n_slots
            for s in range(n_slots):
                start_img_path = chain_images[s]
                end_img_path = chain_images[s + 1]
                if not start_img_path or not end_img_path or not start_img_path.exists() or not end_img_path.exists():
                    slot_actions.append(f"Smooth cinematic transition (slot {s})")
                    continue

                s_b64 = base64.b64encode(start_img_path.read_bytes()).decode()
                e_b64 = base64.b64encode(end_img_path.read_bytes()).decode()

                user_content = [
                    {"type": "text", "text": f"You are a visual effects director for a music video. {master_context}"
                        f"This is slot {s + 1} of {n_slots} in a multi-slot transition from {tr['from']} to {tr['to']}.\n\n"},
                    {"type": "text", "text": "START frame for this slot:\n"},
                    {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": s_b64}},
                    {"type": "text", "text": "\nEND frame for this slot:\n"},
                    {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": e_b64}},
                    {"type": "text", "text": f"\nSlot duration: {slot_duration:.1f}s.\n\n"
                        "Write a concise cinematic description (1-3 sentences) of what happens visually during this slot. "
                        "The start and end frames may look similar -- describe the motion, energy, and subtle transformations "
                        "that should occur between them. Focus on camera movement, lighting shifts, and particle/element behavior. "
                        "This will be used as a prompt for Veo video generation.\n\n"
                        "CRITICAL: Do NOT include any text, titles, words, letters, numbers, subtitles, captions, "
                        "or typography in your description. Veo will render any mentioned text literally on screen. "
                        "Describe only visual imagery, motion, color, and light -- never text content.\n\n"
                        "Reply with ONLY the description, no preamble."},
                ]

                response = client.messages.create(
                    model="claude-sonnet-4-20250514",
                    max_tokens=300,
                    messages=[{"role": "user", "content": user_content}],
                )
                slot_actions.append(response.content[0].text.strip())

            tr["slot_actions"] = slot_actions
            if slot_actions:
                tr["action"] = slot_actions[0]

        update_transition(pdir, tr_id, action=tr.get("action", ""))

        _log(f"  Saved action for {tr_id}")
        return {"success": True, "action": tr.get("action", ""), "slotActions": tr.get("slot_actions", [])}
    except ApiError:
        raise
    except Exception as e:
        _log(f"  FAILED: {e}")
        import traceback
        traceback.print_exc()
        raise ApiError("INTERNAL_ERROR", str(e), status_code=500)


@router.post(
    "/api/projects/{name}/enhance-transition-action",
    operation_id="enhance_transition_action",
    dependencies=[Depends(project_dir)],
)
def enhance_transition_action(
    name: str,
    body: EnhanceTransitionActionBody,
    pdir: Path = Depends(project_dir),
) -> dict:
    """Enhance an existing action prompt to be more descriptive."""
    tr_id = body.transitionId
    current_action = body.action
    section_context = body.sectionContext
    if not current_action:
        raise ApiError("BAD_REQUEST", "Missing 'transitionId' or 'action'", status_code=400)

    try:
        _log(f"enhance-transition-action: {tr_id}")
        import base64
        import os
        from scenecraft.db import get_transition

        tr = get_transition(pdir, tr_id)
        if not tr:
            raise ApiError("NOT_FOUND", f"Transition {tr_id} not found", status_code=404)

        api_key = os.environ.get("ANTHROPIC_API_KEY")
        if not api_key:
            raise ApiError("INTERNAL_ERROR", "ANTHROPIC_API_KEY not set", status_code=500)

        from_img = pdir / "selected_keyframes" / f"{tr['from']}.png"
        to_img = pdir / "selected_keyframes" / f"{tr['to']}.png"

        section_text = f"\n\nMusical context for this section:\n{section_context}\n" if section_context else ""
        user_content = [
            {"type": "text", "text":
                "You are a visual effects director enhancing a transition prompt for Veo video generation. "
                "Take the user's existing prompt and make it more vivid, specific, and cinematic. "
                "Add details about camera movement, lighting, particle effects, color shifts, and timing. "
                "Keep the core intent but make it significantly more descriptive for AI video generation.\n\n"
                f"Current prompt: \"{current_action}\"\n\n"
                f"{section_text}"},
        ]

        if from_img.exists() and to_img.exists():
            from_b64 = base64.b64encode(from_img.read_bytes()).decode()
            to_b64 = base64.b64encode(to_img.read_bytes()).decode()
            user_content.extend([
                {"type": "text", "text": "FROM keyframe:\n"},
                {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": from_b64}},
                {"type": "text", "text": "\nTO keyframe:\n"},
                {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": to_b64}},
                {"type": "text", "text": "\nUse these images to inform your enhancement -- reference specific visual elements you see.\n\n"},
            ])

        user_content.append({"type": "text", "text":
            "CRITICAL: Do NOT include any text, titles, words, letters, numbers, subtitles, captions, "
            "or typography in your description. Veo will render any mentioned text literally on screen. "
            "Describe only visual imagery, motion, color, and light -- never text content.\n\n"
            "Reply with ONLY the enhanced prompt, no preamble or explanation. "
            "Keep it to 2-4 sentences."})

        from anthropic import Anthropic
        client = Anthropic(api_key=api_key)
        response = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=400,
            messages=[{"role": "user", "content": user_content}],
        )

        enhanced = response.content[0].text.strip()
        return {"success": True, "action": enhanced}
    except ApiError:
        raise
    except Exception as e:
        raise ApiError("INTERNAL_ERROR", str(e), status_code=500)


@router.post(
    "/api/projects/{name}/update-transition-style",
    operation_id="update_transition_style",
    dependencies=[Depends(project_dir)],
)
def update_transition_style(
    name: str,
    pdir: Path = Depends(project_dir),
    body: UpdateTransitionStyleBody = Body(...),
) -> dict:
    """Update visual style properties on a transition."""
    from scenecraft.db import undo_begin, update_transition

    undo_begin(pdir, f"Update transition style {body.transitionId}")

    _FIELD_MAP: list[tuple[str, str]] = [
        ("blendMode", "blend_mode"),
        ("opacity", "opacity"),
        ("opacityCurve", "opacity_curve"),
        ("redCurve", "red_curve"),
        ("greenCurve", "green_curve"),
        ("blueCurve", "blue_curve"),
        ("blackCurve", "black_curve"),
        ("hueShiftCurve", "hue_shift_curve"),
        ("saturationCurve", "saturation_curve"),
        ("invertCurve", "invert_curve"),
        ("brightnessCurve", "brightness_curve"),
        ("contrastCurve", "contrast_curve"),
        ("exposureCurve", "exposure_curve"),
        ("maskCenterX", "mask_center_x"),
        ("maskCenterY", "mask_center_y"),
        ("maskRadius", "mask_radius"),
        ("maskFeather", "mask_feather"),
        ("transformX", "transform_x"),
        ("transformY", "transform_y"),
        ("transformXCurve", "transform_x_curve"),
        ("transformYCurve", "transform_y_curve"),
        ("transformZCurve", "transform_z_curve"),
        ("chromaKey", "chroma_key"),
        ("anchorX", "anchor_x"),
        ("anchorY", "anchor_y"),
    ]

    fields: dict[str, Any] = {}
    body_dict = body.model_dump(exclude_unset=True)
    for camel, snake in _FIELD_MAP:
        if camel in body_dict:
            fields[snake] = body_dict[camel]

    if "isAdjustment" in body_dict:
        fields["is_adjustment"] = int(body_dict["isAdjustment"])
    if "hidden" in body_dict:
        fields["hidden"] = body_dict["hidden"]

    tr_id = body.transitionId
    _log(f"update-transition-style: {tr_id} {fields}")
    update_transition(pdir, tr_id, **fields)
    return {"success": True}


@router.post(
    "/api/projects/{name}/update-transition-label",
    operation_id="update_transition_label",
    dependencies=[Depends(project_dir)],
)
def update_transition_label(
    name: str,
    pdir: Path = Depends(project_dir),
    body: UpdateTransitionLabelBody = Body(...),
) -> dict:
    """Update label, label_color, and optional tags on a transition."""
    from scenecraft.db import update_transition

    tr_id = body.transitionId
    fields: dict[str, Any] = {
        "label": body.label if body.label is not None else "",
        "label_color": body.labelColor if body.labelColor is not None else "",
    }
    if body.tags is not None:
        fields["tags"] = body.tags

    _log(f"update-transition-label: {tr_id} label={fields.get('label', '')!r} tags={body.tags}")
    update_transition(pdir, tr_id, **fields)
    return {"success": True}


# ---------------------------------------------------------------------------
# Copy / duplicate / generate (non-structural)
# ---------------------------------------------------------------------------


@router.post(
    "/api/projects/{name}/copy-transition-style",
    operation_id="copy_transition_style",
    dependencies=[Depends(project_dir)],
)
def copy_transition_style(
    name: str,
    pdir: Path = Depends(project_dir),
    body: CopyTransitionStyleBody = Body(...),
) -> dict:
    """Copy all style fields + effects from source transition to target."""
    from scenecraft.db import (
        add_transition_effect,
        delete_transition_effect,
        get_transition,
        get_transition_effects,
        update_transition,
    )

    source_id = body.sourceId
    target_id = body.targetId

    src = get_transition(pdir, source_id)
    if not src:
        raise ApiError("NOT_FOUND", f"Source {source_id} not found", status_code=404)

    style_keys = (
        "blend_mode", "opacity", "opacity_curve", "red_curve", "green_curve",
        "blue_curve", "black_curve", "hue_shift_curve", "saturation_curve",
        "invert_curve", "brightness_curve", "contrast_curve", "exposure_curve",
        "chroma_key", "is_adjustment", "hidden",
        "mask_center_x", "mask_center_y", "mask_radius", "mask_feather",
        "transform_x", "transform_y", "transform_x_curve", "transform_y_curve",
        "transform_z_curve", "anchor_x", "anchor_y",
    )
    style_fields: dict[str, Any] = {}
    for key in style_keys:
        style_fields[key] = src.get(key)
    if style_fields:
        update_transition(pdir, target_id, **style_fields)

    existing_fx = get_transition_effects(pdir, target_id)
    for fx in existing_fx:
        delete_transition_effect(pdir, fx["id"])
    source_fx = get_transition_effects(pdir, source_id)
    for fx in source_fx:
        add_transition_effect(pdir, target_id, fx["type"], fx.get("params"))

    _log(f"copy-transition-style: {source_id} -> {target_id} ({len(style_fields)} fields, {len(source_fx)} effects)")
    return {"success": True}


@router.post(
    "/api/projects/{name}/duplicate-transition-video",
    operation_id="duplicate_transition_video",
    dependencies=[Depends(project_dir)],
)
def duplicate_transition_video(
    name: str,
    pdir: Path = Depends(project_dir),
    body: DuplicateTransitionVideoBody = Body(...),
) -> dict:
    """Copy selected video, pool candidates, action, and selected fields from source to target."""
    from scenecraft.db import (
        clone_tr_candidates,
        get_transition,
        update_transition,
    )

    source_id = body.sourceId
    target_id = body.targetId

    src_sel = pdir / "selected_transitions" / f"{source_id}_slot_0.mp4"
    if src_sel.exists():
        dst_sel = pdir / "selected_transitions" / f"{target_id}_slot_0.mp4"
        dst_sel.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(str(src_sel), str(dst_sel))

    clone_tr_candidates(
        pdir,
        source_transition_id=source_id,
        target_transition_id=target_id,
        new_source="cross-tr-copy",
    )

    src_tr = get_transition(pdir, source_id)
    if src_tr:
        updates: dict[str, Any] = {}
        if src_tr.get("selected"):
            updates["selected"] = src_tr["selected"]
        if src_tr.get("action"):
            updates["action"] = src_tr["action"]
        if updates:
            update_transition(pdir, target_id, **updates)

    dst_tr = get_transition(pdir, target_id)
    if dst_tr and dst_tr.get("from"):
        from_kf_id = dst_tr["from"]
        sel_video = pdir / "selected_transitions" / f"{target_id}_slot_0.mp4"
        if sel_video.exists():
            def _extract() -> None:
                try:
                    sel_kf_dir = pdir / "selected_keyframes"
                    sel_kf_dir.mkdir(parents=True, exist_ok=True)
                    sp.run(
                        ["ffmpeg", "-y", "-i", str(sel_video),
                         "-vframes", "1", "-q:v", "2",
                         str(sel_kf_dir / f"{from_kf_id}.png")],
                        capture_output=True, timeout=10,
                    )
                except Exception:
                    pass

            threading.Thread(target=_extract, daemon=True).start()

    _log(f"duplicate-transition-video: {source_id} -> {target_id}")

    audio_link: dict[str, Any] | None = None
    try:
        from scenecraft.audio.linking import link_audio_for_transition
        audio_link = link_audio_for_transition(pdir, target_id, replace=True)
    except Exception as e:
        _log(f"  audio auto-link failed (non-fatal): {e}")
        audio_link = {"status": "error", "transition_id": target_id, "reason": str(e)}

    return {"success": True, "audioLink": audio_link}


@router.post(
    "/api/projects/{name}/generate-transition-candidates",
    operation_id="generate_transition_candidates",
    dependencies=[Depends(project_dir)],
)
def generate_transition_candidates(
    name: str,
    body: GenerateTransitionCandidatesBody,
    pdir: Path = Depends(project_dir),
) -> dict:
    """Async Veo generation with WebSocket progress."""
    tr_id = body.transitionId
    count = body.count
    slot_index = body.slotIndex
    duration = body.duration
    use_next_tr_frame = body.useNextTransitionFrame
    no_end_frame = body.noEndFrame
    generate_audio = body.generateAudio
    req_ingredients = body.ingredients
    req_negative_prompt = body.negativePrompt
    req_seed = body.seed

    _log(f"[generate-transition-candidates] tr={tr_id} count={count} duration={duration} "
         f"useNextTrFrame={use_next_tr_frame} noEndFrame={no_end_frame} generateAudio={generate_audio} "
         f"ingredients={len(req_ingredients) if req_ingredients else 0} negPrompt={bool(req_negative_prompt)} seed={req_seed}")

    from scenecraft.db import get_transition, get_meta
    tr = get_transition(pdir, tr_id)
    if not tr:
        raise ApiError("NOT_FOUND", f"Transition {tr_id} not found", status_code=404)

    meta = get_meta(pdir)
    motion_prompt = meta.get("motionPrompt") or meta.get("motion_prompt") or ""
    max_seconds = duration or meta.get("transition_max_seconds") or 8

    from_kf_id = tr["from"]
    to_kf_id = tr["to"]
    n_slots = tr.get("slots", 1)
    tr_duration = tr.get("duration_seconds", 0)
    action = tr.get("action") or "Smooth cinematic transition"

    selected_kf_dir = pdir / "selected_keyframes"
    start_img = str(selected_kf_dir / f"{from_kf_id}.png")
    end_img = str(selected_kf_dir / f"{to_kf_id}.png")

    if not Path(start_img).exists():
        raise ApiError("BAD_REQUEST", f"Start keyframe image not found: {from_kf_id}", status_code=400)

    if use_next_tr_frame:
        from scenecraft.db import get_transitions as _get_all_trs
        all_trs = _get_all_trs(pdir)
        next_tr = next((t for t in all_trs if t["from"] == to_kf_id and t.get("track_id") == tr.get("track_id")), None)
        if next_tr:
            next_sel_video = pdir / "selected_transitions" / f"{next_tr['id']}_slot_0.mp4"
            if next_sel_video.exists():
                import subprocess as _sp
                extracted = pdir / "selected_keyframes" / f"_next_tr_start_{tr_id}.png"
                extracted.parent.mkdir(parents=True, exist_ok=True)
                _sp.run(["ffmpeg", "-y", "-i", str(next_sel_video), "-vframes", "1", "-q:v", "2", str(extracted)],
                        capture_output=True, timeout=10)
                if extracted.exists():
                    end_img = str(extracted)
                    _log(f"  useNextTransitionFrame: using first frame of {next_tr['id']} as end image")

    if not no_end_frame and not Path(end_img).exists():
        raise ApiError("BAD_REQUEST", f"End keyframe image not found: {to_kf_id}", status_code=400)

    from scenecraft.db import get_tr_candidates as _db_get_tr_cands
    existing_count = 0
    if slot_index is not None:
        existing_count = len(_db_get_tr_cands(pdir, tr_id, slot_index))
    else:
        for si in range(n_slots):
            existing_count = max(existing_count, len(_db_get_tr_cands(pdir, tr_id, si)))

    use_global = tr.get("use_global_prompt", True)
    prompt = f"{action}. Camera and motion style: {motion_prompt}" if use_global and motion_prompt else action
    if duration:
        slot_duration = duration
    else:
        slot_duration = min(max_seconds, tr_duration / n_slots) if tr_duration > 0 else max_seconds

    ingredient_paths_raw = req_ingredients if req_ingredients else tr.get("ingredients", [])
    ingredient_paths = [str(pdir / p) for p in ingredient_paths_raw if p] if ingredient_paths_raw else None
    if ingredient_paths:
        ingredient_paths = [p for p in ingredient_paths if Path(p).exists()]
        if not ingredient_paths:
            ingredient_paths = None

    negative_prompt = req_negative_prompt if req_negative_prompt else tr.get("negativePrompt", "") or None
    veo_seed = req_seed if req_seed is not None else tr.get("seed")

    _log(f"  veo: {tr_id} {from_kf_id}->{to_kf_id} prompt={prompt[:60]!r} dur={slot_duration}s "
         f"count={count} existing={existing_count} ingredients={len(ingredient_paths) if ingredient_paths else 0} "
         f"negPrompt={bool(negative_prompt)} seed={veo_seed}")

    from scenecraft.ws_server import job_manager
    job_id = job_manager.create_job("transition_candidates", total=count, meta={"transitionId": tr_id, "project": name})

    vid_backend = _get_video_backend(pdir)

    # Capture closure variables for the background thread
    _pdir = pdir
    _n_slots = n_slots
    _slot_index = slot_index
    _start_img = start_img
    _end_img = end_img
    _no_end_frame = no_end_frame
    _generate_audio = generate_audio
    _ingredient_paths = ingredient_paths
    _negative_prompt = negative_prompt
    _veo_seed = veo_seed
    _slot_duration = slot_duration
    _prompt = prompt
    _count = count
    _from_kf_id = from_kf_id
    _to_kf_id = to_kf_id
    _motion_prompt = motion_prompt
    _action = action
    _use_global = use_global
    _ingredient_paths_raw = ingredient_paths_raw
    _use_next_tr_frame = use_next_tr_frame

    def _run():
        try:
            from scenecraft.render.google_video import GoogleVideoClient, PromptRejectedError
            from concurrent.futures import ThreadPoolExecutor, as_completed

            if vid_backend.startswith("runway"):
                from scenecraft.render.google_video import RunwayVideoClient
                _, _, runway_model = vid_backend.partition("/")
                client = RunwayVideoClient(model=runway_model or "veo3.1_fast")
            else:
                client = GoogleVideoClient(vertex=True)
            job_manager.update_progress(job_id, 0, f"Starting video generation ({vid_backend})...")

            import uuid as _uuid
            pool_segs_dir = _pdir / "pool" / "segments"
            pool_segs_dir.mkdir(parents=True, exist_ok=True)

            gen_jobs = []
            for si in range(_n_slots):
                if _slot_index is not None and si != _slot_index:
                    continue
                s_img = _start_img if si == 0 else str(_pdir / "selected_slot_keyframes" / f"{tr_id}_slot_{si - 1}.png")
                e_img = _end_img if si == _n_slots - 1 else str(_pdir / "selected_slot_keyframes" / f"{tr_id}_slot_{si}.png")
                if not Path(s_img).exists():
                    s_img = _start_img
                if not Path(e_img).exists():
                    e_img = _end_img
                for _ in range(_count):
                    seg_uuid = _uuid.uuid4().hex
                    pool_name = f"cand_{seg_uuid}.mp4"
                    output = str(pool_segs_dir / pool_name)
                    gen_jobs.append({
                        "slot": si, "start": s_img, "end": e_img,
                        "output": output, "seg_uuid": seg_uuid,
                        "pool_path": f"pool/segments/{pool_name}",
                    })

            if not gen_jobs:
                job_manager.complete_job(job_id, {"transitionId": tr_id, "candidates": {}})
                return

            _log(f"[job {job_id}] Generating {len(gen_jobs)} Veo clips for {tr_id}...")
            completed_count = [0]
            rejected = []

            gen_params_template = {
                "provider": vid_backend.split("/")[0] if "/" in vid_backend else vid_backend,
                "model": vid_backend.split("/")[1] if "/" in vid_backend else None,
                "prompt": _prompt,
                "negative_prompt": _negative_prompt,
                "seed": _veo_seed,
                "ingredients": {
                    "from_keyframe_id": _from_kf_id,
                    "to_keyframe_id": _to_kf_id,
                    "motion_prompt": _motion_prompt if _use_global else "",
                    "action": _action,
                    "ingredient_paths": _ingredient_paths_raw if _ingredient_paths_raw else [],
                },
                "params": {
                    "duration_target": _slot_duration,
                    "generate_audio": _generate_audio,
                    "no_end_frame": _no_end_frame,
                    "use_next_tr_frame": _use_next_tr_frame,
                },
            }

            auth_user = "local"

            def _record_candidate(j):
                try:
                    output = Path(j["output"])
                    if not output.exists():
                        _log(f"    warning: expected output missing: {output}")
                        return
                    import subprocess as _sp
                    dur = None
                    try:
                        r = _sp.run(
                            ["ffprobe", "-v", "error", "-show_entries",
                             "format=duration", "-of", "csv=p=0", str(output)],
                            capture_output=True, text=True, timeout=5,
                        )
                        if r.returncode == 0 and r.stdout.strip():
                            dur = float(r.stdout.strip())
                    except Exception:
                        pass
                    byte_size = output.stat().st_size

                    from scenecraft.db import get_db as _get_db, _now_iso, add_tr_candidate as _add_tc
                    _conn = _get_db(_pdir)
                    _conn.execute(
                        """INSERT INTO pool_segments
                           (id, pool_path, kind, created_by, original_filename, original_filepath,
                            label, generation_params, created_at, duration_seconds, width, height, byte_size)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (j["seg_uuid"], j["pool_path"], "generated", auth_user, None, None,
                         "", json.dumps(gen_params_template), _now_iso(), dur, None, None, byte_size),
                    )
                    _conn.commit()
                    _add_tc(_pdir, transition_id=tr_id, slot=j["slot"],
                            pool_segment_id=j["seg_uuid"], source="generated")
                except Exception as e:
                    _log(f"    warning: failed to record pool candidate for {j.get('pool_path')}: {e}")

            def _gen(j):
                try:
                    if _no_end_frame:
                        client.generate_video_from_image(
                            image_path=j["start"], prompt=_prompt,
                            output_path=j["output"], duration_seconds=int(_slot_duration),
                            generate_audio=_generate_audio,
                            ingredients=_ingredient_paths,
                            negative_prompt=_negative_prompt, seed=_veo_seed,
                            on_status=lambda msg: job_manager.update_progress(job_id, completed_count[0], msg),
                        )
                    else:
                        client.generate_video_transition(
                            start_frame_path=j["start"], end_frame_path=j["end"],
                            prompt=_prompt, output_path=j["output"],
                            duration_seconds=int(_slot_duration),
                            generate_audio=_generate_audio,
                            ingredients=_ingredient_paths,
                            negative_prompt=_negative_prompt, seed=_veo_seed,
                            on_status=lambda msg: job_manager.update_progress(job_id, completed_count[0], msg),
                        )
                    completed_count[0] += 1
                    _record_candidate(j)
                    _log(f"    {tr_id} slot_{j['slot']} {j['seg_uuid'][:8]} done")
                except PromptRejectedError as e:
                    max_rejection_retries = 5
                    succeeded = False
                    for retry_i in range(max_rejection_retries):
                        _log(f"    warning: PROMPT REJECTED (attempt {retry_i + 1}/{max_rejection_retries}): {tr_id} -- {e}")
                        job_manager.update_progress(job_id, completed_count[0], f"Prompt rejected, retrying ({retry_i + 1}/{max_rejection_retries})...")
                        import time as _time
                        _time.sleep(2)
                        try:
                            if _no_end_frame:
                                client.generate_video_from_image(
                                    image_path=j["start"], prompt=_prompt,
                                    output_path=j["output"], duration_seconds=int(_slot_duration),
                                    generate_audio=_generate_audio,
                                    ingredients=_ingredient_paths,
                                    negative_prompt=_negative_prompt, seed=_veo_seed,
                                    on_status=lambda msg: job_manager.update_progress(job_id, completed_count[0], msg),
                                )
                            else:
                                client.generate_video_transition(
                                    start_frame_path=j["start"], end_frame_path=j["end"],
                                    prompt=_prompt, output_path=j["output"],
                                    duration_seconds=int(_slot_duration),
                                    generate_audio=_generate_audio,
                                    ingredients=_ingredient_paths,
                                    negative_prompt=_negative_prompt, seed=_veo_seed,
                                    on_status=lambda msg: job_manager.update_progress(job_id, completed_count[0], msg),
                                )
                            completed_count[0] += 1
                            _record_candidate(j)
                            succeeded = True
                            break
                        except PromptRejectedError:
                            continue
                        except Exception:
                            break
                    if not succeeded:
                        rejected.append(tr_id)
                        job_manager.update_progress(job_id, completed_count[0], f"warning: {tr_id}: prompt rejected after {max_rejection_retries} attempts")
                except Exception as e:
                    _log(f"    warning: {tr_id} slot_{j['slot']} {j.get('seg_uuid', '?')[:8]} FAILED: {e}")

            with ThreadPoolExecutor(max_workers=min(len(gen_jobs), 4)) as pool:
                futures = [pool.submit(_gen, j) for j in gen_jobs]
                for f in as_completed(futures):
                    try:
                        f.result()
                    except Exception:
                        pass

            from scenecraft.db import get_tr_candidates as _db_get_tc
            candidates = {}
            for si in range(_n_slots):
                cands = _db_get_tc(_pdir, tr_id, si)
                if cands:
                    candidates[f"slot_{si}"] = [c["poolPath"] for c in cands]

            result = {"transitionId": tr_id, "candidates": candidates}
            if rejected:
                result["rejected"] = rejected
                result["rejectionMessage"] = f"Prompt rejected for {len(rejected)} variant(s) after retries"
            job_manager.complete_job(job_id, result)
        except Exception as e:
            _log(f"[job {job_id}] FAILED: {e}")
            import traceback
            traceback.print_exc()
            err = str(e)
            if "transient" in err.lower() or "None" in err:
                job_manager.fail_job(job_id, f"Veo returned empty results after retries -- try again. ({err[:80]})")
            else:
                job_manager.fail_job(job_id, err)

    import threading
    threading.Thread(target=_run, daemon=True).start()
    return {"jobId": job_id, "transitionId": tr_id}


# ---------------------------------------------------------------------------
# Link-audio -- takes ``tr_id`` as a path param
# ---------------------------------------------------------------------------


@router.post(
    "/api/projects/{name}/transitions/{tr_id}/link-audio",
    operation_id="link_transition_audio",
    dependencies=[Depends(project_dir)],
)
def link_transition_audio(
    name: str,
    tr_id: str,
    pdir: Path = Depends(project_dir),
    body: LinkAudioBody = Body(...),
) -> JSONResponse:
    """Extract audio from the transition's selected video, create audio_clips + links."""
    from scenecraft.audio.linking import link_audio_for_transition

    replace = bool(body.replace or body.force)
    result = link_audio_for_transition(pdir, tr_id, replace=replace)
    status_code = 200 if result["status"] in ("linked", "exists", "skipped") else 500
    return JSONResponse(status_code=status_code, content=result)


# ---------------------------------------------------------------------------
# Transition effects
# ---------------------------------------------------------------------------


@router.post(
    "/api/projects/{name}/transition-effects/add",
    operation_id="add_transition_effect",
    dependencies=[Depends(project_dir)],
)
def add_transition_effect(
    name: str,
    pdir: Path = Depends(project_dir),
    body: TransitionEffectAddBody = Body(...),
) -> dict:
    """Add a new effect to a transition."""
    from scenecraft.db import add_transition_effect as db_add_transition_effect

    tr_id = body.transitionId
    etype = body.type
    params = body.params or {}

    effect_id = db_add_transition_effect(pdir, tr_id, etype, params)
    _log(f"transition-effects/add: {tr_id} type={etype} -> {effect_id}")
    return {"success": True, "id": effect_id}


@router.post(
    "/api/projects/{name}/transition-effects/update",
    operation_id="update_transition_effect",
    dependencies=[Depends(project_dir)],
)
def update_transition_effect(
    name: str,
    pdir: Path = Depends(project_dir),
    body: TransitionEffectUpdateBody = Body(...),
) -> dict:
    """Update fields on an existing transition effect."""
    from scenecraft.db import update_transition_effect as db_update_transition_effect

    body_dict = body.model_dump()
    effect_id = body_dict.pop("id")
    _log(f"transition-effects/update: {effect_id} {body_dict}")
    db_update_transition_effect(pdir, effect_id, **body_dict)
    return {"success": True}


@router.post(
    "/api/projects/{name}/transition-effects/delete",
    operation_id="delete_transition_effect",
    dependencies=[Depends(project_dir)],
)
def delete_transition_effect(
    name: str,
    pdir: Path = Depends(project_dir),
    body: TransitionEffectDeleteBody = Body(...),
) -> dict:
    """Delete a transition effect by id."""
    from scenecraft.db import delete_transition_effect as db_delete_transition_effect

    fx_id = body.id
    _log(f"transition-effects/delete: {fx_id}")
    db_delete_transition_effect(pdir, fx_id)
    return {"success": True}


# ---------------------------------------------------------------------------
# Catch-all update-transition -- chat-tool-only in legacy
# ---------------------------------------------------------------------------


@router.post(
    "/api/projects/{name}/update-transition",
    operation_id="update_transition",
    dependencies=[Depends(project_dir)],
)
def update_transition(
    name: str,
    pdir: Path = Depends(project_dir),
    body: dict = Body(...),
) -> dict:
    from scenecraft.chat import _exec_update_transition

    tr_id = body.get("transition_id")
    if not tr_id or not isinstance(tr_id, str):
        raise ApiError("BAD_REQUEST", "Missing 'transition_id'", status_code=400)

    result = _exec_update_transition(pdir, body)
    if isinstance(result, dict) and "error" in result:
        raise ApiError("BAD_REQUEST", result["error"], status_code=400)
    return result


__all__ = ["router"]
