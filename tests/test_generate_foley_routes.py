"""Tests for M18 task-145 foley REST handlers.

Exercise the _handle_run / _handle_list / _handle_retry handlers directly
(bypasses api_server's dispatch; keeps the test focused on the handlers'
validation + delegation logic).
"""

from __future__ import annotations

import time
from pathlib import Path
from unittest.mock import patch

import pytest

from scenecraft.db import get_db
from scenecraft.plugins.generate_foley import routes
from scenecraft.plugins.generate_foley import generate_foley as impl


# --- Fixtures ---------------------------------------------------------------


@pytest.fixture
def project_dir(tmp_path):
    get_db(tmp_path)
    (tmp_path / "pool" / "segments").mkdir(parents=True, exist_ok=True)
    return tmp_path


@pytest.fixture
def with_token(monkeypatch):
    monkeypatch.setenv("REPLICATE_API_TOKEN", "r8_test")


@pytest.fixture
def fake_provider(monkeypatch, tmp_path):
    """Replace provider.run_prediction with a configurable fake."""
    from scenecraft.plugin_api.providers import replicate as repmod

    output_path = tmp_path / "_fake.wav"
    output_path.write_bytes(b"X")

    class FakeResult:
        prediction_id = "pred_fake"
        status = "succeeded"
        output_paths = [output_path]
        spend_ledger_id = "ledger_fake"
        raw = {}

    state = {"result": FakeResult()}

    def fake_run(**kwargs):
        if isinstance(state["result"], Exception):
            raise state["result"]
        return state["result"]

    monkeypatch.setattr(repmod, "run_prediction", fake_run)
    return state


def _wait(project_dir, generation_id, timeout=5.0):
    import scenecraft.plugin_api as plugin_api
    deadline = time.time() + timeout
    while time.time() < deadline:
        g = plugin_api.get_foley_generation(project_dir, generation_id)
        if g and g["status"] in ("completed", "failed"):
            return g
        time.sleep(0.05)
    raise TimeoutError(generation_id)


# --- POST /run validation --------------------------------------------------


def test_run_rejects_count_not_one(project_dir):
    r = routes._handle_run("/path", project_dir, "test", {"count": 2})
    assert "count must be 1" in r["error"]


def test_run_rejects_v2fx_missing_range(project_dir):
    r = routes._handle_run("/path", project_dir, "test", {
        "source_candidate_id": "ps_x",
        # no in/out
    })
    assert "v2fx mode requires source_in_seconds" in r["error"]


def test_run_rejects_out_le_in(project_dir):
    r = routes._handle_run("/path", project_dir, "test", {
        "source_candidate_id": "ps_x",
        "source_in_seconds": 5.0,
        "source_out_seconds": 3.0,
    })
    assert "source_out_seconds must be > source_in_seconds" in r["error"]


def test_run_rejects_range_too_long(project_dir):
    r = routes._handle_run("/path", project_dir, "test", {
        "source_candidate_id": "ps_x",
        "source_in_seconds": 0.0,
        "source_out_seconds": 45.0,
    })
    assert "30" in r["error"]  # ceiling mentioned


def test_run_rejects_duration_out_of_bounds(project_dir):
    r = routes._handle_run("/path", project_dir, "test", {
        "duration_seconds": 60.0,
    })
    assert "out of bounds" in r["error"]


def test_run_rejects_invalid_entity_type(project_dir):
    r = routes._handle_run("/path", project_dir, "test", {
        "duration_seconds": 2.0,
        "entity_type": "audio_clip",
    })
    assert "entity_type must be 'transition'" in r["error"]


# --- POST /run happy path -------------------------------------------------


def test_run_happy_path_t2fx(project_dir, with_token, fake_provider):
    r = routes._handle_run("/path", project_dir, "test", {
        "prompt": "footsteps",
        "duration_seconds": 2.0,
    })
    assert "error" not in r
    assert r["status"] == "pending"
    assert r["mode"] == "t2fx"
    gen = _wait(project_dir, r["generation_id"])
    assert gen["status"] == "completed"


# --- GET /generations ------------------------------------------------------


def test_list_returns_all_when_no_filter(project_dir, with_token, fake_provider):
    # Create two generations
    r1 = routes._handle_run("/path", project_dir, "test", {
        "prompt": "a", "duration_seconds": 2.0,
    })
    _wait(project_dir, r1["generation_id"])
    r2 = routes._handle_run("/path", project_dir, "test", {
        "prompt": "b", "duration_seconds": 3.0,
    })
    _wait(project_dir, r2["generation_id"])

    listing = routes._handle_list("/path", project_dir, "test", {})
    ids = {g["id"] for g in listing["generations"]}
    assert r1["generation_id"] in ids
    assert r2["generation_id"] in ids


def test_list_filters_by_entity(project_dir, with_token, fake_provider):
    # Two generations, one attached to tr_A, one to tr_B
    r_a = routes._handle_run("/path", project_dir, "test", {
        "prompt": "a", "duration_seconds": 2.0,
    })
    _wait(project_dir, r_a["generation_id"])
    # Seed a pool_segment for v2fx
    from scenecraft.db import add_pool_segment
    src_id = add_pool_segment(
        project_dir,
        pool_path="pool/segments/src.mp4",
        kind="imported",
        created_by="test",
    )
    (project_dir / "pool" / "segments" / "src.mp4").write_bytes(b"VIDEO")

    # Stub pretrim so we don't need ffmpeg
    from scenecraft.plugins.generate_foley import pretrim
    orig_trim = pretrim.trim_to_range

    def fake_trim(*, source_path, in_seconds, out_seconds, output_path=None):
        p = Path(f"/tmp/trimmed_{source_path.stem}.mp4")
        p.write_bytes(b"TRIMMED")
        return p

    with patch.object(pretrim, "trim_to_range", fake_trim):
        r_b = routes._handle_run("/path", project_dir, "test", {
            "prompt": "b",
            "source_candidate_id": src_id,
            "source_in_seconds": 0.0,
            "source_out_seconds": 2.0,
            "entity_type": "transition",
            "entity_id": "tr_B",
        })
        _wait(project_dir, r_b["generation_id"])

    # Filter to tr_B only
    listing = routes._handle_list(
        "/path", project_dir, "test",
        {"entityType": "transition", "entityId": "tr_B"},
    )
    ids = {g["id"] for g in listing["generations"]}
    assert r_b["generation_id"] in ids
    assert r_a["generation_id"] not in ids


def test_list_limit_is_clamped(project_dir):
    listing = routes._handle_list("/path", project_dir, "test", {"limit": "99999"})
    # No crash, returns empty list safely
    assert "generations" in listing


# --- POST /generations/:id/retry ------------------------------------------


def test_retry_unknown_returns_error(project_dir):
    r = routes._handle_retry(
        "/api/projects/X/plugins/generate-foley/generations/gen_nope/retry",
        project_dir, "X", {},
    )
    assert "not found" in r["error"]


def test_retry_in_flight_is_rejected(project_dir):
    import scenecraft.plugin_api as plugin_api
    plugin_api.add_foley_generation(
        project_dir,
        generation_id="gen_running",
        mode="t2fx",
        model="zsxkib/mmaudio",
        status="running",
    )
    r = routes._handle_retry(
        "/api/projects/X/plugins/generate-foley/generations/gen_running/retry",
        project_dir, "X", {},
    )
    assert "still running" in r["error"]


def test_retry_completed_generation_creates_new_row(
    project_dir, with_token, fake_provider
):
    # Run + complete an original
    r1 = routes._handle_run("/path", project_dir, "test", {
        "prompt": "original",
        "duration_seconds": 2.0,
        "cfg_strength": 5.5,
        "seed": 42,
    })
    _wait(project_dir, r1["generation_id"])

    # Retry it
    retry = routes._handle_retry(
        f"/api/projects/test/plugins/generate-foley/generations/{r1['generation_id']}/retry",
        project_dir, "test", {},
    )
    assert "error" not in retry
    assert retry["generation_id"] != r1["generation_id"]

    # Wait on retry and verify params carried over
    _wait(project_dir, retry["generation_id"])
    import scenecraft.plugin_api as plugin_api
    new_gen = plugin_api.get_foley_generation(project_dir, retry["generation_id"])
    assert new_gen["prompt"] == "original"
    assert new_gen["cfg_strength"] == 5.5
    assert new_gen["seed"] == 42


def test_retry_malformed_path_error(project_dir):
    r = routes._handle_retry("/some/nonsense/path", project_dir, "X", {})
    assert "malformed" in r["error"]


# --- register() smoke test -------------------------------------------------


def test_register_installs_three_routes():
    """register() should add three routes to PluginHost's _rest_routes_by_method."""
    from scenecraft.plugin_host import PluginHost
    import scenecraft.plugin_api as plugin_api

    # Reset to a known state
    PluginHost._rest_routes_by_method = {}

    class FakeContext:
        project_dir = None  # skip the resume_in_flight scan
        subscriptions = []

    routes.register(plugin_api, FakeContext())

    post_routes = PluginHost._rest_routes_by_method.get("POST", {})
    get_routes = PluginHost._rest_routes_by_method.get("GET", {})

    post_keys = list(post_routes.keys())
    get_keys = list(get_routes.keys())
    # Two POST routes: /run and /retry. One GET route: /generations.
    assert any("generate-foley/run$" in k for k in post_keys)
    assert any("/retry$" in k for k in post_keys)
    assert any("/generations$" in k for k in get_keys)
