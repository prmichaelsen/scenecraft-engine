"""Tests for the ``analyze_master_bus`` chat tool (M15 task-6).

Covers the analysis half of the flow only — the frontend WS round-trip
(OfflineAudioContext render → /mix-render-upload) is stubbed by writing a
pre-rendered WAV directly to ``pool/mixes/<hash>.wav``.

Also note: on this branch ``db_mix_cache`` and ``mix_graph_hash`` are STUBS
(to be replaced at merge time with the sibling ``m15-mix-schema`` branch's
real SQLite-backed implementations). The stubs' in-memory state is cleared
between tests via the ``clean_mix_stub_state`` fixture.

Test cases:
- Registration: ANALYZE_MASTER_BUS_TOOL appears in TOOLS, schema shape.
- Destructiveness: _is_destructive("analyze_master_bus") is False.
- Happy path: sine-at-0.5 WAV → peak ~ -6 dBFS, LUFS defined, RMS datapoints
  written, dynamic_range present.
- Cache hit: second call with same hash/window/sr returns cached=True, same
  run_id, no new rendered_path churn.
- force_rerun=True: new run_id, old run deleted from the stub store.
- Clipping-injected WAV: one merged clipping event.
- Silent WAV: low RMS, no clipping, no dynamic_range value persisted.
- Missing pool/mixes/<hash>.wav: error, no cached run written.
- Mismatched sample rate between requested and WAV on disk: error.
- Unknown analysis names: silently skipped.
- end_time_s=None resolves from audio_clips MAX(end_time).
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from scenecraft.chat import (
    ANALYZE_MASTER_BUS_TOOL,
    TOOLS,
    _exec_analyze_master_bus,
    _is_destructive,
)
from scenecraft.db import get_db


STUB_HASH = "0" * 64  # current stub value of compute_mix_graph_hash


# ── Helpers ─────────────────────────────────────────────────────────────────


def _write_wav_float(path: Path, y: np.ndarray, sr: int = 48000) -> None:
    """Write a float32/64 array to a 16-bit PCM WAV. Accepts mono (N,) or
    stereo (N, 2)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    import soundfile as sf
    sf.write(str(path), y.astype(np.float32), sr, subtype="PCM_16")


def _sine(duration_s: float, freq: float = 440.0, amp: float = 0.5, sr: int = 48000) -> np.ndarray:
    n = int(sr * duration_s)
    t = np.arange(n, dtype=np.float64) / sr
    return (amp * np.sin(2 * np.pi * freq * t)).astype(np.float32)


def _place_mix(project_dir: Path, y: np.ndarray, mix_hash: str = STUB_HASH, sr: int = 48000) -> Path:
    """Write a rendered WAV at pool/mixes/<mix_hash>.wav and return its path."""
    wav_abs = project_dir / "pool" / "mixes" / f"{mix_hash}.wav"
    _write_wav_float(wav_abs, y, sr=sr)
    return wav_abs


def _insert_audio_clip(project_dir: Path, *, start: float = 0.0, end: float = 3.0, clip_id: str = "c1") -> None:
    """Insert a minimal audio_clips row so _resolve_mix_end_time returns ``end``."""
    conn = get_db(project_dir)
    conn.execute(
        "INSERT INTO audio_clips (id, track_id, source_path, start_time, end_time) "
        "VALUES (?, ?, ?, ?, ?)",
        (clip_id, "t1", "dummy.wav", float(start), float(end)),
    )
    conn.commit()


# ── Fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def clean_mix_stub_state():
    """Reset the in-memory stub state between tests.

    The sibling m15-mix-schema branch replaces these with SQLite tables; once
    that lands this fixture becomes a no-op and the schema's own per-project
    isolation handles cleanup.
    """
    import scenecraft.db_mix_cache as dbmc
    dbmc._STUB_RUNS.clear()
    dbmc._STUB_BY_ID.clear()
    dbmc._STUB_DATAPOINTS.clear()
    dbmc._STUB_SECTIONS.clear()
    dbmc._STUB_SCALARS.clear()
    yield


@pytest.fixture
def project(tmp_path) -> Path:
    """A fresh project with initialized schema and one audio_clip
    (end_time=3.0) so default end_time_s resolves to 3.0."""
    project_dir = tmp_path / "mix_project"
    project_dir.mkdir()
    get_db(project_dir)  # force schema
    _insert_audio_clip(project_dir, start=0.0, end=3.0)
    return project_dir


# ── Registration ────────────────────────────────────────────────────────────


def test_analyze_master_bus_is_registered_in_tools():
    names = {t["name"] for t in TOOLS}
    assert "analyze_master_bus" in names


def test_analyze_master_bus_schema_shape():
    props = ANALYZE_MASTER_BUS_TOOL["input_schema"]["properties"]
    for field in ("start_time_s", "end_time_s", "sample_rate", "analyses", "force_rerun"):
        assert field in props
    # All inputs are optional — the tool can be invoked with {}
    assert ANALYZE_MASTER_BUS_TOOL["input_schema"]["required"] == []


def test_analyze_master_bus_is_not_destructive():
    # generate_/analyze_ substrings shouldn't gate this; the allowlist carves it out.
    assert _is_destructive("analyze_master_bus") is False
    # Sibling generate_dsp remains allowlisted too; generate_keyframe_candidates remains destructive.
    assert _is_destructive("generate_dsp") is False
    assert _is_destructive("generate_keyframe_candidates") is True


# ── Happy path ──────────────────────────────────────────────────────────────


def test_sine_wav_produces_expected_scalars(project):
    # 3s, 440Hz, amp=0.5 → peak ~ -6 dBFS.
    y = _sine(3.0, freq=440.0, amp=0.5, sr=48000)
    _place_mix(project, y)

    result = _exec_analyze_master_bus(project, {})

    assert "error" not in result, result
    assert result["cached"] is False
    assert isinstance(result["run_id"], str) and result["run_id"]
    assert result["mix_graph_hash"] == STUB_HASH
    assert result["start_time_s"] == 0.0
    assert result["end_time_s"] == 3.0
    assert result["rendered_path"] == f"pool/mixes/{STUB_HASH}.wav"

    scalars = result["scalars"]
    # 0.5 amplitude → -6.02 dBFS peak; allow small float slop.
    assert scalars["peak_db"] == pytest.approx(-6.02, abs=0.2)
    # true_peak on a clean sine should be within ~0.5 dB of peak.
    assert scalars["true_peak_db"] == pytest.approx(scalars["peak_db"], abs=0.5)
    # LUFS for a -6dB sine is around -9 LUFS on a BS.1770 meter.
    assert -14.0 < scalars["lufs_integrated"] < -5.0
    assert scalars["clip_count"] == 0.0
    # dynamic_range = peak - lufs, should be positive and finite.
    assert scalars["dynamic_range_db"] > 0

    assert result["clipping_events"] == 0
    # All default analyses should have fired.
    for a in ("peak", "true_peak", "rms", "lufs", "clipping_detect",
              "spectral_centroid", "dynamic_range"):
        assert a in result["analyses_written"]


def test_rms_and_centroid_datapoints_are_persisted(project):
    import scenecraft.db_mix_cache as dbmc

    y = _sine(2.0, freq=440.0, amp=0.5, sr=48000)
    _place_mix(project, y)

    result = _exec_analyze_master_bus(project, {})
    run_id = result["run_id"]
    pk_run_key = (str(project.resolve()), run_id)
    dps = dbmc._STUB_DATAPOINTS.get(pk_run_key, [])
    # Should have both rms and spectral_centroid datapoints.
    rms_rows = [d for d in dps if d[0] == "rms"]
    cent_rows = [d for d in dps if d[0] == "spectral_centroid"]
    assert len(rms_rows) > 10
    assert len(cent_rows) > 10
    # RMS should hover around 0.5 / sqrt(2) ≈ 0.354 for a 0.5-amp sine.
    mid_rms = np.median([r[2] for r in rms_rows])
    assert 0.2 < mid_rms < 0.5


# ── Cache semantics ─────────────────────────────────────────────────────────


def test_second_call_returns_cached(project):
    y = _sine(2.0, freq=440.0, amp=0.5, sr=48000)
    _place_mix(project, y)

    first = _exec_analyze_master_bus(project, {})
    assert first["cached"] is False
    second = _exec_analyze_master_bus(project, {})

    assert second["cached"] is True
    assert second["run_id"] == first["run_id"]
    # Scalars from the cached path must match.
    assert second["scalars"] == first["scalars"]
    assert second["clipping_events"] == first["clipping_events"]


def test_force_rerun_replaces_old_run(project):
    import scenecraft.db_mix_cache as dbmc

    y = _sine(2.0, freq=440.0, amp=0.5, sr=48000)
    _place_mix(project, y)

    first = _exec_analyze_master_bus(project, {})
    first_id = first["run_id"]
    second = _exec_analyze_master_bus(project, {"force_rerun": True})

    assert second["cached"] is False
    assert second["run_id"] != first_id
    # Old run should be gone from the stub store.
    pk = str(project.resolve())
    assert (pk, first_id) not in dbmc._STUB_BY_ID
    # New run should be present.
    assert (pk, second["run_id"]) in dbmc._STUB_BY_ID


# ── Clipping detection ──────────────────────────────────────────────────────


def test_clipping_injected_wav_detects_one_event(project):
    # 2s of a -12dB sine + a ~5-sample burst at 0.999 amplitude near t=1s.
    sr = 48000
    y = _sine(2.0, freq=440.0, amp=0.25, sr=sr).copy()
    injection_start = sr  # 1.0s
    y[injection_start:injection_start + 5] = 0.999
    _place_mix(project, y)

    result = _exec_analyze_master_bus(project, {})
    assert "error" not in result, result
    assert result["clipping_events"] == 1
    assert result["scalars"]["clip_count"] == 1.0


# ── Silence ─────────────────────────────────────────────────────────────────


def test_silent_wav_reports_no_errors(project):
    sr = 48000
    y = np.zeros(sr * 2, dtype=np.float32)
    _place_mix(project, y)

    result = _exec_analyze_master_bus(project, {})
    assert "error" not in result, result
    assert result["clipping_events"] == 0
    # peak_db is -inf for all-zeros; we don't require a specific scalar key to
    # exist but the tool should not crash persisting -inf.
    # RMS of silence is 0 across the board; datapoints may still be written.
    # dynamic_range cannot be computed from -inf values and must be skipped.
    assert "dynamic_range_db" not in result["scalars"]


# ── Error paths ─────────────────────────────────────────────────────────────


def test_missing_rendered_wav_returns_error(project):
    # Do not place any WAV at pool/mixes/<hash>.wav.
    result = _exec_analyze_master_bus(project, {})
    assert "error" in result
    assert "rendered mix WAV not found" in result["error"]
    # mix_graph_hash is still returned for debuggability.
    assert result["mix_graph_hash"] == STUB_HASH


def test_sample_rate_mismatch_returns_error(project):
    # WAV is 44100 but we ask for 48000.
    y = _sine(2.0, freq=440.0, amp=0.5, sr=44100)
    _place_mix(project, y, sr=44100)
    result = _exec_analyze_master_bus(project, {"sample_rate": 48000})
    assert "error" in result
    assert "sample rate" in result["error"].lower()


def test_invalid_start_time_returns_error(project):
    result = _exec_analyze_master_bus(project, {"start_time_s": "not a number"})
    assert "error" in result


def test_end_before_start_returns_error(project):
    y = _sine(2.0, freq=440.0, amp=0.5, sr=48000)
    _place_mix(project, y)
    result = _exec_analyze_master_bus(
        project, {"start_time_s": 5.0, "end_time_s": 1.0},
    )
    assert "error" in result


def test_no_audio_clips_default_end_time_errors(tmp_path):
    # Empty project: default end_time_s resolves to 0.0, which < start_time_s.
    project_dir = tmp_path / "empty_project"
    project_dir.mkdir()
    get_db(project_dir)  # schema only, no clips
    result = _exec_analyze_master_bus(project_dir, {})
    assert "error" in result


# ── Unknown analyses ────────────────────────────────────────────────────────


def test_unknown_analysis_names_are_skipped(project):
    y = _sine(2.0, freq=440.0, amp=0.5, sr=48000)
    _place_mix(project, y)
    result = _exec_analyze_master_bus(
        project,
        {"analyses": ["peak", "lufs", "bogus", "also_fake"]},
    )
    assert "error" not in result
    assert "peak" in result["analyses_written"]
    assert "lufs" in result["analyses_written"]
    assert "bogus" not in result["analyses_written"]
    assert "also_fake" not in result["analyses_written"]
    # Unrelated analyses should not appear.
    assert "rms" not in result["analyses_written"]


def test_explicit_end_time_overrides_audio_clips(project):
    # Project's audio_clips MAX(end_time) is 3.0, but we pass 1.5.
    y = _sine(1.5, freq=440.0, amp=0.5, sr=48000)
    _place_mix(project, y)
    result = _exec_analyze_master_bus(project, {"end_time_s": 1.5})
    assert "error" not in result
    assert result["end_time_s"] == 1.5
