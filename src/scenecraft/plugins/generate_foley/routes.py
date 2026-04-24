"""REST routes for generate-foley.

Three endpoints, all under ``/api/projects/:name/plugins/generate-foley/``:

  POST   /run                                     kick off a generation
  GET    /generations?entityType=&entityId=       list (optionally filtered)
  POST   /generations/:id/retry                   re-run with identical params

WS event stream (``job_started`` / ``job_progress`` / ``job_completed`` /
``job_failed``) is wired automatically by ``plugin_api.job_manager`` on
``/ws/jobs`` — no per-plugin WS handler needed. The worker in
``generate_foley.run`` is what emits those events.

On activation this module also kicks off ``resume_in_flight`` to reattach
polling for predictions left dangling from a prior server run
(disconnect-survival invariant).
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any

from scenecraft.plugins.generate_foley import generate_foley as impl

logger = logging.getLogger(__name__)

MIN_DURATION = impl.MIN_DURATION
MAX_DURATION = impl.MAX_DURATION


# --- Handlers --------------------------------------------------------------


def _handle_run(
    path: str, project_dir: Path, project_name: str, body: dict
) -> dict:
    """POST /run — start a foley generation."""
    body = body or {}

    # Validate count==1 at API boundary; deeper validation is in impl.run
    count = body.get("count", 1)
    if count != 1:
        return {"error": "count must be 1 in MVP; multi-variant coming later"}

    # Range validation is applied upstream too but we echo a clearer error here.
    source_in = body.get("source_in_seconds")
    source_out = body.get("source_out_seconds")
    source_candidate_id = body.get("source_candidate_id")
    if source_candidate_id and (source_in is None or source_out is None):
        return {"error": "v2fx mode requires source_in_seconds and source_out_seconds"}
    if source_in is not None and source_out is not None:
        if source_out <= source_in:
            return {"error": "source_out_seconds must be > source_in_seconds"}
        if (source_out - source_in) > MAX_DURATION:
            return {"error": f"range exceeds {MAX_DURATION}s ceiling"}

    # Duration validation for t2fx mode
    duration = body.get("duration_seconds")
    if source_candidate_id is None and duration is not None:
        if not (MIN_DURATION <= duration <= MAX_DURATION):
            return {
                "error": f"duration_seconds out of bounds [{MIN_DURATION}, {MAX_DURATION}]"
            }

    # entity_type validation
    entity_type = body.get("entity_type")
    if entity_type is not None and entity_type != "transition":
        return {"error": f"entity_type must be 'transition' (got {entity_type!r})"}

    try:
        return impl.run(
            project_dir,
            project_name,
            prompt=body.get("prompt"),
            duration_seconds=duration,
            source_candidate_id=source_candidate_id,
            source_in_seconds=source_in,
            source_out_seconds=source_out,
            negative_prompt=body.get("negative_prompt"),
            cfg_strength=body.get("cfg_strength"),
            seed=body.get("seed"),
            entity_type=entity_type,
            entity_id=body.get("entity_id"),
            variant_count=count,
        )
    except ValueError as e:
        return {"error": str(e)}


def _handle_list(
    path: str, project_dir: Path, project_name: str, query: dict
) -> dict:
    """GET /generations?entityType=&entityId= — list filtered newest-first."""
    query = query or {}
    entity_type = query.get("entityType") or None
    entity_id = query.get("entityId") or None

    # Optional pagination
    try:
        limit = int(query.get("limit", 200))
    except (TypeError, ValueError):
        limit = 200
    limit = max(1, min(limit, 500))  # sanity

    from scenecraft import plugin_api

    rows = plugin_api.get_foley_generations_for_entity(
        project_dir,
        entity_type=entity_type,
        entity_id=entity_id,
        limit=limit,
    )
    return {"generations": rows}


_RETRY_PATH_RE = re.compile(
    r"^/api/projects/[^/]+/plugins/generate-foley/generations/([^/]+)/retry$"
)


def _handle_retry(
    path: str, project_dir: Path, project_name: str, body: dict
) -> dict:
    """POST /generations/:id/retry — reuse params from an existing generation."""
    m = _RETRY_PATH_RE.match(path)
    if not m:
        return {"error": "malformed retry path"}
    original_id = m.group(1)

    from scenecraft import plugin_api

    original = plugin_api.get_foley_generation(project_dir, original_id)
    if original is None:
        return {"error": f"generation {original_id} not found", "_status": 404}
    if original["status"] in ("pending", "running"):
        return {
            "error": f"generation {original_id} is still {original['status']}; wait for it to finish before retrying",
        }

    # Re-run with identical params
    try:
        return impl.run(
            project_dir,
            project_name,
            prompt=original.get("prompt"),
            mode=original.get("mode"),
            duration_seconds=original.get("duration_seconds"),
            source_candidate_id=original.get("source_candidate_id"),
            source_in_seconds=original.get("source_in_seconds"),
            source_out_seconds=original.get("source_out_seconds"),
            negative_prompt=original.get("negative_prompt"),
            cfg_strength=original.get("cfg_strength"),
            seed=original.get("seed"),
            entity_type=original.get("entity_type"),
            entity_id=original.get("entity_id"),
            variant_count=original.get("variant_count", 1),
        )
    except ValueError as e:
        return {"error": str(e)}


# --- Registration ---------------------------------------------------------


def register(plugin_api, context) -> None:
    """Called by generate_foley/__init__.py on activation."""
    project_dir = getattr(context, "project_dir", None)

    # Register REST routes
    plugin_api.register_rest_endpoint(
        path_regex=r"^/api/projects/[^/]+/plugins/generate-foley/run$",
        handler=_handle_run,
        method="POST",
        context=context,
    )
    plugin_api.register_rest_endpoint(
        path_regex=r"^/api/projects/[^/]+/plugins/generate-foley/generations$",
        handler=_handle_list,
        method="GET",
        context=context,
    )
    plugin_api.register_rest_endpoint(
        path_regex=r"^/api/projects/[^/]+/plugins/generate-foley/generations/[^/]+/retry$",
        handler=_handle_retry,
        method="POST",
        context=context,
    )

    # Disconnect-survival scan
    if project_dir is not None:
        try:
            reattached = impl.resume_in_flight(Path(project_dir))
            if reattached:
                logger.info(
                    "[generate-foley] reattached polling for %d in-flight generations: %s",
                    len(reattached), reattached,
                )
        except Exception:
            logger.exception("[generate-foley] failed to reattach polling on activate")
