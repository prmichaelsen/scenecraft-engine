"""Regression tests for local.engine-db-analysis-caches.md.

Covers the five cache systems (DSP, mix, audio-description, bounce, waveform-peaks):

- Unit section: one test per Base/Edge case. Docstrings open with
  `covers Rn[, Rm, OQ-K]`. Target-state OQ resolutions that require post-M16
  infrastructure (CLI subcommands, SHA-256 peaks migration, startup sweep,
  LRU cap, stat-check on hit) are marked
  `@pytest.mark.xfail(reason="target-state; awaits M16/M17 infrastructure",
  strict=False)` — they must PASS if the engine already ships them.

- E2E section (`class TestEndToEnd`): exercises the only cache with a real
  HTTP boundary today — the waveform-peaks filesystem cache via
  `GET /api/projects/:name/audio-clips/:id/peaks`. The four DB-backed caches
  (DSP, mix, audio-description, bounce) are WS-only today; their HTTP e2e
  coverage is marked `xfail(strict=False)` pending the M16 FastAPI refactor.
"""
from __future__ import annotations

import hashlib
import os
import sqlite3
import threading
import time
import wave
from pathlib import Path
from unittest import mock

import pytest

from scenecraft import db as scdb
from scenecraft import db_analysis_cache as dac
from scenecraft import db_bounces as dbc
from scenecraft import db_mix_cache as dmc
from scenecraft.audio import peaks as peaks_mod


# ---------------------------------------------------------------------------
# Domain-scoped inline helpers (prefixed `_caches_` per task-74 directive)
# ---------------------------------------------------------------------------


def _caches_seed_pool_segment(project_dir: Path, *, pool_path: str = "pool/x.wav") -> str:
    return scdb.add_pool_segment(
        project_dir, kind="generated", created_by="test", pool_path=pool_path,
    )


def _caches_short_wav(path: Path, *, seconds: float = 0.25, sr: int = 16000) -> Path:
    """Write a tiny WAV for ffmpeg-driven peaks tests."""
    path.parent.mkdir(parents=True, exist_ok=True)
    n = int(sr * seconds)
    pcm = (b"\x10\x00" * n)  # small constant non-zero sample so peaks != 0
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(pcm)
    return path


def _caches_create_dsp_with_children(project_dir: Path, seg_id: str,
                                     analyzer_version: str = "v1",
                                     params_hash: str = "p1") -> str:
    run = dac.create_dsp_run(
        project_dir, seg_id, analyzer_version, params_hash,
        analyses=["rms"], created_at="2026-04-27T00:00:00Z",
    )
    dac.bulk_insert_dsp_datapoints(project_dir, run.id, [
        ("rms", 0.0, 0.1, None), ("rms", 0.5, 0.2, None), ("rms", 1.0, 0.3, None),
    ])
    dac.bulk_insert_dsp_sections(project_dir, run.id, [
        (0.0, 1.0, "chorus", "A", 0.9),
    ])
    dac.set_dsp_scalars(project_dir, run.id, {"lufs": -14.2, "peak": -1.0})
    return run.id


def _caches_create_mix_with_children(project_dir: Path,
                                     *, h: str = "h1", start: float = 0.0,
                                     end: float = 30.0, sr: int = 48000,
                                     ver: str = "mix-librosa-0.10.2",
                                     rendered_path: str | None = None) -> str:
    run = dmc.create_mix_run(
        project_dir, h, start, end, sr, ver,
        analyses=["lufs"], rendered_path=rendered_path,
        created_at="2026-04-27T00:00:00Z",
    )
    dmc.bulk_insert_mix_datapoints(project_dir, run.id, [
        ("lufs", 0.0, -14.0, None), ("lufs", 1.0, -13.8, None),
    ])
    dmc.bulk_insert_mix_sections(project_dir, run.id, [(0.0, 30.0, "full", None, None)])
    dmc.set_mix_scalars(project_dir, run.id, {"integrated": -14.2})
    return run.id


def _caches_create_desc_with_children(project_dir: Path, seg_id: str,
                                      model: str = "gemini-1.5",
                                      prompt_version: str = "v2") -> str:
    run = dac.create_audio_description_run(
        project_dir, seg_id, model, prompt_version, 4.0, "2026-04-27T00:00:00Z",
    )
    dac.bulk_insert_audio_descriptions(project_dir, run.id, [
        (0.0, 1.0, "mood", "bright", None, 0.9, None),
    ])
    dac.set_audio_description_scalars(project_dir, run.id, [
        ("genre", "electronic", None, 0.95),
    ])
    return run.id


# ===========================================================================
# UNIT SECTION — Base Cases
# ===========================================================================


# ---- DSP cache ----

def test_dsp_lookup_miss_returns_none(project_dir: Path, db_conn):
    """covers R7 — dsp-lookup-miss-returns-none."""
    # Given: empty DB
    # When
    got = dac.get_dsp_run(project_dir, "seg-x", "v1", "p1")
    # Then
    assert got is None, "returns-none: empty table lookup must be None (not [])"
    # no-children-queried: nothing inserted as a side effect
    assert dac.query_dsp_datapoints(project_dir, "any", "rms") == [], \
        "no-children-queried: lookup must not write datapoints"


def test_dsp_lookup_hit_returns_row(project_dir: Path, db_conn):
    """covers R7, R11 — dsp-lookup-hit-returns-row."""
    # Given
    seg = _caches_seed_pool_segment(project_dir)
    run_id = _caches_create_dsp_with_children(project_dir, seg, "v1", "p1")
    # When
    got = dac.get_dsp_run(project_dir, seg, "v1", "p1")
    # Then
    assert got is not None and got.id == run_id, "returns-row: cached row identity matches"
    assert got.analyzer_version == "v1" and got.params_hash == "p1", \
        "columns-verbatim: stored columns returned as-is"


def test_dsp_duplicate_key_rejected(project_dir: Path, db_conn):
    """covers R1 — dsp-duplicate-key-rejected."""
    # Given
    seg = _caches_seed_pool_segment(project_dir)
    dac.create_dsp_run(project_dir, seg, "v1", "p1", [], "2026-04-27T00:00:00Z")
    # When / Then
    with pytest.raises(sqlite3.IntegrityError):
        dac.create_dsp_run(project_dir, seg, "v1", "p1", [], "2026-04-27T00:00:01Z")
    # no-new-row
    assert len(dac.list_dsp_runs(project_dir, seg)) == 1, \
        "no-new-row: duplicate insert does not produce a second row"


def test_dsp_delete_cascades_children(project_dir: Path, db_conn):
    """covers R5 — dsp-delete-cascades-children."""
    # Given
    seg = _caches_seed_pool_segment(project_dir)
    run_id = _caches_create_dsp_with_children(project_dir, seg)
    # When
    dac.delete_dsp_run(project_dir, run_id)
    # Then
    assert dac.get_dsp_run(project_dir, seg, "v1", "p1") is None, "parent-gone"
    assert dac.query_dsp_datapoints(project_dir, run_id, "rms") == [], "datapoints-gone"
    assert dac.query_dsp_sections(project_dir, run_id) == [], "sections-gone"
    assert dac.get_dsp_scalars(project_dir, run_id) == {}, "scalars-gone"


# ---- Mix cache ----

def test_mix_lookup_hit_returns_row(project_dir: Path, db_conn):
    """covers R8, R11 — mix-lookup-hit-returns-row."""
    # Given
    _caches_create_mix_with_children(project_dir)
    # When
    got = dmc.get_mix_run(project_dir, "h1", 0.0, 30.0, 48000, "mix-librosa-0.10.2")
    # Then
    assert got is not None, "returns-row: full 5-tuple match returns the row"
    assert got.mix_graph_hash == "h1" and got.sample_rate == 48000, \
        "columns-verbatim"


def test_mix_lookup_miss_on_window_change(project_dir: Path, db_conn):
    """covers R2, R8 — mix-lookup-miss-on-window-change."""
    # Given
    _caches_create_mix_with_children(project_dir, start=0.0, end=30.0)
    # When
    miss = dmc.get_mix_run(project_dir, "h1", 0.0, 45.0, 48000, "mix-librosa-0.10.2")
    hit = dmc.get_mix_run(project_dir, "h1", 0.0, 30.0, 48000, "mix-librosa-0.10.2")
    # Then
    assert miss is None, "returns-none: different end_time_s is a miss"
    assert hit is not None, "original-row-untouched: original key still hits"


def test_mix_lookup_miss_on_sample_rate_change(project_dir: Path, db_conn):
    """covers R2, R8 — mix-lookup-miss-on-sample-rate-change."""
    # Given
    _caches_create_mix_with_children(project_dir, sr=48000)
    # When
    got = dmc.get_mix_run(project_dir, "h1", 0.0, 30.0, 44100, "mix-librosa-0.10.2")
    # Then
    assert got is None, "returns-none: differing sample_rate is a cache miss"


def test_mix_analyzer_version_miss_keeps_old_row(project_dir: Path, db_conn):
    """covers R13, R14 — mix-analyzer-version-miss-keeps-old-row."""
    # Given
    _caches_create_mix_with_children(project_dir, ver="mix-librosa-0.10.2")
    # When
    new_ver_lookup = dmc.get_mix_run(project_dir, "h1", 0.0, 30.0, 48000, "mix-librosa-0.10.3")
    old_ver_lookup = dmc.get_mix_run(project_dir, "h1", 0.0, 30.0, 48000, "mix-librosa-0.10.2")
    # Then
    assert new_ver_lookup is None, "returns-none-on-new-version"
    assert old_ver_lookup is not None, "old-row-still-readable"
    # Also assert A/B coexistence
    dmc.create_mix_run(project_dir, "h1", 0.0, 30.0, 48000, "mix-librosa-0.10.3",
                      [], None, "2026-04-27T00:01:00Z")
    all_for_h = dmc.list_mix_runs_for_hash(project_dir, "h1")
    assert len(all_for_h) == 2, "list-contains-both: both analyzer_versions coexist"


def test_mix_duplicate_key_rejected(project_dir: Path, db_conn):
    """covers R2 — mix-duplicate-key-rejected."""
    _caches_create_mix_with_children(project_dir)
    with pytest.raises(sqlite3.IntegrityError):
        dmc.create_mix_run(project_dir, "h1", 0.0, 30.0, 48000, "mix-librosa-0.10.2",
                          [], None, "2026-04-27T00:01:00Z")


def test_mix_delete_cascades_children(project_dir: Path, db_conn):
    """covers R5 — mix-delete-cascades-children."""
    run_id = _caches_create_mix_with_children(project_dir)
    # Simulate rendered WAV on disk (not touched by delete)
    wav_path = project_dir / "pool" / "mixes" / "h1.wav"
    wav_path.parent.mkdir(parents=True, exist_ok=True)
    wav_path.write_bytes(b"RIFFstub")
    # When
    dmc.delete_mix_run(project_dir, run_id)
    # Then
    assert dmc.get_mix_run(project_dir, "h1", 0.0, 30.0, 48000, "mix-librosa-0.10.2") is None, \
        "parent-gone"
    assert dmc.query_mix_datapoints(project_dir, run_id, "lufs") == [], "datapoints-gone"
    assert dmc.query_mix_sections(project_dir, run_id) == [], "sections-gone"
    assert dmc.get_mix_scalars(project_dir, run_id) == {}, "scalars-gone"
    assert wav_path.exists(), "wav-file-not-touched: DAL delete does not remove rendered WAV"


# ---- Description cache ----

def test_desc_lookup_hit_returns_row(project_dir: Path, db_conn):
    """covers R9, R11 — desc-lookup-hit-returns-row."""
    seg = _caches_seed_pool_segment(project_dir)
    run_id = _caches_create_desc_with_children(project_dir, seg, "gemini-1.5", "v2")
    got = dac.get_audio_description_run(project_dir, seg, "gemini-1.5", "v2")
    assert got is not None and got.id == run_id, "returns-row"


def test_desc_prompt_version_miss_keeps_old_row(project_dir: Path, db_conn):
    """covers R3, R9, R14 — desc-prompt-version-miss-keeps-old-row."""
    seg = _caches_seed_pool_segment(project_dir)
    _caches_create_desc_with_children(project_dir, seg, prompt_version="v2")
    # Miss on v3
    assert dac.get_audio_description_run(project_dir, seg, "gemini-1.5", "v3") is None, \
        "returns-none: new prompt_version is a miss"
    # Old v2 still present
    assert dac.get_audio_description_run(project_dir, seg, "gemini-1.5", "v2") is not None, \
        "old-still-readable"
    # A/B coexist after v3 persists
    dac.create_audio_description_run(project_dir, seg, "gemini-1.5", "v3", 4.0,
                                     "2026-04-27T00:01:00Z")
    assert len(dac.list_audio_description_runs(project_dir, seg)) == 2, \
        "ab-preserved: both prompt_versions coexist"


def test_desc_duplicate_key_rejected(project_dir: Path, db_conn):
    """covers R3 — desc-duplicate-key-rejected."""
    seg = _caches_seed_pool_segment(project_dir)
    dac.create_audio_description_run(project_dir, seg, "gemini-1.5", "v2", 4.0,
                                     "2026-04-27T00:00:00Z")
    with pytest.raises(sqlite3.IntegrityError):
        dac.create_audio_description_run(project_dir, seg, "gemini-1.5", "v2", 4.0,
                                         "2026-04-27T00:01:00Z")


def test_desc_delete_cascades_children(project_dir: Path, db_conn):
    """covers R5 — desc-delete-cascades-children."""
    seg = _caches_seed_pool_segment(project_dir)
    run_id = _caches_create_desc_with_children(project_dir, seg)
    dac.delete_audio_description_run(project_dir, run_id)
    assert dac.query_audio_descriptions(project_dir, run_id) == [], "time-ranged-gone"
    assert dac.get_audio_description_scalars(project_dir, run_id) == [], "scalars-gone"


# ---- Bounce cache ----

def test_bounce_lookup_hit_returns_row(project_dir: Path, db_conn):
    """covers R10 — bounce-lookup-hit-returns-row."""
    # Given
    h = "abc" + "0" * 61  # 64 hex
    dbc.create_bounce(
        project_dir, composite_hash=h, start_time_s=0.0, end_time_s=30.0,
        mode="full", selection={}, sample_rate=48000, bit_depth=24,
        rendered_path=f"pool/bounces/{h}.wav",
    )
    # When
    got = dbc.get_bounce_by_hash(project_dir, h)
    # Then
    assert got is not None, "returns-row"
    assert got.rendered_path == f"pool/bounces/{h}.wav", "rendered-path-present"


def test_bounce_duplicate_hash_rejected(project_dir: Path, db_conn):
    """covers R4 — bounce-duplicate-hash-rejected."""
    h = "abc" + "0" * 61
    dbc.create_bounce(project_dir, composite_hash=h, start_time_s=0.0, end_time_s=30.0,
                     mode="full", selection={}, sample_rate=48000, bit_depth=24)
    with pytest.raises(sqlite3.IntegrityError):
        dbc.create_bounce(project_dir, composite_hash=h, start_time_s=0.0, end_time_s=30.0,
                         mode="full", selection={}, sample_rate=48000, bit_depth=24)


def test_bounce_delete_does_not_remove_wav(project_dir: Path, db_conn):
    """covers R31 — bounce-delete-does-not-remove-wav."""
    h = "abc" + "0" * 61
    wav = project_dir / "pool" / "bounces" / f"{h}.wav"
    wav.parent.mkdir(parents=True, exist_ok=True)
    wav.write_bytes(b"RIFFstub")
    b = dbc.create_bounce(project_dir, composite_hash=h, start_time_s=0.0, end_time_s=30.0,
                         mode="full", selection={}, sample_rate=48000, bit_depth=24,
                         rendered_path=f"pool/bounces/{h}.wav")
    dbc.delete_bounce(project_dir, b.id)
    assert dbc.get_bounce_by_id(project_dir, b.id) is None, "row-gone"
    assert wav.exists(), "wav-file-untouched: DB delete decoupled from filesystem"


def test_bounce_null_rendered_path_triggers_retry(project_dir: Path, db_conn):
    """covers R32, R15–R17 — bounce-null-rendered-path-triggers-retry.

    Spec states: a bounce row with ``rendered_path IS NULL`` is pending/failed
    and the producer MUST delete-then-recreate. This unit test exercises the
    semantic contract: null rendered_path row is cleared before a fresh insert
    would collide on UNIQUE. We emulate the producer's delete-then-create
    with the documented DAL calls.
    """
    h = "abc" + "0" * 61
    pending = dbc.create_bounce(project_dir, composite_hash=h, start_time_s=0.0,
                               end_time_s=30.0, mode="full", selection={},
                               sample_rate=48000, bit_depth=24, rendered_path=None)
    assert pending.rendered_path is None, "given: null rendered_path (pending)"
    # Simulated producer contract: delete the pending row, then create fresh
    dbc.delete_bounce(project_dir, pending.id)
    fresh = dbc.create_bounce(project_dir, composite_hash=h, start_time_s=0.0,
                             end_time_s=30.0, mode="full", selection={},
                             sample_rate=48000, bit_depth=24,
                             rendered_path=f"pool/bounces/{h}.wav")
    assert fresh.id != pending.id, "pending-row-deleted: new id, no UNIQUE conflict"
    got = dbc.get_bounce_by_hash(project_dir, h)
    assert got is not None and got.rendered_path is not None, "new-row-created"


# ---- Force rerun + transaction discipline ----

def test_force_rerun_deletes_then_creates(project_dir: Path, db_conn):
    """covers R16, R19, R20 — force-rerun-deletes-then-creates.

    Emulates the producer contract: on force_rerun=True + cache hit, delete
    old run (cascading children) BEFORE re-creating under the same 3-tuple.
    """
    seg = _caches_seed_pool_segment(project_dir)
    old_id = _caches_create_dsp_with_children(project_dir, seg, "v1", "p1")
    old_dp = dac.query_dsp_datapoints(project_dir, old_id, "rms")
    assert len(old_dp) == 3, "given: old run has children"
    # Producer contract under force_rerun
    dac.delete_dsp_run(project_dir, old_id)
    new_run = dac.create_dsp_run(project_dir, seg, "v1", "p1", ["rms"],
                                 "2026-04-27T00:05:00Z")
    dac.bulk_insert_dsp_datapoints(project_dir, new_run.id,
                                   [("rms", 0.0, 0.7, None)])
    # Then
    assert new_run.id != old_id, "new-row-created: different id"
    assert dac.query_dsp_datapoints(project_dir, old_id, "rms") == [], \
        "old-children-gone"
    assert len(dac.query_dsp_datapoints(project_dir, new_run.id, "rms")) == 1, \
        "new-children-present"


def test_force_rerun_on_miss_is_noop_plus_run(project_dir: Path, db_conn):
    """covers R18 — force-rerun-on-miss-is-noop-plus-run."""
    seg = _caches_seed_pool_segment(project_dir)
    # Cache miss — lookup returns None; producer contract is "no delete needed"
    assert dac.get_dsp_run(project_dir, seg, "v1", "p1") is None, "given: cache miss"
    # Producer creates the row directly; no exception, no predicate-only delete
    run = dac.create_dsp_run(project_dir, seg, "v1", "p1", [], "2026-04-27T00:00:00Z")
    assert dac.get_dsp_run(project_dir, seg, "v1", "p1") is not None, "new-row-created"
    assert run.id, "run-has-id"


def test_default_rerun_returns_cached(project_dir: Path, db_conn):
    """covers R17, R21 — default-rerun-returns-cached."""
    seg = _caches_seed_pool_segment(project_dir)
    run_id = _caches_create_dsp_with_children(project_dir, seg, "v1", "p1")
    # When: lookup again (force_rerun=False)
    got = dac.get_dsp_run(project_dir, seg, "v1", "p1")
    # Then
    assert got is not None and got.id == run_id, "cached-returned"
    assert len(dac.list_dsp_runs(project_dir, seg)) == 1, \
        "no-new-row: re-lookup does not create a duplicate"


def test_producer_exception_deletes_partial_row(project_dir: Path, db_conn):
    """covers R21 — producer-exception-deletes-partial-row.

    Simulates the documented try/except pattern: create run row, then raise
    during bulk-insert; finally path deletes the partial row.
    """
    seg = _caches_seed_pool_segment(project_dir)
    created_id = None
    try:
        run = dac.create_dsp_run(project_dir, seg, "v1", "p1", [],
                                 "2026-04-27T00:00:00Z")
        created_id = run.id
        # simulate producer failure mid-analysis
        raise RuntimeError("synthetic analysis failure")
    except RuntimeError:
        if created_id:
            dac.delete_dsp_run(project_dir, created_id)
    # Then
    assert dac.get_dsp_run(project_dir, seg, "v1", "p1") is None, "partial-row-deleted"
    assert dac.query_dsp_datapoints(project_dir, created_id, "rms") == [], "no-orphan-children"


def test_bulk_insert_is_idempotent(project_dir: Path, db_conn):
    """covers R6, R23 — bulk-insert-is-idempotent."""
    seg = _caches_seed_pool_segment(project_dir)
    run = dac.create_dsp_run(project_dir, seg, "v1", "p1", [], "2026-04-27T00:00:00Z")
    # First insert
    dac.bulk_insert_dsp_datapoints(project_dir, run.id,
                                   [("rms", 1.25, 0.9, None)])
    # Second insert: same (data_type, time_s) tuple, different value
    dac.bulk_insert_dsp_datapoints(project_dir, run.id,
                                   [("rms", 1.25, 0.42, None)])
    # Then
    dps = dac.query_dsp_datapoints(project_dir, run.id, "rms")
    assert len(dps) == 1, "no-duplicate: PRIMARY KEY is upserted"
    assert dps[0].value == pytest.approx(0.42), "value-overwritten"


def test_bulk_insert_empty_is_noop(project_dir: Path, db_conn):
    """covers R23 — bulk-insert-empty-is-noop."""
    seg = _caches_seed_pool_segment(project_dir)
    run = dac.create_dsp_run(project_dir, seg, "v1", "p1", [], "2026-04-27T00:00:00Z")
    n = dac.bulk_insert_dsp_datapoints(project_dir, run.id, [])
    assert n == 0, "returns-zero"
    assert dac.query_dsp_datapoints(project_dir, run.id, "rms") == [], "no-rows-inserted"


def test_pool_segment_delete_cascades_to_runs(project_dir: Path, db_conn):
    """covers R1, R3, R5 — pool-segment-delete-cascades-to-runs."""
    seg = _caches_seed_pool_segment(project_dir)
    dsp_id = _caches_create_dsp_with_children(project_dir, seg)
    desc_id = _caches_create_desc_with_children(project_dir, seg)
    # When
    conn = scdb.get_db(project_dir)
    conn.execute("DELETE FROM pool_segments WHERE id = ?", (seg,))
    conn.commit()
    # Then
    assert dac.get_dsp_run(project_dir, seg, "v1", "p1") is None, "dsp-run-gone"
    assert dac.get_audio_description_run(project_dir, seg, "gemini-1.5", "v2") is None, \
        "desc-run-gone"
    assert dac.query_dsp_datapoints(project_dir, dsp_id, "rms") == [], "dsp-children-gone"
    assert dac.query_audio_descriptions(project_dir, desc_id) == [], "desc-children-gone"


def test_list_on_empty_returns_empty_list(project_dir: Path, db_conn):
    """covers Behavior Row 30 — list-on-empty-returns-empty-list (negative)."""
    assert dac.list_dsp_runs(project_dir, "seg-any") == [], "dsp: []"
    assert dmc.list_mix_runs_for_hash(project_dir, "h-any") == [], "mix: []"
    assert dac.list_audio_description_runs(project_dir, "seg-any") == [], "desc: []"
    assert dbc.list_bounces(project_dir) == [], "bounce: []"


# ---- Peaks cache ----

def test_peaks_cache_hit_skips_decode(project_dir: Path, tmp_path: Path):
    """covers R26 — peaks-cache-hit-skips-decode."""
    # Given: pre-existing cache file at the expected key path
    src = _caches_short_wav(tmp_path / "src.wav")
    key = peaks_mod._cache_key(src, 0.0, 0.1, 400)
    cache_dir = project_dir / "audio_staging" / ".peaks"
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_file = cache_dir / f"{key}.f16"
    payload = b"\x01\x00" * 40
    cache_file.write_bytes(payload)
    # When: ensure ffmpeg never called
    with mock.patch("scenecraft.audio.peaks.subprocess.Popen") as popen:
        got = peaks_mod.compute_peaks(src, 0.0, 0.1, 400, project_dir=project_dir)
    # Then
    assert got == payload, "bytes-equal-file"
    popen.assert_not_called(), "no-ffmpeg-invocation"


def test_peaks_cache_miss_decodes_and_writes(project_dir: Path, tmp_path: Path):
    """covers R27 — peaks-cache-miss-decodes-and-writes."""
    src = _caches_short_wav(tmp_path / "src.wav", seconds=0.25)
    # When
    got = peaks_mod.compute_peaks(src, 0.0, 0.25, 400, project_dir=project_dir)
    # Then
    assert isinstance(got, bytes) and len(got) > 0, "bytes-returned"
    key = peaks_mod._cache_key(src, 0.0, 0.25, 400)
    cache_file = project_dir / "audio_staging" / ".peaks" / f"{key}.f16"
    assert cache_file.exists(), "cache-file-written"
    assert cache_file.read_bytes() == got, "cache-file-bytes-match-return"


def test_peaks_source_edit_invalidates_key(project_dir: Path, tmp_path: Path):
    """covers R25 — peaks-source-edit-invalidates-key."""
    src = _caches_short_wav(tmp_path / "src.wav", seconds=0.25)
    old_key = peaks_mod._cache_key(src, 0.0, 0.25, 400)
    peaks_mod.compute_peaks(src, 0.0, 0.25, 400, project_dir=project_dir)
    old_file = project_dir / "audio_staging" / ".peaks" / f"{old_key}.f16"
    assert old_file.exists(), "sanity: first write created old-key file"
    # When: edit source (re-write with different sample count → new mtime + size)
    time.sleep(0.01)  # ensure mtime_ns advances
    _caches_short_wav(src, seconds=0.30)
    new_key = peaks_mod._cache_key(src, 0.0, 0.25, 400)
    # Then
    assert new_key != old_key, "new-key-computed"
    peaks_mod.compute_peaks(src, 0.0, 0.25, 400, project_dir=project_dir)
    assert old_file.exists(), "old-file-lingers: no automatic cleanup"


# ===========================================================================
# UNIT SECTION — Edge Cases
# ===========================================================================


def test_peaks_cache_write_failure_is_non_fatal(project_dir: Path, tmp_path: Path, caplog):
    """covers R28 — peaks-cache-write-failure-is-non-fatal (negative)."""
    src = _caches_short_wav(tmp_path / "src.wav", seconds=0.25)
    with mock.patch("pathlib.Path.write_bytes",
                    side_effect=OSError("disk full")):
        # Then: does not raise
        got = peaks_mod.compute_peaks(src, 0.0, 0.25, 400, project_dir=project_dir)
    assert isinstance(got, bytes) and len(got) > 0, "bytes-still-returned"


def test_list_dsp_runs_ordering(project_dir: Path, db_conn):
    """covers list_dsp_runs ordering — list-dsp-runs-ordering.

    From db_analysis_cache.py:89 — `ORDER BY created_at DESC`.
    """
    seg = _caches_seed_pool_segment(project_dir)
    dac.create_dsp_run(project_dir, seg, "v1", "pa", [], "2026-04-27T00:00:01Z")
    dac.create_dsp_run(project_dir, seg, "v1", "pb", [], "2026-04-27T00:00:02Z")
    dac.create_dsp_run(project_dir, seg, "v1", "pc", [], "2026-04-27T00:00:03Z")
    got = dac.list_dsp_runs(project_dir, seg)
    assert [r.params_hash for r in got] == ["pc", "pb", "pa"], \
        "ordered-desc-by-created-at"


def test_peaks_key_format_precision(project_dir: Path, tmp_path: Path):
    """covers R25 — peaks-key-format-precision."""
    src = _caches_short_wav(tmp_path / "src.wav")
    k1 = peaks_mod._cache_key(src, 0.1, 0.25, 400)
    k2 = peaks_mod._cache_key(src, 0.100001, 0.25, 400)
    assert k1 != k2, "different-keys: 6-decimal format distinguishes inputs"


def test_bounce_selection_json_empty_for_full_mode(project_dir: Path, db_conn):
    """covers R4 — bounce-selection-json-empty-for-full-mode."""
    h = "def" + "0" * 61
    dbc.create_bounce(project_dir, composite_hash=h, start_time_s=0.0, end_time_s=30.0,
                     mode="full", selection={}, sample_rate=48000, bit_depth=24)
    got = dbc.get_bounce_by_hash(project_dir, h)
    assert got.mode == "full", "mode-full"
    assert got.selection == {}, "selection-empty: {} round-trips as {}"


def test_dsp_datapoint_extra_json_null_vs_object(project_dir: Path, db_conn):
    """covers R23 — dsp-datapoint-extra-json-null-vs-object."""
    seg = _caches_seed_pool_segment(project_dir)
    run = dac.create_dsp_run(project_dir, seg, "v1", "p1", [], "2026-04-27T00:00:00Z")
    dac.bulk_insert_dsp_datapoints(project_dir, run.id, [
        ("rms", 0.0, 0.1, None),
        ("rms", 1.0, 0.2, {"bin": 42}),
    ])
    dps = sorted(dac.query_dsp_datapoints(project_dir, run.id, "rms"),
                 key=lambda d: d.time_s)
    assert dps[0].extra is None, "null-extra-roundtrips: None (not 'null')"
    assert dps[1].extra == {"bin": 42}, "object-extra-roundtrips"


def test_concurrent_create_no_internal_lock(project_dir: Path, db_conn):
    """covers R37, OQ-5 — concurrent-create-no-internal-lock (negative).

    INV-1: no DAL-level lock is held across create_*_run. UNIQUE constraint
    is the only contention surface; a second concurrent caller with the
    same key gets IntegrityError.
    """
    seg = _caches_seed_pool_segment(project_dir)
    errors: list[Exception] = []
    successes: list[str] = []
    barrier = threading.Barrier(2)

    def _attempt(params_hash: str) -> None:
        try:
            barrier.wait(timeout=2.0)
            r = dac.create_dsp_run(project_dir, seg, "v1", params_hash, [],
                                   "2026-04-27T00:00:00Z")
            successes.append(r.id)
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    t1 = threading.Thread(target=_attempt, args=("p1",))
    t2 = threading.Thread(target=_attempt, args=("p1",))
    t1.start(); t2.start()
    t1.join(5.0); t2.join(5.0)
    # Exactly one wins; the other hits UNIQUE
    assert len(successes) == 1, "one-winner: UNIQUE is the only contention surface"
    assert len(errors) == 1 and isinstance(errors[0], sqlite3.IntegrityError), \
        "loser-sees-IntegrityError (no internal retry)"


# ---- Target-state tests (resolves OQ-1..OQ-7) ----

@pytest.mark.xfail(reason="target-state (R33/OQ-1): awaits `scenecraft cache prune` CLI",
                   strict=False)
def test_cache_prune_by_analyzer_version(project_dir: Path, db_conn):
    """covers R33, OQ-1 — cache-prune-by-analyzer-version."""
    # Given: two DSP rows at different analyzer_versions
    seg = _caches_seed_pool_segment(project_dir)
    dac.create_dsp_run(project_dir, seg, "dsp-librosa-0.10.2", "p1", [],
                       "2026-04-27T00:00:00Z")
    dac.create_dsp_run(project_dir, seg, "dsp-librosa-0.10.3", "p1", [],
                       "2026-04-27T00:00:01Z")
    # When: CLI subcommand
    from scenecraft.cli import cache_prune  # type: ignore[attr-defined]
    cache_prune(project_dir, analyzer_version="dsp-librosa-0.10.2")
    # Then
    remaining = [r.analyzer_version for r in dac.list_dsp_runs(project_dir, seg)]
    assert "dsp-librosa-0.10.2" not in remaining, "old-version-gone"
    assert "dsp-librosa-0.10.3" in remaining, "new-version-kept"


@pytest.mark.xfail(reason="target-state (R34/OQ-2): peaks SHA-256 migration not shipped; "
                          "current impl uses SHA-1[:16]", strict=False)
def test_hash_standardized_sha256_peaks_migration(project_dir: Path, tmp_path: Path):
    """covers R34, OQ-2 — hash-standardized-sha256-peaks-migration."""
    # Given
    src = _caches_short_wav(tmp_path / "src.wav")
    # When: after spec R34 is implemented, compute_peaks produces a 64-char SHA-256 key
    peaks_mod.compute_peaks(src, 0.0, 0.25, 400, project_dir=project_dir)
    cache_files = list((project_dir / "audio_staging" / ".peaks").glob("*.f16"))
    assert cache_files, "at least one cache file written"
    stem = cache_files[0].stem
    # Then
    assert len(stem) == 64, f"new-key-is-sha256: got {len(stem)}-char key (legacy was 16)"
    int(stem, 16)  # hex-parseable


@pytest.mark.xfail(reason="target-state (R35/OQ-3): startup sweep not shipped", strict=False)
def test_startup_sweep_deletes_partial_rows(project_dir: Path, db_conn):
    """covers R35, OQ-3 — startup-sweep-deletes-partial-rows."""
    seg = _caches_seed_pool_segment(project_dir)
    # 15-min-old partial (no children)
    dac.create_dsp_run(project_dir, seg, "v1", "old-partial", [],
                       "2026-04-27T00:00:00Z")
    # 2-min-old partial — should survive
    dac.create_dsp_run(project_dir, seg, "v1", "recent-partial", [],
                       "2026-04-27T00:13:00Z")
    # 15-min-old populated
    run_pop = dac.create_dsp_run(project_dir, seg, "v1", "old-populated", [],
                                 "2026-04-27T00:00:00Z")
    dac.bulk_insert_dsp_datapoints(project_dir, run_pop.id,
                                   [("rms", 0.0, 0.1, None)])
    # When
    from scenecraft.startup_sweep import run_partial_row_sweep  # type: ignore[attr-defined]
    run_partial_row_sweep(project_dir, now_iso="2026-04-27T00:15:00Z",
                          threshold_minutes=10)
    # Then
    got = {r.params_hash for r in dac.list_dsp_runs(project_dir, seg)}
    assert "old-partial" not in got, "old-partial-deleted"
    assert "recent-partial" in got, "recent-partial-kept"
    assert "old-populated" in got, "old-populated-kept"


@pytest.mark.xfail(reason="target-state (R36/OQ-4): LRU cap not shipped", strict=False)
def test_lru_cap_200_evicts_oldest(project_dir: Path, db_conn):
    """covers R36, OQ-4 — lru-cap-200-evicts-oldest."""
    for i in range(200):
        dbc.create_bounce(
            project_dir, composite_hash=f"{i:064d}", start_time_s=0.0,
            end_time_s=1.0, mode="full", selection={}, sample_rate=48000,
            bit_depth=24, created_at=f"2026-04-27T00:00:{i:02d}Z",
        )
    dbc.create_bounce(
        project_dir, composite_hash="z" * 64, start_time_s=0.0, end_time_s=1.0,
        mode="full", selection={}, sample_rate=48000, bit_depth=24,
        created_at="2026-04-27T99:99:99Z",
    )
    rows = dbc.list_bounces(project_dir)
    assert len(rows) == 200, "total-count-200"
    hashes = {r.composite_hash for r in rows}
    assert "0" * 64 not in hashes, "oldest-evicted"
    assert "z" * 64 in hashes, "newest-present"


@pytest.mark.xfail(reason="target-state (R32/R39/OQ-7): stat-check on mix cache hit "
                          "not shipped in DAL", strict=False)
def test_mix_missing_wav_treated_as_miss(project_dir: Path, db_conn):
    """covers R32, R39, OQ-7 — mix-missing-wav-treated-as-miss."""
    # Given: row says rendered_path="pool/mixes/abc.wav" but file doesn't exist
    run = dmc.create_mix_run(project_dir, "abc", 0.0, 30.0, 48000, "v1", [],
                             rendered_path="pool/mixes/abc.wav",
                             created_at="2026-04-27T00:00:00Z")
    # When: DAL-level stat-check on cache hit (target behavior)
    from scenecraft.db_mix_cache import get_mix_run_verified  # type: ignore[attr-defined]
    got = get_mix_run_verified(project_dir, "abc", 0.0, 30.0, 48000, "v1")
    # Then: returns None because the WAV is missing (caller treats as miss)
    assert got is None, "treated-as-miss"


@pytest.mark.xfail(reason="target-state (R38/OQ-6): `scenecraft cache gc` CLI not shipped",
                   strict=False)
def test_cache_gc_purges_peaks_orphans(project_dir: Path, tmp_path: Path):
    """covers R38, OQ-6 — cache-gc-purges-peaks-orphans."""
    cache_dir = project_dir / "audio_staging" / ".peaks"
    cache_dir.mkdir(parents=True, exist_ok=True)
    valid = cache_dir / ("a" * 16 + ".f16"); valid.write_bytes(b"\x00")
    orphan = cache_dir / ("b" * 16 + ".f16"); orphan.write_bytes(b"\x00")
    stale = cache_dir / ("c" * 16 + ".f16"); stale.write_bytes(b"\x00")
    from scenecraft.cli import cache_gc  # type: ignore[attr-defined]
    cache_gc(project_dir)
    assert valid.exists(), "valid-file-kept"
    assert not orphan.exists(), "orphan-deleted"
    assert not stale.exists(), "stale-deleted"


# ===========================================================================
# E2E SECTION — HTTP round-trip for every cache with an HTTP boundary
# ===========================================================================


class TestEndToEnd:
    """End-to-end coverage via the live `engine_server` fixture.

    Today's HTTP surface only exposes the peaks cache directly
    (GET /audio-clips/:id/peaks, GET /pool/:seg_id/peaks). The DB-backed
    caches (DSP, mix, audio-description, bounce) are driven through the
    WebSocket chat bridge — their dedicated POST routes are a target-state
    deliverable of the M16 FastAPI migration and are xfailed here.
    """

    # ---- Peaks HTTP: filesystem cache populate + hit ----

    def test_peaks_endpoint_populates_and_then_hits_cache(
        self, engine_server, project_name,
    ):
        """covers R24..R27 — HTTP-level peaks cache populate → hit."""
        project_dir = engine_server.work_dir / project_name
        # Given: short WAV + audio_clip pointing at it
        wav_rel = "source.wav"
        _caches_short_wav(project_dir / wav_rel, seconds=0.25)
        clip_id = "clipA"
        scdb.add_audio_clip(project_dir, {
            "id": clip_id, "track_id": "track_1", "source_path": wav_rel,
            "start_time": 0.0, "end_time": 0.25, "source_offset": 0.0,
        })
        peaks_dir = project_dir / "audio_staging" / ".peaks"
        # Close any connection the test made so the server can open its own
        scdb.close_db(project_dir)

        # When (1st call): cache miss — file written
        status, _h, body1 = engine_server.request(
            "GET", f"/api/projects/{project_name}/audio-clips/{clip_id}/peaks?resolution=400",
        )
        assert status == 200, f"first peaks GET: {status}"
        assert len(body1) > 0, "first-call-returns-bytes"
        files_after_first = list(peaks_dir.glob("*.f16"))
        assert len(files_after_first) == 1, "cache-file-written"
        cache_file = files_after_first[0]
        mtime_after_first = cache_file.stat().st_mtime_ns

        # When (2nd call): cache hit — file bytes served verbatim, no rewrite
        status2, _h2, body2 = engine_server.request(
            "GET", f"/api/projects/{project_name}/audio-clips/{clip_id}/peaks?resolution=400",
        )
        assert status2 == 200
        assert body2 == body1, "cache-hit-returns-identical-bytes"
        # File count unchanged (same cache key)
        assert len(list(peaks_dir.glob("*.f16"))) == 1, "no-new-cache-file"
        # Either mtime unchanged (no rewrite) or rewrite is idempotent with same bytes
        assert cache_file.stat().st_mtime_ns == mtime_after_first or \
               cache_file.read_bytes() == body1, \
               "cache-hit-does-not-corrupt-file"

    def test_peaks_endpoint_source_edit_invalidates_and_rebuilds(
        self, engine_server, project_name,
    ):
        """covers R25 — HTTP-level source-edit produces new cache key + file."""
        project_dir = engine_server.work_dir / project_name
        wav_rel = "source.wav"
        _caches_short_wav(project_dir / wav_rel, seconds=0.25)
        clip_id = "clipA"
        scdb.add_audio_clip(project_dir, {
            "id": clip_id, "track_id": "track_1", "source_path": wav_rel,
            "start_time": 0.0, "end_time": 0.25, "source_offset": 0.0,
        })
        scdb.close_db(project_dir)

        engine_server.request("GET",
            f"/api/projects/{project_name}/audio-clips/{clip_id}/peaks?resolution=400")
        peaks_dir = project_dir / "audio_staging" / ".peaks"
        files_1 = {p.name for p in peaks_dir.glob("*.f16")}
        assert len(files_1) == 1, "first-call-writes-one-file"

        # Edit source — changes mtime + size
        time.sleep(0.01)
        _caches_short_wav(project_dir / wav_rel, seconds=0.50)

        engine_server.request("GET",
            f"/api/projects/{project_name}/audio-clips/{clip_id}/peaks?resolution=400")
        files_2 = {p.name for p in peaks_dir.glob("*.f16")}
        # A new cache file (new key) should exist
        assert len(files_2) >= 2, "source-edit-produces-new-key"
        assert files_1.issubset(files_2), "old-file-lingers: no auto-cleanup"

    def test_peaks_endpoint_returns_404_for_missing_clip(
        self, engine_server, project_name,
    ):
        """covers handler 404 path; no cache side effect on lookup miss."""
        status, _h, _b = engine_server.request(
            "GET", f"/api/projects/{project_name}/audio-clips/does-not-exist/peaks",
        )
        assert status == 404, "clip-404"

    # ---- Bounce HTTP: DB-side via bounce-upload (the one DB cache
    #      already wired to the HTTP surface today) ----

    def test_bounce_upload_writes_pool_file_and_is_idempotent_by_hash(
        self, engine_server, project_name,
    ):
        """covers R31 — POST /bounce-upload is content-addressable: two
        uploads with the same composite_hash write the same file path and
        return the same file bytes. The DB row side of the bounce cache
        (audio_bounces) is populated by the WS chat flow (_exec_bounce_audio),
        not by this HTTP endpoint — see surfaced-bug note below.

        **Surfaced bug**: the bounce-upload endpoint writes the on-disk WAV
        but does NOT insert or update the corresponding audio_bounces row.
        The row is only created by the WS-driven chat flow. A direct HTTP
        upload (bypassing chat) leaves a file on disk with no DAL row —
        the download endpoint /bounces/<id>.wav will 404. Logged for
        triage; this test asserts the observable file-side behavior only.
        """
        import urllib.request

        project_dir = engine_server.work_dir / project_name
        # Multipart body — endpoint requires sample_rate + channels to match
        # the WAV header, so use values that agree with _caches_short_wav().
        wav_path = project_dir / "staging.wav"
        _caches_short_wav(wav_path, seconds=0.25, sr=16000)
        wav_bytes = wav_path.read_bytes()
        composite_hash = hashlib.sha256(b"test-composite").hexdigest()

        def _multipart(hash_val: str) -> tuple[bytes, str]:
            boundary = "----testboundary" + os.urandom(8).hex()
            fields = {
                "composite_hash": hash_val,
                "start_time_s": "0.0",
                "end_time_s": "0.25",
                "sample_rate": "16000",
                "bit_depth": "16",
                "channels": "1",
            }
            parts: list[bytes] = []
            for name, val in fields.items():
                parts.append(f"--{boundary}\r\n".encode())
                parts.append(
                    f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode()
                )
                parts.append(val.encode() + b"\r\n")
            parts.append(f"--{boundary}\r\n".encode())
            parts.append(
                b'Content-Disposition: form-data; name="audio"; filename="b.wav"\r\n'
                b"Content-Type: audio/wav\r\n\r\n"
            )
            parts.append(wav_bytes + b"\r\n")
            parts.append(f"--{boundary}--\r\n".encode())
            return b"".join(parts), f"multipart/form-data; boundary={boundary}"

        url = f"{engine_server.base_url}/api/projects/{project_name}/bounce-upload"

        body, ct = _multipart(composite_hash)
        req = urllib.request.Request(url, data=body, method="POST",
                                     headers={"Content-Type": ct})
        with urllib.request.urlopen(req, timeout=15) as resp:
            assert resp.status in (200, 201), f"first-upload: {resp.status}"

        # File landed at the content-addressable path
        expected_wav = project_dir / "pool" / "bounces" / f"{composite_hash}.wav"
        assert expected_wav.exists(), "content-addressable-path: pool/bounces/<hash>.wav"
        first_bytes = expected_wav.read_bytes()

        # Second upload with same composite_hash — same file, idempotent
        body2, ct2 = _multipart(composite_hash)
        req2 = urllib.request.Request(url, data=body2, method="POST",
                                      headers={"Content-Type": ct2})
        with urllib.request.urlopen(req2, timeout=15) as resp2:
            assert resp2.status in (200, 201), f"second-upload: {resp2.status}"
        assert expected_wav.read_bytes() == first_bytes, \
            "content-addressable-idempotent: same hash writes same bytes"

    # ---- Target-state HTTP endpoints (not in engine today) ----

    @pytest.mark.xfail(reason="target-state: POST /api/projects/:name/bounce "
                              "not shipped (M16 FastAPI)", strict=False)
    def test_post_bounce_endpoint_populates_and_hits_cache(
        self, engine_server, project_name,
    ):
        """covers R4, R10 — first POST /bounce writes row; second returns cached."""
        status1, body1 = engine_server.json(
            "POST", f"/api/projects/{project_name}/bounce",
            {"mode": "full", "selection": {}, "start_time_s": 0.0,
             "end_time_s": 1.0, "sample_rate": 48000, "bit_depth": 24},
        )
        assert status1 == 200 and body1 and "composite_hash" in body1
        status2, body2 = engine_server.json(
            "POST", f"/api/projects/{project_name}/bounce",
            {"mode": "full", "selection": {}, "start_time_s": 0.0,
             "end_time_s": 1.0, "sample_rate": 48000, "bit_depth": 24},
        )
        assert status2 == 200
        assert body2.get("bounce_id") == body1.get("bounce_id"), "cache-hit-same-id"

    @pytest.mark.xfail(reason="target-state: POST /analyze-master-bus not shipped (WS-only today)",
                       strict=False)
    def test_post_analyze_master_bus_hits_cache(self, engine_server, project_name):
        """covers R8, R11 — first POST builds mix_analysis_runs row; second hits cache."""
        body = {"start_s": 0.0, "end_s": 1.0, "sample_rate": 48000}
        s1, b1 = engine_server.json(
            "POST", f"/api/projects/{project_name}/analyze-master-bus", body)
        assert s1 == 200
        s2, b2 = engine_server.json(
            "POST", f"/api/projects/{project_name}/analyze-master-bus", body)
        assert s2 == 200 and b2.get("run_id") == b1.get("run_id"), "cache-hit-same-run-id"

    @pytest.mark.xfail(reason="target-state: POST /generate-dsp not shipped (WS-only today)",
                       strict=False)
    def test_post_generate_dsp_hits_cache(self, engine_server, project_name):
        """covers R1, R7 — DSP cache populate + hit via HTTP."""
        project_dir = engine_server.work_dir / project_name
        seg = _caches_seed_pool_segment(project_dir)
        scdb.close_db(project_dir)
        body = {"source_segment_id": seg, "params_hash": "p1"}
        s1, b1 = engine_server.json(
            "POST", f"/api/projects/{project_name}/generate-dsp", body)
        assert s1 == 200
        s2, b2 = engine_server.json(
            "POST", f"/api/projects/{project_name}/generate-dsp", body)
        assert s2 == 200 and b2.get("run_id") == b1.get("run_id"), "cache-hit-same-run-id"

    @pytest.mark.xfail(reason="target-state: POST /generate-descriptions not shipped "
                              "(WS-only today)", strict=False)
    def test_post_generate_descriptions_hits_cache(self, engine_server, project_name):
        """covers R3, R9 — description cache populate + hit via HTTP."""
        project_dir = engine_server.work_dir / project_name
        seg = _caches_seed_pool_segment(project_dir)
        scdb.close_db(project_dir)
        body = {"source_segment_id": seg, "model": "gemini-1.5", "prompt_version": "v2"}
        s1, b1 = engine_server.json(
            "POST", f"/api/projects/{project_name}/generate-descriptions", body)
        assert s1 == 200
        s2, b2 = engine_server.json(
            "POST", f"/api/projects/{project_name}/generate-descriptions", body)
        assert s2 == 200 and b2.get("run_id") == b1.get("run_id"), "cache-hit-same-run-id"

    @pytest.mark.xfail(reason="target-state: force_rerun param over HTTP requires M16 route",
                       strict=False)
    def test_post_bounce_force_rerun_deletes_and_recreates(
        self, engine_server, project_name,
    ):
        """covers R16 — force_rerun=true over HTTP deletes + recreates the row."""
        body1 = {"mode": "full", "selection": {}, "start_time_s": 0.0,
                 "end_time_s": 1.0, "sample_rate": 48000, "bit_depth": 24}
        s1, b1 = engine_server.json(
            "POST", f"/api/projects/{project_name}/bounce", body1)
        assert s1 == 200
        body2 = dict(body1, force_rerun=True)
        s2, b2 = engine_server.json(
            "POST", f"/api/projects/{project_name}/bounce", body2)
        assert s2 == 200
        assert b2.get("bounce_id") != b1.get("bounce_id"), \
            "force-rerun-yields-new-id"
