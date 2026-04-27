"""Range-aware file streaming for the FastAPI app (M16 T57, spec R20-R22).

This is the single most fragile piece of the migration: browsers issue
``Range: bytes=0-`` on every ``<video>`` / ``<audio>`` element load, and
a naive ``Response(content=f.read())`` would OOM on multi-GB project
assets. The behavior mirrors the legacy ``api_server._handle_serve_file``
exactly:

  - Full GET            → 200 + Accept-Ranges + body bytes, chunked read
  - ``bytes=X-Y``       → 206 + Content-Range + streamed range
  - ``bytes=X-``        → 206 to end-of-file
  - Out-of-bounds start → 416 + ``Content-Range: bytes */<size>``
  - Suffix (``bytes=-N``) → 416 (legacy never supported this; preserve)
  - Path traversal      → 404 (resolved path must be under work_dir)

Chunk size is ``min(65536, remaining)`` — identical to the legacy
``f.read(min(65536, remaining))`` loop. 64 KiB is small enough that a
single request never buffers >64 KiB in userspace, and large enough
that syscall overhead is negligible for typical video scrubbing.
"""

from __future__ import annotations

import mimetypes
import re
from pathlib import Path
from typing import AsyncIterator, Iterator

from fastapi import HTTPException, Request, status
from starlette.responses import Response, StreamingResponse

from scenecraft.api.errors import ApiError


CHUNK_SIZE = 65536  # 64 KiB — matches legacy.

# Parser for ``Range: bytes=<start>-<end>`` only. Suffix ranges
# (``bytes=-N``) and multi-range requests (``bytes=0-10,20-30``) fall
# through to the 416 path by design — legacy behavior (R21).
_RANGE_RE = re.compile(r"^bytes=(\d+)-(\d*)$")


def _resolve_under(root: Path, candidate: Path) -> Path | None:
    """Return the resolved candidate iff it lives under root; else None.

    ``root`` must already be ``.resolve()``'d. Catches ``../`` traversal
    attempts at the FS-resolution layer — never trust path strings from
    clients, even after ``pathlib`` joins.
    """
    try:
        resolved = candidate.resolve()
    except (OSError, RuntimeError):
        return None
    try:
        resolved.relative_to(root)
    except ValueError:
        return None
    return resolved


def _iter_file_range(path: Path, start: int, length: int) -> Iterator[bytes]:
    """Yield ``length`` bytes starting at ``start``, 64 KiB at a time."""
    remaining = length
    with path.open("rb") as f:
        f.seek(start)
        while remaining > 0:
            chunk = f.read(min(CHUNK_SIZE, remaining))
            if not chunk:
                break
            yield chunk
            remaining -= len(chunk)


def _iter_file_full(path: Path) -> Iterator[bytes]:
    with path.open("rb") as f:
        while True:
            chunk = f.read(CHUNK_SIZE)
            if not chunk:
                break
            yield chunk


def _mime_for(path: Path) -> str:
    return mimetypes.guess_type(str(path))[0] or "application/octet-stream"


def file_response_with_range(
    full_path: Path,
    request: Request,
    *,
    head_only: bool = False,
) -> Response:
    """Serve ``full_path`` honoring any ``Range`` header on ``request``.

    ``full_path`` MUST already be resolved — callers validate path
    traversal before calling this. ``work_dir`` is pulled from
    ``request.app.state.work_dir``; if the caller forgot to set it, we
    refuse the request rather than silently serve arbitrary paths.
    """
    work_dir: Path | None = getattr(request.app.state, "work_dir", None)
    if work_dir is None:
        raise ApiError(
            "INTERNAL_ERROR",
            "File serving not configured (work_dir missing)",
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )

    # Defense in depth: re-verify the resolved path is under work_dir.
    # Callers also check this, but a second check here guarantees no
    # future router accidentally serves an unvetted path.
    if _resolve_under(work_dir.resolve(), full_path) is None:
        raise ApiError("NOT_FOUND", "File not found", status_code=status.HTTP_404_NOT_FOUND)

    if not full_path.exists() or not full_path.is_file():
        raise ApiError("NOT_FOUND", "File not found", status_code=status.HTTP_404_NOT_FOUND)

    size = full_path.stat().st_size
    media_type = _mime_for(full_path)

    range_header = request.headers.get("range") or request.headers.get("Range")
    if range_header:
        match = _RANGE_RE.match(range_header.strip())
        if not match:
            # Malformed / suffix range — legacy rejects with 416 (R21).
            raise ApiError(
                "RANGE_NOT_SATISFIABLE",
                "Invalid Range header",
                status_code=416,
                headers={"Content-Range": f"bytes */{size}"},
            )
        start = int(match.group(1))
        end_group = match.group(2)
        end = int(end_group) if end_group else size - 1
        if start >= size or end < start:
            raise ApiError(
                "RANGE_NOT_SATISFIABLE",
                "Range not satisfiable",
                status_code=416,
                headers={"Content-Range": f"bytes */{size}"},
            )
        end = min(end, size - 1)
        length = end - start + 1
        headers = {
            "Content-Range": f"bytes {start}-{end}/{size}",
            "Accept-Ranges": "bytes",
            "Content-Length": str(length),
        }
        if head_only:
            return Response(
                status_code=status.HTTP_206_PARTIAL_CONTENT,
                headers=headers,
                media_type=media_type,
            )
        return StreamingResponse(
            _iter_file_range(full_path, start, length),
            status_code=status.HTTP_206_PARTIAL_CONTENT,
            headers=headers,
            media_type=media_type,
        )

    # No Range → full file.
    full_headers = {
        "Accept-Ranges": "bytes",
        "Content-Length": str(size),
    }
    if head_only:
        return Response(
            status_code=status.HTTP_200_OK,
            headers=full_headers,
            media_type=media_type,
        )
    return StreamingResponse(
        _iter_file_full(full_path),
        status_code=status.HTTP_200_OK,
        headers=full_headers,
        media_type=media_type,
    )


# Silence unused import if asyncio iter types become relevant in T62.
_ = AsyncIterator


__all__ = ["file_response_with_range", "CHUNK_SIZE"]
