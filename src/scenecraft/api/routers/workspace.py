"""Workspace views router (M16 T60).

Four routes mirroring ``api_server.py`` lines 260-280 (GET) and
1045-1067 (POST upsert + POST delete).
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends

from scenecraft.api.deps import current_user, project_dir as project_dir_dep
from scenecraft.api.models.projects import WorkspaceViewBody


router = APIRouter(tags=["workspace"], dependencies=[Depends(current_user)])


def _log(msg: str) -> None:
    from scenecraft.api_server import _log as legacy_log

    legacy_log(msg)


@router.get(
    "/api/projects/{name}/workspace-views",
    operation_id="list_workspace_views",
    summary="List all saved workspace layouts",
)
async def list_workspace_views(
    name: str, proj: Path = Depends(project_dir_dep)
) -> dict:
    from scenecraft.db import get_meta

    meta = get_meta(proj)
    views = {
        k.replace("workspace_view:", ""): v
        for k, v in meta.items()
        if k.startswith("workspace_view:")
    }
    return {"views": views}


@router.get(
    "/api/projects/{name}/workspace-views/{view_name}",
    operation_id="get_workspace_view",
    summary="Fetch a single saved workspace layout",
)
async def get_workspace_view(
    name: str, view_name: str, proj: Path = Depends(project_dir_dep)
) -> dict:
    from scenecraft.api.errors import ApiError
    from scenecraft.db import get_meta

    meta = get_meta(proj)
    layout = meta.get(f"workspace_view:{view_name}")
    if layout is None:
        raise ApiError(
            "NOT_FOUND",
            f"Workspace view not found: {view_name}",
            status_code=404,
        )
    return {"layout": layout}


@router.post(
    "/api/projects/{name}/workspace-views/{view_name}",
    operation_id="upsert_workspace_view",
    summary="Save (create or overwrite) a workspace layout",
)
async def upsert_workspace_view(
    name: str,
    view_name: str,
    body: WorkspaceViewBody,
    proj: Path = Depends(project_dir_dep),
) -> dict:
    from scenecraft.db import set_meta

    set_meta(proj, f"workspace_view:{view_name}", body.layout or {})
    _log(f"workspace-view saved: {name} / {view_name}")
    return {"success": True}


@router.post(
    "/api/projects/{name}/workspace-views/{view_name}/delete",
    operation_id="delete_workspace_view",
    summary="Delete a saved workspace layout",
)
async def delete_workspace_view(
    name: str, view_name: str, proj: Path = Depends(project_dir_dep)
) -> dict:
    from scenecraft.db import get_db

    conn = get_db(proj)
    conn.execute(
        "DELETE FROM meta WHERE key = ?", (f"workspace_view:{view_name}",)
    )
    conn.commit()
    _log(f"workspace-view deleted: {name} / {view_name}")
    return {"success": True}


__all__ = ["router"]
