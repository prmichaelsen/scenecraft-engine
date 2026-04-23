"""generate-music plugin — Musicful-backed AI music generation.

Entry point: activate(context) registers REST routes + chat tool wiring.
Public handler: run(project_dir, project_name, ...) kicks off a generation.

See agent/specs/local.music-generation-plugin.md for the full contract.
"""

from __future__ import annotations

from scenecraft.plugins.generate_music.generate_music import run, check_api_key

PLUGIN_ID = "generate-music"

__all__ = ["run", "check_api_key", "activate", "PLUGIN_ID"]


def activate(context):
    """Plugin activation hook — called by PluginHost at server startup."""
    # Import here to avoid circular imports at module load time.
    from scenecraft.plugins.generate_music import routes

    routes.register(context)
    return context
