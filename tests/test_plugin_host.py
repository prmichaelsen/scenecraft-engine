"""Tests for the static PluginHost registry.

These tests exercise the MVP surface: operation registration + lookup +
filtering, duplicate detection, and REST path dispatch. Dynamic loading is out
of scope.
"""

from __future__ import annotations

import pytest

from scenecraft.plugin_host import OperationDef, PluginHost


@pytest.fixture(autouse=True)
def _reset_host():
    """Each test gets a clean PluginHost. The host is process-global, so we
    have to reset it before and after every test to avoid cross-pollution."""
    PluginHost._reset_for_tests()
    yield
    PluginHost._reset_for_tests()


# --- register_operation / get_operation ----------------------------------


def test_register_and_get_operation_round_trip():
    called = {}

    def handler(entity_type, entity_id, context):
        called["args"] = (entity_type, entity_id, context)
        return {"ok": True}

    op = OperationDef(
        id="test.op",
        label="Test Op",
        entity_types=["audio_clip"],
        handler=handler,
    )
    PluginHost.register_operation(op)

    got = PluginHost.get_operation("test.op")
    assert got is op
    result = got.handler("audio_clip", "clip-1", {"k": "v"})
    assert result == {"ok": True}
    assert called["args"] == ("audio_clip", "clip-1", {"k": "v"})


def test_get_operation_unknown_returns_none():
    assert PluginHost.get_operation("nope") is None


def test_register_operation_duplicate_raises():
    op = OperationDef(
        id="dup.op",
        label="Dup",
        entity_types=["audio_clip"],
        handler=lambda *_: {},
    )
    PluginHost.register_operation(op)

    dup = OperationDef(
        id="dup.op",
        label="Dup 2",
        entity_types=["audio_clip"],
        handler=lambda *_: {},
    )
    with pytest.raises(AssertionError, match="duplicate operation id"):
        PluginHost.register_operation(dup)


# --- list_operations ------------------------------------------------------


def test_list_operations_no_filter_returns_all():
    a = OperationDef("a", "A", ["audio_clip"], lambda *_: {})
    b = OperationDef("b", "B", ["video_clip"], lambda *_: {})
    PluginHost.register_operation(a)
    PluginHost.register_operation(b)

    ops = PluginHost.list_operations()
    assert {op.id for op in ops} == {"a", "b"}


def test_list_operations_filters_by_entity_type():
    a = OperationDef("a", "A", ["audio_clip"], lambda *_: {})
    b = OperationDef("b", "B", ["video_clip"], lambda *_: {})
    c = OperationDef("c", "C", ["audio_clip", "video_clip"], lambda *_: {})
    PluginHost.register_operation(a)
    PluginHost.register_operation(b)
    PluginHost.register_operation(c)

    audio_ops = PluginHost.list_operations("audio_clip")
    assert {op.id for op in audio_ops} == {"a", "c"}

    video_ops = PluginHost.list_operations("video_clip")
    assert {op.id for op in video_ops} == {"b", "c"}

    none_ops = PluginHost.list_operations("pool_segment")
    assert none_ops == []


# --- dispatch_rest --------------------------------------------------------


def test_dispatch_rest_matches_and_invokes_handler():
    captured = {}

    def handler(path, *args, **kwargs):
        captured["path"] = path
        captured["args"] = args
        captured["kwargs"] = kwargs
        return {"routed": path}

    PluginHost._rest_routes[r"^/api/plugins/isolate_vocals/.*$"] = handler

    result = PluginHost.dispatch_rest(
        "/api/plugins/isolate_vocals/run", "arg1", extra="ok"
    )
    assert result == {"routed": "/api/plugins/isolate_vocals/run"}
    assert captured["path"] == "/api/plugins/isolate_vocals/run"
    assert captured["args"] == ("arg1",)
    assert captured["kwargs"] == {"extra": "ok"}


def test_dispatch_rest_no_match_returns_none():
    PluginHost._rest_routes[r"^/api/plugins/foo/.*$"] = lambda *a, **k: "hit"
    assert PluginHost.dispatch_rest("/api/unrelated") is None


def test_dispatch_rest_empty_registry_returns_none():
    assert PluginHost.dispatch_rest("/anything") is None


# --- register (full module activation) -----------------------------------


def test_register_calls_plugin_activate():
    import types

    captured_api = {}

    fake_plugin = types.ModuleType("fake_plugin")

    def activate(api):
        captured_api["api"] = api

    fake_plugin.activate = activate

    PluginHost.register(fake_plugin)

    # plugin_api module should have been passed in
    assert captured_api["api"] is not None
    assert hasattr(captured_api["api"], "extract_audio_as_wav")
    assert hasattr(captured_api["api"], "register_rest_endpoint")
    assert "fake_plugin" in PluginHost._registered
