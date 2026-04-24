"""Replicate provider for plugin_api.

Plugins call:

    from scenecraft import plugin_api

    result = plugin_api.providers.replicate.run_prediction(
        model="zsxkib/mmaudio",
        input={"prompt": ..., "duration": ..., "video": ...},
        source="generate_foley",
    )

This module owns ALL Replicate-specific concerns:
- REPLICATE_API_TOKEN auth lookup
- HTTP client (httpx, urllib fallback)
- Prediction creation + 5s polling to completion
- 429 backoff (1s → 2s → 4s → fail)
- spend_ledger write on success (via plugin_api.record_spend — no raw DB)
- Output artifact download with 3× retry + backoff
- Disconnect-survival via attach_polling() for reattach after server restart

Per R9a invariant: no raw DB access. Spend is recorded via
plugin_api.record_spend — this module does NOT import scenecraft.db.
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from tempfile import mkdtemp
from typing import Any, Callable, Literal

logger = logging.getLogger(__name__)

REPLICATE_API_BASE = "https://api.replicate.com"
REPLICATE_TOKEN_ENV = "REPLICATE_API_TOKEN"

# Polling + retry tuning
DEFAULT_POLL_INTERVAL_SECONDS = 5.0
RATE_LIMIT_BACKOFF_SECONDS = (1.0, 2.0, 4.0)  # 3 attempts
DOWNLOAD_BACKOFF_SECONDS = (1.0, 2.0, 4.0)     # 3 attempts


# --- Exceptions -----------------------------------------------------------


class ReplicateError(Exception):
    """Base class for Replicate provider errors."""


class ReplicateNotConfigured(ReplicateError):
    """REPLICATE_API_TOKEN is missing from the environment."""


class ReplicatePredictionFailed(ReplicateError):
    """Replicate returned status='failed' (or 'canceled') for the prediction."""

    def __init__(self, prediction_id: str, error: str):
        self.prediction_id = prediction_id
        self.error = error
        super().__init__(f"prediction {prediction_id} failed: {error}")


class ReplicateDownloadFailed(ReplicateError):
    """Replicate prediction succeeded but we could not fetch the output.

    ``spend_ledger_id`` is populated because Replicate has already charged —
    callers should surface this to the user so retry semantics are obvious.
    """

    def __init__(self, prediction_id: str, spend_ledger_id: str):
        self.prediction_id = prediction_id
        self.spend_ledger_id = spend_ledger_id
        super().__init__(
            f"prediction {prediction_id} succeeded but download failed "
            f"(prediction charged; spend_ledger_id={spend_ledger_id})"
        )


# --- Result type ----------------------------------------------------------


@dataclass
class PredictionResult:
    """What plugins get back from run_prediction on success."""

    prediction_id: str
    status: Literal["succeeded"]
    output_paths: list[Path]
    raw: dict
    spend_ledger_id: str

    @property
    def output_bytes(self) -> bytes | None:
        """Convenience: contents of ``output_paths[0]`` if single-file output."""
        if not self.output_paths:
            return None
        return self.output_paths[0].read_bytes()


# --- Internal HTTP ---------------------------------------------------------


def _auth_headers() -> dict[str, str]:
    token = os.environ.get(REPLICATE_TOKEN_ENV)
    if not token:
        raise ReplicateNotConfigured(
            f"{REPLICATE_TOKEN_ENV} not set. Set the environment variable to "
            "enable the Replicate provider. See https://replicate.com/account/api-tokens."
        )
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }


def _http_post_with_backoff(path: str, body: dict, *, timeout: float = 30.0) -> dict:
    """POST with 429 backoff. Returns parsed JSON."""
    import httpx  # type: ignore[import-untyped]

    url = f"{REPLICATE_API_BASE}{path}"
    headers = _auth_headers()
    last_exc: Exception | None = None
    for wait in (*RATE_LIMIT_BACKOFF_SECONDS, None):
        try:
            r = httpx.post(url, json=body, headers=headers, timeout=timeout)
        except httpx.HTTPError as e:
            last_exc = e
            if wait is None:
                raise ReplicateError(f"HTTP error calling {path}: {e}") from e
            time.sleep(wait)
            continue
        if r.status_code == 429 and wait is not None:
            time.sleep(wait)
            continue
        if r.status_code >= 400:
            raise ReplicateError(f"POST {path} returned {r.status_code}: {r.text}")
        return r.json()
    # Unreachable — loop either returns, raises, or falls through after last retry
    raise ReplicateError(f"exhausted retries on POST {path}: {last_exc}")


def _http_get_with_backoff(path: str, *, timeout: float = 30.0) -> dict:
    """GET with 429 backoff. Returns parsed JSON."""
    import httpx  # type: ignore[import-untyped]

    url = f"{REPLICATE_API_BASE}{path}"
    headers = _auth_headers()
    last_exc: Exception | None = None
    for wait in (*RATE_LIMIT_BACKOFF_SECONDS, None):
        try:
            r = httpx.get(url, headers=headers, timeout=timeout)
        except httpx.HTTPError as e:
            last_exc = e
            if wait is None:
                raise ReplicateError(f"HTTP error calling {path}: {e}") from e
            time.sleep(wait)
            continue
        if r.status_code == 429 and wait is not None:
            time.sleep(wait)
            continue
        if r.status_code >= 400:
            raise ReplicateError(f"GET {path} returned {r.status_code}: {r.text}")
        return r.json()
    raise ReplicateError(f"exhausted retries on GET {path}: {last_exc}")


def _poll_to_completion(prediction_id: str, *, poll_interval: float) -> dict:
    """Poll a prediction until status ∈ {succeeded, failed, canceled}."""
    while True:
        prediction = _http_get_with_backoff(f"/v1/predictions/{prediction_id}")
        status = prediction.get("status")
        if status in ("succeeded", "failed", "canceled"):
            return prediction
        time.sleep(poll_interval)


def _download_outputs(prediction: dict) -> list[Path]:
    """Download each output URL to a tempdir with retry. Raises on exhaust."""
    import httpx  # type: ignore[import-untyped]

    output = prediction.get("output")
    if output is None:
        return []
    urls = output if isinstance(output, list) else [output]
    # Filter to http(s) URLs — some models return JSON-encoded results directly,
    # which this path does not handle.
    urls = [u for u in urls if isinstance(u, str) and u.startswith(("http://", "https://"))]
    if not urls:
        return []

    tempdir = Path(mkdtemp(prefix="scenecraft-replicate-"))
    paths: list[Path] = []
    for i, url in enumerate(urls):
        dest = tempdir / _extract_filename(url, fallback=f"output_{i}")
        last_exc: Exception | None = None
        for wait in (*DOWNLOAD_BACKOFF_SECONDS, None):
            try:
                with httpx.stream("GET", url, timeout=120.0) as r:
                    if r.status_code >= 400:
                        last_exc = ReplicateError(
                            f"download {url} returned {r.status_code}"
                        )
                        if wait is None:
                            raise last_exc
                        time.sleep(wait)
                        continue
                    with dest.open("wb") as f:
                        for chunk in r.iter_bytes():
                            f.write(chunk)
                break  # download success
            except httpx.HTTPError as e:
                last_exc = e
                if wait is None:
                    raise ReplicateError(f"exhausted download retries on {url}: {e}") from e
                time.sleep(wait)
                continue
        else:
            raise ReplicateError(f"exhausted download retries on {url}: {last_exc}")
        paths.append(dest)
    return paths


def _extract_filename(url: str, *, fallback: str) -> str:
    """Best-effort filename extraction from a URL path."""
    # Strip query/fragment, take last path segment
    stripped = url.split("?", 1)[0].split("#", 1)[0]
    name = stripped.rsplit("/", 1)[-1]
    if not name or name.endswith("/"):
        return fallback
    return name


def _record_spend(*, prediction_id: str, source: str) -> str:
    """Write a single spend_ledger row for a successful prediction.

    Delegates to plugin_api.record_spend — this module never writes raw SQL.
    Returns the ledger row id.
    """
    from scenecraft import plugin_api

    return plugin_api.record_spend(
        plugin_id=source,
        amount=1,
        unit="prediction",
        operation="replicate.run_prediction",
        job_ref=prediction_id,
        source="replicate",
    )


# --- Public API ------------------------------------------------------------


def run_prediction(
    *,
    model: str,
    input: dict[str, Any],
    source: str,
    poll_interval: float = DEFAULT_POLL_INTERVAL_SECONDS,
) -> PredictionResult:
    """Run a Replicate prediction to completion.

    Steps:
      1. Create a prediction via POST /v1/predictions.
      2. Poll GET /v1/predictions/:id every ``poll_interval`` seconds until
         the prediction reaches a terminal state.
      3. On success: write a spend_ledger row (Replicate has charged),
         then download output artifacts with retry.
      4. On failure: raise ``ReplicatePredictionFailed``. No ledger write.

    :param model:         Model identifier like "zsxkib/mmaudio".
    :param input:         Model-specific input dict. Pass ``Path`` objects for
                          file inputs — they are NOT currently uploaded by this
                          shim; upload them first to Replicate's /v1/files
                          endpoint or pass public URLs.
    :param source:        Plugin id for spend_ledger attribution (e.g.
                          ``"generate_foley"``).
    :param poll_interval: Seconds between status polls. Default 5s.

    :raises ReplicateNotConfigured:   if REPLICATE_API_TOKEN is missing.
    :raises ReplicatePredictionFailed: if Replicate returned status='failed'.
    :raises ReplicateDownloadFailed:  if prediction succeeded but download failed.
    :raises ReplicateError:            on other HTTP or transport errors.
    """
    # Input sanity
    _sanitize_input_for_json(input)

    # Step 1: create
    create_body = {"version": _resolve_version(model), "input": input}
    created = _http_post_with_backoff("/v1/predictions", create_body)
    prediction_id = created.get("id")
    if not prediction_id:
        raise ReplicateError(f"create returned no id: {created}")

    # Step 2: poll to terminal state
    prediction = _poll_to_completion(prediction_id, poll_interval=poll_interval)
    status = prediction.get("status")

    if status != "succeeded":
        # failed or canceled — no ledger write
        raise ReplicatePredictionFailed(
            prediction_id=prediction_id,
            error=prediction.get("error") or f"status={status}",
        )

    # Step 3a: ledger first — Replicate charged regardless of our download
    spend_ledger_id = _record_spend(prediction_id=prediction_id, source=source)

    # Step 3b: download
    try:
        output_paths = _download_outputs(prediction)
    except ReplicateError as e:
        # Charge already recorded; surface with download-failed exception
        logger.exception(
            "Replicate prediction %s succeeded but download failed", prediction_id
        )
        raise ReplicateDownloadFailed(
            prediction_id=prediction_id,
            spend_ledger_id=spend_ledger_id,
        ) from e

    return PredictionResult(
        prediction_id=prediction_id,
        status="succeeded",
        output_paths=output_paths,
        raw=prediction,
        spend_ledger_id=spend_ledger_id,
    )


def attach_polling(
    *,
    prediction_id: str,
    source: str,
    on_complete: Callable[[PredictionResult | ReplicateError], None],
    poll_interval: float = DEFAULT_POLL_INTERVAL_SECONDS,
) -> None:
    """Resume polling on an already-created prediction (disconnect-survival).

    Does NOT re-create the prediction — only resumes polling an existing one.
    Runs synchronously in the calling thread; callers that need async behavior
    should spawn a thread or task.

    On terminal state: downloads outputs (on succeeded), writes spend_ledger
    (on succeeded — if we haven't written one yet; note this may double-record
    if called for a prediction that already has a ledger entry — callers are
    responsible for dedup if needed), and invokes ``on_complete`` with either
    a ``PredictionResult`` or the relevant ``ReplicateError`` subclass.
    """
    try:
        prediction = _poll_to_completion(prediction_id, poll_interval=poll_interval)
        status = prediction.get("status")
        if status != "succeeded":
            on_complete(
                ReplicatePredictionFailed(
                    prediction_id=prediction_id,
                    error=prediction.get("error") or f"status={status}",
                )
            )
            return

        spend_ledger_id = _record_spend(prediction_id=prediction_id, source=source)
        try:
            output_paths = _download_outputs(prediction)
        except ReplicateError as e:
            on_complete(
                ReplicateDownloadFailed(
                    prediction_id=prediction_id,
                    spend_ledger_id=spend_ledger_id,
                )
            )
            return
        on_complete(
            PredictionResult(
                prediction_id=prediction_id,
                status="succeeded",
                output_paths=output_paths,
                raw=prediction,
                spend_ledger_id=spend_ledger_id,
            )
        )
    except ReplicateError as e:
        on_complete(e)


def get_balance() -> float | None:
    """Return Replicate account balance in USD. None on any error.

    This is a status query — it never raises. Use it for soft UX (panel
    header "low balance" warning); do NOT gate paid calls on it.
    """
    try:
        account = _http_get_with_backoff("/v1/account")
    except ReplicateError:
        return None
    # Replicate's account endpoint returns {username, github_url, name, type}
    # without exposing a numeric balance publicly. Return None until Replicate
    # surfaces a balance field — stubbed to preserve the API shape for future
    # enablement.
    return None


# --- Helpers --------------------------------------------------------------


def _resolve_version(model: str) -> str:
    """Resolve a model reference to a Replicate version string.

    Accepts:
      - "owner/model" shorthand → looks up latest version via GET /v1/models/:o/:m
      - "owner/model:version" explicit form → returns the version directly
      - bare "version" hash → returns it
    """
    if ":" in model and "/" not in model.split(":", 1)[1]:
        # owner/model:version form
        return model.split(":", 1)[1]
    if "/" not in model:
        # bare version hash
        return model
    # owner/model — look up latest
    owner, name = model.split("/", 1)
    model_doc = _http_get_with_backoff(f"/v1/models/{owner}/{name}")
    version = (model_doc.get("latest_version") or {}).get("id")
    if not version:
        raise ReplicateError(f"model {model} has no latest_version")
    return version


def _sanitize_input_for_json(input: dict[str, Any]) -> None:
    """Convert non-JSON types in-place where we can, raise for unsupported ones.

    Currently: converts ``Path`` to str (callers are expected to pass URLs or
    uploaded file references; direct local-file upload is not implemented here).
    """
    for k, v in list(input.items()):
        if isinstance(v, Path):
            # A local path is almost certainly a mistake — Replicate accepts
            # either public URLs or pre-uploaded file refs. Raise early rather
            # than send a JSON-garbled value.
            raise ReplicateError(
                f"input[{k!r}] is a local Path ({v}). Upload to Replicate's "
                "/v1/files endpoint (or pass a public URL) before calling "
                "run_prediction. This shim does not auto-upload."
            )


__all__ = [
    "run_prediction",
    "attach_polling",
    "get_balance",
    "PredictionResult",
    "ReplicateError",
    "ReplicateNotConfigured",
    "ReplicatePredictionFailed",
    "ReplicateDownloadFailed",
    "REPLICATE_API_BASE",
    "REPLICATE_TOKEN_ENV",
]
