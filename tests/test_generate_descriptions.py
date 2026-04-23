"""Tests for the ``generate_descriptions`` chat tool (Phase 3 LLM analysis).

Real Gemini calls are slow + flaky + cost money, so we monkeypatch both the
chunk splitter and the structured-description helper. The chat-tool layer is
the target of these tests; the helper itself is covered manually.

Covers:
- Tool registered in TOOLS, schema has required fields.
- First call writes rows (cached=False), subsequent same-cache-key call hits
  the cache (cached=True, same run_id, no new rows).
- force_rerun=True replaces the old run.
- Mocked helper returns invalid JSON (None) → chunk is skipped, others land.
- Missing source_segment → error, no DB writes.
- Missing on-disk file → error, no DB writes.
- _is_destructive("generate_descriptions") is False (allowlist carve-out).
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from scenecraft import audio_intelligence, chat as chat_mod
from scenecraft.chat import (
    GENERATE_DESCRIPTIONS_TOOL,
    TOOLS,
    _exec_generate_descriptions,
    _is_destructive,
)
from scenecraft.db import add_pool_segment, get_db


SAMPLE_RATE = 22050


def _write_sine_wav(path: Path, duration_s: float = 2.0, freq_hz: float = 440.0) -> None:
    """Write a short mono sine-wave WAV to ``path``."""
    import wave
    n = int(SAMPLE_RATE * duration_s)
    t = np.arange(n, dtype=np.float32) / SAMPLE_RATE
    y = (0.3 * np.sin(2 * np.pi * freq_hz * t)).astype(np.float32)
    pcm = (y * 32767.0).astype(np.int16)
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(SAMPLE_RATE)
        wf.writeframes(pcm.tobytes())


@pytest.fixture
def project_with_audio(tmp_path):
    """A project containing one pool_segment backed by a real WAV file."""
    project_dir = tmp_path / "desc_project"
    project_dir.mkdir()
    get_db(project_dir)  # force schema

    rel_path = "pool/segments/seg_test.wav"
    wav_abs = project_dir / rel_path
    _write_sine_wav(wav_abs, duration_s=2.0)

    seg_id = add_pool_segment(
        project_dir,
        kind="imported",
        created_by="test",
        pool_path=rel_path,
        original_filename="seg_test.wav",
        original_filepath=str(wav_abs),
        label="Sine test clip",
        duration_seconds=2.0,
    )
    return project_dir, seg_id


def _canned_chunks() -> list[dict]:
    """Three fixed chunks covering 0-30, 30-60, 60-75 seconds."""
    return [
        {"start_time": 0.0, "end_time": 30.0, "path": "/fake/chunk_000.mp3", "index": 0},
        {"start_time": 30.0, "end_time": 60.0, "path": "/fake/chunk_001.mp3", "index": 1},
        {"start_time": 60.0, "end_time": 75.0, "path": "/fake/chunk_002.mp3", "index": 2},
    ]


def _canned_description(idx: int) -> dict:
    """A valid structured description for chunk index ``idx``."""
    moods = ["dark", "uplifting", "reflective"]
    section_types = ["intro", "verse", "chorus"]
    return {
        "section_type": section_types[idx % len(section_types)],
        "mood": moods[idx % len(moods)],
        "energy": 0.25 + 0.25 * idx,
        "vocal_style": "sung" if idx % 2 == 0 else None,
        "instrumentation": ["acoustic_guitar", "male_vocals"] if idx == 0 else ["synthesizer"],
        "notes": f"chunk {idx} notes",
    }


@pytest.fixture
def patch_gemini(monkeypatch):
    """Default happy-path: chunks split into 3, each returns a valid description."""
    def fake_chunk(audio_path: str, chunk_duration: float = 30.0) -> list[dict]:
        return _canned_chunks()

    calls: list[dict] = []

    def fake_describe(chunk_path, start_time, end_time, *, model="gemini-2.5-pro", prompt_version="v1"):
        idx = int(Path(chunk_path).stem.rsplit("_", 1)[-1])
        calls.append({
            "path": chunk_path, "start": start_time, "end": end_time,
            "model": model, "prompt_version": prompt_version,
        })
        return _canned_description(idx)

    monkeypatch.setattr(audio_intelligence, "_chunk_audio_for_gemini", fake_chunk)
    monkeypatch.setattr(
        audio_intelligence, "_gemini_describe_chunk_structured", fake_describe,
    )
    return calls


# ── Registration ────────────────────────────────────────────────────────────


def test_generate_descriptions_is_registered_in_tools():
    names = {t["name"] for t in TOOLS}
    assert "generate_descriptions" in names


def test_generate_descriptions_tool_schema_has_required_fields():
    props = GENERATE_DESCRIPTIONS_TOOL["input_schema"]["properties"]
    assert "source_segment_id" in props
    assert "model" in props
    assert "chunk_size_s" in props
    assert "prompt_version" in props
    assert "force_rerun" in props
    assert GENERATE_DESCRIPTIONS_TOOL["input_schema"]["required"] == ["source_segment_id"]


def test_generate_descriptions_is_not_destructive():
    # "generate_" substring would match, but the allowlist carve-out keeps
    # description runs out of the confirmation gate.
    assert _is_destructive("generate_descriptions") is False
    # Sanity: other generate_ tools remain gated.
    assert _is_destructive("generate_keyframe_candidates") is True


# ── Happy path ──────────────────────────────────────────────────────────────


def test_first_call_writes_new_run(project_with_audio, patch_gemini):
    project_dir, seg_id = project_with_audio

    result = _exec_generate_descriptions(project_dir, {"source_segment_id": seg_id})

    assert "error" not in result, result
    assert result["cached"] is False
    assert result["source_segment_id"] == seg_id
    assert isinstance(result["run_id"], str) and result["run_id"]
    assert result["chunks_analyzed"] == 3
    assert result["chunks_failed"] == 0
    assert result["descriptions_written"] > 0

    # DB-level sanity: the run row + descriptions exist.
    conn = get_db(project_dir)
    assert conn.execute(
        "SELECT COUNT(*) FROM audio_description_runs WHERE id = ?",
        (result["run_id"],),
    ).fetchone()[0] == 1
    stored_rows = conn.execute(
        "SELECT COUNT(*) FROM audio_descriptions WHERE run_id = ?",
        (result["run_id"],),
    ).fetchone()[0]
    assert stored_rows == result["descriptions_written"]

    # Each chunk's structured fields produced distinct property rows.
    props_for_chunk0 = conn.execute(
        "SELECT property FROM audio_descriptions WHERE run_id = ? "
        "AND start_s = 0.0 ORDER BY property",
        (result["run_id"],),
    ).fetchall()
    prop_names = {r[0] for r in props_for_chunk0}
    assert "section_type" in prop_names
    assert "mood" in prop_names
    assert "energy" in prop_names


def test_second_call_returns_cached_run(project_with_audio, patch_gemini):
    project_dir, seg_id = project_with_audio

    first = _exec_generate_descriptions(project_dir, {"source_segment_id": seg_id})
    assert first["cached"] is False
    first_id = first["run_id"]

    conn = get_db(project_dir)
    rows_before = conn.execute(
        "SELECT COUNT(*) FROM audio_descriptions",
    ).fetchone()[0]
    runs_before = conn.execute(
        "SELECT COUNT(*) FROM audio_description_runs",
    ).fetchone()[0]

    second = _exec_generate_descriptions(project_dir, {"source_segment_id": seg_id})

    assert second["cached"] is True
    assert second["run_id"] == first_id
    assert second["descriptions_written"] == first["descriptions_written"]
    assert second["chunks_analyzed"] == first["chunks_analyzed"]

    # No new rows written.
    assert conn.execute(
        "SELECT COUNT(*) FROM audio_descriptions",
    ).fetchone()[0] == rows_before
    assert conn.execute(
        "SELECT COUNT(*) FROM audio_description_runs",
    ).fetchone()[0] == runs_before


def test_force_rerun_replaces_old_run(project_with_audio, patch_gemini):
    project_dir, seg_id = project_with_audio

    first = _exec_generate_descriptions(project_dir, {"source_segment_id": seg_id})
    first_id = first["run_id"]
    assert first["cached"] is False

    second = _exec_generate_descriptions(
        project_dir, {"source_segment_id": seg_id, "force_rerun": True},
    )
    assert second["cached"] is False
    assert second["run_id"] != first_id

    # Old run is gone; its descriptions cascaded.
    conn = get_db(project_dir)
    assert conn.execute(
        "SELECT COUNT(*) FROM audio_description_runs WHERE id = ?", (first_id,),
    ).fetchone()[0] == 0
    assert conn.execute(
        "SELECT COUNT(*) FROM audio_descriptions WHERE run_id = ?", (first_id,),
    ).fetchone()[0] == 0
    # New run has its own data.
    assert conn.execute(
        "SELECT COUNT(*) FROM audio_descriptions WHERE run_id = ?", (second["run_id"],),
    ).fetchone()[0] > 0


def test_chunk_returning_none_is_skipped(project_with_audio, monkeypatch):
    """When the structured helper returns None (invalid JSON / API error),
    that chunk is counted as failed but other chunks still land."""
    project_dir, seg_id = project_with_audio

    monkeypatch.setattr(
        audio_intelligence, "_chunk_audio_for_gemini",
        lambda audio_path, chunk_duration=30.0: _canned_chunks(),
    )

    def fake_describe(chunk_path, start_time, end_time, *, model="gemini-2.5-pro", prompt_version="v1"):
        idx = int(Path(chunk_path).stem.rsplit("_", 1)[-1])
        if idx == 1:
            # Simulate a parse failure / API error for chunk 1.
            return None
        return _canned_description(idx)

    monkeypatch.setattr(
        audio_intelligence, "_gemini_describe_chunk_structured", fake_describe,
    )

    result = _exec_generate_descriptions(project_dir, {"source_segment_id": seg_id})

    assert "error" not in result
    assert result["cached"] is False
    assert result["chunks_analyzed"] == 2
    assert result["chunks_failed"] == 1
    assert result["descriptions_written"] > 0

    # No rows in the (30, 60) range — the failed chunk produced nothing.
    conn = get_db(project_dir)
    skipped = conn.execute(
        "SELECT COUNT(*) FROM audio_descriptions "
        "WHERE run_id = ? AND start_s = 30.0",
        (result["run_id"],),
    ).fetchone()[0]
    assert skipped == 0


# ── Error paths ─────────────────────────────────────────────────────────────


def test_missing_source_segment_id_returns_error(project_with_audio, patch_gemini):
    project_dir, _seg_id = project_with_audio

    result = _exec_generate_descriptions(project_dir, {})
    assert "error" in result
    conn = get_db(project_dir)
    assert conn.execute(
        "SELECT COUNT(*) FROM audio_description_runs",
    ).fetchone()[0] == 0


def test_unknown_source_segment_returns_error(project_with_audio, patch_gemini):
    project_dir, _seg_id = project_with_audio

    result = _exec_generate_descriptions(
        project_dir, {"source_segment_id": "does_not_exist"},
    )
    assert "error" in result
    assert "pool_segment not found" in result["error"]
    conn = get_db(project_dir)
    assert conn.execute(
        "SELECT COUNT(*) FROM audio_description_runs",
    ).fetchone()[0] == 0


def test_missing_on_disk_file_returns_error(tmp_path, patch_gemini):
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

    result = _exec_generate_descriptions(project_dir, {"source_segment_id": seg_id})
    assert "error" in result
    assert "source file not found" in result["error"]
    conn = get_db(project_dir)
    assert conn.execute(
        "SELECT COUNT(*) FROM audio_description_runs",
    ).fetchone()[0] == 0
