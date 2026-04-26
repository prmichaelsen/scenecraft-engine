"""tools_scene_live tests — covers spec R18-R28."""

from __future__ import annotations

from pathlib import Path

import pytest

from scenecraft.db import get_db
from scenecraft.plugins import light_show
from scenecraft.plugins.light_show.tests.conftest import make_scene


def test_scene_live_activate_by_scene_id(tool_ctx: dict) -> None:
    """R18, R19, R21, R26."""
    s = make_scene(tool_ctx, "x")
    r = light_show.tools_scene_live(
        {"action": "activate", "scene_id": s["id"], "fade_in_sec": 1.5}, tool_ctx
    )
    assert r["active"] is True
    assert r["scene_id"] == s["id"]
    assert r["fade_in_sec"] == 1.5
    # status reads back
    s2 = light_show.tools_scene_live({"action": "status"}, tool_ctx)
    assert s2["active"] is True and s2["scene_id"] == s["id"]


def test_scene_live_activate_with_inline_scene(tool_ctx: dict) -> None:
    """R18, R20, R22."""
    r = light_show.tools_scene_live(
        {"action": "activate", "scene": {"type": "rotating_head", "params": {"period_sec": 4}}},
        tool_ctx,
    )
    assert r["active"] is True
    assert r["inline_type"] == "rotating_head"
    assert r["inline_params"] == {"period_sec": 4}


def test_scene_live_activate_rejects_both_forms(tool_ctx: dict) -> None:
    """R18: scene_id + inline both → reject."""
    s = make_scene(tool_ctx, "x")
    r = light_show.tools_scene_live(
        {
            "action": "activate",
            "scene_id": s["id"],
            "scene": {"type": "rotating_head", "params": {}},
        },
        tool_ctx,
    )
    assert "error" in r and "not both" in r["error"].lower()


def test_scene_live_activate_save_as_persists(tool_ctx: dict) -> None:
    """R22: save_as creates a new scene row referenced by the override."""
    r = light_show.tools_scene_live(
        {
            "action": "activate",
            "scene": {"type": "static_color", "params": {"color": [0, 1, 0]}},
            "save_as": "Saved Green",
        },
        tool_ctx,
    )
    assert r["active"] is True
    assert r["scene_id"]  # new uuid
    # Verify scene appears in library
    listed = light_show.tools_scenes({"action": "list"}, tool_ctx)
    saved = next((s for s in listed["scenes"] if s["id"] == r["scene_id"]), None)
    assert saved is not None
    assert saved["label"] == "Saved Green"
    assert saved["params"] == {"color": [0, 1, 0]}


def test_scene_live_activate_replaces_existing(tool_ctx: dict) -> None:
    """R21: subsequent activate silently replaces."""
    s1 = make_scene(tool_ctx, "a")
    s2 = make_scene(tool_ctx, "b")
    light_show.tools_scene_live({"action": "activate", "scene_id": s1["id"]}, tool_ctx)
    r = light_show.tools_scene_live({"action": "activate", "scene_id": s2["id"]}, tool_ctx)
    assert r["scene_id"] == s2["id"]


def test_scene_live_deactivate_no_op_when_inactive(tool_ctx: dict) -> None:
    """R25: deactivate when nothing active returns {active: False}."""
    r = light_show.tools_scene_live({"action": "deactivate"}, tool_ctx)
    assert r == {"active": False}


def test_scene_live_save_as_requires_inline(tool_ctx: dict) -> None:
    """R23: save_as without inline scene → reject."""
    s = make_scene(tool_ctx, "x")
    r = light_show.tools_scene_live(
        {"action": "activate", "scene_id": s["id"], "save_as": "Bad"},
        tool_ctx,
    )
    assert "error" in r and "save_as" in r["error"].lower()


def test_live_override_persists_across_restart(tmp_project_dir: Path) -> None:
    """R28: override survives 'restart' (connection re-open).

    The SQLite project DB is on disk; closing+reopening the connection
    is the in-process analog of an engine restart. We open a brand-new
    sqlite3 connection bypassing get_db's cache to confirm the row is
    physically persisted, then verify status reads back correctly via
    the normal API.
    """
    import sqlite3
    ctx = {"project_dir": tmp_project_dir, "project_name": "test"}
    s = make_scene(ctx, "x")
    light_show.tools_scene_live({"action": "activate", "scene_id": s["id"]}, ctx)

    # Bypass the cached connection: open the DB file directly.
    db_path = tmp_project_dir / "project.db"
    fresh = sqlite3.connect(db_path)
    fresh.row_factory = sqlite3.Row
    row = fresh.execute(
        "SELECT scene_id FROM light_show__live_override WHERE id = 'current'"
    ).fetchone()
    assert row is not None and row["scene_id"] == s["id"]
    fresh.close()

    # Verify the API path also reads it back correctly.
    r = light_show.tools_scene_live({"action": "status"}, ctx)
    assert r["active"] is True and r["scene_id"] == s["id"]


def test_scene_live_unknown_action(tool_ctx: dict) -> None:
    """R27."""
    r = light_show.tools_scene_live({"action": "bogus"}, tool_ctx)
    assert "error" in r and "unknown action" in r["error"].lower()


def test_negative_no_broadcast_on_rejected_set(tool_ctx: dict, ws_capture: list) -> None:
    """When an upsert is rejected (e.g. unknown id), no WS broadcast fires."""
    light_show.tools_scenes({"action": "set", "scenes": [{"id": "nope"}]}, tool_ctx)
    # Filter for scenes-kind broadcasts
    scene_events = [e for e in ws_capture if e["payload"].get("kind") == "scenes"]
    assert scene_events == []


def test_ws_broadcast_kind_on_each_mutation(tool_ctx: dict, ws_capture: list) -> None:
    """R29: each mutation emits the correct kind."""
    s = make_scene(tool_ctx, "x")  # scenes
    make_scene  # noqa
    light_show.tools_scene_timeline(
        {"action": "set", "placements": [{"scene_id": s["id"], "start_time": 0, "end_time": 5}]},
        tool_ctx,
    )  # placements
    light_show.tools_scene_live({"action": "activate", "scene_id": s["id"]}, tool_ctx)  # live

    kinds = [e["payload"]["kind"] for e in ws_capture]
    assert "scenes" in kinds
    assert "placements" in kinds
    assert "live" in kinds
