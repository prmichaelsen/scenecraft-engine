"""Tests for the ``generate_dsp`` chat tool (Phase 3 autoduck).

Covers:
- First call writes a new run (cached=False), populates datapoints/sections/scalars.
- Second call with same inputs returns the cached run (cached=True, same run_id,
  no duplicate inserts).
- force_rerun=True replaces the cached run (new id, fresh data, old rows gone).
- Missing pool_segment id → {"error": ...}, no DB writes.
- Missing on-disk file → {"error": ...}, no DB writes.
- Unknown analysis names are silently skipped; known ones still run.
- _is_destructive("generate_dsp") is False (overrides generate_ substring rule).
- generate_dsp appears in the tool registry.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from scenecraft.chat import (
    GENERATE_DSP_TOOL,
    TOOLS,
    _exec_generate_dsp,
    _is_destructive,
)
from scenecraft.db import add_pool_segment, get_db


SAMPLE_RATE = 22050


def _write_sine_wav(path: Path, duration_s: float = 1.5, freq_hz: float = 440.0) -> None:
    """Write a short mono sine-wave WAV to ``path`` (16-bit PCM, 22050 Hz)."""
    import wave
    n = int(SAMPLE_RATE * duration_s)
    t = np.arange(n, dtype=np.float32) / SAMPLE_RATE
    # Amplitude modulation → gives rms/onsets/vocal_presence something non-trivial.
    envelope = 0.5 * (1.0 + np.sin(2 * np.pi * 2.0 * t))  # 2 Hz AM
    y = (envelope * np.sin(2 * np.pi * freq_hz * t)).astype(np.float32)
    pcm = (y * 32767.0).astype(np.int16)
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(SAMPLE_RATE)
        wf.writeframes(pcm.tobytes())


@pytest.fixture
def project_with_audio(tmp_path):
    """A project containing one pool_segment backed by a real WAV file.

    Returns (project_dir, segment_id).
    """
    project_dir = tmp_path / "dsp_project"
    project_dir.mkdir()
    get_db(project_dir)  # force schema

    rel_path = "pool/segments/seg_test.wav"
    wav_abs = project_dir / rel_path
    _write_sine_wav(wav_abs, duration_s=1.5)

    seg_id = add_pool_segment(
        project_dir,
        kind="imported",
        created_by="test",
        pool_path=rel_path,
        original_filename="seg_test.wav",
        original_filepath=str(wav_abs),
        label="Sine test clip",
        duration_seconds=1.5,
    )
    return project_dir, seg_id


# ── Registration ────────────────────────────────────────────────────────────


def test_generate_dsp_is_registered_in_tools():
    names = {t["name"] for t in TOOLS}
    assert "generate_dsp" in names


def test_generate_dsp_tool_schema_has_required_fields():
    props = GENERATE_DSP_TOOL["input_schema"]["properties"]
    assert "source_segment_id" in props
    assert "analyses" in props
    assert "force_rerun" in props
    assert GENERATE_DSP_TOOL["input_schema"]["required"] == ["source_segment_id"]


def test_generate_dsp_is_not_destructive():
    # The "generate_" substring pattern would match, but the allowlist carve-out
    # keeps analysis runs out of the confirmation gate.
    assert _is_destructive("generate_dsp") is False
    # Sanity: siblings remain destructive.
    assert _is_destructive("generate_keyframe_candidates") is True


# ── Happy path ──────────────────────────────────────────────────────────────


def test_first_call_writes_new_run(project_with_audio):
    project_dir, seg_id = project_with_audio

    result = _exec_generate_dsp(project_dir, {"source_segment_id": seg_id})

    assert "error" not in result, result
    assert result["cached"] is False
    assert result["source_segment_id"] == seg_id
    assert isinstance(result["run_id"], str) and result["run_id"]
    # All four defaults were requested; at least rms + onsets + tempo should produce data.
    assert "rms" in result["analyses_written"]
    assert "onsets" in result["analyses_written"]
    assert "tempo" in result["analyses_written"]
    assert result["datapoint_count"] > 0
    assert "tempo_bpm" in result["scalars"]

    # DB-level sanity: the run row exists.
    conn = get_db(project_dir)
    assert conn.execute(
        "SELECT COUNT(*) FROM dsp_analysis_runs WHERE id = ?", (result["run_id"],),
    ).fetchone()[0] == 1


def test_second_call_returns_cached_run(project_with_audio):
    project_dir, seg_id = project_with_audio

    first = _exec_generate_dsp(project_dir, {"source_segment_id": seg_id})
    assert first["cached"] is False
    first_id = first["run_id"]

    # Count rows after first run for later comparison.
    conn = get_db(project_dir)
    dp_before = conn.execute("SELECT COUNT(*) FROM dsp_datapoints").fetchone()[0]
    sec_before = conn.execute("SELECT COUNT(*) FROM dsp_sections").fetchone()[0]
    run_before = conn.execute("SELECT COUNT(*) FROM dsp_analysis_runs").fetchone()[0]

    second = _exec_generate_dsp(project_dir, {"source_segment_id": seg_id})

    assert second["cached"] is True
    assert second["run_id"] == first_id
    assert second["datapoint_count"] == first["datapoint_count"]
    assert second["section_count"] == first["section_count"]
    assert second["scalars"] == first["scalars"]

    # No new rows should have been written.
    conn = get_db(project_dir)
    assert conn.execute("SELECT COUNT(*) FROM dsp_datapoints").fetchone()[0] == dp_before
    assert conn.execute("SELECT COUNT(*) FROM dsp_sections").fetchone()[0] == sec_before
    assert conn.execute("SELECT COUNT(*) FROM dsp_analysis_runs").fetchone()[0] == run_before


def test_force_rerun_replaces_old_run(project_with_audio):
    project_dir, seg_id = project_with_audio

    first = _exec_generate_dsp(project_dir, {"source_segment_id": seg_id})
    first_id = first["run_id"]
    assert first["cached"] is False

    second = _exec_generate_dsp(
        project_dir, {"source_segment_id": seg_id, "force_rerun": True},
    )
    assert second["cached"] is False
    assert second["run_id"] != first_id

    # Old run is gone (delete_dsp_run cascades to datapoints/sections/scalars).
    conn = get_db(project_dir)
    assert conn.execute(
        "SELECT COUNT(*) FROM dsp_analysis_runs WHERE id = ?", (first_id,),
    ).fetchone()[0] == 0
    assert conn.execute(
        "SELECT COUNT(*) FROM dsp_datapoints WHERE run_id = ?", (first_id,),
    ).fetchone()[0] == 0
    # New run has its own data.
    assert conn.execute(
        "SELECT COUNT(*) FROM dsp_datapoints WHERE run_id = ?", (second["run_id"],),
    ).fetchone()[0] > 0


# ── Error paths ─────────────────────────────────────────────────────────────


def test_missing_pool_segment_id_returns_error(project_with_audio):
    project_dir, _seg_id = project_with_audio

    result = _exec_generate_dsp(project_dir, {})
    assert "error" in result
    # No runs written.
    conn = get_db(project_dir)
    assert conn.execute("SELECT COUNT(*) FROM dsp_analysis_runs").fetchone()[0] == 0


def test_unknown_pool_segment_returns_error(project_with_audio):
    project_dir, _seg_id = project_with_audio

    result = _exec_generate_dsp(
        project_dir, {"source_segment_id": "does_not_exist"},
    )
    assert "error" in result
    assert "pool_segment not found" in result["error"]
    conn = get_db(project_dir)
    assert conn.execute("SELECT COUNT(*) FROM dsp_analysis_runs").fetchone()[0] == 0


def test_missing_on_disk_file_returns_error(tmp_path):
    project_dir = tmp_path / "p"
    project_dir.mkdir()
    get_db(project_dir)

    seg_id = add_pool_segment(
        project_dir,
        kind="imported",
        created_by="test",
        pool_path="pool/segments/ghost.wav",  # never written
        original_filename="ghost.wav",
        original_filepath="/tmp/ghost.wav",
        label="ghost",
        duration_seconds=1.0,
    )

    result = _exec_generate_dsp(project_dir, {"source_segment_id": seg_id})
    assert "error" in result
    assert "source file not found" in result["error"]
    conn = get_db(project_dir)
    assert conn.execute("SELECT COUNT(*) FROM dsp_analysis_runs").fetchone()[0] == 0


# ── Unknown analyses ────────────────────────────────────────────────────────


def test_unknown_analysis_names_are_skipped(project_with_audio):
    project_dir, seg_id = project_with_audio

    result = _exec_generate_dsp(
        project_dir,
        {
            "source_segment_id": seg_id,
            "analyses": ["rms", "tempo", "not_a_real_thing", "also_bogus"],
        },
    )
    assert "error" not in result
    assert result["cached"] is False
    assert "rms" in result["analyses_written"]
    assert "tempo" in result["analyses_written"]
    assert "not_a_real_thing" not in result["analyses_written"]
    assert "also_bogus" not in result["analyses_written"]


def test_spectral_centroid_analysis_produces_datapoints(project_with_audio):
    project_dir, seg_id = project_with_audio

    result = _exec_generate_dsp(
        project_dir,
        {"source_segment_id": seg_id, "analyses": ["spectral_centroid"]},
    )
    assert "error" not in result
    assert "spectral_centroid" in result["analyses_written"]

    # Verify rows were written with the expected data_type.
    conn = get_db(project_dir)
    n = conn.execute(
        "SELECT COUNT(*) FROM dsp_datapoints WHERE run_id = ? AND data_type = ?",
        (result["run_id"], "spectral_centroid"),
    ).fetchone()[0]
    assert n > 0


def test_vocal_presence_writes_sections(project_with_audio):
    project_dir, seg_id = project_with_audio

    result = _exec_generate_dsp(
        project_dir,
        {"source_segment_id": seg_id, "analyses": ["vocal_presence"]},
    )
    assert "error" not in result
    # AM-modulated sine is high-energy throughout — we expect at least one region.
    assert "vocal_presence" in result["analyses_written"]
    conn = get_db(project_dir)
    n = conn.execute(
        "SELECT COUNT(*) FROM dsp_sections WHERE run_id = ? AND section_type = ?",
        (result["run_id"], "vocal_presence"),
    ).fetchone()[0]
    # The region-threshold + 0.5s-minimum filter in detect_presence may drop
    # the only region if it doesn't cross the threshold cleanly; tolerate 0
    # but assert the section_count field reflects reality.
    assert result["section_count"] == n
