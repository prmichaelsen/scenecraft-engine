"""REST route registration for generate-foley.

Populated by task-145 with the three endpoints:
  POST /api/projects/:project/plugins/generate-foley/run
  GET  /api/projects/:project/plugins/generate-foley/generations
  POST /api/projects/:project/plugins/generate-foley/generations/:id/retry

MVP stub — just resumes in-flight predictions on activation so server restarts
don't leave dangling generations.
"""

from __future__ import annotations

import logging
from pathlib import Path

from scenecraft.plugins.generate_foley.generate_foley import resume_in_flight

logger = logging.getLogger(__name__)


def register(plugin_api, context) -> None:
    """Called from ``generate_foley/__init__.py`` on activation."""
    # Disconnect-survival scan. Routes themselves come in task-145.
    # Scans only run against the current project if one is active; the host
    # invokes activate per-project, so this runs with the right project_dir.
    project_dir = getattr(context, "project_dir", None)
    if project_dir is not None:
        try:
            reattached = resume_in_flight(Path(project_dir))
            if reattached:
                logger.info(
                    "[generate-foley] reattached polling for %d in-flight generations: %s",
                    len(reattached), reattached,
                )
        except Exception:
            logger.exception("[generate-foley] failed to reattach polling on activate")
