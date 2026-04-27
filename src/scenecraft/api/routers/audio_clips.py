"""Audio clips router (M16 T62).

Ports ``/audio-clips/*`` routes: add, add-from-pool, update, delete,
batch-ops, align-detect, list, peaks.

🔧 chat-tool alignment:
  * ``add_audio_clip`` → ``/audio-clips/add-from-pool`` (matches the chat
    tool ``_exec_add_audio_clip`` which adds from the pool, not from raw args).
  * ``apply_mix_plan`` → ``/audio-clips/batch-ops`` (today the chat tool
    calls the same batch-ops DB path internally).
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, Query, Request, Response, status

from scenecraft.api.deps import current_user, project_dir
from scenecraft.api.errors import ApiError
from scenecraft.api.models.audio import (
    AddAudioClipBody,
    AddAudioClipFromPoolBody,
    AudioClipAlignDetectBody,
    AudioClipsBatchOpsBody,
    DeleteAudioClipBody,
    UpdateAudioClipBody,
)


router = APIRouter(prefix="/api/projects", tags=["audio"], dependencies=[Depends(current_user)])


def _classify_media_type(path: str) -> str:
    """Tiny mirror of ``api_server._classify_media_type`` for audio.

    Kept inline — the one in ``api_server.py`` is a module-level function that
    we don't want to import during the parallel-port phase because it pulls in
    other module-load-time side-effects. Audio extensions are the only case
    this endpoint cares about; it's a static enumeration.
    """
    lower = path.lower()
    for ext in (".wav", ".mp3", ".m4a", ".flac", ".ogg", ".opus"):
        if lower.endswith(ext):
            return "audio"
    return "video"


# ---------------------------------------------------------------------------
# GET /audio-clips + /audio-clips/{id}/peaks
# ---------------------------------------------------------------------------


@router.get("/{name}/audio-clips", operation_id="list_audio_clips")
async def list_audio_clips(
    name: str,
    trackId: str | None = Query(default=None),
    pd: Path = Depends(project_dir),
) -> dict:
    from scenecraft.db import get_audio_clips

    return {"audioClips": get_audio_clips(pd, trackId)}


@router.get(
    "/{name}/audio-clips/{clip_id}/peaks",
    operation_id="get_audio_clip_peaks",
    responses={200: {"content": {"application/octet-stream": {}}}},
)
async def get_audio_clip_peaks(
    name: str,
    clip_id: str,
    resolution: int = Query(default=400),
    pd: Path = Depends(project_dir),
) -> Response:
    from scenecraft.audio.peaks import compute_peaks
    from scenecraft.db import get_audio_clips

    clip = next((c for c in get_audio_clips(pd) if c["id"] == clip_id), None)
    if clip is None:
        raise ApiError("NOT_FOUND", f"Audio clip not found: {clip_id}", status_code=404)

    source_rel = clip.get("source_path", "")
    if not source_rel:
        raise ApiError("BAD_REQUEST", f"Audio clip has no source_path: {clip_id}", status_code=400)
    source_path = (pd / source_rel).resolve()
    try:
        source_path.relative_to(pd.resolve())
    except ValueError:
        raise ApiError("BAD_REQUEST", "source_path outside project", status_code=400)
    if not source_path.exists():
        raise ApiError("NOT_FOUND", f"Source audio missing on disk: {source_rel}", status_code=404)

    duration = float(clip.get("end_time", 0)) - float(clip.get("start_time", 0))
    source_offset = float(clip.get("source_offset", 0))
    try:
        data = compute_peaks(source_path, source_offset, duration, resolution, project_dir=pd)
    except RuntimeError as exc:
        raise ApiError("PEAKS_FAILED", str(exc), status_code=500)

    return Response(
        content=data,
        media_type="application/octet-stream",
        headers={
            "Content-Length": str(len(data)),
            "X-Peak-Resolution": str(resolution),
            "X-Peak-Duration": f"{duration:.6f}",
        },
    )


# ---------------------------------------------------------------------------
# POST /audio-clips/add
# ---------------------------------------------------------------------------


@router.post("/{name}/audio-clips/add", operation_id="add_audio_clip_core")
async def add_audio_clip_core(
    name: str,
    body: AddAudioClipBody,
    pd: Path = Depends(project_dir),
) -> dict:
    from scenecraft.db import (
        add_audio_clip as db_add_audio_clip,
        generate_id,
    )

    track_id = body.trackId
    if not track_id:
        raise ApiError("BAD_REQUEST", "Missing 'trackId'", status_code=400)

    clip_id = generate_id("audio_clip")
    clip = {
        "id": clip_id,
        "track_id": track_id,
        "source_path": body.sourcePath or "",
        "start_time": body.startTime or 0,
        "end_time": body.endTime or 0,
        "source_offset": body.sourceOffset or 0,
        "volume_curve": body.volumeCurve,
        "muted": bool(body.muted) if body.muted is not None else False,
        "remap": body.remap or {"method": "linear", "target_duration": 0},
    }
    if body.label is not None:
        clip["label"] = body.label
    db_add_audio_clip(pd, clip)
    return {"success": True, "id": clip_id}


# ---------------------------------------------------------------------------
# POST /audio-clips/add-from-pool (🔧 chat-tool: add_audio_clip)
# ---------------------------------------------------------------------------


@router.post(
    "/{name}/audio-clips/add-from-pool", operation_id="add_audio_clip",
)
async def add_audio_clip_from_pool(
    name: str,
    body: AddAudioClipFromPoolBody,
    pd: Path = Depends(project_dir),
) -> dict:
    from scenecraft.db import (
        add_audio_clip as db_add_audio_clip,
        generate_id,
        get_pool_segment,
        list_pool_segments,
    )

    if not body.trackId:
        raise ApiError("BAD_REQUEST", "Missing 'trackId'", status_code=400)
    if not body.poolSegmentId and not body.poolPath:
        raise ApiError("BAD_REQUEST", "Provide 'poolSegmentId' or 'poolPath'", status_code=400)

    start_time = float(body.startTime or 0)
    seg = None
    if body.poolSegmentId:
        seg = get_pool_segment(pd, body.poolSegmentId)
    if seg is None and body.poolPath:
        for ps in list_pool_segments(pd):
            if ps.get("poolPath") == body.poolPath:
                seg = ps
                break
    if seg is None:
        raise ApiError("NOT_FOUND", "Pool segment not found", status_code=404)
    if _classify_media_type(seg["poolPath"]) != "audio":
        raise ApiError("BAD_REQUEST", "Pool segment is not audio", status_code=400)

    duration = float(seg.get("durationSeconds") or 0)
    clip_id = generate_id("audio_clip")
    seed_label = seg.get("label") or seg.get("originalFilename") or ""
    if seed_label and "." in seed_label and not seg.get("label"):
        seed_label = seed_label.rsplit(".", 1)[0]
    clip = {
        "id": clip_id,
        "track_id": body.trackId,
        "source_path": seg["poolPath"],
        "start_time": start_time,
        "end_time": start_time + duration,
        "source_offset": 0,
        "volume_curve": None,
        "muted": False,
        "remap": {"method": "linear", "target_duration": 0},
        "label": seed_label or None,
    }
    db_add_audio_clip(pd, clip)
    return {"success": True, "id": clip_id}


# ---------------------------------------------------------------------------
# POST /audio-clips/update | delete
# ---------------------------------------------------------------------------


@router.post("/{name}/audio-clips/update", operation_id="update_audio_clip")
async def update_audio_clip(
    name: str,
    body: UpdateAudioClipBody,
    pd: Path = Depends(project_dir),
) -> dict:
    from scenecraft.db import update_audio_clip as db_update_audio_clip

    if not body.id:
        raise ApiError("BAD_REQUEST", "Missing 'id'", status_code=400)

    raw = body.model_dump(exclude_none=True, by_alias=False, exclude={"id"})
    mapping = {
        "trackId": "track_id",
        "sourcePath": "source_path",
        "startTime": "start_time",
        "endTime": "end_time",
        "sourceOffset": "source_offset",
        "volumeCurve": "volume_curve",
    }
    allowed = {
        "track_id",
        "source_path",
        "start_time",
        "end_time",
        "source_offset",
        "volume_curve",
        "muted",
        "remap",
        "label",
    }
    # Pydantic already gives us snake-case keys (populate_by_name=True,
    # by_alias=False) — but be defensive for camelCase leaks.
    mapped: dict = {}
    for k, v in raw.items():
        target = mapping.get(k, k)
        if target in allowed:
            mapped[target] = v
    db_update_audio_clip(pd, body.id, **mapped)
    return {"success": True}


@router.post("/{name}/audio-clips/delete", operation_id="delete_audio_clip")
async def delete_audio_clip(
    name: str,
    body: DeleteAudioClipBody,
    pd: Path = Depends(project_dir),
) -> dict:
    from scenecraft.db import delete_audio_clip as db_delete_audio_clip

    db_delete_audio_clip(pd, body.id)
    return {"success": True}


# ---------------------------------------------------------------------------
# POST /audio-clips/batch-ops (🔧 chat-tool: apply_mix_plan)
# ---------------------------------------------------------------------------


@router.post(
    "/{name}/audio-clips/batch-ops",
    operation_id="apply_mix_plan",
)
async def audio_clips_batch_ops(
    name: str,
    body: AudioClipsBatchOpsBody,
    pd: Path = Depends(project_dir),
) -> dict:
    from scenecraft.db import (
        add_audio_clip as _db_add_clip,
        delete_audio_clip as _db_delete_clip,
        get_audio_clips as _db_get_clips,
        undo_begin,
        update_audio_clip as _db_update_clip,
    )

    label = body.label or "audio clip batch op"
    ops = body.ops
    if not isinstance(ops, list) or not ops:
        raise ApiError("BAD_REQUEST", "ops must be a non-empty list", status_code=400)

    valid_kinds = {"trim", "split", "delete", "insert"}
    for i, op in enumerate(ops):
        if not isinstance(op, dict):
            raise ApiError("BAD_REQUEST", f"ops[{i}] must be an object", status_code=400)
        kind = op.get("op")
        if kind not in valid_kinds:
            raise ApiError("BAD_REQUEST", f"ops[{i}]: unknown op '{kind}'", status_code=400)
        if kind in ("trim", "split", "delete") and not op.get("id"):
            raise ApiError("BAD_REQUEST", f"ops[{i}]: '{kind}' requires id", status_code=400)
        if kind == "split":
            if "at" not in op or "new_id" not in op:
                raise ApiError("BAD_REQUEST", f"ops[{i}]: 'split' requires at + new_id", status_code=400)
        if kind == "insert":
            clip = op.get("clip")
            if not isinstance(clip, dict):
                raise ApiError("BAD_REQUEST", f"ops[{i}]: 'insert' requires clip object", status_code=400)
            for key in ("id", "track_id", "source_path", "start_time", "end_time"):
                if clip.get(key) is None:
                    raise ApiError("BAD_REQUEST", f"ops[{i}]: insert.clip missing {key}", status_code=400)

    undo_begin(pd, label)

    _clips_cache: dict[str, dict] = {}

    def _resolve_clip(cid: str) -> dict | None:
        if cid not in _clips_cache:
            _clips_cache.clear()
            for c in _db_get_clips(pd):
                _clips_cache[c["id"]] = c
        return _clips_cache.get(cid)

    for op in ops:
        kind = op["op"]
        if kind == "trim":
            fields: dict = {}
            if "start_time" in op and op["start_time"] is not None:
                fields["start_time"] = float(op["start_time"])
            if "end_time" in op and op["end_time"] is not None:
                fields["end_time"] = float(op["end_time"])
            if "source_offset" in op and op["source_offset"] is not None:
                fields["source_offset"] = float(op["source_offset"])
            if fields:
                _db_update_clip(pd, op["id"], **fields)
                _clips_cache.clear()
        elif kind == "delete":
            _db_delete_clip(pd, op["id"])
            _clips_cache.clear()
        elif kind == "insert":
            clip = dict(op["clip"])
            _db_add_clip(pd, clip)
            _clips_cache.clear()
        elif kind == "split":
            original = _resolve_clip(op["id"])
            if not original:
                continue
            at = float(op["at"])
            orig_start = float(original["start_time"])
            orig_end = float(original["end_time"])
            orig_src_offset = float(original.get("source_offset", 0))
            if at <= orig_start or at >= orig_end:
                continue
            _db_update_clip(pd, op["id"], end_time=at)
            right_src_offset = op.get("source_offset_right")
            if right_src_offset is None:
                right_src_offset = orig_src_offset + (at - orig_start)
            new_clip = {
                "id": op["new_id"],
                "track_id": original["track_id"],
                "source_path": original.get("source_path", ""),
                "start_time": at,
                "end_time": orig_end,
                "source_offset": float(right_src_offset),
                "volume_curve": original.get("volume_curve"),
                "muted": bool(original.get("muted", False)),
                "remap": original.get(
                    "remap", {"method": "linear", "target_duration": 0}
                ),
            }
            _db_add_clip(pd, new_clip)
            _clips_cache.clear()

    return {"success": True, "ops_applied": len(ops)}


# ---------------------------------------------------------------------------
# POST /audio-clips/align-detect
# ---------------------------------------------------------------------------


@router.post(
    "/{name}/audio-clips/align-detect",
    operation_id="align_audio_clips",
)
async def align_audio_clips(
    name: str,
    body: AudioClipAlignDetectBody,
    pd: Path = Depends(project_dir),
) -> dict:
    if not body.anchorClipId:
        raise ApiError("BAD_REQUEST", "anchorClipId + clipIds (>=2) required", status_code=400)
    if not isinstance(body.clipIds, list) or len(body.clipIds) < 2:
        raise ApiError("BAD_REQUEST", "anchorClipId + clipIds (>=2) required", status_code=400)
    if body.anchorClipId not in body.clipIds:
        raise ApiError("BAD_REQUEST", "anchorClipId must be in clipIds", status_code=400)

    try:
        from scenecraft.audio.align import detect_offsets
        from scenecraft.db import get_audio_clips

        all_clips = get_audio_clips(pd)
        by_id = {c["id"]: c for c in all_clips}
        selected: list[dict] = []
        for cid in body.clipIds:
            if cid not in by_id:
                raise ApiError("NOT_FOUND", f"Audio clip not found: {cid}", status_code=404)
            selected.append(by_id[cid])
        offsets, confidence = detect_offsets(pd, selected, body.anchorClipId)
        return {
            "anchorClipId": body.anchorClipId,
            "offsets": offsets,
            "confidence": confidence,
        }
    except FileNotFoundError as e:
        raise ApiError("SOURCE_NOT_FOUND", str(e), status_code=404)
    except ApiError:
        raise
    except Exception as e:
        raise ApiError("INTERNAL_ERROR", str(e), status_code=500)
