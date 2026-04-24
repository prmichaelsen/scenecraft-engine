"""Typed per-provider modules exposed to plugins.

Plugins consume a typed provider surface rather than the string-keyed
``call_service`` shim. Each provider owns: auth, HTTP, polling, backoff,
spend_ledger attribution, disconnect-survival, and artifact download.

The ``replicate`` submodule is the first concrete implementation (M18 task-142).
Future providers (``musicful``, ``elevenlabs``, ``openai``, etc.) land as
additional submodules following the same shape.

Per R9a invariant: providers write to ``spend_ledger`` via
``plugin_api.record_spend`` only — no raw DB access.
"""

from . import replicate

__all__ = ["replicate"]
