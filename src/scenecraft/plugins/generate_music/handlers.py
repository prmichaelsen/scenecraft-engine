"""Chat-tool + operation handlers for generate_music.

Resolved from plugin.yaml via ``backend:<attr_name>`` refs. The module is
kept thin — each handler unpacks the standard (args, context) or
(entity_type, entity_id, context) shape, calls into ``generate_music.run``
or ``client.musicful_get_key_info``, and shapes the return for the
caller (chat vs. operation dispatcher).

Auth (task-126) stays deferred; context['auth'] is populated with empty
strings by the middleware-less dev-mode path, which ``run()`` already
handles.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from scenecraft.plugins.generate_music import generate_music as _gm
from scenecraft.plugins.generate_music.client import musicful_get_key_info
from scenecraft import plugin_api as _pa


def handle_generate_music(args: dict, context: dict) -> dict:
    """Chat-tool handler for ``generate_music__run``.

    Elicitation was handled upstream by the destructive-pattern gate in
    chat.py; if this function runs, the user accepted. Missing API key
    surfaces as a plain error dict (ServiceConfigError path in ``run()``).

    Chat tool invocation carries no editor selection — ``entity_type``
    and ``entity_id`` are always null. A future selection-context bridge
    (chat request → editor state → tool args) is out of M16 scope.
    """
    project_dir: Path = context["project_dir"]
    project_name: str = context.get("project_name") or ""
    auth = context.get("auth") or {}

    result = _gm.run(
        project_dir,
        project_name,
        action=args.get("action", "auto"),
        style=args.get("style", ""),
        lyrics=args.get("lyrics"),
        title=args.get("title"),
        instrumental=int(args.get("instrumental", 1)),
        gender=args.get("gender"),
        model=args.get("model", "MFV2.0"),
        entity_type=None,
        entity_id=None,
        auth_context=auth,
    )
    if "error" in result:
        return {"error": result["error"]}
    return {
        "generation_id": result["generation_id"],
        "task_ids": result["task_ids"],
        "status": "running",
    }


def handle_get_credits(args: dict, context: dict) -> dict:  # noqa: ARG001 — args unused
    """Chat-tool handler for ``generate_music__credits``.

    Read-only; no elicitation gate. Missing API key is an admin-facing
    error, not a tool failure — the shape is
    ``{credits: None, error: <message>}`` so the assistant can summarize
    it instead of taking the error-result branch.
    """
    if not os.environ.get("MUSICFUL_API_KEY"):
        return {
            "credits": None,
            "error": "This plugin requires a Musicful API key. Please contact your administrator.",
        }
    try:
        info = musicful_get_key_info()
    except _pa.ServiceError as e:
        return {"credits": None, "error": f"Musicful returned HTTP {e.status}"}
    except Exception as e:  # noqa: BLE001
        return {"credits": None, "error": str(e)}
    credits_raw = info.get("key_music_counts")
    try:
        credits = int(credits_raw) if credits_raw is not None else 0
    except (TypeError, ValueError):
        credits = 0
    return {
        "credits": credits,
        "last_checked_at": datetime.now(timezone.utc).isoformat(),
    }


def run_operation(entity_type: str, entity_id: str, context: dict) -> dict:
    """Operation handler for ``generate_music.run``.

    Triggered by the context-menu path from the frontend (task-131).
    Bridges the operation signature ``(entity_type, entity_id, context)``
    into a ``generate_music.run()`` call that knows which entity to bind.

    The context dict is expected to carry operation inputs
    (``style``, ``action``, etc.) the same way other plugins do — any
    missing fields pass through as the function's defaults, matching the
    chat-tool branch above for consistency.
    """
    project_dir: Path = context["project_dir"]
    project_name: str = context.get("project_name") or ""
    auth = context.get("auth") or {}
    opts: dict[str, Any] = context.get("operation_args") or {}

    if entity_type not in ("audio_clip", "transition"):
        return {"error": f"unsupported entity_type: {entity_type}"}
    if not entity_id:
        return {"error": "missing entity_id"}

    return _gm.run(
        project_dir,
        project_name,
        action=opts.get("action", "auto"),
        style=opts.get("style", ""),
        lyrics=opts.get("lyrics"),
        title=opts.get("title"),
        instrumental=int(opts.get("instrumental", 1)),
        gender=opts.get("gender"),
        model=opts.get("model", "MFV2.0"),
        entity_type=entity_type,
        entity_id=entity_id,
        auth_context=auth,
    )
