"""Tests for M18 task-144 backend foley plugin.

Covers:
- Input validation (mode inference, range, count, duration bounds)
- t2fx end-to-end with mocked provider
- v2fx end-to-end with mocked pretrim + provider
- Error paths: prediction failed, download failed, not configured
- check_api_key
- Resume-in-flight (disconnect-survival) hook

Uses monkeypatched providers.replicate.run_prediction so no network.
"""

from __future__ import annotations

import time
from pathlib import Path
from unittest.mock import patch

import pytest

from scenecraft.db import get_db, add_pool_segment
from scenecraft.plugins.generate_foley import generate_foley as gf


# --- Fixtures --------------------------------------------------------------


@pytest.fixture
def project_dir(tmp_path):
    get_db(tmp_path)
    (tmp_path / "pool" / "segments").mkdir(parents=True, exist_ok=True)
    return tmp_path


@pytest.fixture
def with_token(monkeypatch):
    monkeypatch.setenv("REPLICATE_API_TOKEN", "r8_test")


@pytest.fixture
def without_token(monkeypatch):
    monkeypatch.delenv("REPLICATE_API_TOKEN", raising=False)


@pytest.fixture
def fake_provider(monkeypatch, tmp_path):
    """Replace providers.replicate.run_prediction with a configurable fake."""
    from scenecraft.plugin_api.providers import replicate as repmod

    output_path = tmp_path / "_fake_output.wav"
    output_path.write_bytes(b"FAKE_AUDIO_BYTES")

    class FakeResult:
        prediction_id = "pred_fake"
        status = "succeeded"
        output_paths = [output_path]
        spend_ledger_id = "ledger_fake"
        raw = {"id": "pred_fake", "status": "succeeded"}

    state = {"result": FakeResult(), "calls": []}

    def fake_run(**kwargs):
        state["calls"].append(kwargs)
        if isinstance(state["result"], Exception):
            raise state["result"]
        return state["result"]

    monkeypatch.setattr(repmod, "run_prediction", fake_run)
    return state


# --- check_api_key ---------------------------------------------------------


def test_check_api_key_with_token(with_token):
    assert gf.check_api_key() == {"passed": True}


def test_check_api_key_without_token(without_token):
    r = gf.check_api_key()
    assert r["passed"] is False
    assert "REPLICATE_API_TOKEN" in r["message"]


# --- Mode resolution -------------------------------------------------------


def test_mode_inferred_t2fx_when_no_candidate():
    assert gf._resolve_mode(None, source_candidate_id=None) == "t2fx"


def test_mode_inferred_v2fx_when_candidate_provided():
    assert gf._resolve_mode(None, source_candidate_id="ps_123") == "v2fx"


def test_mode_explicit_overrides_inference():
    assert gf._resolve_mode("t2fx", source_candidate_id="ps_123") == "t2fx"


# --- Validation ------------------------------------------------------------


def test_validate_rejects_count_not_one():
    with pytest.raises(ValueError, match="variant_count"):
        gf._validate(
            mode="t2fx", prompt="x", duration_seconds=2.0,
            source_candidate_id=None, source_in_seconds=None,
            source_out_seconds=None, variant_count=2,
        )


def test_validate_t2fx_rejects_source_candidate():
    with pytest.raises(ValueError, match="t2fx mode must not include"):
        gf._validate(
            mode="t2fx", prompt="x", duration_seconds=2.0,
            source_candidate_id="ps_1", source_in_seconds=None,
            source_out_seconds=None, variant_count=1,
        )


def test_validate_v2fx_requires_candidate_and_range():
    with pytest.raises(ValueError, match="v2fx mode requires source_candidate_id"):
        gf._validate(
            mode="v2fx", prompt=None, duration_seconds=None,
            source_candidate_id=None, source_in_seconds=1.0,
            source_out_seconds=3.0, variant_count=1,
        )
    with pytest.raises(ValueError, match="requires source_in_seconds"):
        gf._validate(
            mode="v2fx", prompt=None, duration_seconds=None,
            source_candidate_id="ps_1", source_in_seconds=None,
            source_out_seconds=None, variant_count=1,
        )


def test_validate_v2fx_range_ordering():
    with pytest.raises(ValueError, match="must be >"):
        gf._validate(
            mode="v2fx", prompt=None, duration_seconds=None,
            source_candidate_id="ps_1", source_in_seconds=3.0,
            source_out_seconds=1.0, variant_count=1,
        )


def test_validate_v2fx_range_too_long():
    with pytest.raises(ValueError, match="span out of bounds"):
        gf._validate(
            mode="v2fx", prompt=None, duration_seconds=None,
            source_candidate_id="ps_1", source_in_seconds=0.0,
            source_out_seconds=40.0, variant_count=1,
        )


def test_validate_t2fx_duration_bounds():
    with pytest.raises(ValueError, match="duration_seconds out of bounds"):
        gf._validate(
            mode="t2fx", prompt=None, duration_seconds=0.5,
            source_candidate_id=None, source_in_seconds=None,
            source_out_seconds=None, variant_count=1,
        )
    with pytest.raises(ValueError, match="duration_seconds out of bounds"):
        gf._validate(
            mode="t2fx", prompt=None, duration_seconds=40.0,
            source_candidate_id=None, source_in_seconds=None,
            source_out_seconds=None, variant_count=1,
        )


# --- run() end-to-end: t2fx --------------------------------------------------


def _wait_for_terminal(project_dir, generation_id, timeout=5.0):
    """Block until the generation is no longer pending/running."""
    import scenecraft.plugin_api as plugin_api
    deadline = time.time() + timeout
    while time.time() < deadline:
        gen = plugin_api.get_foley_generation(project_dir, generation_id)
        if gen and gen["status"] in ("completed", "failed"):
            return gen
        time.sleep(0.05)
    raise TimeoutError(f"generation {generation_id} did not finish within {timeout}s")


def test_run_t2fx_happy_path(project_dir, with_token, fake_provider):
    result = gf.run(
        project_dir, "test_project",
        prompt="footsteps on gravel",
        duration_seconds=2.0,
    )
    assert result["mode"] == "t2fx"
    assert result["status"] == "pending"
    assert result["generation_id"].startswith("fgen_")

    # Wait for worker
    gen = _wait_for_terminal(project_dir, result["generation_id"])
    assert gen["status"] == "completed"

    # Tracks row exists
    import scenecraft.plugin_api as plugin_api
    tracks = plugin_api.get_foley_generation_tracks(project_dir, result["generation_id"])
    assert len(tracks) == 1
    assert tracks[0]["replicate_prediction_id"] == "pred_fake"
    assert tracks[0]["spend_ledger_id"] == "ledger_fake"
    assert tracks[0]["variant_index"] == 0

    # Provider was called with the prompt + duration (t2fx, no video)
    call = fake_provider["calls"][0]
    assert call["model"] == "zsxkib/mmaudio"
    assert call["source"] == "generate-foley"
    assert call["input"]["prompt"] == "footsteps on gravel"
    assert call["input"]["duration"] == 2.0
    assert "video" not in call["input"]

    # pool_segment was created with variant_kind=foley
    seg = plugin_api.get_pool_segment(project_dir, tracks[0]["pool_segment_id"])
    assert seg is not None
    # Note: _row_to_pool_segment uses camelCase and doesn't include variant_kind
    # in the dict, so verify via raw SQL.
    assert seg["createdBy"] == "plugin:generate-foley"
    assert seg["kind"] == "generated"
    conn = get_db(project_dir)
    vk = conn.execute(
        "SELECT variant_kind FROM pool_segments WHERE id = ?",
        (tracks[0]["pool_segment_id"],),
    ).fetchone()[0]
    assert vk == "foley"


def test_run_t2fx_optional_prompt_passes_empty(project_dir, with_token, fake_provider):
    result = gf.run(project_dir, "test_project", duration_seconds=2.0)
    gen = _wait_for_terminal(project_dir, result["generation_id"])
    assert gen["status"] == "completed"
    call = fake_provider["calls"][0]
    assert "prompt" not in call["input"]  # optional — omitted when empty


# --- run() error paths -----------------------------------------------------


def test_run_replicate_prediction_failed_sets_failed_status(
    project_dir, with_token, fake_provider
):
    from scenecraft.plugin_api.providers.replicate import ReplicatePredictionFailed

    fake_provider["result"] = ReplicatePredictionFailed(
        prediction_id="pred_oops", error="model exploded"
    )

    result = gf.run(project_dir, "test_project", prompt="x", duration_seconds=2.0)
    gen = _wait_for_terminal(project_dir, result["generation_id"])
    assert gen["status"] == "failed"
    assert "model exploded" in gen["error"]


def test_run_download_failed_preserves_spend_info(
    project_dir, with_token, fake_provider
):
    from scenecraft.plugin_api.providers.replicate import ReplicateDownloadFailed

    fake_provider["result"] = ReplicateDownloadFailed(
        prediction_id="pred_dl", spend_ledger_id="ledger_charged",
    )

    result = gf.run(project_dir, "test_project", prompt="x", duration_seconds=2.0)
    gen = _wait_for_terminal(project_dir, result["generation_id"])
    assert gen["status"] == "failed"
    assert "ledger_charged" in gen["error"]
    assert "prediction charged" in gen["error"]
    assert "Retry will re-charge" in gen["error"]


def test_run_not_configured_fails_cleanly(project_dir, without_token, fake_provider):
    from scenecraft.plugin_api.providers.replicate import ReplicateNotConfigured

    fake_provider["result"] = ReplicateNotConfigured("no token")

    result = gf.run(project_dir, "test_project", prompt="x", duration_seconds=2.0)
    gen = _wait_for_terminal(project_dir, result["generation_id"])
    assert gen["status"] == "failed"
    assert "no token" in gen["error"]


# --- run() v2fx -----------------------------------------------------------


def test_run_v2fx_calls_provider_with_data_uri_video(
    project_dir, with_token, fake_provider, monkeypatch, tmp_path
):
    # Create a fake source pool_segment to resolve the candidate
    fake_src_path = tmp_path / "pool" / "segments" / "src_video.mp4"
    fake_src_path.parent.mkdir(parents=True, exist_ok=True)
    fake_src_path.write_bytes(b"FAKE_VIDEO_BYTES_0123456789")

    src_seg_id = add_pool_segment(
        project_dir,
        pool_path="pool/segments/src_video.mp4",
        kind="imported",
        created_by="test",
    )

    # Stub pretrim.trim_to_range to return a fake trimmed file (avoids ffmpeg)
    trimmed = tmp_path / "trimmed.mp4"
    trimmed.write_bytes(b"TRIMMED_VIDEO_0123456789")

    def fake_trim(*, source_path, in_seconds, out_seconds, output_path=None):
        return trimmed

    from scenecraft.plugins.generate_foley import pretrim as pretrim_mod
    monkeypatch.setattr(pretrim_mod, "trim_to_range", fake_trim)

    result = gf.run(
        project_dir, "test_project",
        prompt="door slam",
        source_candidate_id=src_seg_id,
        source_in_seconds=1.0,
        source_out_seconds=3.0,
        entity_type="transition",
        entity_id="tr_abc",
    )
    assert result["mode"] == "v2fx"
    gen = _wait_for_terminal(project_dir, result["generation_id"])
    assert gen["status"] == "completed", f"error: {gen.get('error')}"

    # Provider was called with a data URI video input
    call = fake_provider["calls"][0]
    assert call["input"]["video"].startswith("data:video/mp4;base64,")
    assert call["input"]["prompt"] == "door slam"
    # Duration = out - in
    assert call["input"]["duration"] == 2.0


def test_run_v2fx_missing_candidate_raises(project_dir, with_token):
    with pytest.raises(ValueError, match="v2fx mode requires"):
        gf.run(
            project_dir, "test_project",
            mode="v2fx",  # explicit
            source_in_seconds=1.0,
            source_out_seconds=3.0,
        )


# --- resume_in_flight -----------------------------------------------------


def test_resume_in_flight_marks_unstartable_as_failed(project_dir, with_token):
    """A generation stuck in 'pending' with NO prediction_id is unrecoverable."""
    import scenecraft.plugin_api as plugin_api

    plugin_api.add_foley_generation(
        project_dir,
        generation_id="fgen_orphan",
        mode="t2fx",
        model="zsxkib/mmaudio",
        status="pending",
    )
    # No track row → no prediction_id → can't resume

    reattached = gf.resume_in_flight(project_dir)
    assert "fgen_orphan" not in reattached

    gen = plugin_api.get_foley_generation(project_dir, "fgen_orphan")
    assert gen["status"] == "failed"
    assert "server restart" in gen["error"]


# --- Helpers ---------------------------------------------------------------


def test_file_to_data_uri_encodes_correctly(tmp_path):
    p = tmp_path / "tiny.mp4"
    p.write_bytes(b"hello world")
    uri = gf._file_to_data_uri(p, mime="video/mp4")
    assert uri == "data:video/mp4;base64,aGVsbG8gd29ybGQ="
