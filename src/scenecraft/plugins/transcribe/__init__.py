"""Transcribe plugin — Whisper on Replicate.

Declarative contributions (settings schema, the
``transcribe__transcribe_clip`` MCP tool, the ``transcribe.run``
context-menu operation) are declared in ``plugin.yaml``. The host parses
that manifest before calling ``activate()`` and makes it available via
``context.manifest``.

This plugin uses the manifest as source of truth — ``activate()`` simply
asks the host to wire every declared contribution via
``PluginHost.register_declared``. Handler refs in the manifest
(``backend:handle_transcribe_clip``) resolve against the names
re-exported from this package root below.
"""

from __future__ import annotations

from scenecraft.plugin_host import PluginContext, PluginHost

from .handlers import (
    GET_TRANSCRIPTION_INPUT_SCHEMA,
    GET_TRANSCRIPTION_TOOL_DESCRIPTION,
    LIST_TRANSCRIPTIONS_INPUT_SCHEMA,
    LIST_TRANSCRIPTIONS_TOOL_DESCRIPTION,
    TRANSCRIBE_CLIP_INPUT_SCHEMA,
    TRANSCRIBE_CLIP_TOOL_DESCRIPTION,
    handle_get_transcription,
    handle_list_transcriptions,
    handle_rest_get_run,
    handle_rest_list_runs,
    handle_rest_run,
    handle_transcribe_clip,
    handle_transcribe_operation,
)

# Re-exports so the manifest's ``handler: "backend:<attr>"`` refs can find
# these at the plugin module root via getattr().
__all__ = [
    "GET_TRANSCRIPTION_INPUT_SCHEMA",
    "GET_TRANSCRIPTION_TOOL_DESCRIPTION",
    "LIST_TRANSCRIPTIONS_INPUT_SCHEMA",
    "LIST_TRANSCRIPTIONS_TOOL_DESCRIPTION",
    "TRANSCRIBE_CLIP_INPUT_SCHEMA",
    "TRANSCRIBE_CLIP_TOOL_DESCRIPTION",
    "handle_get_transcription",
    "handle_list_transcriptions",
    "handle_rest_get_run",
    "handle_rest_list_runs",
    "handle_rest_run",
    "handle_transcribe_clip",
    "handle_transcribe_operation",
]


def activate(plugin_api, context: PluginContext) -> None:
    """Imperative hook — registers every contribution declared in the manifest.

    All wiring is driven by ``plugin.yaml``; this plugin has no additional
    side effects. If a plugin needed to pre-warm caches or spawn
    background threads it would do so here alongside the
    ``register_declared`` call.
    """
    del plugin_api  # unused for this plugin
    import sys
    PluginHost.register_declared(_this_module(), context)


def _this_module():
    """Return this package as a module object for handler resolution."""
    import sys
    return sys.modules[__name__]
