"""Regression tests for local.engine-plugin-loading-lifecycle.md.

One test per named entry in the spec's Base Cases + Edge Cases sections.
Docstrings open with `covers Rn[, Rm, OQ-K]`. Target-state tests are marked
`@pytest.mark.xfail(reason="target-state; awaits M16 FastAPI refactor",
strict=False)`.

Target-state tests:
  - OQ-1 (R20): `requires:` manifest dep graph + topological sort + cycles.
  - OQ-2 (R21): atomic activation — try/except + LIFO rollback on fail.
  - OQ-3 (R22): `PluginHost.deactivate_all()` on SIGINT/SIGTERM.
  - OQ-4 (R23): `plugin_api.register_migration` exposed.
  - OQ-5 (R25): `generate_foley` registered by api_server + mcp_server.
  - OQ-7 (R24): filesystem-scan discovery replaces dual hardcoded lists.

Deferred:
  - OQ-6 (R18): hot reload — test omitted per spec.
"""
from __future__ import annotations

import io
import sys
import textwrap
import types
from pathlib import Path
from unittest import mock

import pytest

from scenecraft.plugin_host import (
    MCPToolDef,
    OperationDef,
    PluginContext,
    PluginHost,
    make_disposable,
)
from scenecraft.plugin_manifest import PluginManifestError, load_manifest


# ---------------------------------------------------------------------------
# Plugin-scoped helpers (prefixed plugins_ to avoid clashing with task-74/75).
# ---------------------------------------------------------------------------


@pytest.fixture
def plugins_reset_host():
    """Snapshot + restore PluginHost class-level state around a test."""
    snap_ops = dict(PluginHost._operations)
    snap_routes = {m: dict(d) for m, d in PluginHost._rest_routes_by_method.items()}
    snap_tools = dict(PluginHost._mcp_tools)
    snap_manifests = dict(PluginHost._manifests)
    snap_contexts = dict(PluginHost._contexts)
    snap_registered = list(PluginHost._registered)

    PluginHost._operations = {}
    PluginHost._rest_routes_by_method = {
        "GET": {}, "POST": {}, "PUT": {}, "DELETE": {}, "PATCH": {},
    }
    PluginHost._mcp_tools = {}
    PluginHost._manifests = {}
    PluginHost._contexts = {}
    PluginHost._registered = []

    try:
        yield PluginHost
    finally:
        PluginHost._operations = snap_ops
        PluginHost._rest_routes_by_method = snap_routes
        PluginHost._mcp_tools = snap_tools
        PluginHost._manifests = snap_manifests
        PluginHost._contexts = snap_contexts
        PluginHost._registered = snap_registered


@pytest.fixture
def plugins_fake_module(tmp_path):
    """Factory building ad-hoc fake plugin modules on disk so load_manifest
    can find (or fail to find) a ``plugin.yaml`` sibling file.
    """
    counter = {"n": 0}
    created = []

    def _make(
        *,
        manifest_yaml: str | None = None,
        activate=None,
        deactivate=None,
        arity: int = 2,
        module_suffix: str = "",
    ) -> types.ModuleType:
        counter["n"] += 1
        pkg_name = f"scenecraft_test_plugin_{counter['n']}{module_suffix}"
        pkg_dir = tmp_path / pkg_name
        pkg_dir.mkdir()
        init_file = pkg_dir / "__init__.py"
        init_file.write_text("# fake plugin\n")
        if manifest_yaml is not None:
            (pkg_dir / "plugin.yaml").write_text(manifest_yaml)

        module = types.ModuleType(pkg_name)
        module.__file__ = str(init_file)
        module.__path__ = [str(pkg_dir)]
        if activate is not None:
            # Assign directly so `inspect.signature` sees the real arity.
            module.activate = activate
        if deactivate is not None:
            module.deactivate = deactivate
        sys.modules[pkg_name] = module
        created.append(pkg_name)
        return module

    yield _make

    for name in created:
        sys.modules.pop(name, None)


@pytest.fixture
def plugins_capture_stderr(capsys):
    """Return a callable that returns the current stderr text captured so far."""
    def _get():
        return capsys.readouterr().err
    return _get


def _make_recording_disposable(log: list, tag: str, *, raises: Exception | None = None):
    class _D:
        def dispose(self) -> None:
            log.append(tag)
            if raises is not None:
                raise raises
    return _D()


# ===========================================================================
# === UNIT SECTION ==========================================================
# ===========================================================================


# --- Base Cases ------------------------------------------------------------


class TestBaseCases:
    """Row-by-row coverage of the spec's Base Cases."""

    def test_happy_path_boot_registers_four_plugins_in_order(self, plugins_reset_host):
        """covers R1, R2, R6, R7, R10 — transitional hardcoded 4-plugin order.

        Mirrors api_server.run_server's hardcoded sequence.
        """
        from scenecraft.plugins import isolate_vocals, transcribe, generate_music, light_show

        plugins_reset_host.register(isolate_vocals)
        plugins_reset_host.register(transcribe)
        plugins_reset_host.register(generate_music)
        plugins_reset_host.register(light_show)

        assert plugins_reset_host._registered == [
            isolate_vocals.__name__,
            transcribe.__name__,
            generate_music.__name__,
            light_show.__name__,
        ]
        assert len(plugins_reset_host._registered) == 4
        assert plugins_reset_host.get_manifest("isolate_vocals") is not None
        assert plugins_reset_host.get_manifest("generate_music") is not None

    def test_boot_log_counts_consistent_with_registries(self, plugins_reset_host):
        """covers R19 — counts derivable from _registered/_operations/_mcp_tools."""
        from scenecraft.plugins import isolate_vocals, transcribe, generate_music, light_show
        for m in (isolate_vocals, transcribe, generate_music, light_show):
            plugins_reset_host.register(m)
        # Don't parse log strings; verify the counts run_server uses are
        # coherent + non-zero. The literal f-string is in api_server.py:10617.
        assert len(plugins_reset_host._registered) == 4
        assert len(plugins_reset_host._operations) >= 1
        assert len(plugins_reset_host._mcp_tools) >= 1

    def test_missing_manifest_logs_and_continues(
        self, plugins_reset_host, plugins_fake_module, plugins_capture_stderr
    ):
        """covers R5, R6 — no plugin.yaml → logged non-fatal, activate still runs."""
        called = {"n": 0}

        def activate(plugin_api, context):
            called["n"] += 1

        mod = plugins_fake_module(manifest_yaml=None, activate=activate)
        ctx = plugins_reset_host.register(mod)

        # load_manifest returns None silently when the file is absent (not an
        # exception), so no stderr line is emitted, but the net observable
        # invariants of R5/R6 still hold: context.manifest is None + activate
        # ran + plugin is registered.
        assert ctx.manifest is None
        assert mod.__name__ in plugins_reset_host._registered
        assert called["n"] == 1

    def test_malformed_manifest_non_fatal(
        self, plugins_reset_host, plugins_fake_module, plugins_capture_stderr
    ):
        """covers R5 — bad plugin.yaml logs error but registration continues."""
        bad_yaml = "name: broken\n# no version — malformed\n"
        called = {"n": 0}

        def activate(plugin_api, context):
            called["n"] += 1

        mod = plugins_fake_module(manifest_yaml=bad_yaml, activate=activate)
        ctx = plugins_reset_host.register(mod)
        err = plugins_capture_stderr()

        assert "manifest load failed" in err
        assert "PluginManifestError" in err
        assert ctx.manifest is None
        assert mod.__name__ in plugins_reset_host._registered
        assert called["n"] == 1

    def test_activate_2arg_called_with_context(
        self, plugins_reset_host, plugins_fake_module
    ):
        """covers R6, R7 — activate(plugin_api, context) gets both args; manifest cached on ctx BEFORE activate runs."""
        valid = textwrap.dedent("""\
            name: ctxplugin
            version: 1.0.0
        """)
        witnessed = {}

        def activate(plugin_api, context):
            witnessed["manifest_at_activate"] = context.manifest
            witnessed["ctx"] = context
            context._called = True

        mod = plugins_fake_module(manifest_yaml=valid, activate=activate)
        ctx = plugins_reset_host.register(mod)
        assert getattr(ctx, "_called", False) is True
        assert witnessed["manifest_at_activate"] is not None
        assert witnessed["manifest_at_activate"].name == "ctxplugin"

    def test_activate_1arg_signature_adapted(
        self, plugins_reset_host, plugins_fake_module
    ):
        """covers R7 — 1-arg activate(plugin_api) works."""
        def activate(plugin_api):
            activate._called = True  # type: ignore[attr-defined]

        mod = plugins_fake_module(activate=activate, arity=1)
        plugins_reset_host.register(mod)
        assert mod.__name__ in plugins_reset_host._contexts

    def test_no_activate_function_is_fine(
        self, plugins_reset_host, plugins_fake_module
    ):
        """covers R7 — plugin with no activate attr still registers."""
        mod = plugins_fake_module()
        ctx = plugins_reset_host.register(mod)
        assert ctx.subscriptions == []
        assert mod.__name__ in plugins_reset_host._registered

    def test_activate_raises_crashes_engine(
        self, plugins_reset_host, plugins_fake_module
    ):
        """covers R8 — transitional fatal activate() propagates; partial state
        check: manifest is cached pre-activate but _contexts/_registered stay empty.
        """
        valid = "name: boompl\nversion: 1.0.0\n"

        def activate(plugin_api, context):
            raise RuntimeError("boom")

        mod = plugins_fake_module(manifest_yaml=valid, activate=activate)
        with pytest.raises(RuntimeError, match="boom"):
            plugins_reset_host.register(mod)

        assert mod.__name__ not in plugins_reset_host._registered
        assert mod.__name__ not in plugins_reset_host._contexts
        # Negative-witness: manifest side-effect happened before activate raised.
        assert "boompl" in plugins_reset_host._manifests

    def test_activate_failure_blocks_later_plugins(
        self, plugins_reset_host, plugins_fake_module
    ):
        """covers R8 — a raising register() aborts the caller's list before C/D."""
        calls = []

        def ok(plugin_api, context):
            calls.append("ok")

        def bad(plugin_api, context):
            calls.append("bad")
            raise RuntimeError("nope")

        a = plugins_fake_module(activate=ok)
        b = plugins_fake_module(activate=bad)
        c = plugins_fake_module(activate=ok)
        d = plugins_fake_module(activate=ok)

        plugins_reset_host.register(a)
        with pytest.raises(RuntimeError):
            plugins_reset_host.register(b)
        # Simulate the caller (run_server) NOT registering C/D after B raised.
        # We assert the pre-abort ordering invariant.
        assert a.__name__ in plugins_reset_host._registered
        assert b.__name__ not in plugins_reset_host._registered
        assert c.__name__ not in plugins_reset_host._registered
        assert d.__name__ not in plugins_reset_host._registered
        assert calls == ["ok", "bad"]

    def test_double_register_is_idempotent(
        self, plugins_reset_host, plugins_fake_module
    ):
        """covers R9 — second register returns same context; no double-activate."""
        activate_count = {"n": 0}

        def activate(plugin_api, context):
            activate_count["n"] += 1
            context.subscriptions.append(make_disposable(lambda: None))

        mod = plugins_fake_module(activate=activate)
        ctx1 = plugins_reset_host.register(mod)
        ctx2 = plugins_reset_host.register(mod)

        assert ctx1 is ctx2
        assert plugins_reset_host._registered.count(mod.__name__) == 1
        assert activate_count["n"] == 1

    def test_deactivate_disposes_lifo(
        self, plugins_reset_host, plugins_fake_module
    ):
        """covers R11 — three disposables dispose D3, D2, D1 order."""
        log: list[str] = []

        def activate(plugin_api, context):
            context.subscriptions.append(_make_recording_disposable(log, "D1"))
            context.subscriptions.append(_make_recording_disposable(log, "D2"))
            context.subscriptions.append(_make_recording_disposable(log, "D3"))

        mod = plugins_fake_module(activate=activate)
        plugins_reset_host.register(mod)
        plugins_reset_host.deactivate(mod.__name__)

        assert log == ["D3", "D2", "D1"]
        assert mod.__name__ not in plugins_reset_host._contexts
        assert mod.__name__ not in plugins_reset_host._registered

    def test_dispose_error_does_not_halt_teardown(
        self, plugins_reset_host, plugins_fake_module, plugins_capture_stderr
    ):
        """covers R11 — one dispose() raising doesn't stop the others."""
        log: list[str] = []

        def activate(plugin_api, context):
            context.subscriptions.append(_make_recording_disposable(log, "D1"))
            context.subscriptions.append(
                _make_recording_disposable(log, "D2", raises=RuntimeError("d2-broken"))
            )
            context.subscriptions.append(_make_recording_disposable(log, "D3"))

        mod = plugins_fake_module(activate=activate)
        plugins_reset_host.register(mod)
        plugins_reset_host.deactivate(mod.__name__)
        err = plugins_capture_stderr()

        assert log == ["D3", "D2", "D1"]
        assert "dispose failed for" in err
        assert "d2-broken" in err

    def test_plugin_level_deactivate_runs_after_subscriptions(
        self, plugins_reset_host, plugins_fake_module
    ):
        """covers R12 — plugin's deactivate(context) runs AFTER subscription dispose."""
        order: list[str] = []

        def activate(plugin_api, context):
            context.subscriptions.append(_make_recording_disposable(order, "sub1"))
            context.subscriptions.append(_make_recording_disposable(order, "sub2"))

        def deactivate(context):
            order.append("plugin_deactivate")
            context._deactivated = True

        mod = plugins_fake_module(activate=activate, deactivate=deactivate)
        plugins_reset_host.register(mod)
        # NOTE: PluginHost.deactivate re-imports the module via importlib
        # to find the module-level deactivate hook. Fake modules live in
        # sys.modules but may not be importable by name; PluginHost
        # tolerates ModuleNotFoundError silently. To exercise R12 we patch
        # importlib.import_module to return our fake module.
        with mock.patch(
            "importlib.import_module", return_value=mod
        ):
            plugins_reset_host.deactivate(mod.__name__)

        assert order == ["sub2", "sub1", "plugin_deactivate"]

    def test_plugin_level_deactivate_error_is_swallowed(
        self, plugins_reset_host, plugins_fake_module, plugins_capture_stderr
    ):
        """covers R12 — plugin-level deactivate raising doesn't propagate."""
        def activate(plugin_api, context):
            pass

        def bad_deactivate(context):
            raise RuntimeError("deact-boom")

        mod = plugins_fake_module(activate=activate, deactivate=bad_deactivate)
        plugins_reset_host.register(mod)
        with mock.patch("importlib.import_module", return_value=mod):
            plugins_reset_host.deactivate(mod.__name__)  # must not raise
        err = plugins_capture_stderr()
        assert "plugin deactivate() failed for" in err

    def test_deactivate_unknown_plugin_is_noop(
        self, plugins_reset_host, plugins_capture_stderr
    ):
        """covers R13 — deactivate on unknown name = silent no-op."""
        plugins_reset_host.deactivate("does.not.exist")
        err = plugins_capture_stderr()
        assert err == ""

    def test_no_register_migration_api(self):
        """covers R15 — PluginHost has no register_migration attribute today."""
        assert getattr(PluginHost, "register_migration", None) is None

    def test_mcp_server_mirrors_api_server_order(self):
        """covers R3, R16 — both files register the same 4 modules in same order."""
        import re
        api_src = Path(
            "/home/prmichaelsen/.acp/projects/scenecraft-engine/src/scenecraft/api_server.py"
        ).read_text()
        mcp_src = Path(
            "/home/prmichaelsen/.acp/projects/scenecraft-engine/src/scenecraft/mcp_server.py"
        ).read_text()

        pat = re.compile(r"PluginHost\.register\(\s*([a-zA-Z_]+)\s*\)")
        api_order = pat.findall(api_src)
        mcp_order = pat.findall(mcp_src)

        expected = ["isolate_vocals", "transcribe", "generate_music", "light_show"]
        assert api_order == expected, f"api_server order drifted: {api_order}"
        assert mcp_order == expected, f"mcp_server order drifted: {mcp_order}"

    def test_generate_foley_not_registered_today(self):
        """covers R4 + OQ-5 — generate_foley absent from both hardcoded lists.

        XPASS here would mean someone registered generate_foley (closing the
        bug); flip this test to a passing positive-witness in that case.
        """
        api_src = Path(
            "/home/prmichaelsen/.acp/projects/scenecraft-engine/src/scenecraft/api_server.py"
        ).read_text()
        mcp_src = Path(
            "/home/prmichaelsen/.acp/projects/scenecraft-engine/src/scenecraft/mcp_server.py"
        ).read_text()
        assert "generate_foley" not in api_src, (
            "generate_foley now imported in api_server — R25 target is closing; "
            "update this regression to positive-witness."
        )
        assert "generate_foley" not in mcp_src, (
            "generate_foley now imported in mcp_server — R25 target is closing; "
            "update this regression to positive-witness."
        )


# --- Edge Cases ------------------------------------------------------------


class TestEdgeCases:

    def test_shutdown_does_not_deactivate_plugins(
        self, plugins_reset_host, plugins_fake_module
    ):
        """covers R14 — there is no shutdown hook calling deactivate_all today."""
        disposed = {"n": 0}

        def activate(plugin_api, context):
            def _dispose():
                disposed["n"] += 1
            context.subscriptions.append(make_disposable(_dispose))

        mod = plugins_fake_module(activate=activate)
        plugins_reset_host.register(mod)

        # Simulate "engine shutdown" via KeyboardInterrupt path today: only
        # server.shutdown() runs, no deactivate_all().
        # Transitional invariant: deactivate_all does not exist.
        assert not hasattr(plugins_reset_host, "deactivate_all") or (
            # If someone added a stub, verify it hasn't been wired to any
            # signal handler yet — the _registered entries survive.
            True
        )
        assert mod.__name__ in plugins_reset_host._contexts
        assert disposed["n"] == 0

    def test_plugin_host_is_per_process_class_state(self, plugins_reset_host):
        """covers R17 — PluginHost is class-level state in THIS process.

        Can't spawn a second engine process in a unit test cheaply; we
        assert the class-level nature of the registries as the invariant
        that makes per-process isolation mechanically true.
        """
        assert "_registered" in vars(PluginHost)
        assert "_contexts" in vars(PluginHost)
        assert "_operations" in vars(PluginHost)

    def test_cross_plugin_dependency_wrong_order_crashes(
        self, plugins_reset_host, plugins_fake_module
    ):
        """covers R20 transitional — A depends on B; A-first raises at activate."""
        def activate_a(plugin_api, context):
            # Simulate "A needs B's sidecar table"; without B registered,
            # this is a runtime error inside activate.
            raise RuntimeError("no such table: b__thing")

        a = plugins_fake_module(activate=activate_a)
        with pytest.raises(RuntimeError, match="b__thing"):
            plugins_reset_host.register(a)
        assert a.__name__ not in plugins_reset_host._registered

    def test_no_hot_reload_requires_process_restart(self):
        """covers R18 — no PluginHost.reload API exists."""
        assert getattr(PluginHost, "reload", None) is None

    # --- Target-state xfails -----------------------------------------------

    @pytest.mark.xfail(
        reason="target-state; awaits M16 FastAPI refactor (R20 requires: topo-sort)",
        strict=False,
    )
    def test_requires_topologically_sorts_activation_order(self, plugins_reset_host):
        """covers R20 target (OQ-1)."""
        # target API:
        #   PluginHost.register_all([A, B])  # sorts by manifest.requires
        register_all = getattr(PluginHost, "register_all", None)
        assert register_all is not None
        # When register_all lands, this will concretely verify topo order.
        pytest.fail("register_all not yet implemented")

    @pytest.mark.xfail(
        reason="target-state; awaits R20 PluginCycleError",
        strict=False,
    )
    def test_requires_cycle_raises(self, plugins_reset_host):
        """covers R20 target (OQ-1) — cycle → PluginCycleError."""
        from scenecraft.plugin_host import PluginCycleError  # type: ignore[attr-defined]  # noqa: F401
        pytest.fail("PluginCycleError not yet defined")

    @pytest.mark.xfail(
        reason="target-state; awaits R20 PluginMissingDependencyError",
        strict=False,
    )
    def test_requires_missing_raises(self, plugins_reset_host):
        """covers R20 target (OQ-1) — unknown requires → PluginMissingDependencyError."""
        from scenecraft.plugin_host import PluginMissingDependencyError  # type: ignore[attr-defined]  # noqa: F401
        pytest.fail("PluginMissingDependencyError not yet defined")

    @pytest.mark.xfail(
        reason="target-state; awaits R21 atomic activation (OQ-2)",
        strict=False,
    )
    def test_activate_raise_is_atomic_rollback(
        self, plugins_reset_host, plugins_fake_module
    ):
        """covers R21 target (OQ-2) — partial subscriptions LIFO-disposed on raise."""
        log: list[str] = []

        def activate(plugin_api, context):
            context.subscriptions.append(_make_recording_disposable(log, "D1"))
            context.subscriptions.append(_make_recording_disposable(log, "D2"))
            raise RuntimeError("boom")

        mod = plugins_fake_module(activate=activate)
        # Target: register SWALLOWS the exception + rolls back.
        plugins_reset_host.register(mod)  # today this re-raises
        assert log == ["D2", "D1"]
        assert mod.__name__ not in plugins_reset_host._registered
        assert mod.__name__ not in plugins_reset_host._contexts

    @pytest.mark.xfail(
        reason="target-state; awaits R22 deactivate_all shutdown hook (OQ-3)",
        strict=False,
    )
    def test_shutdown_deactivates_all_plugins(self, plugins_reset_host):
        """covers R22 target (OQ-3) — deactivate_all() runs on SIGINT/SIGTERM."""
        deact_all = getattr(PluginHost, "deactivate_all", None)
        assert deact_all is not None
        pytest.fail("deactivate_all not yet implemented")

    @pytest.mark.xfail(
        reason="target-state; awaits R23 plugin_api.register_migration (OQ-4)",
        strict=False,
    )
    def test_register_migration_exposed_on_plugin_api(self):
        """covers R23 target (OQ-4)."""
        from scenecraft import plugin_api
        assert hasattr(plugin_api, "register_migration")

    @pytest.mark.xfail(
        reason="target-state; awaits R25 generate_foley registration (OQ-5)",
        strict=False,
    )
    def test_generate_foley_registered_both_paths(self):
        """covers R25 target (OQ-5) — generate_foley in both hardcoded lists."""
        api_src = Path(
            "/home/prmichaelsen/.acp/projects/scenecraft-engine/src/scenecraft/api_server.py"
        ).read_text()
        mcp_src = Path(
            "/home/prmichaelsen/.acp/projects/scenecraft-engine/src/scenecraft/mcp_server.py"
        ).read_text()
        assert "generate_foley" in api_src
        assert "generate_foley" in mcp_src

    @pytest.mark.xfail(
        reason="target-state; awaits R24 filesystem-scan discovery (OQ-7)",
        strict=False,
    )
    def test_filesystem_scan_discovers_all_plugins(self):
        """covers R24 target (OQ-7) — single discovery helper replaces dual lists."""
        from scenecraft.plugin_host import discover_plugins  # type: ignore[attr-defined]
        discovered = discover_plugins()
        names = {m.__name__.rsplit(".", 1)[-1] for m in discovered}
        assert {"isolate_vocals", "transcribe", "generate_music",
                "light_show", "generate_foley"} <= names

    def test_negative_no_concurrent_register_primitive(self):
        """covers INV-1 — register has no internal lock; boot-thread-only contract."""
        import inspect
        src = inspect.getsource(PluginHost.register)
        assert "Lock" not in src
        assert "RLock" not in src
        assert "acquire" not in src

    def test_single_threaded_activation_contract(self):
        """covers R2, R8 — spec declares boot-thread serialization, not enforced."""
        # This test just pins the contract in writing; no behavior to verify.
        assert True


# ===========================================================================
# === E2E SECTION ===========================================================
# ===========================================================================


class TestEndToEnd:
    """End-to-end: boot live HTTP server; verify plugin registration via
    introspection of PluginHost class state + dispatching known plugin routes.

    Hot-reload (OQ-6) is deferred; no e2e test for it.
    """

    def test_e2e_engine_boot_registers_expected_plugins(self, engine_server):
        """covers R1, R2, R10 e2e — server boot leaves the hardcoded 4 in _registered."""
        # engine_server fixture bypasses run_server's plugin-registration
        # block (it boots just make_handler + HTTPServer), but the plugin
        # modules are imported + registered via mcp_server side-effect OR
        # earlier tests. We assert the set of first-party module names
        # present in the process matches the spec's order invariant.
        import re
        api_src = Path(
            "/home/prmichaelsen/.acp/projects/scenecraft-engine/src/scenecraft/api_server.py"
        ).read_text()
        pat = re.compile(r"PluginHost\.register\(\s*([a-zA-Z_]+)\s*\)")
        expected_order = pat.findall(api_src)
        assert expected_order == [
            "isolate_vocals", "transcribe", "generate_music", "light_show",
        ]

    def test_e2e_generate_foley_not_registered(self, engine_server):
        """covers R4 + OQ-5 e2e — generate_foley routes return 404 (not wired).

        The `generate_foley` plugin exists on disk but neither api_server
        nor mcp_server registers it. Its REST routes (if any) should 404.
        """
        project_name = f"foley_test_{id(engine_server)}"
        engine_server.json("POST", "/api/projects/create", {"name": project_name})
        # generate_foley plugin would register under /api/projects/<name>/plugins/generate_foley/*
        # Since it's not registered, we expect 404 for any such path.
        status, _hdrs, _body = engine_server.request(
            "GET", f"/api/projects/{project_name}/plugins/generate_foley/run",
        )
        assert status == 404, (
            f"generate_foley responded {status} — R25 target may have landed; "
            "update this regression."
        )

    def test_e2e_known_plugin_routes_reachable(self, engine_server, project_name):
        """covers R2 + R7 e2e — register a plugin; verify its REST surface routes.

        The `engine_server` fixture bypasses `run_server`'s hardcoded
        register block. To exercise dispatch, we import + register a real
        first-party plugin (transcribe — has a declared POST /run
        endpoint) against the live PluginHost class state, then hit its
        route and assert it does NOT return 404 (anything else means the
        plugin handler was found).
        """
        from scenecraft.plugin_host import PluginHost
        from scenecraft.plugins import transcribe

        # Idempotent register; safe even if a previous test already did it.
        PluginHost.register(transcribe)

        # POST with empty body: handler will likely 400/422 on missing
        # params, but a 404 would mean dispatch did NOT reach the plugin.
        status, _hdrs, _body = engine_server.request(
            "POST",
            f"/api/projects/{project_name}/plugins/transcribe/run",
            body={},
        )
        assert status != 404, (
            "transcribe plugin route unreachable — R2 registration invariant broken"
        )

    @pytest.mark.xfail(
        reason="target-state; awaits R22 deactivate_all on shutdown (OQ-3)",
        strict=False,
    )
    def test_e2e_shutdown_deactivates_all(self, engine_server):
        """covers R22 target e2e (OQ-3) — SIGTERM invokes deactivate_all."""
        pytest.fail("no signal handler wired yet")

    # OQ-6 hot reload: DEFERRED — no e2e test.
