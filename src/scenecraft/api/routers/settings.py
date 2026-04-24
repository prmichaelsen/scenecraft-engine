"""Settings + section-settings router (M16 T60).

Four routes mirroring ``api_server.py::_handle_get_settings``,
``_handle_update_settings``, ``_handle_get_section_settings``, and
``_handle_section_settings``.
"""

from __future__ import annotations

import json
from pathlib import Path

from fastapi import APIRouter, Depends, Request

from scenecraft.api.deps import current_user, project_dir as project_dir_dep
from scenecraft.api.errors import ApiError
from scenecraft.api.models.projects import SectionSettingsBody, SettingsBody


router = APIRouter(tags=["settings"], dependencies=[Depends(current_user)])


def _log(msg: str) -> None:
    from scenecraft.api.utils import _log as _util_log

    _util_log(msg)


def _work_dir(request: Request) -> Path:
    wd = getattr(request.app.state, "work_dir", None)
    if wd is None:
        raise ApiError(
            "INTERNAL_ERROR", "work_dir missing", status_code=500
        )
    return wd


@router.get(
    "/api/projects/{name}/settings",
    operation_id="get_settings",
    summary="Return per-project settings with defaults applied",
)
async def get_settings(name: str, request: Request) -> dict:
    _log(f"get-settings: {name}")
    wd = _work_dir(request)
    settings_path = wd / name / "settings.json"
    defaults = {
        "preview_quality": 50,
        "render_preview_fps": 24,
        "preview_scale_factor": 0.5,
    }
    if settings_path.exists():
        with open(settings_path) as f:
            saved = json.load(f)
        defaults.update(saved)
    return defaults


@router.post(
    "/api/projects/{name}/settings",
    operation_id="update_settings",
    summary="Persist per-project settings and invalidate preview cache",
)
async def update_settings(
    name: str, body: SettingsBody, request: Request
) -> dict:
    wd = _work_dir(request)
    _log("update-settings: settings updated")
    settings_path = wd / name / "settings.json"
    existing: dict = {}
    if settings_path.exists():
        with open(settings_path) as f:
            existing = json.load(f)
    allowed = {"preview_quality", "render_preview_fps", "preview_scale_factor"}
    data = body.model_dump(exclude_none=True)
    for key in allowed:
        if key in data:
            existing[key] = data[key]
    with open(settings_path, "w") as f:
        json.dump(existing, f, indent=2)
    try:
        from scenecraft.render.preview_worker import RenderCoordinator

        RenderCoordinator.instance().invalidate_project(wd / name)
    except Exception:
        pass
    return {"success": True, **existing}


@router.get(
    "/api/projects/{name}/section-settings",
    operation_id="get_section_settings",
    summary="Return per-section still + suggestions",
)
async def get_section_settings(
    name: str, request: Request, section: str = ""
) -> dict:
    wd = _work_dir(request)
    _log(f"get-section-settings: section={section}")
    pd = wd / name
    if not pd.is_dir():
        return {}
    try:
        from scenecraft.db import get_meta

        meta = get_meta(pd)
        still = meta.get(f"section_still:{section}", None)
        suggestions_raw = meta.get(f"section_suggestions:{section}", None)
        suggestions = (
            json.loads(suggestions_raw)
            if isinstance(suggestions_raw, str)
            else suggestions_raw
        )
        return {"still": still, "suggestions": suggestions}
    except Exception as e:
        raise ApiError("INTERNAL_ERROR", str(e), status_code=500)


@router.post(
    "/api/projects/{name}/section-settings",
    operation_id="update_section_settings",
    summary="Persist per-section still/suggestions",
)
async def update_section_settings(
    name: str, body: SectionSettingsBody, proj: Path = Depends(project_dir_dep)
) -> dict:
    if not body.sectionLabel:
        raise ApiError("BAD_REQUEST", "Missing 'sectionLabel'", status_code=400)
    try:
        _log(f"section-settings: {body.sectionLabel}")
        from scenecraft.db import set_meta

        data = body.model_dump(exclude_none=True)
        if "still" in data:
            set_meta(proj, f"section_still:{body.sectionLabel}", data["still"])
        if "suggestions" in data:
            set_meta(
                proj,
                f"section_suggestions:{body.sectionLabel}",
                json.dumps(data["suggestions"]),
            )
        return {"success": True}
    except ApiError:
        raise
    except Exception as e:
        raise ApiError("INTERNAL_ERROR", str(e), status_code=500)


__all__ = ["router"]
