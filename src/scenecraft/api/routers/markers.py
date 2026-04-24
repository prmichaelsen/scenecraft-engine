"""Markers router (M16 T60).

Four routes wrapping ``scenecraft.db.{get,add,update,delete}_marker``.
"""

from __future__ import annotations

import time
from pathlib import Path

from fastapi import APIRouter, Depends

from scenecraft.api.deps import current_user, project_dir as project_dir_dep
from scenecraft.api.errors import ApiError
from scenecraft.api.models.projects import (
    MarkerAddBody,
    MarkerRemoveBody,
    MarkerUpdateBody,
)


router = APIRouter(tags=["markers"], dependencies=[Depends(current_user)])


def _log(msg: str) -> None:
    from scenecraft.api_server import _log as legacy_log

    legacy_log(msg)


@router.get(
    "/api/projects/{name}/markers",
    operation_id="list_markers",
    summary="List timeline markers on a project",
)
async def list_markers(name: str, proj: Path = Depends(project_dir_dep)) -> dict:
    from scenecraft.db import get_markers

    return {"markers": get_markers(proj)}


@router.post(
    "/api/projects/{name}/markers/add",
    operation_id="add_marker",
    summary="Add a timeline marker",
)
async def add_marker(
    name: str, body: MarkerAddBody, proj: Path = Depends(project_dir_dep)
) -> dict:
    from scenecraft.db import add_marker as _db_add

    marker_id = body.id or f"m_{int(time.time() * 1000)}"
    _log(
        f"markers/add: {marker_id} time={body.time or 0} label={(body.label or '')!r}"
    )
    _db_add(
        proj,
        marker_id,
        body.time or 0,
        body.label or "",
        body.type or "note",
    )
    return {"success": True, "id": marker_id}


@router.post(
    "/api/projects/{name}/markers/update",
    operation_id="update_marker",
    summary="Update a timeline marker",
)
async def update_marker(
    name: str, body: MarkerUpdateBody, proj: Path = Depends(project_dir_dep)
) -> dict:
    from scenecraft.db import update_marker as _db_update

    if not body.id:
        raise ApiError("BAD_REQUEST", "Missing 'id'", status_code=400)
    data = body.model_dump(exclude_none=True)
    updates = {k: v for k, v in data.items() if k in ("time", "label", "type")}
    _log(f"markers/update: {body.id} {updates}")
    _db_update(proj, body.id, **updates)
    return {"success": True}


@router.post(
    "/api/projects/{name}/markers/remove",
    operation_id="remove_marker",
    summary="Delete a timeline marker",
)
async def remove_marker(
    name: str, body: MarkerRemoveBody, proj: Path = Depends(project_dir_dep)
) -> dict:
    from scenecraft.db import delete_marker

    _log(f"markers/remove: {body.id or ''}")
    delete_marker(proj, body.id or "")
    return {"success": True}


__all__ = ["router"]
