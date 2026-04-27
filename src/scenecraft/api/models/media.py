"""Pydantic models for rendering / pool / candidates routes (M16 T63).

The body-shape is the FastAPI-port of the body dicts the legacy
handlers expected. Field names mirror the on-wire JSON exactly
(``poolSegmentId`` not ``pool_segment_id``) so front-ends stay
unchanged after the cutover.

The models use ``model_config = ConfigDict(extra="ignore")`` for the
POST bodies — the legacy server silently dropped unknown keys, and
preserving that behavior avoids breaking clients that send forward-
compatible fields the old server didn't know about.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class _Loose(BaseModel):
    """Base that mirrors legacy "accept-unknown-keys" JSON bodies."""

    model_config = ConfigDict(extra="ignore")


# ---------------------------------------------------------------------------
# rendering.py query params
# ---------------------------------------------------------------------------


class RenderFrameQuery(BaseModel):
    """Query shape for ``GET /render-frame``.

    Not strictly used by the router (FastAPI parses query params directly),
    but defining it documents the endpoint in OpenAPI with the right field
    types. Quality is clamped server-side to 1..100 matching legacy.
    """

    t: float = 0.0
    quality: int = 85


class FilmstripQuery(BaseModel):
    t: float = 0.0
    height: int = 48


class DownloadPreviewQuery(BaseModel):
    start: float = 0.0
    end: float = 0.0


# ---------------------------------------------------------------------------
# pool.py bodies
# ---------------------------------------------------------------------------


class PoolAddBody(_Loose):
    sourcePath: str
    type: str = "transition"


class PoolImportBody(_Loose):
    # sourcePath OR filepath (legacy accepted both)
    sourcePath: str | None = None
    filepath: str | None = None
    label: str = ""


class PoolUploadForm(_Loose):
    """Multipart form fields for ``pool/upload``.

    The ``file`` field is parsed separately via ``UploadFile``; this
    model covers the non-binary form fields so they can be documented
    in OpenAPI. Keep in sync with ``_handle_pool_upload`` in legacy.
    """

    label: str = ""
    originalFilepath: str = ""


class PoolRenameBody(_Loose):
    poolSegmentId: str
    label: str = ""


class PoolTagBody(_Loose):
    poolSegmentId: str
    tag: str


class PoolUntagBody(_Loose):
    poolSegmentId: str
    tag: str


class AssignPoolVideoBody(_Loose):
    transitionId: str
    poolSegmentId: str | None = None
    poolPath: str | None = None  # legacy fallback


# ---------------------------------------------------------------------------
# candidates.py bodies
# ---------------------------------------------------------------------------


class PromoteStagedBody(_Loose):
    keyframeId: str
    stagingId: str
    variant: int = 1


class GenerateStagedBody(_Loose):
    prompt: str
    stillName: str
    stagingId: str
    count: int = 1


# ---------------------------------------------------------------------------
# effects.py body — user-authored effects (transitions/keyframes), NOT audio
# ---------------------------------------------------------------------------


class EffectsBody(_Loose):
    # Both lists are opaque JSON-shaped blobs passed through to db.save_effects.
    effects: list[Any] = Field(default_factory=list)
    suppressions: list[Any] | None = None


__all__ = [
    "RenderFrameQuery",
    "FilmstripQuery",
    "DownloadPreviewQuery",
    "PoolAddBody",
    "PoolImportBody",
    "PoolUploadForm",
    "PoolRenameBody",
    "PoolTagBody",
    "PoolUntagBody",
    "AssignPoolVideoBody",
    "PromoteStagedBody",
    "GenerateStagedBody",
    "EffectsBody",
]
