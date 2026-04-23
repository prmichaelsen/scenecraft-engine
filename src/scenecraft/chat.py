"""Chat assistant — Claude-powered WebSocket chat with streaming and tool calling."""

from __future__ import annotations

import asyncio
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
  • add_keyframe — insert a new keyframe at a timestamp with a prompt.
  • update_keyframe — generic: update any subset of fields (prompt, timestamp, label,
    track, section, blend_mode, opacity, …) in one undo group.
  • update_transition — generic: update duration, action, slots, label, tags, remap,
    blend_mode, opacity, seed, negative_prompt, flags, etc.
  • update_keyframe_prompt / update_keyframe_timestamp — narrow variants if only
    that one field is changing.
  • update_curve — replace a transition's color/opacity curve points.
  • update_transform_curve — replace a transition's transform X/Y/Z curve points.
  • split_transition — divide a transition at a time point, inserting a new keyframe.
  • assign_keyframe_image — mark a candidate variant (v{{N}}.png) as selected.
  • assign_pool_video — mark a pool_segment as selected for a transition slot (the
    segment must already be a candidate via tr_candidates).
  • checkpoint(name?) — create a non-destructive restore point (snapshots project.db).
    Call this BEFORE a batch of risky edits.
  • list_checkpoints — inspect available checkpoint filenames + names + timestamps.
  • restore_checkpoint(filename) — roll back to a checkpoint (destructive, user-confirmed).
  • delete_keyframe, delete_transition — soft-delete one item (asks to confirm).
  • batch_delete_keyframes, batch_delete_transitions — soft-delete many in ONE
    confirmation and ONE undo group. Prefer these over looping single-deletes.
  • generate_keyframe_candidates — run Imagen to create N new image candidates for a
    keyframe. Slow + costs API credit. User must confirm.
  • generate_transition_candidates — run Veo to create N new video candidates for a
    transition (inherits ingredients/seed/prompt from the transition record; use
    update_transition first if you want to tweak them). Slow + expensive.
  • isolate_vocals__run — separate an audio source into vocal + background stems
    (DFN3 + residual). Returns a new audio_isolations run id with stem
    pool_segment ids. Works on audio_clip (MVP) or transition (planned). Slow
    (~realtime CPU). User-confirmed.
  • add_audio_track — create a new, empty audio track (auto name + display_order).
  • add_audio_clip — place a pool_segment on an audio track; end_time auto-computed
    from the segment's duration when omitted.
  • update_volume_curve — replace the volume_curve on an audio track or audio
    clip with a new list of [time, value] points. Times must start at 0 and be
    strictly increasing. Min 2 points. Wrapped in an undo group.
  • generate_dsp — run librosa analyses (onsets, rms, vocal_presence, tempo,
    spectral_centroid) on a pool_segment and cache the results in the dsp_*
    tables. Returns a run_id. Results are cached by (segment, analyzer_version,
    params_hash); re-calling with the same inputs returns the cached run
    without re-running librosa. Pass force_rerun=True to overwrite.
  • generate_descriptions — run Gemini over chunks of a pool_segment's audio
    and cache structured semantic labels (section_type, mood, energy,
    vocal_style, instrumentation) in the audio_description* tables. Results
    are cached by (segment, model, prompt_version); re-calling with the same
    inputs returns the cached run without re-invoking Gemini. Pass
    force_rerun=True to overwrite.

After generation completes, use `assign_keyframe_image` / `assign_pool_video` to
pick one of the new candidates (coming in task-54).

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

GENERATE_KEYFRAME_CANDIDATES_TOOL: dict = {
    "name": "generate_keyframe_candidates",
    "description": (
        "Generate new image candidates for a keyframe using Imagen. Requires the "
        "keyframe to already have a selected source image (user must pick one "
        "from the bin first). Generation takes 20-60 seconds and consumes API "
        "credit — requires user confirmation. Returns the updated candidates "
        "list so you can then call `assign_keyframe_image` to pick one."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "keyframe_id": {"type": "string"},
            "count": {
                "type": "integer",
                "minimum": 1,
                "maximum": 8,
                "default": 3,
                "description": "Number of new candidates to generate. Default 3.",
            },
            "prompt_override": {
                "type": "string",
                "description": "Optional. Uses the keyframe's saved prompt if omitted.",
            },
        },
        "required": ["keyframe_id"],
    },
}

ADD_KEYFRAME_TOOL: dict = {
    "name": "add_keyframe",
    "description": (
        "Insert a new keyframe on the timeline. Auto-generated ID. Wrapped in an "
        "undo group. Returns the new keyframe_id so you can immediately assign an "
        "image or generate candidates."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "timestamp": {"type": "string", "description": "Position on the timeline: 'm:ss', 'mm:ss.fff', or seconds as a string."},
            "prompt": {"type": "string", "description": "Prompt text for image generation."},
            "track_id": {"type": "string", "description": "Optional; defaults to 'track_1'."},
            "section": {"type": "string", "description": "Optional narrative section label."},
            "label": {"type": "string", "description": "Optional display label."},
            "label_color": {"type": "string", "description": "Optional hex color like '#ff8800'."},
        },
        "required": ["timestamp", "prompt"],
    },
}

UPDATE_KEYFRAME_TOOL: dict = {
    "name": "update_keyframe",
    "description": (
        "Update any subset of a keyframe's fields in one call. Pass only the fields "
        "you want to change. Wrapped in an undo group. For narrow cases use "
        "update_keyframe_prompt or update_keyframe_timestamp."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "keyframe_id": {"type": "string"},
            "timestamp": {"type": "string"},
            "prompt": {"type": "string"},
            "track_id": {"type": "string"},
            "section": {"type": "string"},
            "label": {"type": "string"},
            "label_color": {"type": "string", "description": "Hex color like '#ff8800'."},
            "blend_mode": {"type": "string", "description": "'normal', 'add', 'multiply', 'screen', 'overlay', etc."},
            "opacity": {"type": "number", "minimum": 0, "maximum": 1},
            "refinement_prompt": {"type": "string"},
        },
        "required": ["keyframe_id"],
    },
}

UPDATE_TRANSITION_TOOL: dict = {
    "name": "update_transition",
    "description": (
        "Update any subset of a transition's metadata fields. Does NOT handle color "
        "or transform curves — use update_curve / update_transform_curve for those. "
        "Wrapped in an undo group."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "transition_id": {"type": "string"},
            "duration_seconds": {"type": "number", "minimum": 0},
            "slots": {"type": "integer", "minimum": 1},
            "action": {"type": "string", "description": "Motion/intent prompt, e.g. 'crossfade', 'cut', 'slow pan left'."},
            "label": {"type": "string"},
            "label_color": {"type": "string"},
            "track_id": {"type": "string"},
            "tags": {"type": "array", "items": {"type": "string"}},
            "blend_mode": {"type": "string"},
            "opacity": {"type": "number"},
            "use_global_prompt": {"type": "boolean"},
            "include_section_desc": {"type": "boolean"},
            "hidden": {"type": "boolean"},
            "is_adjustment": {"type": "boolean"},
            "remap": {
                "type": "object",
                "description": "Playback remap config.",
                "properties": {
                    "method": {"type": "string", "enum": ["linear", "ease-in", "ease-out", "ease-in-out"]},
                    "target_duration": {"type": "number"},
                },
            },
            "negative_prompt": {"type": "string"},
            "seed": {"type": "integer"},
        },
        "required": ["transition_id"],
    },
}

CHECKPOINT_TOOL: dict = {
    "name": "checkpoint",
    "description": (
        "Create a named restore point by snapshotting project.db. Non-destructive; "
        "shows up in the Checkpoints panel. Call this before a risky batch of edits "
        "so the user can restore if something goes wrong."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "description": "Optional human-readable label. Defaults to the timestamp.",
            },
        },
    },
}

RESTORE_CHECKPOINT_TOOL: dict = {
    "name": "restore_checkpoint",
    "description": (
        "Replace the current project database with a checkpoint snapshot. DESTRUCTIVE — "
        "all changes since that checkpoint will be lost. Inspect the checkpoints "
        "table via sql_query (or ask the user to pick from the Checkpoints panel) "
        "to find the filename first. Requires user confirmation."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "filename": {
                "type": "string",
                "description": "Checkpoint filename, e.g. 'project.db.checkpoint-20260418_140530'.",
            },
        },
        "required": ["filename"],
    },
}

LIST_CHECKPOINTS_TOOL: dict = {
    "name": "list_checkpoints",
    "description": (
        "List all checkpoints for the current project. Returns filename, name, "
        "created, and size_bytes for each entry."
    ),
    "input_schema": {
        "type": "object",
        "properties": {},
    },
}

SPLIT_TRANSITION_TOOL: dict = {
    "name": "split_transition",
    "description": (
        "Divide a transition into two transitions at a time point. Creates a new "
        "keyframe at the split, then updates the original transition to end at the "
        "new keyframe and inserts a new transition going new_kf → original_to_kf. "
        "The new transition inherits action/slots/track from the original. `at_time` "
        "must fall strictly between the transition's from and to keyframe timestamps. "
        "Wrapped in an undo group. tr_candidates are NOT copied; regenerate if needed."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "transition_id": {"type": "string"},
            "at_time": {
                "type": "string",
                "description": "Absolute timeline time ('m:ss', 'mm:ss.fff', or seconds as a string) strictly between from_kf and to_kf.",
            },
            "new_keyframe_prompt": {
                "type": "string",
                "description": "Optional prompt for the inserted keyframe; defaults to empty.",
            },
        },
        "required": ["transition_id", "at_time"],
    },
}

ASSIGN_KEYFRAME_IMAGE_TOOL: dict = {
    "name": "assign_keyframe_image",
    "description": (
        "Mark a candidate as the selected image for a keyframe. Pass `variant` (the "
        "integer N in v{N}.png — inspect the keyframe's `candidates` list or run "
        "sql_query to discover). Updates the keyframe's `selected` field. Wrapped "
        "in an undo group."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "keyframe_id": {"type": "string"},
            "variant": {
                "type": "integer",
                "minimum": 1,
                "description": "1-based variant number (N in v{N}.png).",
            },
        },
        "required": ["keyframe_id", "variant"],
    },
}

ASSIGN_POOL_VIDEO_TOOL: dict = {
    "name": "assign_pool_video",
    "description": (
        "Mark a pool_segment as the selected video for a transition slot. The "
        "(transition_id, slot, pool_segment_id) triple must already exist in "
        "tr_candidates (i.e. the segment was generated or imported for that slot). "
        "Updates the transition's `selected` list. Wrapped in an undo group."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "transition_id": {"type": "string"},
            "pool_segment_id": {"type": "string"},
            "slot": {
                "type": "integer",
                "minimum": 0,
                "default": 0,
                "description": "Slot index within the transition. Default 0.",
            },
        },
        "required": ["transition_id", "pool_segment_id"],
    },
}

GENERATE_TRANSITION_CANDIDATES_TOOL: dict = {
    "name": "generate_transition_candidates",
    "description": (
        "Generate new video candidates for a transition using Veo. Slow (1-3 minutes) "
        "and expensive — requires user confirmation. Inherits ingredients, seed, "
        "negative prompt, and action from the transition record. To change those, "
        "call `update_transition` first, then generate. Returns new pool_segment IDs "
        "so you can call `assign_pool_video` to pick one."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "transition_id": {"type": "string"},
            "count": {
                "type": "integer",
                "minimum": 1,
                "maximum": 4,
                "default": 2,
                "description": "Number of new candidates per slot.",
            },
            "slot": {
                "type": "integer",
                "minimum": 0,
                "description": "Optional slot index. Omit to generate for every slot.",
            },
        },
        "required": ["transition_id"],
    },
}

ISOLATE_VOCALS_TOOL: dict = {
    # Convention: {plugin_name}__{tool_name}. The `isolate_vocals` plugin
    # exposes exactly one operation; its id is `isolate_vocals.run` internally
    # and `isolate_vocals__run` across the Claude API boundary (dots disallowed
    # by Claude's tool-name regex).
    "name": "isolate_vocals__run",
    "description": (
        "Separate a voice-over-noise audio source into vocal + background stems "
        "using DeepFilterNet3. Accepts an audio_clip (MVP) or transition as the "
        "source entity. Returns an audio_isolations run id with N stem "
        "pool_segment ids. Slow (~realtime on CPU). Requires user confirmation. "
        "Use `get_audio_clips` or sql_query on audio_clips / transitions to find "
        "entity ids first."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "entity_type": {"type": "string", "enum": ["audio_clip", "transition"]},
            "entity_id":   {"type": "string", "description": "ID of the source entity."},
            "range_mode":  {"type": "string", "enum": ["full", "subset"], "default": "full"},
            "trim_in":     {"type": "number", "description": "Required when range_mode='subset'."},
            "trim_out":    {"type": "number", "description": "Required when range_mode='subset'."},
        },
        "required": ["entity_type", "entity_id"],
    },
}

ADD_AUDIO_TRACK_TOOL: dict = {
    "name": "add_audio_track",
    "description": (
        "Create a new, empty audio track on the timeline. Auto-generated id and "
        "display_order (appended to end). Name auto-generated if omitted. Wrapped "
        "in an undo group. Returns the new track_id so you can place clips on it."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "name":   {"type": "string", "description": "Display name. Auto-generated if omitted."},
            "muted":  {"type": "boolean", "default": False},
            "volume": {
                "type": "number", "default": 1.0, "minimum": 0.0, "maximum": 2.0,
                "description": "Initial static volume (0..2). Becomes a constant volume_curve.",
            },
        },
        "required": [],
    },
}

ADD_AUDIO_CLIP_TOOL: dict = {
    "name": "add_audio_clip",
    "description": (
        "Place a pool_segment (by id) on an audio track at a timeline position. "
        "Trim into the source via trim_in/trim_out (preferred) OR "
        "source_offset/end_time (lower-level). When trim is provided: "
        "source_offset = trim_in, end_time = start_time + (trim_out - trim_in). "
        "If no trim/end is given, the clip plays the full source from source_offset. "
        "Wrapped in an undo group. Returns the new audio_clip_id. "
        "Use sql_query on pool_segments to find the source id first."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "track_id":          {"type": "string", "description": "Destination audio_tracks.id. Use add_audio_track first if none exists."},
            "source_segment_id": {"type": "string", "description": "pool_segments.id (the uuid) of the audio source."},
            "start_time":        {"type": "number", "description": "Timeline start in seconds. Required."},
            "trim_in":           {"type": "number", "description": "Preferred: where in the source the clip starts playing (seconds). Equivalent to source_offset."},
            "trim_out":          {"type": "number", "description": "Preferred: where in the source the clip stops playing (seconds). Sets end_time = start_time + (trim_out - trim_in)."},
            "source_offset":     {"type": "number", "default": 0.0, "description": "Lower-level alias for trim_in. Ignored if trim_in is provided."},
            "end_time":          {"type": "number", "description": "Lower-level timeline end in seconds. Ignored if trim_out is provided. Auto-computed from source duration when omitted."},
            "volume_curve":      {"type": "string", "default": "[[0,1],[1,1]]", "description": "JSON curve points string. Default full volume."},
            "label":             {"type": "string", "description": "Optional display label. Seeded from pool segment label if omitted."},
        },
        "required": ["track_id", "source_segment_id", "start_time"],
    },
}

UPDATE_VOLUME_CURVE_TOOL: dict = {
    "name": "update_volume_curve",
    "description": (
        "Replace the volume_curve on an audio track or audio clip with a new list "
        "of [time, value] points. `target_type` is 'track' or 'clip'; `target_id` "
        "is the matching audio_tracks.id or audio_clips.id. Points are [time, value] "
        "pairs where time is in seconds (or 0..1 normalised — match existing curve "
        "convention on the target). First point's time MUST be 0.0; times must be "
        "strictly increasing; minimum 2 points. `points` may be passed as a JSON "
        "string or a parsed list. `interpolation` is accepted ('bezier' | 'linear' "
        "| 'step') but the current schema has no dedicated column — it is noted in "
        "the result. Wrapped in an undo group."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "target_type": {"type": "string", "enum": ["track", "clip"]},
            "target_id":   {"type": "string", "description": "audio_tracks.id or audio_clips.id."},
            "points": {
                "description": "Array of [time, value] pairs, or a JSON string of the same.",
                "oneOf": [
                    {
                        "type": "array",
                        "items": {
                            "type": "array",
                            "items": {"type": "number"},
                            "minItems": 2,
                            "maxItems": 2,
                        },
                        "minItems": 2,
                    },
                    {"type": "string"},
                ],
            },
            "interpolation": {
                "type": "string",
                "enum": ["bezier", "linear", "step"],
                "default": "bezier",
                "description": "Curve interpolation mode. Currently not persisted (no column).",
            },
        },
        "required": ["target_type", "target_id", "points"],
    },
}

GENERATE_DSP_TOOL: dict = {
    "name": "generate_dsp",
    "description": (
        "Run librosa analyses on a pool_segment's on-disk audio and cache the "
        "results in the dsp_* tables. Supported analyses: "
        "'rms' (amplitude envelope as time-series datapoints), "
        "'onsets' (transient events with strength), "
        "'vocal_presence' (time-ranged sections above an RMS threshold — the "
        "key primitive for auto-duck/sidechain targeting), "
        "'tempo' (global BPM as a scalar), "
        "'spectral_centroid' (brightness over time as datapoints). "
        "Results are cached by (source_segment_id, analyzer_version, params_hash) "
        "— calling again with the same analyses returns the cached run. "
        "Pass force_rerun=True to discard the cached run and recompute. "
        "Unknown analysis names are silently skipped (not an error). "
        "Non-destructive: analysis runs are write-once cached artifacts, no "
        "undo required."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "source_segment_id": {
                "type": "string",
                "description": "pool_segments.id of the audio to analyze.",
            },
            "analyses": {
                "type": "array",
                "items": {"type": "string"},
                "default": ["onsets", "rms", "vocal_presence", "tempo"],
                "description": (
                    "Which analyses to run. Any of 'onsets', 'rms', "
                    "'vocal_presence', 'tempo', 'spectral_centroid'. "
                    "Unknown names are skipped."
                ),
            },
            "force_rerun": {
                "type": "boolean",
                "default": False,
                "description": (
                    "If true, delete any existing run matching the cache key "
                    "and recompute. Default false (return the cached run)."
                ),
            },
        },
        "required": ["source_segment_id"],
    },
}

GENERATE_DESCRIPTIONS_TOOL: dict = {
    "name": "generate_descriptions",
    "description": (
        "Run Gemini over chunks of a pool_segment's audio and cache the "
        "structured semantic labels (section_type, mood, energy, vocal_style, "
        "instrumentation) in the audio_description* tables. Results are "
        "cached by (source_segment_id, model, prompt_version); re-calling "
        "with the same inputs returns the cached run without re-invoking "
        "Gemini. Pass force_rerun=True to discard the cached run and recompute. "
        "Non-destructive: description runs are write-once cached artifacts, "
        "no undo required."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "source_segment_id": {
                "type": "string",
                "description": "pool_segments.id of the audio to analyze.",
            },
            "model": {
                "type": "string",
                "default": "gemini-2.5-pro",
                "description": (
                    "Gemini model identifier used for chunk description. "
                    "Part of the cache key — changing it produces a new run."
                ),
            },
            "chunk_size_s": {
                "type": "number",
                "default": 30.0,
                "description": (
                    "Chunk duration in seconds for audio slicing before "
                    "sending to Gemini."
                ),
            },
            "prompt_version": {
                "type": "string",
                "default": "v1",
                "description": (
                    "Prompt template version. Part of the cache key so "
                    "iterating on the prompt produces a new run instead of "
                    "overwriting historical analysis."
                ),
            },
            "force_rerun": {
                "type": "boolean",
                "default": False,
                "description": (
                    "If true, delete any existing run matching the cache "
                    "key and recompute. Default false (return the cached run)."
                ),
            },
        },
        "required": ["source_segment_id"],
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
    ADD_KEYFRAME_TOOL,
    UPDATE_KEYFRAME_TOOL,
    UPDATE_TRANSITION_TOOL,
    SPLIT_TRANSITION_TOOL,
    ASSIGN_KEYFRAME_IMAGE_TOOL,
    ASSIGN_POOL_VIDEO_TOOL,
    CHECKPOINT_TOOL,
    LIST_CHECKPOINTS_TOOL,
    RESTORE_CHECKPOINT_TOOL,
    GENERATE_KEYFRAME_CANDIDATES_TOOL,
    GENERATE_TRANSITION_CANDIDATES_TOOL,
    ISOLATE_VOCALS_TOOL,
    ADD_AUDIO_TRACK_TOOL,
    ADD_AUDIO_CLIP_TOOL,
    UPDATE_VOLUME_CURVE_TOOL,
    GENERATE_DSP_TOOL,
    GENERATE_DESCRIPTIONS_TOOL,
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
    # Generation tools are not DB-destructive but cost real money and take time;
    # gate them behind the same confirmation flow.
    "generate_",
    # Isolation runs the DFN3 model + writes stems — not destructive but
    # expensive + long-running. Same treatment as generate_.
    "isolate_",
)


_DESTRUCTIVE_TOOL_ALLOWLIST: frozenset[str] = frozenset({
    # generate_dsp runs librosa and writes a write-once cache row — it is
    # neither DB-destructive nor slow/expensive in the generate_/isolate_
    # sense, so the "generate_" substring pattern must not gate it.
    "generate_dsp",
    # generate_descriptions runs Gemini once per chunk and caches structured
    # output. Cacheable, non-destructive — no confirmation gate needed.
    "generate_descriptions",
})


def _is_destructive(tool_name: str) -> bool:
    name = tool_name.lower()
    if name in _DESTRUCTIVE_TOOL_ALLOWLIST:
        return False
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
        "generate_keyframe_candidates", "generate_transition_candidates",
        "restore_checkpoint",
        "isolate_vocals__run",
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
        kfs = {kid: get_keyframe(project_dir, kid) for kid in ids}
        valid_ids = [kid for kid in ids if kfs.get(kid)]
        missing_ids = [kid for kid in ids if not kfs.get(kid)]
        items: list[str] = []
        for kid in valid_ids[:10]:
            items.append(_kf_line(kfs[kid]))
        if len(valid_ids) > 10:
            items.append(f"… and {len(valid_ids) - 10} more")
        for m in missing_ids[:4]:
            items.append(f"{m} (not found)")
        if len(missing_ids) > 4:
            items.append(f"… and {len(missing_ids) - 4} more missing")
        msg = f"Delete {len(ids)} keyframes? ({len(valid_ids)} valid, {len(missing_ids)} missing)" if missing_ids else f"Delete {len(ids)} keyframes?"
        return msg, items

    if tool_name == "batch_delete_transitions":
        ids = [s for s in (input_dict.get("transition_ids") or []) if isinstance(s, str)]
        trs = {tid: get_transition(project_dir, tid) for tid in ids}
        valid_ids = [tid for tid in ids if trs.get(tid)]
        missing_ids = [tid for tid in ids if not trs.get(tid)]
        items = []
        for tid in valid_ids[:10]:
            items.append(_tr_line(trs[tid]))
        if len(valid_ids) > 10:
            items.append(f"… and {len(valid_ids) - 10} more")
        for m in missing_ids[:4]:
            items.append(f"{m} (not found)")
        if len(missing_ids) > 4:
            items.append(f"… and {len(missing_ids) - 4} more missing")
        msg = f"Delete {len(ids)} transitions? ({len(valid_ids)} valid, {len(missing_ids)} missing)" if missing_ids else f"Delete {len(ids)} transitions?"
        return msg, items

    if tool_name == "generate_keyframe_candidates":
        kf_id = input_dict.get("keyframe_id", "")
        count = int(input_dict.get("count") or 3)
        kf = get_keyframe(project_dir, kf_id)
        if not kf:
            return f"Generate {count} images for {kf_id}?", [f"{kf_id} (not found)"]
        prompt = (input_dict.get("prompt_override") or kf.get("prompt") or "").strip()
        prompt_preview = prompt[:80] + ("..." if len(prompt) > 80 else "")
        est_cost_usd = count * 0.04  # Imagen ≈ $0.04/image
        items = [
            _kf_line(kf),
            f"prompt: {prompt_preview}" if prompt else "prompt: (empty — will fail)",
            f"~{est_cost_usd:.2f} USD · ~{count * 15}-{count * 30}s",
        ]
        return f"Generate {count} image candidates for {kf_id}?", items

    if tool_name == "generate_transition_candidates":
        from scenecraft.db import get_keyframe as _get_kf
        tr_id = input_dict.get("transition_id", "")
        count = int(input_dict.get("count") or 2)
        slot = input_dict.get("slot")
        tr = get_transition(project_dir, tr_id)
        if not tr:
            return f"Generate {count} videos for {tr_id}?", [f"{tr_id} (not found)"]
        n_slots = int(tr.get("slots", 1))
        target_slots = 1 if slot is not None else n_slots
        total = count * target_slots
        est_cost_usd = total * 0.50  # Veo ≈ $0.50/video
        action = (tr.get("action") or "").strip()
        action_preview = action[:80] + ("..." if len(action) > 80 else "")
        items = [_tr_line(tr)]
        if action:
            items.append(f"prompt: {action_preview}")
        items.append(
            f"slots: {'#' + str(slot) if slot is not None else f'all {n_slots}'} · "
            f"{count}/slot = {total} videos"
        )
        items.append(f"~{est_cost_usd:.2f} USD · ~{total * 45}-{total * 180}s")
        return f"Generate {total} video candidates for {tr_id}?", items

    if tool_name == "isolate_vocals__run":
        from scenecraft.db import get_audio_clips
        entity_type = input_dict.get("entity_type", "audio_clip")
        entity_id = input_dict.get("entity_id", "")
        range_mode = input_dict.get("range_mode", "full")
        trim_in = input_dict.get("trim_in")
        trim_out = input_dict.get("trim_out")

        label = entity_id
        total_dur = 0.0
        found = False
        if entity_type == "audio_clip":
            clip = next((c for c in get_audio_clips(project_dir) if c.get("id") == entity_id), None)
            if clip:
                found = True
                start = float(clip.get("start_time", 0))
                end = float(clip.get("end_time", 0))
                total_dur = max(0.0, end - start)
                src = clip.get("source_path") or ""
                label = src.rsplit("/", 1)[-1] if src else entity_id
        elif entity_type == "transition":
            tr = get_transition(project_dir, entity_id)
            if tr:
                found = True
                total_dur = float(tr.get("duration_seconds", 0))
                label = tr.get("label") or f"{tr.get('from','?')} → {tr.get('to','?')}"

        if not found:
            return (
                f"Isolate vocals on {entity_type} {entity_id}?",
                [f"{entity_id} (NOT FOUND)"],
            )

        if range_mode == "subset":
            active = max(0.0, (trim_out if trim_out is not None else total_dur) - (trim_in or 0.0))
            range_line = f"range: subset {trim_in}s–{trim_out}s ({active:.1f}s)"
        else:
            active = total_dur
            range_line = f"range: full ({active:.1f}s)"

        eta_low = max(1, int(active * 1.0))
        eta_high = max(2, int(active * 2.0))
        items = [
            f"{entity_type}: {label} ({entity_id})",
            range_line,
            "model: DeepFilterNet3 (CPU)",
            "output: 2 stems — vocal + background (new pool_segments, grouped under one audio_isolations run)",
            f"~{eta_low}-{eta_high}s to complete",
        ]
        return f"Isolate vocals on {entity_type} {entity_id}?", items

    if tool_name == "restore_checkpoint":
        from scenecraft.db import get_checkpoint as _db_get_checkpoint, get_db
        filename = input_dict.get("filename", "")
        entry = _db_get_checkpoint(project_dir, filename)
        file_path = project_dir / filename
        items: list[str] = []
        if entry:
            label = entry.get("name") or "(unnamed)"
            created = entry.get("created_at", "?")
            items.append(f"{filename}")
            items.append(f"name: {label}")
            items.append(f"created: {created}")
        elif file_path.exists():
            items.append(f"{filename}")
            items.append("metadata: not recorded in checkpoints table")
        else:
            return f"Restore checkpoint {filename}?", [f"{filename} (NOT FOUND)"]

        try:
            conn = get_db(project_dir)
            checkpoint_ts = entry.get("created_at") if entry else None
            if checkpoint_ts:
                since = conn.execute(
                    "SELECT COUNT(*) FROM undo_groups WHERE timestamp > ?", (checkpoint_ts,)
                ).fetchone()[0]
                if since > 0:
                    items.append(f"⚠ {since} undo groups will be lost")
        except Exception:
            pass
        return f"Restore {filename}? (DESTRUCTIVE)", items

    # Unreachable — caller gates on tool_name
    return f"Confirm `{tool_name}`?", []


def _humanize_tool_name(name: str) -> str:
    """Turn `remember_delete_memory` → `Remember · Delete Memory` for display."""
    parts = name.split("_")
    head = parts[0].capitalize()
    rest = " ".join(p.capitalize() for p in parts[1:])
    return f"{head} · {rest}" if rest else head


async def _recv_elicitation_response(
    waiters: dict[str, asyncio.Future],
    elicitation_id: str,
    timeout: float = 300,
) -> str:
    """Block until the matching elicitation_response arrives.

    Uses a futures dict populated by the single ws reader in
    `handle_chat_connection`. Returns the action ("accept" or "decline");
    anything else — including a timeout or ws close while waiting — is
    treated as a decline so the caller can proceed safely.
    """
    loop = asyncio.get_event_loop()
    fut: asyncio.Future = loop.create_future()
    waiters[elicitation_id] = fut
    try:
        action = await asyncio.wait_for(fut, timeout=timeout)
    except asyncio.TimeoutError:
        _log(f"elicitation {elicitation_id}: timeout, auto-declining")
        return "decline"
    except asyncio.CancelledError:
        # Propagate so the surrounding stream task can clean up and persist
        # its partial content. Don't swallow.
        raise
    finally:
        waiters.pop(elicitation_id, None)
    return "accept" if action == "accept" else "decline"


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


_UPDATE_KEYFRAME_FIELDS: tuple[str, ...] = (
    "timestamp", "prompt", "track_id", "section", "label", "label_color",
    "blend_mode", "opacity", "refinement_prompt",
)

_UPDATE_TRANSITION_FIELDS: tuple[str, ...] = (
    "duration_seconds", "slots", "action", "label", "label_color", "track_id",
    "tags", "blend_mode", "opacity", "use_global_prompt", "include_section_desc",
    "hidden", "is_adjustment", "remap", "negative_prompt", "seed",
)


def _exec_add_keyframe(project_dir: Path, input_data: dict) -> dict:
    from scenecraft.db import add_keyframe, next_keyframe_id, get_keyframe, undo_begin
    timestamp = input_data.get("timestamp")
    prompt = input_data.get("prompt")
    if not timestamp or not isinstance(timestamp, str):
        return {"error": "missing timestamp"}
    if not isinstance(prompt, str):
        return {"error": "prompt must be a string"}
    track_id = input_data.get("track_id") or "track_1"
    section = input_data.get("section") or ""
    label = input_data.get("label") or ""
    label_color = input_data.get("label_color") or ""

    kf_id = next_keyframe_id(project_dir)
    undo_begin(project_dir, f"Chat: add keyframe {kf_id} @ {timestamp}")
    add_keyframe(project_dir, {
        "id": kf_id,
        "timestamp": timestamp,
        "prompt": prompt,
        "track_id": track_id,
        "section": section,
        "label": label,
        "label_color": label_color,
        "candidates": [],
    })
    created = get_keyframe(project_dir, kf_id) or {}
    return {
        "keyframe_id": kf_id,
        "timestamp": created.get("timestamp", timestamp),
        "prompt": created.get("prompt", prompt),
        "track_id": created.get("track_id", track_id),
        "section": created.get("section", section),
        "label": created.get("label", label),
    }


def _exec_add_audio_track(project_dir: Path, input_data: dict) -> dict:
    import json
    from scenecraft.db import (
        add_audio_track as db_add_audio_track,
        get_audio_tracks as db_get_audio_tracks,
        generate_id, undo_begin,
    )
    name = input_data.get("name")
    muted = bool(input_data.get("muted", False))
    try:
        volume = float(input_data.get("volume", 1.0))
    except (TypeError, ValueError):
        return {"error": "volume must be a number"}
    if volume < 0.0 or volume > 2.0:
        return {"error": "volume must be between 0.0 and 2.0"}

    existing = db_get_audio_tracks(project_dir)
    track_id = generate_id("audio_track")
    display_order = max((t["display_order"] for t in existing), default=-1) + 1
    if not name:
        name = f"Audio Track {len(existing) + 1}"
    volume_curve = json.dumps([[0, volume], [1, volume]])

    undo_begin(project_dir, f"Chat: add audio track {track_id}")
    db_add_audio_track(project_dir, {
        "id": track_id,
        "name": name,
        "display_order": display_order,
        "hidden": False,
        "muted": muted,
        "solo": False,
        "volume_curve": volume_curve,
    })
    return {
        "track_id": track_id,
        "name": name,
        "display_order": display_order,
        "muted": muted,
        "volume": volume,
    }


def _exec_add_audio_clip(project_dir: Path, input_data: dict) -> dict:
    from scenecraft.db import (
        add_audio_clip as db_add_audio_clip,
        get_pool_segment, get_audio_tracks as db_get_audio_tracks,
        generate_id, undo_begin,
    )
    track_id = input_data.get("track_id")
    source_segment_id = input_data.get("source_segment_id")
    if not track_id or not isinstance(track_id, str):
        return {"error": "missing track_id"}
    if not source_segment_id or not isinstance(source_segment_id, str):
        return {"error": "missing source_segment_id (pool_segments.id)"}

    # Validate track exists
    tracks = db_get_audio_tracks(project_dir)
    if not any(t["id"] == track_id for t in tracks):
        return {"error": f"audio track not found: {track_id}"}

    # Resolve pool segment by id. Its pool_path becomes audio_clips.source_path.
    seg = get_pool_segment(project_dir, source_segment_id)
    if seg is None:
        return {"error": f"pool_segment not found: {source_segment_id}"}
    source_path = seg.get("poolPath") or seg.get("pool_path")
    if not source_path:
        return {"error": f"pool_segment {source_segment_id} has no pool_path"}

    try:
        start_time = float(input_data["start_time"])
    except (KeyError, TypeError, ValueError):
        return {"error": "start_time is required and must be a number"}

    # Resolve trim_in / source_offset — trim_in wins if provided
    trim_in = input_data.get("trim_in")
    if trim_in is not None:
        try:
            source_offset = float(trim_in)
        except (TypeError, ValueError):
            return {"error": "trim_in must be a number"}
    else:
        try:
            source_offset = float(input_data.get("source_offset", 0.0))
        except (TypeError, ValueError):
            return {"error": "source_offset must be a number"}
    if source_offset < 0:
        return {"error": "trim_in / source_offset must be >= 0"}

    # Resolve end_time — trim_out wins, else explicit end_time, else auto from duration
    trim_out = input_data.get("trim_out")
    end_time_in = input_data.get("end_time")
    if trim_out is not None:
        try:
            trim_out_f = float(trim_out)
        except (TypeError, ValueError):
            return {"error": "trim_out must be a number"}
        if trim_out_f <= source_offset:
            return {"error": f"trim_out ({trim_out_f}) must be greater than trim_in ({source_offset})"}
        end_time = start_time + (trim_out_f - source_offset)
    elif end_time_in is not None:
        try:
            end_time = float(end_time_in)
        except (TypeError, ValueError):
            return {"error": "end_time must be a number"}
    else:
        duration = seg.get("durationSeconds") or seg.get("duration_seconds")
        if duration is None:
            return {
                "error": (
                    f"cannot auto-compute end_time: pool_segment {source_segment_id} "
                    "has no duration_seconds; pass end_time or trim_out explicitly."
                )
            }
        end_time = start_time + (float(duration) - source_offset)

    if end_time <= start_time:
        return {"error": f"end_time ({end_time}) must be greater than start_time ({start_time})"}

    # Seed label from pool segment if none provided
    label = input_data.get("label")
    if not label:
        seed = seg.get("label") or seg.get("originalFilename") or seg.get("original_filename")
        if seed and "." in seed and not seg.get("label"):
            seed = seed.rsplit(".", 1)[0]
        label = seed or None

    volume_curve = input_data.get("volume_curve", "[[0,1],[1,1]]")

    clip_id = generate_id("audio_clip")
    undo_begin(project_dir, f"Chat: add audio clip {clip_id} to track {track_id}")
    db_add_audio_clip(project_dir, {
        "id": clip_id,
        "track_id": track_id,
        "source_path": source_path,
        "start_time": start_time,
        "end_time": end_time,
        "source_offset": source_offset,
        "volume_curve": volume_curve,
        "muted": False,
        "remap": {"method": "linear", "target_duration": 0},
        "label": label,
    })
    return {
        "audio_clip_id": clip_id,
        "track_id": track_id,
        "source_segment_id": source_segment_id,
        "source_path": source_path,
        "start_time": start_time,
        "end_time": end_time,
        "source_offset": source_offset,
        "label": label,
    }


def _exec_update_volume_curve(project_dir: Path, input_data: dict) -> dict:
    """Replace volume_curve on an audio_track or audio_clip.

    The underlying schema only stores the ``volume_curve`` JSON column — there is
    no dedicated ``interpolation`` column on audio_tracks/audio_clips. We accept
    the ``interpolation`` argument (validated to the same vocabulary as
    EffectCurve), but note in the result that it is not persisted.
    """
    import json
    import math
    from scenecraft.db import (
        update_audio_track as db_update_audio_track,
        update_audio_clip as db_update_audio_clip,
        get_audio_tracks as db_get_audio_tracks,
        get_audio_clips as db_get_audio_clips,
        undo_begin,
    )

    target_type = input_data.get("target_type")
    if target_type not in ("track", "clip"):
        return {"error": "target_type must be 'track' or 'clip'"}

    target_id = input_data.get("target_id")
    if not target_id or not isinstance(target_id, str):
        return {"error": "target_id is required and must be a string"}

    interpolation = input_data.get("interpolation", "bezier")
    if interpolation not in ("bezier", "linear", "step"):
        return {"error": "interpolation must be one of 'bezier', 'linear', 'step'"}

    # Parse points — accept either list-of-[t,v] or a JSON string of that.
    raw_points = input_data.get("points")
    if raw_points is None:
        return {"error": "points is required"}
    if isinstance(raw_points, str):
        try:
            points = json.loads(raw_points)
        except (ValueError, TypeError) as exc:
            return {"error": f"points JSON string is not valid JSON: {exc}"}
    else:
        points = raw_points

    if not isinstance(points, list):
        return {"error": "points must be a list of [time, value] pairs"}
    if len(points) < 2:
        return {"error": "volume curve requires at least 2 points"}

    # Each point must be [number, number]; times strictly increasing; first time == 0.
    cleaned: list[list[float]] = []
    prev_t: float | None = None
    for idx, pt in enumerate(points):
        if not isinstance(pt, (list, tuple)) or len(pt) != 2:
            return {"error": f"point at index {idx} must be a [time, value] pair"}
        try:
            t = float(pt[0])
            v = float(pt[1])
        except (TypeError, ValueError):
            return {"error": f"point at index {idx} must have numeric [time, value]"}
        if not (math.isfinite(t) and math.isfinite(v)):
            return {"error": f"point at index {idx} has non-finite value"}
        if idx == 0 and t != 0.0:
            return {"error": "first point time must be 0.0"}
        if prev_t is not None and t <= prev_t:
            return {
                "error": (
                    f"point times must be strictly increasing; "
                    f"got {t} after {prev_t} at index {idx}"
                )
            }
        cleaned.append([t, v])
        prev_t = t

    # Validate target exists before starting an undo group.
    if target_type == "track":
        if not any(t["id"] == target_id for t in db_get_audio_tracks(project_dir)):
            return {"error": f"track not found: {target_id}"}
    else:
        if not any(c["id"] == target_id for c in db_get_audio_clips(project_dir)):
            return {"error": f"clip not found: {target_id}"}

    volume_curve_json = json.dumps(cleaned)

    undo_group_id = undo_begin(
        project_dir,
        f"Chat: update volume_curve on {target_type} {target_id}",
    )
    if target_type == "track":
        db_update_audio_track(project_dir, target_id, volume_curve=volume_curve_json)
    else:
        db_update_audio_clip(project_dir, target_id, volume_curve=volume_curve_json)

    result: dict = {
        "ok": True,
        "target_type": target_type,
        "target_id": target_id,
        "points_written": len(cleaned),
        "undo_group_id": undo_group_id,
    }
    # interpolation has no persisted column yet — surface the fact so callers
    # aren't surprised when the value doesn't round-trip through the DB.
    result["interpolation_note"] = (
        f"interpolation='{interpolation}' accepted but not persisted "
        "(audio_tracks/audio_clips have no interpolation column)."
    )
    return result
_DSP_KNOWN_ANALYSES: frozenset[str] = frozenset({
    "rms", "onsets", "vocal_presence", "tempo", "spectral_centroid",
})
_DSP_SAMPLE_RATE: int = 22050
_DSP_HOP_LENGTH: int = 512


def _dsp_params_hash(analyses: list[str], sr: int, hop_length: int) -> str:
    """Deterministic 16-char hash of the parameters that can change a run's
    output. Sorted analyses so order doesn't affect the cache key."""
    import hashlib
    payload = json.dumps(
        {"analyses": sorted(analyses), "sr": sr, "hop_length": hop_length},
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def _exec_generate_dsp(project_dir: Path, input_data: dict) -> dict:
    """Run librosa analyses on a pool_segment and cache results in dsp_* tables.

    Returns a dict describing the run (new or cached). Never raises for
    invalid inputs — returns ``{"error": ...}`` instead.
    """
    source_segment_id = input_data.get("source_segment_id")
    if not source_segment_id or not isinstance(source_segment_id, str):
        return {"error": "missing source_segment_id"}

    raw_analyses = input_data.get("analyses")
    if raw_analyses is None:
        analyses = ["onsets", "rms", "vocal_presence", "tempo"]
    elif not isinstance(raw_analyses, list):
        return {"error": "analyses must be a list of strings"}
    else:
        analyses = [str(a) for a in raw_analyses]

    force_rerun = bool(input_data.get("force_rerun", False))

    # Resolve the pool segment and its on-disk file.
    from scenecraft.db import get_pool_segment
    seg = get_pool_segment(project_dir, source_segment_id)
    if seg is None:
        return {"error": f"pool_segment not found: {source_segment_id}"}
    pool_path = seg.get("poolPath") or seg.get("pool_path")
    if not pool_path:
        return {"error": f"pool_segment {source_segment_id} has no pool_path"}
    abs_path = (Path(project_dir) / pool_path).resolve()
    if not abs_path.exists():
        return {"error": f"source file not found: {abs_path}"}

    import librosa as _librosa  # local import — keeps chat.py import cheap

    analyzer_version = f"librosa-{_librosa.__version__}"
    params_hash = _dsp_params_hash(analyses, _DSP_SAMPLE_RATE, _DSP_HOP_LENGTH)

    from scenecraft.db_analysis_cache import (
        bulk_insert_dsp_datapoints,
        bulk_insert_dsp_sections,
        create_dsp_run,
        delete_dsp_run,
        get_dsp_run,
        get_dsp_scalars,
        query_dsp_datapoints,
        query_dsp_sections,
        set_dsp_scalars,
    )

    # Cache-hit path: return the existing run (unless forcing rerun).
    existing = get_dsp_run(project_dir, source_segment_id, analyzer_version, params_hash)
    if existing is not None and not force_rerun:
        # Count what's already stored for each known data_type/section_type.
        dp_count = 0
        for dt in ("rms", "onset", "spectral_centroid"):
            dp_count += len(query_dsp_datapoints(project_dir, existing.id, dt))
        sec_count = len(query_dsp_sections(project_dir, existing.id))
        scalars = get_dsp_scalars(project_dir, existing.id)
        return {
            "run_id": existing.id,
            "cached": True,
            "source_segment_id": source_segment_id,
            "analyses_written": list(existing.analyses),
            "datapoint_count": dp_count,
            "section_count": sec_count,
            "scalars": scalars,
        }

    # Forced rerun: clear the old row so the UNIQUE cache key is available.
    if existing is not None and force_rerun:
        delete_dsp_run(project_dir, existing.id)

    # Load audio once and reuse across analyses.
    from scenecraft.analyzer import detect_presence, load_audio
    from scenecraft.audio_intelligence import _compute_rms_envelope, _detect_onsets

    try:
        y, sr = load_audio(str(abs_path), sr=_DSP_SAMPLE_RATE)
    except (FileNotFoundError, ValueError) as e:
        return {"error": f"failed to load audio: {e}"}

    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat()

    # Track what actually ran so known-but-failed analyses can be reported.
    analyses_written: list[str] = []
    analyses_to_store: list[str] = []
    datapoint_count = 0
    section_count = 0
    scalars: dict[str, float] = {}
    datapoints: list[tuple[str, float, float, dict[str, Any] | None]] = []
    sections: list[tuple[float, float, str, str | None, float | None]] = []

    for analysis in analyses:
        if analysis not in _DSP_KNOWN_ANALYSES:
            # Unknown: skip silently (per spec).
            continue

        if analysis == "rms":
            env = _compute_rms_envelope(y, sr, hop_length=_DSP_HOP_LENGTH)
            for p in env:
                datapoints.append(("rms", float(p["time"]), float(p["energy"]), None))
            analyses_written.append("rms")
            analyses_to_store.append("rms")

        elif analysis == "onsets":
            onsets = _detect_onsets(y, sr, hop_length=_DSP_HOP_LENGTH)
            for o in onsets:
                strength = float(o["strength"])
                datapoints.append((
                    "onset", float(o["time"]), strength, {"strength": strength},
                ))
            analyses_written.append("onsets")
            analyses_to_store.append("onsets")

        elif analysis == "vocal_presence":
            regions = detect_presence(y, sr, hop_length=_DSP_HOP_LENGTH)
            for r in regions:
                sections.append((
                    float(r["start_time"]), float(r["end_time"]),
                    "vocal_presence", None, None,
                ))
            analyses_written.append("vocal_presence")
            analyses_to_store.append("vocal_presence")

        elif analysis == "tempo":
            try:
                tempo, _beats = _librosa.beat.beat_track(
                    y=y, sr=sr, hop_length=_DSP_HOP_LENGTH,
                )
                # librosa may return a scalar or a 1-element array.
                import numpy as _np
                tempo_val = float(_np.atleast_1d(tempo)[0])
                scalars["tempo_bpm"] = tempo_val
                analyses_written.append("tempo")
                analyses_to_store.append("tempo")
            except Exception as e:
                _log(f"tempo analysis failed: {e}")

        elif analysis == "spectral_centroid":
            try:
                import numpy as _np
                centroid = _librosa.feature.spectral_centroid(
                    y=y, sr=sr, hop_length=_DSP_HOP_LENGTH,
                )[0]
                frames_per_sec = sr / _DSP_HOP_LENGTH
                # Downsample to ~20 pts/sec for manageable storage.
                step = max(1, int(frames_per_sec / 20))
                for i in range(0, len(centroid), step):
                    t = float(i / frames_per_sec)
                    datapoints.append((
                        "spectral_centroid", t, float(centroid[i]), None,
                    ))
                analyses_written.append("spectral_centroid")
                analyses_to_store.append("spectral_centroid")
            except Exception as e:
                _log(f"spectral_centroid analysis failed: {e}")

    # Create the run row *after* analyses succeed, so failures don't leave
    # an empty row behind.
    run = create_dsp_run(
        project_dir,
        source_segment_id=source_segment_id,
        analyzer_version=analyzer_version,
        params_hash=params_hash,
        analyses=analyses_to_store,
        created_at=now,
    )

    if datapoints:
        datapoint_count = bulk_insert_dsp_datapoints(project_dir, run.id, datapoints)
    if sections:
        section_count = bulk_insert_dsp_sections(project_dir, run.id, sections)
    if scalars:
        set_dsp_scalars(project_dir, run.id, scalars)

    return {
        "run_id": run.id,
        "cached": False,
        "source_segment_id": source_segment_id,
        "analyses_written": analyses_written,
        "datapoint_count": datapoint_count,
        "section_count": section_count,
        "scalars": scalars,
    }


# ── generate_descriptions (Phase 3 — structured LLM audio analysis) ────────


_DESCRIPTION_PROPERTIES: tuple[str, ...] = (
    "section_type",
    "mood",
    "energy",
    "vocal_style",
    "instrumentation",
)


def _rows_from_description(
    desc: dict,
    start_s: float,
    end_s: float,
) -> list[tuple[float, float, str, str | None, float | None, float | None, dict[str, Any] | None]]:
    """Convert a structured Gemini description dict to per-property rows.

    Returned tuples are ``(start_s, end_s, property, value_text, value_num,
    confidence, raw)`` — matching ``bulk_insert_audio_descriptions``.

    Missing / invalid fields are skipped silently; the goal is best-effort
    persistence of whatever the LLM returned.
    """
    rows: list[tuple[float, float, str, str | None, float | None, float | None, dict[str, Any] | None]] = []

    section_type = desc.get("section_type")
    if isinstance(section_type, str) and section_type:
        rows.append((start_s, end_s, "section_type", section_type, None, None, None))

    mood = desc.get("mood")
    if isinstance(mood, str) and mood:
        rows.append((start_s, end_s, "mood", mood, None, None, None))

    energy = desc.get("energy")
    if isinstance(energy, (int, float)):
        energy_val = float(energy)
        # Clamp to [0, 1] defensively — the model may drift.
        energy_val = max(0.0, min(1.0, energy_val))
        rows.append((start_s, end_s, "energy", None, energy_val, None, None))

    vocal_style = desc.get("vocal_style")
    if isinstance(vocal_style, str) and vocal_style:
        rows.append((start_s, end_s, "vocal_style", vocal_style, None, None, None))
    # If vocal_style is explicitly null/None, we still record it so queries can
    # distinguish "instrumental" from "not analyzed".
    elif vocal_style is None and "vocal_style" in desc:
        rows.append((start_s, end_s, "vocal_style", None, None, None, None))

    instrumentation = desc.get("instrumentation")
    if isinstance(instrumentation, list):
        # Summarize as a comma-joined string for value_text; keep the raw list
        # in raw_json so callers can reconstruct the array.
        instruments = [str(x) for x in instrumentation if isinstance(x, str) and x]
        if instruments:
            rows.append((
                start_s, end_s, "instrumentation",
                ",".join(instruments), None, None,
                {"instruments": instruments},
            ))

    notes = desc.get("notes")
    if isinstance(notes, str) and notes.strip():
        rows.append((start_s, end_s, "notes", notes, None, None, None))

    return rows


def _exec_generate_descriptions(project_dir: Path, input_data: dict) -> dict:
    """Run Gemini chunk-description over a pool_segment and cache results.

    Returns a dict describing the run (new or cached). Never raises for
    invalid inputs — returns ``{"error": ...}`` instead.
    """
    source_segment_id = input_data.get("source_segment_id")
    if not source_segment_id or not isinstance(source_segment_id, str):
        return {"error": "missing source_segment_id"}

    model = str(input_data.get("model") or "gemini-2.5-pro")
    chunk_size_s = float(input_data.get("chunk_size_s") or 30.0)
    prompt_version = str(input_data.get("prompt_version") or "v1")
    force_rerun = bool(input_data.get("force_rerun", False))

    # Resolve the pool segment and its on-disk file.
    from scenecraft.db import get_pool_segment
    seg = get_pool_segment(project_dir, source_segment_id)
    if seg is None:
        return {"error": f"pool_segment not found: {source_segment_id}"}
    pool_path = seg.get("poolPath") or seg.get("pool_path")
    if not pool_path:
        return {"error": f"pool_segment {source_segment_id} has no pool_path"}
    abs_path = (Path(project_dir) / pool_path).resolve()
    if not abs_path.exists():
        return {"error": f"source file not found: {abs_path}"}

    from scenecraft.db_analysis_cache import (
        bulk_insert_audio_descriptions,
        create_audio_description_run,
        delete_audio_description_run,
        get_audio_description_run,
        query_audio_descriptions,
    )

    # Cache-hit path: return the existing run (unless forcing rerun).
    existing = get_audio_description_run(
        project_dir, source_segment_id, model, prompt_version,
    )
    if existing is not None and not force_rerun:
        stored = query_audio_descriptions(project_dir, existing.id)
        # Count distinct (start_s, end_s) pairs → chunks that produced any row.
        chunks_seen = len({(d.start_s, d.end_s) for d in stored})
        return {
            "run_id": existing.id,
            "cached": True,
            "source_segment_id": source_segment_id,
            "chunks_analyzed": chunks_seen,
            "chunks_failed": 0,
            "descriptions_written": len(stored),
        }

    # Forced rerun: clear the old row so the UNIQUE cache key is available.
    if existing is not None and force_rerun:
        delete_audio_description_run(project_dir, existing.id)

    from scenecraft.audio_intelligence import (
        _chunk_audio_for_gemini,
        _gemini_describe_chunk_structured,
    )

    try:
        chunks = _chunk_audio_for_gemini(str(abs_path), chunk_duration=chunk_size_s)
    except Exception as e:
        return {"error": f"failed to chunk audio: {e}"}

    chunks_analyzed = 0
    chunks_failed = 0
    all_rows: list[tuple[float, float, str, str | None, float | None, float | None, dict[str, Any] | None]] = []

    for chunk in chunks:
        desc = _gemini_describe_chunk_structured(
            chunk["path"],
            float(chunk["start_time"]),
            float(chunk["end_time"]),
            model=model,
            prompt_version=prompt_version,
        )
        if desc is None:
            chunks_failed += 1
            continue
        rows = _rows_from_description(
            desc, float(chunk["start_time"]), float(chunk["end_time"]),
        )
        if rows:
            chunks_analyzed += 1
            all_rows.extend(rows)
        else:
            # Gemini returned a dict, but nothing recognisable inside it.
            chunks_failed += 1

    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat()

    # Create the run row *after* analyses, so failures don't leave an empty
    # row behind. Matches generate_dsp's behaviour.
    run = create_audio_description_run(
        project_dir,
        source_segment_id=source_segment_id,
        model=model,
        prompt_version=prompt_version,
        chunk_size_s=chunk_size_s,
        created_at=now,
    )

    descriptions_written = 0
    if all_rows:
        descriptions_written = bulk_insert_audio_descriptions(
            project_dir, run.id, all_rows,
        )

    return {
        "run_id": run.id,
        "cached": False,
        "source_segment_id": source_segment_id,
        "chunks_analyzed": chunks_analyzed,
        "chunks_failed": chunks_failed,
        "descriptions_written": descriptions_written,
    }


def _exec_update_keyframe(project_dir: Path, input_data: dict) -> dict:
    from scenecraft.db import get_keyframe, update_keyframe, undo_begin
    kf_id = input_data.get("keyframe_id")
    if not kf_id or not isinstance(kf_id, str):
        return {"error": "missing keyframe_id"}
    existing = get_keyframe(project_dir, kf_id)
    if not existing:
        return {"error": f"keyframe not found: {kf_id}"}

    fields: dict[str, Any] = {}
    old_values: dict[str, Any] = {}
    for key in _UPDATE_KEYFRAME_FIELDS:
        if key in input_data and input_data[key] is not None:
            fields[key] = input_data[key]
            old_values[key] = existing.get(key)

    if not fields:
        return {"error": "no updatable fields provided; see schema for allowed keys"}

    undo_begin(project_dir, f"Chat: update keyframe {kf_id}")
    update_keyframe(project_dir, kf_id, **fields)
    return {
        "keyframe_id": kf_id,
        "updated_fields": sorted(fields.keys()),
        "old_values": old_values,
    }


def _exec_update_transition(project_dir: Path, input_data: dict) -> dict:
    from scenecraft.db import get_transition, update_transition, undo_begin
    tr_id = input_data.get("transition_id")
    if not tr_id or not isinstance(tr_id, str):
        return {"error": "missing transition_id"}
    existing = get_transition(project_dir, tr_id)
    if not existing:
        return {"error": f"transition not found: {tr_id}"}

    fields: dict[str, Any] = {}
    old_values: dict[str, Any] = {}
    for key in _UPDATE_TRANSITION_FIELDS:
        if key in input_data and input_data[key] is not None:
            fields[key] = input_data[key]
            # Map fields whose response key differs from storage key
            if key == "negative_prompt":
                old_values[key] = existing.get("negativePrompt")
            else:
                old_values[key] = existing.get(key)

    if not fields:
        return {"error": "no updatable fields provided; see schema for allowed keys"}

    undo_begin(project_dir, f"Chat: update transition {tr_id}")
    update_transition(project_dir, tr_id, **fields)
    return {
        "transition_id": tr_id,
        "updated_fields": sorted(fields.keys()),
        "old_values": old_values,
    }


def _exec_checkpoint(project_dir: Path, input_data: dict) -> dict:
    import sqlite3 as _sqlite3
    from scenecraft.db import add_checkpoint

    db_path = project_dir / "project.db"
    if not db_path.exists():
        return {"error": "no project.db in this project"}

    name = input_data.get("name") or ""
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"project.db.checkpoint-{ts}"
    dst = project_dir / filename

    # Online SQLite backup (safe under WAL)
    src_conn = _sqlite3.connect(str(db_path))
    dst_conn = _sqlite3.connect(str(dst))
    try:
        src_conn.backup(dst_conn)
    finally:
        dst_conn.close()
        src_conn.close()

    created_iso = datetime.now().astimezone().isoformat()
    add_checkpoint(project_dir, filename, name=name, created_at=created_iso)

    return {
        "filename": filename,
        "name": name,
        "created_at": created_iso,
        "size_bytes": dst.stat().st_size,
    }


def _exec_list_checkpoints(project_dir: Path, input_data: dict) -> dict:
    from scenecraft.db import list_checkpoints as _db_list_checkpoints

    meta_by_file = {c["filename"]: c for c in _db_list_checkpoints(project_dir)}
    checkpoints = []
    for f in sorted(project_dir.glob("project.db.checkpoint-*"), reverse=True):
        stat = f.stat()
        meta = meta_by_file.get(f.name, {})
        checkpoints.append({
            "filename": f.name,
            "name": meta.get("name", ""),
            "created_at": meta.get("created_at"),
            "size_bytes": stat.st_size,
        })
    return {"checkpoints": checkpoints, "count": len(checkpoints)}


def _exec_restore_checkpoint(project_dir: Path, input_data: dict) -> dict:
    import sqlite3 as _sqlite3
    from scenecraft.db import close_db

    filename = input_data.get("filename") or ""
    if not filename or not isinstance(filename, str):
        return {"error": "missing filename"}
    if not filename.startswith("project.db.checkpoint-"):
        return {"error": f"not a valid checkpoint filename: {filename}"}

    checkpoint_path = project_dir / filename
    if not checkpoint_path.exists():
        return {"error": f"checkpoint not found: {filename}"}

    db_path = project_dir / "project.db"
    # Close pooled connections before overwriting
    close_db(project_dir)

    src_conn = _sqlite3.connect(str(checkpoint_path))
    dst_conn = _sqlite3.connect(str(db_path))
    try:
        src_conn.backup(dst_conn)
    finally:
        dst_conn.close()
        src_conn.close()

    return {
        "restored_from": filename,
        "restored_at": datetime.now().isoformat(),
    }


def _parse_ts_seconds(ts: Any) -> float | None:
    """Parse a timestamp string into seconds. Returns None on malformed input."""
    if ts is None:
        return None
    if isinstance(ts, (int, float)):
        return float(ts)
    s = str(ts).strip()
    if not s:
        return None
    try:
        if ":" in s:
            parts = s.split(":")
            if len(parts) == 2:
                return int(parts[0]) * 60 + float(parts[1])
            if len(parts) == 3:
                return int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2])
            return None
        return float(s)
    except (ValueError, TypeError):
        return None


def _exec_split_transition(project_dir: Path, input_data: dict) -> dict:
    from scenecraft.db import (
        get_transition, get_keyframe, add_keyframe, add_transition,
        update_transition, next_keyframe_id, next_transition_id, undo_begin,
    )

    tr_id = input_data.get("transition_id")
    at_time = input_data.get("at_time")
    new_prompt = input_data.get("new_keyframe_prompt") or ""

    if not tr_id or not isinstance(tr_id, str):
        return {"error": "missing transition_id"}

    tr = get_transition(project_dir, tr_id)
    if not tr:
        return {"error": f"transition not found: {tr_id}"}
    if tr.get("deleted_at"):
        return {"error": f"transition {tr_id} is deleted; restore it first"}

    from_kf_id = tr.get("from")
    to_kf_id = tr.get("to")
    from_kf = get_keyframe(project_dir, from_kf_id) if from_kf_id else None
    to_kf = get_keyframe(project_dir, to_kf_id) if to_kf_id else None
    if not from_kf or not to_kf:
        return {"error": f"transition {tr_id} has missing endpoints (from={from_kf_id}, to={to_kf_id})"}

    at_sec = _parse_ts_seconds(at_time)
    from_sec = _parse_ts_seconds(from_kf.get("timestamp"))
    to_sec = _parse_ts_seconds(to_kf.get("timestamp"))
    if at_sec is None:
        return {"error": f"invalid at_time: {at_time!r}"}
    if from_sec is None or to_sec is None:
        return {"error": "could not parse endpoint timestamps"}
    if not (from_sec < at_sec < to_sec):
        return {
            "error": f"at_time {at_time} ({at_sec}s) must be strictly between from_kf ({from_sec}s) and to_kf ({to_sec}s)"
        }

    # Format at_sec as m:ss.fff for timeline display
    mins = int(at_sec // 60)
    secs = at_sec - mins * 60
    new_timestamp = f"{mins}:{secs:06.3f}"

    new_kf_id = next_keyframe_id(project_dir)
    new_tr_id = next_transition_id(project_dir)

    undo_begin(project_dir, f"Chat: split {tr_id} at {new_timestamp}")

    add_keyframe(project_dir, {
        "id": new_kf_id,
        "timestamp": new_timestamp,
        "prompt": new_prompt,
        "track_id": tr.get("track_id", "track_1"),
        "section": "",
        "label": "",
        "candidates": [],
    })

    # Duration for each half, preserving total duration
    total_dur = float(tr.get("duration_seconds") or (to_sec - from_sec))
    first_dur = at_sec - from_sec
    second_dur = to_sec - at_sec
    # If the caller had a smaller total_dur than the natural span, scale proportionally
    if total_dur > 0 and abs(total_dur - (to_sec - from_sec)) > 0.001:
        scale = total_dur / (to_sec - from_sec)
        first_dur *= scale
        second_dur *= scale

    add_transition(project_dir, {
        "id": new_tr_id,
        "from": new_kf_id,
        "to": to_kf_id,
        "duration_seconds": second_dur,
        "slots": tr.get("slots", 1),
        "action": tr.get("action", ""),
        "use_global_prompt": int(bool(tr.get("use_global_prompt", True))),
        "include_section_desc": int(bool(tr.get("include_section_desc", True))),
        "track_id": tr.get("track_id", "track_1"),
    })

    update_transition(project_dir, tr_id, to=new_kf_id, duration_seconds=first_dur)

    return {
        "original_transition_id": tr_id,
        "new_keyframe_id": new_kf_id,
        "new_transition_id": new_tr_id,
        "split_at": new_timestamp,
        "first_duration_seconds": round(first_dur, 3),
        "second_duration_seconds": round(second_dur, 3),
    }


def _exec_assign_keyframe_image(project_dir: Path, input_data: dict) -> dict:
    from scenecraft.db import get_keyframe, update_keyframe, undo_begin
    kf_id = input_data.get("keyframe_id")
    variant = input_data.get("variant")
    if not kf_id or not isinstance(kf_id, str):
        return {"error": "missing keyframe_id"}
    try:
        variant = int(variant)
    except (TypeError, ValueError):
        return {"error": "variant must be an integer"}
    if variant < 1:
        return {"error": "variant must be >= 1"}

    kf = get_keyframe(project_dir, kf_id)
    if not kf:
        return {"error": f"keyframe not found: {kf_id}"}

    candidates = kf.get("candidates") or []
    # Derive valid variant numbers from candidate paths like ".../v3.png"
    import re as _re
    available: list[int] = []
    for p in candidates:
        m = _re.search(r"v(\d+)\.\w+$", str(p))
        if m:
            available.append(int(m.group(1)))

    if variant not in available:
        return {
            "error": f"variant {variant} is not among this keyframe's candidates",
            "available_variants": sorted(available),
        }

    previous = kf.get("selected")
    undo_begin(project_dir, f"Chat: assign image v{variant} to {kf_id}")
    update_keyframe(project_dir, kf_id, selected=variant)

    return {
        "keyframe_id": kf_id,
        "selected_variant": variant,
        "previous_variant": previous,
        "candidate_path": next((c for c in candidates if f"v{variant}." in str(c)), None),
    }


def _exec_assign_pool_video(project_dir: Path, input_data: dict) -> dict:
    from scenecraft.db import (
        get_transition, get_pool_segment, update_transition, get_tr_candidates, undo_begin,
    )

    tr_id = input_data.get("transition_id")
    pool_seg_id = input_data.get("pool_segment_id")
    slot = input_data.get("slot", 0)

    if not tr_id or not isinstance(tr_id, str):
        return {"error": "missing transition_id"}
    if not pool_seg_id or not isinstance(pool_seg_id, str):
        return {"error": "missing pool_segment_id"}
    try:
        slot = int(slot)
    except (TypeError, ValueError):
        return {"error": "slot must be an integer"}
    if slot < 0:
        return {"error": "slot must be >= 0"}

    tr = get_transition(project_dir, tr_id)
    if not tr:
        return {"error": f"transition not found: {tr_id}"}

    n_slots = int(tr.get("slots", 1))
    if slot >= n_slots:
        return {"error": f"slot {slot} out of range (transition has {n_slots} slots)"}

    seg = get_pool_segment(project_dir, pool_seg_id)
    if not seg:
        return {"error": f"pool_segment not found: {pool_seg_id}"}

    slot_cands = get_tr_candidates(project_dir, tr_id, slot)
    # get_tr_candidates returns joined pool_segment dicts; pool_segment id is keyed "id"
    cand_ids = [c.get("id") for c in slot_cands if c.get("id")]
    if pool_seg_id not in cand_ids:
        return {
            "error": f"pool_segment {pool_seg_id} is not a candidate for slot {slot}",
            "available_candidates": cand_ids,
        }

    # Build / update the selected list (length == slots, entries are pool_segment_id or None)
    raw_selected = tr.get("selected")
    if raw_selected is None:
        selected_list: list = [None] * n_slots
    elif isinstance(raw_selected, list):
        selected_list = list(raw_selected) + [None] * max(0, n_slots - len(raw_selected))
    else:
        # Legacy: single scalar — expand
        selected_list = [raw_selected] + [None] * (n_slots - 1)

    previous = selected_list[slot]
    selected_list[slot] = pool_seg_id

    undo_begin(project_dir, f"Chat: assign video for {tr_id} slot {slot}")
    update_transition(project_dir, tr_id, selected=selected_list)

    return {
        "transition_id": tr_id,
        "slot": slot,
        "pool_segment_id": pool_seg_id,
        "previous_pool_segment_id": previous,
    }


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


async def _await_generation_job(
    ws, tool_use_id: str, project_name: str, job_id: str, poll_interval: float = 0.5, timeout: float = 900
) -> tuple[dict, bool]:
    """Poll job_manager until terminal state; forward tool_progress events to the chat WS.

    Returns (result_dict, is_error). Final result includes the job's completion payload.
    On timeout, returns an error but does NOT cancel the underlying job — it keeps running.
    """
    import asyncio
    from scenecraft.ws_server import job_manager

    deadline = asyncio.get_event_loop().time() + timeout
    last_completed = -1
    last_detail = ""
    while True:
        if asyncio.get_event_loop().time() > deadline:
            return {"error": f"generation job {job_id} did not finish within {timeout}s; it may still be running"}, True

        job = job_manager.get_job(job_id)
        if job is None:
            return {"error": f"job {job_id} vanished"}, True

        # Forward progress updates if the counter or detail changed
        completed = getattr(job, "completed", 0) or 0
        detail = getattr(job, "meta", {}).get("last_detail", "") or last_detail
        # Job.meta doesn't store detail; use completed/total as proxy
        total = getattr(job, "total", 0) or 0
        if completed != last_completed:
            pct = (completed / total) if total else 0.0
            try:
                await ws.send(json.dumps({
                    "type": "tool_progress",
                    "toolProgress": {
                        "id": tool_use_id,
                        "phase": "generating",
                        "pct": pct,
                        "message": f"{completed}/{total}" if total else str(completed),
                    },
                }))
            except Exception:
                pass
            last_completed = completed

        status = getattr(job, "status", "running")
        if status == "completed":
            return (getattr(job, "result", None) or {}), False
        if status == "failed":
            return {"error": getattr(job, "error", None) or "generation failed"}, True

        await asyncio.sleep(poll_interval)


async def _execute_tool(
    project_dir: Path,
    name: str,
    input_data: dict,
    *,
    ws=None,
    tool_use_id: str | None = None,
    project_name: str | None = None,
) -> tuple[dict, bool]:
    """Execute a tool. Returns (result_dict, is_error).

    Generation tools kick off a background job and await its completion,
    forwarding tool_progress events over `ws`. All other tools are purely
    synchronous DB operations.
    """
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
    if name == "add_keyframe":
        result = _exec_add_keyframe(project_dir, input_data)
        return result, "error" in result
    if name == "add_audio_track":
        result = _exec_add_audio_track(project_dir, input_data)
        return result, "error" in result
    if name == "add_audio_clip":
        result = _exec_add_audio_clip(project_dir, input_data)
        return result, "error" in result
    if name == "update_volume_curve":
        result = _exec_update_volume_curve(project_dir, input_data)
    if name == "generate_dsp":
        result = _exec_generate_dsp(project_dir, input_data)
        return result, "error" in result
    if name == "generate_descriptions":
        result = _exec_generate_descriptions(project_dir, input_data)
        return result, "error" in result
    if name == "update_keyframe":
        result = _exec_update_keyframe(project_dir, input_data)
        return result, "error" in result
    if name == "update_transition":
        result = _exec_update_transition(project_dir, input_data)
        return result, "error" in result
    if name == "split_transition":
        result = _exec_split_transition(project_dir, input_data)
        return result, "error" in result
    if name == "assign_keyframe_image":
        result = _exec_assign_keyframe_image(project_dir, input_data)
        return result, "error" in result
    if name == "assign_pool_video":
        result = _exec_assign_pool_video(project_dir, input_data)
        return result, "error" in result
    if name == "checkpoint":
        result = _exec_checkpoint(project_dir, input_data)
        return result, "error" in result
    if name == "list_checkpoints":
        result = _exec_list_checkpoints(project_dir, input_data)
        return result, "error" in result
    if name == "restore_checkpoint":
        result = _exec_restore_checkpoint(project_dir, input_data)
        return result, "error" in result
    if name == "generate_keyframe_candidates":
        from scenecraft.chat_generation import start_keyframe_generation
        kickoff = start_keyframe_generation(
            project_dir,
            project_name or "",
            input_data.get("keyframe_id", ""),
            int(input_data.get("count") or 3),
            input_data.get("prompt_override"),
        )
        if "error" in kickoff:
            return kickoff, True
        if ws is None or tool_use_id is None:
            return {"error": "generation tools require ws context (internal error)"}, True
        return await _await_generation_job(ws, tool_use_id, project_name or "", kickoff["job_id"])
    if name == "generate_transition_candidates":
        from scenecraft.chat_generation import start_transition_generation
        slot = input_data.get("slot")
        kickoff = start_transition_generation(
            project_dir,
            project_name or "",
            input_data.get("transition_id", ""),
            int(input_data.get("count") or 2),
            slot_index=(int(slot) if slot is not None else None),
        )
        if "error" in kickoff:
            return kickoff, True
        if ws is None or tool_use_id is None:
            return {"error": "generation tools require ws context (internal error)"}, True
        return await _await_generation_job(ws, tool_use_id, project_name or "", kickoff["job_id"])
    if name == "isolate_vocals__run":
        entity_type = input_data.get("entity_type", "audio_clip")
        entity_id = input_data.get("entity_id", "")
        if not entity_id:
            return {"error": "missing entity_id"}, True
        if entity_type not in ("audio_clip", "transition"):
            return {"error": f"unsupported entity_type: {entity_type}"}, True
        from scenecraft.plugin_host import PluginHost

        op = PluginHost.get_operation("isolate_vocals.run")
        if op is None:
            return {"error": "isolate_vocals plugin not registered"}, True
        kickoff = op.handler(
            entity_type,
            entity_id,
            {
                "project_dir": project_dir,
                "project_name": project_name or "",
                "range_mode": input_data.get("range_mode", "full"),
                "trim_in": input_data.get("trim_in"),
                "trim_out": input_data.get("trim_out"),
            },
        )
        if "error" in kickoff:
            return kickoff, True
        if ws is None or tool_use_id is None:
            return {"error": "isolate_vocals requires ws context (internal error)"}, True
        return await _await_generation_job(ws, tool_use_id, project_name or "", kickoff["job_id"])
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

    # Best-effort connect to OAuth-backed MCP services. Fire-and-forget so a
    # missing / expired / unreachable remember token (or any other MCP
    # provider hiccup) cannot block the chat's ws loop from starting. The
    # background task's success just makes those tools available on the
    # NEXT _stream_response call; until then bridge.all_tools() returns []
    # and the built-in tool set is used.
    bridge = MCPBridge()

    async def _bg_connect_service(service: str):
        try:
            await bridge.connect(service, user_id=user_id)
        except Exception as exc:
            _log(f"bridge.connect({service}) raised: {exc}")

    asyncio.create_task(_bg_connect_service("remember"))

    # Tracks the in-flight streaming task so a new user message can halt it
    # mid-generation. _stream_response persists its partial content on
    # asyncio.CancelledError before re-raising, so the cancellation here is
    # safe to wait on without losing data.
    current_stream: asyncio.Task | None = None

    # Single-reader ws pattern: this loop is the only consumer of incoming
    # frames. Elicitation responses from the user are routed to the waiting
    # stream task through futures in this dict (keyed by elicitation id) so
    # we don't need concurrent `ws.recv()` calls.
    elicitation_waiters: dict[str, asyncio.Future] = {}

    async def _halt_current_stream() -> None:
        nonlocal current_stream
        t = current_stream
        if t is None or t.done():
            return
        t.cancel()
        try:
            await t
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            _log(f"halted stream raised: {exc}")

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

                # Halt any in-flight generation and flush its partial content
                # to the DB before accepting the next user message. Rapid-fire
                # sends fall through this path every time.
                await _halt_current_stream()

                user_msg = _add_message(project_dir, user_id, "user", content, images)
                await ws.send(json.dumps({"type": "message", "message": user_msg}))
                # Stream in a task so subsequent frames (including the next
                # "message") can be read by this loop while generation runs.
                current_stream = asyncio.create_task(
                    _stream_response(
                        ws, project_dir, project_name, user_id, bridge,
                        elicitation_waiters,
                    )
                )

            elif msg_type == "elicitation_response":
                # Resolve the waiting stream task's future with the user's
                # accept/decline decision. Ignore responses that no longer
                # have a matching waiter (stream was cancelled, timed out,
                # etc. — safe to drop).
                elic_id = data.get("id")
                fut = elicitation_waiters.pop(elic_id, None) if elic_id else None
                if fut is not None and not fut.done():
                    fut.set_result(data.get("action", "decline"))

            elif msg_type == "stop":
                # Explicit client-initiated halt (e.g. a Stop button) — same
                # persistence semantics as a new-message interruption.
                await _halt_current_stream()

            elif msg_type == "ping":
                await ws.send(json.dumps({"type": "pong"}))

    except Exception as e:
        _log(f"Chat error: {e}")
    finally:
        # Socket is going away — halt any in-flight stream so its partial
        # text is persisted before we tear down the bridge.
        await _halt_current_stream()
        try:
            await bridge.close()
        except Exception as e:
            _log(f"bridge.close raised: {e}")
        _log(f"Chat disconnected: project={project_name} user={user_id}")


async def _stream_response(
    ws: ServerConnection,
    project_dir: Path,
    project_name: str,
    user_id: str,
    bridge,
    elicitation_waiters: dict[str, asyncio.Future],
):
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
    # Rolling buffer of text deltas for the current streaming turn. Reset at
    # the start of each outer iteration and cleared again after `final`
    # materializes (at which point the authoritative text lives in
    # final.content). On asyncio.CancelledError (user sent a new message
    # mid-generation) this buffer is flushed into all_blocks before the
    # partial is persisted.
    streamed_text_this_turn = ""

    try:
        for _ in range(10):  # cap at 10 tool iterations per user message
            streamed_text_this_turn = ""
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
                            streamed_text_this_turn += delta.text
                            await ws.send(json.dumps({"type": "chunk", "content": delta.text}))

                final = await stream.get_final_message()
                # final.content is the authoritative source of truth for this
                # turn's text; clear the running buffer so we don't double-append
                # on a subsequent cancellation in a later iteration.
                streamed_text_this_turn = ""

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
                    action = await _recv_elicitation_response(elicitation_waiters, elic_id)
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
                    result, is_error = await _execute_tool(
                        project_dir,
                        tu["name"],
                        tu["input"],
                        ws=ws,
                        tool_use_id=tu["id"],
                        project_name=project_name,
                    )
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

    except asyncio.CancelledError:
        # User sent a new message mid-generation. Persist whatever the
        # assistant streamed so far (as an interrupted turn) and propagate
        # the cancellation so the caller can start the next stream.
        if streamed_text_this_turn:
            all_blocks.append({"type": "text", "text": streamed_text_this_turn})
        if all_blocks or tool_calls_log:
            has_non_text = any(b.get("type") != "text" for b in all_blocks)
            if has_non_text:
                persisted_content = json.dumps(all_blocks)
            else:
                persisted_content = "".join(
                    b.get("text", "") for b in all_blocks if b.get("type") == "text"
                )
            try:
                assistant_msg = _add_message(
                    project_dir,
                    user_id,
                    "assistant",
                    persisted_content,
                    tool_calls=tool_calls_log or None,
                )
                if has_non_text:
                    assistant_msg["content"] = all_blocks
                if tool_calls_log:
                    assistant_msg["tool_calls"] = tool_calls_log
                assistant_msg["interrupted"] = True
                try:
                    await ws.send(json.dumps(
                        {"type": "message", "message": assistant_msg},
                        default=str,
                    ))
                except Exception:
                    pass
            except Exception as exc:
                _log(f"Failed to persist partial assistant message: {exc}")
        try:
            await ws.send(json.dumps({"type": "halted", "reason": "interrupted_by_user"}))
        except Exception:
            pass
        try:
            await ws.send(json.dumps({"type": "complete"}))
        except Exception:
            pass
        # Re-raise so the caller knows we stopped intentionally; the outer
        # handler uses this to drive the "cancel current stream, start new"
        # transition without treating it as an error.
        raise

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
