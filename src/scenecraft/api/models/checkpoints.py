"""Pydantic bodies for the checkpoints + undo router (M16 T64).

Kept tiny on purpose — the legacy handlers accept ``body.get("name", "")``
and ``body.get("filename", "")`` with no validation beyond "string or
missing". The models preserve that tolerance by marking fields optional
with sensible defaults so existing clients don't get surprise 400s on
empty payloads.
"""

from __future__ import annotations

from pydantic import BaseModel


class CheckpointCreateBody(BaseModel):
    """POST /api/projects/{name}/checkpoint body.

    Legacy accepts ``{}`` — callers often create un-named snapshots from
    a keyboard shortcut that can't prompt for a label.
    """

    name: str = ""


class CheckpointRestoreBody(BaseModel):
    """POST /api/projects/{name}/checkpoint/restore body.

    ``filename`` must start with ``project.db.checkpoint-`` and resolve
    to a file in the project dir — the handler enforces that; the model
    only polices the shape.
    """

    filename: str


class CheckpointDeleteBody(BaseModel):
    """POST /api/projects/{name}/checkpoint/delete body — same shape as restore."""

    filename: str


__all__ = [
    "CheckpointCreateBody",
    "CheckpointRestoreBody",
    "CheckpointDeleteBody",
]
