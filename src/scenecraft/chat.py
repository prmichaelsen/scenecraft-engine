"""Chat assistant — Claude-powered WebSocket chat with streaming and tool calling."""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import time
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

You have access to a read-only SQL tool (`sql_query`) that executes SELECT statements
against the project's SQLite database. Use it for ad-hoc analysis, counts, filters, and
any question you cannot answer from the summary above. Default row limit is 100; pass a
higher `limit` if you need more rows.

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

TOOLS: list[dict] = [SQL_QUERY_TOOL]


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


def _execute_tool(project_dir: Path, name: str, input_data: dict) -> tuple[dict, bool]:
    """Execute a tool. Returns (result_dict, is_error)."""
    if name == "sql_query":
        sql = (input_data or {}).get("sql", "")
        limit = (input_data or {}).get("limit", 100)
        if not sql or not isinstance(sql, str):
            return {"error": "missing sql"}, True
        result = _execute_readonly_sql(project_dir, sql, limit)
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
