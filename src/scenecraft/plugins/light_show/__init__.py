"""light_show plugin — DMX light-show authoring (MVP).

MVP scope is rig-only: fixtures persist in ``light_show__fixtures``, a single
REST endpoint pair serves list/upsert, and four MCP tools let chat
interactively edit the rig. Scenes / timeline / real DMX output are
deliberately out of scope at this stage — the frontend 3D preview panel
runs its own hardcoded scene functions and reads fixtures from this plugin.

Entry point: ``activate(plugin_api, context)`` called by ``PluginHost.register``
at server startup. Tools resolve handlers via the manifest
(``PluginHost.register_declared``); REST endpoints register imperatively.
"""

from __future__ import annotations

import sys

PLUGIN_ID = "light_show"

__all__ = [
    "activate",
    "deactivate",
    "PLUGIN_ID",
    # Tool handlers (referenced by plugin.yaml handler refs as "backend:tools_*")
    "tools_set_rig_layout",
    "tools_list_fixtures",
    "tools_reset_rig",
    "tools_remove_fixtures",
    "tools_set_fixture_state",
    "tools_list_overrides",
    "tools_clear_overrides",
    "tools_screens",
    # M19 scene editor tools
    "tools_scenes",
    "tools_scene_timeline",
    "tools_scene_live",
]


def activate(plugin_api, context=None) -> None:
    """Plugin activation hook — wires REST endpoints + declarative MCP tools."""
    from scenecraft.plugin_host import PluginHost
    from scenecraft.plugins.light_show import routes

    # Imperative wiring — REST endpoints (plugin.yaml does not yet cover REST).
    routes.register(plugin_api, context)

    # Declarative wiring — MCP tools from manifest, handlers resolved against
    # this module's exported attributes.
    if context is not None:
        PluginHost.register_declared(sys.modules[__name__], context)


def deactivate(context) -> None:
    """Optional plugin-level deactivate hook. Most cleanup flows through
    ``context.subscriptions`` disposed by the host in LIFO order; nothing
    extra to tear down for the MVP scope."""
    del context


# --- MCP tool handlers ----------------------------------------------------
# Handlers take (args: dict, tool_context: dict) and return a JSON-serializable
# dict. tool_context is built by chat.py dispatch and includes project_dir +
# project_name among other things.


def _ctx(tool_context):
    """Extract project_dir + project_name from the tool dispatch context.
    project_name falls back to the directory basename if the chat
    dispatcher didn't inject it explicitly."""
    project_dir = tool_context["project_dir"]
    project_name = tool_context.get("project_name") or project_dir.name
    return project_dir, project_name


def _notify(project_name: str, kind: str) -> None:
    """Mirror the routes._broadcast_changed helper so tool-driven changes
    push WS events the same way REST-driven ones do."""
    from scenecraft.plugins.light_show.routes import _broadcast_changed
    _broadcast_changed(project_name, kind)


def tools_set_rig_layout(args, tool_context) -> dict:
    from scenecraft import plugin_api
    project_dir, project_name = _ctx(tool_context)
    fixtures = args.get("fixtures") or []
    if not isinstance(fixtures, list):
        return {"error": "fixtures must be a list"}
    try:
        updated = plugin_api.upsert_light_show_fixtures(project_dir, fixtures)
    except ValueError as e:
        return {"error": str(e)}
    _notify(project_name, "fixtures")
    return {"fixtures": updated}


def tools_list_fixtures(args, tool_context) -> dict:
    del args
    from scenecraft import plugin_api
    project_dir, _ = _ctx(tool_context)
    fixtures = plugin_api.list_light_show_fixtures(project_dir)
    return {"fixtures": fixtures}


def tools_reset_rig(args, tool_context) -> dict:
    del args
    from scenecraft import plugin_api
    project_dir, project_name = _ctx(tool_context)
    fixtures = plugin_api.reset_light_show_fixtures(project_dir)
    _notify(project_name, "fixtures")
    return {"fixtures": fixtures}


def tools_remove_fixtures(args, tool_context) -> dict:
    from scenecraft import plugin_api
    project_dir, project_name = _ctx(tool_context)
    ids = args.get("ids") or []
    if not isinstance(ids, list):
        return {"error": "ids must be a list"}
    fixtures = plugin_api.remove_light_show_fixtures(project_dir, [str(i) for i in ids])
    _notify(project_name, "fixtures")
    return {"fixtures": fixtures}


def tools_set_fixture_state(args, tool_context) -> dict:
    """Override per-fixture channel values (intensity, color, pan, tilt).
    Overrides win over scene output until cleared. Each override entry
    MUST include ``id``; fields not specified stay at whatever they were
    in the existing override (or NULL / scene-driven)."""
    from scenecraft import plugin_api
    project_dir, project_name = _ctx(tool_context)
    overrides = args.get("overrides") or []
    if not isinstance(overrides, list):
        return {"error": "overrides must be a list"}
    try:
        rows = plugin_api.set_light_show_overrides(project_dir, overrides)
    except ValueError as e:
        return {"error": str(e)}
    _notify(project_name, "overrides")
    return {"overrides": rows}


def tools_list_overrides(args, tool_context) -> dict:
    del args
    from scenecraft import plugin_api
    project_dir, _ = _ctx(tool_context)
    return {"overrides": plugin_api.list_light_show_overrides(project_dir)}


def tools_clear_overrides(args, tool_context) -> dict:
    """Clear overrides, restoring scene-driven channel values. If ``ids``
    is provided, only clears those fixtures; otherwise clears everything."""
    from scenecraft import plugin_api
    project_dir, project_name = _ctx(tool_context)
    ids = args.get("ids") or []
    if not isinstance(ids, list):
        return {"error": "ids must be a list"}
    rows = plugin_api.clear_light_show_overrides(project_dir, [str(i) for i in ids] or None)
    _notify(project_name, "overrides")
    return {"overrides": rows}


def tools_screens(args, tool_context) -> dict:
    """Single action-dispatched tool for video screens in the 3D preview.

    Actions:
      - ``list``: return current screens.
      - ``set``: bulk upsert by id with partial-state semantics (omitted
        fields preserve existing values; unknown ids create new screens
        defaulting to a 4x2.25m panel at the origin).
      - ``remove``: delete screens by id list.
      - ``reset``: delete ALL screens.

    At MVP every screen renders the same scenecraft main-timeline frame
    preview — per-screen timelines are a follow-up.
    """
    from scenecraft import plugin_api
    project_dir, project_name = _ctx(tool_context)
    action = args.get("action")
    if action == "list":
        return {"screens": plugin_api.list_light_show_screens(project_dir)}
    if action == "set":
        screens = args.get("screens") or []
        if not isinstance(screens, list):
            return {"error": "screens must be a list"}
        try:
            rows = plugin_api.upsert_light_show_screens(project_dir, screens)
        except ValueError as e:
            return {"error": str(e)}
        _notify(project_name, "screens")
        return {"screens": rows}
    if action == "remove":
        ids = args.get("ids") or []
        if not isinstance(ids, list):
            return {"error": "ids must be a list"}
        rows = plugin_api.remove_light_show_screens(project_dir, [str(i) for i in ids])
        _notify(project_name, "screens")
        return {"screens": rows}
    if action == "reset":
        rows = plugin_api.reset_light_show_screens(project_dir)
        _notify(project_name, "screens")
        return {"screens": rows}
    return {"error": f"unknown action {action!r}; expected one of list/set/remove/reset"}


# ── M19: Scene editor MCP tool handlers ──────────────────────────────────


_VALID_SCENES_ORDER_BY = {"updated_at", "created_at", "label"}
_VALID_PLACEMENTS_ORDER_BY = {"start_time", "created_at"}
_VALID_ORDER = {"asc", "desc"}


def _scenes_list_args(args: dict) -> dict:
    """Coerce + validate list arguments from the action payload. Returns a
    kwargs dict ready for plugin_api.list_light_show_scenes, or raises
    ValueError on invalid enum / out-of-range pagination."""
    f = args.get("filter") or {}
    if not isinstance(f, dict):
        raise ValueError("filter must be an object")
    ids = f.get("ids")
    if ids is not None and not isinstance(ids, list):
        raise ValueError("filter.ids must be a list of strings")

    order_by = args.get("order_by", "updated_at")
    order = args.get("order", "desc")
    if order_by not in _VALID_SCENES_ORDER_BY:
        raise ValueError(f"order_by must be one of {sorted(_VALID_SCENES_ORDER_BY)}")
    if order not in _VALID_ORDER:
        raise ValueError(f"order must be one of {sorted(_VALID_ORDER)}")

    limit = args.get("limit", 50)
    try:
        limit = max(0, min(int(limit), 500))
    except (TypeError, ValueError):
        raise ValueError("limit must be a non-negative integer")
    offset = args.get("offset", 0)
    try:
        offset = max(0, int(offset))
    except (TypeError, ValueError):
        raise ValueError("offset must be a non-negative integer")

    return {
        "ids": ids or None,
        "type_filter": f.get("type"),
        "label_query": f.get("label_query"),
        "limit": limit,
        "offset": offset,
        "order_by": order_by,
        "order": order,
    }


def tools_scenes(args, tool_context) -> dict:
    """Action-dispatched MCP tool for the scene library.

    Actions per spec:
      - ``list``                — paginated/filtered query over scenes
      - ``list_primitives``     — return parsed primitives_catalog.yaml verbatim
      - ``set``                 — bulk upsert with id-presence dispatch
                                  (RFC 7396 JSON Merge Patch on params)
      - ``remove``              — atomic batch delete with reference-blocking

    Mutating actions (``set``, ``remove``) emit a ``light_show__changed`` WS
    event with ``kind: "scenes"``.
    """
    from scenecraft import plugin_api
    from scenecraft.plugins.light_show.routes import _load_catalog
    project_dir, project_name = _ctx(tool_context)
    action = args.get("action")

    if action == "list":
        try:
            kwargs = _scenes_list_args(args)
        except ValueError as e:
            return {"error": str(e)}
        rows, total, has_more = plugin_api.list_light_show_scenes(project_dir, **kwargs)
        return {"scenes": rows, "total": total, "has_more": has_more}

    if action == "list_primitives":
        return _load_catalog()

    if action == "set":
        scenes = args.get("scenes")
        if not isinstance(scenes, list):
            return {"error": "scenes must be a list"}
        known_types = {p["id"] for p in _load_catalog().get("primitives", []) if "id" in p}
        try:
            out = plugin_api.upsert_light_show_scenes(
                project_dir, scenes, known_types=known_types
            )
        except ValueError as e:
            return {"error": str(e)}
        _notify(project_name, "scenes")
        return {"scenes": out}

    if action == "remove":
        ids = args.get("ids")
        if not isinstance(ids, list):
            return {"error": "ids must be a list"}
        try:
            deleted = plugin_api.remove_light_show_scenes(project_dir, [str(i) for i in ids])
        except plugin_api.BlockedByLiveError as e:
            return {
                "error": "scene held by live override; deactivate first",
                "blocked_by_live": e.scene_id,
            }
        except plugin_api.BlockedByPlacementsError as e:
            return {
                "error": "scene(s) still referenced",
                "blocked": e.blocked,
            }
        except ValueError as e:
            return {"error": str(e)}
        _notify(project_name, "scenes")
        return {"scenes": deleted}

    return {
        "error": f"unknown action {action!r}; expected one of "
        "list/list_primitives/set/remove"
    }


def _placements_list_args(args: dict) -> dict:
    """Coerce + validate list arguments for placements."""
    f = args.get("filter") or {}
    if not isinstance(f, dict):
        raise ValueError("filter must be an object")
    ids = f.get("ids")
    if ids is not None and not isinstance(ids, list):
        raise ValueError("filter.ids must be a list of strings")
    tr = f.get("time_range")
    time_start = time_end = None
    if tr is not None:
        if not isinstance(tr, dict) or "start" not in tr or "end" not in tr:
            raise ValueError("filter.time_range must be {start, end}")
        time_start = float(tr["start"])
        time_end = float(tr["end"])

    order_by = args.get("order_by", "start_time")
    order = args.get("order", "asc")
    if order_by not in _VALID_PLACEMENTS_ORDER_BY:
        raise ValueError(f"order_by must be one of {sorted(_VALID_PLACEMENTS_ORDER_BY)}")
    if order not in _VALID_ORDER:
        raise ValueError(f"order must be one of {sorted(_VALID_ORDER)}")

    limit = args.get("limit", 100)
    try:
        limit = max(0, min(int(limit), 1000))
    except (TypeError, ValueError):
        raise ValueError("limit must be a non-negative integer")
    offset = args.get("offset", 0)
    try:
        offset = max(0, int(offset))
    except (TypeError, ValueError):
        raise ValueError("offset must be a non-negative integer")

    return {
        "ids": ids or None,
        "scene_id": f.get("scene_id"),
        "time_start": time_start,
        "time_end": time_end,
        "limit": limit,
        "offset": offset,
        "order_by": order_by,
        "order": order,
    }


def tools_scene_timeline(args, tool_context) -> dict:
    """Action-dispatched MCP tool for placements (timeline schedule).

    Actions per spec:
      - ``list``                — paginated/filtered query
      - ``set``                 — bulk upsert with id-presence dispatch
      - ``remove``              — silently-skips-missing batch delete

    Mutating actions emit ``light_show__changed`` with ``kind: "placements"``.
    """
    from scenecraft import plugin_api
    project_dir, project_name = _ctx(tool_context)
    action = args.get("action")

    if action == "list":
        try:
            kwargs = _placements_list_args(args)
        except ValueError as e:
            return {"error": str(e)}
        rows, total, has_more = plugin_api.list_light_show_placements(project_dir, **kwargs)
        return {"placements": rows, "total": total, "has_more": has_more}

    if action == "set":
        placements = args.get("placements")
        if not isinstance(placements, list):
            return {"error": "placements must be a list"}
        try:
            out = plugin_api.upsert_light_show_placements(project_dir, placements)
        except ValueError as e:
            return {"error": str(e)}
        _notify(project_name, "placements")
        return {"placements": out}

    if action == "remove":
        ids = args.get("ids")
        if not isinstance(ids, list):
            return {"error": "ids must be a list"}
        deleted = plugin_api.remove_light_show_placements(project_dir, [str(i) for i in ids])
        _notify(project_name, "placements")
        return {"placements": deleted}

    return {"error": f"unknown action {action!r}; expected one of list/set/remove"}


def tools_scene_live(args, tool_context) -> dict:
    """Action-dispatched MCP tool for the singleton live override.

    Actions per spec:
      - ``activate``    — set or replace the live override (scene_id XOR inline)
      - ``deactivate``  — start fade-out (evaluator finalizes the row delete)
      - ``status``      — query current state

    Mutating actions emit ``light_show__changed`` with ``kind: "live"``.
    """
    from scenecraft import plugin_api
    from scenecraft.plugins.light_show.routes import _load_catalog
    project_dir, project_name = _ctx(tool_context)
    action = args.get("action")

    if action == "status":
        row = plugin_api.get_light_show_live_override(project_dir)
        if row is None:
            return {"active": False}
        return {"active": True, **row}

    if action == "activate":
        known_types = {p["id"] for p in _load_catalog().get("primitives", []) if "id" in p}
        try:
            row = plugin_api.activate_light_show_live_override(
                project_dir, args, known_types=known_types
            )
        except ValueError as e:
            return {"error": str(e)}
        _notify(project_name, "live")
        return {"active": True, **row}

    if action == "deactivate":
        fade = args.get("fade_out_sec", 0)
        try:
            fade_f = float(fade)
        except (TypeError, ValueError):
            return {"error": "fade_out_sec must be a number"}
        row = plugin_api.deactivate_light_show_live_override(project_dir, fade_out_sec=fade_f)
        _notify(project_name, "live")
        if isinstance(row, dict) and row.get("active") is False:
            return {"active": False}
        return {"active": True, **row}

    return {"error": f"unknown action {action!r}; expected one of activate/deactivate/status"}
