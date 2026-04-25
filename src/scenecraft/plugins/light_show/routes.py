"""REST route handlers for light_show.

GET  /api/projects/:name/plugins/light_show/fixtures  → list current rig
PUT  /api/projects/:name/plugins/light_show/fixtures  → bulk upsert (partial)
POST /api/projects/:name/plugins/light_show/fixtures/reset → re-seed defaults

Matches the ``api_server`` dispatch signature used by the other plugins
(``generate-music``, ``isolate_vocals``): each handler receives
``(path: str, project_dir: Path, project_name: str, body|query: dict)``.
"""

from __future__ import annotations

from pathlib import Path

from scenecraft.plugins.light_show import PLUGIN_ID


def _broadcast_changed(project_name: str, kind: str) -> None:
    """Push a 'changed' event scoped to this plugin. Lets the panel refresh
    immediately on chat-driven changes instead of waiting for its next 2s
    poll tick. Emits WS type ``light_show__changed`` (plugin-namespaced
    via plugin_api.broadcast_event).

    ``kind`` is one of 'fixtures' or 'overrides' — the frontend may
    choose to refetch only the relevant endpoint.
    """
    from scenecraft import plugin_api
    plugin_api.broadcast_event(
        PLUGIN_ID,
        "changed",
        project_name=project_name,
        payload={"kind": kind},
    )


def _handle_list(path: str, project_dir: Path, project_name: str, query: dict) -> dict:
    del path, project_name, query
    from scenecraft import plugin_api
    return {"fixtures": plugin_api.list_light_show_fixtures(project_dir)}


def _handle_upsert(path: str, project_dir: Path, project_name: str, body: dict) -> dict:
    del path
    from scenecraft import plugin_api
    body = body or {}
    fixtures = body.get("fixtures") or []
    if not isinstance(fixtures, list):
        return {"error": "fixtures must be a list"}
    try:
        updated = plugin_api.upsert_light_show_fixtures(project_dir, fixtures)
    except ValueError as e:
        return {"error": str(e)}
    _broadcast_changed(project_name, "fixtures")
    return {"fixtures": updated}


def _handle_reset(path: str, project_dir: Path, project_name: str, body: dict) -> dict:
    del path, body
    from scenecraft import plugin_api
    result = plugin_api.reset_light_show_fixtures(project_dir)
    _broadcast_changed(project_name, "fixtures")
    return {"fixtures": result}


# --- overrides ------------------------------------------------------------


def _handle_list_overrides(path: str, project_dir: Path, project_name: str, query: dict) -> dict:
    del path, project_name, query
    from scenecraft import plugin_api
    return {"overrides": plugin_api.list_light_show_overrides(project_dir)}


def _handle_set_overrides(path: str, project_dir: Path, project_name: str, body: dict) -> dict:
    del path
    from scenecraft import plugin_api
    body = body or {}
    overrides = body.get("overrides") or []
    if not isinstance(overrides, list):
        return {"error": "overrides must be a list"}
    try:
        rows = plugin_api.set_light_show_overrides(project_dir, overrides)
    except ValueError as e:
        return {"error": str(e)}
    _broadcast_changed(project_name, "overrides")
    return {"overrides": rows}


def _handle_clear_overrides(path: str, project_dir: Path, project_name: str, body: dict) -> dict:
    del path
    from scenecraft import plugin_api
    body = body or {}
    ids = body.get("ids") or []
    if not isinstance(ids, list):
        return {"error": "ids must be a list"}
    rows = plugin_api.clear_light_show_overrides(project_dir, [str(i) for i in ids] or None)
    _broadcast_changed(project_name, "overrides")
    return {"overrides": rows}


# --- screens --------------------------------------------------------------


def _handle_list_screens(path: str, project_dir: Path, project_name: str, query: dict) -> dict:
    del path, project_name, query
    from scenecraft import plugin_api
    return {"screens": plugin_api.list_light_show_screens(project_dir)}


def _handle_upsert_screens(path: str, project_dir: Path, project_name: str, body: dict) -> dict:
    del path
    from scenecraft import plugin_api
    body = body or {}
    screens = body.get("screens") or []
    if not isinstance(screens, list):
        return {"error": "screens must be a list"}
    try:
        rows = plugin_api.upsert_light_show_screens(project_dir, screens)
    except ValueError as e:
        return {"error": str(e)}
    _broadcast_changed(project_name, "screens")
    return {"screens": rows}


def _handle_remove_screens(path: str, project_dir: Path, project_name: str, body: dict) -> dict:
    del path
    from scenecraft import plugin_api
    body = body or {}
    ids = body.get("ids") or []
    if not isinstance(ids, list):
        return {"error": "ids must be a list"}
    rows = plugin_api.remove_light_show_screens(project_dir, [str(i) for i in ids])
    _broadcast_changed(project_name, "screens")
    return {"screens": rows}


def _handle_reset_screens(path: str, project_dir: Path, project_name: str, body: dict) -> dict:
    del path, body
    from scenecraft import plugin_api
    rows = plugin_api.reset_light_show_screens(project_dir)
    _broadcast_changed(project_name, "screens")
    return {"screens": rows}


def register(plugin_api, context) -> None:
    """Wire REST endpoints into the plugin-host's dispatch tables."""
    # Fixtures
    plugin_api.register_rest_endpoint(
        r"^/api/projects/[^/]+/plugins/light_show/fixtures$",
        _handle_list,
        method="GET",
        context=context,
    )
    plugin_api.register_rest_endpoint(
        r"^/api/projects/[^/]+/plugins/light_show/fixtures$",
        _handle_upsert,
        method="PUT",
        context=context,
    )
    plugin_api.register_rest_endpoint(
        r"^/api/projects/[^/]+/plugins/light_show/fixtures/reset$",
        _handle_reset,
        method="POST",
        context=context,
    )
    # Overrides
    plugin_api.register_rest_endpoint(
        r"^/api/projects/[^/]+/plugins/light_show/overrides$",
        _handle_list_overrides,
        method="GET",
        context=context,
    )
    plugin_api.register_rest_endpoint(
        r"^/api/projects/[^/]+/plugins/light_show/overrides$",
        _handle_set_overrides,
        method="PUT",
        context=context,
    )
    plugin_api.register_rest_endpoint(
        r"^/api/projects/[^/]+/plugins/light_show/overrides/clear$",
        _handle_clear_overrides,
        method="POST",
        context=context,
    )
    # Screens
    plugin_api.register_rest_endpoint(
        r"^/api/projects/[^/]+/plugins/light_show/screens$",
        _handle_list_screens,
        method="GET",
        context=context,
    )
    plugin_api.register_rest_endpoint(
        r"^/api/projects/[^/]+/plugins/light_show/screens$",
        _handle_upsert_screens,
        method="PUT",
        context=context,
    )
    plugin_api.register_rest_endpoint(
        r"^/api/projects/[^/]+/plugins/light_show/screens/remove$",
        _handle_remove_screens,
        method="POST",
        context=context,
    )
    plugin_api.register_rest_endpoint(
        r"^/api/projects/[^/]+/plugins/light_show/screens/reset$",
        _handle_reset_screens,
        method="POST",
        context=context,
    )
