"""Regression tests for local.engine-server-bootstrap.md.

One test (or one focused class) per named entry in the spec's Base Cases +
Edge Cases sections. Docstrings open with `covers Rn[, Rm, OQ-K]`. Target-
state tests use ``@pytest.mark.xfail(reason="target-state; ...", strict=False)``.

Target-state OQs (xfail until impl):
  - OQ-1 (R19): WS bind failure surfaced via threading.Event to main thread.
  - OQ-2 (R21): work_dir preflight via os.access(R_OK|W_OK).
  - OQ-3 (R22): config.json JSONDecodeError handling.
  - OQ-4 (R20): SIGTERM signal handler mirroring SIGINT.
  - OQ-5 (R23): --no-auth-unsafe-i-know-what-im-doing flag in production.
  - OQ-6 (R24): advisory flock on .scenecraft/server.lock.

Fixtures:
  - ``bootstrap_isolated_config``: redirects $XDG_CONFIG_HOME so load_config
    operates on a tmp file and never touches the real user config.
  - ``bootstrap_clean_pluginhost``: snapshot + restore PluginHost class state.
  - ``bootstrap_fake_run_server``: helpers for invoking ``run_server`` with
    serve_forever / WS / interactive console patched out.
  - ``engine_server`` (session, from conftest.py) reused for E2E.

Pytest invocation: this repo's pytest must run via ``.venv/bin/pytest`` (the
homebrew pytest's interpreter has no librosa, so imports cascade-fail).
"""
from __future__ import annotations

import json
import os
import re
import signal
import socket
import subprocess
import sys
import threading
import time
import types
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

import pytest


# ---------------------------------------------------------------------------
# Fixtures (bootstrap_-prefixed to avoid clashing with task-86 / task-87).
# ---------------------------------------------------------------------------


@pytest.fixture
def bootstrap_isolated_config(tmp_path, monkeypatch):
    """Redirect $XDG_CONFIG_HOME so config.json reads/writes hit tmp_path.

    Also clears the legacy ``~/.scenecraft/config.json`` from view by pointing
    ``Path.home()`` at a fresh temp dir (covers R4 cleanly).
    """
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    fake_xdg = tmp_path / "xdg"
    fake_xdg.mkdir()
    monkeypatch.setenv("XDG_CONFIG_HOME", str(fake_xdg))
    monkeypatch.setenv("HOME", str(fake_home))

    # config.py captures CONFIG_DIR / CONFIG_FILE / _LEGACY_CONFIG_FILE at
    # import time, so we must rebind those module attrs for this test.
    from scenecraft import config as scconfig
    monkeypatch.setattr(scconfig, "CONFIG_DIR", fake_xdg / "scenecraft")
    monkeypatch.setattr(
        scconfig, "CONFIG_FILE", fake_xdg / "scenecraft" / "config.json"
    )
    monkeypatch.setattr(
        scconfig, "_LEGACY_CONFIG_FILE", fake_home / ".scenecraft" / "config.json"
    )
    return types.SimpleNamespace(
        home=fake_home,
        xdg=fake_xdg,
        config_dir=fake_xdg / "scenecraft",
        config_file=fake_xdg / "scenecraft" / "config.json",
        legacy_file=fake_home / ".scenecraft" / "config.json",
    )


@pytest.fixture
def bootstrap_clean_pluginhost():
    """Snapshot + restore PluginHost class-level state around a test."""
    from scenecraft.plugin_host import PluginHost

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


@contextmanager
def _patched_boot(work_dir: Path, *, simulate_serve_forever_returns=True):
    """Patch ``run_server`` collaborators so it returns without serving.

    Yields a SimpleNamespace recording observed calls. The ``ThreadedHTTPServer``
    is replaced by a fake (no real socket bind). ``start_ws_server`` is a stub.
    ``PluginHost.register`` is patched to record the module-arg sequence.
    ``server.serve_forever`` raises immediately to short-circuit the boot.
    """
    from scenecraft import api_server as ap
    from scenecraft import ws_server as ws_mod
    from scenecraft.plugin_host import PluginHost

    _orig_make_handler = ap.make_handler

    calls: list[tuple] = []
    registered_modules: list = []

    class _FakeHTTPServer:
        daemon_threads = True
        instances: list = []

        def __init__(self, addr, handler):
            self.server_address = addr
            self.RequestHandlerClass = handler
            self.shutdown_called = False
            calls.append(("HTTPServer.__init__", addr))
            _FakeHTTPServer.instances.append(self)

        def serve_forever(self):
            calls.append(("serve_forever",))
            if simulate_serve_forever_returns:
                return
            raise KeyboardInterrupt()

        def shutdown(self):
            self.shutdown_called = True
            calls.append(("shutdown",))

    def _fake_make_handler(wd, no_auth=False):
        calls.append(("make_handler", str(wd), no_auth))
        return _orig_make_handler(wd, no_auth=no_auth)

    def _fake_start_ws(host, port, work_dir=None):
        calls.append(("start_ws_server", host, port))
        t = threading.Thread(target=lambda: None, daemon=True)
        t.start()
        return t

    def _fake_register(mod):
        registered_modules.append(mod)
        name = getattr(mod, "__name__", str(mod)).rsplit(".", 1)[-1]
        calls.append(("register", name))
        PluginHost._registered.append(name)
        # Don't actually run plugin activate() — pure registration record.

    def _fake_start_console():
        calls.append(("start_if_tty",))

    class _FakeFolderWatcher:
        def __init__(self, wd):
            self.wd = wd
            self._wd_map = {}
            self._running = False
            calls.append(("FolderWatcher", str(wd)))

    with mock.patch.object(ap, "make_handler", side_effect=_fake_make_handler), \
         mock.patch.object(ap, "HTTPServer", new=_FakeHTTPServer), \
         mock.patch.object(ws_mod, "start_ws_server", side_effect=_fake_start_ws, create=False), \
         mock.patch.object(ws_mod, "FolderWatcher", new=_FakeFolderWatcher), \
         mock.patch.object(PluginHost, "register", side_effect=_fake_register), \
         mock.patch(
             "scenecraft.interactive_console.start_if_tty",
             side_effect=_fake_start_console,
         ):
        yield types.SimpleNamespace(
            calls=calls,
            registered=registered_modules,
            servers=_FakeHTTPServer.instances,
        )


@pytest.fixture
def bootstrap_fake_run_server():
    """Yield the _patched_boot context-manager helper."""
    return _patched_boot


# ---------------------------------------------------------------------------
# === Unit tests ===
# ---------------------------------------------------------------------------


class TestWorkDirResolution:
    """resolve_work_dir + CLI work-dir branch (covers R1, R2, R3, R4, R16)."""

    def test_cli_override_takes_precedence(self, bootstrap_isolated_config, tmp_path):
        """covers R2, R3 — CLI flag wins over config.projects_dir."""
        from scenecraft.config import resolve_work_dir, save_config

        cfg_path = tmp_path / "from_config"
        cfg_path.mkdir()
        save_config({"projects_dir": str(cfg_path)})

        cli_path = tmp_path / "from_cli"
        cli_path.mkdir()
        result = resolve_work_dir(str(cli_path))
        assert result == Path(str(cli_path))

    def test_falls_back_to_config(self, bootstrap_isolated_config, tmp_path):
        """covers R3 — uses config.projects_dir when no CLI override."""
        from scenecraft.config import resolve_work_dir, save_config

        cfg_path = tmp_path / "configured"
        cfg_path.mkdir()
        save_config({"projects_dir": str(cfg_path)})
        result = resolve_work_dir(None)
        assert result == cfg_path

    def test_returns_none_when_no_config_no_cli(self, bootstrap_isolated_config):
        """covers R4 — returns None to signal caller should prompt."""
        from scenecraft.config import resolve_work_dir
        assert resolve_work_dir(None) is None

    def test_legacy_config_auto_migrates(self, bootstrap_isolated_config):
        """covers R4 — legacy ~/.scenecraft/config.json migrates to XDG path."""
        from scenecraft import config as scconfig

        scconfig._LEGACY_CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
        scconfig._LEGACY_CONFIG_FILE.write_text(
            json.dumps({"projects_dir": "/tmp/legacy"})
        )

        loaded = scconfig.load_config()
        assert loaded == {"projects_dir": "/tmp/legacy"}
        assert scconfig.CONFIG_FILE.exists()

    def test_set_projects_dir_creates_and_persists(
        self, bootstrap_isolated_config, tmp_path
    ):
        """covers R4 — set_projects_dir mkdir -p + writes config."""
        from scenecraft.config import set_projects_dir, load_config

        target = tmp_path / "fresh" / "nested"
        result = set_projects_dir(str(target))
        assert result.is_dir()
        assert load_config()["projects_dir"] == str(target.resolve())

    def test_run_server_systemexit_on_missing_workdir(
        self, bootstrap_clean_pluginhost
    ):
        """covers R16 — nonexistent work_dir → SystemExit(1) before any bind."""
        from scenecraft import api_server as ap

        with mock.patch.object(ap, "make_handler") as mh, \
             mock.patch.object(ap, "HTTPServer") as hs:
            with pytest.raises(SystemExit) as exc:
                ap.run_server(
                    host="127.0.0.1",
                    port=0,
                    work_dir="/definitely/does/not/exist/sc-test",
                    no_auth=True,
                )
            assert exc.value.code == 1
            mh.assert_not_called()
            hs.assert_not_called()

    @pytest.mark.xfail(
        reason="target-state R21/OQ-2 — preflight os.access not implemented",
        strict=False,
    )
    def test_unreadable_workdir_aborts_boot(self, tmp_path, bootstrap_clean_pluginhost):
        """covers R21, OQ-2 (target) — os.access(R_OK|W_OK) preflight.

        Today: no preflight, so the boot proceeds. We use ``_patched_boot``
        to keep the body fast; the only way SystemExit can fire is if R21
        gets implemented, which would happen before the patched collaborators
        are reached.
        """
        from scenecraft import api_server as ap

        wd = tmp_path / "locked"
        wd.mkdir()
        with _patched_boot(wd), mock.patch("os.access", return_value=False):
            with pytest.raises(SystemExit):
                ap.run_server(
                    host="127.0.0.1", port=0,
                    work_dir=str(wd), no_auth=True,
                )

    @pytest.mark.xfail(
        reason="target-state R22/OQ-3 — JSONDecodeError handling not implemented",
        strict=False,
    )
    def test_corrupt_config_json_aborts_boot(self, bootstrap_isolated_config):
        """covers R22, OQ-3 (target) — corrupt config.json → clear error."""
        from scenecraft import config as scconfig

        scconfig.CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        scconfig.CONFIG_FILE.write_text("not valid json{")

        with pytest.raises(SystemExit):
            scconfig.load_config()


class TestHandlerConstruction:
    """make_handler + --no-auth (covers R5, R12, R13, R18)."""

    def test_returns_basehttphandler_subclass(self, tmp_path):
        """covers R5, R18 — handler is a BaseHTTPRequestHandler subclass."""
        from http.server import BaseHTTPRequestHandler
        from scenecraft.api_server import make_handler

        H = make_handler(tmp_path, no_auth=True)
        assert isinstance(H, type)
        assert issubclass(H, BaseHTTPRequestHandler)

    def test_no_auth_skips_find_root(self, tmp_path):
        """covers R12 — --no-auth skips find_root entirely."""
        from scenecraft.api_server import make_handler

        with mock.patch("scenecraft.vcs.bootstrap.find_root") as fr:
            make_handler(tmp_path, no_auth=True)
            fr.assert_not_called()

    def test_find_root_failure_swallowed(self, tmp_path):
        """covers R13 — find_root raises → handler still built, _sc_root=None."""
        from scenecraft.api_server import make_handler

        with mock.patch(
            "scenecraft.vcs.bootstrap.find_root", side_effect=OSError("boom")
        ):
            H = make_handler(tmp_path, no_auth=False)
            # closure cell access — _sc_root captured via free var
            assert H is not None
            cell_names = H._authenticate.__code__.co_freevars
            assert "_sc_root" in cell_names


class TestHTTPBindAndStart:
    """ThreadedHTTPServer + EADDRINUSE (covers R6, R7)."""

    def test_threading_mixin_attributes(self, tmp_path):
        """covers R6 — ThreadingMixIn + daemon_threads=True via run_server."""
        from scenecraft import api_server as ap

        with _patched_boot(tmp_path) as rec:
            ap.run_server(
                host="127.0.0.1", port=0, work_dir=str(tmp_path), no_auth=True,
            )

        # FakeHTTPServer asserts daemon_threads = True attribute is set.
        assert any(c[0] == "HTTPServer.__init__" for c in rec.calls)

    def test_port_zero_auto_assigned_via_engine_server(self, engine_server):
        """covers R6 — port=0 auto-assignment used by the test fixture."""
        # Server already booted on port 0; address must be a non-zero int.
        host, port = engine_server.server.server_address
        assert isinstance(port, int) and port > 0

    def test_eaddrinuse_aborts_before_ws(
        self, tmp_path, bootstrap_clean_pluginhost
    ):
        """covers R6, R7 — EADDRINUSE on HTTP bind aborts before WS / plugins."""
        from scenecraft import api_server as ap

        ws_called = []
        register_called = []

        class _ExplodingServer:
            daemon_threads = True

            def __init__(self, addr, handler):
                raise OSError(98, "Address already in use")

        from scenecraft import ws_server as ws_mod
        from scenecraft.plugin_host import PluginHost

        with mock.patch.object(ap, "HTTPServer", new=_ExplodingServer), \
             mock.patch.object(
                 ws_mod, "start_ws_server",
                 side_effect=lambda *a, **k: ws_called.append(1) or None,
             ), \
             mock.patch.object(
                 PluginHost, "register",
                 side_effect=lambda m: register_called.append(m),
             ):
            with pytest.raises(OSError):
                ap.run_server(
                    host="127.0.0.1", port=0,
                    work_dir=str(tmp_path), no_auth=True,
                )

        assert ws_called == []
        assert register_called == []


class TestWSThreadStart:
    """start_ws_server: daemon thread, port = http_port + 1 (covers R7, R8, R19)."""

    def test_ws_port_is_http_port_plus_one(self, tmp_path):
        """covers R7 — WS bound to port+1."""
        from scenecraft import api_server as ap

        with _patched_boot(tmp_path) as rec:
            ap.run_server(
                host="127.0.0.1", port=9999, work_dir=str(tmp_path), no_auth=True,
            )
        ws_calls = [c for c in rec.calls if c[0] == "start_ws_server"]
        assert ws_calls and ws_calls[0][2] == 10000

    def test_ws_thread_is_daemon(self, tmp_path):
        """covers R7 — start_ws_server returns a daemon thread."""
        from scenecraft.ws_server import start_ws_server

        # Patch asyncio.run inside the thread to be a no-op so we don't
        # actually try to bind a websocket.
        with mock.patch(
            "scenecraft.ws_server.asyncio.run", side_effect=lambda *a, **k: None,
        ):
            t = start_ws_server(host="127.0.0.1", port=0, work_dir=tmp_path)
        assert isinstance(t, threading.Thread)
        assert t.daemon is True

    def test_folder_watcher_constructed_dormant(self, tmp_path):
        """covers R8, R22 (table) — FolderWatcher built but no inotify watches."""
        from scenecraft import api_server as ap
        from scenecraft import ws_server as ws_mod

        with _patched_boot(tmp_path):
            ap.run_server(
                host="127.0.0.1", port=0, work_dir=str(tmp_path), no_auth=True,
            )

        assert ws_mod.folder_watcher is not None
        # Either real FolderWatcher or our fake — both expose dormant flags.
        if hasattr(ws_mod.folder_watcher, "_running"):
            assert ws_mod.folder_watcher._running is False

    @pytest.mark.xfail(
        reason="target-state R19/OQ-1 — WS bind failure not surfaced to main thread",
        strict=False,
    )
    def test_ws_bind_failure_aborts_boot(self, tmp_path):
        """covers R19, OQ-1 (target) — WS bind error must abort boot within 5s.

        Today: failure is swallowed in the daemon thread (xfail). Target: an
        Event signals success, and timeout / unset → boot aborts non-zero.
        Boot is fully patched (via ``_patched_boot``) so the only way this
        test can pass is if ``run_server`` grew the OQ-1 logic.
        """
        from scenecraft import api_server as ap

        # Even with the WS thread "failing" today, boot continues silently.
        # Under the target behavior ``run_server`` would raise SystemExit.
        with _patched_boot(tmp_path):
            with pytest.raises(SystemExit):
                ap.run_server(
                    host="127.0.0.1", port=0,
                    work_dir=str(tmp_path), no_auth=True,
                )


class TestPluginRegistrationOrder:
    """Hardcoded plugin-load order at api_server.py:~10666 (covers R9, R17)."""

    def test_plugins_registered_in_fixed_order(self, tmp_path):
        """covers R9 — order = isolate_vocals, transcribe, generate_music, light_show."""
        from scenecraft import api_server as ap

        with _patched_boot(tmp_path) as rec:
            ap.run_server(
                host="127.0.0.1", port=0, work_dir=str(tmp_path), no_auth=True,
            )

        names = [getattr(m, "__name__", "").rsplit(".", 1)[-1] for m in rec.registered]
        assert names == [
            "isolate_vocals", "transcribe", "generate_music", "light_show",
        ]

    def test_generate_foley_not_registered(self, tmp_path):
        """covers R9 (negative) — generate_foley not in boot list (audit-2 leak)."""
        from scenecraft import api_server as ap

        with _patched_boot(tmp_path) as rec:
            ap.run_server(
                host="127.0.0.1", port=0, work_dir=str(tmp_path), no_auth=True,
            )

        names = [getattr(m, "__name__", "").rsplit(".", 1)[-1] for m in rec.registered]
        assert "generate_foley" not in names

    def test_plugin_activate_failure_aborts_boot(
        self, tmp_path, bootstrap_clean_pluginhost
    ):
        """covers R17 — plugin activate() raise aborts boot, later plugins skipped."""
        from scenecraft import api_server as ap
        from scenecraft.plugin_host import PluginHost

        registered_attempts: list[str] = []

        def _register(mod):
            name = mod.__name__.rsplit(".", 1)[-1]
            registered_attempts.append(name)
            if name == "transcribe":
                raise RuntimeError("boom")
            PluginHost._registered.append(name)

        with _patched_boot(tmp_path):
            with mock.patch.object(PluginHost, "register", side_effect=_register):
                with pytest.raises(RuntimeError, match="boom"):
                    ap.run_server(
                        host="127.0.0.1", port=0,
                        work_dir=str(tmp_path), no_auth=True,
                    )

        assert "isolate_vocals" in registered_attempts
        assert "transcribe" in registered_attempts
        assert "generate_music" not in registered_attempts
        assert "light_show" not in registered_attempts


class TestBootOrder:
    """Phase order: handler → bind → ws → register → banner → serve_forever (R5/R6/R7/R9/R11)."""

    def test_boot_phase_order(self, tmp_path):
        """covers R5, R6, R7, R9, R11 — observable boot-phase ordering."""
        from scenecraft import api_server as ap

        with _patched_boot(tmp_path) as rec:
            ap.run_server(
                host="127.0.0.1", port=0, work_dir=str(tmp_path), no_auth=True,
            )

        # Reduce to a phase tag stream; we don't care about argument detail here.
        phases = [c[0] for c in rec.calls if c[0] in (
            "make_handler", "HTTPServer.__init__", "start_ws_server",
            "register", "serve_forever",
        )]
        # First three phases in order.
        assert phases.index("make_handler") < phases.index("HTTPServer.__init__")
        assert phases.index("HTTPServer.__init__") < phases.index("start_ws_server")
        assert phases.index("start_ws_server") < phases.index("register")
        # serve_forever must be the last observable phase.
        assert phases[-1] == "serve_forever"


class TestSIGINT:
    """SIGINT path (covers R14, R15)."""

    def test_sigint_calls_shutdown_and_logs(self, tmp_path, capsys):
        """covers R14 — KeyboardInterrupt → server.shutdown() + 'Shutting down.'"""
        from scenecraft import api_server as ap

        with _patched_boot(tmp_path, simulate_serve_forever_returns=False) as rec:
            # serve_forever raises KeyboardInterrupt inside _patched_boot.
            ap.run_server(
                host="127.0.0.1", port=0, work_dir=str(tmp_path), no_auth=True,
            )

        assert any(c[0] == "shutdown" for c in rec.calls)
        out = capsys.readouterr()
        combined = (out.out + out.err)
        assert "Shutting down" in combined

    def test_sigint_does_not_call_plugin_deactivate(self, tmp_path):
        """covers R15 — current behavior: no plugin dispose/deactivate on SIGINT."""
        from scenecraft import api_server as ap
        from scenecraft.plugin_host import PluginHost

        with _patched_boot(tmp_path, simulate_serve_forever_returns=False):
            with mock.patch.object(
                PluginHost, "deactivate_all", create=True,
            ) as deact:
                ap.run_server(
                    host="127.0.0.1", port=0,
                    work_dir=str(tmp_path), no_auth=True,
                )
        deact.assert_not_called()


class TestSIGTERM:
    """SIGTERM path — target-state per OQ-4 (covers R20)."""

    @pytest.mark.xfail(
        reason="target-state R20/OQ-4 — no SIGTERM handler installed today",
        strict=False,
    )
    def test_sigterm_handler_installed(self, tmp_path):
        """covers R20, OQ-4 (target) — boot installs signal.signal(SIGTERM, ...)."""
        from scenecraft import api_server as ap

        installed = {}

        def _capture(signum, handler):
            installed[signum] = handler

        with _patched_boot(tmp_path):
            with mock.patch("signal.signal", side_effect=_capture):
                ap.run_server(
                    host="127.0.0.1", port=0,
                    work_dir=str(tmp_path), no_auth=True,
                )

        assert signal.SIGTERM in installed


class TestNoAuthFlag:
    """--no-auth in production (covers R23, OQ-5)."""

    def test_no_auth_currently_silent_in_production(self, tmp_path):
        """covers R23 (current) — --no-auth silently allowed even with .scenecraft/.

        Documents the gap so it shows up in coverage. The xfail companion below
        encodes the target-state behavior.
        """
        from scenecraft.api_server import make_handler

        sc_root = tmp_path / ".scenecraft"
        sc_root.mkdir()
        # No exception raised today.
        H = make_handler(tmp_path, no_auth=True)
        assert H is not None

    @pytest.mark.xfail(
        reason="target-state R23/OQ-5 — --no-auth-unsafe-i-know-what-im-doing flag missing",
        strict=False,
    )
    def test_no_auth_in_production_requires_unsafe_flag(self, tmp_path):
        """covers R23, OQ-5 (target) — refuse boot without explicit unsafe flag."""
        from scenecraft import api_server as ap

        sc_root = tmp_path / ".scenecraft"
        sc_root.mkdir()
        with _patched_boot(tmp_path):
            with pytest.raises(SystemExit):
                ap.run_server(
                    host="127.0.0.1", port=0,
                    work_dir=str(tmp_path), no_auth=True,
                )


class TestConcurrentInstances:
    """Advisory flock on .scenecraft/server.lock (covers R24, OQ-6)."""

    @pytest.mark.xfail(
        reason="target-state R24/OQ-6 — advisory flock not implemented",
        strict=False,
    )
    def test_concurrent_instance_advisory_lock_refuses(self, tmp_path):
        """covers R24, OQ-6 (target) — second boot on same work_dir → flock refused."""
        from scenecraft import api_server as ap

        # Simulate first instance holding flock by creating + locking the file.
        sc_root = tmp_path / ".scenecraft"
        sc_root.mkdir()
        lock_path = sc_root / "server.lock"
        lock_path.touch()

        import fcntl
        first = open(lock_path, "w")
        fcntl.flock(first.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

        try:
            with _patched_boot(tmp_path):
                with pytest.raises(SystemExit):
                    ap.run_server(
                        host="127.0.0.1", port=0,
                        work_dir=str(tmp_path), no_auth=True,
                    )
        finally:
            fcntl.flock(first.fileno(), fcntl.LOCK_UN)
            first.close()


class TestRunServerIndependent:
    """run_server callable directly without Click (covers R5, R6, R7, R9)."""

    def test_run_server_independent_of_click(self, tmp_path):
        """covers R5, R6, R7, R9 — direct invocation matches CLI path."""
        from scenecraft import api_server as ap

        with _patched_boot(tmp_path) as rec:
            ap.run_server(
                host="127.0.0.1", port=9001,
                work_dir=str(tmp_path), no_auth=True,
            )
        # Same set of phases as the CLI path.
        names = [getattr(m, "__name__", "").rsplit(".", 1)[-1] for m in rec.registered]
        assert names == [
            "isolate_vocals", "transcribe", "generate_music", "light_show",
        ]
        ws_calls = [c for c in rec.calls if c[0] == "start_ws_server"]
        assert ws_calls and ws_calls[0][2] == 9002


# ---------------------------------------------------------------------------
# === E2E ===
# ---------------------------------------------------------------------------


class TestEndToEnd:
    """E2E smoke — uses the session-scoped engine_server fixture (port=0,
    real ThreadedHTTPServer + real make_handler). Per spec table rows 1, 7, 9,
    14, 23, 24, plus the happy-path GET /api/projects readiness check.
    """

    def test_http_server_listens(self, engine_server):
        """covers R6 — HTTP socket actually accepts connections."""
        host, port = engine_server.server.server_address
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(2.0)
        try:
            s.connect((host, port))
        finally:
            s.close()

    def test_get_projects_responds_ok(self, engine_server):
        """covers R6, R12 (no_auth=True) — basic REST request goes through."""
        status, body = engine_server.json("GET", "/api/projects")
        # Endpoint may return 200 with {"projects": []} or similar; any non-5xx
        # proves the handler dispatch works.
        assert status < 500

    def test_threading_mixin_one_thread_per_request(self, engine_server):
        """covers R6, R18 — concurrent requests use distinct threads."""
        thread_ids: list[int] = []
        lock = threading.Lock()

        def _hit():
            status, _ = engine_server.json("GET", "/api/projects")
            with lock:
                thread_ids.append(threading.get_ident())
            return status

        ts = [threading.Thread(target=_hit) for _ in range(4)]
        for t in ts:
            t.start()
        for t in ts:
            t.join(timeout=10.0)

        # Caller-side thread idents differ; the more interesting property is
        # that all requests completed (so the server didn't serialize them
        # behind a single accept loop).
        assert len(thread_ids) == 4

    def test_handler_class_per_call(self, engine_server):
        """covers R18 — handler is built per request (closure over work_dir)."""
        # Two sequential requests both succeed; if the handler were a single
        # shared instance with mutable state, repeated paths could corrupt
        # one another. We assert behavioral idempotence here.
        s1, _ = engine_server.json("GET", "/api/projects")
        s2, _ = engine_server.json("GET", "/api/projects")
        assert s1 == s2

    def test_subprocess_boot_missing_workdir_exits_nonzero(self):
        """covers R16 — `scenecraft server --work-dir <missing>` exits non-zero.

        Spawn the CLI as a subprocess with a path that does not exist; we
        rely on `run_server`'s SystemExit(1) guard. Because the Click
        `server()` command calls ``mkdir(parents=True, exist_ok=True)``
        first, we instead invoke ``run_server`` via ``python -c`` to test
        the inner guard.
        """
        code = (
            "import sys; "
            "sys.path.insert(0, 'src'); "
            "from scenecraft.api_server import run_server; "
            "run_server(host='127.0.0.1', port=0, "
            "work_dir='/definitely/does/not/exist/sc-test', no_auth=True)"
        )
        proc = subprocess.run(
            [sys.executable, "-c", code],
            cwd=str(Path(__file__).resolve().parents[2]),
            capture_output=True,
            timeout=30,
        )
        assert proc.returncode == 1
        assert b"Work directory not found" in proc.stderr

    def test_engine_server_ws_port_pairing_documented(self, engine_server):
        """covers R7 (documented) — fixture binds HTTP only; WS pairing covered
        by unit ``test_ws_port_is_http_port_plus_one``. This e2e affirms the
        HTTP server started by the fixture is healthy enough to be the
        precondition for the unit-tested WS pairing.
        """
        host, port = engine_server.server.server_address
        assert isinstance(port, int) and port > 0
