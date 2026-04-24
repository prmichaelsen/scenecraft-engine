"""Pydantic request bodies for transition routes (M16 T61).

Mirror the keyframe models — ``extra="ignore"`` everywhere,
required fields match the legacy ``body.get()`` checks so T58's
validation envelope emits ``"Missing '<field>'"`` with the same
text legacy clients already parse.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class _TrBody(BaseModel):
    model_config = ConfigDict(extra="ignore")


# ---------------------------------------------------------------------------
# Selection / trim / move
# ---------------------------------------------------------------------------


class SelectTransitionsBody(_TrBody):
    selections: dict[str, Any] = Field(...)


class UpdateTransitionTrimBody(_TrBody):
    transitionId: str = Field(...)


class ClipTrimEdgeBody(_TrBody):
    transitionId: str = Field(...)
    edge: str = Field(..., description="'l' or 'r'")
    delta: float = Field(...)


class MoveTransitionsBody(_TrBody):
    transitions: list[dict[str, Any]] = Field(...)


# ---------------------------------------------------------------------------
# Structural (lock-guarded)
# ---------------------------------------------------------------------------


class DeleteTransitionBody(_TrBody):
    transitionId: str = Field(...)


class BatchDeleteTransitionsBody(_TrBody):
    transition_ids: list[str] = Field(...)


class RestoreTransitionBody(_TrBody):
    transitionId: str = Field(...)


class SplitTransitionBody(_TrBody):
    transitionId: str = Field(...)
    atTime: str | float | int = Field(...)


# ---------------------------------------------------------------------------
# Action / remap / generate / enhance / style / label
# ---------------------------------------------------------------------------


class UpdateTransitionActionBody(_TrBody):
    transitionId: str = Field(...)
    action: str | None = Field(default=None)


class UpdateTransitionRemapBody(_TrBody):
    transitionId: str = Field(...)
    remap: dict[str, Any] = Field(...)


class GenerateTransitionActionBody(_TrBody):
    transitionId: str = Field(...)


class EnhanceTransitionActionBody(_TrBody):
    transitionId: str = Field(...)


class UpdateTransitionStyleBody(_TrBody):
    transitionId: str = Field(...)


class UpdateTransitionLabelBody(_TrBody):
    transitionId: str = Field(...)
    label: str | None = Field(default=None)
    tags: list[str] | None = Field(default=None)


# ---------------------------------------------------------------------------
# Copy / duplicate / link audio / generate
# ---------------------------------------------------------------------------


class CopyTransitionStyleBody(_TrBody):
    sourceId: str = Field(...)
    targetId: str = Field(...)


class DuplicateTransitionVideoBody(_TrBody):
    sourceId: str = Field(...)
    targetId: str = Field(...)


class LinkAudioBody(_TrBody):
    """POST /transitions/{tr_id}/link-audio — body is an audio-clip selector."""

    audio_clip_id: str | None = Field(default=None)
    unlink: bool = Field(default=False)


class GenerateTransitionCandidatesBody(_TrBody):
    transitionId: str | None = Field(default=None)


# ---------------------------------------------------------------------------
# Transition effects (add/update/delete)
# ---------------------------------------------------------------------------


class TransitionEffectAddBody(_TrBody):
    transitionId: str = Field(...)
    type: str = Field(...)
    params: dict[str, Any] | None = Field(default=None)


class TransitionEffectUpdateBody(_TrBody):
    effectId: str = Field(...)


class TransitionEffectDeleteBody(_TrBody):
    effectId: str = Field(...)


# ---------------------------------------------------------------------------
# Catch-all update
# ---------------------------------------------------------------------------


class UpdateTransitionBody(_TrBody):
    """Chat-tool alignment route."""

    transition_id: str = Field(...)


__all__ = [
    "BatchDeleteTransitionsBody",
    "ClipTrimEdgeBody",
    "CopyTransitionStyleBody",
    "DeleteTransitionBody",
    "DuplicateTransitionVideoBody",
    "EnhanceTransitionActionBody",
    "GenerateTransitionActionBody",
    "GenerateTransitionCandidatesBody",
    "LinkAudioBody",
    "MoveTransitionsBody",
    "RestoreTransitionBody",
    "SelectTransitionsBody",
    "SplitTransitionBody",
    "TransitionEffectAddBody",
    "TransitionEffectDeleteBody",
    "TransitionEffectUpdateBody",
    "UpdateTransitionActionBody",
    "UpdateTransitionBody",
    "UpdateTransitionLabelBody",
    "UpdateTransitionRemapBody",
    "UpdateTransitionStyleBody",
    "UpdateTransitionTrimBody",
]
