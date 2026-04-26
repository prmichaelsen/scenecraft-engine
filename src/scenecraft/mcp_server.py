"""Stdio MCP server that exposes scenecraft plugin tools to external Claude.

Run via:
  uv --directory <scenecraft-engine> run python -m scenecraft.mcp_server

Add to ``~/.claude.json``:
  {
    "mcpServers": {
      "scenecraft": {
        "command": "uv",
        "args": ["--directory", "/path/to/scenecraft-engine", "run",
                 "python", "-m", "scenecraft.mcp_server"],
        "env": {
          "SCENECRAFT_REMOTE_BROADCAST_URL": "http://127.0.0.1:8765"
        }
      }
    }
  }

Architecture
------------
This satellite process runs OUTSIDE the engine. It registers the same
plugins (so ``PluginHost.list_mcp_tools()`` returns the full set) and
delegates each tool call to ``MCPToolDef.handler(args, context)`` —
matching how ``chat.py`` dispatches plugin tools.

Project context
~~~~~~~~~~~~~~~
The engine's chat dispatcher gets ``project_name`` for free from the WS
URL path. We don't have that boundary here, so every tool's input
schema is augmented with a required ``project`` field at list-tools
time. Pre-flight: a ``scenecraft__list_projects`` tool enumerates the
work_dir so the LLM can discover what exists rather than guess.

Broadcasts
~~~~~~~~~~
Plugin handlers internally call ``plugin_api.broadcast_event``. In this
process that function notices ``SCENECRAFT_REMOTE_BROADCAST_URL`` is
set and POSTs the event to the engine's ``/api/_internal/broadcast``
endpoint, which then runs ``job_manager._broadcast`` server-side. Frontend
WS clients get the push without any panel refresh.

If the engine is offline, the POST fails silently (matches the pre-IPC
"never fail the enclosing mutation" contract). DB writes still land;
the panel's 2s poll picks up changes within 2s on next reload.
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

from scenecraft.config import resolve_work_dir
from scenecraft.plugin_host import PluginHost

# Mirror api_server's plugin imports + register order so list_mcp_tools()
# returns the same surface the engine exposes to the embedded chat.
from scenecraft.plugins import isolate_vocals  # noqa: E402
from scenecraft.plugins import transcribe  # noqa: E402
from scenecraft.plugins import generate_music  # noqa: E402
from scenecraft.plugins import light_show  # noqa: E402

PluginHost.register(isolate_vocals)
PluginHost.register(transcribe)
PluginHost.register(generate_music)
PluginHost.register(light_show)

_WORK_DIR = resolve_work_dir()
if _WORK_DIR is None:
    print(
        "scenecraft work_dir not configured. "
        "Run 'scenecraft server' once to set it up, or set SCENECRAFT_WORK_DIR.",
        file=sys.stderr,
    )
    sys.exit(1)

server: Server = Server("scenecraft")


def _list_projects() -> list[str]:
    """Project = any subdir of work_dir that contains a project.db."""
    return sorted(
        d.name
        for d in _WORK_DIR.iterdir()
        if d.is_dir() and (d / "project.db").exists()
    )


def _project_dir(name: str) -> Path:
    p = _WORK_DIR / name
    if not (p / "project.db").exists():
        raise ValueError(f"unknown project: {name!r}")
    return p


@server.list_tools()
async def list_tools() -> list[Tool]:
    tools: list[Tool] = [
        Tool(
            name="scenecraft__list_projects",
            description=(
                "Enumerate available scenecraft projects in the configured "
                "work directory. Call this first to discover project names "
                "before invoking any project-scoped tool."
            ),
            inputSchema={"type": "object", "properties": {}, "additionalProperties": False},
        )
    ]
    for t in PluginHost.list_mcp_tools():
        # Deep-copy the input schema so we don't mutate the registered
        # MCPToolDef. Inject a required `project` field on top.
        schema = json.loads(json.dumps(t.input_schema or {"type": "object"}))
        props = schema.setdefault("properties", {})
        props["project"] = {
            "type": "string",
            "description": "Scenecraft project name (required for all project-scoped tools).",
        }
        required = list(set([*(schema.get("required") or []), "project"]))
        schema["required"] = sorted(required)
        tools.append(
            Tool(
                name=t.full_name,
                description=t.description,
                inputSchema=schema,
            )
        )
    return tools


@server.call_tool()
async def call_tool(name: str, args: dict) -> list[TextContent]:
    if name == "scenecraft__list_projects":
        return [
            TextContent(
                type="text",
                text=json.dumps({"projects": _list_projects()}),
            )
        ]

    project = (args or {}).pop("project", None)
    if not isinstance(project, str) or not project:
        raise ValueError(
            "`project` is required on every project-scoped tool — "
            "call scenecraft__list_projects first if you don't know "
            "the available names."
        )

    tool = PluginHost.get_mcp_tool(name)
    if tool is None:
        raise ValueError(f"unknown tool: {name!r}")

    try:
        pd = _project_dir(project)
    except ValueError as e:
        return [TextContent(type="text", text=json.dumps({"error": str(e)}))]

    # Match chat.py's tool_context shape exactly. ws / tool_use_id are None
    # because this satellite has no WS connection — broadcasts route through
    # SCENECRAFT_REMOTE_BROADCAST_URL → engine's /api/_internal/broadcast
    # instead.
    ctx = {
        "project_dir": pd,
        "project_name": project,
        "ws": None,
        "tool_use_id": None,
    }
    try:
        result = tool.handler(args or {}, ctx)
    except Exception as exc:  # noqa: BLE001
        result = {"error": f"{type(exc).__name__}: {exc}"}
    if not isinstance(result, dict):
        result = {"error": f"non-dict result from {name!r}: {type(result).__name__}"}
    return [TextContent(type="text", text=json.dumps(result, default=str))]


async def main() -> None:
    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
