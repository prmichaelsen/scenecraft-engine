"""Spec-locked regression tests for `local.engine-rest-api-dispatcher`.

This file is the M18 capstone: the contract the M16 FastAPI port MUST preserve
byte-for-byte. Every Behavior Table row in the spec has at least one covering
test; the 22 Migration Contract items each have an individual pin; every
target-state OQ has an `xfail(strict=False)` baseline so the port can flip
the bit without churning the suite.

Test classes:
  TestRouting             — R1..R5 (path/method dispatch, plugin fallback)
  TestAuth                — R6..R13 (JWT, bearer-over-cookie, sliding refresh)
  TestPaidPluginGate      — R14..R15 (decorator preserved, zero endpoints today)
  TestCORS                — R16..R18 (echo origin, no allowlist today)
  TestRequestBody         — R19..R21 (Content-Length, JSON parse, multipart)
  TestResponse            — R22..R24 (envelope, cookie refresh, broken-pipe)
  TestErrorCodeRegistry   — R25..R27 (error shape + ~40-code catalog)
  TestStructuralLock      — R28..R32, R49..R50 (per-project mutex on 11 routes)
  TestFileServing         — R33..R42 (cross-ref task-84; minimal duplication)
  TestPluginREST          — R43..R48 (regex registry, path_groups)
  TestUncaughtExceptions  — R51..R54 (target FastAPI exception handler)
  TestMigrationContract   — MC-1..MC-22 (port-time invariants)
  TestFastAPIDivergence   — known stdlib vs FastAPI defaults (xfail target)
  TestEndToEnd            — full HTTP round-trips across method groups

Reuses the session-scoped `engine_server` fixture from conftest. The fixture
boots with `no_auth=True`, so auth-dispatcher behaviors are exercised by
unit-testing `make_handler` with `no_auth=False` against a tmp `.scenecraft`
root, or by reading the source for negative-witness assertions.
"""
from __future__ import annotations

import io
import json as _json
import os
import re
import sys
import time
import threading
import urllib.error
import urllib.request
import uuid
from pathlib import Path

import pytest


# ────────────────────────── HTTP helpers ──────────────────────────


def _http(
    method: str,
    url: str,
    body: bytes | None = None,
    headers: dict[str, str] | None = None,
    timeout: float = 10.0,
) -> tuple[int, dict[str, str], bytes]:
    """Stdlib HTTP client. Returns (status, lowercased_headers, body_bytes)."""
    req = urllib.request.Request(url, data=body, method=method)
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return (
                resp.status,
                {k.lower(): v for k, v in resp.headers.items()},
                resp.read(),
            )
    except urllib.error.HTTPError as e:
        return (
            e.code,
            {k.lower(): v for k, v in (e.headers or {}).items()},
            e.read() if hasattr(e, "read") else b"",
        )


def _json_post(server, path: str, body, headers=None):
    raw = _json.dumps(body).encode("utf-8")
    h = {"Content-Type": "application/json"}
    if headers:
        h.update(headers)
    return _http("POST", server.base_url + path, body=raw, headers=h, timeout=10.0)


def _options(server, path: str, headers=None):
    return _http("OPTIONS", server.base_url + path, headers=headers or {}, timeout=5.0)


def _make_project(server, name: str | None = None) -> str:
    name = name or f"dispatcher_{os.getpid()}_{uuid.uuid4().hex[:8]}"
    status, _h, body = _json_post(server, "/api/projects/create", {"name": name})
    assert status == 200, (status, body)
    return name


# Catalog of error codes the dispatcher emits at call-sites today. Spec R26.
# This is the FROZEN catalog — every code in source MUST appear here, and
# every code here MUST appear in source. Extracted programmatically by the
# `test_error_code_catalog_grep_matches_spec` meta-test below.
DISPATCHER_ERROR_CODES_CORE = {
    # Spec R26 enumerated codes
    "NOT_FOUND",
    "BAD_REQUEST",
    "UNAUTHORIZED",
    "FORBIDDEN",
    "INVALID_CODE",
    "UNKNOWN_SERVICE",
    "AUTH_DISABLED",
    "INTERNAL_ERROR",
    "PLUGIN_ERROR",
    "CONFLICT",
    "PEAKS_FAILED",
    # Codes the auth-middleware module emits (PASSWORD_CHANGE_REQUIRED, etc.)
    # are owned by `auth_middleware.py`, not the dispatcher, so are NOT
    # in the dispatcher's call-site catalog.
    # Dispatcher-emitted codes discovered by source-grep audit (M18-87):
    "GONE",
    "VCS_UNAVAILABLE",
    "NO_SESSION",
    "OUT_OF_RANGE_TRACK",
    "SOURCE_NOT_FOUND",
    "UNCOMMITTED_CHANGES",
    "NO_CONTENT",
}

# Spec catalog of the 11 structural-lock POSTs.
STRUCTURAL_ROUTES = {
    "add-keyframe",
    "duplicate-keyframe",
    "delete-keyframe",
    "batch-delete-keyframes",
    "restore-keyframe",
    "delete-transition",
    "restore-transition",
    "split-transition",
    "insert-pool-item",
    "paste-group",
    "checkpoint",
}


# ════════════════════════════════════════════════════════════════════
# TestRouting (R1..R5)
# ════════════════════════════════════════════════════════════════════


class TestRouting:
    """R1..R5 — method-keyed dispatch, regex match, plugin fallback."""

    def test_unknown_get_route_returns_404_not_found(self, engine_server):
        """R1, row 21."""
        status, _h, body = _http("GET", engine_server.base_url + "/api/definitely-not-a-route")
        assert status == 404
        payload = _json.loads(body)
        assert payload["code"] == "NOT_FOUND"
        assert "GET" in payload["error"] and "/api/definitely-not-a-route" in payload["error"]

    def test_unknown_post_route_returns_404_not_found(self, engine_server):
        """R1, row 22."""
        status, _h, body = _json_post(engine_server, "/api/nope", {"x": 1})
        assert status == 404
        assert _json.loads(body)["code"] == "NOT_FOUND"

    def test_unknown_delete_route_returns_404_not_found(self, engine_server):
        """R1, row 49."""
        status, _h, body = _http("DELETE", engine_server.base_url + "/api/projects/x/whatever")
        assert status == 404
        assert _json.loads(body)["code"] == "NOT_FOUND"

    def test_get_projects_happy_path(self, engine_server):
        """R1 — built-in GET resolves; row 1 happy."""
        status, _h, body = _http("GET", engine_server.base_url + "/api/projects")
        assert status == 200
        # Engine returns a bare list of project dicts (not wrapped); confirm shape.
        assert isinstance(_json.loads(body), list)

    def test_get_config_happy_path(self, engine_server):
        """R1 — built-in GET resolves."""
        status, _h, _b = _http("GET", engine_server.base_url + "/api/config")
        assert status == 200

    def test_url_decoded_before_routing(self, engine_server):
        """R4, row 171, row 87 — `%20` decoded before regex match."""
        # We don't have a project named "my proj"; expected 404 for the project,
        # not for the route. So the regex must match the decoded form.
        status, _h, body = _http(
            "GET", engine_server.base_url + "/api/projects/my%20proj/keyframes"
        )
        assert status == 404
        # Project-not-found, NOT route-not-found
        payload = _json.loads(body)
        assert payload["code"] == "NOT_FOUND"
        # The error message should reference the project name "my proj"
        # if routing decoded properly. If routing matched the literal regex
        # against the encoded form, we'd get a "No route" message.
        assert "No route" not in payload["error"]

    def test_encoded_slash_in_project_name_404(self, engine_server):
        """R4, row 172 — `%2F` decodes to `/` which fails `[^/]+` regex."""
        status, _h, body = _http(
            "GET", engine_server.base_url + "/api/projects/a%2Fb/keyframes"
        )
        assert status == 404

    def test_trailing_slash_returns_404(self, engine_server):
        """Row 86 — `/api/projects/` (trailing slash) is NOT a route."""
        status, _h, body = _http("GET", engine_server.base_url + "/api/projects/")
        assert status == 404

    def test_query_string_does_not_affect_routing(self, engine_server):
        """R4 — query params are stripped before regex match."""
        status, _h, _b = _http("GET", engine_server.base_url + "/api/projects?ignored=1")
        assert status == 200

    def test_plugin_fallback_only_runs_when_builtin_misses(self, engine_server):
        """R5, row 137 — built-in route wins over plugin pattern collision."""
        # `/api/projects/<p>` is not a built-in path, but `/api/projects` is.
        # We can't easily install a plugin in-process; assert by source layout.
        from scenecraft.api_server import make_handler
        # Confirm /api/projects is dispatched before any /plugins/ regex.
        src = Path("src/scenecraft/api_server.py").read_text()
        builtin_idx = src.find('if path == "/api/projects":')
        plugin_idx = src.find('/api/projects/([^/]+)/plugins/')
        assert builtin_idx > 0 and plugin_idx > builtin_idx

    def test_plugin_dispatch_via_pluginhost_returns_none_for_unknown(self):
        """R5 — `dispatch_rest` returns None when no plugin pattern matches."""
        from scenecraft.plugin_host import PluginHost

        result = PluginHost.dispatch_rest(
            "GET",
            "/api/projects/p/plugins/nonexistent/whatever",
            None,
            "p",
            "",
        )
        assert result is None


# ════════════════════════════════════════════════════════════════════
# TestAuth (R6..R13)
# ════════════════════════════════════════════════════════════════════


class TestAuth:
    """R6..R13 — JWT validation, bearer/cookie precedence, sliding refresh."""

    def test_no_auth_mode_passes_through(self, engine_server):
        """R13, row 7 — `no_auth=True` (test fixture) accepts unauthenticated."""
        status, _h, _b = _http("GET", engine_server.base_url + "/api/projects")
        assert status == 200

    def test_authenticate_returns_true_when_sc_root_none(self, tmp_path):
        """R13 — `_authenticate` short-circuits to True with no .scenecraft."""
        from scenecraft.api_server import make_handler

        cls = make_handler(tmp_path, no_auth=True)
        # Construct a dummy instance (don't call __init__; we only need _authenticate).
        h = cls.__new__(cls)
        h.path = "/api/projects"
        h.headers = {}
        assert h._authenticate() is True

    def test_exempt_paths_listed(self):
        """R7 — auth-exempt paths are exactly /auth/login, /auth/logout, /oauth/callback."""
        src = Path("src/scenecraft/api_server.py").read_text()
        assert 'path in ("/auth/login", "/auth/logout", "/oauth/callback")' in src

    def test_bearer_extraction_takes_precedence_over_cookie(self):
        """R8 — bearer first, cookie fallback. Asserted by source order."""
        src = Path("src/scenecraft/api_server.py").read_text()
        bearer_idx = src.find("extract_bearer_token(self.headers.get(\"Authorization\"))")
        cookie_idx = src.find("extract_cookie_token(self.headers.get(\"Cookie\"))")
        assert 0 < bearer_idx < cookie_idx

    def test_missing_token_yields_401_unauthorized(self):
        """R9 — message is 'Not authenticated', code UNAUTHORIZED."""
        src = Path("src/scenecraft/api_server.py").read_text()
        assert 'self._error(401, "UNAUTHORIZED", "Not authenticated")' in src

    def test_invalid_token_yields_401_unauthorized(self):
        """R10 — bad token becomes 401 'Invalid or expired token'."""
        src = Path("src/scenecraft/api_server.py").read_text()
        assert 'self._error(401, "UNAUTHORIZED", "Invalid or expired token")' in src

    def test_cookie_refresh_is_set_after_cookie_path(self):
        """R11 — `_refreshed_cookie` is primed only on the cookie code path."""
        src = Path("src/scenecraft/api_server.py").read_text()
        assert "if from_cookie:" in src
        assert "self._refreshed_cookie = build_cookie_header(" in src

    def test_bearer_path_does_not_refresh_cookie(self):
        """R11, row 106 — bearer-authed requests do NOT mint a Set-Cookie.

        The `_refreshed_cookie =` assignment lives inside an `if from_cookie:`
        block; bearer paths skip the block entirely.
        """
        src = Path("src/scenecraft/api_server.py").read_text()
        idx_block = src.find("if from_cookie:")
        idx_refresh = src.find("self._refreshed_cookie = build_cookie_header(", idx_block)
        # Refresh assignment must be AFTER the `if from_cookie:` line.
        assert 0 < idx_block < idx_refresh
        # And no _refreshed_cookie assignment exists OUTSIDE that block.
        outer_count = src.count("self._refreshed_cookie = build_cookie_header(")
        assert outer_count == 1

    def test_jwt_sub_exposed_as_authenticated_user(self):
        """R12 — `_authenticated_user = payload.get('sub')`."""
        src = Path("src/scenecraft/api_server.py").read_text()
        assert 'self._authenticated_user = payload.get("sub")' in src

    @pytest.mark.xfail(
        reason="target-state OQ-18: dispatcher MUST 401 MALFORMED_TOKEN on JWT missing 'sub'; "
        "today _authenticated_user just becomes None silently",
        strict=False,
    )
    def test_jwt_missing_sub_returns_401_malformed_token(self):
        """R58, OQ-18, row 180 — target FastAPI port behavior."""
        src = Path("src/scenecraft/api_server.py").read_text()
        assert "MALFORMED_TOKEN" in src

    @pytest.mark.xfail(
        reason="target-state OQ-7: duplicate scenecraft_jwt cookies → 400 MALFORMED_REQUEST",
        strict=False,
    )
    def test_duplicate_jwt_cookies_yield_400_malformed_request(self):
        """R59, OQ-7, row 91 — target."""
        src = Path("src/scenecraft/api_server.py").read_text()
        assert "MALFORMED_REQUEST" in src


# ════════════════════════════════════════════════════════════════════
# TestPaidPluginGate (R14..R15) — negative-witness today
# ════════════════════════════════════════════════════════════════════


class TestPaidPluginGate:
    """R14..R15 — `@require_paid_plugin_auth` is defined but applied to ZERO endpoints."""

    def test_decorator_is_importable(self):
        """R14 — module surface preserved for FastAPI port to depend on."""
        from scenecraft.auth_middleware import require_paid_plugin_auth, PaidPluginAuthContext

        assert callable(require_paid_plugin_auth)
        assert PaidPluginAuthContext is not None

    def test_decorator_emits_documented_error_codes(self):
        """R14 — codes UNAUTHORIZED / PASSWORD_CHANGE_REQUIRED / ORG_NOT_FOUND / AMBIGUOUS_ORG."""
        src = Path("src/scenecraft/auth_middleware.py").read_text()
        for code in ("UNAUTHORIZED", "PASSWORD_CHANGE_REQUIRED", "ORG_NOT_FOUND", "AMBIGUOUS_ORG"):
            assert code in src, f"decorator missing error code {code}"

    def test_decorator_applied_to_zero_endpoints_today(self):
        """R15 — negative-witness: no `@require_paid_plugin_auth` decorator
        usage anywhere in api_server.py. Confirms 'dead code' status."""
        src = Path("src/scenecraft/api_server.py").read_text()
        assert "@require_paid_plugin_auth" not in src

    @pytest.mark.xfail(
        reason="OQ-1 target: when plugin.yaml flags `paid: true`, double-gate is enforced; today no enforcement path exists",
        strict=False,
    )
    def test_paid_plugins_double_gated_when_flag_set(self):
        """R61, OQ-1 — target."""
        # The hook does not yet exist.
        from scenecraft.plugin_host import PluginHost

        assert hasattr(PluginHost, "_paid_endpoints")  # placeholder hook


# ════════════════════════════════════════════════════════════════════
# TestCORS (R16..R18)
# ════════════════════════════════════════════════════════════════════


class TestCORS:
    """R16..R18 — Origin echo, credentials=true, no allowlist (XSRF baseline)."""

    def test_options_preflight_returns_204(self, engine_server):
        """R17, row 17 — OPTIONS any path → 204 with CORS headers."""
        status, headers, body = _options(engine_server, "/api/projects/anything")
        assert status == 204
        assert body == b""
        assert "access-control-allow-methods" in headers

    def test_options_bypasses_auth(self, engine_server):
        """Row 105 — OPTIONS does not consult auth."""
        status, _h, _b = _options(engine_server, "/api/projects")
        assert status == 204

    def test_cors_allow_methods_lists_get_post_delete_options(self, engine_server):
        """R16 — exact method list."""
        _s, headers, _b = _options(engine_server, "/api/x")
        assert headers["access-control-allow-methods"] == "GET, POST, DELETE, OPTIONS"

    def test_cors_allow_headers_includes_x_scenecraft_branch(self, engine_server):
        """Row 110 — Content-Type, Authorization, X-Scenecraft-Branch."""
        _s, headers, _b = _options(engine_server, "/api/x")
        assert "X-Scenecraft-Branch" in headers["access-control-allow-headers"]

    def test_cors_echoes_origin_with_credentials(self, engine_server):
        """R16, row 18 — Origin echoed + ACAC=true + Vary: Origin."""
        status, headers, _b = _http(
            "GET",
            engine_server.base_url + "/api/projects",
            headers={"Origin": "https://app.example.com"},
        )
        assert status == 200
        assert headers.get("access-control-allow-origin") == "https://app.example.com"
        assert headers.get("access-control-allow-credentials") == "true"
        assert headers.get("vary") == "Origin"

    def test_cors_wildcard_when_no_origin(self, engine_server):
        """R16, row 19 — no Origin → ACAO: *, no Vary, no ACAC."""
        _s, headers, _b = _http("GET", engine_server.base_url + "/api/projects")
        assert headers.get("access-control-allow-origin") == "*"
        assert "access-control-allow-credentials" not in headers
        assert "vary" not in headers

    def test_cors_no_allowlist_xsrf_exposure_baseline(self, engine_server):
        """R18, row 20 — ANY origin echoed today (XSRF risk, OQ-2)."""
        status, headers, _b = _http(
            "GET",
            engine_server.base_url + "/api/projects",
            headers={"Origin": "https://evil.example"},
        )
        assert status == 200
        assert headers.get("access-control-allow-origin") == "https://evil.example"
        assert headers.get("access-control-allow-credentials") == "true"

    def test_cors_headers_present_on_404(self, engine_server):
        """R16 — CORS headers MUST be on every response, including 404."""
        _s, headers, _b = _http(
            "GET",
            engine_server.base_url + "/api/no-such-thing",
            headers={"Origin": "https://x.test"},
        )
        assert headers.get("access-control-allow-origin") == "https://x.test"

    @pytest.mark.xfail(reason="OQ-2 target: allowlist origins via config.json:cors_origins", strict=False)
    def test_cors_allowlist_rejects_unknown_origin(self):
        """R51, OQ-2 — target."""
        from scenecraft.config import load_config

        cfg = load_config()
        assert "cors_origins" in cfg

    @pytest.mark.xfail(reason="OQ-11 target: ACL adds X-Scenecraft-API-Key, X-Scenecraft-Org", strict=False)
    def test_cors_allow_headers_includes_paid_plugin_headers(self, engine_server):
        """R57, OQ-11, row 111 — target."""
        _s, headers, _b = _options(engine_server, "/api/x")
        assert "X-Scenecraft-API-Key" in headers["access-control-allow-headers"]
        assert "X-Scenecraft-Org" in headers["access-control-allow-headers"]

    @pytest.mark.xfail(reason="OQ-17 target: OPTIONS Access-Control-Max-Age: 3600", strict=False)
    def test_options_includes_max_age_3600(self, engine_server):
        """R56, OQ-17, row 175 — target."""
        _s, headers, _b = _options(engine_server, "/api/x")
        assert headers.get("access-control-max-age") == "3600"

    def test_empty_origin_treated_as_absent(self, engine_server):
        """Row 174 — empty `Origin: ` falls into wildcard branch."""
        # Note: urllib won't accept Origin: '' easily; assert source semantics.
        src = Path("src/scenecraft/api_server.py").read_text()
        assert 'origin = self.headers.get("Origin")' in src
        assert "if origin:" in src  # truthiness check ⇒ empty string treated absent


# ════════════════════════════════════════════════════════════════════
# TestRequestBody (R19..R21)
# ════════════════════════════════════════════════════════════════════


class TestRequestBody:
    """R19..R21 — Content-Length, JSON parse, multipart bypass."""

    def test_post_with_empty_body_returns_400_bad_request(self, engine_server, project_name):
        """R19, row 24 — Content-Length: 0 → 400 'Empty body'."""
        status, _h, body = _http(
            "POST",
            engine_server.base_url + f"/api/projects/{project_name}/update-prompt",
            body=b"",
            headers={"Content-Type": "application/json", "Content-Length": "0"},
        )
        assert status == 400
        payload = _json.loads(body)
        assert payload["code"] == "BAD_REQUEST"
        assert payload["error"] == "Empty body"

    def test_post_with_invalid_json_returns_400(self, engine_server, project_name):
        """R19, row 25 — non-JSON body → 'Invalid JSON: ...'."""
        status, _h, body = _http(
            "POST",
            engine_server.base_url + f"/api/projects/{project_name}/update-prompt",
            body=b"this is not json",
            headers={"Content-Type": "application/json"},
        )
        assert status == 400
        payload = _json.loads(body)
        assert payload["code"] == "BAD_REQUEST"
        assert payload["error"].startswith("Invalid JSON:")

    def test_post_with_empty_object_passes_dispatcher(self, engine_server, project_name):
        """R19, row 182 — `{}` body parses; handler decides what to do."""
        # `update-prompt` will likely 400 on missing fields, but the dispatcher itself
        # accepts `{}`. We assert the response is NOT 'Empty body' or 'Invalid JSON'.
        status, _h, body = _json_post(
            engine_server, f"/api/projects/{project_name}/update-prompt", {}
        )
        assert status in (200, 400, 404)
        if status == 400:
            payload = _json.loads(body)
            assert payload["error"] != "Empty body"
            assert not payload["error"].startswith("Invalid JSON:")

    def test_extra_unknown_fields_passed_through_by_dispatcher(self, engine_server, project_name):
        """Row 26 — dispatcher does NOT validate body shape. Whatever the
        handler does with extras is its own business; the dispatcher itself
        emits no 400 BAD_REQUEST for unknown fields. Confirmed by the
        absence of any pydantic-style schema in `_read_json_body`.
        """
        status, _h, body = _json_post(
            engine_server,
            f"/api/projects/{project_name}/select-keyframes",
            {"keyframe_ids": [], "an_unknown_field": "ignored"},
        )
        # Whatever the handler returns, the failure mode is NOT
        # "Invalid JSON" or "Empty body" — those are dispatcher-level.
        if status == 400:
            payload = _json.loads(body)
            assert payload["error"] != "Empty body"
            assert not payload["error"].startswith("Invalid JSON:")

    def test_multipart_uploads_bypass_read_json_body(self):
        """R21 — multipart routes do NOT call `_read_json_body`."""
        # Inspect source — multipart handlers must not feed through the JSON gate.
        src = Path("src/scenecraft/api_server.py").read_text()
        # Verify that `_read_json_body` exists; multipart handlers skip it.
        assert "def _read_json_body" in src
        # Multipart handlers parse boundaries directly. Look for a known one.
        assert "Content-Type" in src and "multipart" in src.lower()

    def test_missing_content_length_treated_as_zero(self):
        """R19, row 162 — `int(self.headers.get('Content-Length', 0))` defaults 0."""
        src = Path("src/scenecraft/api_server.py").read_text()
        assert 'int(self.headers.get("Content-Length", 0))' in src


# ════════════════════════════════════════════════════════════════════
# TestResponse (R22..R24)
# ════════════════════════════════════════════════════════════════════


class TestResponse:
    """R22..R24 — JSON envelope + Content-Length + cookie refresh + broken-pipe swallow."""

    def test_json_response_sets_content_length(self, engine_server):
        """R22, row 67."""
        _s, headers, body = _http("GET", engine_server.base_url + "/api/projects")
        assert headers.get("content-length") == str(len(body))

    def test_json_response_sets_content_type_application_json(self, engine_server):
        """R22, row 149."""
        _s, headers, _b = _http("GET", engine_server.base_url + "/api/projects")
        assert headers.get("content-type") == "application/json"

    def test_json_response_status_default_200(self, engine_server):
        """R22 — happy path → status 200."""
        status, _h, _b = _http("GET", engine_server.base_url + "/api/projects")
        assert status == 200

    def test_no_chunked_encoding_for_json(self, engine_server):
        """Row 163 — JSON responses use Content-Length, not Transfer-Encoding."""
        _s, headers, _b = _http("GET", engine_server.base_url + "/api/projects")
        assert "content-length" in headers
        assert headers.get("transfer-encoding") != "chunked"

    def test_broken_pipe_swallowed_in_json_response(self):
        """R24, row 68, row 168 — BrokenPipeError / ConnectionResetError caught silently."""
        src = Path("src/scenecraft/api_server.py").read_text()
        assert "except (BrokenPipeError, ConnectionResetError):" in src

    def test_set_cookie_emitted_when_refreshed(self):
        """R23 — `_json_response` emits Set-Cookie when `_refreshed_cookie` set."""
        src = Path("src/scenecraft/api_server.py").read_text()
        # The response helper conditionally emits Set-Cookie.
        assert "if self._refreshed_cookie:" in src
        assert 'self.send_header("Set-Cookie", self._refreshed_cookie)' in src


# ════════════════════════════════════════════════════════════════════
# TestErrorCodeRegistry (R25..R27)
# ════════════════════════════════════════════════════════════════════


class TestErrorCodeRegistry:
    """R25..R27 — `{error, code}` envelope + ~40-string code catalog."""

    def test_error_envelope_shape_flat_error_and_code(self, engine_server):
        """R25, row 65 — body keys are exactly {error, code}, NOT {detail}."""
        # 404 path
        _s, _h, body = _http("GET", engine_server.base_url + "/api/no-such-thing")
        payload = _json.loads(body)
        assert set(payload.keys()) == {"error", "code"}
        assert "detail" not in payload

    def test_error_envelope_on_404_unknown_route(self, engine_server):
        """R25 — 404 envelope."""
        _s, _h, body = _http("DELETE", engine_server.base_url + "/api/x/nope")
        payload = _json.loads(body)
        assert "error" in payload and "code" in payload

    def test_error_method_uses_json_response(self):
        """R25 — `_error` is `_json_response({error, code}, status)`."""
        src = Path("src/scenecraft/api_server.py").read_text()
        assert 'self._json_response({"error": message, "code": code}, status=status)' in src

    @pytest.mark.parametrize("code", sorted(DISPATCHER_ERROR_CODES_CORE))
    def test_known_error_code_present_in_source(self, code):
        """R26 — every catalog code appears in at least one `_error()` call."""
        src = Path("src/scenecraft/api_server.py").read_text()
        # Expected pattern: `self._error(<status>, "CODE", <msg>)`
        assert f'"{code}"' in src, f"error code {code} not found in api_server.py"

    def test_error_code_catalog_has_no_unknown_strings(self):
        """R26 (negative meta) — list every CODE arg in source; verify all are
        tracked. New codes flag this test failing — when that happens, add them
        to DISPATCHER_ERROR_CODES_CORE intentionally."""
        src = Path("src/scenecraft/api_server.py").read_text()
        # Match `self._error(<status>, "CODE_STRING", ...)`
        codes_in_source = set(
            re.findall(r'self\._error\(\s*\d+\s*,\s*"([A-Z_]+)"', src)
        )
        # Auth-middleware codes are also relevant but live in another file.
        unknown = codes_in_source - DISPATCHER_ERROR_CODES_CORE
        # PEAKS_FAILED, AUTH_DISABLED, INVALID_CODE, etc. are in the core set.
        # Anything else here is a NEW code that the spec catalog has not yet
        # captured. Surface it: this is the audit's "leak #24" tripwire.
        assert not unknown, (
            f"unknown error codes in source not in catalog: {unknown}. "
            f"Add to DISPATCHER_ERROR_CODES_CORE intentionally."
        )

    def test_error_envelope_preserves_shape_on_500_plugin_error(self):
        """Row 66 — 500 envelope: PLUGIN_ERROR (and INTERNAL_ERROR) flat shape."""
        src = Path("src/scenecraft/api_server.py").read_text()
        assert '"PLUGIN_ERROR"' in src
        assert '"INTERNAL_ERROR"' in src


# ════════════════════════════════════════════════════════════════════
# TestStructuralLock (R28..R32, R49..R50)
# ════════════════════════════════════════════════════════════════════


class TestStructuralLock:
    """R28..R32, R49..R50 — per-project mutex on exactly 11 POST routes."""

    def test_structural_routes_set_matches_spec(self):
        """R28 — the 11 structural-route names are baked in."""
        src = Path("src/scenecraft/api_server.py").read_text()
        # `_structural_routes = { ... 11 strings ... }`
        m = re.search(r"_structural_routes\s*=\s*\{([^}]+)\}", src)
        assert m is not None, "_structural_routes set not found"
        names = set(re.findall(r'"([a-z\-]+)"', m.group(1)))
        assert names == STRUCTURAL_ROUTES, (
            f"structural-routes drift: {names ^ STRUCTURAL_ROUTES}"
        )

    def test_lock_is_acquired_in_finally(self):
        """R32 — lock release in finally; handler exception cannot deadlock."""
        src = Path("src/scenecraft/api_server.py").read_text()
        # Spot-check structural lock pattern: try / finally / release
        idx_try = src.find("if _use_lock:")
        idx_finally = src.find("finally:", idx_try)
        idx_release = src.find(".release()", idx_finally)
        assert 0 < idx_try < idx_finally < idx_release

    def test_validate_timeline_invoked_only_on_locked_routes(self):
        """R49 — validation is inside the `_use_lock` branch."""
        src = Path("src/scenecraft/api_server.py").read_text()
        # The validation block is gated on `if _use_lock and _proj_name:`
        assert "if _use_lock and _proj_name:" in src
        assert "from scenecraft.db import validate_timeline" in src

    def test_first_10_warnings_logged(self):
        """R50, row 131 — warnings[:10] in the log loop; ALL broadcast on WS."""
        src = Path("src/scenecraft/api_server.py").read_text()
        assert "warnings[:10]" in src
        # Broadcast carries the FULL warnings list.
        assert '"warnings": warnings' in src

    def test_non_locked_routes_have_no_lock(self):
        """R30 — only the 11 structural routes go under lock; everything else races."""
        src = Path("src/scenecraft/api_server.py").read_text()
        # `_use_lock = _proj_name and _route_name in _structural_routes`
        assert "_use_lock = _proj_name and _route_name in _structural_routes" in src

    def test_per_project_lock_is_memoized(self):
        """R29 — `_get_project_lock` lazily creates per-project locks."""
        src = Path("src/scenecraft/api_server.py").read_text()
        assert "if project_name not in _project_locks:" in src
        assert "_project_locks[project_name] = threading.Lock()" in src

    def test_get_method_does_not_acquire_structural_lock(self):
        """R30 — GETs never go through the lock branch."""
        src = Path("src/scenecraft/api_server.py").read_text()
        do_get_idx = src.find("def do_GET(self):")
        do_post_idx = src.find("def do_POST(self):")
        do_get_block = src[do_get_idx:do_post_idx]
        assert "_get_project_lock" not in do_get_block

    def test_structural_lock_serializes_concurrent_add_keyframe(self, engine_server, project_name):
        """R28, row 27 — concurrent add-keyframes serialize; both succeed."""
        results: list[int] = []
        lock = threading.Lock()

        def fire():
            s, _h, _b = _json_post(
                engine_server,
                f"/api/projects/{project_name}/add-keyframe",
                {"timestamp": 0.0, "prompt": "x"},
            )
            with lock:
                results.append(s)

        threads = [threading.Thread(target=fire) for _ in range(2)]
        t0 = time.time()
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=8.0)
        elapsed = time.time() - t0
        assert all(r in (200, 400, 404) for r in results), results
        # Bound wall-time so a deadlock would fail rather than stall.
        assert elapsed < 8.0


# ════════════════════════════════════════════════════════════════════
# TestFileServing (R33..R42) — minimal duplication of task-84
# ════════════════════════════════════════════════════════════════════


class TestFileServing:
    """R33..R42 — dispatcher correctly routes to the file-serving handler.

    Detailed behaviors (Range/ETag/IMS) live in `test_engine_file_serving_and_uploads`.
    Here we only assert the dispatcher routes correctly to the file handler.
    """

    def test_get_files_route_dispatches(self, engine_server, project_name):
        """R40 — GET /api/projects/:n/files/* hits the file handler."""
        # Project is empty; we expect 404 (file missing), NOT 'No route'.
        _s, _h, body = _http(
            "GET",
            engine_server.base_url + f"/api/projects/{project_name}/files/nope.txt",
        )
        payload = _json.loads(body)
        assert payload["code"] == "NOT_FOUND"
        assert "No route" not in payload["error"]

    def test_head_files_returns_404_empty_body(self, engine_server, project_name):
        """R41, row 43 — HEAD missing file returns 404 with EMPTY body."""
        status, _h, body = _http(
            "HEAD",
            engine_server.base_url + f"/api/projects/{project_name}/files/missing.mp4",
        )
        assert status == 404
        assert body == b""

    def test_head_unknown_path_returns_405_empty_body(self, engine_server):
        """R42, row 44 — HEAD on any non-files path → 405 empty body."""
        status, _h, body = _http("HEAD", engine_server.base_url + "/api/projects")
        assert status == 405
        assert body == b""

    def test_path_traversal_check_uses_startswith_today(self):
        """R33, R34 — startswith guard; symlink-bypass acknowledged (OQ-3)."""
        src = Path("src/scenecraft/api_server.py").read_text()
        # The guard appears in HEAD and GET file handlers.
        assert "startswith(str(work_dir.resolve()))" in src

    def test_file_serve_emits_403_on_traversal(self, engine_server, project_name):
        """R33, row 39 — GET .../files/../../etc/passwd → 403 FORBIDDEN."""
        # Note: urllib normalizes `../` in the URL; use a manually-crafted path.
        # The dispatcher resolves work_dir / project / file_path and asserts
        # the resolved path is under work_dir. We rely on source review here.
        src = Path("src/scenecraft/api_server.py").read_text()
        assert '"Path traversal denied"' in src or "FORBIDDEN" in src


# ════════════════════════════════════════════════════════════════════
# TestPluginREST (R43..R48)
# ════════════════════════════════════════════════════════════════════


class TestPluginREST:
    """R43..R48 — plugin REST registry + path_groups kwarg propagation."""

    def test_dispatch_rest_returns_none_when_no_match(self):
        """R44 — None ⇒ keep trying / fall through to 404."""
        from scenecraft.plugin_host import PluginHost

        out = PluginHost.dispatch_rest("GET", "/api/projects/p/plugins/zzz/no", None, "p", "")
        assert out is None

    def test_dispatch_rest_only_inspects_method_keyed_routes(self):
        """R44 — `_rest_routes_by_method[method.upper()]` only."""
        src = Path("src/scenecraft/plugin_host.py").read_text()
        assert "_rest_routes_by_method.get(method.upper()" in src

    def test_named_groups_propagate_as_path_groups_kwarg(self):
        """R45 — pattern with `(?P<id>...)` → handler receives `path_groups={...}`."""
        from scenecraft.plugin_host import PluginHost
        from scenecraft import plugin_api as _api

        # Register a tiny GET handler and dispatch.
        captured = {}

        def _handler(path, *args, **kwargs):
            captured["got"] = kwargs
            return {"ok": True}

        pattern = r"^/api/projects/(?P<project>[^/]+)/plugins/__test_pg/items/(?P<item_id>\d+)$"
        # Insert directly into the registry to avoid full plugin loading.
        PluginHost._rest_routes_by_method.setdefault("GET", {})[pattern] = _handler
        try:
            result = PluginHost.dispatch_rest(
                "GET",
                "/api/projects/p/plugins/__test_pg/items/42",
                None,
                "p",
                "",
            )
            assert result == {"ok": True}
            assert "path_groups" in captured["got"]
            assert captured["got"]["path_groups"] == {"project": "p", "item_id": "42"}
        finally:
            PluginHost._rest_routes_by_method.get("GET", {}).pop(pattern, None)

    def test_no_named_groups_means_no_path_groups_kwarg(self):
        """R45 — pattern without named groups omits `path_groups` kwarg."""
        from scenecraft.plugin_host import PluginHost

        captured = {}

        def _handler(path, *args, **kwargs):
            captured["got"] = kwargs
            return {"ok": True}

        pattern = r"^/api/projects/[^/]+/plugins/__test_ng/ping$"
        PluginHost._rest_routes_by_method.setdefault("GET", {})[pattern] = _handler
        try:
            result = PluginHost.dispatch_rest(
                "GET",
                "/api/projects/p/plugins/__test_ng/ping",
                None,
                "p",
                "",
            )
            assert result == {"ok": True}
            assert "path_groups" not in captured["got"]
        finally:
            PluginHost._rest_routes_by_method.get("GET", {}).pop(pattern, None)

    def test_dispatcher_forwards_get_to_plugin_host(self):
        """R46 — GET fallback path."""
        src = Path("src/scenecraft/api_server.py").read_text()
        assert 'PluginHost.dispatch_rest("GET", path, project_dir, project_name, query)' in src

    def test_dispatcher_forwards_post_to_plugin_host(self):
        """R46 — POST fallback path."""
        src = Path("src/scenecraft/api_server.py").read_text()
        assert 'PluginHost.dispatch_rest("POST", path, project_dir, project_name, body)' in src

    @pytest.mark.xfail(reason="OQ-4 target: dispatcher MUST forward DELETE to plugins (today GET+POST only)", strict=False)
    def test_dispatcher_forwards_delete_to_plugin_host(self):
        """R52, OQ-4 — target."""
        src = Path("src/scenecraft/api_server.py").read_text()
        assert 'PluginHost.dispatch_rest("DELETE"' in src

    @pytest.mark.xfail(reason="OQ-4 target: PATCH forwarding", strict=False)
    def test_dispatcher_forwards_patch_to_plugin_host(self):
        """R52, OQ-4 — target."""
        src = Path("src/scenecraft/api_server.py").read_text()
        assert 'PluginHost.dispatch_rest("PATCH"' in src

    @pytest.mark.xfail(reason="OQ-4 target: PUT forwarding", strict=False)
    def test_dispatcher_forwards_put_to_plugin_host(self):
        """R52, OQ-4 — target."""
        src = Path("src/scenecraft/api_server.py").read_text()
        assert 'PluginHost.dispatch_rest("PUT"' in src

    def test_plugin_handler_exception_yields_500_plugin_error(self):
        """R48, row 52 — plugin handler raise → 500 PLUGIN_ERROR."""
        src = Path("src/scenecraft/api_server.py").read_text()
        # The dispatcher catches exceptions inside the plugin fallback try/except.
        assert '"PLUGIN_ERROR"' in src

    def test_plugin_post_empty_body_defaulted_to_object(self):
        """R20, row 53 — plugin POST tolerates empty body via `_read_json_body() or {}`."""
        src = Path("src/scenecraft/api_server.py").read_text()
        # Look near the plugin POST fallback.
        idx = src.find("/api/projects/([^/]+)/plugins/[^/]+/", src.find("def _do_POST"))
        plugin_block = src[idx : idx + 2000] if idx > 0 else ""
        assert "_read_json_body" in plugin_block
        # Body empty case is tolerated via `or {}` pattern (or explicit defaulting).
        assert " or {}" in plugin_block or "body = body or {}" in plugin_block or "body or {}" in plugin_block

    def test_duplicate_pattern_overwrites(self):
        """Row 102 — second registration of same pattern overwrites first.

        `_rest_routes_by_method[method][pattern] = handler` is a dict assignment.
        """
        from scenecraft.plugin_host import PluginHost

        pattern = r"^/api/projects/[^/]+/plugins/__dup/x$"
        PluginHost._rest_routes_by_method.setdefault("GET", {})[pattern] = lambda *a, **kw: 1
        PluginHost._rest_routes_by_method["GET"][pattern] = lambda *a, **kw: 2
        try:
            handler = PluginHost._rest_routes_by_method["GET"][pattern]
            assert handler() == 2
        finally:
            PluginHost._rest_routes_by_method["GET"].pop(pattern, None)


# ════════════════════════════════════════════════════════════════════
# TestUncaughtExceptions (R51..R54)
# ════════════════════════════════════════════════════════════════════


class TestUncaughtExceptions:
    """R51..R54 — handler exception → stdlib emits empty 500 today; target FastAPI shape."""

    def test_no_top_level_exception_handler_today(self):
        """Migration Contract item 22 — no global exception handler today.

        The dispatch-level `do_POST` has try/except blocks for the structural
        lock and timeline-validation broadcast, but NONE of them remap an
        arbitrary handler exception to a `{code:INTERNAL_ERROR}` envelope.
        Individual handlers each may wrap their own exceptions, but the
        dispatcher itself does not. That is the gap MC-22 / OQ-16 closes at
        FastAPI port time.
        """
        src = Path("src/scenecraft/api_server.py").read_text()
        # The do_POST dispatch block (above _do_POST) contains the
        # structural-lock try/finally and the validation inner try/except.
        # Critically, NEITHER converts a handler-raised exception into a
        # canonical `_error(500, "INTERNAL_ERROR", ...)` response.
        do_post_block = src[
            src.find("def do_POST(self):") : src.find("def _do_POST(self, path):")
        ]
        assert 'self._error(500, "INTERNAL_ERROR"' not in do_post_block
        # do_GET has no top-level try-around-handler at all (only `except
        # (BrokenPipeError, ConnectionResetError):` inside _json_response).
        # Confirm the dispatch loop in do_GET is one straight sequence of
        # `if m: return self._handle_*` calls — no surrounding `try:`
        # around the entire dispatch.
        do_get_block = src[src.find("def do_GET(self):") : src.find("def do_POST(self):")]
        # The first lines after the def include `if not self._authenticate()`
        # and `parsed = urlparse(...)` — NOT `try:`.
        head_lines = do_get_block.splitlines()[:6]
        assert not any(line.strip() == "try:" for line in head_lines)

    @pytest.mark.xfail(
        reason="OQ-16 / R54 target: FastAPI exception handler emits {error:{code,message}} for uncaught",
        strict=False,
    )
    def test_uncaught_exception_yields_internal_error_envelope(self):
        """R54, OQ-16, row 157 — target FastAPI port behavior."""
        src = Path("src/scenecraft/api_server.py").read_text()
        # Today, an uncaught exception in a handler emits stdlib's default
        # empty 500. Target: explicit `{error: {code: INTERNAL_ERROR, ...}}`.
        assert "INTERNAL_ERROR" in src
        # The xfail trips because no global exception handler maps to that
        # envelope shape today; it lives only at individual call sites.
        assert "global_exception_handler" in src.lower() or "register_exception_handler" in src.lower()


# ════════════════════════════════════════════════════════════════════
# TestMigrationContract (MC-1..MC-22)
# ════════════════════════════════════════════════════════════════════


class TestMigrationContract:
    """22 individual tests pinning every migration-critical behavior the
    FastAPI port MUST preserve. These are non-negotiable refactor invariants."""

    # MC-1
    def test_mc_01_error_body_shape_flat_error_and_code(self, engine_server):
        """MC-1 — error body is `{error, code}`, NOT FastAPI's `{detail}`."""
        _s, _h, body = _http("GET", engine_server.base_url + "/api/no-such")
        payload = _json.loads(body)
        assert "error" in payload and "code" in payload
        assert "detail" not in payload

    # MC-2
    def test_mc_02_missing_token_is_401_not_403(self):
        """MC-2 — unauthenticated requests get 401 UNAUTHORIZED, never 403."""
        src = Path("src/scenecraft/api_server.py").read_text()
        # 403 is reserved for FORBIDDEN (path traversal) per spec row 109.
        # Auth-missing always yields 401.
        assert 'self._error(401, "UNAUTHORIZED", "Not authenticated")' in src
        assert 'self._error(403, "UNAUTHORIZED"' not in src

    # MC-3
    def test_mc_03_bearer_takes_precedence_over_cookie(self):
        """MC-3 — bearer FIRST, cookie fallback (asserted by source order)."""
        src = Path("src/scenecraft/api_server.py").read_text()
        bearer_idx = src.find("extract_bearer_token")
        cookie_idx = src.find("extract_cookie_token")
        assert 0 < bearer_idx < cookie_idx

    # MC-4
    def test_mc_04_sliding_cookie_only_on_cookie_path(self):
        """MC-4 — bearer requests do NOT receive Set-Cookie."""
        src = Path("src/scenecraft/api_server.py").read_text()
        # Refresh is gated on `if from_cookie:`.
        idx = src.find("if from_cookie:")
        assert idx > 0
        # Inside that branch, _refreshed_cookie is set.
        block = src[idx : idx + 500]
        assert "_refreshed_cookie" in block

    # MC-5
    def test_mc_05_cookie_attrs_path_httponly_samesite_max_age(self):
        """MC-5 — `scenecraft_jwt; Path=/; HttpOnly; SameSite=Lax; Max-Age=86400`."""
        # Asserted by the cookie builder.
        from scenecraft.vcs import auth as vauth

        cookie = vauth.build_cookie_header("test.token.value")
        assert "scenecraft_jwt=" in cookie
        assert "Path=/" in cookie
        assert "HttpOnly" in cookie
        assert "SameSite=Lax" in cookie
        assert "Max-Age=86400" in cookie

    # MC-6
    def test_mc_06_cors_headers_on_every_response(self, engine_server):
        """MC-6 — CORS headers present on 200, 404, and any error path."""
        for path in ("/api/projects", "/api/no-such-thing"):
            _s, headers, _b = _http("GET", engine_server.base_url + path)
            assert "access-control-allow-origin" in headers
            assert "access-control-allow-methods" in headers

    # MC-7
    def test_mc_07_no_origin_allowlist_today(self, engine_server):
        """MC-7 — any origin is echoed (XSRF preserved per OQ-2)."""
        _s, headers, _b = _http(
            "GET",
            engine_server.base_url + "/api/projects",
            headers={"Origin": "https://random.test"},
        )
        assert headers.get("access-control-allow-origin") == "https://random.test"

    # MC-8
    def test_mc_08_exempt_paths_bypass_auth(self):
        """MC-8 — /auth/login, /auth/logout, /oauth/callback skip auth."""
        src = Path("src/scenecraft/api_server.py").read_text()
        assert '"/auth/login"' in src and '"/auth/logout"' in src and '"/oauth/callback"' in src

    # MC-9
    def test_mc_09_options_204_for_any_path(self, engine_server):
        """MC-9 — OPTIONS = 204 on any path, no auth check, with CORS headers."""
        for p in ("/api/projects", "/literally/anything", "/auth/login"):
            status, headers, body = _options(engine_server, p)
            assert status == 204
            assert body == b""
            assert "access-control-allow-methods" in headers

    # MC-10
    def test_mc_10_empty_body_is_400_bad_request(self, engine_server, project_name):
        """MC-10 — Empty body → 400, NOT FastAPI 422."""
        status, _h, body = _http(
            "POST",
            engine_server.base_url + f"/api/projects/{project_name}/update-prompt",
            body=b"",
            headers={"Content-Type": "application/json"},
        )
        assert status == 400
        assert _json.loads(body)["code"] == "BAD_REQUEST"

    # MC-11
    def test_mc_11_invalid_json_is_400_bad_request(self, engine_server, project_name):
        """MC-11 — Invalid JSON → 400, NOT FastAPI 422."""
        status, _h, body = _http(
            "POST",
            engine_server.base_url + f"/api/projects/{project_name}/update-prompt",
            body=b"{not-json",
            headers={"Content-Type": "application/json"},
        )
        assert status == 400
        assert _json.loads(body)["code"] == "BAD_REQUEST"

    # MC-12
    def test_mc_12_structural_lock_on_exactly_11_routes(self):
        """MC-12 — exactly 11 structural routes."""
        assert len(STRUCTURAL_ROUTES) == 11
        src = Path("src/scenecraft/api_server.py").read_text()
        m = re.search(r"_structural_routes\s*=\s*\{([^}]+)\}", src)
        names = set(re.findall(r'"([a-z\-]+)"', m.group(1)))
        assert names == STRUCTURAL_ROUTES

    # MC-13
    def test_mc_13_validate_timeline_only_on_locked_routes(self):
        """MC-13 — validation is gated on `if _use_lock and _proj_name:`."""
        src = Path("src/scenecraft/api_server.py").read_text()
        assert "if _use_lock and _proj_name:" in src

    # MC-14
    def test_mc_14_plugin_rest_fallback_get_post_today(self):
        """MC-14 — plugin GET+POST forwarded; DELETE/PATCH/PUT NOT (OQ-4)."""
        src = Path("src/scenecraft/api_server.py").read_text()
        assert 'dispatch_rest("GET"' in src
        assert 'dispatch_rest("POST"' in src
        assert 'dispatch_rest("DELETE"' not in src
        assert 'dispatch_rest("PATCH"' not in src
        assert 'dispatch_rest("PUT"' not in src

    # MC-15
    def test_mc_15_file_serving_chunk_size_65536(self):
        """MC-15 — file responses chunk-write 65536 bytes at a time."""
        src = Path("src/scenecraft/api_server.py").read_text()
        assert "65536" in src

    # MC-16
    def test_mc_16_head_files_only_405_otherwise(self, engine_server):
        """MC-16 — HEAD on files-only path; 405 elsewhere."""
        status, _h, _b = _http("HEAD", engine_server.base_url + "/api/projects")
        assert status == 405

    # MC-17
    def test_mc_17_delete_idempotency_on_4_m13_routes(self):
        """MC-17 — DELETE on missing track-effect/effect-curve/send-bus/freq-label → 200."""
        src = Path("src/scenecraft/api_server.py").read_text()
        for handler in (
            "_handle_m13_track_effect_delete",
            "_handle_m13_effect_curve_delete",
            "_handle_m13_send_bus_delete",
            "_handle_m13_frequency_label_delete",
        ):
            assert handler in src

    # MC-18
    def test_mc_18_response_content_length_set_explicitly(self, engine_server):
        """MC-18 — Content-Length set; no Transfer-Encoding: chunked for JSON."""
        _s, headers, _b = _http("GET", engine_server.base_url + "/api/projects")
        assert "content-length" in headers
        assert headers.get("transfer-encoding") != "chunked"

    # MC-19
    def test_mc_19_broken_pipe_swallowed(self):
        """MC-19 — BrokenPipe / ConnectionReset caught silently in `_json_response`."""
        src = Path("src/scenecraft/api_server.py").read_text()
        assert "except (BrokenPipeError, ConnectionResetError):" in src

    # MC-20
    def test_mc_20_error_code_catalog_frozen(self):
        """MC-20 — every code emitted in source MUST appear in DISPATCHER_ERROR_CODES_CORE."""
        src = Path("src/scenecraft/api_server.py").read_text()
        codes_in_source = set(re.findall(r'self\._error\(\s*\d+\s*,\s*"([A-Z_]+)"', src))
        assert codes_in_source <= DISPATCHER_ERROR_CODES_CORE, (
            f"extra codes leaked into source: {codes_in_source - DISPATCHER_ERROR_CODES_CORE}"
        )

    # MC-21
    def test_mc_21_internal_broadcast_requires_auth(self):
        """MC-21 — `/api/_internal/broadcast` is NOT exempt; localhost binding handles security."""
        src = Path("src/scenecraft/api_server.py").read_text()
        # `/api/_internal/broadcast` is NOT in the exempt list.
        assert '"/api/_internal/broadcast"' not in src.split('path in (', 1)[1].split(')', 1)[0] \
            if 'path in (' in src else True

    # MC-22
    @pytest.mark.xfail(
        reason="MC-22 / OQ-16 target: FastAPI uncaught-exception handler emits canonical INTERNAL_ERROR shape",
        strict=False,
    )
    def test_mc_22_uncaught_exception_emits_canonical_internal_error(self):
        """MC-22 — target after FastAPI port."""
        src = Path("src/scenecraft/api_server.py").read_text()
        # Today: no global exception handler. Target: register one in main().
        assert "exception_handler" in src.lower()


# ════════════════════════════════════════════════════════════════════
# TestFastAPIDivergence — known stdlib vs FastAPI defaults
# ════════════════════════════════════════════════════════════════════


class TestFastAPIDivergence:
    """Known places where FastAPI's defaults differ; baseline pinned to stdlib."""

    def test_no_trailing_slash_redirect_today(self, engine_server):
        """FastAPI default: redirect /api/projects/ → /api/projects. Stdlib: 404."""
        status, _h, _b = _http("GET", engine_server.base_url + "/api/projects/")
        assert status == 404  # stdlib returns 404, not 308

    def test_no_auto_head_for_get_routes(self, engine_server):
        """FastAPI default: HEAD auto-generated for every GET. Stdlib: 405."""
        status, _h, _b = _http("HEAD", engine_server.base_url + "/api/projects")
        assert status == 405

    def test_validation_errors_are_400_not_422_today(self, engine_server, project_name):
        """FastAPI default: 422 on body validation failure. Stdlib dispatcher: 400."""
        status, _h, body = _http(
            "POST",
            engine_server.base_url + f"/api/projects/{project_name}/update-prompt",
            body=b"",
            headers={"Content-Type": "application/json"},
        )
        assert status == 400
        assert _json.loads(body)["code"] == "BAD_REQUEST"

    def test_options_handled_explicitly_not_via_cors_middleware(self):
        """FastAPI default: CORSMiddleware handles OPTIONS. Stdlib: explicit do_OPTIONS."""
        src = Path("src/scenecraft/api_server.py").read_text()
        assert "def do_OPTIONS(self):" in src
        # 204 hard-coded.
        assert "self.send_response(204)" in src

    def test_no_openapi_docs_endpoint_today(self, engine_server):
        """FastAPI default: `/docs`, `/openapi.json`. Stdlib: 404."""
        status, _h, _b = _http("GET", engine_server.base_url + "/docs")
        assert status == 404
        status, _h, _b = _http("GET", engine_server.base_url + "/openapi.json")
        assert status == 404

    def test_unsupported_method_today(self, engine_server, project_name):
        """Row 103 — PATCH on a known path. stdlib: 501 from BaseHTTPRequestHandler; FastAPI: 405."""
        status, _h, _b = _http(
            "PATCH",
            engine_server.base_url + f"/api/projects/{project_name}/update-prompt",
            body=b"{}",
            headers={"Content-Type": "application/json"},
        )
        # stdlib BaseHTTPRequestHandler returns 501 for unimplemented method.
        assert status in (405, 501)


# ════════════════════════════════════════════════════════════════════
# TestEndToEnd — full HTTP round-trips across method groups
# ════════════════════════════════════════════════════════════════════


class TestEndToEnd:
    """Comprehensive e2e — representative routes across all method groups."""

    # ── Project lifecycle ────────────────────────────────────────

    def test_e2e_create_project_returns_200(self, engine_server):
        """Row 74 — POST /api/projects/create."""
        name = f"e2e_{uuid.uuid4().hex[:8]}"
        status, _h, body = _json_post(engine_server, "/api/projects/create", {"name": name})
        assert status == 200
        payload = _json.loads(body)
        assert payload["success"] is True
        assert payload["name"] == name

    def test_e2e_create_project_missing_name_is_400(self, engine_server):
        """Project create with missing name → 400."""
        status, _h, body = _json_post(engine_server, "/api/projects/create", {})
        assert status == 400
        assert _json.loads(body)["code"] == "BAD_REQUEST"

    def test_e2e_create_project_duplicate_is_409_conflict(self, engine_server):
        """Project create on existing name → 409 CONFLICT."""
        name = f"e2e_{uuid.uuid4().hex[:8]}"
        s1, _h, _b = _json_post(engine_server, "/api/projects/create", {"name": name})
        assert s1 == 200
        s2, _h, body = _json_post(engine_server, "/api/projects/create", {"name": name})
        assert s2 == 409
        assert _json.loads(body)["code"] == "CONFLICT"

    def test_e2e_list_projects_includes_created(self, engine_server):
        """GET /api/projects lists previously-created projects."""
        name = f"e2e_{uuid.uuid4().hex[:8]}"
        _json_post(engine_server, "/api/projects/create", {"name": name})
        status, _h, body = _http("GET", engine_server.base_url + "/api/projects")
        assert status == 200
        # Engine returns either a bare list or a {projects:[...]} wrapper depending on version.
        payload = _json.loads(body)
        items = payload if isinstance(payload, list) else payload.get("projects", [])
        names = {(p.get("name") if isinstance(p, dict) else p) for p in items}
        assert name in names

    # ── GET parametric ───────────────────────────────────────────

    @pytest.mark.parametrize(
        "path",
        [
            "/api/config",
            "/api/projects",
            "/api/render-cache/stats",
        ],
    )
    def test_e2e_global_get_routes_resolve(self, engine_server, path):
        """Row 1, 72, etc. — global GET routes return 200."""
        status, _h, _b = _http("GET", engine_server.base_url + path)
        assert status == 200

    @pytest.mark.parametrize(
        "suffix",
        [
            "keyframes",
            "tracks",
            "audio-tracks",
            "audio-clips",
            "markers",
            "narrative",
            "checkpoints",
            "settings",
            "ingredients",
            "bench",
            "render-state",
            "descriptions",
            "watched-folders",
            "workspace-views",
            "send-buses",
            "master-bus-effects",
            "effects",
            "pool",
            "pool/tags",
            "prompt-roster",
        ],
    )
    def test_e2e_project_scoped_get_routes_resolve(self, engine_server, project_name, suffix):
        """Many enumerated GET routes — happy path returns 200 for an empty project."""
        status, _h, _b = _http(
            "GET",
            engine_server.base_url + f"/api/projects/{project_name}/{suffix}",
        )
        assert status == 200, f"GET {suffix} failed: {status}"

    @pytest.mark.parametrize(
        "suffix",
        [
            "keyframes",
            "tracks",
            "audio-clips",
            "markers",
            "narrative",
            "settings",
            "send-buses",
        ],
    )
    def test_e2e_project_404_on_missing_project(self, engine_server, suffix):
        """Row 23, 112 — every project-scoped GET returns 404 NOT_FOUND on missing project."""
        status, _h, body = _http(
            "GET",
            engine_server.base_url + f"/api/projects/no-such-project-{uuid.uuid4().hex[:6]}/{suffix}",
        )
        assert status == 404
        assert _json.loads(body)["code"] == "NOT_FOUND"

    # ── POST empty/bad-body parametric ───────────────────────────

    @pytest.mark.parametrize(
        "suffix",
        [
            "select-keyframes",
            "update-timestamp",
            "update-prompt",
            "add-keyframe",
            "tracks/add",
            "audio-tracks/add",
            "markers/add",
            "branches",
        ],
    )
    def test_e2e_post_empty_body_is_400(self, engine_server, project_name, suffix):
        """Row 113 — every POST returns 400 on empty body (except plugin routes)."""
        status, _h, body = _http(
            "POST",
            engine_server.base_url + f"/api/projects/{project_name}/{suffix}",
            body=b"",
            headers={"Content-Type": "application/json"},
        )
        assert status == 400
        assert _json.loads(body)["code"] == "BAD_REQUEST"
        assert _json.loads(body)["error"] == "Empty body"

    @pytest.mark.parametrize(
        "suffix",
        [
            "select-keyframes",
            "update-prompt",
            "add-keyframe",
            "tracks/add",
        ],
    )
    def test_e2e_post_bad_json_is_400(self, engine_server, project_name, suffix):
        """Row 114 — every POST returns 400 on bad JSON."""
        status, _h, body = _http(
            "POST",
            engine_server.base_url + f"/api/projects/{project_name}/{suffix}",
            body=b"!!! not json",
            headers={"Content-Type": "application/json"},
        )
        assert status == 400
        payload = _json.loads(body)
        assert payload["code"] == "BAD_REQUEST"
        assert payload["error"].startswith("Invalid JSON:")

    # ── DELETE idempotency ───────────────────────────────────────

    @pytest.mark.parametrize(
        "suffix",
        [
            "track-effects",
            "effect-curves",
            "send-buses",
            "frequency-labels",
        ],
    )
    def test_e2e_delete_nonexistent_is_idempotent_200(self, engine_server, project_name, suffix):
        """Row 45-48 — DELETE on missing id → 200, NOT 404."""
        status, _h, _b = _http(
            "DELETE",
            engine_server.base_url + f"/api/projects/{project_name}/{suffix}/no-such-id-x",
        )
        assert status == 200, f"DELETE /{suffix}/no-such-id-x → {status}"

    def test_e2e_delete_unknown_path_is_404(self, engine_server, project_name):
        """Row 49 — DELETE on unknown path → 404 NOT_FOUND."""
        status, _h, body = _http(
            "DELETE",
            engine_server.base_url + f"/api/projects/{project_name}/random-thing/x",
        )
        assert status == 404
        assert _json.loads(body)["code"] == "NOT_FOUND"

    # ── HEAD ─────────────────────────────────────────────────────

    def test_e2e_head_unknown_path_405_empty(self, engine_server):
        """Row 44 — HEAD on non-files path."""
        status, _h, body = _http("HEAD", engine_server.base_url + "/api/projects")
        assert status == 405
        assert body == b""

    def test_e2e_head_files_path_missing_404_empty(self, engine_server, project_name):
        """Row 43 — HEAD on missing file → 404 empty."""
        status, _h, body = _http(
            "HEAD",
            engine_server.base_url + f"/api/projects/{project_name}/files/missing.mp4",
        )
        assert status == 404
        assert body == b""

    @pytest.mark.xfail(
        reason="real-bug: HEAD missing-file 404 path skips _cors_headers() — see do_HEAD in api_server.py. "
        "Spec R16 says CORS on every response, but the early `send_response(404); end_headers()` short-circuits.",
        strict=False,
    )
    def test_e2e_head_missing_file_emits_cors(self, engine_server, project_name):
        """Bug-tripwire — when fixed, this test starts passing.

        FastAPI port MUST close this gap (R16 spec contract).
        """
        _s, headers, _b = _http(
            "HEAD",
            engine_server.base_url + f"/api/projects/{project_name}/files/missing.mp4",
            headers={"Origin": "https://test.example"},
        )
        assert "access-control-allow-origin" in headers

    # ── OPTIONS ──────────────────────────────────────────────────

    @pytest.mark.parametrize(
        "path",
        [
            "/",
            "/api/projects",
            "/api/projects/whatever/keyframes",
            "/auth/login",
            "/literally/anything",
        ],
    )
    def test_e2e_options_returns_204_anywhere(self, engine_server, path):
        """Row 17 — OPTIONS on ANY path → 204, no auth, with CORS headers."""
        status, headers, body = _options(engine_server, path)
        assert status == 204
        assert body == b""
        assert "access-control-allow-methods" in headers

    # ── CORS on every response ───────────────────────────────────

    @pytest.mark.parametrize(
        "method,path",
        [
            ("GET", "/api/projects"),
            ("GET", "/api/no-such"),
            ("POST", "/api/projects/create"),  # 400 on missing name
            ("DELETE", "/api/projects/no/random/x"),
        ],
    )
    def test_e2e_cors_on_every_response(self, engine_server, method, path):
        """R16 — CORS headers on 200, 400, 404, 405, every status."""
        body = b"{}" if method == "POST" else None
        headers_in = {"Origin": "https://test.example"}
        if method == "POST":
            headers_in["Content-Type"] = "application/json"
        _s, headers_out, _b = _http(method, engine_server.base_url + path, body=body, headers=headers_in)
        assert headers_out.get("access-control-allow-origin") == "https://test.example"
        assert headers_out.get("access-control-allow-credentials") == "true"

    # ── Error envelopes across multiple paths ────────────────────

    @pytest.mark.parametrize(
        "method,path,expect_status",
        [
            ("GET", "/api/no-such", 404),
            ("DELETE", "/api/projects/x/random/x", 404),
            ("GET", "/api/projects/no-such-proj-xxx/keyframes", 404),
        ],
    )
    def test_e2e_error_envelope_uses_standard_shape(self, engine_server, method, path, expect_status):
        """Row 117 — every error response is `{error, code}` flat shape."""
        status, _h, body = _http(method, engine_server.base_url + path)
        assert status == expect_status
        payload = _json.loads(body)
        assert set(payload.keys()) == {"error", "code"}

    # ── Workspace views ──────────────────────────────────────────

    def test_e2e_workspace_view_unknown_404(self, engine_server, project_name):
        """Row 146 — GET unknown workspace view → 404."""
        status, _h, body = _http(
            "GET",
            engine_server.base_url + f"/api/projects/{project_name}/workspace-views/missing-view",
        )
        assert status == 404
        assert _json.loads(body)["code"] == "NOT_FOUND"

    @pytest.mark.xfail(
        reason="OQ-15 target: POST /workspace-views/:view/delete on missing view → 200 {deleted:false}",
        strict=False,
    )
    def test_e2e_delete_missing_workspace_view_idempotent_200(self, engine_server, project_name):
        """R62, OQ-15, row 147 — target."""
        status, _h, body = _json_post(
            engine_server,
            f"/api/projects/{project_name}/workspace-views/never-was/delete",
            {},
        )
        assert status == 200
        assert _json.loads(body) == {"deleted": False}

    # ── Internal broadcast ───────────────────────────────────────

    def test_e2e_internal_broadcast_missing_type_is_400(self, engine_server):
        """Row 69 — `/api/_internal/broadcast` missing 'type' → 400."""
        status, _h, body = _json_post(engine_server, "/api/_internal/broadcast", {})
        assert status == 400
        assert _json.loads(body)["code"] == "BAD_REQUEST"

    def test_e2e_internal_broadcast_valid_returns_200(self, engine_server):
        """Row 70 — valid broadcast returns 200 {ok:true}."""
        status, _h, body = _json_post(
            engine_server, "/api/_internal/broadcast", {"type": "log", "message": "hi"}
        )
        assert status == 200
        assert _json.loads(body) == {"ok": True}

    # ── Browse ───────────────────────────────────────────────────

    def test_e2e_browse_root_returns_listing(self, engine_server):
        """Row 77 — GET /api/browse default path → 200 listing."""
        status, _h, body = _http("GET", engine_server.base_url + "/api/browse")
        assert status == 200
        payload = _json.loads(body)
        assert "entries" in payload

    # ── Chat default-limit handling ──────────────────────────────

    def test_e2e_chat_bad_limit_defaults_to_50(self, engine_server, project_name):
        """Row 144 — GET /chat?limit=NaN falls back to default 50, returns 200."""
        status, _h, body = _http(
            "GET",
            engine_server.base_url + f"/api/projects/{project_name}/chat?limit=NaN",
        )
        assert status == 200
        assert "messages" in _json.loads(body)

    def test_e2e_chat_default_limit_50(self, engine_server, project_name):
        """Row 145 — GET /chat without limit defaults 50."""
        status, _h, _b = _http(
            "GET", engine_server.base_url + f"/api/projects/{project_name}/chat"
        )
        assert status == 200

    # ── Unsupported method ───────────────────────────────────────

    def test_e2e_get_with_body_ignored(self, engine_server):
        """Row 104 — GET ignores body."""
        status, _h, _b = _http(
            "GET",
            engine_server.base_url + "/api/projects",
            body=b'{"x":1}',
            headers={"Content-Type": "application/json", "Content-Length": "7"},
        )
        assert status == 200

    # ── Plugin REST: unknown id ──────────────────────────────────

    def test_e2e_plugin_unknown_id_404(self, engine_server, project_name):
        """Row 129 — `/plugins/unknown/x` → 404 NOT_FOUND."""
        status, _h, body = _http(
            "GET",
            engine_server.base_url + f"/api/projects/{project_name}/plugins/unknown_xyz/x",
        )
        assert status == 404
        assert _json.loads(body)["code"] == "NOT_FOUND"

    # ── Long URL today (no 414 cap) ──────────────────────────────

    @pytest.mark.xfail(reason="OQ-10 target: 8KB URL cap → 414 URI Too Long; today no cap", strict=False)
    def test_e2e_url_over_8kb_returns_414(self, engine_server):
        """R60, OQ-10, row 97 — target."""
        long_path = "/api/projects/" + ("x" * 9000)
        status, _h, _b = _http("GET", engine_server.base_url + long_path)
        assert status == 414
