"""REST route handlers for light_show.

Existing surfaces: fixtures, overrides, screens.

M19 adds the scene editor surfaces:
  GET    /primitives                       → catalog (read-only)
  GET    /scenes (+ filters)               → list
  POST   /scenes                           → create
  GET    /scenes/:id                       → get one
  PATCH  /scenes/:id                       → merge-patch
  DELETE /scenes/:id                       → delete (409-shape on block)
  GET    /placements (+ filters)
  POST   /placements
  GET    /placements/:id
  PATCH  /placements/:id
  DELETE /placements/:id
  GET    /live                             → singleton status
  PUT    /live                             → activate
  DELETE /live (+ ?fade_out_sec)           → deactivate

Matches the ``api_server`` dispatch signature used by the other plugins
(``generate-music``, ``isolate_vocals``): each handler receives
``(path: str, project_dir: Path, project_name: str, body|query: dict)``.
"""

from __future__ import annotations

import re
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


# ── M19 — query-param helpers ─────────────────────────────────────────────


def _qs_first(q: dict, key: str, default=None):
    """parse_qs returns dict-of-lists; pick first value or default."""
    v = q.get(key)
    if v is None:
        return default
    if isinstance(v, list):
        return v[0] if v else default
    return v


def _qs_all(q: dict, key: str) -> list[str]:
    """Repeated-key array form: ?ids=a&ids=b → ['a', 'b']."""
    v = q.get(key)
    if v is None:
        return []
    if isinstance(v, list):
        return list(v)
    return [v]


def _qs_int(q: dict, key: str, default: int) -> int:
    raw = _qs_first(q, key)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default


def _qs_float(q: dict, key: str, default: float | None = None) -> float | None:
    raw = _qs_first(q, key)
    if raw is None or raw == "":
        return default
    try:
        return float(raw)
    except (TypeError, ValueError):
        return default


# ── M19 — primitives catalog (read-only) ──────────────────────────────────


_CATALOG_CACHE: dict | None = None


def _load_catalog() -> dict:
    """Parse primitives_catalog.yaml once and cache. The catalog ships with
    the plugin code so it doesn't need invalidation between requests."""
    global _CATALOG_CACHE
    if _CATALOG_CACHE is not None:
        return _CATALOG_CACHE
    import yaml
    catalog_path = Path(__file__).parent / "primitives_catalog.yaml"
    parsed = yaml.safe_load(catalog_path.read_text())
    _CATALOG_CACHE = parsed if isinstance(parsed, dict) else {"primitives": []}
    return _CATALOG_CACHE


def _known_primitive_types() -> set[str]:
    """Set of primitive type names (used for backend type validation)."""
    return {p["id"] for p in _load_catalog().get("primitives", []) if "id" in p}


def _handle_get_primitives(path: str, project_dir: Path, project_name: str, query: dict) -> dict:
    del path, project_dir, project_name, query
    return _load_catalog()


# ── M19 — scenes ──────────────────────────────────────────────────────────


def _handle_list_scenes(path: str, project_dir: Path, project_name: str, query: dict) -> dict:
    del path, project_name
    from scenecraft import plugin_api
    ids = _qs_all(query, "ids")
    type_filter = _qs_first(query, "type")
    label_query = _qs_first(query, "label_query")
    limit = _qs_int(query, "limit", 50)
    offset = _qs_int(query, "offset", 0)
    order_by = _qs_first(query, "order_by", "updated_at")
    order = _qs_first(query, "order", "desc")
    try:
        rows, total, has_more = plugin_api.list_light_show_scenes(
            project_dir,
            ids=ids or None,
            type_filter=type_filter,
            label_query=label_query,
            limit=limit,
            offset=offset,
            order_by=order_by,
            order=order,
        )
    except ValueError as e:
        return {"error": str(e)}
    return {"scenes": rows, "total": total, "has_more": has_more}


def _handle_create_scene(path: str, project_dir: Path, project_name: str, body: dict) -> dict:
    del path
    from scenecraft import plugin_api
    body = body or {}
    if "id" in body and body["id"]:
        return {"error": "POST /scenes is for creates; use PATCH to update an existing scene"}
    try:
        out = plugin_api.upsert_light_show_scenes(
            project_dir, [body], known_types=_known_primitive_types()
        )
    except ValueError as e:
        return {"error": str(e)}
    if not out:
        return {"error": "scene creation produced no row"}
    _broadcast_changed(project_name, "scenes")
    return {"scene": out[0]}


def _handle_get_scene(path: str, project_dir: Path, project_name: str, query: dict) -> dict:
    del project_name, query
    from scenecraft import plugin_api
    sid = _extract_id(path, r"/scenes/([^/]+)$")
    if not sid:
        return {"error": "missing scene id"}
    rows, _, _ = plugin_api.list_light_show_scenes(project_dir, ids=[sid], limit=1)
    if not rows:
        return {"error": f"scene not found: {sid}", "status": 404}
    return {"scene": rows[0]}


def _handle_patch_scene(path: str, project_dir: Path, project_name: str, body: dict) -> dict:
    from scenecraft import plugin_api
    sid = _extract_id(path, r"/scenes/([^/]+)$")
    if not sid:
        return {"error": "missing scene id"}
    body = body or {}
    body["id"] = sid
    try:
        out = plugin_api.upsert_light_show_scenes(
            project_dir, [body], known_types=_known_primitive_types()
        )
    except ValueError as e:
        return {"error": str(e)}
    if not out:
        return {"error": f"scene not found: {sid}", "status": 404}
    _broadcast_changed(project_name, "scenes")
    return {"scene": out[0]}


def _handle_delete_scene(path: str, project_dir: Path, project_name: str, body: dict) -> dict:
    del body
    from scenecraft import plugin_api
    sid = _extract_id(path, r"/scenes/([^/]+)$")
    if not sid:
        return {"error": "missing scene id"}
    try:
        deleted = plugin_api.remove_light_show_scenes(project_dir, [sid])
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
    if not deleted:
        return {"error": f"scene not found: {sid}", "status": 404}
    _broadcast_changed(project_name, "scenes")
    return {"scene": deleted[0]}


# ── M19 — placements ──────────────────────────────────────────────────────


def _handle_list_placements(path: str, project_dir: Path, project_name: str, query: dict) -> dict:
    del path, project_name
    from scenecraft import plugin_api
    ids = _qs_all(query, "ids")
    scene_id = _qs_first(query, "scene_id")
    time_start = _qs_float(query, "time_start")
    time_end = _qs_float(query, "time_end")
    limit = _qs_int(query, "limit", 100)
    offset = _qs_int(query, "offset", 0)
    order_by = _qs_first(query, "order_by", "start_time")
    order = _qs_first(query, "order", "asc")
    if (time_start is None) != (time_end is None):
        return {"error": "time_start and time_end must both be present or both omitted"}
    try:
        rows, total, has_more = plugin_api.list_light_show_placements(
            project_dir,
            ids=ids or None,
            scene_id=scene_id,
            time_start=time_start,
            time_end=time_end,
            limit=limit,
            offset=offset,
            order_by=order_by,
            order=order,
        )
    except ValueError as e:
        return {"error": str(e)}
    return {"placements": rows, "total": total, "has_more": has_more}


def _handle_create_placement(path: str, project_dir: Path, project_name: str, body: dict) -> dict:
    del path
    from scenecraft import plugin_api
    body = body or {}
    if "id" in body and body["id"]:
        return {"error": "POST /placements is for creates; use PATCH to update"}
    try:
        out = plugin_api.upsert_light_show_placements(project_dir, [body])
    except ValueError as e:
        return {"error": str(e)}
    if not out:
        return {"error": "placement creation produced no row"}
    _broadcast_changed(project_name, "placements")
    return {"placement": out[0]}


def _handle_get_placement(path: str, project_dir: Path, project_name: str, query: dict) -> dict:
    del project_name, query
    from scenecraft import plugin_api
    pid = _extract_id(path, r"/placements/([^/]+)$")
    if not pid:
        return {"error": "missing placement id"}
    rows, _, _ = plugin_api.list_light_show_placements(project_dir, ids=[pid], limit=1)
    if not rows:
        return {"error": f"placement not found: {pid}", "status": 404}
    return {"placement": rows[0]}


def _handle_patch_placement(path: str, project_dir: Path, project_name: str, body: dict) -> dict:
    from scenecraft import plugin_api
    pid = _extract_id(path, r"/placements/([^/]+)$")
    if not pid:
        return {"error": "missing placement id"}
    body = body or {}
    body["id"] = pid
    try:
        out = plugin_api.upsert_light_show_placements(project_dir, [body])
    except ValueError as e:
        return {"error": str(e)}
    if not out:
        return {"error": f"placement not found: {pid}", "status": 404}
    _broadcast_changed(project_name, "placements")
    return {"placement": out[0]}


def _handle_delete_placement(path: str, project_dir: Path, project_name: str, body: dict) -> dict:
    del body
    from scenecraft import plugin_api
    pid = _extract_id(path, r"/placements/([^/]+)$")
    if not pid:
        return {"error": "missing placement id"}
    deleted = plugin_api.remove_light_show_placements(project_dir, [pid])
    if not deleted:
        return {"error": f"placement not found: {pid}", "status": 404}
    _broadcast_changed(project_name, "placements")
    return {"placement": deleted[0]}


# ── M19 — live override (singleton) ───────────────────────────────────────


def _handle_get_live(path: str, project_dir: Path, project_name: str, query: dict) -> dict:
    del path, project_name, query
    from scenecraft import plugin_api
    row = plugin_api.get_light_show_live_override(project_dir)
    if row is None:
        return {"active": False}
    return {"active": True, **row}


def _handle_put_live(path: str, project_dir: Path, project_name: str, body: dict) -> dict:
    del path
    from scenecraft import plugin_api
    body = body or {}
    try:
        row = plugin_api.activate_light_show_live_override(
            project_dir, body, known_types=_known_primitive_types()
        )
    except ValueError as e:
        return {"error": str(e)}
    _broadcast_changed(project_name, "live")
    return {"active": True, **row}


def _handle_delete_live(path: str, project_dir: Path, project_name: str, query: dict) -> dict:
    del path
    from scenecraft import plugin_api
    fade = _qs_float(query, "fade_out_sec", 0.0) or 0.0
    row = plugin_api.deactivate_light_show_live_override(project_dir, fade_out_sec=fade)
    _broadcast_changed(project_name, "live")
    if isinstance(row, dict) and row.get("active") is False:
        return {"active": False}
    return {"active": True, **row}


# ── id extraction helper ──────────────────────────────────────────────────


def _extract_id(path: str, pattern: str) -> str | None:
    """Pull the resource id off the URL path. The compiled regex is anchored
    to the path tail (after the project / plugin prefix)."""
    m = re.search(pattern, path)
    return m.group(1) if m else None


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
    # ── M19: scene editor endpoints ──────────────────────────────────────
    # Catalog (read-only)
    plugin_api.register_rest_endpoint(
        r"^/api/projects/[^/]+/plugins/light_show/primitives$",
        _handle_get_primitives,
        method="GET",
        context=context,
    )
    # Scenes (collection)
    plugin_api.register_rest_endpoint(
        r"^/api/projects/[^/]+/plugins/light_show/scenes$",
        _handle_list_scenes,
        method="GET",
        context=context,
    )
    plugin_api.register_rest_endpoint(
        r"^/api/projects/[^/]+/plugins/light_show/scenes$",
        _handle_create_scene,
        method="POST",
        context=context,
    )
    # Scenes (item) — must register AFTER the collection routes so the
    # collection regex doesn't accidentally match :id paths.
    plugin_api.register_rest_endpoint(
        r"^/api/projects/[^/]+/plugins/light_show/scenes/[^/]+$",
        _handle_get_scene,
        method="GET",
        context=context,
    )
    plugin_api.register_rest_endpoint(
        r"^/api/projects/[^/]+/plugins/light_show/scenes/[^/]+$",
        _handle_patch_scene,
        method="PATCH",
        context=context,
    )
    plugin_api.register_rest_endpoint(
        r"^/api/projects/[^/]+/plugins/light_show/scenes/[^/]+$",
        _handle_delete_scene,
        method="DELETE",
        context=context,
    )
    # Placements (collection)
    plugin_api.register_rest_endpoint(
        r"^/api/projects/[^/]+/plugins/light_show/placements$",
        _handle_list_placements,
        method="GET",
        context=context,
    )
    plugin_api.register_rest_endpoint(
        r"^/api/projects/[^/]+/plugins/light_show/placements$",
        _handle_create_placement,
        method="POST",
        context=context,
    )
    # Placements (item)
    plugin_api.register_rest_endpoint(
        r"^/api/projects/[^/]+/plugins/light_show/placements/[^/]+$",
        _handle_get_placement,
        method="GET",
        context=context,
    )
    plugin_api.register_rest_endpoint(
        r"^/api/projects/[^/]+/plugins/light_show/placements/[^/]+$",
        _handle_patch_placement,
        method="PATCH",
        context=context,
    )
    plugin_api.register_rest_endpoint(
        r"^/api/projects/[^/]+/plugins/light_show/placements/[^/]+$",
        _handle_delete_placement,
        method="DELETE",
        context=context,
    )
    # Live override (singleton)
    plugin_api.register_rest_endpoint(
        r"^/api/projects/[^/]+/plugins/light_show/live$",
        _handle_get_live,
        method="GET",
        context=context,
    )
    plugin_api.register_rest_endpoint(
        r"^/api/projects/[^/]+/plugins/light_show/live$",
        _handle_put_live,
        method="PUT",
        context=context,
    )
    plugin_api.register_rest_endpoint(
        r"^/api/projects/[^/]+/plugins/light_show/live$",
        _handle_delete_live,
        method="DELETE",
        context=context,
    )
