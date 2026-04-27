"""Config router (M16 T60).

Hosts ``POST /api/config`` (update). ``GET /api/config`` already lives
in ``routers.misc`` from the T57 scaffold — we leave it there and
only add the POST here so the misc router stays minimal.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from scenecraft.api.deps import current_user
from scenecraft.api.models.projects import UpdateConfigBody


router = APIRouter(tags=["config"], dependencies=[Depends(current_user)])


def _log(msg: str) -> None:
    from scenecraft.api.utils import _log as _util_log

    _util_log(msg)


@router.post(
    "/api/config",
    operation_id="update_config",
    summary="Update the persisted scenecraft configuration",
)
async def update_config(body: UpdateConfigBody) -> dict:
    from scenecraft.config import load_config, save_config, set_projects_dir

    data = body.model_dump(exclude_none=True)
    config = load_config()
    if "projects_dir" in data:
        set_projects_dir(data["projects_dir"])
        _log(f"config: projects_dir set to {data['projects_dir']}")
    else:
        config.update(data)
        save_config(config)
        _log(f"config: updated {list(data.keys())}")
    return {"success": True}


__all__ = ["router"]
