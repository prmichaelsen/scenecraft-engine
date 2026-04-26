"""tools_scenes MCP handler tests — covers spec R4-R11."""

from __future__ import annotations

from pathlib import Path

import pytest

from scenecraft.plugins import light_show
from scenecraft.plugins.light_show.tests.conftest import make_scene, make_placement


def test_scenes_list_primitives_returns_catalog_verbatim(tool_ctx: dict) -> None:
    """R4: list_primitives returns parsed catalog YAML wrapped as {primitives: [...]}."""
    r = light_show.tools_scenes({"action": "list_primitives"}, tool_ctx)
    assert "primitives" in r
    ids = [p["id"] for p in r["primitives"]]
    assert "rotating_head" in ids and "static_color" in ids


def test_scenes_set_creates_new_with_server_uuid(tool_ctx: dict) -> None:
    """R5, R6: create with no id assigns a uuid and returns sparse params."""
    r = light_show.tools_scenes(
        {"action": "set", "scenes": [{"label": "x", "type": "rotating_head", "params": {"period_sec": 6}}]},
        tool_ctx,
    )
    assert len(r["scenes"]) == 1
    s = r["scenes"][0]
    assert len(s["id"]) == 32, f"expected 32-char hex uuid, got {s['id']!r}"
    assert s["params"] == {"period_sec": 6}


def test_scenes_set_rejects_create_without_label_or_type(tool_ctx: dict) -> None:
    """R6: create requires both label and type."""
    r = light_show.tools_scenes({"action": "set", "scenes": [{"type": "rotating_head"}]}, tool_ctx)
    assert "error" in r and "label" in r["error"].lower()
    r = light_show.tools_scenes({"action": "set", "scenes": [{"label": "x"}]}, tool_ctx)
    assert "error" in r


def test_scenes_set_rejects_update_with_unknown_id(tool_ctx: dict) -> None:
    """R7: update with id that doesn't exist is rejected."""
    r = light_show.tools_scenes({"action": "set", "scenes": [{"id": "nope"}]}, tool_ctx)
    assert "error" in r and "unknown" in r["error"].lower()


def test_scenes_set_partial_update_preserves_omitted(tool_ctx: dict) -> None:
    """R6: omitted top-level fields preserve existing values."""
    s = make_scene(tool_ctx, "Slow", "rotating_head", {"period_sec": 6})
    r = light_show.tools_scenes(
        {"action": "set", "scenes": [{"id": s["id"], "label": "Updated"}]},
        tool_ctx,
    )
    out = r["scenes"][0]
    assert out["label"] == "Updated"
    assert out["type"] == "rotating_head"  # preserved
    assert out["params"] == {"period_sec": 6}  # preserved


def test_scenes_set_null_deletes_param_key(tool_ctx: dict) -> None:
    """R6: {params: {key: null}} deletes that key from sparse storage."""
    s = make_scene(tool_ctx, "Slow", "rotating_head", {"period_sec": 6, "intensity": 0.5})
    r = light_show.tools_scenes(
        {"action": "set", "scenes": [{"id": s["id"], "params": {"period_sec": None}}]},
        tool_ctx,
    )
    assert r["scenes"][0]["params"] == {"intensity": 0.5}


def test_scenes_set_rejects_null_on_top_level(tool_ctx: dict) -> None:
    """R6: null on label or type is rejected (NOT NULL columns)."""
    s = make_scene(tool_ctx, "x")
    r = light_show.tools_scenes(
        {"action": "set", "scenes": [{"id": s["id"], "label": None}]}, tool_ctx
    )
    assert "error" in r and "null" in r["error"].lower()
    r = light_show.tools_scenes(
        {"action": "set", "scenes": [{"id": s["id"], "type": None}]}, tool_ctx
    )
    assert "error" in r


def test_scenes_set_rejects_null_params_object(tool_ctx: dict) -> None:
    """R6: params: null is rejected (use {} to preserve, {key: null} to delete)."""
    s = make_scene(tool_ctx, "x")
    r = light_show.tools_scenes(
        {"action": "set", "scenes": [{"id": s["id"], "params": None}]}, tool_ctx
    )
    assert "error" in r and "params" in r["error"].lower() and "null" in r["error"].lower()


def test_scenes_roundtrip_list_set_preserves_sparse(tool_ctx: dict) -> None:
    """R5, R6: list returns sparse params; set with that exact value doesn't promote defaults."""
    s = make_scene(tool_ctx, "x", "rotating_head", {"period_sec": 6})
    r = light_show.tools_scenes({"action": "list"}, tool_ctx)
    listed = r["scenes"][0]
    assert listed["params"] == {"period_sec": 6}
    # Now re-upsert with the listed params — should still be sparse, not merged with catalog defaults
    r = light_show.tools_scenes(
        {"action": "set", "scenes": [{"id": s["id"], "params": listed["params"]}]},
        tool_ctx,
    )
    assert r["scenes"][0]["params"] == {"period_sec": 6}


def test_scenes_set_rejects_unknown_type(tool_ctx: dict) -> None:
    """R8: type not in catalog is rejected."""
    r = light_show.tools_scenes(
        {"action": "set", "scenes": [{"label": "x", "type": "fake_primitive"}]},
        tool_ctx,
    )
    assert "error" in r and "unknown primitive type" in r["error"].lower()


def test_scenes_remove_happy_path(tool_ctx: dict) -> None:
    """R9: remove unblocked scene returns the deleted row."""
    s = make_scene(tool_ctx, "x")
    r = light_show.tools_scenes({"action": "remove", "ids": [s["id"]]}, tool_ctx)
    assert r["scenes"][0]["id"] == s["id"]
    # Verify deletion
    r = light_show.tools_scenes({"action": "list"}, tool_ctx)
    assert r["total"] == 0


def test_scenes_remove_rejects_when_placements_reference(tool_ctx: dict) -> None:
    """R9: scene held by placements rejects with structured payload."""
    s = make_scene(tool_ctx, "x")
    p = make_placement(tool_ctx, s["id"], 0, 5)
    r = light_show.tools_scenes({"action": "remove", "ids": [s["id"]]}, tool_ctx)
    assert "error" in r
    assert "blocked" in r
    assert r["blocked"][0]["scene_id"] == s["id"]
    assert p["id"] in r["blocked"][0]["placement_ids"]


def test_scenes_remove_rejects_when_live_override_holds(tool_ctx: dict) -> None:
    """R10: scene held by live override rejects with blocked_by_live."""
    s = make_scene(tool_ctx, "x")
    light_show.tools_scene_live({"action": "activate", "scene_id": s["id"]}, tool_ctx)
    r = light_show.tools_scenes({"action": "remove", "ids": [s["id"]]}, tool_ctx)
    assert "error" in r
    assert r.get("blocked_by_live") == s["id"]


def test_scenes_remove_multiple_atomic_when_one_blocked(tool_ctx: dict) -> None:
    """R9 edge: when any id in the batch is blocked, NO deletes happen."""
    s_blocked = make_scene(tool_ctx, "blocked")
    s_free = make_scene(tool_ctx, "free")
    make_placement(tool_ctx, s_blocked["id"], 0, 5)
    r = light_show.tools_scenes(
        {"action": "remove", "ids": [s_blocked["id"], s_free["id"]]}, tool_ctx
    )
    assert "error" in r
    # Both still exist
    r = light_show.tools_scenes({"action": "list"}, tool_ctx)
    assert r["total"] == 2


def _seed(tool_ctx: dict, count: int, type_: str = "rotating_head") -> list[dict]:
    out = []
    for i in range(count):
        out.append(make_scene(tool_ctx, f"Scene {i:03d}", type_))
    return out


def test_scenes_list_default_pagination(tool_ctx: dict) -> None:
    """R5: default limit 50."""
    _seed(tool_ctx, 60)
    r = light_show.tools_scenes({"action": "list"}, tool_ctx)
    assert r["total"] == 60
    assert len(r["scenes"]) == 50
    assert r["has_more"] is True


def test_scenes_list_pagination_second_page(tool_ctx: dict) -> None:
    """R5: offset advances correctly."""
    _seed(tool_ctx, 60)
    r = light_show.tools_scenes({"action": "list", "limit": 50, "offset": 50}, tool_ctx)
    assert len(r["scenes"]) == 10
    assert r["has_more"] is False


def test_scenes_list_filter_by_type(tool_ctx: dict) -> None:
    """R5: filter.type exact match."""
    _seed(tool_ctx, 3, "rotating_head")
    _seed(tool_ctx, 2, "static_color")
    r = light_show.tools_scenes(
        {"action": "list", "filter": {"type": "static_color"}}, tool_ctx
    )
    assert r["total"] == 2
    assert all(s["type"] == "static_color" for s in r["scenes"])


def test_scenes_list_filter_by_label_query_substring_case_insensitive(tool_ctx: dict) -> None:
    """R5: label_query case-insensitive substring."""
    make_scene(tool_ctx, "Slow Rotating Head")
    make_scene(tool_ctx, "Fast Static")
    r = light_show.tools_scenes(
        {"action": "list", "filter": {"label_query": "rotating"}}, tool_ctx
    )
    assert r["total"] == 1
    r = light_show.tools_scenes(
        {"action": "list", "filter": {"label_query": "ROTATING"}}, tool_ctx
    )
    assert r["total"] == 1


def test_scenes_list_filter_by_ids(tool_ctx: dict) -> None:
    """R5: filter.ids exact lookup."""
    a = make_scene(tool_ctx, "a")
    b = make_scene(tool_ctx, "b")
    make_scene(tool_ctx, "c")
    r = light_show.tools_scenes(
        {"action": "list", "filter": {"ids": [a["id"], b["id"]]}}, tool_ctx
    )
    assert r["total"] == 2


def test_scenes_list_order_by_label_asc(tool_ctx: dict) -> None:
    """R5: order_by=label, order=asc."""
    make_scene(tool_ctx, "Cha")
    make_scene(tool_ctx, "Bbb")
    make_scene(tool_ctx, "Aaa")
    r = light_show.tools_scenes(
        {"action": "list", "order_by": "label", "order": "asc"}, tool_ctx
    )
    labels = [s["label"] for s in r["scenes"]]
    assert labels == ["Aaa", "Bbb", "Cha"]


def test_scenes_list_limit_clamped_to_max(tool_ctx: dict) -> None:
    """R5: limit > 500 clamps to 500 (no error)."""
    r = light_show.tools_scenes({"action": "list", "limit": 99999}, tool_ctx)
    assert "error" not in r  # clamps silently
    assert "total" in r


def test_scenes_remove_returns_deleted_rows(tool_ctx: dict) -> None:
    """R9: success returns deleted rows pre-deletion (NOT just ids)."""
    s = make_scene(tool_ctx, "Slow", "rotating_head", {"period_sec": 6})
    r = light_show.tools_scenes({"action": "remove", "ids": [s["id"]]}, tool_ctx)
    assert r["scenes"][0] == s  # full pre-deletion row


def test_scenes_unknown_action_returns_error(tool_ctx: dict) -> None:
    """R11: unknown action surfaces as error envelope, not exception."""
    r = light_show.tools_scenes({"action": "bogus"}, tool_ctx)
    assert "error" in r and "unknown action" in r["error"].lower()
