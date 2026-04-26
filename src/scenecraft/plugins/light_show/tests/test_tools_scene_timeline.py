"""tools_scene_timeline tests — covers spec R12-R17."""

from __future__ import annotations

from scenecraft.plugins import light_show
from scenecraft.plugins.light_show.tests.conftest import make_scene


def test_scene_timeline_set_inserts_with_auto_uuid(tool_ctx: dict) -> None:
    """R12, R13: missing id → auto uuid; row stored."""
    s = make_scene(tool_ctx, "x")
    r = light_show.tools_scene_timeline(
        {"action": "set", "placements": [{"scene_id": s["id"], "start_time": 0, "end_time": 5}]},
        tool_ctx,
    )
    assert len(r["placements"]) == 1
    p = r["placements"][0]
    assert len(p["id"]) == 32
    assert p["start_time"] == 0
    assert p["end_time"] == 5


def test_scene_timeline_set_rejects_end_before_start(tool_ctx: dict) -> None:
    """R14: end_time <= start_time rejected atomically."""
    s = make_scene(tool_ctx, "x")
    r = light_show.tools_scene_timeline(
        {"action": "set", "placements": [{"scene_id": s["id"], "start_time": 5, "end_time": 5}]},
        tool_ctx,
    )
    assert "error" in r and "end_time" in r["error"]
    # Verify nothing inserted
    r2 = light_show.tools_scene_timeline({"action": "list"}, tool_ctx)
    assert r2["total"] == 0


def test_scene_timeline_set_rejects_unknown_scene_id(tool_ctx: dict) -> None:
    """R15: unknown scene_id rejected atomically."""
    r = light_show.tools_scene_timeline(
        {"action": "set", "placements": [{"scene_id": "nope", "start_time": 0, "end_time": 5}]},
        tool_ctx,
    )
    assert "error" in r and "unknown scene_id" in r["error"]


def test_scene_timeline_list_default_chronological(tool_ctx: dict) -> None:
    """R12: default order = start_time asc."""
    s = make_scene(tool_ctx, "x")
    light_show.tools_scene_timeline(
        {
            "action": "set",
            "placements": [
                {"scene_id": s["id"], "start_time": 20, "end_time": 25},
                {"scene_id": s["id"], "start_time": 5, "end_time": 10},
                {"scene_id": s["id"], "start_time": 12, "end_time": 18},
            ],
        },
        tool_ctx,
    )
    r = light_show.tools_scene_timeline({"action": "list"}, tool_ctx)
    starts = [p["start_time"] for p in r["placements"]]
    assert starts == [5, 12, 20]


def test_scene_timeline_list_filter_time_range(tool_ctx: dict) -> None:
    """R12: time_range filter returns overlapping placements only."""
    s = make_scene(tool_ctx, "x")
    light_show.tools_scene_timeline(
        {
            "action": "set",
            "placements": [
                {"scene_id": s["id"], "start_time": 0, "end_time": 5},
                {"scene_id": s["id"], "start_time": 10, "end_time": 20},
                {"scene_id": s["id"], "start_time": 25, "end_time": 30},
            ],
        },
        tool_ctx,
    )
    r = light_show.tools_scene_timeline(
        {"action": "list", "filter": {"time_range": {"start": 12, "end": 15}}},
        tool_ctx,
    )
    # Only the 10-20 placement overlaps [12, 15]
    assert r["total"] == 1
    assert r["placements"][0]["start_time"] == 10


def test_scene_timeline_list_filter_by_scene_id(tool_ctx: dict) -> None:
    """R12: filter.scene_id."""
    s1 = make_scene(tool_ctx, "a")
    s2 = make_scene(tool_ctx, "b")
    light_show.tools_scene_timeline(
        {"action": "set", "placements": [
            {"scene_id": s1["id"], "start_time": 0, "end_time": 5},
            {"scene_id": s2["id"], "start_time": 10, "end_time": 15},
        ]},
        tool_ctx,
    )
    r = light_show.tools_scene_timeline(
        {"action": "list", "filter": {"scene_id": s1["id"]}}, tool_ctx
    )
    assert r["total"] == 1
    assert r["placements"][0]["scene_id"] == s1["id"]


def test_scene_timeline_set_returns_upserted_only(tool_ctx: dict) -> None:
    """R13: response contains only the rows just upserted, NOT the full list."""
    s = make_scene(tool_ctx, "x")
    light_show.tools_scene_timeline(
        {"action": "set", "placements": [{"scene_id": s["id"], "start_time": 0, "end_time": 5}]},
        tool_ctx,
    )
    light_show.tools_scene_timeline(
        {"action": "set", "placements": [{"scene_id": s["id"], "start_time": 10, "end_time": 15}]},
        tool_ctx,
    )
    # Total should be 2 in DB; second set returned only the new one
    r2 = light_show.tools_scene_timeline(
        {"action": "set", "placements": [{"scene_id": s["id"], "start_time": 20, "end_time": 25}]},
        tool_ctx,
    )
    assert len(r2["placements"]) == 1
    assert r2["placements"][0]["start_time"] == 20


def test_scene_timeline_remove_returns_deleted_rows(tool_ctx: dict) -> None:
    """R16: returns deleted rows, silently skips missing."""
    s = make_scene(tool_ctx, "x")
    r = light_show.tools_scene_timeline(
        {"action": "set", "placements": [{"scene_id": s["id"], "start_time": 0, "end_time": 5}]},
        tool_ctx,
    )
    pid = r["placements"][0]["id"]
    r = light_show.tools_scene_timeline(
        {"action": "remove", "ids": [pid, "nonexistent"]},
        tool_ctx,
    )
    # Returns only the one that existed
    assert len(r["placements"]) == 1
    assert r["placements"][0]["id"] == pid


def test_negative_no_partial_placement_write_on_multi_invalid(tool_ctx: dict) -> None:
    """R14 edge: bulk set with one invalid rejects the whole batch."""
    s = make_scene(tool_ctx, "x")
    r = light_show.tools_scene_timeline(
        {"action": "set", "placements": [
            {"scene_id": s["id"], "start_time": 0, "end_time": 5},  # valid
            {"scene_id": s["id"], "start_time": 10, "end_time": 5},  # invalid (end < start)
        ]},
        tool_ctx,
    )
    assert "error" in r
    r = light_show.tools_scene_timeline({"action": "list"}, tool_ctx)
    assert r["total"] == 0  # no partial write


def test_scene_timeline_unknown_action(tool_ctx: dict) -> None:
    """R17."""
    r = light_show.tools_scene_timeline({"action": "bogus"}, tool_ctx)
    assert "error" in r and "unknown action" in r["error"].lower()
