"""Transcribe plugin — Whisper on Replicate.

Registers a single MCP/chat tool `transcribe__transcribe_clip` plus an
optional right-click operation `transcribe.run` for the audio_clip
entity. Plugin settings (default_model, default_language,
default_word_timestamps) are read via `transcriber.get_plugin_settings`.
"""

from __future__ import annotations

from scenecraft.plugin_host import (
    MCPToolDef,
    OperationDef,
    PluginContext,
    PluginHost,
)

from .handlers import (
    TRANSCRIBE_CLIP_INPUT_SCHEMA,
    TRANSCRIBE_CLIP_TOOL_DESCRIPTION,
    handle_transcribe_clip,
    handle_transcribe_operation,
)


def activate(plugin_api, context: PluginContext) -> None:
    """Register the plugin's contributions with the host."""
    del plugin_api  # unused for this plugin — tool is self-contained

    tool = MCPToolDef(
        plugin="transcribe",
        tool_id="transcribe_clip",
        description=TRANSCRIBE_CLIP_TOOL_DESCRIPTION,
        input_schema=TRANSCRIBE_CLIP_INPUT_SCHEMA,
        handler=handle_transcribe_clip,
        destructive=False,
    )
    context.subscriptions.append(PluginHost.register_mcp_tool(tool, context=None))

    op = OperationDef(
        id="transcribe.run",
        label="Transcribe…",
        entity_types=["audio_clip"],
        handler=handle_transcribe_operation,
    )
    context.subscriptions.append(PluginHost.register_operation(op))
