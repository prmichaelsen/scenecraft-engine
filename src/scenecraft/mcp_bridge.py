"""Bridge between the chat loop and external MCP servers.

For each OAuth-connected service (e.g. "remember") we open a long-lived MCP
client session over SSE, authenticated with a Bearer token from the OAuth
token store. Tools exposed by that server are discovered once on connect and
merged into Claude's tool list; tool calls from Claude are routed back to the
appropriate session.

Tool-name prefixes are used to route:
  - Tools from service "remember" stay as-is (they already start with
    `remember_` by convention of the remember-mcp server).
  - Tools from other services are prefixed `<service>_` if they don't already
    start with that prefix, to avoid collisions with built-in tools.

The bridge is scoped per-chat-connection: on disconnect the sessions are
torn down.
"""

from __future__ import annotations

import asyncio
import sys
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


def _log(msg: str):
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] [mcp] {msg}", file=sys.stderr, flush=True)


@dataclass
class MCPSession:
    service: str
    session: Any  # mcp.ClientSession — imported lazily
    tools: list[dict] = field(default_factory=list)
    exit_stack: AsyncExitStack | None = None


class MCPBridge:
    """Collection of live MCP client sessions for a single chat connection.

    Usage:
        bridge = MCPBridge()
        await bridge.connect_remember(user_id="alice")   # best-effort
        tools = bridge.all_tools()                       # merge into Claude tool list
        result, is_error = await bridge.call_tool("remember_search_memory", {...})
        await bridge.close()
    """

    def __init__(self):
        self._sessions: dict[str, MCPSession] = {}
        # tool_name → service — populated as each session connects
        self._tool_routing: dict[str, str] = {}

    # ── Connection ──────────────────────────────────────────────────

    async def connect(self, service: str, user_id: str) -> bool:
        """Connect to an OAuth-backed MCP service. Returns True on success.

        Best-effort: errors (no tokens, network failure, etc.) are logged and
        return False so the chat still works without this service's tools.
        """
        if service in self._sessions:
            return True

        from scenecraft.oauth_client import SERVICES, get_valid_access_token

        svc_cfg = SERVICES.get(service)
        if svc_cfg is None:
            _log(f"connect({service}): unknown service")
            return False

        access_token = get_valid_access_token(user_id, service)
        if not access_token:
            _log(f"connect({service}): no valid token for user={user_id}")
            return False

        url = svc_cfg["mcp_url"]

        # Lazy import — mcp SDK is optional.
        try:
            from mcp import ClientSession
            from mcp.client.sse import sse_client
        except ImportError:
            _log(f"connect({service}): mcp SDK not installed (pip install 'scenecraft-engine[ai]')")
            return False

        stack = AsyncExitStack()
        try:
            headers = {"Authorization": f"Bearer {access_token}"}
            read, write = await stack.enter_async_context(sse_client(url, headers=headers))
            session = await stack.enter_async_context(ClientSession(read, write))
            await asyncio.wait_for(session.initialize(), timeout=15)

            list_result = await asyncio.wait_for(session.list_tools(), timeout=15)
            raw_tools = getattr(list_result, "tools", []) or []

            claude_tools: list[dict] = []
            for t in raw_tools:
                name = t.name
                # Remember tools already start with "remember_"; prefix others for routing clarity
                routed_name = name if name.startswith(f"{service}_") or service == "remember" else f"{service}_{name}"
                claude_tools.append({
                    "name": routed_name,
                    "description": t.description or "",
                    "input_schema": t.inputSchema or {"type": "object", "properties": {}},
                })
                self._tool_routing[routed_name] = service

            self._sessions[service] = MCPSession(
                service=service,
                session=session,
                tools=claude_tools,
                exit_stack=stack,
            )
            _log(f"connect({service}): {len(claude_tools)} tools discovered")
            return True
        except Exception as e:
            _log(f"connect({service}): failed — {type(e).__name__}: {e}")
            try:
                await stack.aclose()
            except Exception:
                pass
            return False

    async def close(self):
        """Tear down all sessions."""
        for sess in list(self._sessions.values()):
            if sess.exit_stack is not None:
                try:
                    await sess.exit_stack.aclose()
                except Exception as e:
                    _log(f"close({sess.service}): {e}")
        self._sessions.clear()
        self._tool_routing.clear()

    # ── Query ───────────────────────────────────────────────────────

    def all_tools(self) -> list[dict]:
        out: list[dict] = []
        for sess in self._sessions.values():
            out.extend(sess.tools)
        return out

    def has_tool(self, name: str) -> bool:
        return name in self._tool_routing

    # ── Execute ─────────────────────────────────────────────────────

    async def call_tool(self, name: str, arguments: dict) -> tuple[dict, bool]:
        """Dispatch a tool call to the right MCP session. Returns (output, is_error)."""
        service = self._tool_routing.get(name)
        if service is None:
            return {"error": f"unknown MCP tool: {name}"}, True
        sess = self._sessions.get(service)
        if sess is None:
            return {"error": f"no live session for service: {service}"}, True

        # If the name was prefixed for routing, strip the prefix before sending
        upstream_name = name
        if service != "remember" and name.startswith(f"{service}_"):
            upstream_name = name[len(service) + 1:]

        try:
            result = await asyncio.wait_for(
                sess.session.call_tool(upstream_name, arguments=arguments or {}),
                timeout=60,
            )
        except asyncio.TimeoutError:
            return {"error": f"MCP tool {name} timed out"}, True
        except Exception as e:
            return {"error": f"MCP tool {name} failed: {type(e).__name__}: {e}"}, True

        is_error = bool(getattr(result, "isError", False))
        content = getattr(result, "content", []) or []
        # Flatten content: prefer .text, fall back to a dict of the item
        flat: list[Any] = []
        for item in content:
            text = getattr(item, "text", None)
            if text is not None:
                flat.append(text)
            else:
                flat.append(_item_to_dict(item))

        if len(flat) == 1 and isinstance(flat[0], str):
            output: Any = flat[0]
        else:
            output = flat

        return ({"output": output} if not is_error else {"error": output}), is_error


def _item_to_dict(item: Any) -> dict:
    """Best-effort conversion of an MCP content item to a JSON-serializable dict."""
    if hasattr(item, "model_dump"):
        try:
            return item.model_dump()
        except Exception:
            pass
    if hasattr(item, "__dict__"):
        return {k: v for k, v in vars(item).items() if not k.startswith("_")}
    return {"value": str(item)}
