"""Audio + video tracks router (M16 T62).

Ports the legacy ``/tracks/*`` and ``/audio-tracks/*`` surface. Handlers
are thin wrappers over ``scenecraft.db`` helpers — no business logic lives
here, matching the legacy ``api_server.py`` pattern.

operation_id convention:
  * ``list_tracks`` / ``list_audio_tracks`` — GET
  * ``add_track`` / ``update_track`` / ``delete_track`` / ``reorder_tracks``
  * ``add_audio_track`` (🔧 chat-tool aligned) / ``update_audio_track`` /
    ``delete_audio_track`` / ``reorder_audio_tracks``
  * ``update_volume_curve`` (🔧 chat-tool aligned) — new route with no
    legacy REST equivalent (chat tool calls ``db.update_volume_curve``
    directly today; T67 needs a matching operation).

No ``project_lock`` on any of these — none of the route-tail names are in
``STRUCTURAL_ROUTES``.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends

from scenecraft.api.deps import current_user, project_dir
from scenecraft.api.errors import ApiError
from scenecraft.api.models.audio import (
    AddAudioTrackBody,
    AddTrackBody,
    DeleteAudioTrackBody,
    DeleteTrackBody,
    ReorderAudioTracksBody,
    ReorderTracksBody,
    UpdateAudioTrackBody,
    UpdateTrackBody,
    UpdateVolumeCurveBody,
)


router = APIRouter(prefix="/api/projects", tags=["audio"], dependencies=[Depends(current_user)])


# ---------------------------------------------------------------------------
# GET /tracks  +  /audio-tracks
# ---------------------------------------------------------------------------


@router.get("/{name}/tracks", operation_id="list_tracks")
async def list_tracks(name: str, pd: Path = Depends(project_dir)) -> dict:
    from scenecraft.db import get_opacity_keyframes, get_tracks

    tracks = get_tracks(pd)
    for t in tracks:
        t["opacityKeyframes"] = get_opacity_keyframes(pd, t["id"])
    return {"tracks": tracks}


@router.get("/{name}/audio-tracks", operation_id="list_audio_tracks")
async def list_audio_tracks(name: str, pd: Path = Depends(project_dir)) -> dict:
    from scenecraft.db import get_audio_clips, get_audio_tracks

    tracks = get_audio_tracks(pd)
    for t in tracks:
        t["clips"] = get_audio_clips(pd, t["id"])
    return {"audioTracks": tracks}


# ---------------------------------------------------------------------------
# POST /tracks/add|update|delete|reorder
# ---------------------------------------------------------------------------


@router.post("/{name}/tracks/add", operation_id="add_track")
async def add_track(
    name: str,
    body: AddTrackBody,
    pd: Path = Depends(project_dir),
) -> dict:
    from scenecraft.db import (
        add_track as db_add_track,
        generate_id,
        get_tracks as db_get_tracks,
    )

    existing = db_get_tracks(pd)
    track_id = generate_id("track")
    z_order = max((t["z_order"] for t in existing), default=-1) + 1
    raw = body.model_dump(exclude_none=True)
    fields = {
        k: v
        for k, v in raw.items()
        if k in ("blend_mode", "base_opacity", "muted")
    }
    db_add_track(
        pd,
        {
            "id": track_id,
            "name": raw.get("name") or f"Track {len(existing) + 1}",
            "z_order": z_order,
            **fields,
        },
    )
    return {"success": True, "id": track_id}


@router.post("/{name}/tracks/update", operation_id="update_track")
async def update_track(
    name: str,
    body: UpdateTrackBody,
    pd: Path = Depends(project_dir),
) -> dict:
    from scenecraft.db import update_track as db_update_track

    track_id = body.id
    if not track_id:
        raise ApiError("BAD_REQUEST", "Missing 'id'", status_code=400)
    raw = body.model_dump(exclude_none=True, by_alias=False, exclude={"id"})
    allowed = {
        "name",
        "blend_mode",
        "base_opacity",
        "muted",
        "z_order",
        "chroma_key",
        "hidden",
        "solo",
    }
    mapped = {k: v for k, v in raw.items() if k in allowed}
    db_update_track(pd, track_id, **mapped)
    return {"success": True}


@router.post("/{name}/tracks/delete", operation_id="delete_track")
async def delete_track(
    name: str,
    body: DeleteTrackBody,
    pd: Path = Depends(project_dir),
) -> dict:
    from scenecraft.db import delete_track as db_delete_track

    db_delete_track(pd, body.id)
    return {"success": True}


@router.post("/{name}/tracks/reorder", operation_id="reorder_tracks")
async def reorder_tracks(
    name: str,
    body: ReorderTracksBody,
    pd: Path = Depends(project_dir),
) -> dict:
    from scenecraft.db import reorder_tracks as db_reorder_tracks

    db_reorder_tracks(pd, body.trackIds)
    return {"success": True}


# ---------------------------------------------------------------------------
# POST /audio-tracks/add|update|delete|reorder (🔧 add_audio_track = chat tool)
# ---------------------------------------------------------------------------


@router.post("/{name}/audio-tracks/add", operation_id="add_audio_track")
async def add_audio_track(
    name: str,
    body: AddAudioTrackBody,
    pd: Path = Depends(project_dir),
) -> dict:
    from scenecraft.db import (
        add_audio_track as db_add_audio_track,
        generate_id,
        get_audio_tracks as db_get_audio_tracks,
    )

    existing = db_get_audio_tracks(pd)
    track_id = generate_id("audio_track")
    display_order = max((t["display_order"] for t in existing), default=-1) + 1
    raw = body.model_dump(exclude_none=True, by_alias=False)
    fields = {
        k: v
        for k, v in raw.items()
        if k in ("hidden", "muted", "solo", "volume_curve")
    }
    db_add_audio_track(
        pd,
        {
            "id": track_id,
            "name": raw.get("name") or f"Audio Track {len(existing) + 1}",
            "display_order": display_order,
            **fields,
        },
    )
    return {"success": True, "id": track_id}


@router.post("/{name}/audio-tracks/update", operation_id="update_audio_track")
async def update_audio_track(
    name: str,
    body: UpdateAudioTrackBody,
    pd: Path = Depends(project_dir),
) -> dict:
    from scenecraft.db import update_audio_track as db_update_audio_track

    if not body.id:
        raise ApiError("BAD_REQUEST", "Missing 'id'", status_code=400)
    raw = body.model_dump(exclude_none=True, by_alias=False, exclude={"id"})
    allowed = {"name", "display_order", "hidden", "muted", "solo", "volume_curve"}
    mapped = {k: v for k, v in raw.items() if k in allowed}
    db_update_audio_track(pd, body.id, **mapped)
    return {"success": True}


@router.post("/{name}/audio-tracks/delete", operation_id="delete_audio_track")
async def delete_audio_track(
    name: str,
    body: DeleteAudioTrackBody,
    pd: Path = Depends(project_dir),
) -> dict:
    from scenecraft.db import delete_audio_track as db_delete_audio_track

    db_delete_audio_track(pd, body.id)
    return {"success": True}


@router.post("/{name}/audio-tracks/reorder", operation_id="reorder_audio_tracks")
async def reorder_audio_tracks(
    name: str,
    body: ReorderAudioTracksBody,
    pd: Path = Depends(project_dir),
) -> dict:
    from scenecraft.db import reorder_audio_tracks as db_reorder_audio_tracks

    db_reorder_audio_tracks(pd, body.trackIds)
    return {"success": True}


# ---------------------------------------------------------------------------
# POST /audio-tracks/{track_id}/volume-curve (🔧 chat-tool: update_volume_curve)
# ---------------------------------------------------------------------------


@router.post(
    "/{name}/audio-tracks/{track_id}/volume-curve",
    operation_id="update_volume_curve",
)
async def update_volume_curve(
    name: str,
    track_id: str,
    body: UpdateVolumeCurveBody,
    pd: Path = Depends(project_dir),
) -> dict:
    """Thin wrapper over ``chat._exec_update_volume_curve``.

    The chat tool supports ``target_type in {'track', 'clip'}``. For REST, the
    track_id comes from the path; the body may override ``target_type`` to
    ``'clip'`` (in which case ``target_id`` identifies the clip — path
    ``track_id`` is ignored).
    """
    from scenecraft.chat import _exec_update_volume_curve

    target_type = body.target_type or "track"
    target_id = body.target_id or track_id
    payload = {
        "target_type": target_type,
        "target_id": target_id,
        "interpolation": body.interpolation or "bezier",
        "points": body.points,
    }
    result = _exec_update_volume_curve(pd, payload)
    if isinstance(result, dict) and "error" in result:
        raise ApiError("BAD_REQUEST", str(result["error"]), status_code=400)
    return result
