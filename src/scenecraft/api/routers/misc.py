"""Misc router — hosts ``GET /api/config``.

T57 shipped this open for the scaffold spike; T58 gates it behind the real
``current_user`` dependency since the legacy server treats ``/api/config`` as
an authenticated endpoint (any path not in the public carve-out list is
auth-gated — see ``api_server.py::_authenticate`` line 130).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from scenecraft.api.deps import current_user

router = APIRouter(tags=["misc"], dependencies=[Depends(current_user)])


@router.get(
    "/api/config",
    operation_id="get_config",
    summary="Return the persisted scenecraft configuration",
)
async def get_config() -> dict:
    from scenecraft.config import load_config

    return load_config()
