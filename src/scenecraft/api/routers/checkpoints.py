"""Checkpoints + undo/redo router (M16 T64).

Ports the following legacy endpoints verbatim:

  * ``GET  /api/projects/{name}/checkpoints``       — ``list_checkpoints``
  * ``GET  /api/projects/{name}/undo-history``      — ``get_undo_history``
  * ``POST /api/projects/{name}/checkpoint``        — ``checkpoint`` 🔧🔒
  * ``POST /api/projects/{name}/checkpoint/restore``— ``restore_checkpoint`` 🔧
  * ``POST /api/projects/{name}/checkpoint/delete`` — ``delete_checkpoint``
  * ``POST /api/projects/{name}/undo``              — ``undo``
  * ``POST /api/projects/{name}/redo``              — ``redo``

🔧 operationId matches an existing chat-tool name (kept stable so T66's
codegen can emit the same tool signatures the chat agent already uses).

🔒 ``checkpoint`` lives in ``STRUCTURAL_ROUTES`` — it mutates the
timeline (snapshot → new file) and must serialize under ``project_lock``.
Restore/delete intentionally do NOT carry the structural dep because
the legacy server didn't either: restore calls ``close_db`` itself
before swapping the SQLite backing file, which races with the per-
project lock if both tried to acquire it. The legacy lock only wraps
"mutations inside the running DB"; restore/delete are out-of-band.

Business logic is imported from ``scenecraft.db`` / ``scenecraft.chat``;
we copy the legacy endpoint shape line-for-line. No behavior rewrite.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, Depends, status

from scenecraft.api.deps import current_user, project_dir, project_lock
from scenecraft.api.errors import ApiError
from scenecraft.api.models.checkpoints import (
    CheckpointCreateBody,
    CheckpointDeleteBody,
    CheckpointRestoreBody,
)

router = APIRouter(
    prefix="/api/projects",
    tags=["checkpoints"],
    dependencies=[Depends(current_user)],
)


def _log(msg: str) -> None:
    """Mirror ``api_server._log`` for byte-for-byte log parity."""
    import sys
    from datetime import datetime as _dt

    ts = _dt.now().strftime("%H:%M:%S")
    print(f"[{ts}] {msg}", file=sys.stderr, flush=True)


# ---------------------------------------------------------------------------
# GET — listings
# ---------------------------------------------------------------------------


@router.get(
    "/{name}/checkpoints",
    operation_id="list_checkpoints",
    summary="List project checkpoints (snapshot files + metadata)",
)
async def list_checkpoints(
    name: str, pd: Path = Depends(project_dir)
) -> dict:
    """Return ``{"checkpoints": [...], "active": "project.db"}``.

    Reads the ``checkpoints`` metadata table plus the filesystem for any
    snapshot file whose metadata row was lost (e.g. restored from an older
    DB). The filesystem is authoritative for presence; the table supplies
    the human-readable label.
    """
    from scenecraft.db import list_checkpoints as _db_list_checkpoints

    meta_by_file = {c["filename"]: c for c in _db_list_checkpoints(pd)}
    checkpoints: list[dict] = []
    for f in sorted(pd.glob("project.db.checkpoint-*"), reverse=True):
        stat = f.stat()
        meta = meta_by_file.get(f.name, {})
        checkpoints.append(
            {
                "filename": f.name,
                "name": meta.get("name", ""),
                "created": meta.get("created_at")
                or datetime.fromtimestamp(stat.st_mtime).isoformat(),
                "size_bytes": stat.st_size,
            }
        )
    return {"checkpoints": checkpoints, "active": "project.db"}


@router.get(
    "/{name}/undo-history",
    operation_id="get_undo_history",
    summary="Return the project's undo/redo history rows",
)
async def get_undo_history(
    name: str, pd: Path = Depends(project_dir)
) -> dict:
    from scenecraft.db import undo_history

    return {"history": undo_history(pd)}


# ---------------------------------------------------------------------------
# POST — structural checkpoint create (🔒 project_lock)
# ---------------------------------------------------------------------------


@router.post(
    "/{name}/checkpoint",
    operation_id="checkpoint",
    summary="Snapshot project.db into a new checkpoint file",
    dependencies=[Depends(project_lock)],
)
async def checkpoint(
    name: str,
    body: CheckpointCreateBody,
    pd: Path = Depends(project_dir),
) -> dict:
    """Use SQLite's online backup API to snapshot the active DB.

    Snapshot filename embeds a second-resolution timestamp — two very
    rapid snapshots in the same second would collide, but the
    ``project_lock`` serializes creates so only one request holds the
    lock at a time; a second request that lands in the same second
    would overwrite its own file and get the same name. Legacy had
    identical behavior — we don't fix it here.
    """
    db_path = pd / "project.db"
    if not db_path.exists():
        raise ApiError(
            "NOT_FOUND",
            "No project.db found",
            status_code=status.HTTP_404_NOT_FOUND,
        )
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"project.db.checkpoint-{ts}"
    dst = pd / filename

    # SQLite backup API — safe for WAL-mode DBs because it takes a page-
    # level snapshot over the live write-ahead log.
    src_conn = sqlite3.connect(str(db_path))
    dst_conn = sqlite3.connect(str(dst))
    try:
        src_conn.backup(dst_conn)
    finally:
        dst_conn.close()
        src_conn.close()

    # Persist checkpoint metadata to the ``checkpoints`` table so the
    # listing endpoint can surface a friendly name next to the raw file.
    from scenecraft.db import add_checkpoint as _db_add_checkpoint

    label = body.name or ""
    _db_add_checkpoint(
        pd,
        filename,
        name=label,
        created_at=datetime.now().astimezone().isoformat(),
    )
    _log(f"checkpoint: {name} -> {filename}{' (' + label + ')' if label else ''}")
    return {"success": True, "filename": filename, "name": label}


@router.post(
    "/{name}/checkpoint/restore",
    operation_id="restore_checkpoint",
    summary="Restore project.db from an existing checkpoint file",
)
async def restore_checkpoint(
    name: str,
    body: CheckpointRestoreBody,
    pd: Path = Depends(project_dir),
) -> dict:
    """Close live connections and backup-copy the snapshot over project.db.

    Not structural because we explicitly close the DB mid-request —
    acquiring ``project_lock`` here would deadlock against any handler
    running concurrently that already holds a connection. The legacy
    server had identical semantics.
    """
    filename = body.filename
    checkpoint_path = pd / filename
    if (
        not filename.startswith("project.db.checkpoint-")
        or not checkpoint_path.exists()
    ):
        raise ApiError(
            "NOT_FOUND",
            f"Checkpoint not found: {filename}",
            status_code=status.HTTP_404_NOT_FOUND,
        )
    db_path = pd / "project.db"

    # Close every cached connection so SQLite's backup() can reopen the
    # destination exclusively. ``close_db`` is idempotent and thread-safe.
    from scenecraft.db import close_db

    close_db(pd)

    src_conn = sqlite3.connect(str(checkpoint_path))
    dst_conn = sqlite3.connect(str(db_path))
    try:
        src_conn.backup(dst_conn)
    finally:
        dst_conn.close()
        src_conn.close()
    _log(f"checkpoint restore: {name} <- {filename}")
    return {"success": True, "message": f"Restored from {filename}"}


@router.post(
    "/{name}/checkpoint/delete",
    operation_id="delete_checkpoint",
    summary="Delete a checkpoint file + its metadata row",
)
async def delete_checkpoint(
    name: str,
    body: CheckpointDeleteBody,
    pd: Path = Depends(project_dir),
) -> dict:
    filename = body.filename
    checkpoint_path = pd / filename
    if (
        not filename.startswith("project.db.checkpoint-")
        or not checkpoint_path.exists()
    ):
        raise ApiError(
            "NOT_FOUND",
            f"Checkpoint not found: {filename}",
            status_code=status.HTTP_404_NOT_FOUND,
        )
    checkpoint_path.unlink()
    from scenecraft.db import remove_checkpoint as _db_remove_checkpoint

    _db_remove_checkpoint(pd, filename)
    _log(f"checkpoint deleted: {name} / {filename}")
    return {"success": True}


# ---------------------------------------------------------------------------
# POST — undo / redo
# ---------------------------------------------------------------------------


@router.post(
    "/{name}/undo",
    operation_id="undo",
    summary="Undo the most recent undo group",
)
async def undo(name: str, pd: Path = Depends(project_dir)) -> dict:
    from scenecraft.db import undo_execute

    result = undo_execute(pd)
    if result:
        _log(f"undo: {result['description']}")
        return {"success": True, **result}
    return {"success": False, "message": "Nothing to undo"}


@router.post(
    "/{name}/redo",
    operation_id="redo",
    summary="Redo the most recently undone undo group",
)
async def redo(name: str, pd: Path = Depends(project_dir)) -> dict:
    from scenecraft.db import redo_execute

    result = redo_execute(pd)
    if result:
        _log(f"redo: {result['description']}")
        return {"success": True, **result}
    return {"success": False, "message": "Nothing to redo"}


__all__ = ["router"]
