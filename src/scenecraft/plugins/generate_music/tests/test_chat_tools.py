"""Chat-tool contribution tests for generate_music.

Covers the plugin-manifest-driven registration path:
  - `generate_music__run` shows up in PluginHost.list_mcp_tools()
  - `generate_music__credits` shows up + is marked non-destructive
  - destructive flag flows through _is_destructive() for the run tool
  - handler dispatch works end-to-end for get_credits (happy + no-key)
  - run handler forwards args into generate_music.run()
  - no `generate_lyrics` tool exists (per Q6.1)
"""

from __future__ import annotations

import http.server
import socket
import threading
import urllib.parse
from pathlib import Path

import pytest

from scenecraft.plugins.generate_music.tests.test_generate_music import (  # noqa: E402
    MockMusicfulState,
    _make_handler,
    _free_port,
)


@pytest.fixture
def plugin_registered():
    """Register generate_music with PluginHost and tear down after."""
    from scenecraft.plugin_host import PluginHost
    from scenecraft.plugins import generate_music

    PluginHost._reset_for_tests()
    PluginHost.register(generate_music)
    yield PluginHost
    PluginHost._reset_for_tests()


@pytest.fixture
def project_dir(tmp_path):
    from scenecraft.db import get_db
    pd = tmp_path / "project"
    pd.mkdir()
    get_db(pd)
    return pd


@pytest.fixture
def server_root(tmp_path, monkeypatch):
    root = tmp_path / ".scenecraft"
    root.mkdir()
    monkeypatch.setenv("SCENECRAFT_ROOT", str(root))
    from scenecraft.vcs.bootstrap import get_server_db
    get_server_db(root)
    return root


@pytest.fixture
def mock_musicful(monkeypatch):
    state = MockMusicfulState()
    port = _free_port()
    handler = _make_handler(state)
    server = http.server.ThreadingHTTPServer(("127.0.0.1", port), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    import scenecraft.plugin_api as pa
    monkeypatch.setitem(
        pa.SERVICE_REGISTRY, "musicful",
        (f"http://127.0.0.1:{port}", "MUSICFUL_API_KEY", "x-api-key"),
    )
    monkeypatch.setenv("MUSICFUL_API_KEY", "test-key")
    yield state
    server.shutdown()
    thread.join(timeout=2)


# ── Registration ────────────────────────────────────────────────────────


def test_registers_two_mcp_tools(plugin_registered):
    tools = {t.full_name: t for t in plugin_registered.list_mcp_tools()}
    assert "generate_music__run" in tools
    assert "generate_music__credits" in tools


def test_run_tool_is_destructive(plugin_registered):
    tool = plugin_registered.get_mcp_tool("generate_music__run")
    assert tool is not None
    assert tool.destructive is True


def test_credits_tool_is_not_destructive(plugin_registered):
    tool = plugin_registered.get_mcp_tool("generate_music__credits")
    assert tool is not None
    assert tool.destructive is False


def test_registers_run_operation(plugin_registered):
    op = plugin_registered.get_operation("generate_music.run")
    assert op is not None
    assert set(op.entity_types) == {"audio_clip", "transition"}


def test_is_destructive_respects_mcp_flag(plugin_registered):
    # chat._is_destructive(name) trusts MCPToolDef.destructive for
    # plugin-contributed tools — the run tool should gate, credits
    # should not.
    from scenecraft.chat import _is_destructive
    assert _is_destructive("generate_music__run") is True
    assert _is_destructive("generate_music__credits") is False


def test_no_generate_lyrics_tool_exists(plugin_registered):
    # Per Q6.1: Claude drafts lyrics inline, no dedicated tool.
    names = {t.full_name for t in plugin_registered.list_mcp_tools()}
    assert not any("lyrics" in n for n in names)


def test_tool_appears_in_chat_tools_for_claude(plugin_registered):
    # Simulate the plugin_contributed merge that chat.py does when
    # building tools_for_claude; asserts the schema round-trips.
    tools = plugin_registered.list_mcp_tools()
    serialized = [
        {
            "name": t.full_name,
            "description": t.description,
            "input_schema": t.input_schema,
        }
        for t in tools
    ]
    run_tool = next(x for x in serialized if x["name"] == "generate_music__run")
    assert run_tool["input_schema"]["required"] == ["action", "style"]
    assert run_tool["input_schema"]["properties"]["action"]["enum"] == ["auto", "custom"]


# ── Handler dispatch ───────────────────────────────────────────────────


def test_get_credits_handler_returns_balance(plugin_registered, project_dir, mock_musicful):
    state = mock_musicful
    state.key_info = {"key_music_counts": 237, "email": "test@x"}
    tool = plugin_registered.get_mcp_tool("generate_music__credits")
    result = tool.handler({}, {"project_dir": project_dir, "project_name": "p1"})
    assert result["credits"] == 237
    assert "last_checked_at" in result


def test_get_credits_handler_missing_key_returns_admin_error(plugin_registered, project_dir, monkeypatch):
    monkeypatch.delenv("MUSICFUL_API_KEY", raising=False)
    tool = plugin_registered.get_mcp_tool("generate_music__credits")
    result = tool.handler({}, {"project_dir": project_dir, "project_name": "p1"})
    assert result["credits"] is None
    assert "Musicful API key" in result["error"]


def test_run_handler_forwards_to_generate_music_run(
    plugin_registered, project_dir, server_root, mock_musicful, monkeypatch,
):
    state = mock_musicful
    state.generate_responses.append(["t1"])
    state.tasks["t1"] = {
        "id": "t1", "title": "x", "style": "dark", "duration": 60,
        "audio_url": f"http://127.0.0.1/x.mp3", "cover_url": "", "status": 3,
    }
    from scenecraft.plugins.generate_music import generate_music as gm
    monkeypatch.setattr(gm, "POLL_INTERVAL_SECONDS", 0.05)

    tool = plugin_registered.get_mcp_tool("generate_music__run")
    result = tool.handler(
        {"action": "auto", "style": "dark cinematic"},
        {"project_dir": project_dir, "project_name": "p1", "auth": {}},
    )
    assert result.get("status") == "running"
    assert "generation_id" in result
    assert "task_ids" in result


def test_run_handler_surfaces_validation_error(plugin_registered, project_dir, server_root, mock_musicful):
    tool = plugin_registered.get_mcp_tool("generate_music__run")
    result = tool.handler(
        {"action": "auto", "style": ""},
        {"project_dir": project_dir, "project_name": "p1", "auth": {}},
    )
    assert "error" in result
    assert "style" in result["error"].lower()
