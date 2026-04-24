"""Bench router (M16 T60).

Ports the 5 bench routes. ``bench_capture`` and ``bench_upload`` have
sizable bodies in the legacy handler — we keep them thin by calling
the legacy subprocess-+-ffmpeg pipeline directly. Upload parses its
own multipart because the legacy handler does; using ``UploadFile``
would force a body shape change front-end-side.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, Request

from scenecraft.api.deps import current_user, project_dir as project_dir_dep
from scenecraft.api.errors import ApiError
from scenecraft.api.models.projects import (
    BenchAddBody,
    BenchCaptureBody,
    BenchRemoveBody,
)


router = APIRouter(tags=["bench"], dependencies=[Depends(current_user)])


def _log(msg: str) -> None:
    from scenecraft.api_server import _log as legacy_log

    legacy_log(msg)


@router.get(
    "/api/projects/{name}/bench",
    operation_id="get_bench",
    summary="List benched items with usage tracking",
)
async def get_bench(name: str, request: Request) -> dict:
    wd = getattr(request.app.state, "work_dir", None)
    _log(f"get-bench: {name}")
    if wd is None:
        raise ApiError("INTERNAL_ERROR", "work_dir missing", status_code=500)
    pd = wd / name
    if not pd.is_dir():
        return {"items": []}
    try:
        from scenecraft.db import get_bench as _get_bench

        return {"items": _get_bench(pd)}
    except Exception as e:
        raise ApiError("INTERNAL_ERROR", str(e), status_code=500)


@router.post(
    "/api/projects/{name}/bench/capture",
    operation_id="bench_capture",
    summary="Capture a frame at a timeline time and add to the bench",
)
async def bench_capture(
    name: str, body: BenchCaptureBody, proj: Path = Depends(project_dir_dep)
) -> dict:
    import subprocess as sp
    import time as _time
    import traceback as _tb

    from scenecraft.db import add_to_bench, get_keyframes, get_transitions

    if body.time is None:
        raise ApiError("BAD_REQUEST", "Missing 'time'", status_code=400)
    time_sec = body.time
    track_id = body.trackId or "track_1"
    _log(f"bench-capture: {name} time={time_sec} track={track_id}")

    try:

        def parse_ts(ts):
            parts = str(ts).split(":")
            if len(parts) == 2:
                return int(parts[0]) * 60 + float(parts[1])
            return float(ts) if isinstance(ts, (int, float)) else 0

        all_kfs = [
            kf
            for kf in get_keyframes(proj)
            if kf.get("deleted_at") is None
            and kf.get("track_id", "track_1") == track_id
        ]
        sorted_kfs = sorted(all_kfs, key=lambda k: parse_ts(k["timestamp"]))
        current_kf = None
        for k in sorted_kfs:
            if parse_ts(k["timestamp"]) <= time_sec:
                current_kf = k
            else:
                break

        all_trs = [
            tr
            for tr in get_transitions(proj)
            if tr.get("deleted_at") is None
            and tr.get("track_id", "track_1") == track_id
        ]
        active_tr = None
        tr_from_time = 0.0
        tr_to_time = 0.0
        kf_map = {k["id"]: k for k in sorted_kfs}
        for tr in all_trs:
            from_kf = kf_map.get(tr["from"])
            to_kf = kf_map.get(tr["to"])
            if not from_kf or not to_kf:
                continue
            ft = parse_ts(from_kf["timestamp"])
            tt = parse_ts(to_kf["timestamp"])
            sel = tr.get("selected")
            has_video = sel is not None and sel not in (0, "null", "none", "None")
            if has_video and ft <= time_sec < tt:
                vfile = proj / "selected_transitions" / f"{tr['id']}_slot_0.mp4"
                if not vfile.exists():
                    continue
                active_tr = tr
                tr_from_time = ft
                tr_to_time = tt
                break

        snap_dir = proj / "bench_snapshots"
        snap_dir.mkdir(parents=True, exist_ok=True)
        snap_name = f"bench_{int(_time.time() * 1000)}.png"
        snap_path = snap_dir / snap_name

        if active_tr:
            video_path = proj / "selected_transitions" / f"{active_tr['id']}_slot_0.mp4"
            if not video_path.exists():
                raise ApiError(
                    "NOT_FOUND",
                    f"Transition video not found: {active_tr['id']}",
                    status_code=404,
                )
            timeline_dur = tr_to_time - tr_from_time
            progress = (
                (time_sec - tr_from_time) / timeline_dur
                if timeline_dur > 0
                else 0
            )
            probe = sp.run(
                [
                    "ffprobe",
                    "-v",
                    "error",
                    "-show_entries",
                    "format=duration",
                    "-of",
                    "csv=p=0",
                    str(video_path),
                ],
                capture_output=True,
                text=True,
                timeout=5,
            )
            probe_dur = probe.stdout.strip() if probe.returncode == 0 else ""
            probe_dur_f = float(probe_dur) if probe_dur else None
            trim_in = active_tr.get("trim_in") or 0
            trim_out = active_tr.get("trim_out")
            source_dur = active_tr.get("source_video_duration") or probe_dur_f
            if trim_out is None:
                trim_out = source_dur
            if trim_out is None:
                trim_out = active_tr.get("duration_seconds", timeline_dur)
            video_time = float(trim_in) + (
                progress * (float(trim_out) - float(trim_in))
            )
            max_seek = max(0, float(trim_out) - 0.1)
            video_time = min(video_time, max_seek)
            sp.run(
                [
                    "ffmpeg",
                    "-y",
                    "-ss",
                    str(video_time),
                    "-i",
                    str(video_path),
                    "-vframes",
                    "1",
                    "-q:v",
                    "2",
                    str(snap_path),
                ],
                capture_output=True,
                text=True,
                timeout=10,
            )
            label = (
                f"frame @ {int(time_sec // 60)}:{time_sec % 60:05.2f} "
                f"({active_tr['id']})"
            )
        elif current_kf:
            import shutil

            kf_img = proj / "selected_keyframes" / f"{current_kf['id']}.png"
            if kf_img.exists():
                shutil.copy2(str(kf_img), str(snap_path))
            else:
                raise ApiError(
                    "NOT_FOUND",
                    f"No image for {current_kf['id']}",
                    status_code=404,
                )
            label = (
                f"frame @ {int(time_sec // 60)}:{time_sec % 60:05.2f} "
                f"({current_kf['id']})"
            )
        else:
            raise ApiError(
                "NOT_FOUND",
                "No keyframe or transition at this time",
                status_code=404,
            )

        if not snap_path.exists():
            raise ApiError(
                "INTERNAL_ERROR", "Failed to capture frame", status_code=500
            )
        source_path = f"bench_snapshots/{snap_name}"
        bench_id = add_to_bench(proj, "keyframe", source_path, label)
        _log(f"  success: {source_path} ({bench_id})")
        return {"success": True, "benchId": bench_id, "sourcePath": source_path}
    except ApiError:
        raise
    except Exception as e:
        _tb.print_exc()
        raise ApiError("INTERNAL_ERROR", f"{type(e).__name__}: {e}", status_code=500)


@router.post(
    "/api/projects/{name}/bench/upload",
    operation_id="bench_upload",
    summary="Upload a frame snapshot and add to the bench (multipart/form-data)",
)
async def bench_upload(
    name: str, request: Request, proj: Path = Depends(project_dir_dep)
) -> dict:
    """Matches legacy parser: reads raw body, splits on boundary, pulls the
    ``file`` part and optional ``label`` part by hand — FastAPI's
    ``UploadFile`` would implicitly parse with python-multipart but
    re-match the legacy semantics (no temp-file overflow, raw bytes).
    """
    content_type = request.headers.get("content-type", "")
    if "multipart/form-data" not in content_type:
        raise ApiError("BAD_REQUEST", "Expected multipart/form-data", status_code=400)
    try:
        body = await request.body()
        boundary = content_type.split("boundary=")[-1].encode()
        parts = body.split(b"--" + boundary)
        file_data = None
        file_name = None
        label = ""
        for part in parts:
            if b"Content-Disposition" not in part:
                continue
            header_end = part.find(b"\r\n\r\n")
            if header_end < 0:
                continue
            header = part[:header_end].decode("utf-8", errors="replace")
            payload = part[header_end + 4 :]
            if payload.endswith(b"\r\n"):
                payload = payload[:-2]
            if 'name="file"' in header:
                file_data = payload
                for h in header.split("\r\n"):
                    if "filename=" in h:
                        file_name = (
                            h.split("filename=")[-1].strip('"').strip("'")
                        )
            elif 'name="label"' in header:
                label = payload.decode("utf-8", errors="replace").strip()
        if not file_data or not file_name:
            raise ApiError("BAD_REQUEST", "Missing file upload", status_code=400)

        snap_dir = proj / "bench_snapshots"
        snap_dir.mkdir(parents=True, exist_ok=True)
        out_path = snap_dir / file_name
        out_path.write_bytes(file_data)
        from scenecraft.db import add_to_bench

        source_path = f"bench_snapshots/{file_name}"
        bench_id = add_to_bench(proj, "keyframe", source_path, label or file_name)
        _log(f"bench-upload: {name} {source_path} -> {bench_id}")
        return {"success": True, "benchId": bench_id, "sourcePath": source_path}
    except ApiError:
        raise
    except Exception as e:
        raise ApiError("INTERNAL_ERROR", str(e), status_code=500)


@router.post(
    "/api/projects/{name}/bench/add",
    operation_id="bench_add",
    summary="Add an existing keyframe or transition to the bench",
)
async def bench_add(
    name: str, body: BenchAddBody, proj: Path = Depends(project_dir_dep)
) -> dict:
    from scenecraft.db import add_to_bench, get_keyframe, get_transition

    bench_type = body.type
    entity_id = body.entityId
    source_path = body.sourcePath
    label = body.label or ""
    if not bench_type or (not entity_id and not source_path):
        raise ApiError(
            "BAD_REQUEST",
            "Missing 'type' and ('entityId' or 'sourcePath')",
            status_code=400,
        )
    try:
        if not source_path and entity_id:
            if bench_type == "transition":
                tr = get_transition(proj, entity_id)
                if tr:
                    source_path = f"selected_transitions/{entity_id}_slot_0.mp4"
                    if not label:
                        label = f"{entity_id} ({tr['from']}→{tr['to']})"
            elif bench_type == "keyframe":
                kf = get_keyframe(proj, entity_id)
                if kf:
                    source_path = f"selected_keyframes/{entity_id}.png"
                    if not label:
                        label = f"{entity_id} @ {kf['timestamp']}"
        if not source_path:
            raise ApiError(
                "NOT_FOUND", f"Entity {entity_id} not found", status_code=404
            )
        bench_id = add_to_bench(proj, bench_type, source_path, label)
        _log(f"bench-add: {name} {bench_type} {source_path} -> {bench_id}")
        return {"success": True, "benchId": bench_id}
    except ApiError:
        raise
    except Exception as e:
        raise ApiError("INTERNAL_ERROR", str(e), status_code=500)


@router.post(
    "/api/projects/{name}/bench/remove",
    operation_id="bench_remove",
    summary="Remove an item from the bench",
)
async def bench_remove(
    name: str, body: BenchRemoveBody, proj: Path = Depends(project_dir_dep)
) -> dict:
    bench_id = body.benchId
    if not bench_id:
        raise ApiError("BAD_REQUEST", "Missing 'benchId'", status_code=400)
    try:
        _log(f"bench-remove: {bench_id}")
        from scenecraft.db import remove_from_bench

        remove_from_bench(proj, bench_id)
        return {"success": True}
    except Exception as e:
        raise ApiError("INTERNAL_ERROR", str(e), status_code=500)


__all__ = ["router"]
