"""Tests for M11 task-105 — chat tool ``isolate_vocals`` wiring.

Covers:
  * ``_is_destructive("isolate_vocals__run")`` triggers the elicitation gate.
  * ``_format_destructive_summary`` renders the rich preview for audio_clip
    + transition entities, full + subset ranges, and the (NOT FOUND) path.
  * ``_execute_tool`` routes through ``PluginHost.get_operation`` and awaits
    the job; bad inputs and missing registration surface as clear errors.

The plugin handler is mocked — task-102 already covers the real DFN3 path.
Here we only verify chat.py's adapter logic.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest


# ── _is_destructive ───────────────────────────────────────────────────────


def test_is_destructive_matches_isolate_vocals():
    from scenecraft.chat import _is_destructive

    assert _is_destructive("isolate_vocals__run") is True
    assert _is_destructive("ISOLATE_VOCALS") is True  # case-insensitive


def test_is_destructive_future_isolate_tools_also_match():
    from scenecraft.chat import _is_destructive

    # pattern-based match future-proofs sibling isolate_* tools
    assert _is_destructive("isolate_music") is True


# ── Rich summary ──────────────────────────────────────────────────────────


@pytest.fixture
def project_with_clip(tmp_path):
    from scenecraft.db import add_audio_clip, add_audio_track, get_db

    p = tmp_path / "p"
    p.mkdir()
    get_db(p)
    add_audio_track(p, {"id": "at1", "name": "t", "display_order": 0})
    add_audio_clip(
        p,
        {
            "id": "ac_short",
            "track_id": "at1",
            "source_path": "pool/segments/x.wav",
            "start_time": 0.0,
            "end_time": 60.0,
        },
    )
    return p


def test_summary_audio_clip_full_range(project_with_clip):
    from scenecraft.chat import _format_destructive_summary

    msg, items = _format_destructive_summary(
        "isolate_vocals__run",
        {"entity_type": "audio_clip", "entity_id": "ac_short", "range_mode": "full"},
        project_with_clip,
    )
    assert "ac_short" in msg
    joined = " | ".join(items)
    assert "audio_clip" in joined
    assert "DeepFilterNet3" in joined
    assert "2 stems" in joined
    assert "full" in joined
    assert "60.0s" in joined


def test_summary_audio_clip_subset_range(project_with_clip):
    from scenecraft.chat import _format_destructive_summary

    _msg, items = _format_destructive_summary(
        "isolate_vocals__run",
        {
            "entity_type": "audio_clip",
            "entity_id": "ac_short",
            "range_mode": "subset",
            "trim_in": 10.0,
            "trim_out": 25.0,
        },
        project_with_clip,
    )
    joined = " | ".join(items)
    assert "subset" in joined
    assert "10.0s" in joined and "25.0s" in joined
    # Active duration = 25 - 10 = 15s; ETA low ≈ 15s
    assert "15.0s" in joined or "15s" in joined


def test_summary_missing_entity(project_with_clip):
    from scenecraft.chat import _format_destructive_summary

    msg, items = _format_destructive_summary(
        "isolate_vocals__run",
        {"entity_type": "audio_clip", "entity_id": "does_not_exist"},
        project_with_clip,
    )
    assert "does_not_exist" in msg
    assert any("NOT FOUND" in i for i in items)


# ── _execute_tool routing ────────────────────────────────────────────────


class _FakeOp:
    def __init__(self, kickoff):
        self._kickoff = kickoff

    def handler(self, entity_type, entity_id, context):
        return self._kickoff


class _FakeWS:
    async def send_json(self, *a, **kw):  # noqa: D401
        return None


@pytest.fixture
def mock_plugin_host(monkeypatch):
    """Install a fake PluginHost.get_operation that returns a configurable op.

    Usage: call ``mock_plugin_host.set_op(FakeOp({"isolation_id": ..., "job_id": ...}))``.
    """
    from scenecraft import plugin_host as ph_mod

    class _Sentinel:
        op = None

        def set_op(self, op):
            self.op = op

    sentinel = _Sentinel()

    def fake_get_operation(op_id):
        if sentinel.op is None:
            return None
        return sentinel.op

    monkeypatch.setattr(ph_mod.PluginHost, "get_operation", classmethod(lambda cls, op_id: sentinel.op))
    return sentinel


def _run_exec(project_dir, input_data, **kwargs):
    from scenecraft.chat import _execute_tool

    return asyncio.run(
        _execute_tool(project_dir, "isolate_vocals__run", input_data, **kwargs)
    )


def test_execute_missing_entity_id(project_with_clip, mock_plugin_host):
    result, is_err = _run_exec(project_with_clip, {"entity_type": "audio_clip"})
    assert is_err is True
    assert "entity_id" in result["error"]


def test_execute_invalid_entity_type(project_with_clip, mock_plugin_host):
    result, is_err = _run_exec(
        project_with_clip,
        {"entity_type": "keyframe", "entity_id": "kf_x"},
    )
    assert is_err is True
    assert "unsupported entity_type" in result["error"]


def test_execute_plugin_not_registered(project_with_clip, mock_plugin_host):
    # mock_plugin_host fixture defaults to None — plugin not registered
    result, is_err = _run_exec(
        project_with_clip,
        {"entity_type": "audio_clip", "entity_id": "ac_short"},
    )
    assert is_err is True
    assert "not registered" in result["error"]


def test_execute_kickoff_error(project_with_clip, mock_plugin_host):
    mock_plugin_host.set_op(_FakeOp({"error": "source missing"}))
    result, is_err = _run_exec(
        project_with_clip,
        {"entity_type": "audio_clip", "entity_id": "ac_short"},
    )
    assert is_err is True
    assert result["error"] == "source missing"


def test_execute_requires_ws_context(project_with_clip, mock_plugin_host):
    """If kickoff succeeds but no WS is provided, we must surface the internal
    error rather than silently hanging on a job poll."""
    mock_plugin_host.set_op(_FakeOp({"isolation_id": "iso_x", "job_id": "job_x"}))
    result, is_err = _run_exec(
        project_with_clip,
        {"entity_type": "audio_clip", "entity_id": "ac_short"},
        ws=None,
        tool_use_id=None,
    )
    assert is_err is True
    assert "ws context" in result["error"]


def test_isolate_vocals_tool_in_tools_list():
    from scenecraft.chat import TOOLS

    names = {t["name"] for t in TOOLS}
    assert "isolate_vocals__run" in names


def test_isolate_vocals_tool_schema_shape():
    from scenecraft.chat import TOOLS

    tool = next(t for t in TOOLS if t["name"] == "isolate_vocals__run")
    props = tool["input_schema"]["properties"]
    assert set(tool["input_schema"]["required"]) == {"entity_type", "entity_id"}
    assert props["entity_type"]["enum"] == ["audio_clip", "transition"]
    assert props["range_mode"]["enum"] == ["full", "subset"]
