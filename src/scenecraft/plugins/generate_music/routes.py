"""REST route handlers for generate-music.

All four endpoints dispatch off the shared
``/api/projects/:name/plugins/generate-music/...`` prefix via
``plugin_api.register_rest_endpoint``. The WS integration from task-130
needs no wiring here — ``generate_music.run`` already drives
``plugin_api.job_manager``, whose broadcasts land on the existing
``/ws/jobs`` channel.

Auth (task-126) is deferred per the M16 skip-auth directive. These
routes run with whatever auth context ``api_server`` provides (empty
defaults for `username`/`org` in dev mode); when the auth milestone
ships, the double-gate middleware attaches at ``api_server`` level and
this file does not change.

The 3-minute TTL credits cache is a process-global dict — the spec
(R49) calls for a short TTL with "refresh after run" semantics. Run
invalidation happens implicitly because `/run` doesn't touch the cache;
the next `/credits` GET will still hit upstream only if the cache is
stale. That's good enough for MVP; exposing an explicit bust hook is
follow-up work if the UX wants instant refresh after completion.
"""

from __future__ import annotations

from pathlib import Path
import time
from typing import Any

from scenecraft.plugins.generate_music import generate_music as impl


_CREDITS_TTL_SECONDS = 60.0
# Process-global cache; safe because `get_credits()` returns a shallow
# immutable-ish dict and the server is threaded but single-process.
_credits_cache: dict[str, Any] = {"value": None, "fetched_at": 0.0}


def _handle_run(path: str, project_dir: Path, project_name: str, body: dict) -> dict:
    """POST /run — kick off a generation. Returns `{generation_id, task_ids, job_id}`
    on success or `{error}` on validation / upstream failure."""
    body = body or {}
    style = body.get("style")
    if not isinstance(style, str) or not style.strip():
        return {"error": "style is required"}
    return impl.run(
        project_dir,
        project_name,
        action=body.get("action", "auto"),
        style=style,
        lyrics=body.get("lyrics"),
        title=body.get("title"),
        instrumental=int(body.get("instrumental", 1)),
        gender=body.get("gender"),
        model=body.get("model", "MFV2.0"),
        entity_type=body.get("entity_type"),
        entity_id=body.get("entity_id"),
        auth_context=body.get("auth_context"),
    )


def _handle_list(path: str, project_dir: Path, project_name: str, query: dict) -> dict:
    """GET /generations?entityType=&entityId= — list generations for the project,
    optionally filtered to a single (entity_type, entity_id) pair."""
    query = query or {}
    entity_type = query.get("entityType") or None
    entity_id = query.get("entityId") or None
    rows = impl.list_generations(
        project_dir,
        entity_type=entity_type,
        entity_id=entity_id,
    )
    return {"generations": rows}


def _handle_retry(path: str, project_dir: Path, project_name: str, body: dict) -> dict:
    """POST /generations/:id/retry — create a new generation with same params
    as the failed one, `reused_from=<failed_id>`. The original row is not
    mutated. Returns `{generation_id, task_ids, job_id}` on success."""
    # Extract :id from path. The dispatcher matched the trailing /retry
    # regex so we know the shape is .../generations/<id>/retry.
    import re as _re
    m = _re.search(r"/generations/([^/]+)/retry$", path)
    if not m:
        return {"error": "malformed retry path"}
    failed_id = m.group(1)
    body = body or {}
    return impl.retry_generation(
        project_dir,
        project_name,
        failed_id,
        auth_context=body.get("auth_context"),
    )


def _handle_credits(path: str, project_dir: Path, project_name: str, query: dict) -> dict:
    """GET /credits — cached for TTL seconds so rapid UI refreshes don't
    hammer the Musicful endpoint. Bypass the cache via ``?refresh=1``."""
    query = query or {}
    force = query.get("refresh") in ("1", "true", "yes")
    now = time.monotonic()
    cached = _credits_cache.get("value")
    fetched_at = _credits_cache.get("fetched_at", 0.0)
    if not force and cached is not None and (now - fetched_at) < _CREDITS_TTL_SECONDS:
        return cached
    result = impl.get_credits()
    _credits_cache["value"] = result
    _credits_cache["fetched_at"] = now
    return result


def register(plugin_api, context) -> None:
    """Wire the four endpoints into the plugin-host's REST dispatch tables."""
    plugin_api.register_rest_endpoint(
        r"^/api/projects/[^/]+/plugins/generate-music/run$",
        _handle_run,
        method="POST",
        context=context,
    )
    plugin_api.register_rest_endpoint(
        r"^/api/projects/[^/]+/plugins/generate-music/generations$",
        _handle_list,
        method="GET",
        context=context,
    )
    plugin_api.register_rest_endpoint(
        r"^/api/projects/[^/]+/plugins/generate-music/generations/[^/]+/retry$",
        _handle_retry,
        method="POST",
        context=context,
    )
    plugin_api.register_rest_endpoint(
        r"^/api/projects/[^/]+/plugins/generate-music/credits$",
        _handle_credits,
        method="GET",
        context=context,
    )


def _reset_cache_for_tests() -> None:
    """Clear the credits TTL cache. Tests use this between runs."""
    _credits_cache["value"] = None
    _credits_cache["fetched_at"] = 0.0
