"""Static plugin registry.

Collects operations, context-menu contributions, and REST routes from
registered plugin modules. This is NOT a dynamic loader — for MVP the list of
plugins is hardcoded at startup. When a dynamic loader lands later, the same
``PluginHost`` surface will be the integration point; only the call site that
populates the registry will change.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Callable, Optional


@dataclass
class OperationDef:
    """A single plugin-contributed operation.

    ``handler`` is called as ``handler(entity_type, entity_id, context)`` and
    returns a JSON-serializable result dict. ``entity_types`` is the list of
    entity kinds (e.g. ``"audio_clip"``) this operation can be invoked on.
    """

    id: str
    label: str
    entity_types: list[str]
    handler: Callable[[str, str, dict], dict]


class PluginHost:
    """Static registry for plugin contributions.

    Class-level state on purpose: there is exactly one host per process, and
    plugin registration is a process-startup activity.
    """

    _operations: dict[str, OperationDef] = {}
    _rest_routes: dict[str, Callable] = {}
    _registered: list[str] = []

    @classmethod
    def register(cls, plugin_module) -> None:
        """Activate a plugin module.

        The plugin is expected to expose ``activate(plugin_api)`` and to register
        its own contributions via ``register_operation`` /
        ``register_rest_endpoint`` etc. during activation.
        """
        from scenecraft import plugin_api

        plugin_module.activate(plugin_api)
        cls._registered.append(getattr(plugin_module, "__name__", "<unknown>"))

    @classmethod
    def register_operation(cls, op: OperationDef) -> None:
        assert op.id not in cls._operations, f"duplicate operation id: {op.id}"
        cls._operations[op.id] = op

    @classmethod
    def get_operation(cls, op_id: str) -> Optional[OperationDef]:
        return cls._operations.get(op_id)

    @classmethod
    def list_operations(
        cls, entity_type: Optional[str] = None
    ) -> list[OperationDef]:
        if entity_type is None:
            return list(cls._operations.values())
        return [op for op in cls._operations.values() if entity_type in op.entity_types]

    @classmethod
    def dispatch_rest(cls, path: str, *args, **kwargs) -> Any:
        """Route a REST path to a plugin-registered handler.

        Returns the handler's return value, or ``None`` if no pattern matches.
        ``api_server.py`` uses this as a fallback after its built-in routes fail.
        """
        for pattern, handler in cls._rest_routes.items():
            if re.match(pattern, path):
                return handler(path, *args, **kwargs)
        return None

    # --- Test support -----------------------------------------------------

    @classmethod
    def _reset_for_tests(cls) -> None:
        """Clear all registry state. Intended for tests only."""
        cls._operations = {}
        cls._rest_routes = {}
        cls._registered = []
