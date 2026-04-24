"""Static plugin registry with VSCode-style dispose pattern.

Plugins register contributions during ``activate(plugin_api, context)`` and
push ``Disposable`` objects into ``context.subscriptions``. When a plugin is
deactivated, each disposable's ``.dispose()`` is called in LIFO order so
resources (threads, file watchers, background timers, etc.) can be cleaned
up cleanly.

For MVP the list of plugins is hardcoded at startup. When a dynamic loader
lands later, ``PluginHost.register`` + ``.deactivate`` become the seams.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Callable, Optional, Protocol, runtime_checkable


# ── Disposable contract ─────────────────────────────────────────────────

@runtime_checkable
class Disposable(Protocol):
    """Anything with a ``dispose()`` method. Plugin subscriptions get disposed
    in LIFO order on plugin deactivation — matches VSCode's model."""

    def dispose(self) -> None: ...


class _FunctionDisposable:
    """Wrap a plain callable into a Disposable."""

    __slots__ = ("_fn", "_disposed")

    def __init__(self, fn: Callable[[], None]) -> None:
        self._fn = fn
        self._disposed = False

    def dispose(self) -> None:
        if self._disposed:
            return
        self._disposed = True
        try:
            self._fn()
        except Exception as e:  # noqa: BLE001
            import sys
            print(f"[plugin-host] disposable raised: {e}", file=sys.stderr)


def make_disposable(fn: Callable[[], None]) -> Disposable:
    """Adapt a teardown callable to a ``Disposable``. Plugins can use this to
    register cleanup for threads, sockets, file handles, etc.::

        stop_event = threading.Event()
        thread = threading.Thread(...)
        thread.start()
        context.subscriptions.append(make_disposable(lambda: (stop_event.set(), thread.join(timeout=5))))
    """
    return _FunctionDisposable(fn)


@dataclass
class PluginContext:
    """Per-plugin activation context. ``subscriptions`` is the list of
    ``Disposable`` objects the plugin registers during activation; the host
    disposes them in LIFO order on deactivation.
    """

    name: str
    subscriptions: list[Disposable] = field(default_factory=list)


# ── Operation definition ────────────────────────────────────────────────


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


# ── MCP / chat-tool definition ──────────────────────────────────────────


@dataclass
class MCPToolDef:
    """A plugin-contributed chat/MCP tool.

    The tool appears in Claude's tool list as ``{plugin}__{tool_id}`` —
    the same double-underscore convention the project uses for
    plugin-owned DB tables and the existing ``isolate_vocals__run`` tool.

    ``handler`` is called as ``handler(args, context)`` where ``args`` is
    the parsed ``input`` dict from Claude and ``context`` is a dict the
    chat dispatcher builds (``project_dir``, ``project_name``, ``auth``,
    any other state). Return value is a JSON-serializable dict sent back
    as the tool_result payload.

    ``destructive=True`` routes the call through the elicitation gate the
    same way built-in destructive tools do — no plugin-side work required.
    """

    plugin: str
    tool_id: str
    description: str
    input_schema: dict
    handler: Callable[[dict, dict], dict]
    destructive: bool = False

    @property
    def full_name(self) -> str:
        """The name Claude sees. Must match the tool-name regex."""
        return f"{self.plugin}__{self.tool_id}"


# ── PluginHost ──────────────────────────────────────────────────────────


class PluginHost:
    """Static registry for plugin contributions.

    Class-level state on purpose: there is exactly one host per process, and
    plugin registration is a process-startup activity.
    """

    _operations: dict[str, OperationDef] = {}
    _rest_routes: dict[str, Callable] = {}
    _mcp_tools: dict[str, "MCPToolDef"] = {}
    # Map module name → PluginContext for the registered instance.
    _contexts: dict[str, PluginContext] = {}
    # Mirror of _contexts.keys() in registration order — kept around for
    # startup-log diagnostics that want a simple list.
    _registered: list[str] = []

    @classmethod
    def register(cls, plugin_module) -> PluginContext:
        """Activate a plugin module.

        The plugin is expected to expose ``activate(plugin_api, context)``
        (or the legacy single-arg form ``activate(plugin_api)``) and register
        contributions via ``register_operation`` / ``register_rest_endpoint``
        etc. during activation. Any ``Disposable`` pushed into
        ``context.subscriptions`` gets disposed on ``deactivate``.
        """
        from scenecraft import plugin_api
        import inspect

        name = getattr(plugin_module, "__name__", "<unknown>")
        if name in cls._contexts:
            # Already active. Caller should deactivate first.
            return cls._contexts[name]

        context = PluginContext(name=name)
        activate = plugin_module.activate
        sig = inspect.signature(activate)
        # Support both the 1-arg (legacy) and 2-arg shapes.
        if len(sig.parameters) >= 2:
            activate(plugin_api, context)
        else:
            activate(plugin_api)

        cls._contexts[name] = context
        cls._registered.append(name)
        return context

    @classmethod
    def deactivate(cls, name: str) -> None:
        """Dispose all subscriptions registered by a plugin during activate().

        Disposables fire in LIFO order. If the plugin exports an optional
        ``deactivate(context)`` function, it runs AFTER the subscriptions
        are disposed (gives the plugin a last-chance hook for anything
        it didn't funnel through subscriptions).

        Safe to call on a plugin that isn't registered — silent no-op.
        """
        context = cls._contexts.pop(name, None)
        if name in cls._registered:
            cls._registered.remove(name)
        if context is None:
            return

        # LIFO: last-registered Disposable disposes first.
        while context.subscriptions:
            d = context.subscriptions.pop()
            try:
                d.dispose()
            except Exception as e:  # noqa: BLE001
                import sys
                print(
                    f"[plugin-host] dispose failed for {name}: {e}",
                    file=sys.stderr,
                )

        # Optional plugin-level deactivate hook.
        try:
            import importlib
            module = importlib.import_module(name)
            deact = getattr(module, "deactivate", None)
            if deact is not None:
                deact(context)
        except ModuleNotFoundError:
            pass
        except Exception as e:  # noqa: BLE001
            import sys
            print(
                f"[plugin-host] plugin deactivate() failed for {name}: {e}",
                file=sys.stderr,
            )

    @classmethod
    def register_operation(
        cls,
        op: OperationDef,
        context: Optional[PluginContext] = None,
    ) -> Disposable:
        """Register an operation. Returns a ``Disposable`` that removes it
        from the registry when disposed. If ``context`` is provided, the
        disposable is auto-pushed into ``context.subscriptions``.
        """
        assert op.id not in cls._operations, f"duplicate operation id: {op.id}"
        cls._operations[op.id] = op

        def _dispose() -> None:
            if cls._operations.get(op.id) is op:
                del cls._operations[op.id]

        d = make_disposable(_dispose)
        if context is not None:
            context.subscriptions.append(d)
        return d

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

    # ── MCP / chat-tool contributions ────────────────────────────────

    @classmethod
    def register_mcp_tool(
        cls,
        tool: "MCPToolDef",
        context: Optional[PluginContext] = None,
    ) -> Disposable:
        """Register a plugin-contributed chat/MCP tool.

        The tool's visible name is namespaced as ``{plugin}__{tool_id}`` —
        matching the double-underscore convention used by plugin-owned DB
        tables and the existing ``isolate_vocals__run`` tool. Duplicate
        full-names raise.
        """
        full = tool.full_name
        assert full not in cls._mcp_tools, f"duplicate mcp tool id: {full}"
        cls._mcp_tools[full] = tool

        def _dispose() -> None:
            if cls._mcp_tools.get(full) is tool:
                del cls._mcp_tools[full]

        d = make_disposable(_dispose)
        if context is not None:
            context.subscriptions.append(d)
        return d

    @classmethod
    def get_mcp_tool(cls, full_name: str) -> Optional["MCPToolDef"]:
        return cls._mcp_tools.get(full_name)

    @classmethod
    def list_mcp_tools(cls) -> list["MCPToolDef"]:
        return list(cls._mcp_tools.values())

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

    # ── Test support ────────────────────────────────────────────────────

    @classmethod
    def _reset_for_tests(cls) -> None:
        """Clear all registry state. Intended for tests only."""
        # Best-effort disposal to surface resource leaks in tests too.
        for name in list(cls._contexts.keys()):
            cls.deactivate(name)
        cls._operations = {}
        cls._rest_routes = {}
        cls._contexts = {}
        cls._registered = []
