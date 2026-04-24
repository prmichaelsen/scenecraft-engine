"""Query helpers for the M16 ``audio_bounces`` cache.

An ``audio_bounces`` row represents one rendered WAV on disk at
``pool/bounces/<composite_hash>.wav``. The ``composite_hash`` is the unique
cache key — SHA-256 over ``(mix_graph_hash + selection + format)`` as
computed by :func:`scenecraft.bounce_hash.compute_bounce_hash`. Two bounces
with identical mix graph + selection + format share a hash and reuse the
same file.

Rows start with ``rendered_path=None`` (render in flight) and are updated
to the final relative path once the frontend-uploaded WAV is written to
disk by the ``bounce-upload`` endpoint. Sibling module to
``db_mix_cache.py`` (master-bus analysis cache) — mirrors its shape.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from scenecraft.db import generate_id, get_db
from scenecraft.db_models import AudioBounce


def get_bounce_by_hash(
    project_dir: Path, composite_hash: str,
) -> AudioBounce | None:
    """Return the cached bounce row matching ``composite_hash``, or ``None``.

    This is the primary cache-lookup path — callers check the hash first
    and only call :func:`create_bounce` on a miss.
    """
    row = get_db(project_dir).execute(
        "SELECT * FROM audio_bounces WHERE composite_hash = ?",
        (composite_hash,),
    ).fetchone()
    return _row_to_bounce(row) if row else None


def get_bounce_by_id(
    project_dir: Path, bounce_id: str,
) -> AudioBounce | None:
    """Return the bounce row by primary key, or ``None``. Used by the
    download endpoint to resolve ``/bounces/<id>.wav``."""
    row = get_db(project_dir).execute(
        "SELECT * FROM audio_bounces WHERE id = ?", (bounce_id,),
    ).fetchone()
    return _row_to_bounce(row) if row else None


def create_bounce(
    project_dir: Path,
    *,
    composite_hash: str,
    start_time_s: float,
    end_time_s: float,
    mode: str,
    selection: dict,
    sample_rate: int,
    bit_depth: int,
    channels: int = 2,
    rendered_path: str | None = None,
    size_bytes: int | None = None,
    duration_s: float | None = None,
    created_at: str | None = None,
) -> AudioBounce:
    """Insert a new ``audio_bounces`` row. Caller is responsible for
    checking the cache via :func:`get_bounce_by_hash` first.

    ``rendered_path`` defaults to ``None`` to represent an in-flight render;
    call :func:`update_bounce_rendered` once the WAV has landed on disk.
    """
    bounce_id = generate_id("bounce")
    if created_at is None:
        created_at = datetime.now(timezone.utc).isoformat()
    conn = get_db(project_dir)
    conn.execute(
        "INSERT INTO audio_bounces (id, composite_hash, start_time_s, "
        "end_time_s, mode, selection_json, sample_rate, bit_depth, channels, "
        "rendered_path, size_bytes, duration_s, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            bounce_id, composite_hash, float(start_time_s), float(end_time_s),
            mode, json.dumps(selection, sort_keys=True),
            int(sample_rate), int(bit_depth), int(channels),
            rendered_path,
            int(size_bytes) if size_bytes is not None else None,
            float(duration_s) if duration_s is not None else None,
            created_at,
        ),
    )
    conn.commit()
    return AudioBounce(
        id=bounce_id,
        composite_hash=composite_hash,
        start_time_s=float(start_time_s),
        end_time_s=float(end_time_s),
        mode=mode,
        selection=dict(selection),
        sample_rate=int(sample_rate),
        bit_depth=int(bit_depth),
        channels=int(channels),
        rendered_path=rendered_path,
        size_bytes=int(size_bytes) if size_bytes is not None else None,
        duration_s=float(duration_s) if duration_s is not None else None,
        created_at=created_at,
    )


def update_bounce_rendered(
    project_dir: Path,
    bounce_id: str,
    rendered_path: str,
    size_bytes: int,
    duration_s: float,
) -> None:
    """Mark a bounce row as rendered: set ``rendered_path`` + the on-disk
    ``size_bytes`` and actual ``duration_s``. No-op if the row doesn't exist.
    """
    conn = get_db(project_dir)
    conn.execute(
        "UPDATE audio_bounces SET rendered_path = ?, size_bytes = ?, "
        "duration_s = ? WHERE id = ?",
        (rendered_path, int(size_bytes), float(duration_s), bounce_id),
    )
    conn.commit()


def delete_bounce(project_dir: Path, bounce_id: str) -> None:
    """Delete a bounce row. Caller is responsible for the on-disk WAV."""
    conn = get_db(project_dir)
    conn.execute("DELETE FROM audio_bounces WHERE id = ?", (bounce_id,))
    conn.commit()


def list_bounces(project_dir: Path) -> list[AudioBounce]:
    """All bounce rows for a project, newest first."""
    rows = get_db(project_dir).execute(
        "SELECT * FROM audio_bounces ORDER BY created_at DESC",
    ).fetchall()
    return [_row_to_bounce(r) for r in rows]


# ── Row decoder ─────────────────────────────────────────────────────


def _row_to_bounce(row: sqlite3.Row) -> AudioBounce:
    try:
        selection = json.loads(row["selection_json"]) if row["selection_json"] else {}
    except (json.JSONDecodeError, TypeError):
        selection = {}
    return AudioBounce(
        id=row["id"],
        composite_hash=row["composite_hash"],
        start_time_s=float(row["start_time_s"]),
        end_time_s=float(row["end_time_s"]),
        mode=row["mode"],
        selection=selection,
        sample_rate=int(row["sample_rate"]),
        bit_depth=int(row["bit_depth"]),
        channels=int(row["channels"]),
        rendered_path=row["rendered_path"],
        size_bytes=int(row["size_bytes"]) if row["size_bytes"] is not None else None,
        duration_s=float(row["duration_s"]) if row["duration_s"] is not None else None,
        created_at=row["created_at"],
    )


__all__ = [
    "get_bounce_by_hash",
    "get_bounce_by_id",
    "create_bounce",
    "update_bounce_rendered",
    "delete_bounce",
    "list_bounces",
]
