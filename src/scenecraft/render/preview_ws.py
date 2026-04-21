"""WebSocket handler for /ws/preview-stream/:project_name.

Wire protocol:
  - Client → server (JSON text frames):
      {"action": "play", "t": 0.0}
      {"action": "seek", "t": 12.5}
      {"action": "pause"}
      {"action": "stop"}
  - Server → client (binary frames):
      first: fMP4 init segment
      then: fMP4 media segments, one per ~1s of rendered content
  - Server → client (text frames):
      {"type": "error", "error": "..."}

On connection open we resolve project_dir from the URL path, look up (or
spawn) a RenderWorker via the RenderCoordinator, and start two asyncio
tasks: one that reads client commands, one that forwards encoded fragments
back as binary frames.

On connection close we call worker.pause(); the coordinator's idle
eviction tears the worker down if nobody reconnects.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from pathlib import Path
from typing import Any

import websockets
from websockets.asyncio.server import ServerConnection

from scenecraft.render.preview_worker import RenderCoordinator, RenderWorker


logger = logging.getLogger(__name__)


def _log(msg: str) -> None:
    from datetime import datetime
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] [preview-ws] {msg}", file=sys.stderr, flush=True)


async def _pump_fragments(ws: ServerConnection, worker: RenderWorker) -> None:
    """Forward fragments from the worker to the websocket as binary frames."""
    loop = asyncio.get_running_loop()
    iterator = worker.fragments()

    def _next_chunk():
        try:
            return next(iterator)
        except StopIteration:
            return None

    try:
        while True:
            chunk = await loop.run_in_executor(None, _next_chunk)
            if chunk is None:
                break
            try:
                await ws.send(chunk)
            except websockets.exceptions.ConnectionClosed:
                break
    except Exception as exc:  # pragma: no cover — best-effort
        logger.exception("preview-stream pump failed: %s", exc)


async def _read_commands(ws: ServerConnection, worker: RenderWorker) -> None:
    """Consume client commands until the socket closes."""
    try:
        async for message in ws:
            if isinstance(message, (bytes, bytearray)):
                continue  # no binary messages expected from client
            try:
                data = json.loads(message)
            except json.JSONDecodeError:
                await ws.send(json.dumps({"type": "error", "error": "Invalid JSON"}))
                continue
            action = data.get("action")
            t = data.get("t", 0.0)
            try:
                t = float(t)
            except (TypeError, ValueError):
                t = 0.0
            _log(f"action={action} t={t:.3f}")
            if action == "play":
                worker.play(t)
            elif action == "seek":
                worker.seek(t)
            elif action == "pause":
                worker.pause()
            elif action == "stop":
                worker.stop()
                break
            else:
                await ws.send(json.dumps({"type": "error", "error": f"Unknown action: {action!r}"}))
    except websockets.exceptions.ConnectionClosed:
        pass


async def handle_preview_stream_connection(
    ws: ServerConnection,
    work_dir: Path | None,
    project_name: str,
) -> None:
    """Entry point invoked from ws_server._handle_connection."""
    _log(f"connection opened project={project_name}")
    if not work_dir or not project_name:
        await ws.send(json.dumps({"type": "error", "error": "work_dir not configured"}))
        await ws.close(code=1011, reason="work_dir unset")
        return
    project_dir = Path(work_dir) / project_name
    if not project_dir.is_dir():
        await ws.send(json.dumps({"type": "error", "error": f"Project not found: {project_name}"}))
        await ws.close(code=1008, reason="unknown project")
        return

    coordinator = RenderCoordinator.instance()
    try:
        worker = coordinator.get_worker(project_dir)
        _log(f"worker ready for {project_name}")
    except Exception as exc:
        import traceback
        _log(f"worker spawn failed: {exc}\n{traceback.format_exc()}")
        await ws.send(json.dumps({"type": "error", "error": f"Worker spawn failed: {exc}"}))
        await ws.close(code=1011, reason="worker spawn failed")
        return

    # Kick the pump immediately so the init segment reaches the client before
    # the first fragment (encode_init is called inside fragments()).
    pump_task = asyncio.create_task(_pump_fragments(ws, worker))
    cmd_task = asyncio.create_task(_read_commands(ws, worker))

    try:
        done, pending = await asyncio.wait(
            {pump_task, cmd_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
        for t in pending:
            t.cancel()
    finally:
        worker.pause()
        # Don't release_worker() here — LRU/idle eviction owns lifecycle so
        # reconnects within the idle window get a warm worker.
