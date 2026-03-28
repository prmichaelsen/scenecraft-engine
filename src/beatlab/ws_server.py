"""WebSocket server for real-time job progress — runs alongside the HTTP API server."""

from __future__ import annotations

import asyncio
import json
import sys
import threading
import uuid
from dataclasses import dataclass, field
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


def start_ws_server(host: str = "0.0.0.0", port: int = 8889):
    """Start the WebSocket server in a background thread."""
    def _run():
        asyncio.run(_run_ws_server(host, port))

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()
    return thread
