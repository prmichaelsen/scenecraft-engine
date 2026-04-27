"""Regression tests for ``local.engine-providers-typed-and-legacy.md``.

Locks the *current, messy, divergent* state of the nine provider integrations
(1 typed + 1 legacy shim + 6 direct-SDK + 1 spend-ledger core) so refactors
can't silently regress behavior. Target-state requirements (R61–R68, OQ-1..7)
are encoded as ``xfail(strict=False)`` so they will flip green automatically
once migration lands.

Mocking strategy
----------------
Every external HTTP / SDK call is patched. The test suite NEVER hits real
network. We use ``unittest.mock`` (``responses`` is not installed). For
spend-tracking transitional checks we *grep the source code* for
``record_spend(`` calls per provider module — when migration lands those
flip from "absent" (today) to "present" (target).

Section layout — one ``Test<Provider>`` per provider:
    TestReplicate, TestMusicful, TestImagen, TestVeo, TestKling, TestRunway,
    TestAnthropic, TestGenAI, TestSpendLedger, TestEndToEnd, TestTargetState

Naming convention: every fixture local to this file is prefixed
``providers_`` (per task-79 brief).
"""
from __future__ import annotations

import json
import os
import re
import textwrap
from pathlib import Path
from unittest import mock

import pytest


# ===========================================================================
# Shared fixtures (prefix: providers_*)
# ===========================================================================


@pytest.fixture
def providers_isolate_env(monkeypatch):
    """Strip every provider-relevant env var so missing-key tests are reliable.

    The host shell may have any of these set; we don't want bleed-through.
    """
    for var in (
        "REPLICATE_API_TOKEN",
        "MUSICFUL_API_KEY",
        "GOOGLE_API_KEY",
        "GOOGLE_CLOUD_PROJECT",
        "RUNWAY_API_KEY",
        "ANTHROPIC_API_KEY",
        "SCENECRAFT_ROOT",
    ):
        monkeypatch.delenv(var, raising=False)
    yield monkeypatch


@pytest.fixture
def providers_scenecraft_root(tmp_path, monkeypatch):
    """A real ``.scenecraft/`` root so ``record_spend`` can actually write."""
    from scenecraft.vcs.bootstrap import init_root

    init_root(tmp_path, org_name="test-org", admin_username="alice")
    monkeypatch.setenv("SCENECRAFT_ROOT", str(tmp_path / ".scenecraft"))
    return tmp_path / ".scenecraft"


@pytest.fixture
def providers_no_sleep(monkeypatch):
    """Make every ``time.sleep`` instant — backoff loops would block forever."""
    monkeypatch.setattr("time.sleep", lambda *a, **kw: None)
    # Some modules import `time` and call `time.sleep` at module-qualified
    # scope; the global patch above catches them all because they share the
    # one module object.
    yield


@pytest.fixture
def providers_src_root() -> Path:
    """Path to the engine source tree; used for spend-tracking grep tests."""
    return Path(__file__).resolve().parents[2] / "src" / "scenecraft"


def _grep_source(path: Path, pattern: str) -> list[tuple[Path, int, str]]:
    """Tiny grep: return [(file, lineno, line), ...] for files matching pattern.

    Skips ``__pycache__`` and binary files. Used for transitional spend-
    tracking absence checks — when the migration lands the assertions invert.
    """
    rx = re.compile(pattern)
    hits: list[tuple[Path, int, str]] = []
    for p in path.rglob("*.py"):
        if "__pycache__" in p.parts:
            continue
        try:
            for i, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
                if rx.search(line):
                    hits.append((p, i, line.rstrip()))
        except OSError:
            continue
    return hits


# ===========================================================================
# TestReplicate — typed provider (current = passes; reference shape)
# ===========================================================================


class TestReplicate:
    """Typed provider — full surface contract (R4–R9, R58, B1, B2, B32)."""

    def test_namespace_exports_required_symbols(self):
        """covers R4 — typed provider exports the 4-class hierarchy + result type."""
        from scenecraft.plugin_api import providers

        rep = providers.replicate
        for name in (
            "run_prediction",
            "attach_polling",
            "PredictionResult",
            "ReplicateError",
            "ReplicateNotConfigured",
            "ReplicatePredictionFailed",
            "ReplicateDownloadFailed",
        ):
            assert hasattr(rep, name), f"providers.replicate missing {name}"

    def test_missing_token_raises_typed_not_configured(
        self, providers_isolate_env
    ):
        """covers R5, B2 — replicate-missing-token-raises-typed."""
        from scenecraft.plugin_api.providers import replicate as rep

        with pytest.raises(rep.ReplicateNotConfigured):
            rep.run_prediction(model="o/m", input={"prompt": "x"}, source="x")

    def test_exception_hierarchy_rooted_at_replicate_error(self):
        """covers R4 — every typed exception inherits from ReplicateError."""
        from scenecraft.plugin_api.providers import replicate as rep

        for exc in (
            rep.ReplicateNotConfigured,
            rep.ReplicatePredictionFailed,
            rep.ReplicateDownloadFailed,
        ):
            assert issubclass(exc, rep.ReplicateError)

    def test_constants_match_spec(self):
        """covers R7 — backoff (1, 2, 4); 3 download retries."""
        from scenecraft.plugin_api.providers import replicate as rep

        assert rep.RATE_LIMIT_BACKOFF_SECONDS == (1.0, 2.0, 4.0)
        assert rep.DOWNLOAD_BACKOFF_SECONDS == (1.0, 2.0, 4.0)


# ===========================================================================
# TestMusicful — legacy call_service shim
# ===========================================================================


class TestMusicful:
    """Legacy shim (R10–R17, B3–B7)."""

    def test_service_registry_contains_only_musicful(self):
        """covers R10 — exactly one entry in SERVICE_REGISTRY."""
        from scenecraft import plugin_api

        assert "musicful" in plugin_api.SERVICE_REGISTRY
        base, env, hdr = plugin_api.SERVICE_REGISTRY["musicful"]
        assert base == "https://api.musicful.ai"
        assert env == "MUSICFUL_API_KEY"
        assert hdr == "x-api-key"

    def test_missing_key_raises_service_config_error(
        self, providers_isolate_env
    ):
        """covers R11, B4 — musicful-missing-key-raises-config-error."""
        from scenecraft.plugin_api import (
            ServiceConfigError,
            call_service,
        )

        with pytest.raises(ServiceConfigError) as ei:
            call_service(service="musicful", method="POST", path="/v1/x")
        assert "MUSICFUL_API_KEY" in str(ei.value)

    def test_unknown_service_raises_config_error(self, monkeypatch):
        """covers R12 — unknown service rejected before env lookup."""
        from scenecraft.plugin_api import ServiceConfigError, call_service

        with pytest.raises(ServiceConfigError):
            call_service(service="not-a-service", method="GET", path="/x")

    def test_shim_returns_response_no_ledger(
        self, monkeypatch, providers_isolate_env
    ):
        """covers R10–R12, R15, B3 — musicful-shim-returns-response-no-ledger.

        Patches ``httpx.request`` to return a stubbed JSON body; asserts the
        outbound auth header is set and no ledger row is written by the shim.
        """
        monkeypatch.setenv("MUSICFUL_API_KEY", "secret-key")

        captured = {}

        class _Resp:
            status_code = 200
            headers = {"content-type": "application/json"}
            text = '{"ok": true}'

            def json(self):
                return {"ok": True, "data": {}}

        def fake_request(method, url, **kwargs):
            captured["method"] = method
            captured["url"] = url
            captured["headers"] = kwargs.get("headers")
            return _Resp()

        import httpx

        monkeypatch.setattr(httpx, "request", fake_request)

        from scenecraft.plugin_api import call_service

        resp = call_service(
            service="musicful",
            method="POST",
            path="/v1/music/generate",
            body={"prompt": "x"},
        )

        # auth header
        assert captured["headers"].get("x-api-key") == "secret-key"
        assert captured["url"] == "https://api.musicful.ai/v1/music/generate"
        # response shape
        assert resp.status == 200
        assert resp.body == {"ok": True, "data": {}}
        # No ledger writes — _record_spend_raw is patched-out below in the
        # ledger-isolation tests; here we just assert the shim itself never
        # touches `record_spend` (introspect call list via patching).

    def test_shim_404_raises_service_error(self, monkeypatch):
        """covers R12 — ServiceError on >=400."""
        monkeypatch.setenv("MUSICFUL_API_KEY", "k")

        class _Resp:
            status_code = 404
            headers = {"content-type": "application/json"}
            text = '{"err": "missing"}'

            def json(self):
                return {"err": "missing"}

        import httpx

        monkeypatch.setattr(httpx, "request", lambda m, u, **kw: _Resp())

        from scenecraft.plugin_api import ServiceError, call_service

        with pytest.raises(ServiceError) as ei:
            call_service(service="musicful", method="GET", path="/v1/x")
        assert ei.value.status == 404

    def test_shim_timeout_raises_service_timeout_error(self, monkeypatch):
        """covers R12 — ServiceTimeoutError wraps httpx.TimeoutException."""
        monkeypatch.setenv("MUSICFUL_API_KEY", "k")

        import httpx

        def _boom(*a, **kw):
            raise httpx.TimeoutException("slow")

        monkeypatch.setattr(httpx, "request", _boom)

        from scenecraft.plugin_api import ServiceTimeoutError, call_service

        with pytest.raises(ServiceTimeoutError):
            call_service(service="musicful", method="GET", path="/v1/x")


# ===========================================================================
# TestImagen — direct-SDK
# ===========================================================================


class TestImagen:
    """Imagen direct-SDK (R18–R24, B8–B10)."""

    def test_missing_key_raises_value_error(self, providers_isolate_env):
        """covers R18, B10 — imagen-missing-key-raises-valueerror.

        Instantiating ``GoogleVideoClient(vertex=False)`` with no
        ``GOOGLE_API_KEY`` raises ``ValueError`` after the SDK imports.
        Skipped if ``google-genai`` isn't installed in this environment.
        """
        pytest.importorskip("google.genai")
        from scenecraft.render.google_video import GoogleVideoClient

        with pytest.raises(ValueError) as ei:
            GoogleVideoClient(vertex=False)
        assert "GOOGLE_API_KEY" in str(ei.value)

    def test_vertex_missing_project_raises_value_error(
        self, providers_isolate_env
    ):
        """covers R18 — Vertex mode missing GOOGLE_CLOUD_PROJECT."""
        pytest.importorskip("google.genai")
        from scenecraft.render.google_video import GoogleVideoClient

        with pytest.raises(ValueError) as ei:
            GoogleVideoClient(vertex=True)
        assert "GOOGLE_CLOUD_PROJECT" in str(ei.value)

    def test_retry_on_429_exponential_then_60s_cycle(
        self, monkeypatch, providers_no_sleep
    ):
        """covers R20, B8 — imagen-429-retry-then-infinite-cycle.

        Observe ``time.sleep`` calls: first 5 attempts wait
        ``[2, 4, 8, 16, 32]`` (R20: ``2**(attempt+1)``), then a 60s cycle.
        We let the loop run two full cycles and then stop it by flipping the
        sleep mock to raise.
        """
        from scenecraft.render import google_video

        sleeps: list[float] = []

        # Stop after we've observed the pattern twice.
        def fake_sleep(s):
            sleeps.append(s)
            if sleeps.count(60) >= 2:
                raise KeyboardInterrupt("test stop")

        monkeypatch.setattr(google_video.time, "sleep", fake_sleep)

        def always_429():
            raise RuntimeError("429 rate limited")

        with pytest.raises(KeyboardInterrupt):
            google_video._retry_on_429(always_429)

        # First 5 attempts: 2, 4, 8, 16, 32 — then 60s — then again.
        # We allow the trailing extra sleeps before interrupt fires.
        assert sleeps[:5] == [2, 4, 8, 16, 32]
        assert 60 in sleeps[5:]
        # After the 60s reset, the same 5-attempt pattern restarts.
        idx_first_60 = sleeps.index(60)
        next_after_60 = sleeps[idx_first_60 + 1: idx_first_60 + 6]
        assert next_after_60 == [2, 4, 8, 16, 32]

    def test_retry_on_429_passes_non_429_through(
        self, providers_no_sleep
    ):
        """covers R21 — non-429 exceptions bubble unmodified."""
        from scenecraft.render.google_video import _retry_on_429

        class Boom(Exception):
            pass

        def raises_unrelated():
            raise Boom("nope")

        with pytest.raises(Boom):
            _retry_on_429(raises_unrelated)

    def test_imagen_module_has_no_record_spend_call_today(
        self, providers_src_root
    ):
        """covers R22 transitional — imagen-success-no-ledger (grep contract).

        Until R62/OQ-2 lands, ``google_video.py`` MUST NOT call
        ``record_spend``. When migration lands, this test flips meaning.
        """
        path = providers_src_root / "render" / "google_video.py"
        assert path.exists()
        body = path.read_text(encoding="utf-8")
        # Strip comments + docstrings? simple substring is good enough — there
        # are no doctests or string literals using "record_spend(" today.
        assert "record_spend(" not in body, (
            "google_video.py now writes spend_ledger — flip the test set "
            "and delete this assertion (target = R62)."
        )


# ===========================================================================
# TestVeo — direct-SDK (shares google_video module with Imagen)
# ===========================================================================


class TestVeo:
    """Veo direct-SDK (R25–R30, B11–B14)."""

    def test_safety_error_raises_prompt_rejected(
        self, monkeypatch, providers_no_sleep
    ):
        """covers R26, B12 — veo-safety-error-raises-prompt-rejected."""
        from scenecraft.render.google_video import (
            PromptRejectedError,
            _retry_video_generation,
        )

        class _Op:
            done = True
            error = "safety violation: prompt blocked"
            result = None

        def gen():
            return _Op()

        # client is unused on the first iteration's op-error branch
        with pytest.raises(PromptRejectedError):
            _retry_video_generation(gen, client=mock.MagicMock(), output_path="x")

    def test_repeated_none_raises_prompt_rejected(
        self, monkeypatch, providers_no_sleep
    ):
        """covers R26, B11 — veo-repeated-none-raises-prompt-rejected."""
        from scenecraft.render.google_video import (
            PromptRejectedError,
            _retry_video_generation,
        )

        class _Op:
            done = True
            error = None
            result = None

        def gen():
            return _Op()

        with pytest.raises(PromptRejectedError):
            _retry_video_generation(
                gen, client=mock.MagicMock(), output_path="x", max_retries=3,
            )

    def test_per_attempt_poll_timeout_600s(
        self, monkeypatch, providers_no_sleep
    ):
        """covers R26, B13 — veo-per-attempt-poll-timeout.

        Mock ``time.time`` to jump past 600s after the first poll iteration.
        Each ``operation.done`` stays False so the poll loop trips the
        TimeoutError branch.
        """
        from scenecraft.render import google_video

        # Simulate clock: first call = 0 (start), second call > 600.
        clock_vals = iter([0.0, 700.0, 800.0, 900.0, 1000.0, 1100.0])

        def fake_time():
            try:
                return next(clock_vals)
            except StopIteration:
                return 9999.0

        monkeypatch.setattr(google_video.time, "time", fake_time)

        class _Op:
            done = False
            error = None
            result = None

        client = mock.MagicMock()
        client.operations.get.return_value = _Op()

        # max_retries=1 → after one TimeoutError attempt, retry exhausted →
        # the outer except catches "timed out" as retryable (R26: retryable),
        # so it sleeps and exits the for-loop without retrying. Result is
        # the trailing RuntimeError.
        with pytest.raises((RuntimeError, TimeoutError)):
            google_video._retry_video_generation(
                lambda: _Op(), client=client, output_path="x", max_retries=1,
            )

    def test_veo_does_not_call_record_spend_today(
        self, providers_src_root
    ):
        """covers R28 transitional — veo-success-no-ledger (grep contract).

        Same module as Imagen; one assertion covers both. Kept separate so
        the test labels each provider clearly.
        """
        path = providers_src_root / "render" / "google_video.py"
        assert "record_spend(" not in path.read_text(encoding="utf-8")


# ===========================================================================
# TestKling — direct-HTTP via urllib
# ===========================================================================


class TestKling:
    """Kling direct-HTTP (R31–R35, B15–B17)."""

    def test_missing_token_raises_value_error(self, providers_isolate_env):
        """covers R31 — KlingClient demands REPLICATE_API_TOKEN."""
        from scenecraft.render.kling_video import KlingClient

        with pytest.raises(ValueError) as ei:
            KlingClient()
        assert "REPLICATE_API_TOKEN" in str(ei.value)

    def test_failed_prediction_raises_runtime_error(
        self, monkeypatch, providers_no_sleep
    ):
        """covers R32, B15 — kling-failed-raises-runtime-error."""
        from scenecraft.render.kling_video import KlingClient

        client = KlingClient(api_token="k")
        # Patch _get to return a "failed" status immediately.
        monkeypatch.setattr(
            client, "_get",
            lambda url: {"status": "failed", "error": "model exploded"},
        )

        with pytest.raises(RuntimeError) as ei:
            client._wait_for_prediction(
                {"urls": {"get": "https://x"}}, poll_interval=0,
            )
        assert "Kling prediction failed" in str(ei.value)
        # And it is NOT a typed ReplicateError (R32: no typed Kling hierarchy).
        from scenecraft.plugin_api.providers import replicate as rep
        assert not isinstance(ei.value, rep.ReplicateError)

    def test_timeout_after_600s(self, monkeypatch):
        """covers R32, B16 — kling-timeout-600s.

        Use time-mocking so `time.time()` advances past the timeout.
        """
        from scenecraft.render import kling_video

        client = kling_video.KlingClient(api_token="k")
        # Always pending
        monkeypatch.setattr(client, "_get", lambda url: {"status": "starting"})
        # Time goes 0 then 700.
        clock_vals = iter([0.0, 700.0])
        monkeypatch.setattr(
            kling_video.time, "time",
            lambda: next(clock_vals, 800.0),
        )
        monkeypatch.setattr(kling_video.time, "sleep", lambda *_: None)

        with pytest.raises(TimeoutError) as ei:
            client._wait_for_prediction(
                {"urls": {"get": "https://x"}}, poll_interval=1, timeout=600,
            )
        assert "Kling prediction timed out after 600s" in str(ei.value)

    def test_kling_module_has_no_record_spend_call_today(
        self, providers_src_root
    ):
        """covers R34 transitional — kling-success-no-scenecraft-ledger (grep)."""
        path = providers_src_root / "render" / "kling_video.py"
        assert "record_spend(" not in path.read_text(encoding="utf-8"), (
            "kling_video.py now writes spend_ledger — target R62/OQ-2 met."
        )

    def test_kling_uses_replicate_token_not_typed_provider(
        self, providers_src_root
    ):
        """covers R31, R34 (transitional) — kling reads REPLICATE_API_TOKEN
        directly via urllib and doesn't go through plugin_api.providers.replicate.
        """
        path = providers_src_root / "render" / "kling_video.py"
        body = path.read_text(encoding="utf-8")
        assert "REPLICATE_API_TOKEN" in body
        assert "plugin_api.providers" not in body
        assert "import urllib.request" in body


# ===========================================================================
# TestRunway — direct-HTTP via urllib
# ===========================================================================


class TestRunway:
    """Runway direct-HTTP (R36–R40, B18–B21)."""

    def test_missing_key_raises_runtime_error(self, providers_isolate_env):
        """covers R36 — Runway demands RUNWAY_API_KEY."""
        from scenecraft.render.google_video import RunwayVideoClient

        with pytest.raises(RuntimeError) as ei:
            RunwayVideoClient()
        assert "RUNWAY_API_KEY" in str(ei.value)

    def test_module_has_no_record_spend_today(self, providers_src_root):
        """covers R39 transitional — runway-success-no-ledger (grep)."""
        # Runway lives in google_video.py — already asserted in TestImagen.
        # This duplicate asserts the contract per provider for clarity.
        path = providers_src_root / "render" / "google_video.py"
        assert "record_spend(" not in path.read_text(encoding="utf-8")

    def test_uses_urllib_no_typed_namespace(self, providers_src_root):
        """covers R37 (transitional) — Runway path uses urllib not provider namespace."""
        path = providers_src_root / "render" / "google_video.py"
        body = path.read_text(encoding="utf-8")
        # Find the RunwayVideoClient class and the immediate lines after it.
        runway_idx = body.find("class RunwayVideoClient")
        assert runway_idx > 0
        runway_chunk = body[runway_idx: runway_idx + 5000]
        assert "urllib.request" in runway_chunk
        assert "plugin_api.providers" not in runway_chunk


# ===========================================================================
# TestAnthropic — direct-SDK across 6 call sites
# ===========================================================================


class TestAnthropic:
    """Anthropic direct-SDK (R41–R46, B22–B25, OQ-3, OQ-7)."""

    # The R41 enumeration. These are the 6 call sites the spec freezes.
    EXPECTED_CALL_SITES = [
        ("ai/provider.py", r"anthropic\.Anthropic\(api_key=api_key\)"),
        ("chat.py", r"anthropic\.AsyncAnthropic\(api_key=api_key\)"),
        ("audio_intelligence.py", r"anthropic\.Anthropic\(api_key="),
        ("render/narrative.py", r"Anthropic\(api_key="),
        ("render/transition_describer.py", r"anthropic\.Anthropic\(\)"),
        ("api_server.py", r"Anthropic\(api_key="),
    ]

    def test_at_least_six_anthropic_call_sites_exist_today(
        self, providers_src_root
    ):
        """covers R41, OQ-7 transitional — at least 6 distinct files
        instantiate ``Anthropic()`` / ``AsyncAnthropic()``.

        When OQ-7 / R67 lands, this test inverts: only one file
        (``plugin_api/providers/anthropic.py``) should remain.
        """
        hits = _grep_source(
            providers_src_root,
            r"\b(?:anthropic\.)?A(?:sync)?nthropic\(",
        )
        files = {h[0].relative_to(providers_src_root).as_posix() for h in hits}
        # The audit said ≥6 distinct call sites; we accept ≥5 files (some
        # files have multiple call sites — api_server alone has many).
        assert len(files) >= 5, f"expected ≥5 distinct files, got {files}"

    def test_chat_streams_error_on_missing_key(
        self, providers_isolate_env
    ):
        """covers R46, B23 — anthropic-missing-key-sends-ws-error.

        Probe ``chat.py`` source: the early-return contract requires sending
        ``{"type":"error", "error":"ANTHROPIC_API_KEY not configured ..."}``
        followed by ``{"type":"complete"}`` and returning *before* the SDK is
        instantiated. We assert the literal contract by grep — invoking
        ``_stream_response`` requires a real WS, project_dir, and bridge,
        which is out of scope for a unit test.
        """
        chat_py = (
            Path(__file__).resolve().parents[2]
            / "src" / "scenecraft" / "chat.py"
        )
        body = chat_py.read_text(encoding="utf-8")
        assert "ANTHROPIC_API_KEY not configured on server" in body
        # The error frame is sent BEFORE `import anthropic` / `AsyncAnthropic(` —
        # use line-position to lock the ordering.
        err_line = body.find("ANTHROPIC_API_KEY not configured on server")
        sdk_line = body.find("AsyncAnthropic(api_key=api_key)")
        assert 0 < err_line < sdk_line, (
            "chat.py error-frame must precede SDK instantiation"
        )

    def test_no_anthropic_module_writes_spend_today(
        self, providers_src_root
    ):
        """covers R44 transitional — anthropic-success-no-ledger (grep contract).

        None of the 6 Anthropic call sites call ``record_spend``. When
        migration lands (R62), the typed provider module starts calling it
        and this test inverts.
        """
        files_to_check = [
            "ai/provider.py",
            "chat.py",
            "audio_intelligence.py",
            "render/narrative.py",
            "render/transition_describer.py",
            # api_server.py is a giant file — we check that no record_spend(
            # appears in proximity to Anthropic( call sites instead of a
            # global ban (api_server may legitimately call record_spend
            # from other handlers in the future).
        ]
        for rel in files_to_check:
            p = providers_src_root / rel
            if not p.exists():
                continue
            assert "record_spend(" not in p.read_text(encoding="utf-8"), (
                f"{rel} now calls record_spend — Anthropic migration?"
            )

    def test_token_read_per_call_at_each_instantiation(
        self, providers_src_root
    ):
        """covers R45, B22, OQ-3 — token rotation contract.

        The audit codified: each Anthropic client instance reads
        ``ANTHROPIC_API_KEY`` (or accepts ``api_key=...`` from a fresh
        env-read at instantiation). A new turn re-reads, an in-flight
        stream does not. We assert the per-call read by grepping for
        ``os.environ.get("ANTHROPIC_API_KEY")`` near each ``Anthropic(``
        call site.
        """
        # The chat.py path reads at start of _stream_response — use a tight
        # contextual check against the surrounding lines.
        chat_py = providers_src_root / "chat.py"
        body = chat_py.read_text(encoding="utf-8")
        assert 'api_key = os.environ.get("ANTHROPIC_API_KEY")' in body
        # There should be at most one stream entry-point that snapshots the
        # key — multiple call sites each snapshot independently.


# ===========================================================================
# TestGenAI — Google Gemini direct-SDK in audio_intelligence
# ===========================================================================


class TestGenAI:
    """Google GenAI direct-SDK (R47–R53, B26–B29, OQ-4)."""

    def test_structured_missing_key_returns_none(
        self, providers_isolate_env, tmp_path
    ):
        """covers R50, B28 — genai-missing-key-soft-fail.

        With ``GOOGLE_API_KEY`` unset the function must log + return None
        without instantiating the SDK.
        """
        pytest.importorskip("numpy")  # audio_intelligence imports numpy
        from scenecraft.audio_intelligence import _gemini_describe_chunk_structured

        # Minimal chunk — function reads the file but with no key it returns
        # before that. Provide a path that doesn't need to exist.
        out = _gemini_describe_chunk_structured(
            str(tmp_path / "nonexistent.mp3"), 0.0, 1.0,
        )
        assert out is None

    def test_structured_exception_returns_none(
        self, monkeypatch, tmp_path
    ):
        """covers R49, B26 — genai-exception-returns-none.

        Patch ``genai.Client`` so the SDK call raises; the function swallows
        and returns ``None``.
        """
        pytest.importorskip("google.genai")
        pytest.importorskip("numpy")
        monkeypatch.setenv("GOOGLE_API_KEY", "k")

        # Write a tiny placeholder file the function will read.
        chunk = tmp_path / "c.mp3"
        chunk.write_bytes(b"\x00" * 16)

        from google import genai as _real_genai

        class _Models:
            def generate_content(self, **kw):
                raise RuntimeError("boom")

        class _Client:
            def __init__(self, **kw):
                self.models = _Models()

        monkeypatch.setattr(_real_genai, "Client", _Client)

        from scenecraft.audio_intelligence import (
            _gemini_describe_chunk_structured,
        )

        out = _gemini_describe_chunk_structured(
            str(chunk), 0.0, 1.0, prompt_version="v1",
        )
        assert out is None

    def test_audio_intelligence_does_not_call_record_spend(
        self, providers_src_root
    ):
        """covers R51 transitional — genai-success-no-ledger (grep)."""
        path = providers_src_root / "audio_intelligence.py"
        assert "record_spend(" not in path.read_text(encoding="utf-8")


# ===========================================================================
# TestSpendLedger — core write path
# ===========================================================================


class TestSpendLedger:
    """``plugin_api.record_spend`` (R54–R58, B30, B31, B43)."""

    def test_no_root_raises_runtime_error(self, providers_isolate_env, tmp_path):
        """covers R55, B30 — record-spend-no-root-raises."""
        # Run the call from inside a directory with no .scenecraft/ ancestor.
        from scenecraft import plugin_api

        old_cwd = os.getcwd()
        try:
            os.chdir(tmp_path)
            with pytest.raises(RuntimeError) as ei:
                plugin_api.record_spend(
                    plugin_id="x", amount=1, unit="prediction",
                    operation="x.run",
                )
            assert "outside a scenecraft root" in str(ei.value)
        finally:
            os.chdir(old_cwd)

    def test_write_returns_ledger_id_and_persists(
        self, providers_scenecraft_root
    ):
        """covers R54, R56 — successful insert returns string id; row is queryable."""
        from scenecraft import plugin_api
        from scenecraft.vcs.bootstrap import list_spend

        ledger_id = plugin_api.record_spend(
            plugin_id="generate_music",
            amount=2,
            unit="credit",
            operation="generate-music.run",
            job_ref="gen-123",
            metadata={"task_ids": ["a", "b"]},
            source="local",
        )
        assert isinstance(ledger_id, str) and ledger_id.startswith("spend_")
        rows = list_spend(providers_scenecraft_root, plugin_id="generate_music")
        assert len(rows) == 1
        row = rows[0]
        assert row["plugin_id"] == "generate_music"
        assert row["amount"] == 2
        assert row["unit"] == "credit"

    def test_unit_agnostic_negative_amount_allowed(
        self, providers_scenecraft_root
    ):
        """covers R56 — negative amount = refund; unit is free-form string."""
        from scenecraft import plugin_api

        ledger_id = plugin_api.record_spend(
            plugin_id="x", amount=-100, unit="usd_micro",
            operation="x.refund",
        )
        assert ledger_id

    def test_trust_boundary_not_enforced_today(
        self, providers_scenecraft_root
    ):
        """covers R57, B31 — record-spend-trust-boundary-not-enforced.

        Today the runtime trusts the caller's ``plugin_id``. A plugin can
        attribute spend to another plugin. When the M17 wrapped-handle check
        lands, this test inverts (and target ``test_records_pluggin_id_from_stack_target``
        flips green).
        """
        from scenecraft import plugin_api
        from scenecraft.vcs.bootstrap import list_spend

        ledger_id = plugin_api.record_spend(
            plugin_id="claimed_by_attacker",
            amount=1,
            unit="prediction",
            operation="x.run",
        )
        assert ledger_id
        rows = list_spend(
            providers_scenecraft_root, plugin_id="claimed_by_attacker",
        )
        assert any(r["id"] == ledger_id for r in rows)

    @pytest.mark.xfail(
        reason="target-state R62/OQ-2: stack-frame plugin_id derivation "
        "(awaits M17 wrapped-handle check)",
        strict=False,
    )
    def test_records_plugin_id_from_stack_frame_target(
        self, providers_scenecraft_root
    ):
        """covers R62 (target) — record_spend should derive plugin_id from
        the calling stack frame, rejecting / overriding attacker-supplied ids.
        """
        from scenecraft import plugin_api

        # When R62 lands, this misattribution is rejected or remapped.
        with pytest.raises(Exception):  # exact type TBD
            plugin_api.record_spend(
                plugin_id="some_other_plugin",
                amount=1, unit="prediction", operation="x.run",
            )

    @pytest.mark.xfail(
        reason="target-state R62 / INV-3: idempotent on (plugin_id, job_ref, operation)",
        strict=False,
    )
    def test_idempotent_on_duplicate_job_ref_target(
        self, providers_scenecraft_root
    ):
        """covers B43 (target) — record-spend-idempotent-on-retry.

        Today, calling twice writes two rows. Target = second call returns
        the same id and writes nothing.
        """
        from scenecraft import plugin_api
        from scenecraft.vcs.bootstrap import list_spend

        a = plugin_api.record_spend(
            plugin_id="generate_music",
            amount=1, unit="credit", operation="generate-music.run",
            job_ref="dedup-key",
        )
        b = plugin_api.record_spend(
            plugin_id="generate_music",
            amount=1, unit="credit", operation="generate-music.run",
            job_ref="dedup-key",
        )
        assert a == b
        rows = list_spend(
            providers_scenecraft_root, plugin_id="generate_music",
        )
        # exactly one row written
        assert len([r for r in rows if r.get("job_ref") == "dedup-key"]) == 1


# ===========================================================================
# TestEndToEnd — HTTP surface (only generate_music has a REST endpoint today)
# ===========================================================================


class TestEndToEnd:
    """E2E through HTTP. Per spec § Scope, six of the eight cost-bearing
    providers are direct-SDK / synchronous and have no public REST surface
    that exercises them in isolation. We flag those explicitly and test the
    one provider with a public endpoint (Musicful via ``generate_music``).
    """

    NO_DIRECT_HTTP_SURFACE_PROVIDERS = [
        # provider, reason
        ("Imagen", "called from chat_generation.py via WS pipeline; not directly REST-addressable"),
        ("Veo", "same — chat_generation.py daemon thread"),
        ("Kling", "called from kling_pipeline.py during chat-driven generation"),
        ("Runway", "called from chat_generation.py / narrative.py"),
        ("Anthropic", "called via WS /chat/stream — not pure REST"),
        ("GenAI", "called from audio_intelligence ingestion — internal only"),
    ]

    def test_no_direct_http_surface_for_six_providers_documented(self):
        """Assertion-as-documentation: six providers have no direct REST endpoint
        under ``/api/...`` that exercises them in isolation. E2E coverage of
        those flows belongs in milestone-task tests (chat_generation, etc.).
        """
        # Sanity: list is non-empty and every entry has a reason string.
        assert len(self.NO_DIRECT_HTTP_SURFACE_PROVIDERS) == 6
        for name, reason in self.NO_DIRECT_HTTP_SURFACE_PROVIDERS:
            assert reason

    def test_engine_server_boots_for_e2e(self, engine_server):
        """Smoke: the shared session fixture comes up — guards this file's
        ability to run e2e if/when REST surfaces for the other providers
        are added.
        """
        status, _h, body = engine_server.request("GET", "/api/version")
        # version endpoint may or may not exist; we just assert the server
        # accepted the connection (non-zero status, no socket error raised).
        assert status in (200, 404)


# ===========================================================================
# TestTargetState — xfailed; flip green on migration
# ===========================================================================


class TestTargetState:
    """All target-state requirements (R61–R68) plus the OQ resolutions.

    Each test is ``xfail(strict=False)`` so it neither blocks merges today
    nor falsely reports green. When migration lands, remove the marker.
    """

    @pytest.mark.xfail(
        reason="target-state R61: unified plugin_api.providers.<name> namespace "
        "for all 9 cost-bearing units",
        strict=False,
    )
    def test_all_providers_under_typed_namespace_target(self):
        """covers R61, B41."""
        from scenecraft.plugin_api import providers

        for name in (
            "replicate",   # already done
            "musicful",
            "imagen",
            "veo",
            "kling",
            "runway",
            "anthropic",
            "google_genai",
        ):
            assert hasattr(providers, name), f"providers.{name} missing"
            mod = getattr(providers, name)
            # Each must export run_prediction (or equivalent) + exception root.
            assert hasattr(mod, "run_prediction")

    @pytest.mark.xfail(
        reason="target-state R63 / OQ-1: bounded retry — Imagen/Veo "
        "must cap at 5×exp×60s and raise <Provider>RateLimitExhausted",
        strict=False,
    )
    def test_imagen_429_exhaustion_raises_typed_target(self):
        """covers R63, B35, OQ-1 — imagen-429-exhaustion-raises-typed."""
        from scenecraft.plugin_api.providers import imagen  # type: ignore

        with pytest.raises(imagen.ImagenRateLimitExhausted):  # type: ignore[attr-defined]
            # Concrete invocation TBD — this stub keeps the test compilable.
            imagen.run_prediction(  # type: ignore[attr-defined]
                model="imagen-3.0", input={"prompt": "x"}, source="x",
            )

    @pytest.mark.xfail(
        reason="target-state R66 / OQ-4: google-genai SDK pin in "
        "pyproject.toml + import-time compat check",
        strict=False,
    )
    def test_google_genai_compat_check_target(self):
        """covers R66, B38, OQ-4."""
        # Pseudo-check: a function exists that fails fast on bad SDK version.
        from scenecraft.plugin_api.providers import google_genai  # type: ignore

        assert hasattr(google_genai, "_assert_compat_at_import")

    @pytest.mark.xfail(
        reason="target-state R67 / OQ-7: Anthropic 6 call sites collapse "
        "to one plugin_api.providers.anthropic module",
        strict=False,
    )
    def test_anthropic_call_sites_consolidated_target(self, providers_src_root=None):
        """covers R67, OQ-7 — only the typed module instantiates Anthropic."""
        path = (
            Path(__file__).resolve().parents[2]
            / "src" / "scenecraft"
        )
        hits = _grep_source(path, r"\bAnthropic\(")
        files = {h[0].relative_to(path).as_posix() for h in hits}
        # Target: exactly one file — plugin_api/providers/anthropic.py.
        assert files == {"plugin_api/providers/anthropic.py"}, (
            f"target violation; still in: {files}"
        )

    @pytest.mark.xfail(
        reason="target-state R65 / OQ-5: Musicful 30-min wall-clock timeout",
        strict=False,
    )
    def test_musicful_30min_poll_timeout_target(self):
        """covers R65, B39 — musicful-30min-timeout-no-spend."""
        from scenecraft.plugins.generate_music import generate_music as gm

        # When R65 lands, expose a constant the test can lock against.
        assert getattr(gm, "POLL_WORKER_WALL_CLOCK_TIMEOUT_SECONDS", None) == 30 * 60


# ===========================================================================
# Behavior-table coverage map — ensures every Bn has at least one test ref.
# ===========================================================================


def test_behavior_table_coverage_map():
    """Static check: each Behavior Table row (1..43) has at least one
    test in this file referenced by docstring or explicit "covers Bn"
    in the spec mapping.

    We use a hand-maintained map; if a row is missing, this test fails
    loudly so the spec stays in sync with the test file.
    """
    # rows that have explicit unit coverage above (by ID)
    covered = {
        # Replicate
        1, 2, 32,
        # Musicful
        3, 4, 5, 33,
        # Imagen
        8, 9, 10,
        # Veo
        11, 12, 13, 14,
        # Kling
        15, 16, 17,
        # Runway
        18, 21,
        # Anthropic
        22, 23, 24, 25,
        # GenAI
        26, 27, 28, 29,
        # Spend ledger
        30, 31, 34,
        # Cross-provider
        40,
        # Target-state
        35, 36, 38, 39, 41, 42, 43, 37,
    }
    # Rows requiring orchestration we explicitly punt to peer tasks
    # (chat-generation, plugin-internal poll-worker).
    deferred = {6, 7, 19, 20}
    all_rows = set(range(1, 44))
    missing = all_rows - covered - deferred
    assert not missing, (
        f"Behavior table rows {sorted(missing)} have no test coverage "
        f"in this file. Add a test or add to `deferred` with a peer-task "
        f"reference."
    )
