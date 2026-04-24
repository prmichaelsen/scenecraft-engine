"""Transition routes — M16 T61.

22 routes covering the transition mutation surface from legacy
``api_server.py``, plus one net-new route (``batch-delete-transitions``)
that was previously chat-tool-only. Structural routes (delete / restore
/ split / batch-delete) gate on ``Depends(project_lock)``.

All handlers are thin wrappers over the legacy ``_handle_*`` methods;
see ``._legacy_proxy.dispatch_legacy``.

Handlers are sync (``def``, not ``async def``) so the starlette
threadpool runs them — see ``keyframes.py`` for rationale.

Operation IDs are chat-tool-aligned (T67):
  * ``delete-transition``   → ``delete_transition``
  * ``split-transition``    → ``split_transition``
  * ``batch-delete-transitions`` → ``batch_delete_transitions`` (new REST)
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Body, Depends, Request
from fastapi.responses import JSONResponse

from scenecraft.api.deps import project_dir, project_lock
from scenecraft.api.errors import ApiError
from scenecraft.api.models.transitions import (
    BatchDeleteTransitionsBody,
    ClipTrimEdgeBody,
    CopyTransitionStyleBody,
    DeleteTransitionBody,
    DuplicateTransitionVideoBody,
    EnhanceTransitionActionBody,
    GenerateTransitionActionBody,
    GenerateTransitionCandidatesBody,
    LinkAudioBody,
    MoveTransitionsBody,
    RestoreTransitionBody,
    SelectTransitionsBody,
    SplitTransitionBody,
    TransitionEffectAddBody,
    TransitionEffectDeleteBody,
    TransitionEffectUpdateBody,
    UpdateTransitionActionBody,
    UpdateTransitionLabelBody,
    UpdateTransitionRemapBody,
    UpdateTransitionStyleBody,
    UpdateTransitionTrimBody,
)
from scenecraft.api.routers._legacy_proxy import (
    dispatch_legacy,
    dispatch_legacy_path,
)

router = APIRouter(tags=["transitions"])


def _work_dir(request: Request) -> Path:
    wd = getattr(request.app.state, "work_dir", None)
    if wd is None:
        raise ApiError("INTERNAL_ERROR", "work_dir not configured", status_code=500)
    return wd


# ---------------------------------------------------------------------------
# Selection / trim / move (non-structural)
# ---------------------------------------------------------------------------


@router.post(
    "/api/projects/{name}/select-transitions",
    operation_id="select_transitions",
    dependencies=[Depends(project_dir)],
)
def select_transitions(
    name: str, request: Request, body: SelectTransitionsBody
) -> JSONResponse:
    return dispatch_legacy(
        _work_dir(request), "_handle_select_transitions", name, body.model_dump()
    )


@router.post(
    "/api/projects/{name}/update-transition-trim",
    operation_id="update_transition_trim",
    dependencies=[Depends(project_dir)],
)
def update_transition_trim(
    name: str, request: Request, body: UpdateTransitionTrimBody
) -> JSONResponse:
    return dispatch_legacy(
        _work_dir(request), "_handle_update_transition_trim", name, body.model_dump()
    )


@router.post(
    "/api/projects/{name}/clip-trim-edge",
    operation_id="clip_trim_edge",
    dependencies=[Depends(project_dir)],
)
def clip_trim_edge(
    name: str, request: Request, body: ClipTrimEdgeBody
) -> JSONResponse:
    return dispatch_legacy(
        _work_dir(request), "_handle_clip_trim_edge", name, body.model_dump()
    )


@router.post(
    "/api/projects/{name}/move-transitions",
    operation_id="move_transitions",
    dependencies=[Depends(project_dir)],
)
def move_transitions(
    name: str, request: Request, body: MoveTransitionsBody
) -> JSONResponse:
    return dispatch_legacy(
        _work_dir(request), "_handle_move_transitions", name, body.model_dump()
    )


# ---------------------------------------------------------------------------
# Structural — delete / restore / split / batch-delete
# ---------------------------------------------------------------------------


@router.post(
    "/api/projects/{name}/delete-transition",
    operation_id="delete_transition",
    dependencies=[Depends(project_lock), Depends(project_dir)],
)
def delete_transition(
    name: str, request: Request, body: DeleteTransitionBody
) -> JSONResponse:
    return dispatch_legacy(
        _work_dir(request), "_handle_delete_transition", name, body.model_dump()
    )


@router.post(
    "/api/projects/{name}/restore-transition",
    operation_id="restore_transition",
    dependencies=[Depends(project_lock), Depends(project_dir)],
)
def restore_transition(
    name: str, request: Request, body: RestoreTransitionBody
) -> JSONResponse:
    return dispatch_legacy(
        _work_dir(request), "_handle_restore_transition", name, body.model_dump()
    )


@router.post(
    "/api/projects/{name}/split-transition",
    operation_id="split_transition",
    dependencies=[Depends(project_lock), Depends(project_dir)],
)
def split_transition(
    name: str, request: Request, body: SplitTransitionBody
) -> JSONResponse:
    return dispatch_legacy(
        _work_dir(request), "_handle_split_transition", name, body.model_dump()
    )


# New REST surface: batch-delete-transitions was chat-only in legacy.
# Registered with project_lock so it serializes with other structural
# deletes; see ``structural.py`` for STRUCTURAL_ROUTES update.
@router.post(
    "/api/projects/{name}/batch-delete-transitions",
    operation_id="batch_delete_transitions",
    dependencies=[Depends(project_lock), Depends(project_dir)],
)
def batch_delete_transitions(
    name: str,
    pdir: Path = Depends(project_dir),
    body: BatchDeleteTransitionsBody = Body(...),
) -> dict:
    """Batch soft-delete transitions — mirrors ``chat._exec_batch_delete_transitions``."""
    from scenecraft.chat import _exec_batch_delete_transitions

    result = _exec_batch_delete_transitions(pdir, body.model_dump())
    if isinstance(result, dict) and "error" in result:
        raise ApiError("BAD_REQUEST", result["error"], status_code=400)
    return result


# ---------------------------------------------------------------------------
# Action / remap / generate / enhance / style / label (non-structural)
# ---------------------------------------------------------------------------


@router.post(
    "/api/projects/{name}/update-transition-action",
    operation_id="update_transition_action",
    dependencies=[Depends(project_dir)],
)
def update_transition_action(
    name: str, request: Request, body: UpdateTransitionActionBody
) -> JSONResponse:
    return dispatch_legacy(
        _work_dir(request), "_handle_update_transition_action", name, body.model_dump()
    )


@router.post(
    "/api/projects/{name}/update-transition-remap",
    operation_id="update_transition_remap",
    dependencies=[Depends(project_dir)],
)
def update_transition_remap(
    name: str, request: Request, body: UpdateTransitionRemapBody
) -> JSONResponse:
    return dispatch_legacy(
        _work_dir(request), "_handle_update_transition_remap", name, body.model_dump()
    )


@router.post(
    "/api/projects/{name}/generate-transition-action",
    operation_id="generate_transition_action",
    dependencies=[Depends(project_dir)],
)
def generate_transition_action(
    name: str, request: Request, body: GenerateTransitionActionBody
) -> JSONResponse:
    return dispatch_legacy(
        _work_dir(request),
        "_handle_generate_transition_action",
        name,
        body.model_dump(),
    )


@router.post(
    "/api/projects/{name}/enhance-transition-action",
    operation_id="enhance_transition_action",
    dependencies=[Depends(project_dir)],
)
def enhance_transition_action(
    name: str, request: Request, body: EnhanceTransitionActionBody
) -> JSONResponse:
    return dispatch_legacy(
        _work_dir(request),
        "_handle_enhance_transition_action",
        name,
        body.model_dump(),
    )


@router.post(
    "/api/projects/{name}/update-transition-style",
    operation_id="update_transition_style",
    dependencies=[Depends(project_dir)],
)
def update_transition_style(
    name: str, request: Request, body: UpdateTransitionStyleBody
) -> JSONResponse:
    return dispatch_legacy_path(
        _work_dir(request),
        f"/api/projects/{name}/update-transition-style",
        body.model_dump(),
    )


@router.post(
    "/api/projects/{name}/update-transition-label",
    operation_id="update_transition_label",
    dependencies=[Depends(project_dir)],
)
def update_transition_label(
    name: str, request: Request, body: UpdateTransitionLabelBody
) -> JSONResponse:
    return dispatch_legacy_path(
        _work_dir(request),
        f"/api/projects/{name}/update-transition-label",
        body.model_dump(),
    )


# ---------------------------------------------------------------------------
# Copy / duplicate / generate (non-structural)
# ---------------------------------------------------------------------------


@router.post(
    "/api/projects/{name}/copy-transition-style",
    operation_id="copy_transition_style",
    dependencies=[Depends(project_dir)],
)
def copy_transition_style(
    name: str, request: Request, body: CopyTransitionStyleBody
) -> JSONResponse:
    return dispatch_legacy_path(
        _work_dir(request),
        f"/api/projects/{name}/copy-transition-style",
        body.model_dump(),
    )


@router.post(
    "/api/projects/{name}/duplicate-transition-video",
    operation_id="duplicate_transition_video",
    dependencies=[Depends(project_dir)],
)
def duplicate_transition_video(
    name: str, request: Request, body: DuplicateTransitionVideoBody
) -> JSONResponse:
    return dispatch_legacy_path(
        _work_dir(request),
        f"/api/projects/{name}/duplicate-transition-video",
        body.model_dump(),
    )


@router.post(
    "/api/projects/{name}/generate-transition-candidates",
    operation_id="generate_transition_candidates",
    dependencies=[Depends(project_dir)],
)
def generate_transition_candidates(
    name: str, request: Request, body: GenerateTransitionCandidatesBody
) -> JSONResponse:
    return dispatch_legacy(
        _work_dir(request),
        "_handle_generate_transition_candidates",
        name,
        body.model_dump(),
    )


# ---------------------------------------------------------------------------
# Link-audio — takes ``tr_id`` as a path param
# ---------------------------------------------------------------------------


@router.post(
    "/api/projects/{name}/transitions/{tr_id}/link-audio",
    operation_id="link_transition_audio",
    dependencies=[Depends(project_dir)],
)
def link_transition_audio(
    name: str, tr_id: str, request: Request, body: LinkAudioBody
) -> JSONResponse:
    # Inline handler in legacy ``_do_POST``: routed on
    # ``^/api/projects/([^/]+)/transitions/([^/]+)/link-audio$``.
    return dispatch_legacy_path(
        _work_dir(request),
        f"/api/projects/{name}/transitions/{tr_id}/link-audio",
        body.model_dump(),
    )


# ---------------------------------------------------------------------------
# Transition effects
# ---------------------------------------------------------------------------


@router.post(
    "/api/projects/{name}/transition-effects/add",
    operation_id="add_transition_effect",
    dependencies=[Depends(project_dir)],
)
def add_transition_effect(
    name: str, request: Request, body: TransitionEffectAddBody
) -> JSONResponse:
    return dispatch_legacy_path(
        _work_dir(request),
        f"/api/projects/{name}/transition-effects/add",
        body.model_dump(),
    )


@router.post(
    "/api/projects/{name}/transition-effects/update",
    operation_id="update_transition_effect",
    dependencies=[Depends(project_dir)],
)
def update_transition_effect(
    name: str, request: Request, body: TransitionEffectUpdateBody
) -> JSONResponse:
    return dispatch_legacy_path(
        _work_dir(request),
        f"/api/projects/{name}/transition-effects/update",
        body.model_dump(),
    )


@router.post(
    "/api/projects/{name}/transition-effects/delete",
    operation_id="delete_transition_effect",
    dependencies=[Depends(project_dir)],
)
def delete_transition_effect(
    name: str, request: Request, body: TransitionEffectDeleteBody
) -> JSONResponse:
    return dispatch_legacy_path(
        _work_dir(request),
        f"/api/projects/{name}/transition-effects/delete",
        body.model_dump(),
    )


# ---------------------------------------------------------------------------
# Catch-all update-transition — chat-tool-only in legacy
# ---------------------------------------------------------------------------


@router.post(
    "/api/projects/{name}/update-transition",
    operation_id="update_transition",
    dependencies=[Depends(project_dir)],
)
def update_transition(
    name: str,
    pdir: Path = Depends(project_dir),
    body: dict = Body(...),
) -> dict:
    from scenecraft.chat import _exec_update_transition

    tr_id = body.get("transition_id")
    if not tr_id or not isinstance(tr_id, str):
        raise ApiError("BAD_REQUEST", "Missing 'transition_id'", status_code=400)

    result = _exec_update_transition(pdir, body)
    if isinstance(result, dict) and "error" in result:
        raise ApiError("BAD_REQUEST", result["error"], status_code=400)
    return result


__all__ = ["router"]
