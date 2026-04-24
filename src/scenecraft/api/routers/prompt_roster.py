"""Prompt-roster router (M16 T60).

Four routes wrapping ``scenecraft.db.{get,add,update,delete}_prompt_roster``.
"""

from __future__ import annotations

import time
from pathlib import Path

from fastapi import APIRouter, Depends

from scenecraft.api.deps import current_user, project_dir as project_dir_dep
from scenecraft.api.errors import ApiError
from scenecraft.api.models.projects import (
    PromptRosterAddBody,
    PromptRosterRemoveBody,
    PromptRosterUpdateBody,
)


router = APIRouter(tags=["prompt-roster"], dependencies=[Depends(current_user)])


@router.get(
    "/api/projects/{name}/prompt-roster",
    operation_id="get_prompt_roster",
    summary="List prompt-roster entries",
)
async def get_prompt_roster(
    name: str, proj: Path = Depends(project_dir_dep)
) -> dict:
    from scenecraft.db import get_prompt_roster as _get

    return {"prompts": _get(proj)}


@router.post(
    "/api/projects/{name}/prompt-roster/add",
    operation_id="add_prompt_roster_entry",
    summary="Add a prompt-roster entry",
)
async def add_prompt_roster_entry(
    name: str, body: PromptRosterAddBody, proj: Path = Depends(project_dir_dep)
) -> dict:
    from scenecraft.db import add_prompt_roster as _add

    pid = body.id or f"pr_{int(time.time() * 1000)}"
    _add(
        proj,
        pid,
        body.name or "",
        body.template or "",
        body.category or "general",
    )
    return {"success": True, "id": pid}


@router.post(
    "/api/projects/{name}/prompt-roster/update",
    operation_id="update_prompt_roster_entry",
    summary="Update a prompt-roster entry",
)
async def update_prompt_roster_entry(
    name: str,
    body: PromptRosterUpdateBody,
    proj: Path = Depends(project_dir_dep),
) -> dict:
    from scenecraft.db import update_prompt_roster as _update

    if not body.id:
        raise ApiError("BAD_REQUEST", "Missing 'id'", status_code=400)
    data = body.model_dump(exclude_none=True)
    updates = {
        k: v for k, v in data.items() if k in ("name", "template", "category")
    }
    _update(proj, body.id, **updates)
    return {"success": True}


@router.post(
    "/api/projects/{name}/prompt-roster/remove",
    operation_id="remove_prompt_roster_entry",
    summary="Delete a prompt-roster entry",
)
async def remove_prompt_roster_entry(
    name: str,
    body: PromptRosterRemoveBody,
    proj: Path = Depends(project_dir_dep),
) -> dict:
    from scenecraft.db import delete_prompt_roster

    delete_prompt_roster(proj, body.id or "")
    return {"success": True}


__all__ = ["router"]
