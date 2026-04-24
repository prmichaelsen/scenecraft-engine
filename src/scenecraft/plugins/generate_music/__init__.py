"""generate_music plugin — Musicful-backed AI music generation.

Declarative contributions (the ``generate_music.run`` operation and the
two chat tools ``generate_music__run`` + ``generate_music__credits``) are
declared in ``plugin.yaml``. The host parses it before ``activate()`` and
wires every contribution via ``PluginHost.register_declared``.

REST routes (task-130) stay imperative — ``register_declared`` only knows
about operations and mcpTools; route registration is still driven by the
plugin calling ``plugin_api.register_rest_endpoint`` inside ``activate``.

See agent/specs/local.music-generation-plugin.md for the full contract.
"""

from __future__ import annotations

from scenecraft.plugin_host import PluginContext, PluginHost

from .generate_music import run, check_api_key
from .handlers import (
    handle_generate_music,
    handle_get_credits,
    run_operation,
)

PLUGIN_ID = "generate_music"

# Re-exports so plugin.yaml's ``handler: "backend:<attr>"`` refs resolve
# against this module at register_declared time. Keep this list + the
# plugin.yaml mcpTools/operations handlers in sync.
__all__ = [
    "run",
    "check_api_key",
    "activate",
    "deactivate",
    "handle_generate_music",
    "handle_get_credits",
    "run_operation",
    "PLUGIN_ID",
]


def activate(plugin_api, context: PluginContext) -> None:
    """Wire declarative contributions + imperative REST routes.

    Two-step because ``register_declared`` only reads operations +
    mcpTools from the manifest. REST routes are registered the old way
    via ``plugin_api.register_rest_endpoint`` in the routes module.
    """
    import sys
    # Manifest-driven: operation `generate_music.run` + chat tools.
    PluginHost.register_declared(sys.modules[__name__], context)

    # Imperative REST routes (task-130).
    from scenecraft.plugins.generate_music import routes
    routes.register(plugin_api, context)


def deactivate(context: PluginContext) -> None:  # noqa: ARG001
    """All subscriptions live on context.subscriptions and fire automatically."""
