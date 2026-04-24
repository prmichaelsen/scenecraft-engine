"""Rendering router — ``/render-frame`` hot path + render-state + cache stats +
thumbnails + filmstrip + download-preview (M16 T63).

``/render-frame`` byte-parity contract (spec R4 + task-63 §Notes):
  * Identical cv2.imencode call path as legacy ``_handle_render_frame``.
  * Same ``global_cache`` instance, so MISS/HIT behaviour stays identical.
  * Same ``schedule`` built via ``build_schedule`` — no custom shortcut.
  * Headers: ``Content-Type: image/jpeg``, ``Cache-Control: no-store``,
    ``X-Scenecraft-Cache: MISS|HIT`` (legacy parity).
  * Response body is the raw ``bytes`` object cv2 returned — NEVER
    re-encoded, NEVER wrapped in a StreamingResponse that might apply
    transfer encoding transforms that differ from stdlib ``wfile.write``.

The "don't re-encode" discipline matters because the frontend
``<PreviewViewport>`` caches these JPEGs on an opaque key and would
paint subtly different pixels if the encoder drifted. We therefore use
``Response(content=jpeg_bytes, media_type="image/jpeg")`` — the
simplest shape that reaches the wire identically.
"""

from __future__ import annotations

import json
import subprocess as sp
from pathlib import Path

from fastapi import APIRouter, Depends, Request, status
from fastapi.responses import Response, StreamingResponse

from scenecraft.api.deps import current_user, project_dir
from scenecraft.api.errors import ApiError

router = APIRouter(tags=["rendering"], dependencies=[Depends(current_user)])


# ---------------------------------------------------------------------------
# /render-frame — hot path
# ---------------------------------------------------------------------------


@router.get(
    "/api/projects/{name}/render-frame",
    operation_id="get_render_frame",
    summary="Render a single JPEG frame at time t (cached).",
    responses={
        200: {
            "content": {"image/jpeg": {}},
            "description": "JPEG bytes. Headers: X-Scenecraft-Cache: MISS|HIT.",
        },
        400: {"description": "Invalid t query param."},
        404: {"description": "Project not found or no renderable content."},
    },
)
async def get_render_frame(
    name: str,
    request: Request,
    t: float = 0.0,
    quality: int = 85,
    pdir: Path = Depends(project_dir),
):
    """Render a frame at time ``t`` seconds and return its JPEG bytes.

    Cache-first — the process-global ``frame_cache`` is keyed on
    ``(project_dir, mtime, t, quality)`` so repeat scrubs don't
    re-render. ``quality`` is clamped to ``[1, 100]``; any value
    outside that range is silently clipped (legacy parity).

    Why this handler doesn't use ``StreamingResponse``:
      The JPEG payload is always small (< 200 KB at q=100). Starlette's
      ``Response(content=bytes)`` sends the exact byte string with a
      single ``Content-Length`` header — no chunked transfer, no body
      transformations. That matches the legacy
      ``self.wfile.write(cached_jpeg)`` path byte-for-byte.
    """
    # Clamp quality to 1..100 — matches legacy clamp.
    if quality < 1:
        quality = 1
    if quality > 100:
        quality = 100

    try:
        import cv2  # type: ignore

        from scenecraft.render.compositor import render_frame_at
        from scenecraft.render.frame_cache import global_cache
        from scenecraft.render.schedule import build_schedule
    except ImportError as e:
        raise ApiError(
            "INTERNAL_ERROR",
            f"Render dependencies not installed: {e}",
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )

    cached_jpeg = global_cache.get(pdir, t, quality)
    cache_status = "HIT" if cached_jpeg is not None else "MISS"

    if cached_jpeg is None:
        try:
            schedule = build_schedule(pdir)
        except Exception as e:
            raise ApiError(
                "INTERNAL_ERROR",
                f"build_schedule failed: {e}",
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

        if schedule.duration_seconds <= 0 or not schedule.segments:
            raise ApiError(
                "NO_CONTENT",
                "Project has no renderable content yet",
                status_code=status.HTTP_404_NOT_FOUND,
            )

        # Same clamp legacy applies: clip t into [0, duration - 1/fps].
        t = max(0.0, min(t, schedule.duration_seconds - 1.0 / schedule.fps))

        try:
            frame = render_frame_at(schedule, t, frame_cache={}, scrub=True)
        except Exception as e:
            raise ApiError(
                "INTERNAL_ERROR",
                f"render_frame_at failed: {e}",
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

        ok, buf = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), quality])
        if not ok:
            raise ApiError(
                "INTERNAL_ERROR",
                "JPEG encode failed",
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )
        cached_jpeg = bytes(buf)
        global_cache.put(pdir, t, quality, cached_jpeg)

    return Response(
        content=cached_jpeg,
        media_type="image/jpeg",
        headers={
            "Cache-Control": "no-store",
            "X-Scenecraft-Cache": cache_status,
        },
    )


# ---------------------------------------------------------------------------
# /render-state — worker snapshot
# ---------------------------------------------------------------------------


@router.get(
    "/api/projects/{name}/render-state",
    operation_id="get_render_state",
    summary="Per-bucket render state snapshot for the timeline UI bar.",
)
async def get_render_state(name: str, pdir: Path = Depends(project_dir)) -> dict:
    from scenecraft.render.render_state import snapshot_for_worker

    return snapshot_for_worker(pdir)


# ---------------------------------------------------------------------------
# /api/render-cache/stats
# ---------------------------------------------------------------------------


@router.get(
    "/api/render-cache/stats",
    operation_id="get_render_cache_stats",
    summary="Frame-cache + fragment-cache stats (scrub perf + playback cache).",
)
async def get_render_cache_stats() -> dict:
    from scenecraft.render.frame_cache import global_cache
    from scenecraft.render.fragment_cache import global_fragment_cache

    return {
        "frame_cache": global_cache.stats(),
        "fragment_cache": global_fragment_cache.stats(),
    }


# ---------------------------------------------------------------------------
# /thumb/{path:path} — resized image thumbnail (cached)
# ---------------------------------------------------------------------------


_IMAGE_EXTS = frozenset({".png", ".jpg", ".jpeg", ".webp"})


@router.get(
    "/api/projects/{name}/thumb/{file_path:path}",
    operation_id="get_thumb",
    summary="Resized image thumbnail (256x256 max, JPEG, cached on disk).",
)
async def get_thumb(name: str, file_path: str, pdir: Path = Depends(project_dir)):
    """Mirror of legacy ``_handle_image_thumb``.

    Cache dir ``.thumbs/`` mirrors the source path one-for-one. Regens
    if source mtime is newer than the cached thumbnail.
    """
    source = pdir / file_path
    if not source.exists() or source.suffix.lower() not in _IMAGE_EXTS:
        raise ApiError("NOT_FOUND", f"Image not found: {file_path}", status_code=404)

    thumb_dir = pdir / ".thumbs" / Path(file_path).parent
    thumb_dir.mkdir(parents=True, exist_ok=True)
    thumb_path = thumb_dir / source.name

    if not thumb_path.exists() or thumb_path.stat().st_mtime < source.stat().st_mtime:
        from PIL import Image as _PILImage  # type: ignore

        with _PILImage.open(str(source)) as img:
            img.thumbnail((256, 256), _PILImage.LANCZOS)
            img.save(str(thumb_path), "JPEG", quality=80)

    data = thumb_path.read_bytes()
    return Response(
        content=data,
        media_type="image/jpeg",
        headers={"Cache-Control": "public, max-age=86400"},
    )


# ---------------------------------------------------------------------------
# /thumbnail/{path:path} — first-frame JPEG for a video file
# ---------------------------------------------------------------------------


@router.get(
    "/api/projects/{name}/thumbnail/{file_path:path}",
    operation_id="get_thumbnail",
    summary="First-frame JPEG for a video (cached on disk next to source).",
)
async def get_thumbnail(name: str, file_path: str, request: Request):
    """Mirror of legacy ``_handle_video_thumbnail``.

    Path traversal is guarded by reusing ``work_dir`` as root — the
    project_dir dep alone doesn't catch ``../`` segments in
    ``file_path``. Re-resolve here and verify.
    """
    work_dir: Path | None = getattr(request.app.state, "work_dir", None)
    if work_dir is None:
        raise ApiError("INTERNAL_ERROR", "work_dir missing", status_code=500)

    full_path = (work_dir / name / file_path).resolve()
    try:
        full_path.relative_to(work_dir.resolve())
    except ValueError:
        raise ApiError("FORBIDDEN", "Path traversal denied", status_code=403)
    if not full_path.exists():
        raise ApiError("NOT_FOUND", f"File not found: {file_path}", status_code=404)

    thumb_path = full_path.with_suffix(".thumb.jpg")
    if not thumb_path.exists():
        try:
            sp.run(
                [
                    "ffmpeg", "-y", "-i", str(full_path), "-vframes", "1",
                    "-vf", "scale=320:-1", "-q:v", "4", str(thumb_path),
                ],
                capture_output=True, timeout=10,
            )
        except Exception:
            pass

    if not thumb_path.exists():
        raise ApiError("INTERNAL_ERROR", "Failed to generate thumbnail", status_code=500)

    data = thumb_path.read_bytes()
    return Response(
        content=data,
        media_type="image/jpeg",
        headers={"Cache-Control": "public, max-age=86400"},
    )


# ---------------------------------------------------------------------------
# /transitions/{tr_id}/filmstrip
# ---------------------------------------------------------------------------


@router.get(
    "/api/projects/{name}/transitions/{tr_id}/filmstrip",
    operation_id="get_filmstrip",
    summary="Extract + cache a frame from a transition's selected video.",
)
async def get_filmstrip(
    name: str,
    tr_id: str,
    t: float = 0.0,
    height: int = 48,
    pdir: Path = Depends(project_dir),
):
    """Mirror of legacy ``_handle_transition_filmstrip``.

    Cache key = ``(tr_id, video_mtime, t_ms, height)`` → ``.filmstrip/``.
    """
    try:
        t_seconds = max(0.0, float(t))
        h = max(16, min(256, int(height)))
    except (ValueError, TypeError):
        raise ApiError("BAD_REQUEST", "t and height must be numbers", status_code=400)

    sel_dir = pdir / "selected_transitions"
    video_path = sel_dir / f"{tr_id}_slot_0.mp4"
    if not video_path.exists():
        raise ApiError(
            "NOT_FOUND", f"No selected video for transition {tr_id}", status_code=404
        )

    mtime = int(video_path.stat().st_mtime)
    t_ms = int(round(t_seconds * 1000))
    cache_dir = sel_dir / ".filmstrip"
    cache_dir.mkdir(exist_ok=True)
    cache_path = cache_dir / f"{tr_id}_{mtime}_{t_ms}_{h}.jpg"

    if not cache_path.exists():
        try:
            sp.run(
                [
                    "ffmpeg", "-y", "-ss", str(t_seconds), "-i", str(video_path),
                    "-frames:v", "1", "-vf", f"scale=-2:{h}", "-q:v", "5",
                    str(cache_path),
                ],
                capture_output=True, timeout=15,
            )
        except Exception:
            pass

    if not cache_path.exists():
        raise ApiError(
            "INTERNAL_ERROR", "Failed to generate filmstrip frame", status_code=500
        )

    data = cache_path.read_bytes()
    return Response(
        content=data,
        media_type="image/jpeg",
        headers={"Cache-Control": "public, max-age=86400, immutable"},
    )


# ---------------------------------------------------------------------------
# /download-preview — legacy-preserving stub
# ---------------------------------------------------------------------------


@router.get(
    "/api/projects/{name}/download-preview",
    operation_id="download_preview",
    summary="Stream a generated download-preview video (legacy handler missing).",
)
async def download_preview(
    name: str,
    start: float = 0.0,
    end: float = 0.0,
    pdir: Path = Depends(project_dir),
):
    """The legacy ``_handle_download_preview`` was never implemented.

    The route existed in the legacy ``do_GET`` dispatch table but its
    handler was never written — calling the endpoint on the legacy
    server raises ``AttributeError`` which becomes a plain 500. We
    preserve that behaviour here by raising the same envelope so
    byte-parity clients don't see a shape change during cutover.

    T64 (sibling) or a later task will land the real streaming
    implementation once design lands.
    """
    raise ApiError(
        "INTERNAL_ERROR",
        "download-preview handler not implemented in legacy — see task-63 note",
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
    )
