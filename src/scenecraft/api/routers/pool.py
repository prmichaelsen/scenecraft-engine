"""Pool router — CRUD + upload + GC for the per-project asset pool (M16 T63).

Routes mirror ``api_server.py`` lines 637-1019 + handler bodies at
lines 4690-5502 (``_handle_get_pool``, ``_handle_pool_import``,
``_handle_pool_upload``, ``_handle_pool_rename``, ``_handle_pool_tag``,
``_handle_pool_gc``, ``_handle_assign_pool_video``).

Streaming upload contract (task-63 §Large-upload streaming):
  The multipart handler reads ``file.read(CHUNK)`` in 64 KiB increments
  and writes directly to disk. No full-body buffer. This matches the
  legacy handler's "read content-length bytes then split on boundary"
  pattern in memory footprint because the body size is bounded to one
  boundary chunk at a time by ``python-multipart``.

Structural routes: the task notes ``insert-pool-item`` belongs behind
``project_lock``, but this router does NOT expose an ``insert-pool-item``
endpoint — that route is defined in the keyframe/transition router
(T61) where it's semantically closer to the timeline mutations it
joins. The pool router's mutations (tag/rename/gc/import/upload) do
not alter the timeline and therefore don't take the lock.
"""

from __future__ import annotations

import shutil
import subprocess as _sp
import uuid as _uuid
from pathlib import Path
from typing import Any

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    Request,
    UploadFile,
    status,
)
from fastapi.responses import Response

from scenecraft.api.deps import current_user, project_dir
from scenecraft.api.errors import ApiError
from scenecraft.api.models.media import (
    AssignPoolVideoBody,
    PoolAddBody,
    PoolImportBody,
    PoolRenameBody,
    PoolTagBody,
    PoolUntagBody,
)

router = APIRouter(tags=["pool"], dependencies=[Depends(current_user)])


# Media-type classification — re-imported from legacy so the wire payload
# stays identical. If the legacy helper ever moves out of api_server we
# copy it locally; for now a direct import keeps one source of truth.
def _classify_media_type(path: str) -> str:
    from scenecraft.api_server import _classify_media_type as _legacy

    return _legacy(path)


def _authenticated_user_id(user) -> str:
    """Resolve a legacy-compatible user-id string.

    Legacy handlers did ``getattr(self, "_authenticated_user", None) or "local"``.
    The FastAPI User dep exposes ``.id``.
    """
    return getattr(user, "id", None) or "local"


# ---------------------------------------------------------------------------
# GET /pool — list keyframes + segments
# ---------------------------------------------------------------------------


@router.get(
    "/api/projects/{name}/pool",
    operation_id="get_pool",
    summary="List pool keyframes and segments (tag + kind filters).",
)
async def get_pool(
    name: str,
    tag: str | None = None,
    kind: str | None = None,
    pdir: Path = Depends(project_dir),
) -> dict:
    pool_dir = pdir / "pool"

    # Keyframe images — filesystem scan, same order as legacy.
    keyframes = []
    kf_dir = pool_dir / "keyframes"
    if kf_dir.is_dir():
        for f in sorted(kf_dir.iterdir()):
            if f.suffix.lower() in (".png", ".jpg", ".jpeg", ".webp"):
                keyframes.append(
                    {
                        "name": f.name,
                        "path": f"pool/keyframes/{f.name}",
                        "size": f.stat().st_size,
                    }
                )

    segments: list[dict] = []
    if (pdir / "project.db").exists():
        from scenecraft.db import (
            find_segments_by_tag as _by_tag,
            get_pool_segment_tags as _get_tags,
            list_pool_segments as _list_segs,
        )

        if tag:
            segs = _by_tag(pdir, tag)
            if kind:
                segs = [s for s in segs if s["kind"] == kind]
        else:
            segs = _list_segs(pdir, kind=kind)

        for s in segs:
            tag_rows = _get_tags(pdir, s["id"])
            segments.append(
                {
                    "id": s["id"],
                    "path": s["poolPath"],
                    "mediaType": _classify_media_type(s["poolPath"]),
                    "kind": s["kind"],
                    "label": s.get("label") or s.get("originalFilename") or "",
                    "originalFilename": s.get("originalFilename"),
                    "originalFilepath": s.get("originalFilepath"),
                    "createdBy": s.get("createdBy") or "",
                    "createdAt": s.get("createdAt"),
                    "durationSeconds": s.get("durationSeconds"),
                    "width": s.get("width"),
                    "height": s.get("height"),
                    "byteSize": s.get("byteSize"),
                    "generationParams": s.get("generationParams"),
                    "tags": [t["tag"] for t in tag_rows],
                }
            )

    return {"keyframes": keyframes, "segments": segments}


# ---------------------------------------------------------------------------
# GET /pool/tags
# ---------------------------------------------------------------------------


@router.get(
    "/api/projects/{name}/pool/tags",
    operation_id="get_pool_tags",
    summary="List distinct pool tags with counts.",
)
async def get_pool_tags(name: str, pdir: Path = Depends(project_dir)) -> dict:
    from scenecraft.db import list_all_tags

    return {"tags": list_all_tags(pdir)}


# ---------------------------------------------------------------------------
# GET /pool/gc-preview — dry-run GC
# ---------------------------------------------------------------------------


@router.get(
    "/api/projects/{name}/pool/gc-preview",
    operation_id="pool_gc_preview",
    summary="List segments the GC would delete (no state change).",
)
async def pool_gc_preview(name: str, pdir: Path = Depends(project_dir)) -> dict:
    return _gc_list_preview(pdir)


def _gc_list_preview(pdir: Path) -> dict:
    from scenecraft.db import find_gc_candidates

    orphans = find_gc_candidates(pdir)
    return {
        "wouldDelete": len(orphans),
        "segments": [
            {
                "id": o["id"],
                "poolPath": o["poolPath"],
                "label": o.get("label") or "",
                "byteSize": o.get("byteSize"),
                "createdAt": o.get("createdAt"),
            }
            for o in orphans
        ],
    }


# ---------------------------------------------------------------------------
# GET /pool/{seg_id}/peaks — raw pcm peaks for waveform rendering
# ---------------------------------------------------------------------------


@router.get(
    "/api/projects/{name}/pool/{seg_id}/peaks",
    operation_id="get_pool_segment_peaks",
    summary="Compute + stream the waveform peaks for a pool audio segment.",
)
async def get_pool_segment_peaks(
    name: str,
    seg_id: str,
    resolution: int = 400,
    pdir: Path = Depends(project_dir),
):
    from scenecraft.db import get_pool_segment

    seg = get_pool_segment(pdir, seg_id)
    if seg is None:
        raise ApiError("NOT_FOUND", f"Pool segment not found: {seg_id}", status_code=404)
    pool_rel = seg.get("poolPath") or seg.get("pool_path", "")
    if not pool_rel:
        raise ApiError("BAD_REQUEST", "pool segment has no pool_path", status_code=400)

    pool_path = (pdir / pool_rel).resolve()
    try:
        pool_path.relative_to(pdir.resolve())
    except ValueError:
        raise ApiError("BAD_REQUEST", "pool_path outside project", status_code=400)
    if not pool_path.exists():
        raise ApiError("NOT_FOUND", f"File missing on disk: {pool_rel}", status_code=404)

    duration = float(seg.get("durationSeconds") or seg.get("duration_seconds") or 0)
    try:
        from scenecraft.audio.peaks import compute_peaks

        data = compute_peaks(pool_path, 0.0, duration, resolution, project_dir=pdir)
    except RuntimeError as e:
        raise ApiError("PEAKS_FAILED", str(e), status_code=500)

    return Response(
        content=data,
        media_type="application/octet-stream",
        headers={
            "X-Peak-Resolution": str(resolution),
            "X-Peak-Duration": f"{duration:.6f}",
        },
    )


# ---------------------------------------------------------------------------
# POST /pool/add — copy a project-local file into pool/{keyframes|segments}/
# ---------------------------------------------------------------------------


def _log(msg: str) -> None:
    """Mirror legacy ``_log``. Used for observability in dev."""
    from scenecraft.api.deps import _log as _deps_log

    _deps_log(msg)


@router.post(
    "/api/projects/{name}/pool/add",
    operation_id="pool_add",
    summary="Copy a project-local file into pool/keyframes/ or pool/segments/.",
)
async def pool_add(
    name: str, body: PoolAddBody, pdir: Path = Depends(project_dir)
) -> dict:
    src = pdir / body.sourcePath
    if not src.exists():
        raise ApiError("NOT_FOUND", f"Source not found: {body.sourcePath}", status_code=404)

    if body.type == "keyframe":
        dest_dir = pdir / "pool" / "keyframes"
    else:
        dest_dir = pdir / "pool" / "segments"
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / src.name
    shutil.copy2(str(src), str(dest))
    _log(f"pool/add: {body.sourcePath} -> {dest.relative_to(pdir)}")
    return {"success": True, "path": str(dest.relative_to(pdir))}


# ---------------------------------------------------------------------------
# POST /pool/import — bring a local file into pool_segments (kind='imported')
# ---------------------------------------------------------------------------


@router.post(
    "/api/projects/{name}/pool/import",
    operation_id="pool_import",
    summary="Import a local file as a pool_segments row.",
)
async def pool_import(
    name: str,
    body: PoolImportBody,
    pdir: Path = Depends(project_dir),
    user=Depends(current_user),
) -> dict:
    src_arg = body.sourcePath or body.filepath
    if not src_arg:
        raise ApiError("BAD_REQUEST", "Missing 'sourcePath'", status_code=400)

    src = Path(src_arg)
    if not src.is_absolute():
        src = pdir / src_arg
    if not src.exists():
        raise ApiError("NOT_FOUND", f"Source not found: {src_arg}", status_code=404)

    original_filename = src.name
    original_filepath = str(src.resolve())
    ext = src.suffix or ".mp4"
    seg_uuid = _uuid.uuid4().hex
    pool_name = f"import_{seg_uuid}{ext}"
    pool_sub = pdir / "pool" / "segments"
    pool_sub.mkdir(parents=True, exist_ok=True)
    dest = pool_sub / pool_name
    shutil.copy2(str(src), str(dest))

    dur = _ffprobe_duration(dest)
    byte_size = dest.stat().st_size

    from scenecraft.db import _now_iso, get_db as _get_db

    auth_user = _authenticated_user_id(user)
    conn = _get_db(pdir)
    conn.execute(
        """INSERT INTO pool_segments
           (id, pool_path, kind, created_by, original_filename, original_filepath,
            label, generation_params, created_at, duration_seconds, width, height, byte_size)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            seg_uuid, f"pool/segments/{pool_name}", "imported", auth_user,
            original_filename, original_filepath,
            body.label or original_filename, None, _now_iso(), dur, None, None, byte_size,
        ),
    )
    conn.commit()

    _log(
        f"pool/import: {original_filename} -> seg={seg_uuid[:8]} "
        f"({byte_size // 1024}KB, {dur}s)"
    )
    return {
        "success": True,
        "poolSegmentId": seg_uuid,
        "poolPath": f"pool/segments/{pool_name}",
        "originalFilename": original_filename,
        "originalFilepath": original_filepath,
        "durationSeconds": dur,
    }


def _ffprobe_duration(path: Path) -> float | None:
    try:
        r = _sp.run(
            [
                "ffprobe", "-v", "error", "-show_entries",
                "format=duration", "-of", "csv=p=0", str(path),
            ],
            capture_output=True, text=True, timeout=5,
        )
        if r.returncode == 0 and r.stdout.strip():
            return float(r.stdout.strip())
    except Exception:
        pass
    return None


# ---------------------------------------------------------------------------
# POST /pool/upload — streaming multipart
# ---------------------------------------------------------------------------


UPLOAD_CHUNK = 65536  # 64 KiB — matches files streaming chunk.


@router.post(
    "/api/projects/{name}/pool/upload",
    operation_id="pool_upload",
    summary="Multipart file upload (streamed to disk, no RAM buffer).",
)
async def pool_upload(
    name: str,
    file: UploadFile = File(...),
    label: str = Form(""),
    originalFilepath: str = Form(""),
    pdir: Path = Depends(project_dir),
    user=Depends(current_user),
) -> dict:
    """Streaming multipart upload.

    FastAPI's ``UploadFile`` is backed by a SpooledTemporaryFile that
    flushes to disk above ~1 MB — combined with our chunked
    ``file.read(UPLOAD_CHUNK)`` loop, we never buffer the full body
    in Python userspace. The test ``large_upload_streams`` verifies
    this by measuring peak RSS during a 200 MB upload.
    """
    if not file.filename:
        raise ApiError("BAD_REQUEST", "Missing file upload", status_code=400)

    file_name = file.filename
    ext = Path(file_name).suffix or ".mp4"
    seg_uuid = _uuid.uuid4().hex
    pool_name = f"import_{seg_uuid}{ext}"
    pool_sub = pdir / "pool" / "segments"
    pool_sub.mkdir(parents=True, exist_ok=True)
    dest = pool_sub / pool_name

    # Stream chunks to disk — never accumulate the full body.
    with dest.open("wb") as out:
        while True:
            chunk = await file.read(UPLOAD_CHUNK)
            if not chunk:
                break
            out.write(chunk)

    dur = _ffprobe_duration(dest)
    byte_size = dest.stat().st_size

    from scenecraft.db import _now_iso, get_db as _get_db

    auth_user = _authenticated_user_id(user)
    conn = _get_db(pdir)
    conn.execute(
        """INSERT INTO pool_segments
           (id, pool_path, kind, created_by, original_filename, original_filepath,
            label, generation_params, created_at, duration_seconds, width, height, byte_size)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            seg_uuid, f"pool/segments/{pool_name}", "imported", auth_user,
            file_name, originalFilepath or None,
            label or file_name, None, _now_iso(), dur, None, None, byte_size,
        ),
    )
    conn.commit()

    _log(
        f"pool/upload: {file_name} -> seg={seg_uuid[:8]} "
        f"({byte_size // 1024}KB, {dur}s)"
    )
    return {
        "success": True,
        "poolSegmentId": seg_uuid,
        "poolPath": f"pool/segments/{pool_name}",
        "originalFilename": file_name,
        "originalFilepath": originalFilepath or None,
        "durationSeconds": dur,
    }


# ---------------------------------------------------------------------------
# POST /pool/rename
# ---------------------------------------------------------------------------


@router.post(
    "/api/projects/{name}/pool/rename",
    operation_id="pool_rename",
    summary="Update a pool segment's display label.",
)
async def pool_rename(
    name: str, body: PoolRenameBody, pdir: Path = Depends(project_dir)
) -> dict:
    from scenecraft.db import get_pool_segment, update_pool_segment_label

    if not get_pool_segment(pdir, body.poolSegmentId):
        raise ApiError(
            "NOT_FOUND", f"Pool segment not found: {body.poolSegmentId}", status_code=404
        )
    update_pool_segment_label(pdir, body.poolSegmentId, body.label)
    _log(f"pool/rename: {body.poolSegmentId[:8]} -> {body.label!r}")
    return {"success": True, "poolSegmentId": body.poolSegmentId, "label": body.label}


# ---------------------------------------------------------------------------
# POST /pool/tag  +  /pool/untag
# ---------------------------------------------------------------------------


def _pool_tag_impl(pdir: Path, body: PoolTagBody | PoolUntagBody, *, add: bool, user) -> dict:
    if not body.poolSegmentId or not body.tag.strip():
        raise ApiError(
            "BAD_REQUEST", "Missing 'poolSegmentId' or 'tag'", status_code=400
        )

    from scenecraft.db import (
        add_pool_segment_tag,
        get_pool_segment,
        remove_pool_segment_tag,
    )

    if not get_pool_segment(pdir, body.poolSegmentId):
        raise ApiError(
            "NOT_FOUND",
            f"Pool segment not found: {body.poolSegmentId}",
            status_code=404,
        )

    auth_user = _authenticated_user_id(user)
    if add:
        add_pool_segment_tag(pdir, body.poolSegmentId, body.tag.strip(), tagged_by=auth_user)
        _log(f"pool/tag: {body.poolSegmentId[:8]} +{body.tag.strip()}")
    else:
        remove_pool_segment_tag(pdir, body.poolSegmentId, body.tag.strip())
        _log(f"pool/untag: {body.poolSegmentId[:8]} -{body.tag.strip()}")
    return {"success": True}


@router.post(
    "/api/projects/{name}/pool/tag",
    operation_id="pool_tag",
    summary="Add a tag to a pool segment.",
)
async def pool_tag(
    name: str,
    body: PoolTagBody,
    pdir: Path = Depends(project_dir),
    user=Depends(current_user),
) -> dict:
    return _pool_tag_impl(pdir, body, add=True, user=user)


@router.post(
    "/api/projects/{name}/pool/untag",
    operation_id="pool_untag",
    summary="Remove a tag from a pool segment.",
)
async def pool_untag(
    name: str,
    body: PoolUntagBody,
    pdir: Path = Depends(project_dir),
    user=Depends(current_user),
) -> dict:
    return _pool_tag_impl(pdir, body, add=False, user=user)


# ---------------------------------------------------------------------------
# POST /pool/gc — delete unreferenced generated segments
# ---------------------------------------------------------------------------


@router.post(
    "/api/projects/{name}/pool/gc",
    operation_id="pool_gc",
    summary="Garbage-collect unreferenced generated pool segments (destructive).",
)
async def pool_gc(name: str, pdir: Path = Depends(project_dir)) -> dict:
    from scenecraft.db import delete_pool_segment, find_gc_candidates

    orphans = find_gc_candidates(pdir)
    deleted = 0
    freed_bytes = 0
    for seg in orphans:
        try:
            disk = pdir / seg["poolPath"]
            if disk.exists():
                freed_bytes += disk.stat().st_size
                disk.unlink()
            delete_pool_segment(pdir, seg["id"])
            deleted += 1
        except Exception as e:
            _log(f"  gc failed for {seg['id']}: {e}")
    _log(f"pool/gc: deleted {deleted} segments, freed {freed_bytes // 1024}KB")
    return {"success": True, "deleted": deleted, "freedBytes": freed_bytes}


# ---------------------------------------------------------------------------
# POST /assign-pool-video — attach a pool segment to a transition
# ---------------------------------------------------------------------------


@router.post(
    "/api/projects/{name}/assign-pool-video",
    operation_id="assign_pool_video",
    summary="Attach a pool segment to a transition (no file copy).",
)
async def assign_pool_video(
    name: str,
    body: AssignPoolVideoBody,
    request: Request,
    pdir: Path = Depends(project_dir),
) -> dict:
    """Port of legacy ``_handle_assign_pool_video``.

    Not in ``STRUCTURAL_ROUTES`` despite mutating transitions — legacy
    left it out because the only state change (tr_candidates junction +
    ``selected`` pointer) is idempotent and doesn't restructure the
    timeline graph. Validator re-run on every write would thrash.
    """
    tr_id = body.transitionId
    seg_id = body.poolSegmentId
    pool_path = body.poolPath
    if not tr_id or not (seg_id or pool_path):
        raise ApiError(
            "BAD_REQUEST",
            "Missing 'transitionId' and either 'poolSegmentId' or 'poolPath'",
            status_code=400,
        )

    from scenecraft.db import (
        add_tr_candidate,
        get_pool_segment,
        get_transition,
        list_pool_segments,
        update_transition,
    )

    tr = get_transition(pdir, tr_id)
    if not tr:
        raise ApiError("NOT_FOUND", f"Transition {tr_id} not found", status_code=404)

    if not seg_id and pool_path:
        for s in list_pool_segments(pdir):
            if s["poolPath"] == pool_path:
                seg_id = s["id"]
                break
        if not seg_id:
            raise ApiError(
                "NOT_FOUND", f"No pool_segment for path: {pool_path}", status_code=404
            )

    seg = get_pool_segment(pdir, seg_id)
    if not seg:
        raise ApiError("NOT_FOUND", f"Pool segment not found: {seg_id}", status_code=404)
    source = pdir / seg["poolPath"]
    if not source.exists():
        raise ApiError(
            "NOT_FOUND",
            f"Pool segment file missing on disk: {seg['poolPath']}",
            status_code=404,
        )

    junction_source = "imported" if seg["kind"] == "imported" else "cross-tr-copy"
    add_tr_candidate(
        pdir, transition_id=tr_id, slot=0, pool_segment_id=seg_id, source=junction_source
    )

    sel_dir = pdir / "selected_transitions"
    sel_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(str(source), str(sel_dir / f"{tr_id}_slot_0.mp4"))

    existing_selected = tr.get("selected")
    if isinstance(existing_selected, list):
        current = list(existing_selected)
    elif existing_selected is None or existing_selected == []:
        current = [None] * tr.get("slots", 1)
    else:
        current = [existing_selected]
    while len(current) < tr.get("slots", 1):
        current.append(None)
    current[0] = seg_id
    update_transition(pdir, tr_id, selected=current)

    new_src_dur = seg.get("durationSeconds")
    if new_src_dur and new_src_dur > 0:
        trim_in = tr.get("trim_in") or 0
        trim_out = tr.get("trim_out")
        clamped_trim_out = min(trim_out, new_src_dur) if trim_out is not None else new_src_dur
        clamped_trim_in = min(trim_in, max(0, new_src_dur - 0.1))
        update_transition(
            pdir,
            tr_id,
            source_video_duration=new_src_dur,
            trim_in=clamped_trim_in,
            trim_out=clamped_trim_out,
        )

    # M9 task-89: auto-link audio from the newly-selected pool video. Same
    # non-fatal try/except as legacy.
    audio_link: dict[str, Any] | None = None
    try:
        from scenecraft.audio.linking import link_audio_for_transition

        audio_link = link_audio_for_transition(pdir, tr_id, replace=True)
    except Exception as e:
        _log(f"  audio auto-link failed (non-fatal): {e}")
        audio_link = {"status": "error", "transition_id": tr_id, "reason": str(e)}

    _log(f"  Assigned seg={seg_id[:8]} to {tr_id}")
    return {
        "success": True,
        "transitionId": tr_id,
        "poolSegmentId": seg_id,
        "audioLink": audio_link,
    }
