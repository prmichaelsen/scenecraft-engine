# Task 58: Auth + CORS + error envelope + validation

**Milestone**: [M16 — FastAPI Migration + Tool Codegen](../../milestones/milestone-16-fastapi-migration-and-tool-codegen.md)
**Spec**: [`local.fastapi-migration`](../../specs/local.fastapi-migration.md) — R9, R11, R13–R17, R26–R28, R48–R50
**Estimated Time**: 4–6 hours
**Dependencies**: T57
**Status**: Not Started

---

## Objective

Replace T57's stub auth with the real `current_user` dependency (bearer + cookie), wire the auth/OAuth handshake endpoints, lock in the legacy error envelope (including the critical 400-not-422 validation translation), and prove CORS matches legacy allow-origin policy byte-for-byte.

---

## TDD Plan

Write the 15 named tests below against the T57 app first. They fail (handlers don't exist). Implement `deps.current_user`, the auth router, the OAuth router, the CORS audit, and the error-handler polish until all pass. No business routes are ported in this task.

---

## Steps

### 1. `deps.py::current_user`

Full implementation:
- Read `Authorization: Bearer <token>` header first. Validate against the existing token store (mirror `api_server.py::_authenticate`'s bearer path).
- If absent, read session cookie (name and format from legacy).
- On failure, raise `HTTPException(401, detail="Invalid or expired token")` — exception handler converts to `{"error": "UNAUTHORIZED", "message": "Invalid or expired token"}`.
- Return a `User` object (same dataclass/TypedDict the legacy code uses).

Also implement `project_dir(name: str, user: User = Depends(current_user)) -> Path`:
- Resolve `work_dir / name`.
- Raise 404 `NOT_FOUND` if the project directory doesn't exist.
- Return the resolved path.

### 2. `routers/auth.py`

- `GET /auth/login?code=<one-time-code>` — `operation_id="auth_login"`, public (no auth dep).
  - Exchange code for session; set HttpOnly cookie with same attributes as legacy (`HttpOnly`, `SameSite=Lax`, `Secure` when `request.url.scheme == "https"`).
  - Return the exact redirect response legacy returns (302 with Location header, or HTML+JS per current behavior — match exactly).
- `POST /auth/logout` — `operation_id="auth_logout"`, public.
  - Set-Cookie with `Max-Age=0`; return `{"ok": true}`.

### 3. `routers/oauth.py`

- `GET /api/oauth/{service}/authorize` — `operation_id="oauth_authorize"`.
- `GET /api/oauth/{service}/status` — `operation_id="oauth_status"`.
- `POST /api/oauth/{service}/disconnect` — `operation_id="oauth_disconnect"`.
- `GET /oauth/callback?code=...&state=...` — `operation_id="oauth_callback"`, public.
  - Validate state; 400 on mismatch.
  - Exchange code, store token, redirect to app.

All four preserve legacy URL shapes and response bodies exactly.

### 4. `errors.py` — validation envelope

Install a `RequestValidationError` handler that:
- Picks the first error in `exc.errors()`.
- Emits `{"error": "BAD_REQUEST", "message": <human message>}` at status 400.
- For `missing` errors, message is `f"Missing '{field_name}'"` to match legacy text.
- For other errors, message is `f"<loc>: <msg>"` (e.g., `"body.start_time: value is not a valid number"`).

Ensure default 422 is NOT returned for any route.

### 5. `errors.py` — unknown-route handler

Register a generic 404 handler that emits `{"error": "NOT_FOUND", "message": f"No route: {request.method} {request.url.path}"}`.

### 6. `errors.py` — unhandled-exception handler

Register a generic `Exception` handler:
- Log traceback at ERROR level.
- Return 500 `{"error": "INTERNAL_ERROR", "message": str(exc)}`.

### 7. CORS audit

Read `api_server.py::_cors_headers` and match the allow-origin / allow-methods / allow-headers / allow-credentials policy **exactly**. Install via `app.add_middleware(CORSMiddleware, ...)` — not ad-hoc per-response headers.

### 8. Apply `current_user` globally (with public exceptions)

Add `dependencies=[Depends(current_user)]` to every router created from T59 onward. For this task's routers, mark public routes explicitly (`include_in_schema=True` keeps them in OpenAPI; no auth dep).

Public routes per spec R14: `GET /auth/login`, `GET /oauth/callback`, `GET /openapi.json`, `GET /docs`, `GET /redoc`.

### 9. Tests to Pass

Create `tests/test_fastapi_auth_and_errors.py`:

- `auth_required_returns_401` — GET a protected route (e.g., `/api/config`) with no auth; expect 401 and envelope `{"error": "UNAUTHORIZED", "message": "Invalid or expired token"}`.
- `bearer_auth_succeeds` — GET `/api/config` with valid bearer; 200 + expected body.
- `cookie_auth_succeeds` — GET with valid session cookie; 200.
- `invalid_json_returns_400` — POST with body `"not json"`; 400 envelope (NOT 422).
- `missing_field_returns_400` — POST with body `{}` to a route requiring `name`; 400 with message containing `name`.
- `options_preflight_204` — OPTIONS with CORS preflight headers on any route; 204 + `Access-Control-Allow-Origin` + `Access-Control-Allow-Methods`.
- `validation_envelope_legacy_shape` — a POST that triggers Pydantic type mismatch; expect 400, NOT 422; body has `error`/`message`, NOT FastAPI's `{"detail": [...]}`.
- `auth_login_sets_cookie_and_redirects` — `GET /auth/login?code=<valid>`; `Set-Cookie` has `HttpOnly`, `SameSite=Lax`; status code and Location match legacy fixture.
- `auth_logout_clears_cookie` — `POST /auth/logout`; `Set-Cookie` has `Max-Age=0`; body `{"ok": true}`.
- `oauth_callback_success` — callback with valid code + state; 302 to app; token row inserted.
- `oauth_callback_bad_state` — callback with bad state; 400 envelope; no token stored.
- `unknown_route_404` — GET `/api/nope/nope`; 404 envelope; message includes method + path.
- `unhandled_exception_500_envelope` — monkey-patch a route to raise `RuntimeError("kaboom")`; 500 envelope; traceback in log but NOT in response body.
- `cors_origin_matches_legacy` — compare `Access-Control-Allow-Origin` on a real response to the legacy value (capture legacy value first, commit as a fixture).
- `cors_on_every_response` — for 3 representative routes, verify CORS header on the actual response (not just preflight).

### 10. Cross-check: `/api/config` is now gated

T57's `/api/config` spike was open. Now it requires auth. Update the T57 scaffold test to include a bearer token.

---

## Verification

- [ ] All 15 named tests pass
- [ ] `auth_required_returns_401` fires on **every** route added in this task
- [ ] Public routes (`/auth/login`, `/oauth/callback`, `/openapi.json`, `/docs`, `/redoc`) have no `current_user` dependency
- [ ] Pydantic validation always emits 400, never 422
- [ ] CORS headers on every response match legacy byte-for-byte
- [ ] `python -c "from scenecraft.api.app import app; assert all(r.dependant is not None for r in app.routes)"` — no lingering stub
- [ ] Auth, OAuth, and OAuth callback routes' response bodies match pre-migration fixtures exactly

---

## Tests Covered

`auth-required-returns-401`, `bearer-auth-succeeds`, `cookie-auth-succeeds`, `invalid-json-returns-400`, `missing-field-returns-400`, `options-preflight-204`, `validation-envelope-legacy-shape`, `auth-login-sets-cookie-and-redirects`, `auth-logout-clears-cookie`, `oauth-callback-success`, `oauth-callback-bad-state`, `unknown-route-404`, `unhandled-exception-500-envelope`, `cors-origin-matches-legacy`, `cors-on-every-response`.

---

## Notes

- Cookie attributes (`Secure`, `SameSite`) are environment-dependent. Preserve whatever legacy sets in dev vs prod — the parity target is the exact string produced by legacy for the same request.
- `python-multipart` is already in deps from T57 — no new deps here.
- Keep the public-route list in one place (`deps.PUBLIC_ROUTES` set or similar) so future routers don't accidentally gate them.
