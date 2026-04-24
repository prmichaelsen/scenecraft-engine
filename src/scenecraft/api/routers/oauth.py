"""OAuth router — mirrors legacy ``_handle_oauth_*`` handlers.

Four routes (spec R16):
  * ``GET /api/oauth/{service}/authorize`` — start the flow; auth-gated.
  * ``GET /api/oauth/{service}/status`` — connection state; auth-gated.
  * ``POST /api/oauth/{service}/disconnect`` — delete stored tokens; auth-gated.
  * ``GET /oauth/callback`` — agentbase redirect target; **public** (the browser
    arrives here unauthenticated after the user clicks "Allow" on the consent
    page, so this endpoint has no cookie / bearer yet).

The callback renders a small HTML page that ``postMessage``s the result to the
opener and auto-closes. Mirrors ``_send_callback_html`` byte-for-byte because
several frontends already rely on the exact ``type: 'scenecraft-oauth-callback'``
message format.
"""

from __future__ import annotations

import html as _html
import json

from fastapi import APIRouter, Depends, Request, status
from fastapi.responses import HTMLResponse, JSONResponse

from scenecraft.api.deps import User, current_user
from scenecraft.api.errors import ApiError

# Two routers so we can apply auth selectively: the ``/api/oauth/...`` surface
# requires ``current_user`` but the ``/oauth/callback`` handler must be public.
router = APIRouter(
    prefix="/api/oauth", tags=["oauth"], dependencies=[Depends(current_user)]
)
callback_router = APIRouter(tags=["oauth"])


@router.get(
    "/{service}/authorize",
    operation_id="oauth_authorize",
    summary="Begin an OAuth authorization flow for a third-party service",
)
async def oauth_authorize(service: str, user: User = Depends(current_user)) -> dict:
    from scenecraft.oauth_client import (
        SERVICES,
        build_authorize_url,
        create_pending_state,
        generate_pkce_pair,
    )

    if service not in SERVICES:
        raise ApiError(
            "UNKNOWN_SERVICE",
            f"No OAuth service: {service}",
            status_code=status.HTTP_404_NOT_FOUND,
        )
    verifier, challenge = generate_pkce_pair()
    state = create_pending_state(user_id=user.id, service=service, code_verifier=verifier)
    url = build_authorize_url(service, state, challenge)
    return {"url": url, "state": state}


@router.get(
    "/{service}/status",
    operation_id="oauth_status",
    summary="Check whether the current user is connected to an OAuth service",
)
async def oauth_status(service: str, user: User = Depends(current_user)) -> dict:
    from scenecraft.oauth_client import SERVICES, load_tokens

    if service not in SERVICES:
        raise ApiError(
            "UNKNOWN_SERVICE",
            f"No OAuth service: {service}",
            status_code=status.HTTP_404_NOT_FOUND,
        )
    tokens = load_tokens(user.id, service)
    if tokens is None:
        return {"connected": False}
    return {
        "connected": True,
        "expires_at": tokens.expires_at.isoformat(),
        "has_refresh_token": bool(tokens.refresh_token),
        "created_at": tokens.created_at.isoformat(),
        "updated_at": tokens.updated_at.isoformat(),
    }


@router.post(
    "/{service}/disconnect",
    operation_id="oauth_disconnect",
    summary="Delete stored OAuth tokens for the current user + service",
)
async def oauth_disconnect(service: str, user: User = Depends(current_user)) -> dict:
    from scenecraft.oauth_client import SERVICES, delete_tokens

    if service not in SERVICES:
        raise ApiError(
            "UNKNOWN_SERVICE",
            f"No OAuth service: {service}",
            status_code=status.HTTP_404_NOT_FOUND,
        )
    deleted = delete_tokens(user.id, service)
    return {"disconnected": deleted}


# ---------------------------------------------------------------------------
# Public callback — no auth dep
# ---------------------------------------------------------------------------


def _render_callback_html(*, success: bool, message: str, service: str = "") -> str:
    """Render the OAuth popup callback HTML (legacy parity)."""
    status_color = "#10b981" if success else "#ef4444"
    title = "Connected" if success else "Connection Failed"
    icon = "✓" if success else "✗"
    safe_msg = _html.escape(message or "")
    safe_service = _html.escape(service or "")
    return f"""<!doctype html>
<html>
<head>
  <meta charset="utf-8"/>
  <title>{title}</title>
  <style>
    body {{ font-family: system-ui, sans-serif; background: #0f172a; color: #e2e8f0;
            display: flex; align-items: center; justify-content: center; height: 100vh; margin: 0; }}
    .card {{ text-align: center; padding: 2rem 3rem; background: #1e293b; border-radius: 12px;
             border: 1px solid #334155; max-width: 420px; }}
    .icon {{ font-size: 3rem; color: {status_color}; line-height: 1; margin-bottom: 0.5rem; }}
    h1 {{ font-size: 1.25rem; margin: 0 0 0.5rem 0; }}
    p {{ color: #94a3b8; margin: 0; font-size: 0.9rem; line-height: 1.4; }}
    .hint {{ margin-top: 1rem; font-size: 0.8rem; color: #64748b; }}
  </style>
</head>
<body>
  <div class="card">
    <div class="icon">{icon}</div>
    <h1>{title}</h1>
    <p>{safe_msg}</p>
    <p class="hint">This window will close automatically.</p>
  </div>
  <script>
    try {{
      if (window.opener) {{
        window.opener.postMessage({{
          type: 'scenecraft-oauth-callback',
          success: {str(success).lower()},
          service: {json.dumps(safe_service)},
          message: {json.dumps(safe_msg)},
        }}, '*');
      }}
    }} catch (e) {{}}
    setTimeout(() => {{ try {{ window.close(); }} catch (e) {{}} }}, 1500);
  </script>
</body>
</html>"""


@callback_router.get(
    "/oauth/callback",
    operation_id="oauth_callback",
    summary="OAuth authorization-code callback — exchanges code for tokens",
    include_in_schema=True,
)
async def oauth_callback(
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
    error_description: str | None = None,
) -> HTMLResponse:
    """Complete the OAuth flow. Public by design.

    The browser arrives here without auth after the user authorizes at the
    third party, so this route can't require a cookie or bearer. We still
    validate the ``state`` parameter to bind the callback to the original
    request that created the pending flow — that's the actual auth for this
    endpoint (the state is a 32-byte cryptographic nonce).
    """
    from scenecraft.oauth_client import (
        TokenExchangeError,
        consume_pending_state,
        exchange_code_for_tokens,
        save_tokens,
    )

    if error:
        return HTMLResponse(
            _render_callback_html(success=False, message=error_description or error)
        )
    if not code or not state:
        return HTMLResponse(
            _render_callback_html(success=False, message="Missing code or state")
        )

    pending = consume_pending_state(state)
    if not pending:
        return HTMLResponse(
            _render_callback_html(success=False, message="Invalid or expired state")
        )

    try:
        result = exchange_code_for_tokens(code, pending["code_verifier"])
    except TokenExchangeError as exc:
        msg = (
            exc.body.get("error_description")
            or exc.body.get("error")
            or "Token exchange failed"
        )
        return HTMLResponse(_render_callback_html(success=False, message=msg))

    save_tokens(
        user_id=pending["user_id"],
        service=pending["service"],
        access_token=result["access_token"],
        refresh_token=result.get("refresh_token"),
        expires_in=int(result.get("expires_in", 3600)),
    )
    return HTMLResponse(
        _render_callback_html(
            success=True,
            message=f"Connected {pending['service']}",
            service=pending["service"],
        )
    )


__all__ = ["router", "callback_router"]
