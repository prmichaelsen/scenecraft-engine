"""Pydantic request bodies for transition routes (M16 T61/T65).

Mirror the keyframe models -- ``extra="ignore"`` everywhere,
required fields match the legacy ``body.get()`` checks so T58's
validation envelope emits ``"Missing '<field>'"`` with the same
text legacy clients already parse.

T65 native-port update: models expanded to include all fields the
native handlers actually read from the body, so ``model_dump()``
preserves them (``extra="ignore"`` drops undeclared fields).
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
    trimIn: float | None = Field(default=None)
    trimOut: float | None = Field(default=None)
    fromKfTimestamp: str | None = Field(default=None)
    toKfTimestamp: str | None = Field(default=None)


class ClipTrimEdgeBody(_TrBody):
    transitionId: str = Field(...)
    edge: str = Field(..., description="'right' or 'left'")
    newBoundaryTimestamp: str | float | int = Field(...)
    newTrim: float | int = Field(...)
    mode: str = Field(default="trim", description="'trim' (default) or 'ripple'")


class MoveTransitionsBody(_TrBody):
    mode: str = Field(default="move", description="'move' or 'copy'")
    trackDelta: int = Field(...)
    timeDeltaSeconds: float = Field(...)
    transitionIds: list[str] = Field(...)
    autoCreateTracks: bool = Field(default=True)


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
    useGlobalPrompt: bool | None = Field(default=None)
    includeSectionDesc: bool | None = Field(default=None)
    negativePrompt: str | None = Field(default=None)
    seed: int | None = Field(default=None)
    ingredients: list[str] | None = Field(default=None)


class UpdateTransitionRemapBody(_TrBody):
    transitionId: str = Field(...)
    targetDuration: float | None = Field(default=None)
    method: str | None = Field(default=None)
    curvePoints: list[Any] | None = Field(default=None)


class GenerateTransitionActionBody(_TrBody):
    transitionId: str = Field(...)
    sectionContext: str | None = Field(default=None)


class EnhanceTransitionActionBody(_TrBody):
    transitionId: str = Field(...)
    action: str = Field(default="")
    sectionContext: str | None = Field(default=None)


class UpdateTransitionStyleBody(_TrBody):
    """All style fields are optional; only supplied keys are written to the DB."""

    transitionId: str = Field(...)
    blendMode: str | None = Field(default=None)
    opacity: float | None = Field(default=None)
    opacityCurve: Any = Field(default=None)
    redCurve: Any = Field(default=None)
    greenCurve: Any = Field(default=None)
    blueCurve: Any = Field(default=None)
    blackCurve: Any = Field(default=None)
    hueShiftCurve: Any = Field(default=None)
    saturationCurve: Any = Field(default=None)
    invertCurve: Any = Field(default=None)
    brightnessCurve: Any = Field(default=None)
    contrastCurve: Any = Field(default=None)
    exposureCurve: Any = Field(default=None)
    maskCenterX: float | None = Field(default=None)
    maskCenterY: float | None = Field(default=None)
    maskRadius: float | None = Field(default=None)
    maskFeather: float | None = Field(default=None)
    transformX: float | None = Field(default=None)
    transformY: float | None = Field(default=None)
    transformXCurve: Any = Field(default=None)
    transformYCurve: Any = Field(default=None)
    transformZCurve: Any = Field(default=None)
    chromaKey: Any = Field(default=None)
    isAdjustment: bool | int | None = Field(default=None)
    hidden: bool | int | None = Field(default=None)
    anchorX: float | None = Field(default=None)
    anchorY: float | None = Field(default=None)


class UpdateTransitionLabelBody(_TrBody):
    transitionId: str = Field(...)
    label: str | None = Field(default=None)
    labelColor: str | None = Field(default=None)
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
    """POST /transitions/{tr_id}/link-audio.

    Legacy body: ``{ "replace": false }`` -- true to swap an existing link
    (use when the selected video changes, e.g. Veo completion).
    ``force`` is an alias for ``replace`` kept for backwards compat.
    """

    replace: bool = Field(default=False)
    force: bool = Field(default=False)


class GenerateTransitionCandidatesBody(_TrBody):
    transitionId: str = Field(...)
    count: int = Field(default=4)
    slotIndex: int | None = Field(default=None)
    duration: int | None = Field(default=None)
    useNextTransitionFrame: bool = Field(default=False)
    noEndFrame: bool = Field(default=False)
    generateAudio: bool = Field(default=False)
    ingredients: list[str] | None = Field(default=None)
    negativePrompt: str | None = Field(default=None)
    seed: int | None = Field(default=None)


# ---------------------------------------------------------------------------
# Transition effects (add/update/delete)
# ---------------------------------------------------------------------------


class TransitionEffectAddBody(_TrBody):
    transitionId: str = Field(...)
    type: str = Field(...)
    params: dict[str, Any] | None = Field(default=None)


class TransitionEffectUpdateBody(BaseModel):
    """Update body uses ``id`` (not ``effectId``) per legacy wire format.

    ``extra="allow"`` because the remaining fields are passed through
    as ``**kwargs`` to ``db.update_transition_effect``.
    """

    model_config = ConfigDict(extra="allow")
    id: str = Field(...)


class TransitionEffectDeleteBody(_TrBody):
    """Delete body uses ``id`` per legacy wire format."""

    id: str = Field(...)


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
