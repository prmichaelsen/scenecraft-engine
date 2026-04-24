"""generate-foley plugin — MMAudio-backed AI foley (SFX) generation.

Supports both text-to-FX (t2fx) and video-to-FX (v2fx) modes via the
typed ``plugin_api.providers.replicate`` surface. Selection-driven mode
dispatch; output lands in pool_segments with variant_kind='foley'.

Entry point: activate(context) registers REST routes + chat tool wiring.
Public handler: run(project_dir, project_name, ...) kicks off a generation.

See:
  agent/design/local.foley-generation-plugin.md
  agent/clarifications/clarification-12-foley-generation-plugin.md
"""

from __future__ import annotations

from scenecraft.plugins.generate_foley.generate_foley import (
    run,
    check_api_key,
    resume_in_flight,
)

PLUGIN_ID = "generate-foley"

__all__ = ["run", "check_api_key", "resume_in_flight", "activate", "PLUGIN_ID"]


def activate(plugin_api, context):
    """Plugin activation hook — called by PluginHost at server startup.

    ``plugin_api`` is the ``scenecraft.plugin_api`` module (passed by the
    host); ``context`` is the ``PluginContext`` with
    ``context.subscriptions`` for teardown.

    This hook does NOT register REST routes (those come in task-145).
    It DOES trigger the disconnect-survival scan — any in-flight predictions
    left dangling from a prior server run get their polling reattached.
    """
    # Import here to avoid circular imports at module load time.
    from scenecraft.plugins.generate_foley import routes

    routes.register(plugin_api, context)
    return context
