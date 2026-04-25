"""Tests for M18 task-148 generate_foley chat tool.

Covers:
- Tool schema registered in the chat tool list
- _is_destructive() returns True for 'generate_foley' (elicitation triggers)
- Dispatch branch for name='generate_foley' invokes the plugin
- Validation errors surface as tool errors (not exceptions)
- count > 1 rejected
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from scenecraft import chat as chat_module
from scenecraft.chat import (
    GENERATE_FOLEY_TOOL,
    _is_destructive,
    _execute_tool,
)


# --- Tool schema ----------------------------------------------------------


def test_generate_foley_tool_definition():
    # Tool list is assembled inline in chat.py; verify via the tool def itself.
    assert GENERATE_FOLEY_TOOL["name"] == "generate_foley"
    assert "t2fx" in GENERATE_FOLEY_TOOL["description"]
    assert "v2fx" in GENERATE_FOLEY_TOOL["description"]


def test_generate_foley_in_tool_list():
    # Grep the source for the symbol in the aggregated tools list.
    import inspect
    import scenecraft.chat as chat_mod
    src = inspect.getsource(chat_mod)
    # The list literal contains `GENERATE_FOLEY_TOOL,` between brackets.
    assert "GENERATE_FOLEY_TOOL," in src


def test_schema_has_expected_properties():
    props = GENERATE_FOLEY_TOOL["input_schema"]["properties"]
    for field in (
        "prompt", "duration_seconds", "source_candidate_id",
        "in_seconds", "out_seconds", "negative_prompt",
        "cfg_strength", "seed", "entity_type", "entity_id", "count",
    ):
        assert field in props, f"missing {field}"


def test_schema_duration_bounded():
    dur = GENERATE_FOLEY_TOOL["input_schema"]["properties"]["duration_seconds"]
    assert dur["minimum"] == 1
    assert dur["maximum"] == 30


def test_schema_count_is_const_one():
    count = GENERATE_FOLEY_TOOL["input_schema"]["properties"]["count"]
    assert count["const"] == 1


def test_schema_entity_type_enum_is_transition_only():
    et = GENERATE_FOLEY_TOOL["input_schema"]["properties"]["entity_type"]
    assert et["enum"] == ["transition"]


# --- Destructive flag ------------------------------------------------------


def test_generate_foley_is_destructive():
    """elicitation gate fires before the tool runs."""
    assert _is_destructive("generate_foley") is True


# --- Dispatch --------------------------------------------------------------


@pytest.fixture
def project_dir(tmp_path):
    from scenecraft.db import get_db
    get_db(tmp_path)
    (tmp_path / "pool" / "segments").mkdir(parents=True, exist_ok=True)
    return tmp_path


def test_dispatch_rejects_count_not_one(project_dir):
    """Calling the tool with count=2 returns a tool error."""
    loop = asyncio.new_event_loop()
    try:
        result, is_error = loop.run_until_complete(
            _execute_tool(
                project_dir,
                "generate_foley",
                {"prompt": "x", "duration_seconds": 2.0, "count": 2},
                project_name="test",
                ws=None,
                tool_use_id=None,
            )
        )
    finally:
        loop.close()
    assert is_error is True
    assert "count must be 1" in result["error"]


def test_dispatch_passes_validation_error_back_as_tool_error(project_dir, monkeypatch):
    """If plugin run() raises ValueError (e.g., v2fx missing candidate),
    the tool returns that error string rather than propagating the exception."""
    monkeypatch.setenv("REPLICATE_API_TOKEN", "r8_test")
    loop = asyncio.new_event_loop()
    try:
        result, is_error = loop.run_until_complete(
            _execute_tool(
                project_dir,
                "generate_foley",
                {
                    # v2fx-ish (has candidate) but missing in/out
                    "source_candidate_id": "ps_x",
                    "count": 1,
                },
                project_name="test",
                ws=None,
                tool_use_id=None,
            )
        )
    finally:
        loop.close()
    assert is_error is True
    assert "v2fx mode requires" in result["error"]


def test_dispatch_requires_ws_context(project_dir, monkeypatch):
    """When ws/tool_use_id are None, the tool errors out rather than silently
    returning the kickoff dict without awaiting the job."""
    monkeypatch.setenv("REPLICATE_API_TOKEN", "r8_test")

    # Stub the provider so we don't hit network
    from scenecraft.plugin_api.providers import replicate as repmod
    fake_out = project_dir / "fake.wav"
    fake_out.write_bytes(b"X")

    class FR:
        prediction_id = "pred_x"
        status = "succeeded"
        output_paths = [fake_out]
        spend_ledger_id = "ledger_x"
        raw = {}

    monkeypatch.setattr(repmod, "run_prediction", lambda **kw: FR())

    loop = asyncio.new_event_loop()
    try:
        result, is_error = loop.run_until_complete(
            _execute_tool(
                project_dir,
                "generate_foley",
                {"prompt": "x", "duration_seconds": 2.0},
                project_name="test",
                ws=None,
                tool_use_id=None,
            )
        )
    finally:
        loop.close()
    assert is_error is True
    assert "ws context" in result["error"]
