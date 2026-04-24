"""Dependencies + middleware installers for the FastAPI app (M16 T57).

Scope for this spike:
  - ``install_cors(app)`` — matches ``api_server.py::_cors_headers`` semantics
    (echo Origin when present + ``Allow-Credentials: true``; fall back to ``*``).
  - Placeholder ``current_user`` — T58 lands the real bearer+cookie check.

Spec R49/R50 requires CORS parity with the legacy server. Legacy behavior:
  * ``Access-Control-Allow-Origin`` = request Origin if present, else ``*``
  * ``Access-Control-Allow-Credentials`` = ``true`` (when Origin present)
  * ``Vary: Origin`` (when Origin present)
  * ``Access-Control-Allow-Methods`` = ``GET, POST, DELETE, OPTIONS``
  * ``Access-Control-Allow-Headers`` = ``Content-Type, Authorization, X-Scenecraft-Branch``

FastAPI ``CORSMiddleware`` with ``allow_origin_regex=".*"`` +
``allow_credentials=True`` replicates this: the middleware echoes the
request Origin when present (emitting ``Vary: Origin``) and the regex
ensures it matches every origin.
"""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware


# Match legacy api_server.py::_cors_headers verbatim.
_ALLOW_METHODS = ["GET", "POST", "DELETE", "OPTIONS"]
_ALLOW_HEADERS = ["Content-Type", "Authorization", "X-Scenecraft-Branch"]


def install_cors(app: FastAPI) -> None:
    """Attach CORSMiddleware with legacy-equivalent configuration (R49, R50)."""
    app.add_middleware(
        CORSMiddleware,
        # Regex lets the middleware echo any origin, matching the legacy
        # "origin if present else *" behavior. allow_credentials=True then
        # causes the middleware to set Allow-Credentials: true only when
        # an origin was actually echoed.
        allow_origin_regex=".*",
        allow_credentials=True,
        allow_methods=_ALLOW_METHODS,
        allow_headers=_ALLOW_HEADERS,
    )


# ---------------------------------------------------------------------------
# Auth stub — real implementation lands in T58 (spec R13-R17).
# ---------------------------------------------------------------------------


class User:
    """Stub user object. Replaced by ``scenecraft.vcs.auth.User`` in T58."""

    def __init__(self, user_id: str = "anon") -> None:
        self.id = user_id


async def current_user(request: Request) -> User:
    """TODO: T58 — replace with real bearer-token + cookie authentication.

    For this spike, every request is treated as authenticated so the spike
    routes (``GET /api/config``, ``GET/HEAD /api/projects/{name}/files/...``)
    can be exercised without a login handshake.
    """
    return User()
