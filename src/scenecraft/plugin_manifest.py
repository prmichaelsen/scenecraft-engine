"""plugin.yaml parser + typed manifest schema.

First-party plugins ship a ``plugin.yaml`` next to their ``__init__.py``.
This module reads that file, validates the minimum shape, and produces a
typed ``PluginManifest`` the loader consumes.

The manifest is the source of truth for declarative contributions —
operations, chat/MCP tools, context menus, activation events, and plugin
settings. The plugin's Python ``activate(plugin_api, context)`` hook
remains available for side effects that can't be expressed in YAML
(background threads, model pre-warming, etc.) but should NOT re-register
anything already declared in the manifest.

Handler strings use the format ``"backend:<dotted_attr>"`` (or just
``"<dotted_attr>"``). The path is resolved against the plugin's Python
module root:

    plugins/transcribe/plugin.yaml        handler: "backend:handle_transcribe_clip"
    plugins/transcribe/__init__.py        from .handlers import handle_transcribe_clip

``handle_transcribe_clip`` must therefore be importable from the plugin
package (directly or re-exported via ``__init__``). A ``frontend:...``
prefix passes through as metadata without Python resolution.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType
from typing import Any, Callable


# ── Exceptions ──────────────────────────────────────────────────────────


class PluginManifestError(ValueError):
    """Raised when a plugin.yaml is malformed or references a handler that
    can't be resolved in the plugin's Python module."""


# ── Typed manifest schema ───────────────────────────────────────────────


@dataclass
class OperationManifest:
    id: str
    label: str
    entity_types: list[str]
    handler_ref: str
    # Optional frontend panel reference ('frontend:...'), stored as-is.
    panel_ref: str | None = None
    outputs: list[dict] = field(default_factory=list)


@dataclass
class MCPToolManifest:
    tool_id: str
    description: str
    input_schema: dict
    handler_ref: str
    destructive: bool = False


@dataclass
class SettingSpec:
    name: str
    type: str                     # "enum" | "string" | "boolean" | "number"
    default: Any
    values: list[Any] | None = None
    description: str = ""


@dataclass
class ContextMenuItem:
    operation_id: str
    label: str
    icon: str | None = None
    reveals: str | None = None


@dataclass
class ContextMenuBinding:
    entity_type: str
    items: list[ContextMenuItem]


@dataclass
class PluginManifest:
    # Identification
    name: str                     # plugin id — appears in namespaced tool names as `{name}__{tool_id}`
    version: str
    display_name: str = ""
    description: str = ""
    publisher: str = ""
    license_: str = ""

    # Declarative contributions
    operations: list[OperationManifest] = field(default_factory=list)
    mcp_tools: list[MCPToolManifest] = field(default_factory=list)
    settings: list[SettingSpec] = field(default_factory=list)
    context_menus: list[ContextMenuBinding] = field(default_factory=list)
    activation_events: list[str] = field(default_factory=list)

    # Introspection helpers
    @property
    def settings_defaults(self) -> dict[str, Any]:
        """Merge-ready dict of {setting_name: default_value}."""
        return {s.name: s.default for s in self.settings}


# ── Parsing ─────────────────────────────────────────────────────────────


def _as_list(v: Any) -> list:
    if v is None:
        return []
    if isinstance(v, list):
        return v
    return [v]


def _parse_operation(raw: dict) -> OperationManifest:
    if not isinstance(raw, dict):
        raise PluginManifestError(f"operation entry must be a mapping, got {type(raw).__name__}")
    for req in ("id", "label", "entityTypes", "handler"):
        if req not in raw:
            raise PluginManifestError(f"operation missing required field: {req!r}")
    return OperationManifest(
        id=str(raw["id"]),
        label=str(raw["label"]),
        entity_types=list(raw["entityTypes"]),
        handler_ref=str(raw["handler"]),
        panel_ref=str(raw["panel"]) if raw.get("panel") else None,
        outputs=list(raw.get("outputs") or []),
    )


def _parse_mcp_tool(raw: dict) -> MCPToolManifest:
    if not isinstance(raw, dict):
        raise PluginManifestError(f"mcpTools entry must be a mapping, got {type(raw).__name__}")
    for req in ("id", "description", "handler", "input_schema"):
        if req not in raw:
            raise PluginManifestError(f"mcpTools entry missing required field: {req!r}")
    schema = raw["input_schema"]
    if not isinstance(schema, dict):
        raise PluginManifestError("mcpTools.input_schema must be a mapping")
    return MCPToolManifest(
        tool_id=str(raw["id"]),
        description=str(raw["description"]),
        input_schema=schema,
        handler_ref=str(raw["handler"]),
        destructive=bool(raw.get("destructive", False)),
    )


def _parse_setting(name: str, raw: dict) -> SettingSpec:
    if not isinstance(raw, dict):
        raise PluginManifestError(f"setting {name!r} must be a mapping")
    tp = str(raw.get("type", "string"))
    valid = {"enum", "string", "boolean", "number"}
    if tp not in valid:
        raise PluginManifestError(
            f"setting {name!r}: unknown type {tp!r} (valid: {sorted(valid)})"
        )
    default = raw.get("default")
    values = raw.get("values")
    if tp == "enum" and not isinstance(values, list):
        raise PluginManifestError(f"setting {name!r}: type=enum requires 'values: [...]'")
    return SettingSpec(
        name=name,
        type=tp,
        default=default,
        values=list(values) if values is not None else None,
        description=str(raw.get("description", "")),
    )


def _parse_context_menu(raw: dict) -> ContextMenuBinding:
    if not isinstance(raw, dict):
        raise PluginManifestError(f"contextMenus entry must be a mapping, got {type(raw).__name__}")
    if "entityType" not in raw:
        raise PluginManifestError("contextMenus entry missing 'entityType'")
    items: list[ContextMenuItem] = []
    for it in (raw.get("items") or []):
        if "operation" not in it:
            raise PluginManifestError("contextMenus item missing 'operation'")
        items.append(ContextMenuItem(
            operation_id=str(it["operation"]),
            label=str(it.get("label", "")),
            icon=(str(it["icon"]) if it.get("icon") else None),
            reveals=(str(it["reveals"]) if it.get("reveals") else None),
        ))
    return ContextMenuBinding(
        entity_type=str(raw["entityType"]),
        items=items,
    )


def parse_manifest(data: dict) -> PluginManifest:
    """Parse a pre-loaded dict (from yaml.safe_load) into the typed schema."""
    if not isinstance(data, dict):
        raise PluginManifestError(f"plugin manifest must be a mapping, got {type(data).__name__}")
    for req in ("name", "version"):
        if req not in data:
            raise PluginManifestError(f"plugin manifest missing required field: {req!r}")
    name = str(data["name"])
    if "__" in name:
        raise PluginManifestError(
            f"plugin id {name!r} must not contain '__' — "
            "that's the namespace separator for contributed chat tools."
        )

    contributes = data.get("contributes") or {}
    operations = [_parse_operation(o) for o in _as_list(contributes.get("operations"))]
    mcp_tools = [_parse_mcp_tool(t) for t in _as_list(contributes.get("mcpTools"))]
    context_menus = [_parse_context_menu(m) for m in _as_list(contributes.get("contextMenus"))]

    raw_settings = data.get("settings") or {}
    if not isinstance(raw_settings, dict):
        raise PluginManifestError("top-level 'settings' must be a mapping of name -> spec")
    settings = [_parse_setting(n, s) for n, s in raw_settings.items()]

    return PluginManifest(
        name=name,
        version=str(data["version"]),
        display_name=str(data.get("displayName", "")),
        description=str(data.get("description", "")),
        publisher=str(data.get("publisher", "")),
        license_=str(data.get("license", "")),
        operations=operations,
        mcp_tools=mcp_tools,
        settings=settings,
        context_menus=context_menus,
        activation_events=[str(e) for e in _as_list(data.get("activationEvents"))],
    )


def load_manifest(plugin_module: ModuleType) -> PluginManifest | None:
    """Locate + parse the plugin.yaml next to a plugin's __init__.py.

    Returns ``None`` if no manifest file exists — plugins are free to be
    purely imperative. Any parse / schema error raises PluginManifestError.
    """
    module_file = getattr(plugin_module, "__file__", None)
    if not module_file:
        return None
    manifest_path = Path(module_file).parent / "plugin.yaml"
    if not manifest_path.exists():
        return None
    import yaml  # local import keeps yaml optional at module-load time
    with open(manifest_path, "rb") as f:
        data = yaml.safe_load(f)
    return parse_manifest(data or {})


# ── Handler resolution ──────────────────────────────────────────────────


def resolve_handler(plugin_module: ModuleType, handler_ref: str) -> Callable:
    """Map a manifest handler reference to a Python callable.

    Accepted formats:
      ``"foo"``              -> plugin_module.foo
      ``"backend:foo"``      -> same, prefix stripped
      ``"backend:impl.run"`` -> plugin_module.impl.run (walked via getattr)

    Frontend-only refs (``"frontend:..."``) aren't callables; callers
    should filter those out before reaching here. Raises
    PluginManifestError on misses so the startup log is loud.
    """
    ref = handler_ref
    if ref.startswith("backend:"):
        ref = ref[len("backend:"):]
    elif ref.startswith("frontend:"):
        raise PluginManifestError(
            f"handler ref {handler_ref!r} is a frontend reference; "
            "cannot resolve to a Python callable."
        )
    parts = ref.split(".")
    obj: Any = plugin_module
    for part in parts:
        try:
            obj = getattr(obj, part)
        except AttributeError as exc:
            raise PluginManifestError(
                f"handler {handler_ref!r}: attribute {part!r} not found on "
                f"{getattr(plugin_module, '__name__', '<plugin>')}"
            ) from exc
    if not callable(obj):
        raise PluginManifestError(
            f"handler {handler_ref!r} resolved to non-callable: {type(obj).__name__}"
        )
    return obj


def _log(msg: str) -> None:
    print(f"[plugin-manifest] {msg}", file=sys.stderr, flush=True)
