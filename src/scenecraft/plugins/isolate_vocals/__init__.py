"""isolate_vocals plugin: DFN3 + residual multi-stem audio isolation.

This plugin contributes a single operation, ``isolate_vocals.run``, which
separates a voice-over-noise audio source into ``vocal`` + ``background`` stems.
Both stems land as new ``pool_segments`` rows linked under one
``audio_isolations`` run via the ``isolation_stems`` junction.

Naming conventions:
  • Internal ids (operation id, activation events) use dot notation —
    ``{plugin}.{member}``.
  • The chat-tool surface requires ``{plugin}__{member}`` (double-underscore)
    because Claude's tool-name regex disallows dots. ``chat.py`` exposes this
    operation as ``isolate_vocals__run``.

Activation is driven by ``PluginHost.register(isolate_vocals)`` at server
startup in ``api_server.run_server``.
"""

from __future__ import annotations

from scenecraft.plugin_host import OperationDef, PluginHost

from . import isolate_vocals as impl


def activate(plugin_api) -> None:
    """Register contributions with the PluginHost + REST router.

    Called once by ``PluginHost.register`` at process startup. Plugins MUST NOT
    side-effect anything else from here — they get exactly one callback and
    register all contributions declaratively.
    """
    PluginHost.register_operation(
        OperationDef(
            id="isolate_vocals.run",
            label="Isolate vocals",
            entity_types=["audio_clip", "transition"],
            handler=impl.run,
        )
    )
    plugin_api.register_rest_endpoint(
        r"^/api/projects/[^/]+/plugins/isolate_vocals/run$",
        impl.handle_rest,
    )


# Public re-exports — tests + REST dispatch import through here.
run = impl.run
handle_rest = impl.handle_rest
