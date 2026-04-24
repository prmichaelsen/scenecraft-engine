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


def tools_set_rig_layout(args, tool_context) -> dict:
    from scenecraft import plugin_api
    project_dir = tool_context["project_dir"]
    fixtures = args.get("fixtures") or []
    if not isinstance(fixtures, list):
        return {"error": "fixtures must be a list"}
    try:
        updated = plugin_api.upsert_light_show_fixtures(project_dir, fixtures)
    except ValueError as e:
        return {"error": str(e)}
    return {"fixtures": updated}


def tools_list_fixtures(args, tool_context) -> dict:
    del args
    from scenecraft import plugin_api
    project_dir = tool_context["project_dir"]
    fixtures = plugin_api.list_light_show_fixtures(project_dir)
    return {"fixtures": fixtures}


def tools_reset_rig(args, tool_context) -> dict:
    del args
    from scenecraft import plugin_api
    project_dir = tool_context["project_dir"]
    fixtures = plugin_api.reset_light_show_fixtures(project_dir)
    return {"fixtures": fixtures}


def tools_remove_fixtures(args, tool_context) -> dict:
    from scenecraft import plugin_api
    project_dir = tool_context["project_dir"]
    ids = args.get("ids") or []
    if not isinstance(ids, list):
        return {"error": "ids must be a list"}
    fixtures = plugin_api.remove_light_show_fixtures(project_dir, [str(i) for i in ids])
    return {"fixtures": fixtures}


def tools_set_fixture_state(args, tool_context) -> dict:
    """Override per-fixture channel values (intensity, color, pan, tilt).
    Overrides win over scene output until cleared. Each override entry
    MUST include ``id``; fields not specified stay at whatever they were
    in the existing override (or NULL / scene-driven)."""
    from scenecraft import plugin_api
    project_dir = tool_context["project_dir"]
    overrides = args.get("overrides") or []
    if not isinstance(overrides, list):
        return {"error": "overrides must be a list"}
    try:
        rows = plugin_api.set_light_show_overrides(project_dir, overrides)
    except ValueError as e:
        return {"error": str(e)}
    return {"overrides": rows}


def tools_list_overrides(args, tool_context) -> dict:
    del args
    from scenecraft import plugin_api
    project_dir = tool_context["project_dir"]
    return {"overrides": plugin_api.list_light_show_overrides(project_dir)}


def tools_clear_overrides(args, tool_context) -> dict:
    """Clear overrides, restoring scene-driven channel values. If ``ids``
    is provided, only clears those fixtures; otherwise clears everything."""
    from scenecraft import plugin_api
    project_dir = tool_context["project_dir"]
    ids = args.get("ids") or []
    if not isinstance(ids, list):
        return {"error": "ids must be a list"}
    rows = plugin_api.clear_light_show_overrides(project_dir, [str(i) for i in ids] or None)
    return {"overrides": rows}
