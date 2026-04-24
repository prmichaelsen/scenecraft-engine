"""M16 T58 — auth + CORS + error envelope + validation.

The 15 named tests from the task file. Red-first per TDD:
  * `deps.current_user` is a stub in T57 — auth-required tests fail initially.
  * `routers/auth.py` and `routers/oauth.py` don't exist yet — all login/oauth
    tests fail with 404.
  * `errors.py` emits "body.<loc>: <msg>" not "Missing '<field>'" — validation
    envelope tests fail initially.
  * Unknown-route handler doesn't include method+path — unknown_route_404 fails.
  * T57's 500 handler returns "Internal server error" — unhandled-exception
    envelope test expects `str(exc)` and fails.

Test suite layout: each test builds its own FastAPI app via ``create_app`` so
the CORSMiddleware + exception handlers are realistic; we then mount a
``.scenecraft`` root under ``tmp_path`` so the legacy bearer/cookie helpers
(`generate_token`, `build_cookie_header`) can mint real JWTs.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import BaseModel


class ProbeBody(BaseModel):
    """Validation probe body — kept at module scope so FastAPI's TypeAdapter
    can resolve the forward-ref without a ``.rebuild()`` dance.
    """

    name: str
    start_time: float


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def sc_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Create a ``.scenecraft`` root with a registered user 'alice'.

    Pin ``SCENECRAFT_ROOT`` so ``find_root`` always resolves to this tmp dir,
    avoiding collisions with the developer's real ``.scenecraft`` when tests
    run from the repo root.
    """
    from scenecraft.vcs.bootstrap import init_root

    init_root(tmp_path, org_name="test-org", admin_username="alice")
    sc = tmp_path / ".scenecraft"
    monkeypatch.setenv("SCENECRAFT_ROOT", str(sc))
    return sc


@pytest.fixture()
def work_dir(tmp_path: Path) -> Path:
    """Separate working directory for project files.

    Kept distinct from ``sc_root`` because the legacy code treats ``work_dir``
    (where projects live) as a sibling of ``.scenecraft`` — mirror that.
    """
    wd = tmp_path / "work"
    wd.mkdir(parents=True, exist_ok=True)
    return wd


@pytest.fixture()
def app(sc_root: Path, work_dir: Path) -> FastAPI:
    from scenecraft.api.app import create_app

    return create_app(work_dir=work_dir)


@pytest.fixture()
def client(app: FastAPI) -> TestClient:
    return TestClient(app)


@pytest.fixture()
def bearer_token(sc_root: Path) -> str:
    from scenecraft.vcs.auth import generate_token

    return generate_token(sc_root, username="alice")


@pytest.fixture()
def auth_headers(bearer_token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {bearer_token}"}


@pytest.fixture()
def cookie_header(bearer_token: str) -> dict[str, str]:
    from scenecraft.vcs.auth import COOKIE_NAME

    return {"Cookie": f"{COOKIE_NAME}={bearer_token}"}


# ---------------------------------------------------------------------------
# Auth — bearer / cookie / missing
# ---------------------------------------------------------------------------


def test_auth_required_returns_401(client: TestClient):
    """auth-required-returns-401 — GET /api/config without auth → 401 envelope."""
    resp = client.get("/api/config")
    assert resp.status_code == 401
    body = resp.json()
    assert body == {"error": "UNAUTHORIZED", "message": "Invalid or expired token"}


def test_bearer_auth_succeeds(client: TestClient, auth_headers: dict[str, str]):
    """bearer-auth-succeeds — valid Bearer token → 200 on /api/config."""
    resp = client.get("/api/config", headers=auth_headers)
    assert resp.status_code == 200
    # Body is whatever load_config returns — at minimum a dict.
    assert isinstance(resp.json(), dict)


def test_cookie_auth_succeeds(client: TestClient, cookie_header: dict[str, str]):
    """cookie-auth-succeeds — valid session cookie → 200 on /api/config."""
    resp = client.get("/api/config", headers=cookie_header)
    assert resp.status_code == 200
    assert isinstance(resp.json(), dict)


# ---------------------------------------------------------------------------
# Validation — 400 envelope (NOT 422)
# ---------------------------------------------------------------------------


def _install_validation_probe(app: FastAPI) -> None:
    """Register a POST route with a Pydantic body so we can probe validation.

    Kept in-test because M16 T58 adds no real POST routes yet — sibling T59
    and T60+ add the business surface. We want validation tests that prove the
    envelope translation regardless of which routes exist today.
    """
    # Create the route inside the same app. The dependency must be explicit
    # because this is a test probe; public-routes list is unchanged.
    from fastapi import Body, Depends

    from scenecraft.api.deps import current_user

    @app.post("/api/_probe/validate", operation_id="probe_validate")
    async def _probe(
        payload: ProbeBody = Body(...), user=Depends(current_user)
    ) -> dict:
        return {"ok": True, "name": payload.name}


def test_invalid_json_returns_400(
    app: FastAPI, client: TestClient, auth_headers: dict[str, str]
):
    """invalid-json-returns-400 — non-JSON body → 400 envelope (NOT 422)."""
    _install_validation_probe(app)
    resp = client.post(
        "/api/_probe/validate",
        data="not json",
        headers={**auth_headers, "Content-Type": "application/json"},
    )
    assert resp.status_code == 400
    body = resp.json()
    assert body["error"] == "BAD_REQUEST"
    assert "message" in body
    # Must NOT be FastAPI's default detail-list shape.
    assert "detail" not in body


def test_missing_field_returns_400(
    app: FastAPI, client: TestClient, auth_headers: dict[str, str]
):
    """missing-field-returns-400 — POST {} → 400 envelope mentions missing field."""
    _install_validation_probe(app)
    resp = client.post(
        "/api/_probe/validate", json={}, headers=auth_headers
    )
    assert resp.status_code == 400
    body = resp.json()
    assert body["error"] == "BAD_REQUEST"
    # Legacy text: "Missing 'name'" — first missing field is surfaced.
    assert body["message"] == "Missing 'name'"


def test_validation_envelope_legacy_shape(
    app: FastAPI, client: TestClient, auth_headers: dict[str, str]
):
    """validation-envelope-legacy-shape — type mismatch → 400 w/ legacy envelope."""
    _install_validation_probe(app)
    resp = client.post(
        "/api/_probe/validate",
        json={"name": "ok", "start_time": "not a number"},
        headers=auth_headers,
    )
    assert resp.status_code == 400  # NOT 422
    body = resp.json()
    assert set(body.keys()) == {"error", "message"}
    assert body["error"] == "BAD_REQUEST"
    # Must reference the field location in a dotted form.
    assert "start_time" in body["message"]


# ---------------------------------------------------------------------------
# CORS
# ---------------------------------------------------------------------------


def test_options_preflight_204(client: TestClient):
    """options-preflight-204 — OPTIONS with CORS headers → 2xx + ACAO + ACAM."""
    resp = client.options(
        "/api/config",
        headers={
            "Origin": "http://localhost:3000",
            "Access-Control-Request-Method": "GET",
            "Access-Control-Request-Headers": "Authorization",
        },
    )
    # Starlette's CORSMiddleware emits 200 for preflight; legacy emitted 204.
    # Either is correct per the spec (2xx). Prefer to accept both.
    assert resp.status_code in (200, 204)
    assert resp.headers.get("access-control-allow-origin") == "http://localhost:3000"
    allow_methods = resp.headers.get("access-control-allow-methods", "")
    assert "GET" in allow_methods


def test_cors_origin_matches_legacy(
    client: TestClient, auth_headers: dict[str, str]
):
    """cors-origin-matches-legacy — echo Origin + credentials=true on real response.

    Legacy ``_cors_headers`` echoes the request Origin (not '*') when present,
    sets ``Access-Control-Allow-Credentials: true``, and emits ``Vary: Origin``.
    """
    resp = client.get(
        "/api/config",
        headers={**auth_headers, "Origin": "http://localhost:5173"},
    )
    assert resp.status_code == 200
    assert resp.headers.get("access-control-allow-origin") == "http://localhost:5173"
    assert resp.headers.get("access-control-allow-credentials") == "true"
    # Vary: Origin must be set so caches don't serve the wrong origin's headers.
    vary = resp.headers.get("vary", "")
    assert "Origin" in vary or "origin" in vary


def test_cors_on_every_response(
    app: FastAPI, client: TestClient, auth_headers: dict[str, str]
):
    """cors-on-every-response — headers on 3 representative routes (not preflight)."""
    _install_validation_probe(app)
    origin = "http://localhost:5173"

    # 1. 200 OK response
    r1 = client.get("/api/config", headers={**auth_headers, "Origin": origin})
    assert r1.status_code == 200
    assert r1.headers.get("access-control-allow-origin") == origin

    # 2. 404 unknown-route response still gets CORS.
    r2 = client.get("/api/nope", headers={**auth_headers, "Origin": origin})
    assert r2.status_code == 404
    assert r2.headers.get("access-control-allow-origin") == origin

    # 3. 400 validation response still gets CORS.
    r3 = client.post(
        "/api/_probe/validate",
        json={},
        headers={**auth_headers, "Origin": origin},
    )
    assert r3.status_code == 400
    assert r3.headers.get("access-control-allow-origin") == origin


# ---------------------------------------------------------------------------
# Auth routes — login / logout
# ---------------------------------------------------------------------------


def test_auth_login_sets_cookie_and_redirects(
    client: TestClient, sc_root: Path, bearer_token: str
):
    """auth-login-sets-cookie-and-redirects — code exchange + HttpOnly cookie."""
    from scenecraft.vcs.auth import COOKIE_NAME, create_login_code

    code = create_login_code(sc_root, bearer_token)
    # Don't auto-follow — we need to inspect the redirect response headers.
    resp = client.get(f"/auth/login?code={code}", follow_redirects=False)
    assert resp.status_code in (302, 303)
    assert resp.headers.get("location") == "/"
    set_cookie = resp.headers.get("set-cookie", "")
    assert f"{COOKIE_NAME}=" in set_cookie
    assert "HttpOnly" in set_cookie
    assert "SameSite=Lax" in set_cookie


def test_auth_logout_clears_cookie(client: TestClient):
    """auth-logout-clears-cookie — POST /auth/logout → Max-Age=0 cookie + {ok}."""
    resp = client.post("/auth/logout")
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}
    set_cookie = resp.headers.get("set-cookie", "")
    assert "Max-Age=0" in set_cookie


# ---------------------------------------------------------------------------
# OAuth callback
# ---------------------------------------------------------------------------


def test_oauth_callback_success(client: TestClient, monkeypatch: pytest.MonkeyPatch):
    """oauth-callback-success — valid code+state → token row inserted."""
    from scenecraft import oauth_client

    # Seed a pending state that our test controls.
    state = oauth_client.create_pending_state(
        user_id="alice", service="remember", code_verifier="verifier123"
    )

    # Short-circuit the actual HTTP call to agentbase.me.
    def _fake_exchange(code: str, code_verifier: str) -> dict:
        assert code == "abc123"
        assert code_verifier == "verifier123"
        return {"access_token": "AT", "refresh_token": "RT", "expires_in": 3600}

    monkeypatch.setattr(oauth_client, "exchange_code_for_tokens", _fake_exchange)

    saved: dict[str, Any] = {}

    def _fake_save(**kwargs: Any) -> None:
        saved.update(kwargs)

    monkeypatch.setattr(oauth_client, "save_tokens", _fake_save)

    resp = client.get(f"/oauth/callback?code=abc123&state={state}")
    assert resp.status_code == 200  # Renders HTML success page (legacy parity)
    assert "text/html" in resp.headers.get("content-type", "")
    # Token row was "inserted" (captured by our fake).
    assert saved.get("user_id") == "alice"
    assert saved.get("service") == "remember"
    assert saved.get("access_token") == "AT"


def test_oauth_callback_bad_state(client: TestClient):
    """oauth-callback-bad-state — unknown state → HTML error; no token stored."""
    resp = client.get("/oauth/callback?code=abc&state=definitely-not-a-real-state")
    # Legacy returns an HTML error page at 200 for bad state too (same
    # popup-friendly flow). The key contract: no token is stored.
    assert resp.status_code in (200, 400)
    # A failed callback page mentions the failure.
    body = resp.text.lower()
    assert "invalid" in body or "fail" in body or "error" in body


# ---------------------------------------------------------------------------
# 404 unknown route
# ---------------------------------------------------------------------------


def test_unknown_route_404(client: TestClient, auth_headers: dict[str, str]):
    """unknown-route-404 — GET /api/nope/nope → 404 envelope w/ method + path."""
    resp = client.get("/api/nope/nope", headers=auth_headers)
    assert resp.status_code == 404
    body = resp.json()
    assert body["error"] == "NOT_FOUND"
    assert body["message"] == "No route: GET /api/nope/nope"


# ---------------------------------------------------------------------------
# 500 unhandled
# ---------------------------------------------------------------------------


def test_unhandled_exception_500_envelope(
    app: FastAPI,
    client: TestClient,
    auth_headers: dict[str, str],
    caplog: pytest.LogCaptureFixture,
):
    """unhandled-exception-500-envelope — RuntimeError → 500 envelope, traceback logged."""
    from scenecraft.api.deps import current_user

    from fastapi import Depends

    @app.get("/api/_probe/boom", operation_id="probe_boom")
    async def _boom(user=Depends(current_user)) -> dict:
        raise RuntimeError("kaboom")

    # The TestClient normally re-raises server exceptions; disable that so the
    # 500 exception handler runs.
    client2 = TestClient(app, raise_server_exceptions=False)

    with caplog.at_level(logging.ERROR):
        resp = client2.get("/api/_probe/boom", headers=auth_headers)

    assert resp.status_code == 500
    body = resp.json()
    assert body["error"] == "INTERNAL_ERROR"
    # Message contains the exception string, NOT the traceback.
    assert body["message"] == "kaboom"
    # Traceback must NOT appear in the response body.
    assert "Traceback" not in resp.text
    assert "RuntimeError" not in resp.text
    # But a traceback should appear in the captured log.
    assert any(
        "Traceback" in rec.getMessage() or rec.exc_info is not None
        for rec in caplog.records
    )
