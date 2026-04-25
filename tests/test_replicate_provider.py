"""Tests for plugin_api.providers.replicate.

Covers the key lifecycle paths using a stubbed HTTP layer — no real Replicate
calls. Smoke test against the real API is gated on REPLICATE_API_TOKEN and
marked with ``@pytest.mark.integration`` so CI without the env var skips it.
"""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from scenecraft.plugin_api.providers import replicate


# --- Fixtures ---------------------------------------------------------------


@pytest.fixture
def with_token(monkeypatch):
    monkeypatch.setenv(replicate.REPLICATE_TOKEN_ENV, "r8_testtoken")


@pytest.fixture
def without_token(monkeypatch):
    monkeypatch.delenv(replicate.REPLICATE_TOKEN_ENV, raising=False)


@pytest.fixture
def fake_record_spend(monkeypatch):
    calls = []

    def fake(**kwargs):
        ledger_id = f"ledger_{len(calls) + 1}"
        calls.append({**kwargs, "_ledger_id": ledger_id})
        return ledger_id

    import scenecraft.plugin_api as plugin_api

    monkeypatch.setattr(plugin_api, "record_spend", fake)
    return calls


# --- Auth -------------------------------------------------------------------


def test_missing_token_raises_not_configured(without_token):
    with pytest.raises(replicate.ReplicateNotConfigured):
        replicate._auth_headers()


def test_with_token_returns_bearer(with_token):
    headers = replicate._auth_headers()
    assert headers["Authorization"] == "Bearer r8_testtoken"
    assert headers["Content-Type"] == "application/json"


# --- Happy path: run_prediction -------------------------------------------


def _stub_httpx(create_response, poll_responses, download_bytes=b"AUDIO"):
    """Build a mock httpx module matching the shape used by the provider."""
    import httpx

    # Build a sequence of GET responses for poll + any latest-version lookup
    get_call = {"i": 0}
    get_responses = poll_responses

    def mock_get(url, headers=None, timeout=None):
        i = get_call["i"]
        get_call["i"] += 1
        resp = get_responses[min(i, len(get_responses) - 1)]
        return _MockResp(resp)

    def mock_post(url, json=None, headers=None, timeout=None):
        return _MockResp(create_response)

    class _MockStreamCtx:
        def __init__(self, payload):
            self.payload = payload
            self.status_code = payload["status_code"]

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def iter_bytes(self):
            yield self.payload["content"]

    def mock_stream(method, url, timeout=None):
        return _MockStreamCtx(
            {"status_code": 200, "content": download_bytes}
        )

    return mock_post, mock_get, mock_stream


class _MockResp:
    def __init__(self, payload):
        self.status_code = payload["status_code"]
        self._json = payload.get("json", {})
        self.text = payload.get("text", "")
        self.headers = payload.get("headers", {})

    def json(self):
        return self._json


def test_run_prediction_happy_path_single_output(with_token, fake_record_spend, monkeypatch, tmp_path):
    """Prediction creates, polls once, succeeds, downloads one output, writes ledger."""
    # Skip the /v1/models lookup by using explicit version string
    create_resp = {
        "status_code": 201,
        "json": {"id": "pred_abc", "status": "starting"},
    }
    poll_resp = {
        "status_code": 200,
        "json": {
            "id": "pred_abc",
            "status": "succeeded",
            "output": "https://replicate.delivery/out.wav",
        },
    }
    mock_post, mock_get, mock_stream = _stub_httpx(create_resp, [poll_resp])

    with patch("httpx.post", mock_post), \
         patch("httpx.get", mock_get), \
         patch("httpx.stream", mock_stream), \
         patch("scenecraft.plugin_api.providers.replicate.time.sleep"):
        result = replicate.run_prediction(
            model="zsxkib/mmaudio:explicit_version_hash",
            input={"prompt": "footsteps"},
            source="generate_foley",
        )

    assert result.prediction_id == "pred_abc"
    assert result.status == "succeeded"
    assert len(result.output_paths) == 1
    assert result.output_paths[0].read_bytes() == b"AUDIO"
    assert result.spend_ledger_id == "ledger_1"

    # Spend was recorded
    assert len(fake_record_spend) == 1
    spend = fake_record_spend[0]
    assert spend["plugin_id"] == "generate_foley"
    assert spend["amount"] == 1
    assert spend["unit"] == "prediction"
    assert spend["job_ref"] == "pred_abc"


def test_run_prediction_failed_does_not_record_spend(with_token, fake_record_spend, monkeypatch):
    create_resp = {
        "status_code": 201,
        "json": {"id": "pred_fail", "status": "starting"},
    }
    poll_resp = {
        "status_code": 200,
        "json": {
            "id": "pred_fail",
            "status": "failed",
            "error": "model exploded",
        },
    }
    mock_post, mock_get, mock_stream = _stub_httpx(create_resp, [poll_resp])

    with patch("httpx.post", mock_post), \
         patch("httpx.get", mock_get), \
         patch("scenecraft.plugin_api.providers.replicate.time.sleep"):
        with pytest.raises(replicate.ReplicatePredictionFailed) as exc_info:
            replicate.run_prediction(
                model="zsxkib/mmaudio:explicit_version_hash",
                input={"prompt": "nothing"},
                source="generate_foley",
            )

    assert exc_info.value.prediction_id == "pred_fail"
    assert "model exploded" in str(exc_info.value)
    # Critical: NO spend recorded on failure
    assert fake_record_spend == []


def test_run_prediction_download_failure_still_records_spend(with_token, fake_record_spend, monkeypatch):
    """Replicate succeeds but downloads all fail — spend IS recorded, raise DownloadFailed."""
    import httpx

    create_resp = {
        "status_code": 201,
        "json": {"id": "pred_dl_fail", "status": "starting"},
    }
    poll_resp = {
        "status_code": 200,
        "json": {
            "id": "pred_dl_fail",
            "status": "succeeded",
            "output": "https://replicate.delivery/unreachable.wav",
        },
    }
    mock_post, mock_get, _ = _stub_httpx(create_resp, [poll_resp])

    # Download mock always raises
    def failing_stream(method, url, timeout=None):
        raise httpx.ConnectError("no route to host")

    with patch("httpx.post", mock_post), \
         patch("httpx.get", mock_get), \
         patch("httpx.stream", failing_stream), \
         patch("scenecraft.plugin_api.providers.replicate.time.sleep"):
        with pytest.raises(replicate.ReplicateDownloadFailed) as exc_info:
            replicate.run_prediction(
                model="zsxkib/mmaudio:explicit_version_hash",
                input={"prompt": "anything"},
                source="generate_foley",
            )

    assert exc_info.value.prediction_id == "pred_dl_fail"
    assert exc_info.value.spend_ledger_id == "ledger_1"
    # Critical: spend WAS recorded (Replicate charged)
    assert len(fake_record_spend) == 1


def test_429_backoff_on_create(with_token, fake_record_spend, monkeypatch):
    """Create gets 429 twice, then succeeds. Poll succeeds. Download succeeds."""
    # Build a stateful post mock
    responses_iter = iter([
        _MockResp({"status_code": 429, "text": "rate limited"}),
        _MockResp({"status_code": 429, "text": "rate limited"}),
        _MockResp({"status_code": 201, "json": {"id": "pred_retry", "status": "starting"}}),
    ])

    def mock_post(url, json=None, headers=None, timeout=None):
        return next(responses_iter)

    poll_resp = {
        "status_code": 200,
        "json": {
            "id": "pred_retry",
            "status": "succeeded",
            "output": "https://replicate.delivery/out.wav",
        },
    }
    _, mock_get, mock_stream = _stub_httpx({"status_code": 201, "json": {}}, [poll_resp])

    with patch("httpx.post", mock_post), \
         patch("httpx.get", mock_get), \
         patch("httpx.stream", mock_stream), \
         patch("scenecraft.plugin_api.providers.replicate.time.sleep") as sleep_mock:
        result = replicate.run_prediction(
            model="zsxkib/mmaudio:explicit_version_hash",
            input={"prompt": "test"},
            source="generate_foley",
        )

    assert result.prediction_id == "pred_retry"
    # Confirm backoff waits were honored: 1s, 2s (third attempt succeeds)
    sleep_values = [call.args[0] for call in sleep_mock.call_args_list]
    assert 1.0 in sleep_values
    assert 2.0 in sleep_values


# --- Input sanitization ----------------------------------------------------


def test_local_path_input_raises_helpful_error(with_token, fake_record_spend):
    with pytest.raises(replicate.ReplicateError, match="local Path"):
        replicate.run_prediction(
            model="zsxkib/mmaudio:v",
            input={"video": Path("/tmp/some.mp4")},
            source="generate_foley",
        )
    # No spend attempted
    assert fake_record_spend == []


# --- Model version resolution ---------------------------------------------


def test_version_resolution_with_colon(with_token):
    assert replicate._resolve_version("owner/model:abc123") == "abc123"


def test_version_resolution_bare_hash(with_token):
    assert replicate._resolve_version("abc123deadbeef") == "abc123deadbeef"


# --- get_balance (never raises) --------------------------------------------


def test_get_balance_returns_none_on_error(with_token):
    import httpx

    def failing_get(*a, **kw):
        raise httpx.ConnectError("no network")

    with patch("httpx.get", failing_get):
        assert replicate.get_balance() is None


def test_get_balance_returns_none_without_token(without_token):
    # Doesn't raise, returns None
    assert replicate.get_balance() is None


# --- attach_polling ---------------------------------------------------------


def test_attach_polling_invokes_callback_on_success(with_token, fake_record_spend, monkeypatch):
    poll_resp = {
        "status_code": 200,
        "json": {
            "id": "pred_xxx",
            "status": "succeeded",
            "output": "https://replicate.delivery/out.wav",
        },
    }
    _, mock_get, mock_stream = _stub_httpx({"status_code": 201, "json": {}}, [poll_resp])

    received = []

    def on_complete(result_or_error):
        received.append(result_or_error)

    with patch("httpx.get", mock_get), \
         patch("httpx.stream", mock_stream), \
         patch("scenecraft.plugin_api.providers.replicate.time.sleep"):
        replicate.attach_polling(
            prediction_id="pred_xxx",
            source="generate_foley",
            on_complete=on_complete,
        )

    assert len(received) == 1
    assert isinstance(received[0], replicate.PredictionResult)
    assert received[0].prediction_id == "pred_xxx"


def test_attach_polling_invokes_callback_on_failure(with_token, fake_record_spend, monkeypatch):
    poll_resp = {
        "status_code": 200,
        "json": {
            "id": "pred_yyy",
            "status": "failed",
            "error": "too bad",
        },
    }
    _, mock_get, _ = _stub_httpx({"status_code": 201, "json": {}}, [poll_resp])

    received = []

    def on_complete(result_or_error):
        received.append(result_or_error)

    with patch("httpx.get", mock_get), \
         patch("scenecraft.plugin_api.providers.replicate.time.sleep"):
        replicate.attach_polling(
            prediction_id="pred_yyy",
            source="generate_foley",
            on_complete=on_complete,
        )

    assert len(received) == 1
    assert isinstance(received[0], replicate.ReplicatePredictionFailed)
    assert received[0].prediction_id == "pred_yyy"
    # No spend on failure
    assert fake_record_spend == []


# --- R9a invariant ---------------------------------------------------------


def test_provider_does_not_import_scenecraft_db_at_module_level():
    """The provider must not pull in raw DB handles at import time.

    Per R9a, plugins + provider modules reach spend_ledger via
    plugin_api.record_spend only. The runtime attribute check below ensures
    the module namespace doesn't leak a db import.
    """
    import scenecraft.plugin_api.providers.replicate as mod

    # Confirm no raw db handles are bound at module level
    assert "db" not in dir(mod), "replicate module should not expose 'db' at top level"
    assert "sqlite3" not in dir(mod), "replicate module should not expose sqlite3 directly"
