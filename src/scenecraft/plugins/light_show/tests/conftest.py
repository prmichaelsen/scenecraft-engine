"""Shared pytest fixtures for the M19 scene-editor backend test suite.

Conventions match isolate_vocals/generate_music tests: tmp_project_dir
fixture creates a fresh project DB with _ensure_schema applied; ws_capture
monkey-patches plugin_api.broadcast_event so tests can assert on (kind, ...)
payloads without standing up a real WS server.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any

import pytest

from scenecraft import plugin_api
from scenecraft.db import get_db


@pytest.fixture
def tmp_project_dir(tmp_path: Path) -> Path:
    """Fresh project DB with full schema applied."""
    pd = tmp_path / "proj"
    pd.mkdir()
    get_db(pd)  # forces _ensure_schema
    return pd


@pytest.fixture
def tool_ctx(tmp_project_dir: Path) -> dict:
    return {"project_dir": tmp_project_dir, "project_name": "test"}


@pytest.fixture
def ws_capture(monkeypatch) -> list[dict]:
    """Capture every plugin_api.broadcast_event call. Tests inspect the list
    to assert (or assert absence of) emissions."""
    captured: list[dict] = []

    def _spy(plugin_id, event_type, *, project_name=None, payload=None):
        captured.append({
            "plugin_id": plugin_id,
            "event_type": event_type,
            "project_name": project_name,
            "payload": payload or {},
        })

    monkeypatch.setattr(plugin_api, "broadcast_event", _spy)
    return captured


def make_scene(ctx: dict, label: str, type_: str = "rotating_head", params: Any = None) -> dict:
    """Helper: invoke tools_scenes(action=set, create) and return the new scene row."""
    from scenecraft.plugins import light_show
    r = light_show.tools_scenes(
        {"action": "set", "scenes": [{"label": label, "type": type_, "params": params or {}}]},
        ctx,
    )
    assert "scenes" in r and len(r["scenes"]) == 1, f"unexpected response: {r}"
    return r["scenes"][0]


def make_placement(ctx: dict, scene_id: str, start: float, end: float) -> dict:
    from scenecraft.plugins import light_show
    r = light_show.tools_scene_timeline(
        {"action": "set", "placements": [{"scene_id": scene_id, "start_time": start, "end_time": end}]},
        ctx,
    )
    assert "placements" in r and len(r["placements"]) == 1
    return r["placements"][0]
