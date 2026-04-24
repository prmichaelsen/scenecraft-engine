"""Misc router — hosts ``GET /api/config`` for the T57 smoke test.

Auth is deliberately NOT required here: T58 will add the real
``Depends(current_user)`` dependency once the bearer+cookie code
is ported. This mirrors the legacy server, where ``/api/config``
is public (see ``api_server.py:189``).
"""

from __future__ import annotations

from fastapi import APIRouter

from scenecraft.config import load_config

router = APIRouter(tags=["misc"])


@router.get(
    "/api/config",
    operation_id="get_config",
    summary="Return the persisted scenecraft configuration",
)
async def get_config() -> dict:
    return load_config()
