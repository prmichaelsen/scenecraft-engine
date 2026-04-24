"""Plugin catch-all router (M16 T64).

Plugins register HTTP handlers at runtime via
``scenecraft.plugin_api.register_rest_endpoint(path_regex, handler)``.
Handlers are keyed by a **regex** against the full path, so the only
sensible way to mount them in FastAPI is a single catch-all route that
defers matching to ``PluginHost.dispatch_rest``. Registering each
plugin route as a real FastAPI route would require:

  1. Converting the plugin's regex to a FastAPI path-template.
     Plugin regexes are arbitrary — not expressible as templates.
  2. Re-registering routes every time a plugin is activated/deactivated.
     FastAPI's ``APIRouter`` assumes static route tables; rebuilding the
     route tree at runtime is explicitly unsupported.

Legacy ``api_server.py::do_POST`` used the same approach — a single
``re.match(r"^/api/projects/([^/]+)/plugins/[^/]+/", path)`` fallback
after every built-in ``re.match`` probe fails. We preserve that shape
exactly so plugin handlers that rely on the legacy call signature
(``handler(path, project_dir, project_name, body)``) work unmodified.

Registration order in ``app.py``:

    app.include_router(<builtin>)  # everything first
    app.include_router(plugins.router)  # catch-all LAST

Because every built-in route declares a specific prefix or literal path,
FastAPI's matcher picks the most-specific route; the catch-all only
runs when nothing else matched. No path-regex collision with built-ins
is possible — the catch-all is scoped under ``/plugins/`` so even a
pathologically greedy plugin regex can only capture plugin-prefixed
URLs.

Dispatch edge cases:

  * **Empty body**: legacy ``_read_json_body`` returns ``{}`` for an
    empty payload. We replicate with ``await request.json()`` guarded
    by the ``Content-Length`` header — a length-zero or absent header
    means no body, and we pass ``{}`` to keep plugins that assume a
    dict from blowing up on ``NoneType``.
  * **Method**: legacy only dispatched POST. Plugins that want to
    expose GET endpoints would need a second entry; none ship today,
    so we match legacy and keep this POST-only.
  * **Headers**: plugin handlers receive the parsed body, not the raw
    request — they can't read ``Authorization``, ``X-Scenecraft-Branch``,
    or cookies. Matches legacy exactly (``_read_json_body`` never passed
    headers to plugin handlers). Plugins that need headers should
    graduate to a real FastAPI route in their own router module.
  * **dispatch_rest returns None**: means "no plugin regex matched".
    Legacy emitted a generic ``NOT_FOUND`` envelope at that point — we
    do the same.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, Request, status

from scenecraft.api.deps import current_user, project_dir
from scenecraft.api.errors import ApiError

router = APIRouter(
    prefix="/api/projects",
    tags=["plugins"],
    dependencies=[Depends(current_user)],
)


@router.post(
    "/{name}/plugins/{plugin}/{rest:path}",
    operation_id="plugin_dispatch",
    summary="Dispatch a plugin-registered POST route",
    include_in_schema=False,
)
async def plugin_dispatch(
    name: str,
    plugin: str,
    rest: str,
    request: Request,
    pd: Path = Depends(project_dir),
) -> dict:
    """Hand the request off to the plugin registry.

    Call signature matches legacy exactly::

        PluginHost.dispatch_rest(path, project_dir, project_name, body)

    so plugin handlers that run today under ``api_server.py`` keep
    running here unmodified during Phase A's parallel-port window
    and after the T65 cutover.
    """
    # Legacy: empty body → ``{}``. ``request.json()`` raises on an empty
    # payload, so probe the header before calling it.
    try:
        content_length = int(request.headers.get("content-length") or 0)
    except ValueError:
        content_length = 0
    body: dict = {}
    if content_length > 0:
        try:
            body = await request.json()
        except Exception:
            # Malformed JSON — treat as empty payload to match legacy
            # ``_read_json_body`` which returned ``{}`` on parse failure
            # rather than 400-ing the plugin path.
            body = {}
        if not isinstance(body, dict):
            # Legacy plugins always received a dict; if the client sent
            # a list or scalar, coerce to empty-dict so handlers don't
            # crash on ``body.get(...)``.
            body = {}

    full_path = request.url.path

    # Late import — the plugin host is a global registry but we don't
    # want to pay the import cost at app-boot when no plugins are loaded.
    from scenecraft.plugin_host import PluginHost

    try:
        result = PluginHost.dispatch_rest(full_path, pd, name, body)
    except ApiError:
        # A plugin may raise our own envelope explicitly — let it pass.
        raise
    except Exception as exc:
        # Mirror legacy: wrap arbitrary plugin exceptions in a PLUGIN_ERROR
        # envelope so the FE can tell "plugin failed" from "server bug".
        raise ApiError(
            "PLUGIN_ERROR",
            str(exc),
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        ) from exc

    if result is None:
        raise ApiError(
            "NOT_FOUND",
            f"No route: POST {full_path}",
            status_code=status.HTTP_404_NOT_FOUND,
        )
    return result


__all__ = ["router"]
