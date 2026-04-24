"""Canonical content hash of a project's mix graph.

The ``mix_graph_hash`` is the cache key for master-bus mix analysis (M15).
Every factor that can change the rendered output of the master bus MUST
contribute to the hash; two projects with identical mix state MUST produce
the same hex digest.

Covered factors:
- Audio tracks: id, display_order, muted, solo, volume_curve
- Audio clips (non-deleted only): id, track_id, source_path, source_offset,
  start_time, end_time, volume_curve, muted
- Track effects: id, track_id, effect_type, order_index, enabled,
  static_params (JSON-sorted keys). Includes master-bus effects (rows with
  ``track_id IS NULL``) — they process the summed master bus and absolutely
  affect the rendered output, so they must contribute to the hash. SQLite's
  default ``ORDER BY track_id ASC`` places NULLs first, giving a deterministic
  position for the master-bus chain.
- Effect curves: id, effect_id, param_name, points, interpolation (covers
  automation on master-bus effects automatically — ``effect_id`` FK is
  track-agnostic)
- Send buses: id, bus_type, label, order_index, static_params
- Track sends: track_id, bus_id, level

NOT covered (does not affect mix output):
- ``audio_tracks.hidden`` / ``audio_tracks.name`` (UI state)
- ``audio_clips.label`` / ``audio_clips.remap`` display metadata
- ``effect_curves.visible`` (UI state)
- ``audio_clips.deleted_at`` — we filter those rows out entirely
- ``project_frequency_labels`` (analysis overlay, not a signal path)

Canonicalization rules:
- Each table is queried with a deterministic ORDER BY (never rely on
  insertion order).
- Each row is serialized with ``json.dumps(..., sort_keys=True)``. JSON-valued
  columns (``volume_curve``, ``static_params``, ``points``) are parsed first
  so their internal key order is also normalized; a raw string round-trip
  would fail the "dict reorder → same hash" invariant.
- JSON parse failures fall back to the raw string (preserves hash stability
  for pre-existing malformed rows — don't silently "fix" them here).
- Tables are hashed in a fixed order; each table section is delimited by a
  header line so empty tables still contribute a known sentinel.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Any

from scenecraft.db import get_db


def _canon_json(value: str | None) -> Any:
    """Parse a JSON string column and return the canonical JSON-sortable
    structure, or the raw string on parse failure (preserves stability)."""
    if value is None:
        return None
    try:
        return json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return value


def _dump(obj: Any) -> str:
    """Sorted-keys JSON serialization used for every row."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"))


def _hash_table(
    conn: sqlite3.Connection,
    header: str,
    sql: str,
    row_to_dict,
) -> list[str]:
    """Query ``sql`` and produce hash-input lines: a header + one canonical
    JSON object per row."""
    lines = [f"# {header}"]
    for row in conn.execute(sql).fetchall():
        lines.append(_dump(row_to_dict(row)))
    return lines


def compute_mix_graph_hash(project_dir: Path) -> str:
    """Canonical SHA-256 over every factor that affects mix output.

    Returns a 64-character hex string. Stable across repeated calls on an
    unchanged database; identical across two databases with equivalent mix
    state; changes on any edit that would alter the rendered master bus.
    """
    conn = get_db(project_dir)
    sections: list[list[str]] = []

    sections.append(_hash_table(
        conn,
        "audio_tracks",
        "SELECT id, display_order, muted, solo, volume_curve "
        "FROM audio_tracks ORDER BY display_order, id",
        lambda r: {
            "id": r["id"],
            "display_order": int(r["display_order"]),
            "muted": int(r["muted"]),
            "solo": int(r["solo"]),
            "volume_curve": _canon_json(r["volume_curve"]),
        },
    ))

    sections.append(_hash_table(
        conn,
        "audio_clips",
        "SELECT id, track_id, source_path, source_offset, start_time, "
        "end_time, volume_curve, muted FROM audio_clips "
        "WHERE deleted_at IS NULL ORDER BY track_id, start_time, id",
        lambda r: {
            "id": r["id"],
            "track_id": r["track_id"],
            "source_path": r["source_path"],
            "source_offset": float(r["source_offset"]),
            "start_time": float(r["start_time"]),
            "end_time": float(r["end_time"]),
            "volume_curve": _canon_json(r["volume_curve"]),
            "muted": int(r["muted"]),
        },
    ))

    sections.append(_hash_table(
        conn,
        "track_effects",
        "SELECT id, track_id, effect_type, order_index, enabled, static_params "
        "FROM track_effects ORDER BY track_id, order_index, id",
        lambda r: {
            "id": r["id"],
            "track_id": r["track_id"],
            "effect_type": r["effect_type"],
            "order_index": int(r["order_index"]),
            "enabled": int(r["enabled"]),
            "static_params": _canon_json(r["static_params"]),
        },
    ))

    sections.append(_hash_table(
        conn,
        "effect_curves",
        "SELECT id, effect_id, param_name, points, interpolation "
        "FROM effect_curves ORDER BY effect_id, param_name, id",
        lambda r: {
            "id": r["id"],
            "effect_id": r["effect_id"],
            "param_name": r["param_name"],
            "points": _canon_json(r["points"]),
            "interpolation": r["interpolation"],
        },
    ))

    sections.append(_hash_table(
        conn,
        "project_send_buses",
        "SELECT id, bus_type, label, order_index, static_params "
        "FROM project_send_buses ORDER BY order_index, id",
        lambda r: {
            "id": r["id"],
            "bus_type": r["bus_type"],
            "label": r["label"],
            "order_index": int(r["order_index"]),
            "static_params": _canon_json(r["static_params"]),
        },
    ))

    sections.append(_hash_table(
        conn,
        "track_sends",
        "SELECT track_id, bus_id, level FROM track_sends "
        "ORDER BY track_id, bus_id",
        lambda r: {
            "track_id": r["track_id"],
            "bus_id": r["bus_id"],
            "level": float(r["level"]),
        },
    ))

    payload = "\n".join(line for section in sections for line in section)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


__all__ = ["compute_mix_graph_hash"]
