"""Audio intelligence stubs + audio-isolations listing (M16 T62).

``audio-intelligence`` and ``update-rules`` / ``reapply-rules`` are legacy
stubs that returned empty payloads after the audio-intelligence system
was dismantled; we preserve the same shape for client compatibility.

``audio-isolations`` remains a real read — it lists stem-isolation runs
for a given entity (clip / transition).
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, Query

from scenecraft.api.deps import current_user, project_dir
from scenecraft.api.errors import ApiError
from scenecraft.api.models.audio import ReapplyRulesBody, UpdateRulesBody


router = APIRouter(prefix="/api/projects", tags=["audio"], dependencies=[Depends(current_user)])


@router.get(
    "/{name}/audio-intelligence",
    operation_id="get_audio_intelligence",
)
async def get_audio_intelligence(name: str, pd: Path = Depends(project_dir)) -> dict:
    return {
        "activeFile": None,
        "events": [],
        "sections": [],
        "rules": [],
        "ruleCount": 0,
        "onsets": {},
    }


@router.post(
    "/{name}/update-rules",
    operation_id="update_rules_stub",
)
async def update_rules(
    name: str,
    body: UpdateRulesBody | None = None,
    pd: Path = Depends(project_dir),
) -> dict:
    return {"success": True, "count": 0}


@router.post(
    "/{name}/reapply-rules",
    operation_id="reapply_rules_stub",
)
async def reapply_rules(
    name: str,
    body: ReapplyRulesBody | None = None,
    pd: Path = Depends(project_dir),
) -> dict:
    return {"success": True, "eventCount": 0}


@router.get(
    "/{name}/audio-isolations",
    operation_id="list_audio_isolations",
)
async def list_audio_isolations(
    name: str,
    entityType: str | None = Query(default=None),
    entityId: str | None = Query(default=None),
    pd: Path = Depends(project_dir),
) -> dict:
    if not entityType or not entityId:
        raise ApiError(
            "BAD_REQUEST",
            "entityType and entityId query params required",
            status_code=400,
        )
    from scenecraft.db import get_isolations_for_entity

    rows = get_isolations_for_entity(pd, entityType, entityId)
    return {"isolations": rows}
