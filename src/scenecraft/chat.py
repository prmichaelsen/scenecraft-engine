"""Chat assistant — Claude-powered WebSocket chat with streaming and tool calling."""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from websockets.asyncio.server import ServerConnection


def _log(msg: str):
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] [chat] {msg}", file=sys.stderr, flush=True)


# ── DB helpers ───────────────────────────────────────────────────────


def _add_message(
    project_dir: Path,
    user_id: str,
    role: str,
    content: str,
    images: list[str] | None = None,
    tool_calls: list[dict] | None = None,
):
    from scenecraft.db import get_db
    conn = get_db(project_dir)
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        "INSERT INTO chat_messages (user_id, role, content, images, tool_calls, created_at) VALUES (?, ?, ?, ?, ?, ?)",
        (
            user_id,
            role,
            content,
            json.dumps(images) if images else None,
            json.dumps(tool_calls) if tool_calls else None,
            now,
        ),
    )
    conn.commit()
    msg_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    # Decode content back to blocks for the frontend if JSON-encoded
    display_content: Any = content
    try:
        parsed = json.loads(content)
        if isinstance(parsed, list):
            display_content = parsed
    except (json.JSONDecodeError, TypeError):
        pass
    return {
        "id": msg_id,
        "user_id": user_id,
        "role": role,
        "content": display_content,
        "images": images,
        "created_at": now,
    }


def _get_messages(project_dir: Path, user_id: str, limit: int = 50) -> list[dict]:
    from scenecraft.db import get_db
    conn = get_db(project_dir)
    rows = conn.execute(
        "SELECT id, user_id, role, content, images, tool_calls, created_at FROM chat_messages WHERE user_id = ? ORDER BY id DESC LIMIT ?",
        (user_id, limit),
    ).fetchall()
    messages = []
    for r in reversed(rows):
        raw_content = r[3]
        content: Any = raw_content
        # Decode JSON content blocks (for assistant tool-using messages)
        if r[2] == "assistant":
            try:
                parsed = json.loads(raw_content)
                if isinstance(parsed, list):
                    content = parsed
            except (json.JSONDecodeError, TypeError):
                pass
        msg: dict[str, Any] = {
            "id": r[0],
            "user_id": r[1],
            "role": r[2],
            "content": content,
            "created_at": r[6],
        }
        if r[4]:
            msg["images"] = json.loads(r[4])
        if r[5]:
            msg["tool_calls"] = json.loads(r[5])
        messages.append(msg)
    return messages


# ── Project context ──────────────────────────────────────────────────


def _build_system_prompt(project_dir: Path, project_name: str) -> str:
    """Build system prompt with project context."""
    from scenecraft.db import get_db
    conn = get_db(project_dir)

    kf_count = conn.execute("SELECT COUNT(*) FROM keyframes WHERE deleted_at IS NULL").fetchone()[0]
    tr_count = conn.execute("SELECT COUNT(*) FROM transitions WHERE deleted_at IS NULL").fetchone()[0]
    track_count = conn.execute("SELECT COUNT(*) FROM tracks").fetchone()[0]

    meta = {}
    for row in conn.execute("SELECT key, value FROM meta").fetchall():
        meta[row[0]] = row[1]

    fps = meta.get("fps", "24")
    resolution = meta.get("resolution", "1920,1080")
    title = meta.get("title", project_name)

    return f"""You are an AI assistant embedded in SceneCraft, a video editing application.
You help the user with their project by answering questions and executing actions.

Project: "{title}" ({project_name})
FPS: {fps} | Resolution: {resolution}
Keyframes: {kf_count} | Transitions: {tr_count} | Tracks: {track_count}

Tools available:
  • sql_query — read-only SELECT against project.db (use for counts, filters, schema
    inspection, ad-hoc analysis). Default 100 rows; pass `limit` for more.
  • update_keyframe_prompt — change a keyframe's prompt text.
  • update_keyframe_timestamp — move a keyframe on the timeline.
  • update_curve — replace a transition's color/opacity curve points.
  • update_transform_curve — replace a transition's transform X/Y/Z curve points.
  • delete_keyframe, delete_transition — soft-delete one item (asks to confirm).
  • batch_delete_keyframes, batch_delete_transitions — soft-delete many in ONE
    confirmation and ONE undo group. Prefer these over looping single-deletes.

All mutations are wrapped in undo groups; the user can undo any change you make.
Prefer sql_query to discover IDs/state before mutating. Do not fabricate IDs —
query first.

Key tables: meta, tracks, keyframes, transitions, kf_candidates, tr_candidates,
chat_messages, audio_tracks. Inspect schema with
`SELECT sql FROM sqlite_master WHERE type='table'` when unsure.

Be concise. Use markdown for formatting when useful."""


# ── Tools ────────────────────────────────────────────────────────────


SQL_QUERY_TOOL: dict = {
    "name": "sql_query",
    "description": (
        "Execute a read-only SQL SELECT statement against the project's SQLite "
        "database (project.db). Use for ad-hoc queries about project state: counting "
        "keyframes per track, finding long transitions, inspecting schema, etc. "
        "Write operations (INSERT/UPDATE/DELETE/CREATE/DROP/ATTACH/PRAGMA-writes) are "
        "rejected at the SQLite authorizer level. Returns {columns, rows, row_count, "
        "truncated, limit}. Results cap at `limit` rows (default 100)."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "sql": {
                "type": "string",
                "description": "SQL SELECT query (or WITH ... SELECT). Must be read-only.",
            },
            "limit": {
                "type": "integer",
                "description": "Max rows to return. Default 100. Raise only when needed.",
                "default": 100,
            },
        },
        "required": ["sql"],
    },
}

UPDATE_KEYFRAME_PROMPT_TOOL: dict = {
    "name": "update_keyframe_prompt",
    "description": (
        "Update a keyframe's prompt text (the text used when generating image "
        "candidates for this keyframe). Wraps the mutation in an undo group so the "
        "user can revert."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "keyframe_id": {"type": "string", "description": "Keyframe ID, e.g. 'kf_a3f7c21b'."},
            "prompt": {"type": "string", "description": "New prompt text."},
        },
        "required": ["keyframe_id", "prompt"],
    },
}

UPDATE_KEYFRAME_TIMESTAMP_TOOL: dict = {
    "name": "update_keyframe_timestamp",
    "description": (
        "Move a keyframe to a different timestamp on the timeline. Timestamp is stored "
        "as a string: 'm:ss', 'mm:ss.fff', or seconds as a numeric string. Wrapped "
        "in an undo group."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "keyframe_id": {"type": "string"},
            "timestamp": {"type": "string", "description": "e.g. '0:15', '1:30.250', '45.5'."},
        },
        "required": ["keyframe_id", "timestamp"],
    },
}

UPDATE_CURVE_TOOL: dict = {
    "name": "update_curve",
    "description": (
        "Replace a transition's color/opacity curve with a new list of points. "
        "Each point is [x, y] where x is normalised time 0..1 and y is the curve value. "
        "Curve types: opacity (0..1), saturation (0..2), red/green/blue/black (0..1), "
        "hue_shift (-180..180), invert (0..1), brightness/contrast/exposure (0..2). "
        "Wrapped in an undo group."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "transition_id": {"type": "string"},
            "curve_type": {
                "type": "string",
                "enum": [
                    "opacity", "saturation", "red", "green", "blue", "black",
                    "hue_shift", "invert", "brightness", "contrast", "exposure",
                ],
            },
            "points": {
                "type": "array",
                "description": "Array of [x, y] pairs. x and y are numbers.",
                "items": {
                    "type": "array",
                    "items": {"type": "number"},
                    "minItems": 2,
                    "maxItems": 2,
                },
            },
        },
        "required": ["transition_id", "curve_type", "points"],
    },
}

UPDATE_TRANSFORM_CURVE_TOOL: dict = {
    "name": "update_transform_curve",
    "description": (
        "Replace a transition's transform (pan/zoom) curve for X, Y, or Z axis with a "
        "new list of [x, y] points. x is normalised time 0..1. y is the transform "
        "value (pixels for X/Y, scale multiplier for Z). Wrapped in an undo group."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "transition_id": {"type": "string"},
            "axis": {"type": "string", "enum": ["x", "y", "z"]},
            "points": {
                "type": "array",
                "items": {"type": "array", "items": {"type": "number"}, "minItems": 2, "maxItems": 2},
            },
        },
        "required": ["transition_id", "axis", "points"],
    },
}

DELETE_KEYFRAME_TOOL: dict = {
    "name": "delete_keyframe",
    "description": (
        "Soft-delete a keyframe (moves to bin, can be restored via the bin panel or "
        "undo). Requires user confirmation."
    ),
    "input_schema": {
        "type": "object",
        "properties": {"keyframe_id": {"type": "string"}},
        "required": ["keyframe_id"],
    },
}

DELETE_TRANSITION_TOOL: dict = {
    "name": "delete_transition",
    "description": (
        "Soft-delete a transition (moves to bin). Requires user confirmation."
    ),
    "input_schema": {
        "type": "object",
        "properties": {"transition_id": {"type": "string"}},
        "required": ["transition_id"],
    },
}

BATCH_DELETE_KEYFRAMES_TOOL: dict = {
    "name": "batch_delete_keyframes",
    "description": (
        "Soft-delete multiple keyframes in one undo group. Prefer this over calling "
        "delete_keyframe repeatedly when removing more than one — the user sees a "
        "single confirmation listing every affected keyframe and can undo the whole "
        "batch with one action."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "keyframe_ids": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Keyframe IDs to delete.",
                "minItems": 1,
            },
        },
        "required": ["keyframe_ids"],
    },
}

BATCH_DELETE_TRANSITIONS_TOOL: dict = {
    "name": "batch_delete_transitions",
    "description": (
        "Soft-delete multiple transitions in one undo group. Prefer this over calling "
        "delete_transition repeatedly — single confirmation, single undo."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "transition_ids": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Transition IDs to delete.",
                "minItems": 1,
            },
        },
        "required": ["transition_ids"],
    },
}

TOOLS: list[dict] = [
    SQL_QUERY_TOOL,
    UPDATE_KEYFRAME_PROMPT_TOOL,
    UPDATE_KEYFRAME_TIMESTAMP_TOOL,
    UPDATE_CURVE_TOOL,
    UPDATE_TRANSFORM_CURVE_TOOL,
    DELETE_KEYFRAME_TOOL,
    DELETE_TRANSITION_TOOL,
    BATCH_DELETE_KEYFRAMES_TOOL,
    BATCH_DELETE_TRANSITIONS_TOOL,
]


# ── Elicitation ──────────────────────────────────────────────────────


# Tool name substrings that trigger inline confirmation before execution.
# Case-insensitive. These cover Remember MCP's destructive/publishing tools and
# future built-ins (delete_keyframe, delete_transition, batch_delete_*, etc.).
_DESTRUCTIVE_TOOL_PATTERNS: tuple[str, ...] = (
    "delete",
    "remove",
    "destroy",
    "drop",
    "publish",
    "retract",
    "revise",
    "moderate",
    "restore_checkpoint",
    "batch_delete",
)


def _is_destructive(tool_name: str) -> bool:
    name = tool_name.lower()
    return any(p in name for p in _DESTRUCTIVE_TOOL_PATTERNS)


def _format_tool_input_summary(tool_name: str, tool_input: dict, project_dir: Path | None = None) -> tuple[str, list[str]]:
    """Produce (message, summary_items) for an elicitation card.

    Rich per-tool summaries for the built-in destructive tools; generic fallback
    for everything else.
    """
    input_dict = tool_input or {}

    # Rich summaries for built-in destructive tools
    if project_dir is not None and tool_name in {
        "delete_keyframe", "delete_transition",
        "batch_delete_keyframes", "batch_delete_transitions",
    }:
        try:
            return _format_destructive_summary(tool_name, input_dict, project_dir)
        except Exception as e:
            _log(f"summary enrichment failed for {tool_name}: {e}")
            # Fall through to generic formatting

    items: list[str] = []
    for k, v in list(input_dict.items())[:12]:
        if isinstance(v, (list, dict)):
            items.append(f"{k}: {json.dumps(v, default=str)[:120]}")
        else:
            s = str(v)
            if len(s) > 160:
                s = s[:157] + "..."
            items.append(f"{k}: {s}")
    message = f"Confirm calling `{tool_name}`?"
    return message, items


def _format_destructive_summary(tool_name: str, input_dict: dict, project_dir: Path) -> tuple[str, list[str]]:
    """Build a rich preview for delete/batch_delete tools."""
    from scenecraft.db import get_keyframe, get_transition

    def _kf_line(kf: dict) -> str:
        ts = kf.get("timestamp") or "?"
        prompt = (kf.get("prompt") or "").strip().replace("\n", " ")
        if len(prompt) > 60:
            prompt = prompt[:57] + "..."
        kid = kf.get("id", "")
        return f"{kid} @ {ts}" + (f" — {prompt}" if prompt else "")

    def _tr_line(tr: dict) -> str:
        tid = tr.get("id", "")
        f = tr.get("from", "?")
        t = tr.get("to", "?")
        dur = tr.get("duration_seconds")
        dur_str = f" · {dur:.1f}s" if isinstance(dur, (int, float)) else ""
        return f"{tid}  {f} → {t}{dur_str}"

    if tool_name == "delete_keyframe":
        kf_id = input_dict.get("keyframe_id", "")
        kf = get_keyframe(project_dir, kf_id)
        if kf:
            return f"Delete keyframe {kf_id}?", [_kf_line(kf)]
        return f"Delete keyframe {kf_id}?", [f"{kf_id} (not found)"]

    if tool_name == "delete_transition":
        tr_id = input_dict.get("transition_id", "")
        tr = get_transition(project_dir, tr_id)
        if tr:
            return f"Delete transition {tr_id}?", [_tr_line(tr)]
        return f"Delete transition {tr_id}?", [f"{tr_id} (not found)"]

    if tool_name == "batch_delete_keyframes":
        ids = [s for s in (input_dict.get("keyframe_ids") or []) if isinstance(s, str)]
        items: list[str] = []
        found = 0
        missing: list[str] = []
        for kid in ids[:12]:
            kf = get_keyframe(project_dir, kid)
            if kf:
                items.append(_kf_line(kf))
                found += 1
            else:
                missing.append(kid)
        for m in missing[:6]:
            items.append(f"{m} (not found)")
        if len(ids) > 12:
            items.append(f"… and {len(ids) - 12} more")
        msg = f"Delete {len(ids)} keyframes? ({found} valid, {len(missing)} missing)" if missing else f"Delete {len(ids)} keyframes?"
        return msg, items

    if tool_name == "batch_delete_transitions":
        ids = [s for s in (input_dict.get("transition_ids") or []) if isinstance(s, str)]
        items = []
        found = 0
        missing = []
        for tid in ids[:12]:
            tr = get_transition(project_dir, tid)
            if tr:
                items.append(_tr_line(tr))
                found += 1
            else:
                missing.append(tid)
        for m in missing[:6]:
            items.append(f"{m} (not found)")
        if len(ids) > 12:
            items.append(f"… and {len(ids) - 12} more")
        msg = f"Delete {len(ids)} transitions? ({found} valid, {len(missing)} missing)" if missing else f"Delete {len(ids)} transitions?"
        return msg, items

    # Unreachable — caller gates on tool_name
    return f"Confirm `{tool_name}`?", []


def _humanize_tool_name(name: str) -> str:
    """Turn `remember_delete_memory` → `Remember · Delete Memory` for display."""
    parts = name.split("_")
    head = parts[0].capitalize()
    rest = " ".join(p.capitalize() for p in parts[1:])
    return f"{head} · {rest}" if rest else head


async def _recv_elicitation_response(ws: ServerConnection, elicitation_id: str, timeout: float = 300) -> str:
    """Block until an elicitation_response with matching id arrives.

    Returns the action ("accept" or "decline"). On timeout, returns "decline".
    Other incoming messages during the wait (ping, stray message) are handled
    minimally so they don't derail the flow.
    """
    import asyncio
    import websockets

    deadline = asyncio.get_event_loop().time() + timeout
    while True:
        remaining = deadline - asyncio.get_event_loop().time()
        if remaining <= 0:
            _log(f"elicitation {elicitation_id}: timeout, auto-declining")
            return "decline"
        try:
            raw = await asyncio.wait_for(ws.recv(), timeout=remaining)
        except asyncio.TimeoutError:
            return "decline"
        except websockets.exceptions.ConnectionClosed:
            return "decline"
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            continue
        t = data.get("type")
        if t == "elicitation_response" and data.get("id") == elicitation_id:
            action = data.get("action")
            return "accept" if action == "accept" else "decline"
        if t == "ping":
            await ws.send(json.dumps({"type": "pong"}))
        # Ignore other message types while awaiting a response.


def _readonly_authorizer(action, arg1, arg2, db_name, trigger_name):  # noqa: ANN001
    """SQLite authorizer that allows only read operations."""
    # Action codes from sqlite3 — use getattr so missing constants don't crash on older Python.
    allowed = {
        getattr(sqlite3, "SQLITE_SELECT", 21),
        getattr(sqlite3, "SQLITE_READ", 20),
        getattr(sqlite3, "SQLITE_FUNCTION", 31),
        getattr(sqlite3, "SQLITE_RECURSIVE", 33),
    }
    if action in allowed:
        return sqlite3.SQLITE_OK
    return sqlite3.SQLITE_DENY


def _execute_readonly_sql(project_dir: Path, sql: str, limit: int = 100) -> dict:
    """Run a read-only SQL query. Returns a JSON-serializable result dict or {error}."""
    db_path = project_dir / "project.db"
    if not db_path.exists():
        return {"error": f"project.db not found at {db_path}"}

    try:
        limit = int(limit)
    except (TypeError, ValueError):
        limit = 100
    limit = max(1, min(limit, 100000))

    # Open a fresh connection in URI read-only mode AND install an authorizer.
    # Belt-and-suspenders: ?mode=ro blocks writes at the OS level; authorizer blocks
    # any statement type that isn't a read.
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5)
    except sqlite3.Error as e:
        return {"error": f"failed to open db: {e}"}

    conn.set_authorizer(_readonly_authorizer)

    try:
        cur = conn.execute(sql)
        cols = [d[0] for d in cur.description] if cur.description else []
        rows = cur.fetchmany(limit + 1)
        truncated = len(rows) > limit
        rows = rows[:limit]
        # Coerce non-JSON-safe values (bytes, etc.)
        safe_rows = []
        for r in rows:
            safe_row = []
            for v in r:
                if isinstance(v, (bytes, bytearray)):
                    safe_row.append(f"<{len(v)} bytes>")
                else:
                    safe_row.append(v)
            safe_rows.append(safe_row)
        return {
            "columns": cols,
            "rows": safe_rows,
            "row_count": len(safe_rows),
            "truncated": truncated,
            "limit": limit,
        }
    except sqlite3.DatabaseError as e:
        return {"error": str(e)}
    finally:
        try:
            conn.close()
        except sqlite3.Error:
            pass


_COLOR_CURVE_COLUMNS: dict[str, str] = {
    "opacity": "opacity_curve",
    "saturation": "saturation_curve",
    "red": "red_curve",
    "green": "green_curve",
    "blue": "blue_curve",
    "black": "black_curve",
    "hue_shift": "hue_shift_curve",
    "invert": "invert_curve",
    "brightness": "brightness_curve",
    "contrast": "contrast_curve",
    "exposure": "exposure_curve",
}

_TRANSFORM_CURVE_COLUMNS: dict[str, str] = {
    "x": "transform_x_curve",
    "y": "transform_y_curve",
    "z": "transform_z_curve",
}


def _normalize_points(raw: Any) -> list[list[float]] | None:
    """Coerce [[x,y], ...] into floats. Returns None if malformed."""
    if not isinstance(raw, list):
        return None
    out: list[list[float]] = []
    for pt in raw:
        if not isinstance(pt, (list, tuple)) or len(pt) != 2:
            return None
        try:
            x = float(pt[0])
            y = float(pt[1])
        except (TypeError, ValueError):
            return None
        out.append([x, y])
    return out


def _exec_update_keyframe_prompt(project_dir: Path, input_data: dict) -> dict:
    from scenecraft.db import get_keyframe, update_keyframe, undo_begin
    kf_id = input_data.get("keyframe_id")
    prompt = input_data.get("prompt")
    if not kf_id or not isinstance(kf_id, str):
        return {"error": "missing keyframe_id"}
    if not isinstance(prompt, str):
        return {"error": "prompt must be a string"}
    existing = get_keyframe(project_dir, kf_id)
    if not existing:
        return {"error": f"keyframe not found: {kf_id}"}
    undo_begin(project_dir, f"Chat: update prompt for {kf_id}")
    update_keyframe(project_dir, kf_id, prompt=prompt)
    return {"keyframe_id": kf_id, "old_prompt": existing.get("prompt", ""), "new_prompt": prompt}


def _exec_update_keyframe_timestamp(project_dir: Path, input_data: dict) -> dict:
    from scenecraft.db import get_keyframe, update_keyframe, undo_begin
    kf_id = input_data.get("keyframe_id")
    ts = input_data.get("timestamp")
    if not kf_id or not isinstance(kf_id, str):
        return {"error": "missing keyframe_id"}
    if ts is None:
        return {"error": "missing timestamp"}
    ts_str = str(ts)
    existing = get_keyframe(project_dir, kf_id)
    if not existing:
        return {"error": f"keyframe not found: {kf_id}"}
    undo_begin(project_dir, f"Chat: move {kf_id} to {ts_str}")
    update_keyframe(project_dir, kf_id, timestamp=ts_str)
    return {"keyframe_id": kf_id, "old_timestamp": existing.get("timestamp", ""), "new_timestamp": ts_str}


def _exec_update_curve(project_dir: Path, input_data: dict) -> dict:
    from scenecraft.db import get_transition, update_transition, undo_begin
    tr_id = input_data.get("transition_id")
    curve_type = input_data.get("curve_type")
    points = _normalize_points(input_data.get("points"))
    if not tr_id or not isinstance(tr_id, str):
        return {"error": "missing transition_id"}
    if curve_type not in _COLOR_CURVE_COLUMNS:
        return {"error": f"invalid curve_type '{curve_type}'"}
    if points is None:
        return {"error": "points must be an array of [x, y] numeric pairs"}
    existing = get_transition(project_dir, tr_id)
    if not existing:
        return {"error": f"transition not found: {tr_id}"}
    column = _COLOR_CURVE_COLUMNS[curve_type]
    undo_begin(project_dir, f"Chat: update {curve_type} curve on {tr_id}")
    update_transition(project_dir, tr_id, **{column: points})
    return {"transition_id": tr_id, "curve_type": curve_type, "point_count": len(points)}


def _exec_update_transform_curve(project_dir: Path, input_data: dict) -> dict:
    from scenecraft.db import get_transition, update_transition, undo_begin
    tr_id = input_data.get("transition_id")
    axis = (input_data.get("axis") or "").lower()
    points = _normalize_points(input_data.get("points"))
    if not tr_id or not isinstance(tr_id, str):
        return {"error": "missing transition_id"}
    if axis not in _TRANSFORM_CURVE_COLUMNS:
        return {"error": f"invalid axis '{axis}' (must be x, y, or z)"}
    if points is None:
        return {"error": "points must be an array of [x, y] numeric pairs"}
    existing = get_transition(project_dir, tr_id)
    if not existing:
        return {"error": f"transition not found: {tr_id}"}
    column = _TRANSFORM_CURVE_COLUMNS[axis]
    undo_begin(project_dir, f"Chat: update transform {axis.upper()} on {tr_id}")
    update_transition(project_dir, tr_id, **{column: points})
    return {"transition_id": tr_id, "axis": axis, "point_count": len(points)}


def _exec_delete_keyframe(project_dir: Path, input_data: dict) -> dict:
    from scenecraft.db import get_keyframe, delete_keyframe, undo_begin
    kf_id = input_data.get("keyframe_id")
    if not kf_id or not isinstance(kf_id, str):
        return {"error": "missing keyframe_id"}
    existing = get_keyframe(project_dir, kf_id)
    if not existing:
        return {"error": f"keyframe not found: {kf_id}"}
    if existing.get("deleted_at"):
        return {"error": f"keyframe {kf_id} is already deleted"}
    now = datetime.now(timezone.utc).isoformat()
    undo_begin(project_dir, f"Chat: delete keyframe {kf_id}")
    delete_keyframe(project_dir, kf_id, now)
    return {"keyframe_id": kf_id, "deleted_at": now, "timestamp": existing.get("timestamp", "")}


def _exec_delete_transition(project_dir: Path, input_data: dict) -> dict:
    from scenecraft.db import get_transition, delete_transition, undo_begin
    tr_id = input_data.get("transition_id")
    if not tr_id or not isinstance(tr_id, str):
        return {"error": "missing transition_id"}
    existing = get_transition(project_dir, tr_id)
    if not existing:
        return {"error": f"transition not found: {tr_id}"}
    if existing.get("deleted_at"):
        return {"error": f"transition {tr_id} is already deleted"}
    now = datetime.now(timezone.utc).isoformat()
    undo_begin(project_dir, f"Chat: delete transition {tr_id}")
    delete_transition(project_dir, tr_id, now)
    return {"transition_id": tr_id, "deleted_at": now}


def _exec_batch_delete_keyframes(project_dir: Path, input_data: dict) -> dict:
    from scenecraft.db import get_keyframe, delete_keyframe, undo_begin
    raw = input_data.get("keyframe_ids")
    if not isinstance(raw, list) or not raw:
        return {"error": "keyframe_ids must be a non-empty array"}
    ids = [s for s in raw if isinstance(s, str) and s]
    if not ids:
        return {"error": "keyframe_ids contains no valid strings"}

    now = datetime.now(timezone.utc).isoformat()
    undo_begin(project_dir, f"Chat: delete {len(ids)} keyframes")

    deleted: list[dict] = []
    skipped: list[dict] = []
    for kf_id in ids:
        existing = get_keyframe(project_dir, kf_id)
        if not existing:
            skipped.append({"keyframe_id": kf_id, "reason": "not found"})
            continue
        if existing.get("deleted_at"):
            skipped.append({"keyframe_id": kf_id, "reason": "already deleted"})
            continue
        delete_keyframe(project_dir, kf_id, now)
        deleted.append({"keyframe_id": kf_id, "timestamp": existing.get("timestamp", "")})

    return {
        "deleted_count": len(deleted),
        "skipped_count": len(skipped),
        "deleted": deleted,
        "skipped": skipped,
        "deleted_at": now,
    }


def _exec_batch_delete_transitions(project_dir: Path, input_data: dict) -> dict:
    from scenecraft.db import get_transition, delete_transition, undo_begin
    raw = input_data.get("transition_ids")
    if not isinstance(raw, list) or not raw:
        return {"error": "transition_ids must be a non-empty array"}
    ids = [s for s in raw if isinstance(s, str) and s]
    if not ids:
        return {"error": "transition_ids contains no valid strings"}

    now = datetime.now(timezone.utc).isoformat()
    undo_begin(project_dir, f"Chat: delete {len(ids)} transitions")

    deleted: list[dict] = []
    skipped: list[dict] = []
    for tr_id in ids:
        existing = get_transition(project_dir, tr_id)
        if not existing:
            skipped.append({"transition_id": tr_id, "reason": "not found"})
            continue
        if existing.get("deleted_at"):
            skipped.append({"transition_id": tr_id, "reason": "already deleted"})
            continue
        delete_transition(project_dir, tr_id, now)
        deleted.append({"transition_id": tr_id, "from": existing.get("from"), "to": existing.get("to")})

    return {
        "deleted_count": len(deleted),
        "skipped_count": len(skipped),
        "deleted": deleted,
        "skipped": skipped,
        "deleted_at": now,
    }


def _execute_tool(project_dir: Path, name: str, input_data: dict) -> tuple[dict, bool]:
    """Execute a tool. Returns (result_dict, is_error)."""
    input_data = input_data or {}
    if name == "sql_query":
        sql = input_data.get("sql", "")
        limit = input_data.get("limit", 100)
        if not sql or not isinstance(sql, str):
            return {"error": "missing sql"}, True
        result = _execute_readonly_sql(project_dir, sql, limit)
        return result, "error" in result
    if name == "update_keyframe_prompt":
        result = _exec_update_keyframe_prompt(project_dir, input_data)
        return result, "error" in result
    if name == "update_keyframe_timestamp":
        result = _exec_update_keyframe_timestamp(project_dir, input_data)
        return result, "error" in result
    if name == "update_curve":
        result = _exec_update_curve(project_dir, input_data)
        return result, "error" in result
    if name == "update_transform_curve":
        result = _exec_update_transform_curve(project_dir, input_data)
        return result, "error" in result
    if name == "delete_keyframe":
        result = _exec_delete_keyframe(project_dir, input_data)
        return result, "error" in result
    if name == "delete_transition":
        result = _exec_delete_transition(project_dir, input_data)
        return result, "error" in result
    if name == "batch_delete_keyframes":
        result = _exec_batch_delete_keyframes(project_dir, input_data)
        return result, "error" in result
    if name == "batch_delete_transitions":
        result = _exec_batch_delete_transitions(project_dir, input_data)
        return result, "error" in result
    return {"error": f"unknown tool: {name}"}, True


# ── History → Claude messages ────────────────────────────────────────


def _history_to_claude_messages(history: list[dict]) -> list[dict]:
    """Convert DB rows to Claude's messages[] shape.

    Assistant rows whose content is a list of blocks are split at each tool_use
    boundary; a synthetic user(tool_result) message is injected using the matching
    entry from that row's tool_calls column.
    """
    out: list[dict] = []
    for msg in history:
        role = msg["role"]
        if role not in ("user", "assistant"):
            continue
        content = msg["content"]
        tool_calls = msg.get("tool_calls") or []

        if role == "assistant" and isinstance(content, list):
            tc_by_id = {tc.get("id"): tc for tc in tool_calls if tc.get("id")}
            buf: list[dict] = []
            for block in content:
                buf.append(block)
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    out.append({"role": "assistant", "content": buf})
                    tc = tc_by_id.get(block.get("id"))
                    tr_output = tc.get("output") if tc else {"error": "tool result missing"}
                    out.append({
                        "role": "user",
                        "content": [{
                            "type": "tool_result",
                            "tool_use_id": block.get("id"),
                            "content": json.dumps(tr_output, default=str),
                            "is_error": bool(tc.get("is_error")) if tc else True,
                        }],
                    })
                    buf = []
            if buf:
                out.append({"role": "assistant", "content": buf})
        else:
            out.append({"role": role, "content": content})
    return out


# ── Chat handler ─────────────────────────────────────────────────────


async def handle_chat_connection(ws: ServerConnection, project_dir: Path, project_name: str, user_id: str = "local"):
    """Handle a chat WebSocket connection for a project."""
    from scenecraft.mcp_bridge import MCPBridge

    _log(f"Chat connected: project={project_name} user={user_id}")

    # Best-effort connect to OAuth-backed MCP services. If the user hasn't
    # authorized Remember yet, the chat still works without it.
    bridge = MCPBridge()
    try:
        await bridge.connect("remember", user_id=user_id)
    except Exception as e:
        _log(f"bridge.connect(remember) raised: {e}")

    try:
        async for raw in ws:
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                await ws.send(json.dumps({"type": "error", "error": "Invalid JSON"}))
                continue

            msg_type = data.get("type")

            if msg_type == "message":
                content = data.get("content", "").strip()
                images = data.get("images")
                if not content:
                    continue

                user_msg = _add_message(project_dir, user_id, "user", content, images)
                await ws.send(json.dumps({"type": "message", "message": user_msg}))
                await _stream_response(ws, project_dir, project_name, user_id, bridge)

            elif msg_type == "ping":
                await ws.send(json.dumps({"type": "pong"}))

    except Exception as e:
        _log(f"Chat error: {e}")
    finally:
        try:
            await bridge.close()
        except Exception as e:
            _log(f"bridge.close raised: {e}")
        _log(f"Chat disconnected: project={project_name} user={user_id}")


async def _stream_response(ws: ServerConnection, project_dir: Path, project_name: str, user_id: str, bridge):
    """Call Claude with streaming + tool calling; stream events over the WebSocket."""
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        await ws.send(json.dumps({"type": "error", "error": "ANTHROPIC_API_KEY not configured on server"}))
        await ws.send(json.dumps({"type": "complete"}))
        return

    try:
        import anthropic
    except ImportError:
        await ws.send(json.dumps({"type": "error", "error": "anthropic SDK not installed"}))
        await ws.send(json.dumps({"type": "complete"}))
        return

    history = _get_messages(project_dir, user_id, limit=50)
    messages = _history_to_claude_messages(history)
    system_prompt = _build_system_prompt(project_dir, project_name)
    client = anthropic.AsyncAnthropic(api_key=api_key)

    # Merge built-in tools with any tools exposed by connected MCP services
    mcp_tools = bridge.all_tools() if bridge else []
    tools_for_claude = list(TOOLS) + mcp_tools

    # Blocks accumulated across all tool-call iterations, persisted at the end.
    all_blocks: list[dict] = []
    tool_calls_log: list[dict] = []
    announced_tool_ids: set[str] = set()

    try:
        for _ in range(10):  # cap at 10 tool iterations per user message
            async with client.messages.stream(
                model="claude-sonnet-4-20250514",
                max_tokens=4096,
                system=system_prompt,
                messages=messages,
                tools=tools_for_claude,
            ) as stream:
                async for event in stream:
                    etype = getattr(event, "type", None)
                    if etype == "content_block_start":
                        block = getattr(event, "content_block", None)
                        if block is not None and getattr(block, "type", None) == "tool_use":
                            tid = getattr(block, "id", "")
                            tname = getattr(block, "name", "")
                            if tid and tid not in announced_tool_ids:
                                announced_tool_ids.add(tid)
                                await ws.send(json.dumps({
                                    "type": "tool_call",
                                    "toolCall": {"id": tid, "name": tname, "input": {}},
                                }))
                    elif etype == "content_block_delta":
                        delta = getattr(event, "delta", None)
                        if delta is not None and getattr(delta, "type", None) == "text_delta":
                            await ws.send(json.dumps({"type": "chunk", "content": delta.text}))

                final = await stream.get_final_message()

            # Accumulate this turn's blocks and extract tool uses
            turn_tool_uses: list[dict] = []
            for block in final.content:
                btype = getattr(block, "type", None)
                if btype == "text":
                    all_blocks.append({"type": "text", "text": block.text})
                elif btype == "tool_use":
                    tu = {
                        "type": "tool_use",
                        "id": block.id,
                        "name": block.name,
                        "input": block.input,
                    }
                    all_blocks.append(tu)
                    turn_tool_uses.append(tu)

            if final.stop_reason != "tool_use" or not turn_tool_uses:
                break

            # Execute tool uses, stream tool_result events, queue results for next turn
            tool_result_blocks: list[dict] = []
            for tu in turn_tool_uses:
                # Destructive tools pause for inline user confirmation before running
                if _is_destructive(tu["name"]):
                    elic_id = f"elic_{uuid.uuid4().hex[:12]}"
                    title = _humanize_tool_name(tu["name"])
                    message, summary_items = _format_tool_input_summary(tu["name"], tu["input"] or {}, project_dir)
                    await ws.send(json.dumps({
                        "type": "elicitation",
                        "elicitation": {
                            "id": elic_id,
                            "tool_use_id": tu["id"],
                            "tool_name": tu["name"],
                            "title": title,
                            "message": message,
                            "summary_items": summary_items,
                        },
                    }))
                    action = await _recv_elicitation_response(ws, elic_id)
                    if action != "accept":
                        cancel_result = {"error": "cancelled by user"}
                        await ws.send(json.dumps({
                            "type": "tool_result",
                            "toolResult": {"id": tu["id"], "output": cancel_result, "isError": True},
                            "durationMs": 0,
                        }))
                        tool_result_blocks.append({
                            "type": "tool_result",
                            "tool_use_id": tu["id"],
                            "content": json.dumps(cancel_result),
                            "is_error": True,
                        })
                        tool_calls_log.append({
                            "id": tu["id"],
                            "name": tu["name"],
                            "input": tu["input"],
                            "output": cancel_result,
                            "is_error": True,
                            "duration_ms": 0,
                            "cancelled": True,
                        })
                        continue

                t0 = time.monotonic()
                if bridge and bridge.has_tool(tu["name"]):
                    result, is_error = await bridge.call_tool(tu["name"], tu["input"] or {})
                else:
                    result, is_error = _execute_tool(project_dir, tu["name"], tu["input"])
                dt_ms = int((time.monotonic() - t0) * 1000)

                await ws.send(json.dumps({
                    "type": "tool_result",
                    "toolResult": {"id": tu["id"], "output": result, "isError": is_error},
                    "durationMs": dt_ms,
                }))

                tool_result_blocks.append({
                    "type": "tool_result",
                    "tool_use_id": tu["id"],
                    "content": json.dumps(result, default=str),
                    "is_error": is_error,
                })
                tool_calls_log.append({
                    "id": tu["id"],
                    "name": tu["name"],
                    "input": tu["input"],
                    "output": result,
                    "is_error": is_error,
                    "duration_ms": dt_ms,
                })

            # Feed the assistant turn + tool results back for the next iteration
            messages.append({
                "role": "assistant",
                "content": [_block_to_dict(b) for b in final.content],
            })
            messages.append({"role": "user", "content": tool_result_blocks})

        # Persist assistant message
        has_non_text = any(b.get("type") != "text" for b in all_blocks)
        if has_non_text:
            persisted_content = json.dumps(all_blocks)
        else:
            persisted_content = "".join(b.get("text", "") for b in all_blocks if b.get("type") == "text")

        assistant_msg = _add_message(
            project_dir,
            user_id,
            "assistant",
            persisted_content,
            tool_calls=tool_calls_log or None,
        )
        # Ensure the frontend echo has content blocks decoded (not a JSON string)
        if has_non_text:
            assistant_msg["content"] = all_blocks
        if tool_calls_log:
            assistant_msg["tool_calls"] = tool_calls_log
        await ws.send(json.dumps({"type": "message", "message": assistant_msg}, default=str))
        await ws.send(json.dumps({"type": "complete"}))

    except anthropic.APIError as e:
        _log(f"Claude API error: {e}")
        await ws.send(json.dumps({"type": "error", "error": f"Claude API error: {getattr(e, 'message', str(e))}"}))
        await ws.send(json.dumps({"type": "complete"}))
    except Exception as e:
        _log(f"Stream error: {e}")
        await ws.send(json.dumps({"type": "error", "error": str(e)}))
        await ws.send(json.dumps({"type": "complete"}))


def _block_to_dict(block: Any) -> dict:
    """Convert an anthropic ContentBlock object to a plain dict."""
    btype = getattr(block, "type", None)
    if btype == "text":
        return {"type": "text", "text": block.text}
    if btype == "tool_use":
        return {"type": "tool_use", "id": block.id, "name": block.name, "input": block.input}
    # Fallback: try model_dump
    if hasattr(block, "model_dump"):
        return block.model_dump()
    return {"type": btype or "unknown"}
