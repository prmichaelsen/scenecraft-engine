"""Files router — ``GET`` + ``HEAD`` ``/api/projects/{name}/files/{file_path:path}``.

The two ``operation_id``s on this router (``get_project_file`` and
``head_project_file``) are load-bearing for the Phase B tool codegen
(T66-T68) — renaming them breaks the generated ``chat_tools.py``.

Path traversal: we resolve ``(work_dir / name / file_path)`` and require
the result to live under ``work_dir.resolve()``. Any escape attempt
(``..``, symlinks, absolute segments) falls through to the legacy
404 + envelope response (R22).
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Request, status

from scenecraft.api.errors import ApiError
from scenecraft.api.streaming import file_response_with_range

router = APIRouter(prefix="/api/projects", tags=["files"])


def _resolve_project_file(work_dir: Path, name: str, file_path: str) -> Path:
    """Resolve name/file_path under work_dir or raise NOT_FOUND.

    Matches legacy semantics: the resolved path must be under the
    work_dir root. ``str(full).startswith(str(work_dir.resolve()))``
    was the legacy check; we use ``Path.relative_to`` on resolved paths
    which is the same check under standard FS semantics and rejects
    trailing-slash edge cases correctly.
    """
    root = work_dir.resolve()
    candidate = (work_dir / name / file_path)
    try:
        resolved = candidate.resolve()
    except (OSError, RuntimeError):
        raise ApiError("NOT_FOUND", "File not found", status_code=status.HTTP_404_NOT_FOUND)
    try:
        resolved.relative_to(root)
    except ValueError:
        # Path escaped work_dir root — classic ../../etc/passwd attempt.
        raise ApiError("NOT_FOUND", "File not found", status_code=status.HTTP_404_NOT_FOUND)
    return resolved


@router.get(
    "/{name}/files/{file_path:path}",
    operation_id="get_project_file",
    summary="Serve a file from a project (Range-aware)",
    responses={
        200: {"content": {"application/octet-stream": {}}, "description": "Full file"},
        206: {"description": "Partial content (Range request)"},
        404: {"description": "File not found or path traversal rejected"},
        416: {"description": "Range not satisfiable"},
    },
)
async def get_project_file(name: str, file_path: str, request: Request):
    work_dir: Path | None = getattr(request.app.state, "work_dir", None)
    if work_dir is None:
        raise ApiError(
            "INTERNAL_ERROR",
            "File serving not configured (work_dir missing)",
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )
    full = _resolve_project_file(work_dir, name, file_path)
    return file_response_with_range(full, request)


@router.head(
    "/{name}/files/{file_path:path}",
    operation_id="head_project_file",
    summary="Metadata (Content-Length, Accept-Ranges) for a project file",
    responses={
        200: {"description": "Metadata only; empty body"},
        404: {"description": "File not found or path traversal rejected"},
    },
)
async def head_project_file(name: str, file_path: str, request: Request):
    work_dir: Path | None = getattr(request.app.state, "work_dir", None)
    if work_dir is None:
        raise ApiError(
            "INTERNAL_ERROR",
            "File serving not configured (work_dir missing)",
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )
    full = _resolve_project_file(work_dir, name, file_path)
    return file_response_with_range(full, request, head_only=True)
