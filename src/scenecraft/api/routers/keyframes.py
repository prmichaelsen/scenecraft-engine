"""Keyframe routes — M16 T61.

25 routes covering the keyframe mutation surface from legacy
``api_server.py``. Structural routes (add / delete / batch-delete /
restore / duplicate / paste-group / insert-pool-item) gate on
``Depends(project_lock)`` so the post-handler timeline validator runs
and concurrent mutations on the same project serialize. Non-structural
routes (update / label / style / generate / etc.) only pull
``project_dir`` which chains ``current_user``.

All handlers are thin wrappers over the legacy ``_handle_*`` methods —
see ``._legacy_proxy.dispatch_legacy`` for the shim mechanism.

Operation IDs are chat-tool-aligned where relevant (T67):
  * ``update-prompt``       → ``update_keyframe_prompt``  (NOT ``update_prompt``)
  * ``update-timestamp``    → ``update_keyframe_timestamp``
  * ``delete-keyframe``     → ``delete_keyframe``
  * ``add-keyframe``        → ``add_keyframe``
  * ``update-keyframe``     → ``update_keyframe``  (new, chat-tool only in legacy)

Any mismatch between STRUCTURAL_ROUTES and legacy ``_structural_routes``
is a bug — both sets are synced in ``scenecraft.api.structural``.

Handlers are **sync** (``def``, not ``async def``) because
``dispatch_legacy`` is a blocking call. FastAPI offloads sync handlers
to the starlette threadpool so concurrent requests across different
projects don't block each other on the event loop — which is exactly
what the per-project lock tests rely on (see T59).
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Body, Depends, Request
from fastapi.responses import JSONResponse

from scenecraft.api.deps import project_dir, project_lock
from scenecraft.api.errors import ApiError
from scenecraft.api.models.keyframes import (
    AddKeyframeBody,
    AssignKeyframeImageBody,
    BatchDeleteKeyframesBody,
    BatchSetBaseImageBody,
    DeleteKeyframeBody,
    DuplicateKeyframeBody,
    EnhanceKeyframePromptBody,
    EscalateKeyframeBody,
    GenerateKeyframeCandidatesBody,
    GenerateKeyframeVariationsBody,
    GenerateSlotKeyframeCandidatesBody,
    InsertPoolItemBody,
    PasteGroupBody,
    RestoreKeyframeBody,
    SelectKeyframesBody,
    SelectSlotKeyframesBody,
    SetBaseImageBody,
    SuggestKeyframePromptsBody,
    UnlinkKeyframeBody,
    UpdateKeyframeLabelBody,
    UpdateKeyframeStyleBody,
    UpdatePromptBody,
    UpdateTimestampBody,
)
from scenecraft.api.routers._legacy_proxy import (
    dispatch_legacy,
    dispatch_legacy_path,
)

router = APIRouter(tags=["keyframes"])


def _work_dir(request: Request) -> Path:
    wd = getattr(request.app.state, "work_dir", None)
    if wd is None:
        raise ApiError("INTERNAL_ERROR", "work_dir not configured", status_code=500)
    return wd


# ---------------------------------------------------------------------------
# Selection (non-structural)
# ---------------------------------------------------------------------------


@router.post(
    "/api/projects/{name}/select-keyframes",
    operation_id="select_keyframes",
    dependencies=[Depends(project_dir)],
)
def select_keyframes(
    name: str, request: Request, body: SelectKeyframesBody
) -> JSONResponse:
    return dispatch_legacy(
        _work_dir(request), "_handle_select_keyframes", name, body.model_dump()
    )


@router.post(
    "/api/projects/{name}/select-slot-keyframes",
    operation_id="select_slot_keyframes",
    dependencies=[Depends(project_dir)],
)
def select_slot_keyframes(
    name: str, request: Request, body: SelectSlotKeyframesBody
) -> JSONResponse:
    return dispatch_legacy(
        _work_dir(request), "_handle_select_slot_keyframes", name, body.model_dump()
    )


# ---------------------------------------------------------------------------
# Timestamp / prompt (non-structural; chat-tool-aligned operation_ids)
# ---------------------------------------------------------------------------


@router.post(
    "/api/projects/{name}/update-timestamp",
    operation_id="update_keyframe_timestamp",
    dependencies=[Depends(project_dir)],
)
def update_timestamp(
    name: str, request: Request, body: UpdateTimestampBody
) -> JSONResponse:
    return dispatch_legacy(
        _work_dir(request), "_handle_update_timestamp", name, body.model_dump()
    )


@router.post(
    "/api/projects/{name}/update-prompt",
    operation_id="update_keyframe_prompt",
    dependencies=[Depends(project_dir)],
)
def update_prompt(
    name: str, request: Request, body: UpdatePromptBody
) -> JSONResponse:
    return dispatch_legacy(
        _work_dir(request), "_handle_update_prompt", name, body.model_dump()
    )


# ---------------------------------------------------------------------------
# Structural routes — guarded by project_lock
# ---------------------------------------------------------------------------


@router.post(
    "/api/projects/{name}/add-keyframe",
    operation_id="add_keyframe",
    dependencies=[Depends(project_lock), Depends(project_dir)],
)
def add_keyframe(
    name: str, request: Request, body: AddKeyframeBody
) -> JSONResponse:
    return dispatch_legacy(
        _work_dir(request), "_handle_add_keyframe", name, body.model_dump()
    )


@router.post(
    "/api/projects/{name}/duplicate-keyframe",
    operation_id="duplicate_keyframe",
    dependencies=[Depends(project_lock), Depends(project_dir)],
)
def duplicate_keyframe(
    name: str, request: Request, body: DuplicateKeyframeBody
) -> JSONResponse:
    return dispatch_legacy(
        _work_dir(request), "_handle_duplicate_keyframe", name, body.model_dump()
    )


@router.post(
    "/api/projects/{name}/paste-group",
    operation_id="paste_group",
    dependencies=[Depends(project_lock), Depends(project_dir)],
)
def paste_group(
    name: str, request: Request, body: PasteGroupBody
) -> JSONResponse:
    return dispatch_legacy(
        _work_dir(request), "_handle_paste_group", name, body.model_dump()
    )


@router.post(
    "/api/projects/{name}/delete-keyframe",
    operation_id="delete_keyframe",
    dependencies=[Depends(project_lock), Depends(project_dir)],
)
def delete_keyframe(
    name: str, request: Request, body: DeleteKeyframeBody
) -> JSONResponse:
    return dispatch_legacy(
        _work_dir(request), "_handle_delete_keyframe", name, body.model_dump()
    )


@router.post(
    "/api/projects/{name}/batch-delete-keyframes",
    operation_id="batch_delete_keyframes",
    dependencies=[Depends(project_lock), Depends(project_dir)],
)
def batch_delete_keyframes(
    name: str, request: Request, body: BatchDeleteKeyframesBody
) -> JSONResponse:
    # Legacy handler accepts either key name; forward exactly what the client
    # sent (exclude_none so a ``null`` doesn't mask the other key).
    payload = body.model_dump(exclude_none=True)
    return dispatch_legacy(
        _work_dir(request), "_handle_batch_delete_keyframes", name, payload
    )


@router.post(
    "/api/projects/{name}/restore-keyframe",
    operation_id="restore_keyframe",
    dependencies=[Depends(project_lock), Depends(project_dir)],
)
def restore_keyframe(
    name: str, request: Request, body: RestoreKeyframeBody
) -> JSONResponse:
    return dispatch_legacy(
        _work_dir(request), "_handle_restore_keyframe", name, body.model_dump()
    )


@router.post(
    "/api/projects/{name}/insert-pool-item",
    operation_id="insert_pool_item",
    dependencies=[Depends(project_lock), Depends(project_dir)],
)
def insert_pool_item(
    name: str, request: Request, body: InsertPoolItemBody
) -> JSONResponse:
    return dispatch_legacy(
        _work_dir(request), "_handle_insert_pool_item", name, body.model_dump()
    )


# ---------------------------------------------------------------------------
# Base image / unlink / assign / escalate / label / style (non-structural)
# ---------------------------------------------------------------------------


@router.post(
    "/api/projects/{name}/batch-set-base-image",
    operation_id="batch_set_base_image",
    dependencies=[Depends(project_dir)],
)
def batch_set_base_image(
    name: str, request: Request, body: BatchSetBaseImageBody
) -> JSONResponse:
    return dispatch_legacy(
        _work_dir(request), "_handle_batch_set_base_image", name, body.model_dump()
    )


@router.post(
    "/api/projects/{name}/set-base-image",
    operation_id="set_base_image",
    dependencies=[Depends(project_dir)],
)
def set_base_image(
    name: str, request: Request, body: SetBaseImageBody
) -> JSONResponse:
    return dispatch_legacy(
        _work_dir(request), "_handle_set_base_image", name, body.model_dump()
    )


@router.post(
    "/api/projects/{name}/unlink-keyframe",
    operation_id="unlink_keyframe",
    dependencies=[Depends(project_dir)],
)
def unlink_keyframe(
    name: str, request: Request, body: UnlinkKeyframeBody
) -> JSONResponse:
    return dispatch_legacy(
        _work_dir(request), "_handle_unlink_keyframe", name, body.model_dump()
    )


@router.post(
    "/api/projects/{name}/escalate-keyframe",
    operation_id="escalate_keyframe",
    dependencies=[Depends(project_dir)],
)
def escalate_keyframe(
    name: str, request: Request, body: EscalateKeyframeBody
) -> JSONResponse:
    # Inline handler in legacy ``_do_POST`` — use path dispatch.
    return dispatch_legacy_path(
        _work_dir(request),
        f"/api/projects/{name}/escalate-keyframe",
        body.model_dump(),
    )


@router.post(
    "/api/projects/{name}/update-keyframe-label",
    operation_id="update_keyframe_label",
    dependencies=[Depends(project_dir)],
)
def update_keyframe_label(
    name: str, request: Request, body: UpdateKeyframeLabelBody
) -> JSONResponse:
    return dispatch_legacy_path(
        _work_dir(request),
        f"/api/projects/{name}/update-keyframe-label",
        body.model_dump(),
    )


@router.post(
    "/api/projects/{name}/update-keyframe-style",
    operation_id="update_keyframe_style",
    dependencies=[Depends(project_dir)],
)
def update_keyframe_style(
    name: str, request: Request, body: UpdateKeyframeStyleBody
) -> JSONResponse:
    return dispatch_legacy_path(
        _work_dir(request),
        f"/api/projects/{name}/update-keyframe-style",
        body.model_dump(),
    )


@router.post(
    "/api/projects/{name}/assign-keyframe-image",
    operation_id="assign_keyframe_image",
    dependencies=[Depends(project_dir)],
)
def assign_keyframe_image(
    name: str, request: Request, body: AssignKeyframeImageBody
) -> JSONResponse:
    return dispatch_legacy_path(
        _work_dir(request),
        f"/api/projects/{name}/assign-keyframe-image",
        body.model_dump(),
    )


# ---------------------------------------------------------------------------
# Generation (non-structural)
# ---------------------------------------------------------------------------


@router.post(
    "/api/projects/{name}/generate-keyframe-variations",
    operation_id="generate_keyframe_variations",
    dependencies=[Depends(project_dir)],
)
def generate_keyframe_variations(
    name: str, request: Request, body: GenerateKeyframeVariationsBody
) -> JSONResponse:
    return dispatch_legacy_path(
        _work_dir(request),
        f"/api/projects/{name}/generate-keyframe-variations",
        body.model_dump(),
    )


@router.post(
    "/api/projects/{name}/generate-keyframe-candidates",
    operation_id="generate_keyframe_candidates",
    dependencies=[Depends(project_dir)],
)
def generate_keyframe_candidates(
    name: str, request: Request, body: GenerateKeyframeCandidatesBody
) -> JSONResponse:
    return dispatch_legacy(
        _work_dir(request),
        "_handle_generate_keyframe_candidates",
        name,
        body.model_dump(),
    )


@router.post(
    "/api/projects/{name}/generate-slot-keyframe-candidates",
    operation_id="generate_slot_keyframe_candidates",
    dependencies=[Depends(project_dir)],
)
def generate_slot_keyframe_candidates(
    name: str, request: Request, body: GenerateSlotKeyframeCandidatesBody
) -> JSONResponse:
    return dispatch_legacy(
        _work_dir(request),
        "_handle_generate_slot_keyframe_candidates",
        name,
        body.model_dump(),
    )


@router.post(
    "/api/projects/{name}/suggest-keyframe-prompts",
    operation_id="suggest_keyframe_prompts",
    dependencies=[Depends(project_dir)],
)
def suggest_keyframe_prompts(
    name: str, request: Request, body: SuggestKeyframePromptsBody
) -> JSONResponse:
    return dispatch_legacy(
        _work_dir(request), "_handle_suggest_keyframe_prompts", name, body.model_dump()
    )


@router.post(
    "/api/projects/{name}/enhance-keyframe-prompt",
    operation_id="enhance_keyframe_prompt",
    dependencies=[Depends(project_dir)],
)
def enhance_keyframe_prompt(
    name: str, request: Request, body: EnhanceKeyframePromptBody
) -> JSONResponse:
    return dispatch_legacy(
        _work_dir(request), "_handle_enhance_keyframe_prompt", name, body.model_dump()
    )


# ---------------------------------------------------------------------------
# Catch-all update-keyframe — chat-tool-only in legacy; added as a REST route
# so the openapi.json surfaces it for codegen (T66) + tool annotation (T67).
# Delegates to ``chat._exec_update_keyframe`` for the exact same field mapping.
# ---------------------------------------------------------------------------


@router.post(
    "/api/projects/{name}/update-keyframe",
    operation_id="update_keyframe",
    dependencies=[Depends(project_dir)],
)
def update_keyframe(
    name: str,
    pdir: Path = Depends(project_dir),
    body: dict = Body(...),
) -> dict:
    """Chat-tool alignment: batch-update arbitrary keyframe fields.

    Body must include ``keyframe_id``; all other accepted keys match
    ``chat._UPDATE_KEYFRAME_FIELDS``. Returns the same payload shape
    the chat tool produces, so tests that go through either path see
    the same response envelope.
    """
    from scenecraft.chat import _exec_update_keyframe

    kf_id = body.get("keyframe_id")
    if not kf_id or not isinstance(kf_id, str):
        raise ApiError("BAD_REQUEST", "Missing 'keyframe_id'", status_code=400)

    result = _exec_update_keyframe(pdir, body)
    if isinstance(result, dict) and "error" in result:
        raise ApiError("BAD_REQUEST", result["error"], status_code=400)
    return result


__all__ = ["router"]
