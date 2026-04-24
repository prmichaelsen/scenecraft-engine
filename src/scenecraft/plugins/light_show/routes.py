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


def _handle_list(path: str, project_dir: Path, project_name: str, query: dict) -> dict:
    del path, project_name, query
    from scenecraft import plugin_api
    return {"fixtures": plugin_api.list_light_show_fixtures(project_dir)}


def _handle_upsert(path: str, project_dir: Path, project_name: str, body: dict) -> dict:
    del path, project_name
    from scenecraft import plugin_api
    body = body or {}
    fixtures = body.get("fixtures") or []
    if not isinstance(fixtures, list):
        return {"error": "fixtures must be a list"}
    try:
        updated = plugin_api.upsert_light_show_fixtures(project_dir, fixtures)
    except ValueError as e:
        return {"error": str(e)}
    return {"fixtures": updated}


def _handle_reset(path: str, project_dir: Path, project_name: str, body: dict) -> dict:
    del path, project_name, body
    from scenecraft import plugin_api
    return {"fixtures": plugin_api.reset_light_show_fixtures(project_dir)}


def register(plugin_api, context) -> None:
    """Wire the three endpoints into the plugin-host's REST dispatch tables."""
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
