"""Pydantic request bodies for keyframe routes (M16 T61/T65).

Every model sets ``extra="ignore"`` so legacy clients that send extra
keys (e.g., ``metadata``) still succeed. Required fields match the
legacy server's ``body.get("...")`` checks -- anything that used to
produce ``"Missing '<field>'"`` from legacy is a ``...`` Pydantic
default here, so FastAPI's validator hits T58's envelope translator
and emits the identical message.

T65 native-port update: models expanded to include all fields the
native handlers actually read from the body, so ``model_dump()``
preserves them (``extra="ignore"`` drops undeclared fields).
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class _KfBody(BaseModel):
    """Base for all keyframe bodies -- permissive by design."""

    model_config = ConfigDict(extra="ignore")


# ---------------------------------------------------------------------------
# Selection
# ---------------------------------------------------------------------------


class SelectKeyframesBody(_KfBody):
    selections: dict[str, Any] = Field(..., description="{kf_id: variant_int}")


class SelectSlotKeyframesBody(_KfBody):
    selections: dict[str, Any] = Field(..., description="{slot_key: variant_int}")


# ---------------------------------------------------------------------------
# Timestamp / prompt
# ---------------------------------------------------------------------------


class UpdateTimestampBody(_KfBody):
    keyframeId: str = Field(...)
    newTimestamp: str | float | int | None = Field(default=None)
    timestamp: str | float | int | None = Field(default=None)


class UpdatePromptBody(_KfBody):
    keyframeId: str = Field(...)
    prompt: str = Field(default="")


# ---------------------------------------------------------------------------
# Structural (lock-guarded)
# ---------------------------------------------------------------------------


class AddKeyframeBody(_KfBody):
    timestamp: str | float | int = Field(...)
    section: str = Field(default="")
    prompt: str = Field(default="")
    trackId: str = Field(default="track_1")


class DuplicateKeyframeBody(_KfBody):
    keyframeId: str = Field(...)
    timestamp: str | float | int = Field(...)


class DeleteKeyframeBody(_KfBody):
    keyframeId: str = Field(...)


class BatchDeleteKeyframesBody(_KfBody):
    # Legacy handler accepts either ``keyframeIds`` or ``keyframe_ids``.
    # Declare both with ``extra="ignore"`` and leave validation to handler.
    keyframeIds: list[str] | None = Field(default=None)
    keyframe_ids: list[str] | None = Field(default=None)


class RestoreKeyframeBody(_KfBody):
    keyframeId: str = Field(...)


class PasteGroupBody(_KfBody):
    keyframeIds: list[str] = Field(default_factory=list)
    targetTime: str | float | int = Field(...)
    targetTrackId: str = Field(default="track_1")
    audioClipIds: list[str] = Field(default_factory=list)


class InsertPoolItemBody(_KfBody):
    type: str = Field(..., description="'keyframe' or 'transition'")
    poolPath: str = Field(..., description="pool path")
    atTime: float | int = Field(default=0)
    trackId: str = Field(default="track_1")


# ---------------------------------------------------------------------------
# Base image / unlink / assign / escalate / labels / styles
# ---------------------------------------------------------------------------


class SetBaseImageBody(_KfBody):
    keyframeId: str = Field(...)
    stillName: str = Field(...)


class BatchSetBaseImageBody(_KfBody):
    items: list[dict[str, Any]] = Field(...)


class UnlinkKeyframeBody(_KfBody):
    keyframeId: str = Field(...)
    side: str = Field(default="both", description="'both'|'left'|'right'")


class EscalateKeyframeBody(_KfBody):
    keyframeId: str = Field(...)
    count: int = Field(default=2)


class UpdateKeyframeLabelBody(_KfBody):
    keyframeId: str = Field(...)
    label: str = Field(default="")
    labelColor: str = Field(default="")
    tags: list[str] | None = Field(default=None)


class UpdateKeyframeStyleBody(_KfBody):
    keyframeId: str = Field(...)
    blendMode: str | None = Field(default=None)
    opacity: float | None = Field(default=None)
    refinementPrompt: str | None = Field(default=None)


class AssignKeyframeImageBody(_KfBody):
    keyframeId: str = Field(...)
    sourcePath: str = Field(...)


# ---------------------------------------------------------------------------
# Generation
# ---------------------------------------------------------------------------


class GenerateKeyframeVariationsBody(_KfBody):
    keyframeId: str = Field(...)
    count: int = Field(default=4)


class GenerateKeyframeCandidatesBody(_KfBody):
    keyframeId: str | None = Field(default=None)
    count: int = Field(default=4)
    refinementPrompt: str | None = Field(default=None)
    freeform: bool = Field(default=False)


class GenerateSlotKeyframeCandidatesBody(_KfBody):
    transitionId: str | None = Field(default=None)


class SuggestKeyframePromptsBody(_KfBody):
    sectionLabel: str = Field(default="")
    sectionContent: str = Field(default="")
    events: list[dict[str, Any]] = Field(...)
    baseStillName: str = Field(default="")


class EnhanceKeyframePromptBody(_KfBody):
    prompt: str = Field(...)
    sectionContent: str = Field(default="")
    event: dict[str, Any] | None = Field(default=None)


# ---------------------------------------------------------------------------
# Catch-all update (chat-tool alignment)
# ---------------------------------------------------------------------------


class UpdateKeyframeBody(_KfBody):
    """Chat-tool alignment route: accepts any subset of updatable fields.

    Delegates to ``chat._exec_update_keyframe`` via the wrapper below.
    """

    keyframe_id: str = Field(...)


__all__ = [
    "AddKeyframeBody",
    "AssignKeyframeImageBody",
    "BatchDeleteKeyframesBody",
    "BatchSetBaseImageBody",
    "DeleteKeyframeBody",
    "DuplicateKeyframeBody",
    "EnhanceKeyframePromptBody",
    "EscalateKeyframeBody",
    "GenerateKeyframeCandidatesBody",
    "GenerateKeyframeVariationsBody",
    "GenerateSlotKeyframeCandidatesBody",
    "InsertPoolItemBody",
    "PasteGroupBody",
    "RestoreKeyframeBody",
    "SelectKeyframesBody",
    "SelectSlotKeyframesBody",
    "SetBaseImageBody",
    "SuggestKeyframePromptsBody",
    "UnlinkKeyframeBody",
    "UpdateKeyframeBody",
    "UpdateKeyframeLabelBody",
    "UpdateKeyframeStyleBody",
    "UpdatePromptBody",
    "UpdateTimestampBody",
]
