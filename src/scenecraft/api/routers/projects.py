"""Projects router — CRUD + meta + import + branches + version shims.

Ports roughly 26 routes from ``api_server.py`` covering:
  * project list / create / browse / ls / bin
  * keyframes read (mutations live in T61)
  * beats (read)
  * narrative (get + update)
  * update-meta
  * import (bulk file ingest)
  * save-as-still
  * extend-video (fires a background job + returns jobId)
  * watched-folders (get + watch + unwatch)
  * branches (list + create + delete + checkout)
  * version/* deprecated noops (2 GET noops, 1 POST noop, 3 POST 410s)

Every handler is a thin wrapper over the corresponding legacy
``_handle_*`` body. Behavior is byte-for-byte identical modulo the
error-envelope translation that ``errors.py`` performs on
``ApiError`` / ``HTTPException`` raises.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, Query, Request, status
from fastapi.responses import JSONResponse

from scenecraft.api.deps import current_user, project_dir as project_dir_dep
from scenecraft.api.errors import ApiError
from scenecraft.api.models.projects import (
    BranchCreateBody,
    BranchDeleteBody,
    CheckoutBody,
    CreateProjectBody,
    ExtendVideoBody,
    ImportBody,
    NarrativeBody,
    SaveAsStillBody,
    UpdateMetaBody,
    WatchFolderBody,
)


router = APIRouter(tags=["projects"], dependencies=[Depends(current_user)])


def _log(msg: str) -> None:
    """Thin wrapper around utils._log for parity with legacy traces."""
    from scenecraft.api.utils import _log as _util_log

    _util_log(msg)


def _work_dir(request: Request) -> Path:
    """Return app.state.work_dir or raise 500 INTERNAL_ERROR."""
    wd: Path | None = getattr(request.app.state, "work_dir", None)
    if wd is None:
        raise ApiError(
            "INTERNAL_ERROR",
            "File serving not configured (work_dir missing)",
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )
    return wd


def _require_project_dir(request: Request, name: str) -> Path:
    """Mirror ``api_server._require_project_dir`` — 404 if missing."""
    wd = _work_dir(request)
    pd = wd / name
    if not pd.is_dir():
        raise ApiError(
            "NOT_FOUND",
            f"Project not found: {name}",
            status_code=status.HTTP_404_NOT_FOUND,
        )
    return pd


# ---------------------------------------------------------------------------
# Project list + create + browse
# ---------------------------------------------------------------------------


@router.get(
    "/api/projects",
    operation_id="list_projects",
    summary="List every project in the work directory",
)
async def list_projects(request: Request) -> list[dict]:
    wd = _work_dir(request)
    _log("list-projects: listing projects")
    projects: list[dict] = []
    for entry in sorted(wd.iterdir()):
        if not entry.is_dir():
            continue
        files = list(entry.iterdir())
        filenames = [f.name for f in files]
        has_audio = any(f.endswith((".wav", ".mp3")) for f in filenames)
        has_video = any(f.endswith(".mp4") for f in filenames)
        has_beats = "beats.json" in filenames
        projects.append(
            {
                "name": entry.name,
                "hasAudio": has_audio,
                "hasVideo": has_video,
                "hasBeats": has_beats,
                "fileCount": len(files),
                "modified": entry.stat().st_mtime * 1000,
            }
        )
    return projects


@router.post(
    "/api/projects/create",
    operation_id="create_project",
    summary="Create a new project directory with default meta",
)
async def create_project(body: CreateProjectBody, request: Request) -> dict:
    wd = _work_dir(request)
    name = (body.name or "").strip()
    if not name:
        raise ApiError("BAD_REQUEST", "Missing 'name'", status_code=400)
    pd = wd / name
    if pd.exists():
        raise ApiError(
            "CONFLICT", f"Project '{name}' already exists", status_code=409
        )
    try:
        pd.mkdir(parents=True)
        from scenecraft.db import get_db, set_meta_bulk

        get_db(pd)
        meta = {
            "title": name,
            "fps": body.fps if body.fps is not None else 24,
            "resolution": body.resolution or [1920, 1080],
            "motion_prompt": body.motionPrompt or "",
            "default_transition_prompt": body.defaultTransitionPrompt
            or "Smooth cinematic transition",
        }
        set_meta_bulk(pd, meta)
        _log(f"create-project: {name}")
        return {"success": True, "name": name}
    except ApiError:
        raise
    except Exception as e:  # pragma: no cover — defensive, matches legacy
        raise ApiError("INTERNAL_ERROR", str(e), status_code=500)


@router.get(
    "/api/browse",
    operation_id="browse_projects",
    summary="Browse a subpath of the projects root",
)
async def browse_projects(request: Request, path: str = "") -> dict:
    wd = _work_dir(request)
    _log(f"browse: path={path or '/'}")
    target = (wd / path).resolve() if path else wd.resolve()
    if not str(target).startswith(str(wd.resolve())):
        raise ApiError("FORBIDDEN", "Path traversal denied", status_code=403)
    if not target.is_dir():
        raise ApiError(
            "NOT_FOUND", f"Directory not found: {path or '/'}", status_code=404
        )

    entries: list[dict] = []
    with os.scandir(target) as scanner:
        items = sorted(
            scanner,
            key=lambda e: (not e.is_dir(follow_symlinks=False), e.name.lower()),
        )
        for entry in items:
            is_dir = entry.is_dir(follow_symlinks=False)
            rel = str(Path(entry.path).relative_to(wd.resolve()))
            info: dict = {"name": entry.name, "path": rel, "isDirectory": is_dir}
            if not is_dir:
                ext = Path(entry.name).suffix.lower()
                if ext in (".png", ".jpg", ".jpeg", ".webp"):
                    info["type"] = "image"
                elif ext in (".mp4", ".webm", ".mov"):
                    info["type"] = "video"
                else:
                    info["type"] = "other"
            entries.append(info)
    return {"path": path or "", "entries": entries}


# ---------------------------------------------------------------------------
# ls (directory listing under a project) + bin + keyframes + beats
# ---------------------------------------------------------------------------


@router.get(
    "/api/projects/{name}/ls",
    operation_id="list_project_files",
    summary="List files inside a project directory (optional subpath)",
)
async def list_project_files(
    name: str, request: Request, path: str = ""
) -> list[dict]:
    wd = _work_dir(request)
    _log(f"ls: {name} path={path or '/'}")
    project_root = (wd / name).resolve()
    target = (project_root / path).resolve()
    if not str(target).startswith(str(project_root)):
        raise ApiError("FORBIDDEN", "Path traversal denied", status_code=403)
    if not target.is_dir():
        raise ApiError(
            "NOT_FOUND", f"Directory not found: {path or '/'}", status_code=404
        )
    entries: list[dict] = []
    with os.scandir(target) as scanner:
        items = sorted(
            scanner,
            key=lambda e: (not e.is_dir(follow_symlinks=False), e.name.lower()),
        )
        for entry in items:
            is_dir = entry.is_dir(follow_symlinks=False)
            rel = str(Path(entry.path).relative_to(project_root))
            entries.append({"name": entry.name, "path": rel, "isDirectory": is_dir})
    return entries


@router.get(
    "/api/projects/{name}/bin",
    operation_id="get_project_bin",
    summary="List binned (soft-deleted) keyframes and transitions",
)
async def get_project_bin(name: str, request: Request) -> dict:
    wd = _work_dir(request)
    _log(f"get-bin: {name}")
    pd = wd / name
    if not pd.is_dir():
        # Legacy returns empty bin (not 404) when project dir missing.
        return {"bin": [], "transitionBin": []}

    from scenecraft.db import get_binned_keyframes, get_binned_transitions

    bin_entries: list[dict] = []
    for kf in get_binned_keyframes(pd):
        img_path = pd / "selected_keyframes" / f"{kf['id']}.png"
        bin_entries.append(
            {
                "id": kf["id"],
                "deleted_at": kf.get("deleted_at", ""),
                "timestamp": kf.get("timestamp", "0:00"),
                "section": kf.get("section", ""),
                "prompt": kf.get("prompt", ""),
                "hasSelectedImage": img_path.exists(),
            }
        )

    transition_bin: list[dict] = []
    for tr in get_binned_transitions(pd):
        has_video = (pd / "selected_transitions" / f"{tr['id']}_slot_0.mp4").exists()
        if not has_video:
            continue
        transition_bin.append(
            {
                "id": tr["id"],
                "deleted_at": tr.get("deleted_at", ""),
                "from": tr.get("from", ""),
                "to": tr.get("to", ""),
                "durationSeconds": tr.get("duration_seconds", 0),
                "slots": tr.get("slots", 1),
                "trimIn": tr.get("trim_in") or 0,
                "trimOut": tr.get("trim_out"),
                "sourceVideoDuration": tr.get("source_video_duration"),
            }
        )
    return {"bin": bin_entries, "transitionBin": transition_bin}


@router.get(
    "/api/projects/{name}/keyframes",
    operation_id="get_keyframes",
    summary="Load keyframes + transitions payload for the editor",
)
async def get_keyframes(name: str, request: Request) -> dict:
    """Port of ``api_server.py::_handle_get_keyframes``.

    Legacy handler is a bound method on the HTTPServer handler class —
    binding to one from a FastAPI route is awkward, so we call the
    underlying DB functions directly and reconstruct the payload here.
    The shape (keys, nesting, defaults) is byte-for-byte the same.
    """
    from scenecraft.db import (
        get_all_transition_effects,
        get_keyframes as db_get_keyframes,
        get_meta,
        get_tr_candidates as _db_get_tr_cands,
        get_tracks,
        get_transitions as db_get_transitions,
    )

    wd = _work_dir(request)
    _log(f"get-keyframes: {name}")
    pd = wd / name
    if not pd.is_dir():
        raise ApiError(
            "NOT_FOUND", f"Project not found: {name}", status_code=404
        )

    if not (pd / "project.db").exists():
        return {
            "meta": {"title": name, "fps": 24, "resolution": [1920, 1080]},
            "keyframes": [],
            "transitions": [],
            "audioFile": None,
            "projectName": name,
            "tracks": [
                {
                    "id": "track_1",
                    "name": "Track 1",
                    "zOrder": 0,
                    "blendMode": "normal",
                    "baseOpacity": 1.0,
                    "muted": False,
                    "solo": False,
                }
            ],
        }

    meta = get_meta(pd)
    result_meta = {
        "title": meta.get("title", name),
        "fps": meta.get("fps", 24),
        "resolution": meta.get("resolution", [1920, 1080]),
        "motionPrompt": meta.get("motion_prompt", ""),
        "defaultTransitionPrompt": meta.get(
            "default_transition_prompt", "Smooth cinematic transition"
        ),
    }

    keyframes: list[dict] = []
    for kf in db_get_keyframes(pd):
        kf_id = kf["id"]
        img_path = pd / "selected_keyframes" / f"{kf_id}.png"
        has_selected = kf.get("selected") is not None
        if has_selected and not img_path.exists():
            _log(
                f"⚠ keyframe {kf_id} has selected={kf.get('selected')} "
                f"but file missing: {img_path}"
            )
        candidates_dir = (
            pd / "keyframe_candidates" / "candidates" / f"section_{kf_id}"
        )
        candidate_files: list[str] = []
        if candidates_dir.exists():
            candidate_files = sorted(
                [
                    f"keyframe_candidates/candidates/section_{kf_id}/{f.name}"
                    for f in candidates_dir.glob("v*.png")
                ],
                key=lambda p: int(p.rsplit("v", 1)[-1].split(".")[0]),
            )
        ctx = kf.get("context")
        keyframes.append(
            {
                "id": kf_id,
                "timestamp": kf.get("timestamp", "0:00"),
                "section": kf.get("section", ""),
                "prompt": kf.get("prompt", ""),
                "selected": kf.get("selected"),
                "hasSelectedImage": has_selected,
                "trackId": kf.get("track_id", "track_1"),
                "label": kf.get("label", ""),
                "labelColor": kf.get("label_color", ""),
                "blendMode": kf.get("blend_mode", ""),
                "refinementPrompt": kf.get("refinement_prompt", ""),
                "opacity": kf.get("opacity"),
                "candidates": candidate_files,
                "context": (
                    {
                        "mood": ctx.get("mood", ""),
                        "energy": ctx.get("energy", ""),
                        "instruments": ctx.get("instruments", []),
                        "motifs": ctx.get("motifs", []),
                        "events": ctx.get("events", []),
                        "visual_direction": ctx.get("visual_direction", ""),
                        "details": ctx.get("details", ""),
                    }
                    if ctx
                    else None
                ),
            }
        )

    audio_file = None
    for candidate in ("audio.wav", "audio.mp3"):
        if (pd / candidate).exists():
            audio_file = candidate
            break

    get_all_transition_effects(pd)  # side-effect parity: ensure schema migrated
    transitions: list[dict] = []
    for tr in db_get_transitions(pd):
        tr_id = tr.get("id", "")
        slot_candidates: dict[str, list[str]] = {}
        slot_candidate_details: dict[str, list[dict]] = {}
        for slot_idx in range(tr.get("slots", 1)):
            cands = _db_get_tr_cands(pd, tr_id, slot_idx)
            if cands:
                slot_candidates[f"slot_{slot_idx}"] = [c["poolPath"] for c in cands]
                slot_candidate_details[f"slot_{slot_idx}"] = [
                    {
                        "id": c["id"],
                        "poolPath": c["poolPath"],
                        "kind": c["kind"],
                        "label": c.get("label") or "",
                        "createdBy": c.get("createdBy") or "",
                        "durationSeconds": c.get("durationSeconds"),
                        "addedAt": c.get("addedAt"),
                        "generationParams": c.get("generationParams"),
                    }
                    for c in cands
                ]
        selected_tr_dir = pd / "selected_transitions"
        sel = tr.get("selected")
        selected_list = sel if isinstance(sel, list) else [sel]
        has_selected_videos: list[bool] = []
        for slot_idx in range(tr.get("slots", 1)):
            slot_selected = (
                selected_list[slot_idx] if slot_idx < len(selected_list) else None
            )
            has_selected = slot_selected is not None
            has_selected_videos.append(has_selected)
            if has_selected:
                sel_path = selected_tr_dir / f"{tr_id}_slot_{slot_idx}.mp4"
                if not sel_path.exists():
                    _log(
                        f"⚠ transition {tr_id} slot_{slot_idx} has "
                        f"selected={slot_selected} but file missing: {sel_path}"
                    )

        out: dict = {
            "id": tr_id,
            "from": tr.get("from", ""),
            "to": tr.get("to", ""),
            "durationSeconds": tr.get("duration_seconds", 0),
            "slots": tr.get("slots", 1),
            "action": tr.get("action", ""),
            "remap": tr.get("remap"),
            "selected": tr.get("selected"),
            "hasSelectedVideos": has_selected_videos,
            "slotCandidates": slot_candidates,
            "slotCandidateDetails": slot_candidate_details,
            "trackId": tr.get("track_id", "track_1"),
            "label": tr.get("label", ""),
            "labelColor": tr.get("label_color", ""),
            "blendMode": tr.get("blend_mode", ""),
            "useGlobalPrompt": tr.get("use_global_prompt", True),
            "trimIn": tr.get("trim_in") or 0,
            "trimOut": tr.get("trim_out"),
            "sourceVideoDuration": tr.get("source_video_duration"),
            "opacity": tr.get("opacity"),
        }
        transitions.append(out)

    tracks = get_tracks(pd)
    return {
        "meta": result_meta,
        "keyframes": keyframes,
        "transitions": transitions,
        "audioFile": audio_file,
        "projectName": name,
        "tracks": tracks,
    }


@router.get(
    "/api/projects/{name}/beats",
    operation_id="get_project_beats",
    summary="Return beats.json for the project's audio",
)
async def get_project_beats(name: str, request: Request) -> dict:
    wd = _work_dir(request)
    pd = wd / name
    if not pd.is_dir():
        return {"beats": [], "duration": 0}
    beats_file = pd / "beats.json"
    if not beats_file.exists():
        return {"beats": [], "duration": 0}
    try:
        return json.loads(beats_file.read_text())
    except Exception:
        return {"beats": [], "duration": 0}


# ---------------------------------------------------------------------------
# Narrative / update-meta / save-as-still / import / extend-video
# ---------------------------------------------------------------------------


@router.get(
    "/api/projects/{name}/narrative",
    operation_id="get_narrative",
    summary="Return timeline narrative sections",
)
async def get_narrative(name: str, proj: Path = Depends(project_dir_dep)) -> dict:
    from scenecraft.db import get_sections

    _log(f"get-narrative: {name}")
    return {"sections": get_sections(proj)}


@router.post(
    "/api/projects/{name}/narrative",
    operation_id="update_narrative",
    summary="Replace timeline narrative sections",
)
async def update_narrative(
    name: str, body: NarrativeBody, proj: Path = Depends(project_dir_dep)
) -> dict:
    from scenecraft.db import get_sections, set_sections

    if body.sections is not None:
        _log(f"update-narrative: {len(body.sections)} sections")
        set_sections(proj, body.sections)
    result = get_sections(proj)
    return {"success": True, "sections": len(result)}


@router.post(
    "/api/projects/{name}/update-meta",
    operation_id="update_meta",
    summary="Update motion_prompt / default_transition_prompt / image_model",
)
async def update_meta(
    name: str, body: UpdateMetaBody, proj: Path = Depends(project_dir_dep)
) -> dict:
    from scenecraft.db import get_meta, set_meta

    data = body.model_dump(exclude_none=True)
    _log(
        "update-meta: "
        f"{[k for k in ('motion_prompt', 'default_transition_prompt', 'image_model') if k in data]}"
    )
    meta = get_meta(proj)
    for key in ("motion_prompt", "default_transition_prompt", "image_model"):
        if key in data:
            set_meta(proj, key, data[key])
            meta[key] = data[key]
    return {"success": True, "meta": meta}


@router.post(
    "/api/projects/{name}/import",
    operation_id="import_project",
    summary="Bulk-import images as keyframes and videos as transitions",
)
async def import_project(
    name: str,
    body: ImportBody,
    request: Request,
    proj: Path = Depends(project_dir_dep),
) -> dict:
    import shutil
    from datetime import datetime, timezone

    from scenecraft.db import (
        add_keyframe,
        add_transition,
        get_db,
        next_keyframe_id,
        next_transition_id,
    )

    wd = _work_dir(request)
    source_path = body.sourcePath
    start_timestamp = body.timestamp or "0:00"
    if not source_path:
        raise ApiError("BAD_REQUEST", "Missing 'sourcePath'", status_code=400)

    source = Path(source_path)
    if not source.is_absolute():
        source = (wd / source_path).resolve()
    if not source.exists():
        raise ApiError(
            "NOT_FOUND", f"Source path not found: {source_path}", status_code=404
        )

    try:
        _log(f"import: source={source_path}")
        get_db(proj)
        kf_num = int(next_keyframe_id(proj).replace("kf_", ""))
        tr_num = int(next_transition_id(proj).replace("tr_", ""))
        IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp"}
        VIDEO_EXTS = {".mp4", ".webm", ".mov"}
        files = sorted(source.iterdir()) if source.is_dir() else [source]
        now = datetime.now(timezone.utc).isoformat()
        selected_kf_dir = proj / "selected_keyframes"
        selected_kf_dir.mkdir(parents=True, exist_ok=True)
        selected_tr_dir = proj / "selected_transitions"
        selected_tr_dir.mkdir(parents=True, exist_ok=True)

        def parse_ts(ts: Any) -> float:
            parts = str(ts).split(":")
            if len(parts) == 2:
                return int(parts[0]) * 60 + float(parts[1])
            return 0.0

        def format_ts(seconds: float) -> str:
            m = int(seconds // 60)
            s = seconds % 60
            whole = int(s)
            frac = s - whole
            if frac < 0.005:
                return f"{m}:{whole:02d}"
            return f"{m}:{whole:02d}.{round(frac * 100):02d}"

        current_ts = parse_ts(start_timestamp)
        imported_kf: list[str] = []
        imported_tr: list[str] = []
        for f in files:
            if not f.is_file():
                continue
            ext = f.suffix.lower()
            if ext in IMAGE_EXTS:
                kf_id = f"kf_{kf_num:03d}"
                kf_num += 1
                dest = selected_kf_dir / f"{kf_id}.png"
                shutil.copy2(str(f), str(dest))
                add_keyframe(
                    proj,
                    {
                        "id": kf_id,
                        "timestamp": format_ts(current_ts),
                        "section": "",
                        "source": str(f),
                        "prompt": f"Imported from {f.name}",
                        "context": None,
                        "candidates": [],
                        "selected": 1,
                        "deleted_at": now,
                    },
                )
                imported_kf.append(kf_id)
                current_ts += 1.0
            elif ext in VIDEO_EXTS:
                tr_id = f"tr_{tr_num:03d}"
                tr_num += 1
                dest = selected_tr_dir / f"{tr_id}_slot_0{ext}"
                shutil.copy2(str(f), str(dest))
                add_transition(
                    proj,
                    {
                        "id": tr_id,
                        "from": "",
                        "to": "",
                        "duration_seconds": 0,
                        "slots": 1,
                        "action": f"Imported from {f.name}",
                        "selected": [],
                        "remap": {"method": "linear", "target_duration": 0},
                        "deleted_at": now,
                    },
                )
                imported_tr.append(tr_id)

        return {
            "success": True,
            "imported": {"keyframes": imported_kf, "transitions": imported_tr},
            "summary": (
                f"{len(imported_kf)} keyframe(s), "
                f"{len(imported_tr)} transition(s) imported to bin"
            ),
        }
    except ApiError:
        raise
    except Exception as e:
        raise ApiError("INTERNAL_ERROR", str(e), status_code=500)


@router.post(
    "/api/projects/{name}/save-as-still",
    operation_id="save_as_still",
    summary="Copy a frame/video asset into assets/stills/",
)
async def save_as_still(
    name: str, body: SaveAsStillBody, proj: Path = Depends(project_dir_dep)
) -> dict:
    import shutil

    if not body.sourcePath:
        raise ApiError("BAD_REQUEST", "Missing 'sourcePath'", status_code=400)
    src = proj / body.sourcePath
    if not src.exists():
        raise ApiError(
            "NOT_FOUND", f"Source not found: {body.sourcePath}", status_code=404
        )
    stills_dir = proj / "assets" / "stills"
    stills_dir.mkdir(parents=True, exist_ok=True)
    dest_name = body.name or (src.stem + src.suffix)
    dest = stills_dir / dest_name
    counter = 1
    while dest.exists():
        dest = stills_dir / f"{src.stem}_{counter}{src.suffix}"
        counter += 1
    shutil.copy2(str(src), str(dest))
    _log(f"save-as-still: {body.sourcePath} -> assets/stills/{dest.name}")
    return {
        "success": True,
        "name": dest.name,
        "path": f"assets/stills/{dest.name}",
    }


@router.post(
    "/api/projects/{name}/extend-video",
    operation_id="extend_video",
    summary="Extend an existing transition video using Veo; returns jobId",
)
async def extend_video(
    name: str, body: ExtendVideoBody, proj: Path = Depends(project_dir_dep)
) -> dict:
    """Fires a background job (legacy spawns a thread) and returns the jobId.

    Matches ``api_server.py::_handle_extend_video`` byte-for-byte: the
    Veo call, last-frame extraction, pool-segment insertion, and
    candidate linking all live inside the daemon thread so the HTTP
    response returns immediately.
    """
    import threading
    import uuid as _uuid

    from scenecraft.api.utils import _get_video_backend

    tr_id = body.transitionId
    video_path = body.videoPath
    if not tr_id or not video_path:
        raise ApiError(
            "BAD_REQUEST", "Missing transitionId or videoPath", status_code=400
        )

    from scenecraft.db import get_meta, get_transition

    tr = get_transition(proj, tr_id)
    if not tr:
        raise ApiError("NOT_FOUND", f"Transition {tr_id} not found", status_code=404)

    video_file = proj / video_path
    if not video_file.exists():
        raise ApiError("NOT_FOUND", f"Video not found: {video_path}", status_code=404)

    meta = get_meta(proj)
    motion_prompt = (
        meta.get("motionPrompt") or meta.get("motion_prompt") or ""
    )
    action = tr.get("action") or "Continue the video smoothly"
    use_global = tr.get("use_global_prompt", True)
    if use_global and motion_prompt:
        prompt = f"{action}. Camera and motion style: {motion_prompt}"
    else:
        prompt = action

    from scenecraft.ws_server import job_manager

    job_id = job_manager.create_job(
        "extend_video", total=1, meta={"transitionId": tr_id, "project": name}
    )
    vid_backend = _get_video_backend(proj)

    def _run() -> None:
        try:
            from pathlib import Path as _Path
            import subprocess as _sp

            from scenecraft.render.google_video import GoogleVideoClient

            client = GoogleVideoClient(vertex=True)
            job_manager.update_progress(job_id, 0, "Extracting last frame...")
            pool_segs = proj / "pool" / "segments"
            pool_segs.mkdir(parents=True, exist_ok=True)
            last_frame = (
                pool_segs / f"_extend_last_frame_{tr_id}_{_uuid.uuid4().hex[:8]}.png"
            )
            _sp.run(
                [
                    "ffmpeg",
                    "-y",
                    "-sseof",
                    "-0.1",
                    "-i",
                    str(video_file),
                    "-vframes",
                    "1",
                    "-q:v",
                    "2",
                    str(last_frame),
                ],
                capture_output=True,
                timeout=10,
            )
            if not last_frame.exists():
                job_manager.fail_job(
                    job_id, "Failed to extract last frame from video"
                )
                return

            seg_uuid = _uuid.uuid4().hex
            pool_name = f"cand_{seg_uuid}.mp4"
            output = str(pool_segs / pool_name)
            job_manager.update_progress(job_id, 0, "Extending video with Veo...")
            client.generate_video_from_image(
                image_path=str(last_frame),
                prompt=prompt,
                output_path=output,
                duration_seconds=8,
                generate_audio=False,
                on_status=lambda msg: job_manager.update_progress(
                    job_id, 0, msg
                ),
            )
            if _Path(output).exists():
                probe = _sp.run(
                    [
                        "ffprobe",
                        "-v",
                        "error",
                        "-show_entries",
                        "format=duration",
                        "-of",
                        "csv=p=0",
                        output,
                    ],
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
                dur = (
                    float(probe.stdout.strip())
                    if probe.returncode == 0 and probe.stdout.strip()
                    else None
                )
                byte_size = _Path(output).stat().st_size
                from scenecraft.db import (
                    _now_iso,
                    add_tr_candidate as _add_tc,
                    get_db as _get_db,
                )

                conn = _get_db(proj)
                conn.execute(
                    """INSERT INTO pool_segments
                       (id, pool_path, kind, created_by, original_filename, original_filepath,
                        label, generation_params, created_at, duration_seconds, width, height, byte_size)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        seg_uuid,
                        f"pool/segments/{pool_name}",
                        "generated",
                        "local",
                        None,
                        None,
                        "",
                        json.dumps(
                            {
                                "provider": "google-veo",
                                "prompt": prompt,
                                "source": "extend",
                            }
                        ),
                        _now_iso(),
                        dur,
                        None,
                        None,
                        byte_size,
                    ),
                )
                conn.commit()
                _add_tc(
                    proj,
                    transition_id=tr_id,
                    slot=0,
                    pool_segment_id=seg_uuid,
                    source="generated",
                )

            from scenecraft.db import get_tr_candidates as _db_get_tc

            candidates: dict[str, list[str]] = {}
            for si in range(1):
                cands = _db_get_tc(proj, tr_id, si)
                if cands:
                    candidates[f"slot_{si}"] = [c["poolPath"] for c in cands]
            job_manager.complete_job(
                job_id, {"transitionId": tr_id, "candidates": candidates}
            )
            if last_frame.exists():
                last_frame.unlink()
        except Exception as e:  # pragma: no cover — background thread
            _log(f"[extend-video] FAILED: {e}")
            import traceback

            traceback.print_exc()
            job_manager.fail_job(job_id, str(e))

    threading.Thread(target=_run, daemon=True).start()
    return {"jobId": job_id, "transitionId": tr_id}


# ---------------------------------------------------------------------------
# Watched folders
# ---------------------------------------------------------------------------


@router.get(
    "/api/projects/{name}/watched-folders",
    operation_id="get_watched_folders",
    summary="Return persisted watched-folder paths",
)
async def get_watched_folders(name: str, request: Request) -> dict:
    wd = _work_dir(request)
    _log(f"get-watched-folders: {name}")
    pd = wd / name
    if not pd.is_dir():
        return {"watchedFolders": []}
    try:
        from scenecraft.db import get_meta

        meta = get_meta(pd)
        wf = meta.get("watched_folders", [])
        if isinstance(wf, str):
            wf = json.loads(wf)
        return {"watchedFolders": wf}
    except Exception:
        return {"watchedFolders": []}


@router.post(
    "/api/projects/{name}/watch-folder",
    operation_id="watch_folder",
    summary="Begin watching a folder for auto-import",
)
async def watch_folder(
    name: str, body: WatchFolderBody, proj: Path = Depends(project_dir_dep)
) -> dict:
    from scenecraft.db import get_meta, set_meta
    from scenecraft.ws_server import folder_watcher

    if not body.folderPath:
        raise ApiError("BAD_REQUEST", "Missing 'folderPath'", status_code=400)
    if not folder_watcher:
        raise ApiError(
            "INTERNAL_ERROR", "Folder watcher not initialized", status_code=500
        )
    try:
        _log(f"watch-folder: {body.folderPath}")
        result = folder_watcher.add_watch(name, body.folderPath)
        meta = get_meta(proj)
        watched = meta.get("watched_folders", [])
        if not isinstance(watched, list):
            watched = []
        if body.folderPath not in watched:
            watched.append(body.folderPath)
        set_meta(proj, "watched_folders", watched)
        return {"success": True, **result}
    except ApiError:
        raise
    except Exception as e:
        raise ApiError("BAD_REQUEST", str(e), status_code=400)


@router.post(
    "/api/projects/{name}/unwatch-folder",
    operation_id="unwatch_folder",
    summary="Stop watching a folder",
)
async def unwatch_folder(
    name: str, body: WatchFolderBody, request: Request
) -> dict:
    from scenecraft.db import get_meta, set_meta
    from scenecraft.ws_server import folder_watcher

    if not body.folderPath:
        raise ApiError("BAD_REQUEST", "Missing 'folderPath'", status_code=400)
    _log(f"unwatch-folder: {body.folderPath}")
    if folder_watcher:
        folder_watcher.remove_watch(name, body.folderPath)
    wd = _work_dir(request)
    pd = wd / name
    if pd.is_dir():
        meta = get_meta(pd)
        watched = meta.get("watched_folders", [])
        if not isinstance(watched, list):
            watched = []
        if body.folderPath in watched:
            watched.remove(body.folderPath)
        set_meta(pd, "watched_folders", watched)
    return {"success": True}


# ---------------------------------------------------------------------------
# Branches / checkout
# ---------------------------------------------------------------------------


def _resolve_vcs_project_dir(request: Request, name: str) -> tuple[Path, str, Path]:
    """Port of ``_resolve_project_dir_for_branches`` sans the send-error
    side effect — raises ``ApiError`` so ``errors.py`` converts cleanly.

    Legacy uses a closure variable ``_sc_root`` captured inside
    ``make_handler``. In the FastAPI port we resolve the .scenecraft
    root on every call via ``find_root(work_dir)`` — cheap (cached
    filesystem walk) and keeps this module import-free of the legacy
    HTTPServer closure state.
    """
    from scenecraft.vcs.bootstrap import find_root, get_server_db

    wd = _work_dir(request)
    sc_root = find_root(wd)
    if sc_root is None:
        raise ApiError(
            "VCS_UNAVAILABLE",
            "VCS not initialized (no .scenecraft root)",
            status_code=503,
        )
    # Legacy also requires _authenticated_user; by the time we reach here
    # ``current_user`` has already gated the request, so we know there's
    # a user in play. Scan org_members for a matching project dir.
    conn = get_server_db(sc_root)
    rows = conn.execute("SELECT org FROM org_members").fetchall()
    conn.close()
    org = None
    for row in rows:
        if (sc_root / "orgs" / row["org"] / "projects" / name).is_dir():
            org = row["org"]
            break
    if org is None:
        raise ApiError(
            "NOT_FOUND",
            f"Project not found under any org: {name}",
            status_code=404,
        )
    pdir = sc_root / "orgs" / org / "projects" / name
    if not pdir.is_dir():
        raise ApiError(
            "NOT_FOUND", f"Project directory missing: {pdir}", status_code=404
        )
    return sc_root, org, pdir


@router.get(
    "/api/projects/{name}/branches",
    operation_id="list_branches",
    summary="List VCS branches on this project",
)
async def list_branches(name: str, request: Request) -> dict:
    from scenecraft.vcs.branches import list_branches as _list
    from scenecraft.vcs.sessions import get_session_for_user

    _sc, org, pdir = _resolve_vcs_project_dir(request, name)
    branch_header = request.headers.get("X-Scenecraft-Branch", "main")
    # Use a synthetic user id — current_user has already authed; the lookup
    # here is best-effort and legacy passes the authenticated user directly.
    # We mirror that by pulling the authenticated user out of the request's
    # current_user dep call chain via request.state if set — otherwise fall
    # back to None and get current=None.
    authed_user = getattr(request.state, "user_id", None)
    session = None
    if authed_user:
        session = get_session_for_user(_sc, authed_user, org, name, branch_header)
    current = session["branch"] if session else None
    branches = _list(pdir, current_branch=current)
    return {"branches": branches, "current": current}


@router.post(
    "/api/projects/{name}/branches",
    operation_id="create_branch",
    summary="Create a new VCS branch",
)
async def create_branch(
    name: str, body: BranchCreateBody, request: Request
) -> dict:
    from scenecraft.vcs.branches import BranchError, create_branch as _create

    branch_name = (body.name or "").strip()
    from_branch = (body.fromBranch or "main").strip()
    _sc, _org, pdir = _resolve_vcs_project_dir(request, name)
    try:
        result = _create(pdir, branch_name, from_branch=from_branch)
    except BranchError as e:
        msg = str(e)
        code = "CONFLICT" if "already exists" in msg else "BAD_REQUEST"
        st = 409 if code == "CONFLICT" else 400
        raise ApiError(code, msg, status_code=st)
    return {
        "success": True,
        "branch": result["name"],
        "commitHash": result["commit_hash"],
        "fromBranch": result["from_branch"],
    }


@router.post(
    "/api/projects/{name}/branches/delete",
    operation_id="delete_branch",
    summary="Delete a VCS branch ref",
)
async def delete_branch(
    name: str, body: BranchDeleteBody, request: Request
) -> dict:
    from scenecraft.vcs.branches import BranchError, delete_branch as _delete
    from scenecraft.vcs.sessions import get_session_for_user

    branch_name = (body.name or "").strip()
    if not branch_name:
        raise ApiError("BAD_REQUEST", "Missing 'name'", status_code=400)

    _sc, org, pdir = _resolve_vcs_project_dir(request, name)
    header_branch = request.headers.get("X-Scenecraft-Branch", "main")
    authed_user = getattr(request.state, "user_id", None)
    session = None
    if authed_user:
        session = get_session_for_user(_sc, authed_user, org, name, header_branch)
    current = session["branch"] if session else None
    try:
        _delete(pdir, branch_name, current_branch=current)
    except BranchError as e:
        msg = str(e)
        if "not found" in msg.lower():
            raise ApiError("NOT_FOUND", msg, status_code=404)
        raise ApiError("BAD_REQUEST", msg, status_code=400)
    return {"success": True, "deleted": branch_name}


@router.post(
    "/api/projects/{name}/checkout",
    operation_id="checkout_branch",
    summary="Switch session branch",
)
async def checkout_branch(
    name: str, body: CheckoutBody, request: Request
) -> dict:
    from scenecraft.vcs.branches import BranchError, checkout_branch as _checkout
    from scenecraft.vcs.sessions import get_session_for_user

    target = (body.branch or "").strip()
    force = bool(body.force or False)
    if not target:
        raise ApiError("BAD_REQUEST", "Missing 'branch'", status_code=400)

    _sc, org, pdir = _resolve_vcs_project_dir(request, name)
    branch_header = request.headers.get("X-Scenecraft-Branch", "main")
    authed_user = getattr(request.state, "user_id", None)
    session = None
    if authed_user:
        session = get_session_for_user(
            _sc, authed_user, org, name, branch_header
        )
    if session is None:
        raise ApiError(
            "NO_SESSION",
            f"No active session on branch '{branch_header}'",
            status_code=400,
        )
    try:
        updated = _checkout(_sc, session["id"], target, pdir, force=force)
    except BranchError as e:
        msg = str(e)
        if "uncommitted" in msg.lower():
            raise ApiError("UNCOMMITTED_CHANGES", msg, status_code=409)
        if "not found" in msg.lower():
            raise ApiError("NOT_FOUND", msg, status_code=404)
        raise ApiError("BAD_REQUEST", msg, status_code=400)
    return {
        "success": True,
        "branch": updated["branch"],
        "commitHash": updated["commit_hash"],
        "sessionId": updated["id"],
    }


# ---------------------------------------------------------------------------
# Version shims (deprecated — git removed)
# ---------------------------------------------------------------------------


@router.get(
    "/api/projects/{name}/version/history",
    operation_id="version_history_deprecated",
    summary="Deprecated — returns empty history (git removed)",
)
async def version_history_deprecated(name: str) -> dict:
    return {"commits": [], "branch": "", "branches": []}


@router.get(
    "/api/projects/{name}/version/diff",
    operation_id="version_diff_deprecated",
    summary="Deprecated — returns empty diff (git removed)",
)
async def version_diff_deprecated(name: str) -> dict:
    return {"changes": []}


@router.post(
    "/api/projects/{name}/version/commit",
    operation_id="version_commit_noop",
    summary="Deprecated — noop, returns success/noChanges",
)
async def version_commit_noop(name: str) -> dict:
    return {"success": True, "noChanges": True}


@router.post(
    "/api/projects/{name}/version/checkout",
    operation_id="version_checkout_noop",
    summary="Deprecated — 410 GONE (use checkpoint/restore)",
)
async def version_checkout_noop(name: str):
    raise ApiError(
        "GONE",
        "Git versioning removed — use checkpoint/restore instead",
        status_code=410,
    )


@router.post(
    "/api/projects/{name}/version/branch",
    operation_id="version_branch_noop",
    summary="Deprecated — 410 GONE (use checkpoint/restore)",
)
async def version_branch_noop(name: str):
    raise ApiError(
        "GONE",
        "Git versioning removed — use checkpoint/restore instead",
        status_code=410,
    )


@router.post(
    "/api/projects/{name}/version/delete-branch",
    operation_id="version_delete_branch_noop",
    summary="Deprecated — 410 GONE (use checkpoint/restore)",
)
async def version_delete_branch_noop(name: str):
    raise ApiError(
        "GONE",
        "Git versioning removed — use checkpoint/restore instead",
        status_code=410,
    )


__all__ = ["router"]
