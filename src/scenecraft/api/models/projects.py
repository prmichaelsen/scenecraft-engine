"""Pydantic models for M16 T60 — projects + misc routers.

Every model opts into ``extra="ignore"`` per spec R10 so clients
passing extra fields (older or newer frontends) don't get BAD_REQUEST
envelopes the legacy server never produced.

Response models are deliberately sparse — most legacy handlers return
freeform dicts keyed on whichever DB columns exist at runtime. Pinning
a Pydantic response model with ``model_config[extra]="ignore"`` would
silently strip fields the frontend relies on. Handlers return raw
dicts; FastAPI emits them verbatim. The router-level parity test in
T65 still covers response shape.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict


_PERMISSIVE = ConfigDict(extra="ignore")


# ---------------------------------------------------------------------------
# Project CRUD / meta / import / still / extend
# ---------------------------------------------------------------------------


class CreateProjectBody(BaseModel):
    model_config = _PERMISSIVE
    name: str | None = None
    fps: int | None = None
    resolution: list[int] | None = None
    motionPrompt: str | None = None
    defaultTransitionPrompt: str | None = None


class UpdateMetaBody(BaseModel):
    model_config = _PERMISSIVE
    motion_prompt: str | None = None
    default_transition_prompt: str | None = None
    image_model: str | None = None


class SaveAsStillBody(BaseModel):
    model_config = _PERMISSIVE
    sourcePath: str | None = None
    name: str | None = None


class ImportBody(BaseModel):
    model_config = _PERMISSIVE
    sourcePath: str | None = None
    timestamp: str | None = None


class ExtendVideoBody(BaseModel):
    model_config = _PERMISSIVE
    transitionId: str | None = None
    videoPath: str | None = None


# ---------------------------------------------------------------------------
# Workspace views
# ---------------------------------------------------------------------------


class WorkspaceViewBody(BaseModel):
    model_config = _PERMISSIVE
    layout: Any = None  # Freeform JSON — frontend-managed panel tree


# ---------------------------------------------------------------------------
# Narrative / watched folders
# ---------------------------------------------------------------------------


class NarrativeBody(BaseModel):
    model_config = _PERMISSIVE
    sections: list[dict] | None = None


class WatchFolderBody(BaseModel):
    model_config = _PERMISSIVE
    folderPath: str | None = None


# ---------------------------------------------------------------------------
# Branches / checkout
# ---------------------------------------------------------------------------


class BranchCreateBody(BaseModel):
    model_config = _PERMISSIVE
    name: str | None = None
    fromBranch: str | None = None


class BranchDeleteBody(BaseModel):
    model_config = _PERMISSIVE
    name: str | None = None


class CheckoutBody(BaseModel):
    model_config = _PERMISSIVE
    branch: str | None = None
    force: bool | None = None


# ---------------------------------------------------------------------------
# Settings / section-settings / ingredients
# ---------------------------------------------------------------------------


class SettingsBody(BaseModel):
    model_config = _PERMISSIVE
    preview_quality: int | None = None
    render_preview_fps: int | None = None
    preview_scale_factor: float | None = None


class SectionSettingsBody(BaseModel):
    model_config = _PERMISSIVE
    sectionLabel: str | None = None
    still: Any = None
    suggestions: Any = None


class IngredientsPromoteBody(BaseModel):
    model_config = _PERMISSIVE
    sourceType: str | None = None
    sourcePath: str | None = None
    label: str | None = None


class IngredientsRemoveBody(BaseModel):
    model_config = _PERMISSIVE
    ingredientId: str | None = None


class IngredientsUpdateBody(BaseModel):
    model_config = _PERMISSIVE
    ingredientId: str | None = None
    label: str | None = None


# ---------------------------------------------------------------------------
# Bench
# ---------------------------------------------------------------------------


class BenchCaptureBody(BaseModel):
    model_config = _PERMISSIVE
    time: float | None = None
    trackId: str | None = None


class BenchAddBody(BaseModel):
    model_config = _PERMISSIVE
    type: str | None = None
    entityId: str | None = None
    sourcePath: str | None = None
    label: str | None = None


class BenchRemoveBody(BaseModel):
    model_config = _PERMISSIVE
    benchId: str | None = None


# ---------------------------------------------------------------------------
# Markers
# ---------------------------------------------------------------------------


class MarkerAddBody(BaseModel):
    model_config = _PERMISSIVE
    id: str | None = None
    time: float | None = None
    label: str | None = None
    type: str | None = None


class MarkerUpdateBody(BaseModel):
    model_config = _PERMISSIVE
    id: str | None = None
    time: float | None = None
    label: str | None = None
    type: str | None = None


class MarkerRemoveBody(BaseModel):
    model_config = _PERMISSIVE
    id: str | None = None


# ---------------------------------------------------------------------------
# Prompt roster
# ---------------------------------------------------------------------------


class PromptRosterAddBody(BaseModel):
    model_config = _PERMISSIVE
    id: str | None = None
    name: str | None = None
    template: str | None = None
    category: str | None = None


class PromptRosterUpdateBody(BaseModel):
    model_config = _PERMISSIVE
    id: str | None = None
    name: str | None = None
    template: str | None = None
    category: str | None = None


class PromptRosterRemoveBody(BaseModel):
    model_config = _PERMISSIVE
    id: str | None = None


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


class UpdateConfigBody(BaseModel):
    model_config = _PERMISSIVE
    projects_dir: str | None = None


__all__ = [
    "BenchAddBody",
    "BenchCaptureBody",
    "BenchRemoveBody",
    "BranchCreateBody",
    "BranchDeleteBody",
    "CheckoutBody",
    "CreateProjectBody",
    "ExtendVideoBody",
    "ImportBody",
    "IngredientsPromoteBody",
    "IngredientsRemoveBody",
    "IngredientsUpdateBody",
    "MarkerAddBody",
    "MarkerRemoveBody",
    "MarkerUpdateBody",
    "NarrativeBody",
    "PromptRosterAddBody",
    "PromptRosterRemoveBody",
    "PromptRosterUpdateBody",
    "SaveAsStillBody",
    "SectionSettingsBody",
    "SettingsBody",
    "UpdateConfigBody",
    "UpdateMetaBody",
    "WatchFolderBody",
    "WorkspaceViewBody",
]
