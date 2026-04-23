"""Tests for Phase 3 analysis-cache schema + helpers.

Covers:
- 7 tables exist (dsp_analysis_runs, dsp_datapoints, dsp_sections, dsp_scalars,
  audio_description_runs, audio_descriptions, audio_description_scalars).
- Expected indexes exist.
- UNIQUE cache-key constraints (DSP + descriptions).
- CASCADE from pool_segments → runs → child rows.
- Bulk insert + query round-trips for each helper.
- Time-windowed filtering on datapoints + descriptions.
- Idempotency of _ensure_schema.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
import sqlite3

from scenecraft.db import (
    _ensure_schema,
    add_pool_segment,
    get_db,
    delete_pool_segment,
)
from scenecraft.db_models import (
    AudioDescriptionRun,
    DspAnalysisRun,
    DspDatapoint,
    DspSection,
)
from scenecraft.db_analysis_cache import (
    bulk_insert_audio_descriptions,
    bulk_insert_dsp_datapoints,
    bulk_insert_dsp_sections,
    create_audio_description_run,
    create_dsp_run,
    delete_audio_description_run,
    delete_dsp_run,
    get_audio_description_run,
    get_audio_description_scalars,
    get_dsp_run,
    get_dsp_scalars,
    list_audio_description_runs,
    list_dsp_runs,
    query_audio_descriptions,
    query_dsp_datapoints,
    query_dsp_sections,
    set_audio_description_scalars,
    set_dsp_scalars,
)


@pytest.fixture
def project(tmp_path):
    project_dir = tmp_path / "analysis_project"
    project_dir.mkdir()
    get_db(project_dir)  # force schema create
    return project_dir


@pytest.fixture
def source_segment(project):
    """Insert a pool segment to anchor analysis runs against."""
    return add_pool_segment(
        project,
        kind="imported",
        created_by="test",
        pool_path="pool/segments/seg_vocal_1.wav",
        original_filename="lead_vocal.wav",
        original_filepath="/tmp/lead_vocal.wav",
        label="Lead vocal",
        generation_params={},
        duration_seconds=8.5,
    )


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# -- Schema --------------------------------------------------------------

def test_schema_creates_seven_analysis_cache_tables(project):
    conn = get_db(project)
    names = {r["name"] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    for t in (
        "dsp_analysis_runs",
        "dsp_datapoints",
        "dsp_sections",
        "dsp_scalars",
        "audio_description_runs",
        "audio_descriptions",
        "audio_description_scalars",
    ):
        assert t in names, f"missing table: {t}"


def test_schema_creates_expected_indexes(project):
    conn = get_db(project)
    idx = {r["name"] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index'").fetchall()}
    for name in (
        "idx_dsp_runs_source",
        "idx_dsp_datapoints_type_time",
        "idx_dsp_sections_type_start",
        "idx_audio_desc_runs_source",
        "idx_audio_descriptions_property_time",
    ):
        assert name in idx, f"missing index: {name}"


def test_ensure_schema_is_idempotent(project):
    conn = get_db(project)
    # Re-apply; should not throw or duplicate.
    _ensure_schema(conn)
    _ensure_schema(conn)
    assert conn.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='table'").fetchone()[0] > 0


# -- DSP runs + UNIQUE + CASCADE -----------------------------------------

def test_create_and_get_dsp_run(project, source_segment):
    run = create_dsp_run(
        project,
        source_segment_id=source_segment,
        analyzer_version="librosa-0.10.2",
        params_hash="abc123",
        analyses=["onsets", "rms"],
        created_at=_now(),
    )
    assert isinstance(run, DspAnalysisRun)
    assert run.analyses == ["onsets", "rms"]

    fetched = get_dsp_run(project, source_segment, "librosa-0.10.2", "abc123")
    assert fetched is not None
    assert fetched.id == run.id
    assert fetched.analyses == ["onsets", "rms"]


def test_dsp_run_unique_constraint_enforced_at_sql_layer(project, source_segment):
    create_dsp_run(
        project,
        source_segment_id=source_segment,
        analyzer_version="librosa-0.10.2",
        params_hash="abc123",
        analyses=["onsets"],
        created_at=_now(),
    )
    conn = get_db(project)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO dsp_analysis_runs (id, source_segment_id, analyzer_version, "
            "params_hash, analyses_json, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            ("dsp_run_dup", source_segment, "librosa-0.10.2", "abc123", "[]", _now()),
        )


def test_dsp_run_cascade_on_pool_segment_delete(project, source_segment):
    run = create_dsp_run(
        project, source_segment,
        "librosa-0.10.2", "abc123", ["onsets"], _now(),
    )
    bulk_insert_dsp_datapoints(project, run.id, [("onset", 0.25, 1.0, None)])
    set_dsp_scalars(project, run.id, {"tempo_bpm": 120.0})

    delete_pool_segment(project, source_segment)

    conn = get_db(project)
    assert conn.execute("SELECT COUNT(*) FROM dsp_analysis_runs").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM dsp_datapoints").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM dsp_scalars").fetchone()[0] == 0


def test_list_dsp_runs_ordered_newest_first(project, source_segment):
    run1 = create_dsp_run(project, source_segment, "v1", "h1", ["onsets"], "2026-01-01T00:00:00Z")
    run2 = create_dsp_run(project, source_segment, "v1", "h2", ["rms"], "2026-02-01T00:00:00Z")
    runs = list_dsp_runs(project, source_segment)
    assert [r.id for r in runs] == [run2.id, run1.id]


def test_delete_dsp_run_cascades(project, source_segment):
    run = create_dsp_run(project, source_segment, "v1", "h1", ["onsets"], _now())
    bulk_insert_dsp_datapoints(project, run.id, [("onset", 0.5, 1.0, None)])
    bulk_insert_dsp_sections(project, run.id, [(0.0, 1.0, "silence", None, None)])
    set_dsp_scalars(project, run.id, {"peak_db": -3.2})

    delete_dsp_run(project, run.id)

    conn = get_db(project)
    assert conn.execute("SELECT COUNT(*) FROM dsp_datapoints WHERE run_id = ?", (run.id,)).fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM dsp_sections WHERE run_id = ?", (run.id,)).fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM dsp_scalars WHERE run_id = ?", (run.id,)).fetchone()[0] == 0


# -- DSP datapoints ------------------------------------------------------

def test_bulk_insert_and_query_datapoints(project, source_segment):
    run = create_dsp_run(project, source_segment, "v1", "h1", ["rms"], _now())
    bulk_insert_dsp_datapoints(project, run.id, [
        ("rms", 0.0, 0.1, None),
        ("rms", 0.5, 0.4, None),
        ("rms", 1.0, 0.7, None),
        ("rms", 1.5, 0.5, None),
        ("onset", 0.6, 1.0, {"strength": 0.83}),
    ])
    all_rms = query_dsp_datapoints(project, run.id, "rms")
    assert [d.time_s for d in all_rms] == [0.0, 0.5, 1.0, 1.5]

    windowed = query_dsp_datapoints(project, run.id, "rms", time_start=0.4, time_end=1.2)
    assert [d.time_s for d in windowed] == [0.5, 1.0]

    onsets = query_dsp_datapoints(project, run.id, "onset")
    assert len(onsets) == 1
    assert onsets[0].extra == {"strength": 0.83}


def test_datapoints_pk_prevents_duplicates(project, source_segment):
    run = create_dsp_run(project, source_segment, "v1", "h1", ["rms"], _now())
    # Second insert with same (run_id, data_type, time_s) should REPLACE via our helper.
    bulk_insert_dsp_datapoints(project, run.id, [("rms", 0.5, 0.4, None)])
    bulk_insert_dsp_datapoints(project, run.id, [("rms", 0.5, 0.9, None)])
    dps = query_dsp_datapoints(project, run.id, "rms")
    assert len(dps) == 1
    assert dps[0].value == 0.9


# -- DSP sections --------------------------------------------------------

def test_bulk_insert_and_query_sections(project, source_segment):
    run = create_dsp_run(project, source_segment, "v1", "h1", ["sections"], _now())
    bulk_insert_dsp_sections(project, run.id, [
        (0.0, 0.5, "silence", None, 1.0),
        (0.5, 2.3, "vocal_presence", "verse", 0.9),
        (2.3, 3.1, "silence", None, 0.95),
        (3.1, 5.0, "vocal_presence", "chorus", 0.88),
    ])

    all_sections = query_dsp_sections(project, run.id)
    assert len(all_sections) == 4

    vocals = query_dsp_sections(project, run.id, section_type="vocal_presence")
    assert [s.start_s for s in vocals] == [0.5, 3.1]
    assert [s.label for s in vocals] == ["verse", "chorus"]


# -- DSP scalars ---------------------------------------------------------

def test_scalars_upsert_round_trip(project, source_segment):
    run = create_dsp_run(project, source_segment, "v1", "h1", ["tempo"], _now())
    set_dsp_scalars(project, run.id, {"tempo_bpm": 120.5, "global_rms": 0.12})
    scalars = get_dsp_scalars(project, run.id)
    assert scalars == {"tempo_bpm": 120.5, "global_rms": 0.12}

    # Upsert same metric replaces value
    set_dsp_scalars(project, run.id, {"tempo_bpm": 124.0})
    assert get_dsp_scalars(project, run.id)["tempo_bpm"] == 124.0


# -- Audio description runs + UNIQUE -------------------------------------

def test_create_and_get_audio_description_run(project, source_segment):
    run = create_audio_description_run(
        project,
        source_segment_id=source_segment,
        model="gemini-2.5-pro",
        prompt_version="v1",
        chunk_size_s=30.0,
        created_at=_now(),
    )
    assert isinstance(run, AudioDescriptionRun)

    fetched = get_audio_description_run(project, source_segment, "gemini-2.5-pro", "v1")
    assert fetched is not None
    assert fetched.id == run.id


def test_audio_description_run_unique_constraint(project, source_segment):
    create_audio_description_run(project, source_segment, "gemini-2.5-pro", "v1", 30.0, _now())
    conn = get_db(project)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO audio_description_runs (id, source_segment_id, model, "
            "prompt_version, chunk_size_s, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            ("desc_dup", source_segment, "gemini-2.5-pro", "v1", 30.0, _now()),
        )


def test_audio_description_run_cascade_on_pool_segment_delete(project, source_segment):
    run = create_audio_description_run(project, source_segment, "gemini-2.5-pro", "v1", 30.0, _now())
    bulk_insert_audio_descriptions(project, run.id, [
        (0.0, 10.0, "mood", "reflective", None, 0.8, None),
    ])
    delete_pool_segment(project, source_segment)

    conn = get_db(project)
    assert conn.execute("SELECT COUNT(*) FROM audio_description_runs").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM audio_descriptions").fetchone()[0] == 0


# -- Audio descriptions --------------------------------------------------

def test_audio_descriptions_query_by_property_and_window(project, source_segment):
    run = create_audio_description_run(project, source_segment, "gemini-2.5-pro", "v1", 30.0, _now())
    bulk_insert_audio_descriptions(project, run.id, [
        (0.0, 10.0, "section_type", "intro", None, 0.9, None),
        (10.0, 30.0, "section_type", "verse", None, 0.85, None),
        (30.0, 60.0, "section_type", "chorus", None, 0.92, None),
        (0.0, 10.0, "mood", "reflective", None, 0.8, None),
        (10.0, 60.0, "mood", "uplifting", None, 0.7, None),
    ])

    sections = query_audio_descriptions(project, run.id, "section_type")
    assert [d.value_text for d in sections] == ["intro", "verse", "chorus"]

    windowed_moods = query_audio_descriptions(
        project, run.id, "mood", time_start=20.0, time_end=40.0,
    )
    # Only "uplifting" overlaps [20,40].
    assert [d.value_text for d in windowed_moods] == ["uplifting"]


def test_audio_descriptions_pk_upsert(project, source_segment):
    run = create_audio_description_run(project, source_segment, "gemini-2.5-pro", "v1", 30.0, _now())
    bulk_insert_audio_descriptions(project, run.id, [
        (0.0, 10.0, "mood", "dark", None, 0.6, None),
    ])
    # Same (run_id, start_s, property) → replace.
    bulk_insert_audio_descriptions(project, run.id, [
        (0.0, 10.0, "mood", "bright", None, 0.9, None),
    ])
    all_desc = query_audio_descriptions(project, run.id, "mood")
    assert len(all_desc) == 1
    assert all_desc[0].value_text == "bright"


# -- Audio description scalars -------------------------------------------

def test_audio_description_scalars_round_trip(project, source_segment):
    run = create_audio_description_run(project, source_segment, "gemini-2.5-pro", "v1", 30.0, _now())
    set_audio_description_scalars(project, run.id, [
        ("key", "C minor", None, 0.8),
        ("global_genre", "lo-fi", None, 0.9),
        ("vocal_gender", "female", None, 0.95),
    ])
    scalars = get_audio_description_scalars(project, run.id)
    by_prop = {s.property: s for s in scalars}
    assert by_prop["key"].value_text == "C minor"
    assert by_prop["global_genre"].value_text == "lo-fi"
    assert by_prop["vocal_gender"].confidence == 0.95


# -- List helpers --------------------------------------------------------

def test_list_audio_description_runs_ordered_newest_first(project, source_segment):
    r1 = create_audio_description_run(project, source_segment, "gemini-2.5-pro", "v1", 30.0, "2026-01-01T00:00:00Z")
    r2 = create_audio_description_run(project, source_segment, "gemini-2.5-pro", "v2", 30.0, "2026-02-01T00:00:00Z")
    runs = list_audio_description_runs(project, source_segment)
    assert [r.id for r in runs] == [r2.id, r1.id]


def test_delete_audio_description_run_cascades(project, source_segment):
    run = create_audio_description_run(project, source_segment, "gemini-2.5-pro", "v1", 30.0, _now())
    bulk_insert_audio_descriptions(project, run.id, [
        (0.0, 10.0, "mood", "dark", None, 0.6, None),
    ])
    set_audio_description_scalars(project, run.id, [("key", "C minor", None, 0.8)])

    delete_audio_description_run(project, run.id)

    conn = get_db(project)
    assert conn.execute(
        "SELECT COUNT(*) FROM audio_descriptions WHERE run_id = ?", (run.id,)
    ).fetchone()[0] == 0
    assert conn.execute(
        "SELECT COUNT(*) FROM audio_description_scalars WHERE run_id = ?", (run.id,)
    ).fetchone()[0] == 0
