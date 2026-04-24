"""Narrow host API surface for scenecraft plugins.

Plugins MUST import from this module rather than scenecraft internals. When the
dynamic plugin loader lands, this surface becomes the stable public API.

This is intentionally a thin re-export + a couple of plugin-specific helpers. It
does NOT wrap existing APIs with new semantics; additions here are deliberate
surface expansions.

Per spec R9a (core-invariant): this module MUST NOT export any raw DB
connection or cursor. The set of exported core-table write helpers is the
authoritative allowlist of what plugins may write to core schema.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

# --- DB helpers ------------------------------------------------------------
# Plugins go through these named re-exports only; direct imports of
# scenecraft.db are considered off-surface.
from scenecraft.db import (
    get_audio_clips,
    add_pool_segment,
    get_pool_segment,
    add_audio_candidate,
    assign_audio_candidate,
    get_audio_clip_effective_path,
    add_audio_isolation,
    update_audio_isolation_status,
    add_isolation_stem,
    get_isolations_for_entity,
    get_isolation_stems,
    undo_begin,
    # M16 music generation
    add_music_generation,
    update_music_generation_status,
    add_generation_track,
    get_music_generation,
    get_music_generations_for_entity,
    get_music_generation_tracks,
    set_pool_segment_context,
    # Shared candidate-junction helper (tr-side) — needed by M16 + future plugins
    add_tr_candidate,
)

# --- Job infrastructure ---------------------------------------------------
from scenecraft.ws_server import job_manager

# --- Spend ledger (core, server.db) ---------------------------------------
from scenecraft.vcs.bootstrap import (
    record_spend as _record_spend_raw,
    list_spend,
    find_root,
)


__all__ = [
    "get_audio_clips",
    "add_pool_segment",
    "get_pool_segment",
    "add_audio_candidate",
    "assign_audio_candidate",
    "get_audio_clip_effective_path",
    "add_audio_isolation",
    "update_audio_isolation_status",
    "add_isolation_stem",
    "get_isolations_for_entity",
    "get_isolation_stems",
    "undo_begin",
    "job_manager",
    "extract_audio_as_wav",
    "register_rest_endpoint",
    "make_disposable",
    # M16 music generation
    "add_music_generation",
    "update_music_generation_status",
    "add_generation_track",
    "get_music_generation",
    "get_music_generations_for_entity",
    "get_music_generation_tracks",
    "set_pool_segment_context",
    "add_tr_candidate",
    "record_spend",
    "list_spend",
    "call_service",
    "ServiceResponse",
    "ServiceError",
    "ServiceConfigError",
    "ServiceTimeoutError",
]

# Re-export the disposable factory so plugins can adapt arbitrary teardown
# callables without reaching into plugin_host internals.
from scenecraft.plugin_host import make_disposable  # noqa: E402


def extract_audio_as_wav(
    source_path: Path,
    out_path: Path,
    sample_rate: int = 48000,
) -> Path:
    """Transcode any ffmpeg-readable audio/video to PCM WAV at ``sample_rate``.

    Used by plugins that need a standardized input format (e.g. a vocal-isolation
    model that expects 48kHz mono WAV).

    Raises ``subprocess.CalledProcessError`` if ffmpeg exits non-zero, or
    ``subprocess.TimeoutExpired`` if transcoding takes longer than 60s.
    """
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-i",
            str(source_path),
            "-ac",
            "1",
            "-ar",
            str(sample_rate),
            str(out_path),
        ],
        capture_output=True,
        check=True,
        timeout=60,
    )
    return out_path


def record_spend(
    *,
    plugin_id: str,
    amount: int,
    unit: str,
    operation: str,
    username: str = "",
    org: str = "",
    api_key_id: str | None = None,
    job_ref: str | None = None,
    metadata: dict | None = None,
    source: str = "local",
) -> str:
    """Record a billable event to server.db's spend_ledger.

    The ONLY write path to spend_ledger — plugins never INSERT directly per
    spec R9/R9a. `plugin_id` validation (trust boundary) happens here: plugins
    cannot attribute spend to a different plugin.

    Unit-agnostic (credit / usd_micro / token / character / second). Integer
    `amount` in the smallest atomic unit of `unit`; negative values for refunds.

    Per 2026-04-23 dev directive: username/org default to '' when auth is not
    wired; api_key_id stays None. Add FK enforcement when the auth milestone
    ships.
    """
    # Trust boundary: verify the caller is the claimed plugin. In the current
    # static-PluginHost setup (M11 scaffolding), the caller identity is the
    # plugin's registered module — a stack-frame check would work but is
    # fragile across async/threading. For M16 dev phase we trust the caller;
    # the runtime wrapper landing in M17 adds a process-boundary check.
    # TODO(M17): enforce plugin_id == current plugin context via wrapped handle
    root = find_root()
    if root is None:
        raise RuntimeError(
            "record_spend called outside a scenecraft root — set SCENECRAFT_ROOT "
            "or invoke from within a provisioned box."
        )
    return _record_spend_raw(
        root,
        plugin_id=plugin_id,
        amount=amount,
        unit=unit,
        operation=operation,
        username=username,
        org=org,
        api_key_id=api_key_id,
        job_ref=job_ref,
        metadata=metadata,
        source=source,
    )


# --- External service routing (call_service) ----------------------------
# See task-128 spec. Exposes a single entry point for plugins making outbound
# HTTP calls. BYO mode reads per-service env var; broker mode stubbed until
# scenecraft.online ships.

class ServiceResponse:
    __slots__ = ("status", "headers", "body")

    def __init__(self, status: int, headers: dict, body):
        self.status = status
        self.headers = headers
        self.body = body


class ServiceError(Exception):
    def __init__(self, status: int, body):
        self.status = status
        self.body = body
        super().__init__(f"service error {status}")


class ServiceConfigError(Exception):
    """Raised when a service has no usable mode configured."""


class ServiceTimeoutError(Exception):
    """Raised when a service call times out."""


# (service, base_url, env_var, auth_header_name)
SERVICE_REGISTRY: dict[str, tuple[str, str, str]] = {
    "musicful": ("https://api.musicful.ai", "MUSICFUL_API_KEY", "x-api-key"),
    # Add Veo, Replicate, ElevenLabs, OpenAI, etc. here as they ship.
}


def call_service(
    *,
    service: str,
    method: str,
    path: str,
    body: dict | None = None,
    headers: dict | None = None,
    query: dict | None = None,
    timeout_seconds: float = 30.0,
) -> ServiceResponse:
    """Host-managed outbound HTTP for plugin code.

    Per spec task-128: M16 implements BYO mode only (env-var-present → direct
    call to the provider with the key as an auth header). Broker mode (routing
    through scenecraft.online with markup) is stubbed until the cloud milestone.

    Plugins MUST use this instead of calling providers directly — the routing
    layer will later add brokered mode, quota enforcement, and audit logging
    transparently.
    """
    import os
    try:
        import httpx  # type: ignore[import-untyped]
    except ImportError:  # pragma: no cover
        import urllib.request
        import urllib.error
        return _call_service_urllib(service, method, path, body, headers, query, timeout_seconds)

    if service not in SERVICE_REGISTRY:
        raise ServiceConfigError(f"Unknown service '{service}'. Registered: {list(SERVICE_REGISTRY)}")
    base_url, env_var, auth_header = SERVICE_REGISTRY[service]

    key = os.environ.get(env_var)
    if not key:
        # Broker path would go here; for M16 dev mode we surface the admin-
        # oriented message directly.
        raise ServiceConfigError(
            f"{env_var} not set. Brokered mode for '{service}' is not yet "
            f"available — set the environment variable to enable BYO mode."
        )

    url = f"{base_url}{path}"
    merged_headers = {auth_header: key, **(headers or {})}

    try:
        r = httpx.request(
            method, url, json=body, headers=merged_headers,
            params=query, timeout=timeout_seconds,
        )
    except httpx.TimeoutException as e:
        raise ServiceTimeoutError(str(e)) from e

    # Parse body without leaking the API key under any circumstance.
    content_type = r.headers.get("content-type", "")
    parsed = r.json() if "application/json" in content_type else r.content

    if r.status_code >= 400:
        raise ServiceError(status=r.status_code, body=parsed)

    return ServiceResponse(status=r.status_code, headers=dict(r.headers), body=parsed)


def _call_service_urllib(service, method, path, body, headers, query, timeout_seconds):
    """Fallback implementation using stdlib urllib when httpx is not available.
    Stripped-down feature set; used by tests / minimal deployments."""
    import os
    import json as _json
    import urllib.request
    import urllib.parse
    import urllib.error

    if service not in SERVICE_REGISTRY:
        raise ServiceConfigError(f"Unknown service '{service}'")
    base_url, env_var, auth_header = SERVICE_REGISTRY[service]
    key = os.environ.get(env_var)
    if not key:
        raise ServiceConfigError(f"{env_var} not set")

    url = f"{base_url}{path}"
    if query:
        url = f"{url}?{urllib.parse.urlencode(query)}"
    data = _json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method.upper())
    req.add_header(auth_header, key)
    if body is not None:
        req.add_header("content-type", "application/json")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=timeout_seconds) as resp:
            raw = resp.read()
            ct = resp.headers.get("content-type", "")
            parsed = _json.loads(raw.decode("utf-8")) if "application/json" in ct else raw
            return ServiceResponse(status=resp.status, headers=dict(resp.headers), body=parsed)
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            parsed = _json.loads(raw.decode("utf-8"))
        except Exception:
            parsed = raw
        raise ServiceError(status=e.code, body=parsed) from e
    except urllib.error.URLError as e:
        raise ServiceTimeoutError(str(e)) from e


def register_rest_endpoint(path_regex: str, handler, *, method: str = "POST", context=None):
    """Route a handler on the shared scenecraft REST server.

    Populates a per-method dict that ``api_server.py`` consults during
    request dispatch via ``PluginHost.dispatch_rest``. Returns a
    ``Disposable`` that removes the route when disposed; if ``context``
    is provided (a ``PluginContext`` from ``activate()``), the Disposable
    is auto-pushed into ``context.subscriptions`` so the host cleans it
    up on ``deactivate``.

    ``method`` selects the HTTP-method bucket. Default ``'POST'`` keeps
    pre-task-130 callers working. ``handler`` signature is
    ``handler(path: str, *args, **kwargs) -> Any``. For POST the host
    passes ``(project_dir, project_name, body)``; for GET it passes
    ``(project_dir, project_name, query)`` where ``query`` is a dict of
    already-parsed query-string params.
    """
    from scenecraft.plugin_host import PluginHost

    method_upper = method.upper()
    routes = PluginHost._rest_routes_by_method.setdefault(method_upper, {})
    routes[path_regex] = handler

    def _dispose() -> None:
        routes = PluginHost._rest_routes_by_method.get(method_upper, {})
        if routes.get(path_regex) is handler:
            del routes[path_regex]

    d = make_disposable(_dispose)
    if context is not None:
        context.subscriptions.append(d)
    return d
