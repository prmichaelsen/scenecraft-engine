"""Regression tests for ``local.engine-chat-pipeline`` (M18 task-83).

Exercises the engine-side chat pipeline internals in ``scenecraft/chat.py``:
persistence (R1-R4), system prompt (R5-R7), tool catalog (R8-R12), destructive
classifier (R13-R15), dispatcher (R16-R21), elicitation (R22-R26), streaming
loop (R27-R32), MCP bridge integration (R33-R36), connection handler
(R37-R44), interrupt path (R45-R50), error path (R51-R54), generation polling
(R55-R57), plus target-state OQ resolutions (R58-R63).

Heavy mocking — never hits a real Claude API or MCP server. Target-state tests
use ``@pytest.mark.xfail(reason="target-state; awaits ...", strict=False)``.

Test prefixes are ``chat_`` per the conftest convention. Tests share fixtures
from ``tests/specs/conftest.py``: ``project_dir``, ``db_conn``,
``engine_server``, ``project_name``.
"""
from __future__ import annotations

import asyncio
import inspect
import json
import os
import re
import sys
import time
import types
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from unittest import mock

import pytest

from scenecraft import chat
from scenecraft import db as scdb


# ---------------------------------------------------------------------------
# Helpers — fakes for Claude streaming, ws, plugin host, MCP bridge
# ---------------------------------------------------------------------------


class FakeWS:
    """Captures all ``ws.send`` payloads as parsed dicts.

    Set ``send_raises`` to inject failures. Iteration yields raw frames
    populated via ``feed()`` for the read-loop tests.
    """

    def __init__(self, frames: list[str] | None = None, send_raises: Exception | None = None):
        self.sent: list[dict] = []
        self.sent_raw: list[str] = []
        self._frames = frames or []
        self._send_raises = send_raises
        self.closed = False

    async def send(self, raw: str):
        if self._send_raises is not None:
            raise self._send_raises
        self.sent_raw.append(raw)
        try:
            self.sent.append(json.loads(raw))
        except Exception:
            self.sent.append({"_unparsed": raw})

    def feed(self, frame: str):
        self._frames.append(frame)

    def __aiter__(self):
        return self._aiter()

    async def _aiter(self):
        for f in self._frames:
            yield f
            # Cooperative yield so pending tasks (eg. spawned streams) can run.
            await asyncio.sleep(0)
        self.closed = True

    def types(self) -> list[str]:
        return [m.get("type") for m in self.sent]


# --- Claude streaming fakes -------------------------------------------------


class _FakeBlock:
    def __init__(self, type, **kw):
        self.type = type
        for k, v in kw.items():
            setattr(self, k, v)


class _FakeEvent:
    def __init__(self, type, **kw):
        self.type = type
        for k, v in kw.items():
            setattr(self, k, v)


class _FakeFinal:
    def __init__(self, content, stop_reason="end_turn"):
        self.content = content
        self.stop_reason = stop_reason


class _FakeStreamCM:
    """Async context manager that mimics ``client.messages.stream(...)``."""

    def __init__(self, events, final):
        self._events = events
        self._final = final

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    def __aiter__(self):
        return self._aiter()

    async def _aiter(self):
        for e in self._events:
            yield e

    async def get_final_message(self):
        return self._final


class FakeMessages:
    """Drop-in for ``client.messages``. Drives the 10-iter loop deterministically."""

    def __init__(self, scripted: list[tuple[list, _FakeFinal]] | None = None):
        # Each entry is (events, final). Loop pops one per iter.
        self._scripted = list(scripted or [])
        self.calls: list[dict] = []

    def stream(self, **kw):
        self.calls.append(kw)
        if not self._scripted:
            # Default: empty turn that ends.
            return _FakeStreamCM([], _FakeFinal([], stop_reason="end_turn"))
        events, final = self._scripted.pop(0)
        return _FakeStreamCM(events, final)


class FakeAsyncAnthropic:
    last_kwargs: dict | None = None

    def __init__(self, api_key=None, scripted=None):
        self.api_key = api_key
        self.messages = FakeMessages(scripted=scripted)


def _install_fake_anthropic(monkeypatch, scripted=None, raise_on_stream=None):
    """Inject a fake ``anthropic`` module. Returns the FakeAsyncAnthropic class.

    If ``scripted`` is None we emit a single end_turn iteration with no content.
    """
    fake_anthropic = types.ModuleType("anthropic")

    class _APIError(Exception):
        message = ""

        def __init__(self, msg=""):
            super().__init__(msg)
            self.message = msg

    fake_anthropic.APIError = _APIError

    captured: dict[str, Any] = {"client": None}

    class _AsyncAnthropic(FakeAsyncAnthropic):
        def __init__(self, api_key=None):
            super().__init__(api_key=api_key, scripted=scripted)
            if raise_on_stream is not None:
                orig = self.messages.stream

                def _stream(**kw):
                    self.messages.calls.append(kw)
                    raise raise_on_stream

                self.messages.stream = _stream
            captured["client"] = self

    fake_anthropic.AsyncAnthropic = _AsyncAnthropic
    monkeypatch.setitem(sys.modules, "anthropic", fake_anthropic)
    return fake_anthropic, captured


# --- Bridge / PluginHost stubs ----------------------------------------------


class FakeBridge:
    def __init__(self, tools=None, has=None, call_result=None):
        self._tools = tools or []
        self._has = has or {}
        self._call_result = call_result or ({"ok": True}, False)
        self.calls: list[tuple[str, dict]] = []
        self.closed = False

    def all_tools(self):
        return list(self._tools)

    def has_tool(self, name):
        return bool(self._has.get(name, False))

    async def call_tool(self, name, args):
        self.calls.append((name, args))
        return self._call_result

    async def close(self):
        self.closed = True


# ---------------------------------------------------------------------------
# Persistence (R1-R4)
# ---------------------------------------------------------------------------


class TestPersistence:
    def test_chat_messages_table_schema(self, project_dir, db_conn):
        """covers R1 — chat_messages columns are load-bearing."""
        cols = {r[1]: r for r in db_conn.execute("PRAGMA table_info(chat_messages)").fetchall()}
        for required in ("id", "user_id", "role", "content", "images", "tool_calls", "created_at"):
            assert required in cols, f"missing-col: {required}"
        assert cols["images"]["notnull"] == 0 if isinstance(cols["images"], dict) else cols["images"][3] == 0
        assert cols["tool_calls"][3] == 0  # nullable

    def test_add_message_inserts_and_returns_dict(self, project_dir, db_conn):
        """covers R2 — _add_message inserts one row + returns documented dict."""
        out = chat._add_message(project_dir, "alice", "user", "hi", images=None, tool_calls=None)
        assert out["role"] == "user"
        assert out["user_id"] == "alice"
        assert out["content"] == "hi"
        assert out["images"] is None
        assert "id" in out and isinstance(out["id"], int)
        # ISO-8601 UTC.
        assert "T" in out["created_at"], "iso-format"
        # One row in DB.
        row_count = db_conn.execute("SELECT COUNT(*) FROM chat_messages").fetchone()[0]
        assert row_count == 1

    def test_add_message_decodes_json_list_content(self, project_dir):
        """covers R2 — list-shaped JSON content is decoded back in returned dict."""
        blocks = [{"type": "text", "text": "hello"}]
        out = chat._add_message(project_dir, "alice", "assistant", json.dumps(blocks))
        assert out["content"] == blocks

    def test_add_message_keeps_plain_string_content(self, project_dir):
        """covers R2 — plain string content not decoded."""
        out = chat._add_message(project_dir, "alice", "user", "just a string")
        assert out["content"] == "just a string"

    def test_get_messages_oldest_first_with_limit(self, project_dir):
        """covers R3, R4 — _get_messages returns up to limit, oldest-first per user."""
        for i in range(5):
            chat._add_message(project_dir, "alice", "user", f"m{i}")
        # Other user — should not leak.
        chat._add_message(project_dir, "bob", "user", "bob-msg")
        msgs = chat._get_messages(project_dir, "alice", limit=3)
        assert len(msgs) == 3
        # Oldest-first: last 3 of alice = m2,m3,m4.
        assert [m["content"] for m in msgs] == ["m2", "m3", "m4"]

    def test_get_messages_default_limit_is_50(self, project_dir):
        """covers R4 — default limit literal preserved."""
        sig = inspect.signature(chat._get_messages)
        assert sig.parameters["limit"].default == 50

    def test_get_messages_decodes_assistant_json_list_only(self, project_dir):
        """covers R3 — assistant JSON-list content decoded; user string left alone."""
        blocks = [{"type": "text", "text": "hi"}]
        chat._add_message(project_dir, "alice", "assistant", json.dumps(blocks))
        chat._add_message(project_dir, "alice", "user", '["not decoded"]')
        msgs = chat._get_messages(project_dir, "alice")
        # Order: assistant first (oldest), then user.
        assert msgs[0]["role"] == "assistant"
        assert msgs[0]["content"] == blocks
        assert msgs[1]["role"] == "user"
        assert msgs[1]["content"] == '["not decoded"]'

    def test_get_messages_assistant_non_list_json_stays_string(self, project_dir):
        """covers R3 — assistant content that's JSON but not a list stays string."""
        chat._add_message(project_dir, "alice", "assistant", '"just a quoted string"')
        msgs = chat._get_messages(project_dir, "alice")
        assert msgs[0]["content"] == '"just a quoted string"'

    def test_get_messages_attaches_images_and_tool_calls(self, project_dir):
        """covers R3 — images/tool_calls JSON parsed when non-null."""
        chat._add_message(project_dir, "alice", "user", "hi", images=["a.png"], tool_calls=None)
        chat._add_message(
            project_dir, "alice", "assistant", "ok",
            tool_calls=[{"id": "t1", "name": "x", "output": {"k": 1}}],
        )
        msgs = chat._get_messages(project_dir, "alice")
        user_msg = msgs[0]
        assert user_msg["images"] == ["a.png"]
        assistant_msg = msgs[1]
        assert assistant_msg["tool_calls"][0]["id"] == "t1"


# ---------------------------------------------------------------------------
# System prompt (R5-R7)
# ---------------------------------------------------------------------------


def _seed_meta(db_conn, fps=None, resolution=None, title=None):
    """Helper: project_dir creation only sets up empty meta — populate via INSERT."""
    if fps is not None:
        db_conn.execute("INSERT OR REPLACE INTO meta(key,value) VALUES (?,?)", ("fps", fps))
    if resolution is not None:
        db_conn.execute("INSERT OR REPLACE INTO meta(key,value) VALUES (?,?)", ("resolution", resolution))
    if title is not None:
        db_conn.execute("INSERT OR REPLACE INTO meta(key,value) VALUES (?,?)", ("title", title))
    db_conn.commit()


class TestSystemPrompt:
    def test_dynamic_per_call_recomputes(self, project_dir, db_conn):
        """covers R5 — _build_system_prompt re-reads counts on every call."""
        first = chat._build_system_prompt(project_dir, "proj")
        # Insert a keyframe; count should increase by exactly 1.
        # keyframes has many NOT NULL columns; supply minimum viable row.
        db_conn.execute(
            "INSERT INTO keyframes(id, timestamp, section, source, prompt, candidates, "
            "track_id, label, label_color, blend_mode, refinement_prompt, last_modified_by) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            ("kf1", "0:01", "intro", "user", "p", "[]", "t1", "", "#fff", "normal", "", "test"),
        )
        db_conn.commit()
        second = chat._build_system_prompt(project_dir, "proj")
        m1 = re.search(r"Keyframes:\s*(\d+)", first)
        m2 = re.search(r"Keyframes:\s*(\d+)", second)
        assert m1 and m2
        assert int(m2.group(1)) == int(m1.group(1)) + 1

    def test_default_meta_values(self, project_dir):
        """covers R6 — missing meta keys fall back to documented defaults."""
        out = chat._build_system_prompt(project_dir, "proj_x")
        assert "FPS: 24" in out
        assert "1920,1080" in out
        # Title defaults to project_name.
        assert "proj_x" in out

    def test_meta_overrides(self, project_dir, db_conn):
        """covers R6 — meta values override defaults."""
        _seed_meta(db_conn, fps="30", resolution="3840,2160", title="My Cool Project")
        out = chat._build_system_prompt(project_dir, "proj")
        assert "FPS: 30" in out
        assert "3840,2160" in out
        assert "My Cool Project" in out

    def test_excludes_soft_deleted_keyframes(self, project_dir, db_conn):
        """covers R7 — soft-deleted keyframes excluded from count."""
        db_conn.executemany(
            "INSERT INTO keyframes(id, timestamp, section, source, prompt, candidates, "
            "track_id, label, label_color, blend_mode, refinement_prompt, last_modified_by, "
            "deleted_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [
                ("kf1", "0:01", "intro", "user", "p", "[]", "t1", "", "#fff", "normal", "", "test", None),
                ("kf2", "0:02", "intro", "user", "p", "[]", "t1", "", "#fff", "normal", "", "test", None),
                ("kf3", "0:03", "intro", "user", "p", "[]", "t1", "", "#fff", "normal", "", "test", "2026-01-01T00:00:00Z"),
            ],
        )
        db_conn.commit()
        out = chat._build_system_prompt(project_dir, "proj")
        m = re.search(r"Keyframes:\s*(\d+)", out)
        assert m and int(m.group(1)) == 2


# ---------------------------------------------------------------------------
# Tool catalog (R8-R12)
# ---------------------------------------------------------------------------


class TestToolCatalog:
    def test_three_key_shape_for_every_builtin(self):
        """covers R8 — every TOOLS entry has exactly {name, description, input_schema}."""
        for t in chat.TOOLS:
            assert set(t.keys()) == {"name", "description", "input_schema"}, f"bad-keys: {t.get('name')}"

    def test_builtin_tool_count(self):
        """covers R2 (contract) / behavior table — TOOLS has at least the documented 34 built-ins.

        Generation tools may grow with new plugin-aware features; assert >= 34
        rather than exact to allow forward addition (regression bound on the
        floor, not the ceiling).
        """
        assert len(chat.TOOLS) >= 34, f"got {len(chat.TOOLS)} built-ins"

    def test_tool_names_unique(self):
        """Negative — no duplicate tool name in the built-in catalog."""
        names = [t["name"] for t in chat.TOOLS]
        assert len(names) == len(set(names))


# ---------------------------------------------------------------------------
# Destructive classifier (R13-R15)
# ---------------------------------------------------------------------------


class TestDestructiveClassifier:
    @pytest.mark.parametrize(
        "name,expected",
        [
            ("delete_keyframe", True),
            ("batch_delete_transitions", True),
            ("generate_keyframe_candidates", True),
            ("isolate_vocals", True),
            ("update_keyframe", False),
            ("sql_query", False),
            ("add_keyframe", False),
        ],
    )
    def test_pattern_substrings(self, name, expected):
        """covers R15 — substring patterns."""
        assert chat._is_destructive(name) is expected

    @pytest.mark.parametrize(
        "name",
        [
            "generate_dsp",
            "generate_descriptions",
            "analyze_master_bus",
            "bounce_audio",
        ],
    )
    def test_allowlist_wins_over_pattern(self, name):
        """covers R13 — allowlist short-circuits destructive substring match."""
        # generate_dsp / generate_descriptions DO match the "generate_" pattern.
        assert chat._is_destructive(name) is False

    def test_plugin_flag_wins_over_pattern(self, monkeypatch):
        """covers R14 — plugin-declared destructive bool overrides substring patterns."""
        from scenecraft import plugin_host as ph

        def _fake_get(name):
            class _T: destructive = False
            return _T() if name == "foo__delete_thing" else None

        monkeypatch.setattr(ph.PluginHost, "get_mcp_tool", classmethod(lambda cls, n: _fake_get(n)))
        # "delete" substring would normally match True; plugin's destructive=False wins.
        assert chat._is_destructive("foo__delete_thing") is False

    def test_plugin_flag_true_returns_true(self, monkeypatch):
        """covers R14 — plugin destructive=True returns True regardless of name."""
        from scenecraft import plugin_host as ph

        class _T: destructive = True

        monkeypatch.setattr(ph.PluginHost, "get_mcp_tool", classmethod(lambda cls, n: _T() if n == "foo__safe_seeming" else None))
        assert chat._is_destructive("foo__safe_seeming") is True

    def test_classifier_lookup_exception_swallowed(self, monkeypatch):
        """covers R14 — PluginHost lookup exceptions fall through, never propagate."""
        from scenecraft import plugin_host as ph

        def _boom(cls, name):
            raise RuntimeError("plugin host crash")

        monkeypatch.setattr(ph.PluginHost, "get_mcp_tool", classmethod(_boom))
        # No exception; falls through to substring match. "foo__delete" matches "delete".
        assert chat._is_destructive("foo__delete_thing") is True


# ---------------------------------------------------------------------------
# Dispatcher (R16-R21)
# ---------------------------------------------------------------------------


def _run(coro):
    """Helper: run an async function in a fresh event loop."""
    return asyncio.run(coro)


class TestDispatcher:
    def test_unknown_tool_returns_documented_error(self, project_dir):
        """covers R18 — unknown tool returns ({error: ...}, True)."""
        result, is_error = _run(chat._execute_tool(project_dir, "no_such_tool", {}))
        assert is_error is True
        assert "unknown tool: no_such_tool" in result["error"]

    def test_input_data_none_coerced_to_dict(self, project_dir, monkeypatch):
        """covers R21 — None input becomes {} before dispatch."""
        from scenecraft import plugin_host as ph
        captured = {}

        class _T:
            destructive = False

            def handler(self, args, ctx):
                captured["args"] = args
                captured["ctx"] = ctx
                return {"ok": True}

        monkeypatch.setattr(ph.PluginHost, "get_mcp_tool", classmethod(lambda cls, n: _T() if n == "p__t" else None))
        result, is_error = _run(chat._execute_tool(
            project_dir, "p__t", None, ws=None, tool_use_id="tu1", project_name="proj",
        ))
        assert captured["args"] == {}
        assert is_error is False

    def test_plugin_handler_receives_context_dict(self, project_dir, monkeypatch):
        """covers R20 — context dict has exactly the documented four keys."""
        from scenecraft import plugin_host as ph
        captured = {}

        class _T:
            destructive = False

            def handler(self, args, ctx):
                captured["ctx"] = ctx
                return {"ok": True}

        monkeypatch.setattr(ph.PluginHost, "get_mcp_tool", classmethod(lambda cls, n: _T() if n == "p__t" else None))
        ws = FakeWS()
        _run(chat._execute_tool(
            project_dir, "p__t", {}, ws=ws, tool_use_id="tu1", project_name="proj",
        ))
        ctx = captured["ctx"]
        assert set(ctx.keys()) == {"project_dir", "project_name", "ws", "tool_use_id"}
        assert ctx["project_dir"] == project_dir
        assert ctx["project_name"] == "proj"
        assert ctx["ws"] is ws
        assert ctx["tool_use_id"] == "tu1"

    def test_plugin_exception_wrapped(self, project_dir, monkeypatch):
        """covers R19 — plugin handler exception → ({error: TypeName: msg}, True)."""
        from scenecraft import plugin_host as ph

        class _T:
            destructive = False

            def handler(self, args, ctx):
                raise RuntimeError("boom")

        monkeypatch.setattr(ph.PluginHost, "get_mcp_tool", classmethod(lambda cls, n: _T() if n == "p__t" else None))
        result, is_error = _run(chat._execute_tool(project_dir, "p__t", {}))
        assert is_error is True
        assert result == {"error": "RuntimeError: boom"}

    def test_plugin_non_dict_return_wrapped(self, project_dir, monkeypatch):
        """covers R19 — non-dict plugin return → error dict mentioning non-dict."""
        from scenecraft import plugin_host as ph

        class _T:
            destructive = False

            def handler(self, args, ctx):
                return "ok"

        monkeypatch.setattr(ph.PluginHost, "get_mcp_tool", classmethod(lambda cls, n: _T() if n == "p__t" else None))
        result, is_error = _run(chat._execute_tool(project_dir, "p__t", {}))
        assert is_error is True
        assert "non-dict" in result["error"]

    def test_plugin_namespaced_with_no_match_falls_through(self, project_dir, monkeypatch):
        """covers R16 — plugin-namespaced miss falls through to built-in switch (unknown error)."""
        from scenecraft import plugin_host as ph
        monkeypatch.setattr(ph.PluginHost, "get_mcp_tool", classmethod(lambda cls, n: None))
        result, is_error = _run(chat._execute_tool(project_dir, "foo__missing_tool", {}))
        assert is_error is True
        assert "unknown tool: foo__missing_tool" in result["error"]

    def test_sql_query_dispatched_to_readonly_handler(self, project_dir, db_conn):
        """covers R16 — built-in if-chain routes sql_query through _execute_readonly_sql."""
        # Ensure schema is migrated so the query has something to read.
        result, is_error = _run(chat._execute_tool(project_dir, "sql_query", {"sql": "SELECT 1"}))
        # Any well-formed SELECT should return rows or columns; absent a real
        # row, just assert no error and no error key.
        assert "error" not in result, f"sql_query unexpectedly errored: {result}"

    def test_sql_query_missing_sql_errors(self, project_dir):
        """covers built-in branch error semantics — missing required arg returns error."""
        result, is_error = _run(chat._execute_tool(project_dir, "sql_query", {}))
        assert is_error is True
        assert "missing sql" in result["error"]


# ---------------------------------------------------------------------------
# Elicitation (R22-R26)
# ---------------------------------------------------------------------------


class TestElicitation:
    def test_timeout_returns_decline(self, monkeypatch):
        """covers R23 — timeout auto-declines."""
        # Patch wait_for to immediately raise TimeoutError.
        async def _go():
            waiters: dict = {}
            # Use a tiny timeout to keep the test fast; the contract is that
            # the literal default is 300, but we override here for speed.
            return await chat._recv_elicitation_response(waiters, "elic_1", timeout=0.01)

        result = _run(_go())
        assert result == "decline"

    def test_default_timeout_is_300(self):
        """covers R23 — literal default timeout=300."""
        sig = inspect.signature(chat._recv_elicitation_response)
        assert sig.parameters["timeout"].default == 300

    def test_finally_pops_waiter(self):
        """covers R25 — waiter dict entry popped after timeout."""
        async def _go():
            waiters: dict = {}
            await chat._recv_elicitation_response(waiters, "elic_X", timeout=0.01)
            return waiters
        waiters = _run(_go())
        assert "elic_X" not in waiters

    def test_accept_action_returned(self):
        """covers R26 — action="accept" returns "accept"."""
        async def _go():
            waiters: dict = {}
            task = asyncio.create_task(
                chat._recv_elicitation_response(waiters, "e1", timeout=2)
            )
            await asyncio.sleep(0)  # let task register the future
            assert "e1" in waiters
            waiters["e1"].set_result("accept")
            return await task
        assert _run(_go()) == "accept"

    def test_non_accept_action_normalized_to_decline(self):
        """covers R26 — anything other than literal "accept" → "decline"."""
        async def _go():
            waiters: dict = {}
            task = asyncio.create_task(
                chat._recv_elicitation_response(waiters, "e1", timeout=2)
            )
            await asyncio.sleep(0)
            waiters["e1"].set_result("reject_or_whatever")
            return await task
        assert _run(_go()) == "decline"

    def test_cancelled_error_reraised(self):
        """covers R24 — CancelledError propagates, never swallowed as decline."""
        async def _go():
            waiters: dict = {}
            task = asyncio.create_task(
                chat._recv_elicitation_response(waiters, "e1", timeout=2)
            )
            await asyncio.sleep(0)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            return waiters
        waiters = _run(_go())
        assert "e1" not in waiters, "finally must pop on cancel"

    def test_stream_response_does_not_call_ws_recv(self):
        """covers R22 negative — _stream_response source contains no ws.recv() calls."""
        src = inspect.getsource(chat._stream_response)
        assert "ws.recv(" not in src, "_stream_response must NOT call ws.recv directly"
        assert ".recv()" not in src, "no concurrent recv allowed"


# ---------------------------------------------------------------------------
# Streaming loop (R27-R32)
# ---------------------------------------------------------------------------


def _ensure_anthropic_key(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-fake")


class TestStreamingLoop:
    def test_no_api_key_early_return(self, project_dir, monkeypatch):
        """covers R51 — missing key emits error+complete, no Claude call."""
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        ws = FakeWS()
        _install_fake_anthropic(monkeypatch)
        async def _go():
            await chat._stream_response(
                ws, project_dir, "proj", "alice",
                bridge=FakeBridge(), elicitation_waiters={},
            )
        _run(_go())
        types_seen = ws.types()
        assert types_seen[0] == "error"
        assert "ANTHROPIC_API_KEY" in ws.sent[0]["error"]
        assert types_seen[-1] == "complete"

    def test_early_exit_on_end_turn(self, project_dir, monkeypatch):
        """covers R27 — stop_reason != tool_use exits after one stream call."""
        _ensure_anthropic_key(monkeypatch)
        scripted = [(
            [_FakeEvent("content_block_delta", delta=_FakeBlock("text_delta", text="hello"))],
            _FakeFinal([_FakeBlock("text", text="hello")], stop_reason="end_turn"),
        )]
        _, captured = _install_fake_anthropic(monkeypatch, scripted=scripted)
        ws = FakeWS()
        _run(chat._stream_response(ws, project_dir, "proj", "alice", FakeBridge(), {}))
        assert len(captured["client"].messages.calls) == 1
        # text chunk + assistant message + complete.
        types_seen = ws.types()
        assert "chunk" in types_seen
        assert types_seen[-1] == "complete"

    def test_text_delta_emits_chunk_with_content(self, project_dir, monkeypatch):
        """covers R29 — text_delta event emits {type:"chunk", content:<text>}."""
        _ensure_anthropic_key(monkeypatch)
        scripted = [(
            [
                _FakeEvent("content_block_delta", delta=_FakeBlock("text_delta", text="he")),
                _FakeEvent("content_block_delta", delta=_FakeBlock("text_delta", text="llo")),
            ],
            _FakeFinal([_FakeBlock("text", text="hello")], stop_reason="end_turn"),
        )]
        _install_fake_anthropic(monkeypatch, scripted=scripted)
        ws = FakeWS()
        _run(chat._stream_response(ws, project_dir, "proj", "alice", FakeBridge(), {}))
        chunks = [m for m in ws.sent if m.get("type") == "chunk"]
        assert [c["content"] for c in chunks] == ["he", "llo"]

    def test_tool_call_deduped_per_id(self, project_dir, monkeypatch):
        """covers R28 — duplicate content_block_start for same tool_use id only emits once."""
        _ensure_anthropic_key(monkeypatch)
        tu_block = _FakeBlock("tool_use", id="tu_1", name="sql_query", input={})
        scripted = [(
            [
                _FakeEvent("content_block_start", content_block=tu_block),
                _FakeEvent("content_block_start", content_block=tu_block),  # duplicate
            ],
            _FakeFinal([], stop_reason="end_turn"),
        )]
        _install_fake_anthropic(monkeypatch, scripted=scripted)
        ws = FakeWS()
        _run(chat._stream_response(ws, project_dir, "proj", "alice", FakeBridge(), {}))
        tool_calls = [m for m in ws.sent if m.get("type") == "tool_call"]
        assert len(tool_calls) == 1
        assert tool_calls[0]["toolCall"]["id"] == "tu_1"

    def test_ten_iteration_cap(self, project_dir, monkeypatch):
        """covers R27 — outer loop capped at 10 stream calls.

        We script 12 turns each ending with stop_reason='tool_use' so the loop
        would, absent the cap, iterate beyond 10. Built-in tool 'sql_query'
        with empty sql returns an error result; that doesn't change the cap.
        """
        _ensure_anthropic_key(monkeypatch)
        scripted = []
        for i in range(12):
            tu = _FakeBlock("tool_use", id=f"tu_{i}", name="sql_query", input={})
            scripted.append((
                [_FakeEvent("content_block_start", content_block=tu)],
                _FakeFinal([tu], stop_reason="tool_use"),
            ))
        _, captured = _install_fake_anthropic(monkeypatch, scripted=scripted)
        ws = FakeWS()
        _run(chat._stream_response(ws, project_dir, "proj", "alice", FakeBridge(), {}))
        assert len(captured["client"].messages.calls) == 10

    def test_model_and_max_tokens_literal(self, project_dir, monkeypatch):
        """covers R32 — model + max_tokens preserved literally on every stream call."""
        _ensure_anthropic_key(monkeypatch)
        _, captured = _install_fake_anthropic(monkeypatch, scripted=[
            ([], _FakeFinal([], stop_reason="end_turn")),
        ])
        ws = FakeWS()
        _run(chat._stream_response(ws, project_dir, "proj", "alice", FakeBridge(), {}))
        kw = captured["client"].messages.calls[0]
        assert kw["model"] == "claude-sonnet-4-20250514"
        assert kw["max_tokens"] == 4096

    def test_tools_passed_to_claude_three_key_shape(self, project_dir, monkeypatch):
        """covers R8, R10 — tools passed to messages.stream all have three keys."""
        _ensure_anthropic_key(monkeypatch)
        _, captured = _install_fake_anthropic(monkeypatch, scripted=[
            ([], _FakeFinal([], stop_reason="end_turn")),
        ])
        ws = FakeWS()
        _run(chat._stream_response(ws, project_dir, "proj", "alice", FakeBridge(), {}))
        tools = captured["client"].messages.calls[0]["tools"]
        for t in tools:
            assert set(t.keys()) == {"name", "description", "input_schema"}

    def test_catalog_merge_order_builtins_plugins_bridge(self, project_dir, monkeypatch):
        """covers R9, R11, R12 — order is TOOLS + plugin_contributed + bridge.all_tools."""
        _ensure_anthropic_key(monkeypatch)
        _, captured = _install_fake_anthropic(monkeypatch, scripted=[
            ([], _FakeFinal([], stop_reason="end_turn")),
        ])
        from scenecraft import plugin_host as ph

        plugin_def = types.SimpleNamespace(
            full_name="myplugin__my_tool",
            description="plugin tool",
            input_schema={"type": "object", "properties": {}},
        )
        monkeypatch.setattr(ph.PluginHost, "list_mcp_tools", classmethod(lambda cls: [plugin_def]))

        bridge_tool = {"name": "remember_search", "description": "br", "input_schema": {}}
        bridge = FakeBridge(tools=[bridge_tool])

        ws = FakeWS()
        _run(chat._stream_response(ws, project_dir, "proj", "alice", bridge, {}))
        tools = captured["client"].messages.calls[0]["tools"]
        n = len(chat.TOOLS)
        # Built-ins first.
        assert [t["name"] for t in tools[:n]] == [t["name"] for t in chat.TOOLS]
        # Then plugin entries (using full_name).
        assert tools[n]["name"] == "myplugin__my_tool"
        # Bridge entry last.
        assert tools[-1]["name"] == "remember_search"

    def test_assistant_message_persisted_after_loop(self, project_dir, monkeypatch):
        """covers R31 — assistant row persisted with concatenated text on natural end."""
        _ensure_anthropic_key(monkeypatch)
        scripted = [(
            [_FakeEvent("content_block_delta", delta=_FakeBlock("text_delta", text="bye"))],
            _FakeFinal([_FakeBlock("text", text="bye")], stop_reason="end_turn"),
        )]
        _install_fake_anthropic(monkeypatch, scripted=scripted)
        ws = FakeWS()
        _run(chat._stream_response(ws, project_dir, "proj", "alice", FakeBridge(), {}))
        # One assistant row in DB.
        msgs = chat._get_messages(project_dir, "alice")
        assistant_rows = [m for m in msgs if m["role"] == "assistant"]
        assert len(assistant_rows) == 1
        assert assistant_rows[0]["content"] == "bye"


# ---------------------------------------------------------------------------
# MCP bridge integration (R33-R36)
# ---------------------------------------------------------------------------


class TestMCPBridge:
    def test_bridge_empty_first_stream(self, project_dir, monkeypatch):
        """covers R34 — empty bridge.all_tools() does not error the stream."""
        _ensure_anthropic_key(monkeypatch)
        _, captured = _install_fake_anthropic(monkeypatch, scripted=[
            ([], _FakeFinal([], stop_reason="end_turn")),
        ])
        bridge = FakeBridge(tools=[])
        ws = FakeWS()
        _run(chat._stream_response(ws, project_dir, "proj", "alice", bridge, {}))
        # No error frame.
        assert not any(m.get("type") == "error" for m in ws.sent)

    def test_bridge_dispatch_preferred_over_execute_tool(self, project_dir, monkeypatch):
        """covers R17 — bridge.has_tool match dispatches via bridge.call_tool, skipping _execute_tool."""
        _ensure_anthropic_key(monkeypatch)
        # Single tool_use turn followed by an end_turn.
        tu = _FakeBlock("tool_use", id="tu_1", name="remember_search", input={"q": "x"})
        scripted = [
            (
                [_FakeEvent("content_block_start", content_block=tu)],
                _FakeFinal([tu], stop_reason="tool_use"),
            ),
            ([], _FakeFinal([_FakeBlock("text", text="done")], stop_reason="end_turn")),
        ]
        _install_fake_anthropic(monkeypatch, scripted=scripted)
        bridge = FakeBridge(has={"remember_search": True}, call_result=({"hits": []}, False))

        # Spy: ensure _execute_tool is NOT called for remember_search.
        execute_calls = []
        original = chat._execute_tool

        async def _spy(*args, **kwargs):
            execute_calls.append((args, kwargs))
            return await original(*args, **kwargs)

        monkeypatch.setattr(chat, "_execute_tool", _spy)
        ws = FakeWS()
        _run(chat._stream_response(ws, project_dir, "proj", "alice", bridge, {}))
        assert bridge.calls == [("remember_search", {"q": "x"})]
        assert all(call[0][1] != "remember_search" for call in execute_calls)

    def test_lazy_connect_via_create_task(self):
        """covers R33 — connect kicked off via asyncio.create_task, never awaited inline.

        The handler defines an inner ``_bg_connect_service`` coroutine that DOES
        ``await bridge.connect(...)`` — that is the fire-and-forget body. The
        invariant is that the read loop must spawn it via ``create_task`` and
        must NOT await ``_bg_connect_service`` itself.
        """
        src = inspect.getsource(chat.handle_chat_connection)
        # The connect must be spawned, not awaited.
        assert "asyncio.create_task(_bg_connect_service" in src
        # The bg helper itself must not be awaited synchronously by the loop.
        assert "await _bg_connect_service" not in src


# ---------------------------------------------------------------------------
# Connection handler (R37-R44)
# ---------------------------------------------------------------------------


def _patch_handle_connection_deps(monkeypatch, *, scripted=None, bridge=None):
    """Common patches for handle_chat_connection: stub MCPBridge, anthropic, plugin host."""
    from scenecraft import mcp_bridge as mb

    if bridge is None:
        bridge = FakeBridge()

    monkeypatch.setattr(mb, "MCPBridge", lambda: bridge)
    _install_fake_anthropic(monkeypatch, scripted=scripted)
    return bridge


class TestConnectionHandler:
    def test_invalid_json_continues_loop(self, project_dir, monkeypatch):
        """covers R39 — JSONDecodeError → error frame, loop continues."""
        _ensure_anthropic_key(monkeypatch)
        _patch_handle_connection_deps(monkeypatch, scripted=[
            ([], _FakeFinal([_FakeBlock("text", text="ok")], stop_reason="end_turn")),
        ])
        ws = FakeWS(frames=[
            "{not valid json",
            json.dumps({"type": "ping"}),
        ])
        _run(chat.handle_chat_connection(ws, project_dir, "proj"))
        types_seen = ws.types()
        assert "error" in types_seen
        assert any(
            m.get("type") == "error" and m.get("error") == "Invalid JSON"
            for m in ws.sent
        )
        assert "pong" in types_seen, "next valid frame still processed"

    def test_ping_replies_pong(self, project_dir, monkeypatch):
        """covers R43 — ping → pong."""
        _ensure_anthropic_key(monkeypatch)
        _patch_handle_connection_deps(monkeypatch)
        ws = FakeWS(frames=[json.dumps({"type": "ping"})])
        _run(chat.handle_chat_connection(ws, project_dir, "proj"))
        assert any(m.get("type") == "pong" for m in ws.sent)

    def test_unknown_frame_type_ignored(self, project_dir, monkeypatch):
        """covers R38 — unknown frame type silently ignored."""
        _ensure_anthropic_key(monkeypatch)
        _patch_handle_connection_deps(monkeypatch)
        ws = FakeWS(frames=[
            json.dumps({"type": "nonsense_xyz"}),
            json.dumps({"type": "ping"}),
        ])
        _run(chat.handle_chat_connection(ws, project_dir, "proj"))
        # No error frame for the unknown type.
        types_seen = ws.types()
        assert "pong" in types_seen
        # The only acceptable error is none from unknown type.
        errs = [m for m in ws.sent if m.get("type") == "error"]
        assert errs == []

    def test_empty_message_dropped(self, project_dir, monkeypatch):
        """covers R41 — message with empty content does not persist or stream."""
        _ensure_anthropic_key(monkeypatch)
        _patch_handle_connection_deps(monkeypatch)
        ws = FakeWS(frames=[json.dumps({"type": "message", "content": "   "})])
        _run(chat.handle_chat_connection(ws, project_dir, "proj"))
        # No message echoed back.
        msg_frames = [m for m in ws.sent if m.get("type") == "message"]
        assert msg_frames == []
        # No DB row.
        msgs = chat._get_messages(project_dir, "local")
        assert msgs == []

    def test_message_persists_user_row_and_echoes(self, project_dir, monkeypatch):
        """covers R40 — message: persist user row + echo + spawn stream."""
        _ensure_anthropic_key(monkeypatch)
        _patch_handle_connection_deps(monkeypatch, scripted=[
            ([], _FakeFinal([_FakeBlock("text", text="ok")], stop_reason="end_turn")),
        ])
        ws = FakeWS(frames=[json.dumps({"type": "message", "content": "hi"})])
        _run(chat.handle_chat_connection(ws, project_dir, "proj"))
        # User row exists.
        msgs = chat._get_messages(project_dir, "local")
        assert msgs[0]["role"] == "user"
        assert msgs[0]["content"] == "hi"
        # User echo emitted.
        echoes = [m for m in ws.sent if m.get("type") == "message"]
        assert len(echoes) >= 1

    def test_stale_elicitation_response_dropped(self, project_dir, monkeypatch):
        """covers R38 (stale elicitation_response) — no error, no state change."""
        _ensure_anthropic_key(monkeypatch)
        _patch_handle_connection_deps(monkeypatch)
        ws = FakeWS(frames=[
            json.dumps({"type": "elicitation_response", "id": "no_such_elic", "action": "accept"}),
            json.dumps({"type": "ping"}),
        ])
        _run(chat.handle_chat_connection(ws, project_dir, "proj"))
        # No error from the stale response; ping still answered.
        errs = [m for m in ws.sent if m.get("type") == "error"]
        assert errs == []
        assert any(m.get("type") == "pong" for m in ws.sent)

    def test_bridge_close_called_in_finally(self, project_dir, monkeypatch):
        """covers R36 — bridge.close awaited on disconnect."""
        _ensure_anthropic_key(monkeypatch)
        bridge = FakeBridge()
        _patch_handle_connection_deps(monkeypatch, bridge=bridge)
        ws = FakeWS(frames=[json.dumps({"type": "ping"})])
        _run(chat.handle_chat_connection(ws, project_dir, "proj"))
        assert bridge.closed is True

    def test_bridge_close_error_swallowed(self, project_dir, monkeypatch):
        """covers R36 — bridge.close exception is logged + swallowed."""
        _ensure_anthropic_key(monkeypatch)

        class _BadBridge(FakeBridge):
            async def close(self):
                raise RuntimeError("close failed")

        bridge = _BadBridge()
        _patch_handle_connection_deps(monkeypatch, bridge=bridge)
        ws = FakeWS(frames=[json.dumps({"type": "ping"})])
        # Must NOT propagate.
        _run(chat.handle_chat_connection(ws, project_dir, "proj"))


# ---------------------------------------------------------------------------
# Interrupt path (R45-R50)
# ---------------------------------------------------------------------------


class TestInterruptPath:
    def test_empty_cancel_no_persist_but_halted_emitted(self, project_dir, monkeypatch):
        """covers R46, R49, R50 — empty cancel emits halted+complete, persists nothing, re-raises."""
        _ensure_anthropic_key(monkeypatch)

        # Stream that hangs on first event so we can cancel before any text or tool output.
        class _HangingStream:
            async def __aenter__(self): return self
            async def __aexit__(self, *a): return False
            def __aiter__(self): return self._gen()
            async def _gen(self):
                # Yield nothing; instead block until cancelled.
                await asyncio.sleep(60)
                if False:
                    yield
            async def get_final_message(self):
                return _FakeFinal([], stop_reason="end_turn")

        fake_anthropic = types.ModuleType("anthropic")

        class _APIError(Exception): pass
        fake_anthropic.APIError = _APIError

        class _AsyncAnthropic:
            def __init__(self, api_key=None):
                self.messages = self
            def stream(self, **kw):
                return _HangingStream()
        fake_anthropic.AsyncAnthropic = _AsyncAnthropic
        monkeypatch.setitem(sys.modules, "anthropic", fake_anthropic)

        ws = FakeWS()

        async def _go():
            task = asyncio.create_task(
                chat._stream_response(ws, project_dir, "proj", "alice", FakeBridge(), {})
            )
            # Let it start.
            await asyncio.sleep(0.01)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        _run(_go())
        # No assistant row.
        msgs = chat._get_messages(project_dir, "alice")
        assert [m for m in msgs if m["role"] == "assistant"] == []
        # halted + complete emitted.
        types_seen = ws.types()
        assert "halted" in types_seen
        assert "complete" in types_seen
        halted = next(m for m in ws.sent if m.get("type") == "halted")
        assert halted["reason"] == "interrupted_by_user"

    def test_partial_text_flushed_persisted_marked_interrupted(self, project_dir, monkeypatch):
        """covers R45, R47, R48 — buffered text appended, persisted, message has interrupted=True."""
        _ensure_anthropic_key(monkeypatch)

        # Stream that emits one chunk, then hangs forever.
        class _PartialStream:
            async def __aenter__(self): return self
            async def __aexit__(self, *a): return False
            def __aiter__(self): return self._gen()
            async def _gen(self):
                yield _FakeEvent("content_block_delta", delta=_FakeBlock("text_delta", text="partial "))
                await asyncio.sleep(60)
                if False:
                    yield
            async def get_final_message(self):
                return _FakeFinal([_FakeBlock("text", text="partial ")], stop_reason="end_turn")

        fake_anthropic = types.ModuleType("anthropic")
        class _APIError(Exception): pass
        fake_anthropic.APIError = _APIError
        class _AsyncAnthropic:
            def __init__(self, api_key=None):
                self.messages = self
            def stream(self, **kw):
                return _PartialStream()
        fake_anthropic.AsyncAnthropic = _AsyncAnthropic
        monkeypatch.setitem(sys.modules, "anthropic", fake_anthropic)

        ws = FakeWS()

        async def _go():
            task = asyncio.create_task(
                chat._stream_response(ws, project_dir, "proj", "alice", FakeBridge(), {})
            )
            await asyncio.sleep(0.05)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        _run(_go())
        # Persisted assistant row with the partial text.
        msgs = chat._get_messages(project_dir, "alice")
        assistants = [m for m in msgs if m["role"] == "assistant"]
        assert len(assistants) == 1
        assert "partial" in str(assistants[0]["content"])
        # Emitted message frame has interrupted=True.
        msg_frames = [m for m in ws.sent if m.get("type") == "message"]
        assert any(mf.get("message", {}).get("interrupted") is True for mf in msg_frames)

    def test_persist_failure_swallowed_during_cancel(self, project_dir, monkeypatch):
        """covers R47 — _add_message exception during cancel is logged + swallowed."""
        _ensure_anthropic_key(monkeypatch)

        # Same hanging stream as above, but with one chunk so we have content
        # to persist.
        class _PartialStream:
            async def __aenter__(self): return self
            async def __aexit__(self, *a): return False
            def __aiter__(self): return self._gen()
            async def _gen(self):
                yield _FakeEvent("content_block_delta", delta=_FakeBlock("text_delta", text="x"))
                await asyncio.sleep(60)
                if False:
                    yield
            async def get_final_message(self):
                return _FakeFinal([], stop_reason="end_turn")

        fake_anthropic = types.ModuleType("anthropic")
        class _APIError(Exception): pass
        fake_anthropic.APIError = _APIError
        class _AsyncAnthropic:
            def __init__(self, api_key=None):
                self.messages = self
            def stream(self, **kw):
                return _PartialStream()
        fake_anthropic.AsyncAnthropic = _AsyncAnthropic
        monkeypatch.setitem(sys.modules, "anthropic", fake_anthropic)

        # Patch _add_message to raise.
        def _raise(*a, **kw):
            raise RuntimeError("db down")

        monkeypatch.setattr(chat, "_add_message", _raise)

        ws = FakeWS()

        async def _go():
            task = asyncio.create_task(
                chat._stream_response(ws, project_dir, "proj", "alice", FakeBridge(), {})
            )
            await asyncio.sleep(0.05)
            task.cancel()
            # CancelledError still propagates despite persist failure.
            with pytest.raises(asyncio.CancelledError):
                await task
        _run(_go())
        # halted still attempted.
        assert any(m.get("type") == "halted" for m in ws.sent)


# ---------------------------------------------------------------------------
# Error paths (R51-R54)
# ---------------------------------------------------------------------------


class TestErrorPaths:
    def test_no_anthropic_sdk_emits_error_complete(self, project_dir, monkeypatch):
        """covers R52 — ImportError from `import anthropic` → error+complete."""
        _ensure_anthropic_key(monkeypatch)
        # Inject a sentinel module that raises on attribute access AND ensure
        # `import anthropic` re-imports. Easiest: set it to a module that
        # is None — but `import anthropic` already-cached succeeds. So we
        # use a meta_path hook to make a fresh import fail.
        # Simpler: temporarily remove the cached module and add a finder.
        monkeypatch.delitem(sys.modules, "anthropic", raising=False)

        class _BlockFinder:
            def find_spec(self, fullname, path, target=None):
                if fullname == "anthropic":
                    raise ImportError("blocked for test")
                return None
        finder = _BlockFinder()
        sys.meta_path.insert(0, finder)
        try:
            ws = FakeWS()
            _run(chat._stream_response(ws, project_dir, "proj", "alice", FakeBridge(), {}))
            types_seen = ws.types()
            assert types_seen[0] == "error"
            assert "anthropic" in ws.sent[0]["error"].lower()
            assert types_seen[-1] == "complete"
        finally:
            sys.meta_path.remove(finder)

    def test_api_error_emits_error_complete_no_halted(self, project_dir, monkeypatch):
        """covers R53 — APIError surface; no halted frame on this path."""
        _ensure_anthropic_key(monkeypatch)
        # Prepare APIError class first; reuse its instance below.
        fake_anthropic = types.ModuleType("anthropic")

        class _APIError(Exception):
            def __init__(self, msg):
                super().__init__(msg)
                self.message = msg

        fake_anthropic.APIError = _APIError

        class _AsyncAnthropic:
            def __init__(self, api_key=None):
                self.messages = self
            def stream(self, **kw):
                raise _APIError("upstream rate limit")
        fake_anthropic.AsyncAnthropic = _AsyncAnthropic
        monkeypatch.setitem(sys.modules, "anthropic", fake_anthropic)

        ws = FakeWS()
        _run(chat._stream_response(ws, project_dir, "proj", "alice", FakeBridge(), {}))
        types_seen = ws.types()
        assert "error" in types_seen
        assert "halted" not in types_seen, "APIError must NOT emit halted (reserved for interrupt)"
        assert types_seen[-1] == "complete"
        err = next(m for m in ws.sent if m.get("type") == "error")
        assert "Claude API error" in err["error"]

    def test_generic_exception_emits_error_complete_no_halted(self, project_dir, monkeypatch):
        """covers R54 — generic exception surface; no halted frame."""
        _ensure_anthropic_key(monkeypatch)
        fake_anthropic = types.ModuleType("anthropic")

        class _APIError(Exception): pass
        fake_anthropic.APIError = _APIError

        class _AsyncAnthropic:
            def __init__(self, api_key=None):
                self.messages = self
            def stream(self, **kw):
                raise RuntimeError("boom")
        fake_anthropic.AsyncAnthropic = _AsyncAnthropic
        monkeypatch.setitem(sys.modules, "anthropic", fake_anthropic)

        ws = FakeWS()
        _run(chat._stream_response(ws, project_dir, "proj", "alice", FakeBridge(), {}))
        types_seen = ws.types()
        assert "error" in types_seen
        assert "halted" not in types_seen
        assert types_seen[-1] == "complete"


# ---------------------------------------------------------------------------
# Generation polling (R55-R57)
# ---------------------------------------------------------------------------


class _FakeJob:
    def __init__(self, status="running", completed=0, total=4, result=None, error=None):
        self.status = status
        self.completed = completed
        self.total = total
        self.result = result
        self.error = error
        self.meta = {}


class _FakeJobManager:
    def __init__(self, transitions):
        self._transitions = list(transitions)
        self._current = transitions[0] if transitions else _FakeJob(status="completed", result={})
        self.cancel_calls = 0

    def get_job(self, job_id):
        if self._transitions:
            self._current = self._transitions.pop(0)
        return self._current

    def cancel(self, job_id):
        self.cancel_calls += 1


class TestGenerationPolling:
    def test_progress_emitted_on_counter_change(self, monkeypatch):
        """covers R56 — frame only emitted when completed counter changes."""
        from scenecraft import ws_server as wss
        manager = _FakeJobManager([
            _FakeJob(status="running", completed=0, total=4),
            _FakeJob(status="running", completed=1, total=4),
            _FakeJob(status="running", completed=1, total=4),  # no change
            _FakeJob(status="running", completed=2, total=4),
            _FakeJob(status="completed", completed=4, total=4, result={"ok": True}),
        ])
        monkeypatch.setattr(wss, "job_manager", manager)
        ws = FakeWS()
        result, is_error = _run(chat._await_generation_job(
            ws, "tu_1", "proj", "job_1", poll_interval=0, timeout=5,
        ))
        assert is_error is False
        # completed transitions: 0 -> 1 -> 2 -> 4 → 4 progress frames (one per change).
        progress = [m for m in ws.sent if m.get("type") == "tool_progress"]
        # First poll observes 0 (counter change from -1) → emits.
        # Then 1 (change), then 1 (no change, no emit), then 2 (change), then 4 (change).
        assert len(progress) == 4
        # Final result returned correctly.
        assert result == {"ok": True}

    def test_progress_throttled_on_no_change(self, monkeypatch):
        """covers R56 — repeated identical counters do not emit duplicates."""
        from scenecraft import ws_server as wss
        manager = _FakeJobManager([
            _FakeJob(status="running", completed=3, total=4),
            _FakeJob(status="running", completed=3, total=4),
            _FakeJob(status="running", completed=3, total=4),
            _FakeJob(status="completed", completed=3, total=4, result={"done": 1}),
        ])
        monkeypatch.setattr(wss, "job_manager", manager)
        ws = FakeWS()
        _run(chat._await_generation_job(ws, "tu_1", "proj", "j", poll_interval=0, timeout=5))
        progress = [m for m in ws.sent if m.get("type") == "tool_progress"]
        # Only the initial transition emits.
        assert len(progress) == 1

    def test_failed_job_returns_error(self, monkeypatch):
        """covers _await_generation_job terminal failure path."""
        from scenecraft import ws_server as wss
        manager = _FakeJobManager([
            _FakeJob(status="failed", completed=2, total=4, error="model crashed"),
        ])
        monkeypatch.setattr(wss, "job_manager", manager)
        ws = FakeWS()
        result, is_error = _run(chat._await_generation_job(ws, "tu_1", "proj", "j", poll_interval=0))
        assert is_error is True
        assert "model crashed" in result["error"]

    def test_timeout_returns_error_and_does_not_cancel(self, monkeypatch):
        """covers R57 — 900s timeout returns error; underlying job NOT cancelled.

        We bypass the wall-clock by using timeout=0 so the FIRST iteration
        immediately exceeds the deadline.
        """
        from scenecraft import ws_server as wss
        # Job perpetually running.
        manager = _FakeJobManager([_FakeJob(status="running", completed=1, total=4)])
        monkeypatch.setattr(wss, "job_manager", manager)
        ws = FakeWS()
        # timeout=0 (or very small) → first deadline check trips.
        result, is_error = _run(chat._await_generation_job(
            ws, "tu_1", "proj", "j_long", poll_interval=0, timeout=0,
        ))
        assert is_error is True
        assert "did not finish" in result["error"]
        # Underlying job NOT cancelled (preserves disconnect-survival invariant).
        assert manager.cancel_calls == 0

    def test_progress_send_failure_does_not_raise(self, monkeypatch):
        """covers R55/R56 — ws.send failures during tool_progress are swallowed."""
        from scenecraft import ws_server as wss
        manager = _FakeJobManager([
            _FakeJob(status="running", completed=1, total=4),
            _FakeJob(status="completed", completed=1, total=4, result={}),
        ])
        monkeypatch.setattr(wss, "job_manager", manager)

        class _ExplodingWS(FakeWS):
            async def send(self, raw):
                # First call (tool_progress) explodes; subsequent calls succeed.
                if not getattr(self, "_exploded", False):
                    self._exploded = True
                    raise RuntimeError("ws closed")
                self.sent_raw.append(raw)
                self.sent.append(json.loads(raw))

        ws = _ExplodingWS()
        result, is_error = _run(chat._await_generation_job(ws, "tu_1", "proj", "j", poll_interval=0))
        assert is_error is False


# ---------------------------------------------------------------------------
# Migration contract — observable invariants any refactor MUST preserve
# ---------------------------------------------------------------------------


class TestMigrationContract:
    """Pin observable invariants. If any of these change, refactor is no longer pure."""

    def test_history_window_is_50(self):
        sig = inspect.signature(chat._get_messages)
        assert sig.parameters["limit"].default == 50

    def test_elicitation_timeout_is_300(self):
        sig = inspect.signature(chat._recv_elicitation_response)
        assert sig.parameters["timeout"].default == 300

    def test_generation_timeout_is_900(self):
        sig = inspect.signature(chat._await_generation_job)
        assert sig.parameters["timeout"].default == 900

    def test_tool_loop_cap_is_10(self):
        src = inspect.getsource(chat._stream_response)
        # Cap is encoded as `for _ in range(10):` in chat.py.
        assert "range(10)" in src, "tool-loop cap literal '10' must be preserved"

    def test_model_literal(self):
        src = inspect.getsource(chat._stream_response)
        assert 'model="claude-sonnet-4-20250514"' in src
        assert "max_tokens=4096" in src

    def test_lazy_mcp_connect(self):
        """Connect must be fired-and-forgotten (asyncio.create_task), never awaited.

        The bg helper itself contains an ``await bridge.connect(...)`` — that's
        the body of the fire-and-forget task. We assert the SPAWN pattern is
        present and the spawn is not synchronously awaited.
        """
        src = inspect.getsource(chat.handle_chat_connection)
        assert "asyncio.create_task(_bg_connect_service" in src
        assert "await _bg_connect_service" not in src

    def test_dispatcher_precedence_in_execute_tool(self):
        """Plugin-namespaced (__) checked BEFORE built-in if/elif chain."""
        src = inspect.getsource(chat._execute_tool)
        # Find positions: PluginHost lookup must precede the first `name == "sql_query"` check.
        plugin_pos = src.find("PluginHost.get_mcp_tool")
        sql_pos = src.find('name == "sql_query"')
        assert 0 < plugin_pos < sql_pos, "plugin dispatch must precede builtin if-chain"

    def test_dispatcher_precedence_in_stream_response(self):
        """bridge.has_tool checked BEFORE _execute_tool in per-tool dispatch."""
        src = inspect.getsource(chat._stream_response)
        bridge_pos = src.find("bridge.has_tool")
        execute_pos = src.find("_execute_tool(")
        assert 0 < bridge_pos < execute_pos


# ---------------------------------------------------------------------------
# Negative-assertion / current-state pins for OQ resolutions
# ---------------------------------------------------------------------------


class TestCurrentStatePins:
    """Capture the engine's current behavior so target-state xfails have a baseline."""

    def test_emits_bare_event_names_today(self):
        """INV-4 divergence — engine currently emits bare 'chunk', 'tool_call', etc.

        Target is 'core__chat__*' namespacing; this pins today's reality.
        """
        src = inspect.getsource(chat._stream_response)
        assert '"type": "chunk"' in src
        assert '"type": "tool_call"' in src
        assert '"type": "tool_result"' in src
        # Target name should NOT yet be in source.
        assert "core__chat__" not in src

    def test_no_per_project_lock(self):
        """INV-1 negative — _stream_response and handle_chat_connection hold no per-project lock."""
        for fn in (chat._stream_response, chat.handle_chat_connection):
            src = inspect.getsource(fn)
            assert "threading.Lock" not in src
            # asyncio.Lock would be the foot-gun here. (Note: asyncio.Future
            # is fine — it's the elicitation_waiters mechanism.)
            assert "asyncio.Lock" not in src


# ---------------------------------------------------------------------------
# Target-state xfails (OQ-1 .. OQ-6, INV-4 namespacing)
# ---------------------------------------------------------------------------


@pytest.mark.xfail(reason="target-state OQ-1; awaits explicit session-scope codification of tool_progress", strict=False)
def test_tool_progress_session_scoped_no_cross_talk():
    """covers R58/OQ-1 — tool_progress emission is scoped to originating session WS only.

    Today's code already only sends via the `ws` arg, but R58 codifies the
    boundary explicitly with a registration / lookup contract. Failing until
    the refactor publishes that contract.
    """
    src = inspect.getsource(chat._await_generation_job)
    # Target: explicit "session_id" or "originating_ws_only" guard documented.
    assert "session_id" in src or "originating_ws" in src


@pytest.mark.xfail(reason="target-state OQ-2; plugin/built-in name-collision warning not emitted today", strict=False)
def test_plugin_builtin_collision_warns():
    """covers R59/OQ-2 — plugin tool registration that shadows a built-in must WARN."""
    from scenecraft import plugin_host as ph
    src = inspect.getsource(ph)
    # Target marker.
    assert "collision" in src.lower() or "shadowed" in src.lower()


@pytest.mark.xfail(reason="target-state OQ-3; row-vs-turn semantics pending explicit contract", strict=False)
def test_history_window_row_vs_turn_semantics_documented():
    """covers R60/OQ-3 — the 50-row window is row-count, not turn-count, by design."""
    src = inspect.getsource(chat._get_messages)
    assert "row count" in src.lower() or "turn count" in src.lower()


@pytest.mark.xfail(reason="target-state OQ-4; explicit elicitation_waiters purge on disconnect not coded", strict=False)
def test_disconnect_cancels_and_purges_elicitation_waiters():
    """covers R61/OQ-4 — on WS disconnect, all pending futures cancelled + waiters dict purged."""
    src = inspect.getsource(chat.handle_chat_connection)
    # Target: explicit purge in finally.
    assert "elicitation_waiters.clear()" in src or "for fut in elicitation_waiters" in src


@pytest.mark.xfail(reason="target-state OQ-5; mcp_tools_ready event not yet emitted on late connect", strict=False)
def test_mcp_late_connect_emits_ready_event():
    """covers R62/OQ-5 — successful background connect emits core__chat__mcp_tools_ready once."""
    src = inspect.getsource(chat.handle_chat_connection)
    assert "mcp_tools_ready" in src


@pytest.mark.xfail(reason="target-state OQ-6; anthropic upper-bound pin not in pyproject yet", strict=False)
def test_anthropic_sdk_pinned_with_upper_bound():
    """covers R63/OQ-6 — pyproject.toml pins anthropic with both lower and upper bound."""
    pyproject = Path(__file__).resolve().parents[2] / "pyproject.toml"
    text = pyproject.read_text()
    # Target: '<1.0' style upper bound.
    assert re.search(r"anthropic\s*[>=]+\s*0\.\d+\s*,\s*<\s*1\.0", text), (
        "expected explicit upper bound on anthropic"
    )


@pytest.mark.xfail(reason="target-state OQ-6; import-time compat check not present", strict=False)
def test_anthropic_sdk_import_time_compat_check():
    """covers R63/OQ-6 — engine boot validates anthropic version at import."""
    src = inspect.getsource(chat)
    # Target marker — explicit version check.
    assert "anthropic.__version__" in src


@pytest.mark.xfail(reason="target-state INV-4; bare event types still emitted, target is core__chat__* prefix", strict=False)
def test_event_types_use_core_chat_namespace():
    """covers INV-4 — wire event types use 'core__chat__*' prefix."""
    src = inspect.getsource(chat._stream_response)
    assert '"type": "core__chat__chunk"' in src


# ---------------------------------------------------------------------------
# End-to-end via engine_server (HTTP only — WS not booted by this fixture)
# ---------------------------------------------------------------------------


class TestEndToEnd:
    """E2E HTTP coverage. The session-scoped engine_server fixture in conftest.py
    only boots the HTTP API (no WebSocket server) — so WS-level scenarios remain
    in the unit-test sections above. The HTTP /api/projects/:name/chat endpoint
    is the observable persistence surface for chat history.
    """

    def test_chat_history_endpoint_returns_messages_shape(self, engine_server, project_name):
        """covers R3, R4 + endpoint contract — GET /api/projects/:name/chat returns list shape."""
        # Empty project → empty list.
        status, body = engine_server.json("GET", f"/api/projects/{project_name}/chat")
        assert status == 200
        assert "messages" in body
        assert isinstance(body["messages"], list)
        assert body["messages"] == []

    def test_chat_history_persists_and_returns_inserted_message(self, engine_server, project_name):
        """covers R2, R3 — _add_message-inserted rows are observable via GET /chat."""
        # Insert directly via the function (chat WS isn't booted in this fixture).
        work_dir = engine_server.work_dir
        project_dir = Path(work_dir) / project_name
        chat._add_message(project_dir, "local", "user", "hello e2e")
        status, body = engine_server.json("GET", f"/api/projects/{project_name}/chat")
        assert status == 200
        msgs = body["messages"]
        assert len(msgs) == 1
        assert msgs[0]["role"] == "user"
        assert msgs[0]["content"] == "hello e2e"

    def test_chat_history_respects_limit_query_param(self, engine_server, project_name):
        """covers R4 — limit query param honored at HTTP layer."""
        work_dir = engine_server.work_dir
        project_dir = Path(work_dir) / project_name
        for i in range(10):
            chat._add_message(project_dir, "local", "user", f"msg-{i}")
        status, body = engine_server.json(
            "GET", f"/api/projects/{project_name}/chat?limit=3"
        )
        assert status == 200
        assert len(body["messages"]) == 3
        # Oldest-first (R3).
        assert [m["content"] for m in body["messages"]] == ["msg-7", "msg-8", "msg-9"]

    def test_chat_history_assistant_json_blocks_decoded(self, engine_server, project_name):
        """covers R3 — assistant JSON-list content decoded to blocks for HTTP consumers."""
        work_dir = engine_server.work_dir
        project_dir = Path(work_dir) / project_name
        blocks = [{"type": "text", "text": "hi"}, {"type": "tool_use", "id": "t1", "name": "x", "input": {}}]
        chat._add_message(project_dir, "local", "assistant", json.dumps(blocks))
        status, body = engine_server.json("GET", f"/api/projects/{project_name}/chat")
        assert status == 200
        msg = body["messages"][0]
        assert msg["role"] == "assistant"
        assert msg["content"] == blocks


# Note: WS-level e2e (live `/ws/chat/` round-trips with mocked Anthropic) is
# intentionally deferred. The engine_server fixture in conftest.py boots only
# HTTPServer, not the websockets server — wiring a live WS test requires a
# parallel server fixture and is tracked as a separate task. The unit-test
# sections above exercise every WS frame shape via FakeWS at the function
# boundary, providing equivalent coverage at lower cost.
