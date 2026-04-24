"""Effects router — user-authored transition/keyframe effect rules (M16 T63).

NOT to be confused with M13 audio effects (track_effects, curves, macro
panel) — those live in the audio router (T62). This module only covers
the legacy ``GET/POST /api/projects/{name}/effects`` pair which loads
and saves the user-authored effect-rule document used by the transition
renderer.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends

from scenecraft.api.deps import current_user, project_dir
from scenecraft.api.errors import ApiError
from scenecraft.api.models.media import EffectsBody

router = APIRouter(tags=["effects"], dependencies=[Depends(current_user)])


@router.get(
    "/api/projects/{name}/effects",
    operation_id="list_effects",
    summary="Load user-authored effect rules + suppressions.",
)
async def list_effects(name: str, pdir: Path = Depends(project_dir)) -> dict:
    from scenecraft.db import get_effects, get_suppressions

    return {
        "effects": get_effects(pdir),
        "suppressions": get_suppressions(pdir),
    }


@router.post(
    "/api/projects/{name}/effects",
    operation_id="upsert_effects",
    summary="Save user-authored effect rules (and optionally suppressions).",
)
async def upsert_effects(
    name: str, body: EffectsBody, pdir: Path = Depends(project_dir)
) -> dict:
    """Legacy parity: suppressions are only rewritten if present in the body.

    The frontend posts ``{effects: [...]}`` to edit rules without
    touching suppressions. When ``suppressions`` is explicitly in the
    body we overwrite; otherwise we merge the existing list back in.
    """
    from scenecraft.db import get_suppressions, save_effects

    effects = body.effects
    suppressions = body.suppressions if body.suppressions is not None else get_suppressions(pdir)
    try:
        save_effects(pdir, effects, suppressions)
    except Exception as e:
        raise ApiError("INTERNAL_ERROR", str(e), status_code=500)
    return {"success": True}
