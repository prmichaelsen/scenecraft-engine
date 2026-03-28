"""WebSocket server for real-time job progress — runs alongside the HTTP API server."""

from __future__ import annotations

import asyncio
import json
import sys
import time
import threading
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import websockets
from websockets.asyncio.server import ServerConnection


def _log(msg: str):
    from datetime import datetime
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] [ws] {msg}", file=sys.stderr, flush=True)


# ── Job Manager (thread-safe) ────────────────────────────────────────


@dataclass
class Job:
    id: str
    type: str  # "transition_candidates", "keyframe_candidates", etc.
    status: str = "pending"  # pending, running, completed, failed
    completed: int = 0
    total: int = 0
    result: Any = None
    error: str | None = None
    meta: dict = field(default_factory=dict)


class JobManager:
    """Thread-safe job tracking with WebSocket broadcast."""

    def __init__(self):
        self._jobs: dict[str, Job] = {}
        self._lock = threading.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._connections: set[ServerConnection] = set()

    def set_loop(self, loop: asyncio.AbstractEventLoop):
        self._loop = loop

    def create_job(self, job_type: str, total: int = 0, meta: dict | None = None) -> str:
        job_id = f"job_{uuid.uuid4().hex[:8]}"
        job = Job(id=job_id, type=job_type, status="running", total=total, meta=meta or {})
        with self._lock:
            self._jobs[job_id] = job
        self._broadcast({"type": "job_started", "jobId": job_id, "jobType": job_type, "total": total, "meta": meta or {}})
        return job_id

    def update_progress(self, job_id: str, completed: int, detail: str = ""):
        with self._lock:
            job = self._jobs.get(job_id)
            if not job:
                return
            job.completed = completed
        self._broadcast({"type": "job_progress", "jobId": job_id, "completed": completed, "total": job.total, "detail": detail})

    def complete_job(self, job_id: str, result: Any = None):
        with self._lock:
            job = self._jobs.get(job_id)
            if not job:
                return
            job.status = "completed"
            job.result = result
        self._broadcast({"type": "job_completed", "jobId": job_id, "result": result})

    def fail_job(self, job_id: str, error: str):
        with self._lock:
            job = self._jobs.get(job_id)
            if not job:
                return
            job.status = "failed"
            job.error = error
        self._broadcast({"type": "job_failed", "jobId": job_id, "error": error})

    def get_job(self, job_id: str) -> Job | None:
        with self._lock:
            return self._jobs.get(job_id)

    def register_connection(self, ws: ServerConnection):
        self._connections.add(ws)

    def unregister_connection(self, ws: ServerConnection):
        self._connections.discard(ws)

    def _broadcast(self, message: dict):
        """Broadcast a message to all connected WebSocket clients."""
        if not self._loop or not self._connections:
            return
        data = json.dumps(message)
        for ws in list(self._connections):
            try:
                asyncio.run_coroutine_threadsafe(ws.send(data), self._loop)
            except Exception:
                self._connections.discard(ws)


# ── Singleton ─────────────────────────────────────────────────────────

job_manager = JobManager()


# ── WebSocket Server ──────────────────────────────────────────────────


async def _handle_connection(ws: ServerConnection):
    job_manager.register_connection(ws)
    _log(f"Client connected ({len(job_manager._connections)} total)")
    try:
        async for message in ws:
            try:
                data = json.loads(message)
                msg_type = data.get("type")

                if msg_type == "ping":
                    await ws.send(json.dumps({"type": "pong"}))

                elif msg_type == "get_job":
                    job_id = data.get("jobId")
                    job = job_manager.get_job(job_id)
                    if job:
                        await ws.send(json.dumps({
                            "type": "job_status",
                            "jobId": job.id,
                            "status": job.status,
                            "completed": job.completed,
                            "total": job.total,
                            "result": job.result,
                            "error": job.error,
                        }))
                    else:
                        await ws.send(json.dumps({"type": "error", "message": f"Job {job_id} not found"}))

            except json.JSONDecodeError:
                await ws.send(json.dumps({"type": "error", "message": "Invalid JSON"}))
    except websockets.exceptions.ConnectionClosed:
        pass
    finally:
        job_manager.unregister_connection(ws)
        _log(f"Client disconnected ({len(job_manager._connections)} total)")


async def _run_ws_server(host: str, port: int):
    job_manager.set_loop(asyncio.get_running_loop())
    async with websockets.serve(_handle_connection, host, port):
        _log(f"WebSocket server running at ws://{host}:{port}")
        await asyncio.Future()  # run forever


# ── Folder Watcher ────────────────────────────────────────────────────

IMAGE_EXTS = {'.png', '.jpg', '.jpeg', '.webp'}
VIDEO_EXTS = {'.mp4', '.webm', '.mov'}


class FolderWatcher:
    """Watches folders for new files and auto-imports them to project bins."""

    def __init__(self, work_dir: Path):
        self._work_dir = work_dir
        self._watches: dict[str, dict] = {}  # key: "project:path" -> config
        self._seen: dict[str, set[str]] = {}  # key: "project:path" -> set of filenames already seen
        self._lock = threading.Lock()
        self._running = False
        self._thread: threading.Thread | None = None

    def add_watch(self, project: str, folder_path: str) -> dict:
        """Start watching a folder for new importable files."""
        key = f"{project}:{folder_path}"

        # Resolve the folder
        resolved = (self._work_dir / folder_path).resolve() if not Path(folder_path).is_absolute() else Path(folder_path).resolve()
        if not resolved.is_dir():
            raise ValueError(f"Not a directory: {folder_path}")

        # Snapshot current files
        current_files = set()
        for f in resolved.iterdir():
            if f.is_file() and f.suffix.lower() in (IMAGE_EXTS | VIDEO_EXTS):
                current_files.add(f.name)

        with self._lock:
            self._watches[key] = {
                "project": project,
                "folder_path": folder_path,
                "resolved": resolved,
            }
            self._seen[key] = current_files

        if not self._running:
            self._start()

        _log(f"Watching folder: {resolved} for project {project} ({len(current_files)} existing files)")
        return {"watching": str(resolved), "existingFiles": len(current_files)}

    def remove_watch(self, project: str, folder_path: str):
        key = f"{project}:{folder_path}"
        with self._lock:
            self._watches.pop(key, None)
            self._seen.pop(key, None)
        _log(f"Stopped watching: {folder_path} for project {project}")

    def get_watches(self, project: str) -> list[str]:
        with self._lock:
            return [w["folder_path"] for k, w in self._watches.items() if w["project"] == project]

    def _start(self):
        self._running = True
        self._thread = threading.Thread(target=self._poll_loop, daemon=True)
        self._thread.start()

    def _poll_loop(self):
        while self._running:
            time.sleep(3)
            self._check_all()

    def _check_all(self):
        with self._lock:
            watches = dict(self._watches)
            seen = dict(self._seen)

        for key, config in watches.items():
            resolved: Path = config["resolved"]
            project: str = config["project"]
            if not resolved.is_dir():
                continue

            current_files = set()
            for f in resolved.iterdir():
                if f.is_file() and f.suffix.lower() in (IMAGE_EXTS | VIDEO_EXTS):
                    current_files.add(f.name)

            prev_seen = seen.get(key, set())
            new_files = current_files - prev_seen
            if not new_files:
                continue

            with self._lock:
                self._seen[key] = current_files

            # Import new files
            self._import_files(project, resolved, sorted(new_files))

    def _import_files(self, project: str, folder: Path, filenames: list[str]):
        """Import new files into the project bin."""
        import yaml as pyyaml
        import shutil
        from datetime import datetime, timezone

        yaml_path = self._work_dir / project / "narrative_keyframes.yaml"
        project_dir = self._work_dir / project

        if not yaml_path.exists():
            return

        with open(yaml_path) as f:
            parsed = pyyaml.safe_load(f) or {}

        keyframes = parsed.get("keyframes", [])
        kf_bin = parsed.get("bin", [])
        tr_bin = parsed.get("transition_bin", [])

        all_kf_ids = [kf.get("id", "") for kf in keyframes + kf_bin]
        max_kf = max((int(m.group(1)) for kid in all_kf_ids if (m := __import__('re').match(r'kf_(\d+)', kid))), default=0)

        all_tr_ids = [tr.get("id", "") for tr in parsed.get("transitions", []) + tr_bin]
        max_tr = max((int(m.group(1)) for tid in all_tr_ids if (m := __import__('re').match(r'tr_(\d+)', tid))), default=0)

        now = datetime.now(timezone.utc).isoformat()
        selected_kf_dir = project_dir / "selected_keyframes"
        selected_kf_dir.mkdir(parents=True, exist_ok=True)
        selected_tr_dir = project_dir / "selected_transitions"
        selected_tr_dir.mkdir(parents=True, exist_ok=True)

        imported_kf = []
        imported_tr = []

        for name in filenames:
            f = folder / name
            ext = f.suffix.lower()

            if ext in IMAGE_EXTS:
                max_kf += 1
                kf_id = f"kf_{max_kf:03d}"
                dest = selected_kf_dir / f"{kf_id}.png"
                shutil.copy2(str(f), str(dest))
                kf_bin.append({
                    "id": kf_id,
                    "timestamp": "0:00",
                    "section": "",
                    "source": str(f),
                    "prompt": f"Auto-imported from {name}",
                    "context": None,
                    "candidates": [],
                    "selected": 1,
                    "deleted_at": now,
                })
                imported_kf.append(kf_id)

            elif ext in VIDEO_EXTS:
                max_tr += 1
                tr_id = f"tr_{max_tr:03d}"
                dest = selected_tr_dir / f"{tr_id}_slot_0{ext}"
                shutil.copy2(str(f), str(dest))
                tr_bin.append({
                    "id": tr_id,
                    "from": "",
                    "to": "",
                    "duration_seconds": 0,
                    "slots": 1,
                    "action": f"Auto-imported from {name}",
                    "candidates": [],
                    "selected": [],
                    "remap": {"method": "linear", "target_duration": 0},
                    "deleted_at": now,
                })
                imported_tr.append(tr_id)

        if not imported_kf and not imported_tr:
            return

        parsed["bin"] = kf_bin
        parsed["transition_bin"] = tr_bin

        with open(yaml_path, "w") as f:
            pyyaml.dump(parsed, f, default_flow_style=False, allow_unicode=True, width=1000)

        summary = f"{len(imported_kf)} keyframe(s), {len(imported_tr)} transition(s)"
        _log(f"Auto-imported from watched folder: {summary}")

        # Broadcast over WebSocket
        job_manager._broadcast({
            "type": "folder_import",
            "project": project,
            "imported": {"keyframes": imported_kf, "transitions": imported_tr},
            "summary": summary,
        })


folder_watcher: FolderWatcher | None = None


def start_ws_server(host: str = "0.0.0.0", port: int = 8889):
    """Start the WebSocket server in a background thread."""
    def _run():
        asyncio.run(_run_ws_server(host, port))

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()
    return thread
