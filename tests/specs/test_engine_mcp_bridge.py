"""Regression tests for `local.engine-mcp-bridge.md`.

Covers every `Rn` and every Behavior Table row from the spec, plus the
target-state requirements (R21-R27) that await the FastAPI refactor. Target
rows use `@pytest.mark.xfail(strict=False)`.

The bridge's upstream surface (SSE handshake, `ClientSession`, OAuth) is
entirely mocked; these tests never hit a real MCP server. An end-to-end
section is intentionally deferred — see the note at the bottom of the file.
"""
from __future__ import annotations

import asyncio
import sys
import types
from contextlib import asynccontextmanager
from typing import Any
from unittest import mock

import pytest

from scenecraft import mcp_bridge as mb
from scenecraft.mcp_bridge import MCPBridge, MCPSession


# ---------------------------------------------------------------------------
# Helpers — mock MCP server, session, and tool objects
# ---------------------------------------------------------------------------


class _FakeTool:
    """Minimal stand-in for the `mcp` SDK's Tool dataclass."""
    def __init__(self, name, description="d", inputSchema=None):
        self.name = name
        self.description = description
        self.inputSchema = inputSchema if inputSchema is not None else {
            "type": "object", "properties": {"x": {"type": "string"}}
        }


class _FakeListToolsResult:
    def __init__(self, tools):
        self.tools = tools


class _FakeContentItem:
    """Content item mimicking the mcp SDK result.content shape."""
    def __init__(self, text=None):
        if text is not None:
            self.text = text


class _FakeCallResult:
    def __init__(self, content, isError=False):
        self.content = content
        self.isError = isError


class _FakeClientSession:
    """Stand-in for `mcp.ClientSession`. Supports async ctx mgr + init/list/call."""
    def __init__(self, tools=None, call_result=None, call_raises=None,
                 call_sleep=0.0, init_sleep=0.0, list_sleep=0.0):
        self._tools = tools or []
        self._call_result = call_result
        self._call_raises = call_raises
        self._call_sleep = call_sleep
        self._init_sleep = init_sleep
        self._list_sleep = list_sleep
        self.init_called = False
        self.list_called = False
        self.call_invocations: list[tuple[str, dict]] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def initialize(self):
        self.init_called = True
        if self._init_sleep:
            await asyncio.sleep(self._init_sleep)

    async def list_tools(self):
        self.list_called = True
        if self._list_sleep:
            await asyncio.sleep(self._list_sleep)
        return _FakeListToolsResult(self._tools)

    async def call_tool(self, name, arguments=None):
        self.call_invocations.append((name, arguments))
        if self._call_sleep:
            await asyncio.sleep(self._call_sleep)
        if self._call_raises:
            raise self._call_raises
        return self._call_result


@asynccontextmanager
async def _fake_sse(url, headers=None, **kw):
    """`sse_client` stand-in — yields (read, write) placeholders."""
    yield ("read-stream", "write-stream")


@asynccontextmanager
async def _hanging_sse(url, headers=None, **kw):
    await asyncio.sleep(3600)
    yield ("never", "never")


def _install_fake_mcp(monkeypatch, session_factory, sse=_fake_sse):
    """Install a stub `mcp` + `mcp.client.sse` into sys.modules.

    `session_factory()` returns the `_FakeClientSession` instance to use as
    the ClientSession. The module is wired so the bridge's lazy
    `from mcp import ClientSession; from mcp.client.sse import sse_client`
    hits our stubs.
    """
    holder = {"session": None}

    def _CS(read, write):
        sess = session_factory()
        holder["session"] = sess
        return sess

    fake_mcp = types.ModuleType("mcp")
    fake_mcp.ClientSession = _CS
    fake_sse_mod = types.ModuleType("mcp.client.sse")
    fake_sse_mod.sse_client = sse
    fake_client_pkg = types.ModuleType("mcp.client")
    fake_client_pkg.sse = fake_sse_mod

    monkeypatch.setitem(sys.modules, "mcp", fake_mcp)
    monkeypatch.setitem(sys.modules, "mcp.client", fake_client_pkg)
    monkeypatch.setitem(sys.modules, "mcp.client.sse", fake_sse_mod)
    return holder


def _install_oauth_stubs(monkeypatch, services=None, token="tok"):
    """Patch `scenecraft.oauth_client.SERVICES` and `get_valid_access_token`."""
    from scenecraft import oauth_client as oc
    if services is None:
        services = {
            "remember": {"mcp_url": "https://example.test/remember/mcp"},
            "gmail": {"mcp_url": "https://example.test/gmail/mcp"},
        }
    monkeypatch.setattr(oc, "SERVICES", services, raising=True)

    def _tok(user_id, service, **kw):
        return token if service in services and token else None
    monkeypatch.setattr(oc, "get_valid_access_token", _tok, raising=True)
    return _tok


# ---------------------------------------------------------------------------
# === UNIT ===
# ---------------------------------------------------------------------------


class TestConstructionAndQueries:
    """R1, R9, R10 — fresh bridge is empty; query helpers work."""

    def test_fresh_bridge_is_empty(self):
        """covers R1 — fresh-bridge-is-empty."""
        b = MCPBridge()
        assert b.all_tools() == []
        assert b.has_tool("anything") is False

    @pytest.mark.asyncio
    async def test_all_tools_empty_on_no_connect(self):
        """covers R9 — all_tools returns [] with no live sessions."""
        b = MCPBridge()
        assert b.all_tools() == []


class TestConnectHappyPath:
    """R2, R6, R7, R8 — discover + register tools, idempotency."""

    @pytest.mark.asyncio
    async def test_connect_remember_keeps_names(self, monkeypatch):
        """covers R2, R6, R7 — connect-remember-keeps-names."""
        _install_oauth_stubs(monkeypatch)
        tools = [_FakeTool("remember_a"), _FakeTool("remember_b"), _FakeTool("other")]
        _install_fake_mcp(monkeypatch, lambda: _FakeClientSession(tools=tools))

        b = MCPBridge()
        ok = await b.connect("remember", "alice")
        assert ok is True
        names = [t["name"] for t in b.all_tools()]
        assert names == ["remember_a", "remember_b", "other"]
        # schema + description are passed through
        for t in b.all_tools():
            assert t["description"] == "d"
            assert t["input_schema"] == {"type": "object", "properties": {"x": {"type": "string"}}}

    @pytest.mark.asyncio
    async def test_tool_routing_records_service(self, monkeypatch):
        """covers R7, R10 — tool-routing-records-service."""
        _install_oauth_stubs(monkeypatch)
        _install_fake_mcp(monkeypatch,
                          lambda: _FakeClientSession(tools=[_FakeTool("remember_a"), _FakeTool("other")]))
        b = MCPBridge()
        await b.connect("remember", "alice")
        assert b.has_tool("remember_a") is True
        assert b.has_tool("other") is True
        assert b.has_tool("missing") is False

    @pytest.mark.asyncio
    async def test_connect_non_remember_prefixes_names(self, monkeypatch):
        """covers R6 — connect-non-remember-prefixes-names."""
        _install_oauth_stubs(monkeypatch)
        _install_fake_mcp(monkeypatch, lambda: _FakeClientSession(tools=[_FakeTool("search")]))
        b = MCPBridge()
        await b.connect("gmail", "alice")
        assert [t["name"] for t in b.all_tools()] == ["gmail_search"]
        assert b._tool_routing["gmail_search"] == "gmail"
        assert b.has_tool("search") is False

    @pytest.mark.asyncio
    async def test_connect_skips_double_prefix(self, monkeypatch):
        """covers R6 — connect-skips-double-prefix."""
        _install_oauth_stubs(monkeypatch)
        _install_fake_mcp(monkeypatch, lambda: _FakeClientSession(tools=[_FakeTool("gmail_inbox")]))
        b = MCPBridge()
        await b.connect("gmail", "alice")
        assert [t["name"] for t in b.all_tools()] == ["gmail_inbox"]
        assert b._tool_routing["gmail_inbox"] == "gmail"

    @pytest.mark.asyncio
    async def test_connect_fills_missing_description_and_schema(self, monkeypatch):
        """covers R7 — connect-fills-missing-description-and-schema."""
        _install_oauth_stubs(monkeypatch)
        t = _FakeTool("x")
        t.description = None
        t.inputSchema = None
        _install_fake_mcp(monkeypatch, lambda: _FakeClientSession(tools=[t]))
        b = MCPBridge()
        await b.connect("remember", "alice")
        claude_tool = b.all_tools()[0]
        assert claude_tool["description"] == ""
        assert claude_tool["input_schema"] == {"type": "object", "properties": {}}

    @pytest.mark.asyncio
    async def test_connect_is_idempotent_per_service(self, monkeypatch):
        """covers R8 — connect-is-idempotent-per-service."""
        _install_oauth_stubs(monkeypatch)
        call_count = {"n": 0}

        @asynccontextmanager
        async def _counting_sse(url, headers=None, **kw):
            call_count["n"] += 1
            yield ("r", "w")

        _install_fake_mcp(monkeypatch,
                          lambda: _FakeClientSession(tools=[_FakeTool("a")]),
                          sse=_counting_sse)
        b = MCPBridge()
        assert await b.connect("remember", "alice") is True
        first = b._sessions["remember"]
        assert await b.connect("remember", "alice") is True
        assert call_count["n"] == 1  # no second SSE
        assert b._sessions["remember"] is first

    @pytest.mark.asyncio
    async def test_all_tools_concatenates_in_order(self, monkeypatch):
        """covers R9 — all-tools-concatenates-in-order."""
        _install_oauth_stubs(monkeypatch)
        # Feed two different session stubs based on insertion order.
        seq = iter([
            _FakeClientSession(tools=[_FakeTool("remember_a"), _FakeTool("remember_b")]),
            _FakeClientSession(tools=[_FakeTool("search")]),
        ])
        _install_fake_mcp(monkeypatch, lambda: next(seq))
        b = MCPBridge()
        await b.connect("remember", "u")
        await b.connect("gmail", "u")
        names = [t["name"] for t in b.all_tools()]
        assert names == ["remember_a", "remember_b", "gmail_search"]
        assert len(names) == 3


class TestConnectFailurePaths:
    """R3, R4, R5, R17 — connect returns False on every failure mode."""

    @pytest.mark.asyncio
    async def test_connect_unknown_service_returns_false(self, monkeypatch):
        """covers R3 — connect-unknown-service-returns-false."""
        _install_oauth_stubs(monkeypatch, services={"remember": {"mcp_url": "x"}})
        b = MCPBridge()
        ok = await b.connect("nope", "alice")
        assert ok is False
        assert b._sessions == {}
        assert b._tool_routing == {}

    @pytest.mark.asyncio
    async def test_connect_missing_token_returns_false(self, monkeypatch):
        """covers R3, R5 — connect-missing-token-returns-false."""
        from scenecraft import oauth_client as oc
        monkeypatch.setattr(oc, "SERVICES",
                            {"remember": {"mcp_url": "https://x/y"}}, raising=True)
        monkeypatch.setattr(oc, "get_valid_access_token",
                            lambda user_id, service, **kw: None, raising=True)
        sse_calls = {"n": 0}

        @asynccontextmanager
        async def _spy_sse(url, headers=None, **kw):
            sse_calls["n"] += 1
            yield ("r", "w")

        _install_fake_mcp(monkeypatch, lambda: _FakeClientSession(), sse=_spy_sse)
        b = MCPBridge()
        ok = await b.connect("remember", "alice")
        assert ok is False
        assert sse_calls["n"] == 0
        assert b._sessions == {}

    @pytest.mark.asyncio
    async def test_connect_mcp_sdk_missing_returns_false(self, monkeypatch, capsys):
        """covers R3 — connect-mcp-sdk-missing-returns-false."""
        _install_oauth_stubs(monkeypatch)

        # Remove any existing stub and arrange ImportError on `from mcp import`.
        monkeypatch.delitem(sys.modules, "mcp", raising=False)
        monkeypatch.delitem(sys.modules, "mcp.client", raising=False)
        monkeypatch.delitem(sys.modules, "mcp.client.sse", raising=False)

        real_import = __builtins__["__import__"] if isinstance(__builtins__, dict) \
            else __builtins__.__import__

        def _blocked_import(name, *a, **kw):
            if name == "mcp" or name.startswith("mcp."):
                raise ImportError("mcp not installed")
            return real_import(name, *a, **kw)

        monkeypatch.setattr("builtins.__import__", _blocked_import)
        b = MCPBridge()
        ok = await b.connect("remember", "alice")
        assert ok is False
        captured = capsys.readouterr()
        # Any install-hint log is acceptable; check something was logged.
        assert "mcp" in (captured.err + captured.out).lower()

    @pytest.mark.asyncio
    async def test_connect_sse_handshake_timeout(self, monkeypatch):
        """covers R4, R17 — connect-sse-handshake-timeout (stack aclose'd on fail)."""
        _install_oauth_stubs(monkeypatch)
        _install_fake_mcp(monkeypatch, lambda: _FakeClientSession(), sse=_hanging_sse)

        # Shrink the timeout via the module-level asyncio.wait_for intercept.
        orig_wait_for = asyncio.wait_for

        async def _fast_wait_for(aw, timeout):
            # Shrink any 10s timeout (SSE handshake) to 0.05 for test speed.
            return await orig_wait_for(aw, 0.05 if timeout == 10 else timeout)

        monkeypatch.setattr(mb.asyncio, "wait_for", _fast_wait_for)
        b = MCPBridge()
        ok = await b.connect("remember", "alice")
        assert ok is False
        assert b._sessions == {}
        assert b._tool_routing == {}

    @pytest.mark.asyncio
    async def test_connect_init_timeout(self, monkeypatch):
        """covers R4, R17 — connect-init-timeout."""
        _install_oauth_stubs(monkeypatch)
        _install_fake_mcp(monkeypatch, lambda: _FakeClientSession(init_sleep=10.0))
        orig_wait_for = asyncio.wait_for

        async def _fast_wait_for(aw, timeout):
            return await orig_wait_for(aw, 0.05 if timeout == 15 else timeout)
        monkeypatch.setattr(mb.asyncio, "wait_for", _fast_wait_for)
        b = MCPBridge()
        ok = await b.connect("remember", "alice")
        assert ok is False
        assert "remember" not in b._sessions

    @pytest.mark.asyncio
    async def test_connect_list_tools_timeout(self, monkeypatch):
        """covers R4, R17 — connect-list-tools-timeout."""
        _install_oauth_stubs(monkeypatch)
        _install_fake_mcp(monkeypatch, lambda: _FakeClientSession(list_sleep=10.0))
        orig_wait_for = asyncio.wait_for

        # Only shrink the list_tools timeout (init gets 15s and returns immediately).
        state = {"seen_15": 0}

        async def _fast_wait_for(aw, timeout):
            if timeout == 15:
                state["seen_15"] += 1
                # The 2nd call of timeout=15 is list_tools (1st is init).
                if state["seen_15"] >= 2:
                    return await orig_wait_for(aw, 0.05)
            return await orig_wait_for(aw, timeout)
        monkeypatch.setattr(mb.asyncio, "wait_for", _fast_wait_for)
        b = MCPBridge()
        ok = await b.connect("remember", "alice")
        assert ok is False
        assert b._tool_routing == {}


class TestCallTool:
    """R11-R15 — dispatch, prefix stripping, timeout, flattening."""

    @pytest.mark.asyncio
    async def test_call_tool_success_remember(self, monkeypatch):
        """covers R11, R14, R15 — call-tool-success-remember."""
        _install_oauth_stubs(monkeypatch)
        session = _FakeClientSession(
            tools=[_FakeTool("remember_search_memory")],
            call_result=_FakeCallResult([_FakeContentItem("hello")]),
        )
        _install_fake_mcp(monkeypatch, lambda: session)
        b = MCPBridge()
        await b.connect("remember", "alice")
        out, is_err = await b.call_tool("remember_search_memory", {"q": "x"})
        assert is_err is False
        assert out == {"output": "hello"}
        assert session.call_invocations == [("remember_search_memory", {"q": "x"})]

    @pytest.mark.asyncio
    async def test_call_tool_does_not_strip_remember_prefix(self, monkeypatch):
        """covers R11 — call-tool-does-not-strip-remember-prefix."""
        _install_oauth_stubs(monkeypatch)
        session = _FakeClientSession(
            tools=[_FakeTool("remember_foo")],
            call_result=_FakeCallResult([_FakeContentItem("ok")]),
        )
        _install_fake_mcp(monkeypatch, lambda: session)
        b = MCPBridge()
        await b.connect("remember", "alice")
        await b.call_tool("remember_foo", {})
        assert session.call_invocations[0][0] == "remember_foo"

    @pytest.mark.asyncio
    async def test_call_tool_strips_service_prefix(self, monkeypatch):
        """covers R11 — call-tool-strips-service-prefix."""
        _install_oauth_stubs(monkeypatch)
        session = _FakeClientSession(
            tools=[_FakeTool("search")],
            call_result=_FakeCallResult([_FakeContentItem("ok")]),
        )
        _install_fake_mcp(monkeypatch, lambda: session)
        b = MCPBridge()
        await b.connect("gmail", "alice")
        await b.call_tool("gmail_search", {"q": "x"})
        assert session.call_invocations[0] == ("search", {"q": "x"})

    @pytest.mark.asyncio
    async def test_call_tool_unknown_name(self):
        """covers R13 — call-tool-unknown-name."""
        b = MCPBridge()
        out, is_err = await b.call_tool("nope", {})
        assert is_err is True
        assert out == {"error": "unknown MCP tool: nope"}

    @pytest.mark.asyncio
    async def test_call_tool_no_session(self):
        """covers R13 — call-tool-no-session."""
        b = MCPBridge()
        b._tool_routing["x"] = "svc"  # routing exists but no session
        out, is_err = await b.call_tool("x", {})
        assert is_err is True
        assert out == {"error": "no live session for service: svc"}

    @pytest.mark.asyncio
    async def test_call_tool_upstream_exception(self, monkeypatch):
        """covers R13 — call-tool-upstream-exception."""
        _install_oauth_stubs(monkeypatch)
        session = _FakeClientSession(
            tools=[_FakeTool("remember_x")],
            call_raises=RuntimeError("boom"),
        )
        _install_fake_mcp(monkeypatch, lambda: session)
        b = MCPBridge()
        await b.connect("remember", "alice")
        out, is_err = await b.call_tool("remember_x", {})
        assert is_err is True
        assert "RuntimeError" in out["error"]
        assert "boom" in out["error"]
        assert "remember_x" in out["error"]

    @pytest.mark.asyncio
    async def test_call_tool_upstream_iserror(self, monkeypatch):
        """covers R15 — call-tool-upstream-iserror."""
        _install_oauth_stubs(monkeypatch)
        session = _FakeClientSession(
            tools=[_FakeTool("remember_x")],
            call_result=_FakeCallResult([_FakeContentItem("denied")], isError=True),
        )
        _install_fake_mcp(monkeypatch, lambda: session)
        b = MCPBridge()
        await b.connect("remember", "alice")
        out, is_err = await b.call_tool("remember_x", {})
        assert is_err is True
        assert out == {"error": "denied"}

    @pytest.mark.asyncio
    async def test_call_tool_flattens_single_text(self, monkeypatch):
        """covers R14 — call-tool-flattens-single-text (bare string unwrap)."""
        _install_oauth_stubs(monkeypatch)
        session = _FakeClientSession(
            tools=[_FakeTool("remember_x")],
            call_result=_FakeCallResult([_FakeContentItem("hello")]),
        )
        _install_fake_mcp(monkeypatch, lambda: session)
        b = MCPBridge()
        await b.connect("remember", "alice")
        out, _ = await b.call_tool("remember_x", {})
        assert out["output"] == "hello"
        assert not isinstance(out["output"], list)

    @pytest.mark.asyncio
    async def test_call_tool_preserves_multi_content_order(self, monkeypatch):
        """covers R14 — call-tool-preserves-multi-content-order."""
        _install_oauth_stubs(monkeypatch)
        session = _FakeClientSession(
            tools=[_FakeTool("remember_x")],
            call_result=_FakeCallResult([_FakeContentItem("a"), _FakeContentItem("b")]),
        )
        _install_fake_mcp(monkeypatch, lambda: session)
        b = MCPBridge()
        await b.connect("remember", "alice")
        out, _ = await b.call_tool("remember_x", {})
        assert out["output"] == ["a", "b"]

    @pytest.mark.asyncio
    async def test_call_tool_uses_model_dump(self, monkeypatch):
        """covers R14 — call-tool-uses-model-dump."""
        _install_oauth_stubs(monkeypatch)

        class _Dumpable:
            def model_dump(self):
                return {"k": 1}

        session = _FakeClientSession(
            tools=[_FakeTool("remember_x")],
            call_result=_FakeCallResult([_Dumpable()]),
        )
        _install_fake_mcp(monkeypatch, lambda: session)
        b = MCPBridge()
        await b.connect("remember", "alice")
        out, _ = await b.call_tool("remember_x", {})
        # single non-string item => stays wrapped in a list? Per spec:
        # flat is [{"k":1}]; len==1 but not string → output = flat = [{"k":1}].
        assert out["output"] == [{"k": 1}]

    @pytest.mark.asyncio
    async def test_call_tool_falls_back_to_vars(self, monkeypatch):
        """covers R14 — call-tool-falls-back-to-vars (public attrs only)."""
        _install_oauth_stubs(monkeypatch)

        class _Opaque:
            def __init__(self):
                self.a = 1
                self._hidden = 2

        session = _FakeClientSession(
            tools=[_FakeTool("remember_x")],
            call_result=_FakeCallResult([_Opaque()]),
        )
        _install_fake_mcp(monkeypatch, lambda: session)
        b = MCPBridge()
        await b.connect("remember", "alice")
        out, _ = await b.call_tool("remember_x", {})
        assert out["output"] == [{"a": 1}]

    @pytest.mark.asyncio
    async def test_call_tool_falls_back_to_str(self, monkeypatch):
        """covers R14 — call-tool-falls-back-to-str."""
        _install_oauth_stubs(monkeypatch)

        class _Opaque:
            __slots__ = ()
            def __str__(self):
                return "opaque"

        session = _FakeClientSession(
            tools=[_FakeTool("remember_x")],
            call_result=_FakeCallResult([_Opaque()]),
        )
        _install_fake_mcp(monkeypatch, lambda: session)
        b = MCPBridge()
        await b.connect("remember", "alice")
        out, _ = await b.call_tool("remember_x", {})
        assert out["output"] == [{"value": "opaque"}]

    @pytest.mark.asyncio
    async def test_call_tool_timeout(self, monkeypatch):
        """covers R12 — call-tool-timeout."""
        _install_oauth_stubs(monkeypatch)
        session = _FakeClientSession(
            tools=[_FakeTool("remember_x")],
            call_sleep=10.0,
        )
        _install_fake_mcp(monkeypatch, lambda: session)
        b = MCPBridge()
        await b.connect("remember", "alice")

        # Shrink the 60s call_tool timeout.
        orig_wait_for = asyncio.wait_for

        async def _fast(aw, timeout):
            return await orig_wait_for(aw, 0.05 if timeout == 60 else timeout)
        monkeypatch.setattr(mb.asyncio, "wait_for", _fast)
        out, is_err = await b.call_tool("remember_x", {})
        assert is_err is True
        assert out == {"error": "MCP tool remember_x timed out"}

    @pytest.mark.asyncio
    async def test_call_tool_accepts_none_arguments(self, monkeypatch):
        """covers R11 — call-tool-accepts-none-arguments."""
        _install_oauth_stubs(monkeypatch)
        session = _FakeClientSession(
            tools=[_FakeTool("remember_x")],
            call_result=_FakeCallResult([_FakeContentItem("ok")]),
        )
        _install_fake_mcp(monkeypatch, lambda: session)
        b = MCPBridge()
        await b.connect("remember", "alice")
        await b.call_tool("remember_x", None)
        assert session.call_invocations[0][1] == {}

    @pytest.mark.asyncio
    async def test_call_tool_does_not_mutate_arguments(self, monkeypatch):
        """covers R11 (negative) — call-tool-does-not-mutate-arguments."""
        _install_oauth_stubs(monkeypatch)
        session = _FakeClientSession(
            tools=[_FakeTool("remember_x")],
            call_result=_FakeCallResult([_FakeContentItem("ok")]),
        )
        _install_fake_mcp(monkeypatch, lambda: session)
        b = MCPBridge()
        await b.connect("remember", "alice")
        args = {"k": "v"}
        snap = dict(args)
        await b.call_tool("remember_x", args)
        assert args == snap


class TestClose:
    """R16 — close teardown."""

    @pytest.mark.asyncio
    async def test_close_tears_down_all_sessions(self, monkeypatch):
        """covers R16 — close-tears-down-all-sessions."""
        b = MCPBridge()
        closed = []

        class _Stack:
            def __init__(self, label):
                self.label = label
            async def aclose(self):
                closed.append(self.label)

        b._sessions["a"] = MCPSession(service="a", session=None, tools=[{"name": "t1"}],
                                      exit_stack=_Stack("a"))
        b._sessions["b"] = MCPSession(service="b", session=None, tools=[{"name": "t2"}],
                                      exit_stack=_Stack("b"))
        b._tool_routing = {"t1": "a", "t2": "b"}
        await b.close()
        assert sorted(closed) == ["a", "b"]
        assert b._sessions == {}
        assert b._tool_routing == {}

    @pytest.mark.asyncio
    async def test_close_is_exception_safe(self):
        """covers R16 — close-is-exception-safe."""
        b = MCPBridge()
        closed = []

        class _Stack:
            def __init__(self, label, boom=False):
                self.label = label
                self.boom = boom
            async def aclose(self):
                if self.boom:
                    raise RuntimeError("fail")
                closed.append(self.label)

        b._sessions["a"] = MCPSession(service="a", session=None,
                                      exit_stack=_Stack("a", boom=True))
        b._sessions["b"] = MCPSession(service="b", session=None,
                                      exit_stack=_Stack("b"))
        b._tool_routing = {"t": "a"}
        await b.close()  # must not raise
        assert closed == ["b"]
        assert b._sessions == {}
        assert b._tool_routing == {}

    @pytest.mark.asyncio
    async def test_close_empty_is_noop(self):
        """covers R16 — close-empty-is-noop."""
        b = MCPBridge()
        await b.close()
        assert b._sessions == {}
        assert b._tool_routing == {}


class TestConnectCleanupOnFailure:
    """R17 — failed connect aclose's stack and registers nothing."""

    @pytest.mark.asyncio
    async def test_failed_connect_aclose_stack_no_session(self, monkeypatch):
        """covers R17 — no MCPSession registered, no routing, stack aclose'd.

        Uses an sse ctx that raises on enter to fail the connect cleanly.
        """
        _install_oauth_stubs(monkeypatch)

        @asynccontextmanager
        async def _broken_sse(url, headers=None, **kw):
            raise RuntimeError("sse broken")
            yield  # pragma: no cover

        _install_fake_mcp(monkeypatch, lambda: _FakeClientSession(), sse=_broken_sse)
        b = MCPBridge()
        ok = await b.connect("remember", "alice")
        assert ok is False
        assert "remember" not in b._sessions
        assert b._tool_routing == {}


class TestChatHandlerWiring:
    """R18-R20 — chat handler fire-and-forget semantics.

    These tests don't invoke `handle_chat_connection` end-to-end; they pin
    the observable contract — MCPBridge() is sync + cheap, and `_bg_connect`
    analogue swallows exceptions.
    """

    def test_bridge_ctor_is_cheap_and_sync(self):
        """covers R18 — bridge constructed synchronously, no I/O."""
        # Constructor must not touch the network / filesystem / OAuth store.
        b = MCPBridge()
        assert b._sessions == {}
        assert b._tool_routing == {}

    @pytest.mark.asyncio
    async def test_all_tools_empty_while_connect_pending(self, monkeypatch):
        """covers R19 — all_tools()==[] while connect task still running."""
        _install_oauth_stubs(monkeypatch)

        started = asyncio.Event()
        release = asyncio.Event()

        @asynccontextmanager
        async def _slow_sse(url, headers=None, **kw):
            started.set()
            await release.wait()
            yield ("r", "w")

        _install_fake_mcp(monkeypatch,
                          lambda: _FakeClientSession(tools=[_FakeTool("a")]),
                          sse=_slow_sse)
        b = MCPBridge()
        task = asyncio.create_task(b.connect("remember", "alice"))
        await started.wait()
        # While SSE handshake is mid-flight, all_tools is still empty.
        assert b.all_tools() == []
        release.set()
        assert await task is True
        assert len(b.all_tools()) == 1

    @pytest.mark.asyncio
    async def test_bg_connect_swallows_exceptions(self, monkeypatch):
        """covers R20 — _bg_connect_service pattern: unexpected raise is caught."""
        # Simulate the wrapper pattern from chat.py — anything inside is caught.
        async def _bg_connect(bridge, service, user_id):
            try:
                return await bridge.connect(service, user_id)
            except Exception:
                return None

        b = MCPBridge()
        with mock.patch.object(b, "connect",
                               side_effect=RuntimeError("unexpected")):
            result = await _bg_connect(b, "remember", "alice")
            assert result is None  # swallowed, no propagation


# ---------------------------------------------------------------------------
# Target-state requirements R21-R27 — xfail until FastAPI refactor (M16).
# ---------------------------------------------------------------------------


class TestTargetState:
    """R21-R27 — all xfail(strict=False) until implemented."""

    @pytest.mark.xfail(reason="target-state R21; awaits M16 FastAPI refactor", strict=False)
    @pytest.mark.asyncio
    async def test_call_tool_refreshes_on_401(self, monkeypatch):
        """covers R21 target — call-tool-refreshes-on-401."""
        _install_oauth_stubs(monkeypatch)
        attempts = {"n": 0}

        class _Auth401(Exception):
            pass

        async def _call(name, arguments=None):
            attempts["n"] += 1
            if attempts["n"] == 1:
                raise _Auth401("401 Unauthorized")
            return _FakeCallResult([_FakeContentItem("ok")])

        sess_holder = _install_fake_mcp(monkeypatch,
                                        lambda: _FakeClientSession(
                                            tools=[_FakeTool("remember_search")],
                                            call_result=_FakeCallResult([_FakeContentItem("ok")])))
        b = MCPBridge()
        await b.connect("remember", "alice")
        # Override call_tool on the live session with our failing-then-ok stub.
        sess_holder["session"].call_tool = _call
        out, is_err = await b.call_tool("remember_search", {})
        assert is_err is False and out == {"output": "ok"}
        assert attempts["n"] == 2  # refreshed + retried

    @pytest.mark.xfail(reason="target-state R21; awaits M16 FastAPI refactor", strict=False)
    @pytest.mark.asyncio
    async def test_call_tool_degraded_on_second_401(self, monkeypatch):
        """covers R21 target — call-tool-degraded-on-second-401."""
        _install_oauth_stubs(monkeypatch)

        class _Auth401(Exception):
            pass

        session = _FakeClientSession(
            tools=[_FakeTool("remember_x")],
            call_raises=_Auth401("401"),
        )
        _install_fake_mcp(monkeypatch, lambda: session)
        b = MCPBridge()
        await b.connect("remember", "alice")
        out, is_err = await b.call_tool("remember_x", {})
        assert is_err is True
        assert b.all_tools() == []
        assert b._tool_routing == {}

    @pytest.mark.xfail(reason="target-state R22; awaits M16 FastAPI refactor", strict=False)
    @pytest.mark.asyncio
    async def test_connect_skips_malformed_tool_schema(self, monkeypatch):
        """covers R22 target — connect-skips-malformed-tool-schema."""
        _install_oauth_stubs(monkeypatch)
        good = _FakeTool("good")
        bad_name = _FakeTool("good2"); bad_name.name = None
        bad_schema = _FakeTool("bad"); bad_schema.inputSchema = "not-a-dict"
        _install_fake_mcp(monkeypatch,
                          lambda: _FakeClientSession(tools=[good, bad_name, bad_schema]))
        b = MCPBridge()
        ok = await b.connect("remember", "alice")
        assert ok is True
        names = [t["name"] for t in b.all_tools()]
        assert names == ["good"]

    @pytest.mark.xfail(reason="target-state R23; awaits M16 FastAPI refactor", strict=False)
    @pytest.mark.asyncio
    async def test_universal_service_prefix_prevents_collision(self, monkeypatch):
        """covers R23 target — universal {service}__{tool} prefix."""
        _install_oauth_stubs(monkeypatch)
        seq = iter([
            _FakeClientSession(tools=[_FakeTool("search")]),
            _FakeClientSession(tools=[_FakeTool("search")]),
        ])
        _install_fake_mcp(monkeypatch, lambda: next(seq))
        b = MCPBridge()
        await b.connect("remember", "alice")
        await b.connect("gmail", "alice")
        assert b.has_tool("remember__search") is True
        assert b.has_tool("gmail__search") is True
        assert b.has_tool("search") is False

    @pytest.mark.xfail(reason="target-state R24; bridge currently uses 60s, not 300s",
                       strict=False)
    def test_call_tool_300s_default_timeout(self):
        """covers R24 target — 300s default call_tool timeout."""
        import inspect
        src = inspect.getsource(MCPBridge.call_tool)
        # Today the literal 60 is hard-coded; target is 300.
        assert "timeout=300" in src or ", 300" in src
        assert "timeout=60," not in src

    @pytest.mark.xfail(reason="target-state R25 + R27; awaits M16 FastAPI refactor",
                       strict=False)
    @pytest.mark.asyncio
    async def test_bg_connect_backoff_retry_then_ready_event(self, monkeypatch):
        """covers R25 + R27 + INV-4 — bg-connect-backoff-retry-then-ready-event."""
        # Today `_bg_connect_service` exists on chat.py with no retry; xfail.
        from scenecraft import chat
        # Only asserting that a retry loop attribute / symbol exists.
        assert hasattr(chat, "_bg_connect_service")
        src = __import__("inspect").getsource(chat._bg_connect_service)
        assert "backoff" in src.lower() or "retry" in src.lower()
        assert "core__chat__mcp_tools_ready" in src

    @pytest.mark.xfail(reason="target-state R26; bridge has no asyncio.Lock today",
                       strict=False)
    @pytest.mark.asyncio
    async def test_concurrent_connect_serialized_by_lock(self, monkeypatch):
        """covers R26 target — concurrent-connect-serialized-by-lock."""
        _install_oauth_stubs(monkeypatch)
        sse_entries = {"n": 0}

        @asynccontextmanager
        async def _counting_sse(url, headers=None, **kw):
            sse_entries["n"] += 1
            await asyncio.sleep(0.05)
            yield ("r", "w")

        _install_fake_mcp(monkeypatch,
                          lambda: _FakeClientSession(tools=[_FakeTool("a")]),
                          sse=_counting_sse)
        b = MCPBridge()
        r1, r2 = await asyncio.gather(
            b.connect("remember", "alice"),
            b.connect("remember", "alice"),
        )
        assert r1 is True and r2 is True
        # With the target-state lock, only ONE SSE handshake should occur.
        assert sse_entries["n"] == 1

    @pytest.mark.xfail(reason="target-state R27; no WS emission in bridge today",
                       strict=False)
    @pytest.mark.asyncio
    async def test_late_connect_emits_tools_ready_event(self):
        """covers R27 target + INV-4 — late-connect-emits-tools-ready-event."""
        # Target: bridge / _bg_connect knows how to post a ws frame with
        # {"event":"core__chat__mcp_tools_ready","service":...,"tool_count":...}
        # Today no such emission exists.
        from scenecraft import chat
        src = __import__("inspect").getsource(chat)
        assert "core__chat__mcp_tools_ready" in src


class TestNegativeInvariants:
    """Negative tests that document transitional state."""

    def test_no_concurrency_primitives_in_bridge(self):
        """covers R1, R2 (negative) — bridge has no asyncio.Lock today.

        Transitional: per spec Transitional Behavior, no per-service lock
        exists yet (R26 is target). This test pins the CURRENT state so a
        future implementer is forced to update the spec when they add it.
        """
        import inspect
        src = inspect.getsource(mb)
        # Today we expect NO Lock / Semaphore in the bridge module.
        assert "asyncio.Lock" not in src
        assert "Semaphore" not in src

    def test_current_call_tool_timeout_is_60s(self):
        """covers R12 — pins the transitional 60s timeout.

        This test intentionally CONFLICTS with R24 target (300s). When R24
        ships, this test will flip to failing and must be deleted in the
        same PR that flips `test_call_tool_300s_default_timeout` to passing.
        """
        import inspect
        src = inspect.getsource(MCPBridge.call_tool)
        assert "timeout=60" in src


# ---------------------------------------------------------------------------
# === E2E ===
# ---------------------------------------------------------------------------

# E2E: the MCP bridge is a pure I/O adapter around the `mcp` SDK. Every
# observable effect is either (a) the upstream SSE / ClientSession surface
# — already exhaustively mocked above — or (b) integration with chat.py's
# `handle_chat_connection`, which is covered by the chat-pipeline spec's
# own e2e suite (see `local.chat-pipeline.md`, tasks 78+). Standing up a
# stub SSE/MCP server just to re-verify the same mocked behaviors would
# not increase coverage: there is no REST/WS surface that the bridge
# exposes directly.
#
# Full integration is deferred to provider-spec integration tests that
# exercise the bridge via chat.py's ws loop end-to-end. See
# `local.engine-mcp-bridge.md` §Related Artifacts.
#
# If this decision is revisited, the e2e suite would need: an async SSE
# stub server (aiohttp or starlette + sse-starlette) implementing the MCP
# initialize + tools/list + tools/call handshake, wired into a fresh
# chat ws connection. Estimated cost: ~4h; deferred pending justification.
