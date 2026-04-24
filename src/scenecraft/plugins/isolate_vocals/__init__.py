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

from scenecraft.plugin_host import PluginHost

from . import isolate_vocals as impl


def activate(plugin_api, context=None) -> None:
    """Register contributions + imperative side effects for isolate_vocals.

    The ``isolate_vocals.run`` operation is declared in ``plugin.yaml``
    so `PluginHost.register_declared` wires it up from the manifest.
    The REST endpoint stays imperative (no YAML schema for REST yet —
    plugins own their own URL patterns).

    ``context`` is optional to keep legacy-signature tests compatible; in
    normal operation the host always passes one.
    """
    import sys
    if context is not None:
        PluginHost.register_declared(sys.modules[__name__], context)
    plugin_api.register_rest_endpoint(
        r"^/api/projects/[^/]+/plugins/isolate_vocals/run$",
        impl.handle_rest,
        context=context,
    )


def deactivate(context) -> None:
    """Optional plugin-level deactivate hook.

    Most cleanup flows through ``context.subscriptions``; this is the place
    for anything that doesn't fit the Disposable shape (one-shot finalizers,
    log flushes, etc.). For isolate_vocals there's nothing extra — the DFN3
    model cache is process-global and harmless to leave behind, and run
    threads are daemon=True so they die with the process.
    """
    del context  # unused


# Public re-exports — tests + REST dispatch import through here.
run = impl.run
handle_rest = impl.handle_rest
