"""Tests for M15 master-bus mix analysis cache schema + helpers.

Covers:
- Schema creates 4 tables + expected index.
- UNIQUE cache-key constraint on mix_analysis_runs.
- CASCADE from run → datapoints / sections / scalars.
- Round-trip for create_mix_run → get_mix_run.
- update_mix_run_rendered_path works.
- Bulk datapoints / sections / set_mix_scalars / get_mix_scalars round-trip.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
import sqlite3

from scenecraft.db import get_db
from scenecraft.db_models import MixAnalysisRun
from scenecraft.db_mix_cache import (
    bulk_insert_mix_datapoints,
    bulk_insert_mix_sections,
    create_mix_run,
    delete_mix_run,
    get_mix_run,
    get_mix_scalars,
    list_mix_runs_for_hash,
    query_mix_datapoints,
    query_mix_sections,
    set_mix_scalars,
    update_mix_run_rendered_path,
)


@pytest.fixture
def project(tmp_path):
    project_dir = tmp_path / "mix_project"
    project_dir.mkdir()
    get_db(project_dir)  # force schema create
    return project_dir


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


HASH_A = "a" * 64
HASH_B = "b" * 64


# -- Schema --------------------------------------------------------------

def test_schema_creates_four_mix_tables(project):
    conn = get_db(project)
    names = {r["name"] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    for t in (
        "mix_analysis_runs",
        "mix_datapoints",
        "mix_sections",
        "mix_scalars",
    ):
        assert t in names, f"missing table: {t}"


def test_schema_creates_expected_mix_index(project):
    conn = get_db(project)
    idx = {r["name"] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index'").fetchall()}
    for name in (
        "idx_mix_runs_hash",
        "idx_mix_datapoints_type_time",
    ):
        assert name in idx, f"missing index: {name}"


# -- UNIQUE + CASCADE ----------------------------------------------------

def test_create_and_get_mix_run(project):
    run = create_mix_run(
        project,
        mix_graph_hash=HASH_A,
        start_s=0.0,
        end_s=12.5,
        sample_rate=48000,
        analyzer_version="mix-v1",
        analyses=["rms", "lufs"],
        rendered_path=None,
        created_at=_now(),
    )
    assert isinstance(run, MixAnalysisRun)
    assert run.rendered_path is None
    assert run.analyses == ["rms", "lufs"]

    fetched = get_mix_run(project, HASH_A, 0.0, 12.5, 48000, "mix-v1")
    assert fetched is not None
    assert fetched.id == run.id
    assert fetched.mix_graph_hash == HASH_A
    assert fetched.sample_rate == 48000


def test_mix_run_unique_constraint(project):
    create_mix_run(
        project, HASH_A, 0.0, 10.0, 48000, "mix-v1",
        ["rms"], None, _now(),
    )
    conn = get_db(project)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO mix_analysis_runs (id, mix_graph_hash, start_time_s, "
            "end_time_s, sample_rate, analyzer_version, analyses_json, "
            "rendered_path, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("mix_dup", HASH_A, 0.0, 10.0, 48000, "mix-v1", "[]", None, _now()),
        )


def test_different_window_is_distinct_run(project):
    # Different start/end → fresh row permitted under same hash.
    r1 = create_mix_run(project, HASH_A, 0.0, 10.0, 48000, "mix-v1",
                       ["rms"], None, _now())
    r2 = create_mix_run(project, HASH_A, 10.0, 20.0, 48000, "mix-v1",
                       ["rms"], None, _now())
    assert r1.id != r2.id


def test_delete_mix_run_cascades(project):
    run = create_mix_run(
        project, HASH_A, 0.0, 10.0, 48000, "mix-v1",
        ["rms"], None, _now(),
    )
    bulk_insert_mix_datapoints(project, run.id, [("rms", 0.5, 0.4, None)])
    bulk_insert_mix_sections(project, run.id, [(0.0, 0.5, "clipping_event", None, 0.9)])
    set_mix_scalars(project, run.id, {"peak_db": -1.2})

    delete_mix_run(project, run.id)

    conn = get_db(project)
    assert conn.execute(
        "SELECT COUNT(*) FROM mix_datapoints WHERE run_id = ?", (run.id,)
    ).fetchone()[0] == 0
    assert conn.execute(
        "SELECT COUNT(*) FROM mix_sections WHERE run_id = ?", (run.id,)
    ).fetchone()[0] == 0
    assert conn.execute(
        "SELECT COUNT(*) FROM mix_scalars WHERE run_id = ?", (run.id,)
    ).fetchone()[0] == 0


# -- Helper round trips --------------------------------------------------

def test_update_mix_run_rendered_path(project):
    run = create_mix_run(
        project, HASH_A, 0.0, 10.0, 48000, "mix-v1",
        ["rms"], None, _now(),
    )
    assert get_mix_run(project, HASH_A, 0.0, 10.0, 48000, "mix-v1").rendered_path is None

    update_mix_run_rendered_path(project, run.id, f"pool/mixes/{HASH_A}.wav")

    refreshed = get_mix_run(project, HASH_A, 0.0, 10.0, 48000, "mix-v1")
    assert refreshed.rendered_path == f"pool/mixes/{HASH_A}.wav"


def test_bulk_insert_and_query_datapoints(project):
    run = create_mix_run(project, HASH_A, 0.0, 10.0, 48000, "mix-v1",
                        ["rms"], None, _now())
    bulk_insert_mix_datapoints(project, run.id, [
        ("rms", 0.0, 0.1, None),
        ("rms", 0.5, 0.4, None),
        ("rms", 1.0, 0.7, None),
        ("short_term_lufs", 0.5, -14.2, {"window_s": 3.0}),
    ])

    rms = query_mix_datapoints(project, run.id, "rms")
    assert [d.time_s for d in rms] == [0.0, 0.5, 1.0]

    windowed = query_mix_datapoints(project, run.id, "rms", time_start=0.3, time_end=0.9)
    assert [d.time_s for d in windowed] == [0.5]

    lufs = query_mix_datapoints(project, run.id, "short_term_lufs")
    assert len(lufs) == 1
    assert lufs[0].extra == {"window_s": 3.0}


def test_bulk_insert_and_query_sections(project):
    run = create_mix_run(project, HASH_A, 0.0, 10.0, 48000, "mix-v1",
                        ["clipping"], None, _now())
    bulk_insert_mix_sections(project, run.id, [
        (0.0, 0.5, "silence", None, 1.0),
        (1.0, 1.1, "clipping_event", "hard_clip", 0.98),
        (5.0, 5.3, "clipping_event", "near_clip", 0.82),
    ])

    all_sections = query_mix_sections(project, run.id)
    assert len(all_sections) == 3

    clips = query_mix_sections(project, run.id, section_type="clipping_event")
    assert [s.start_s for s in clips] == [1.0, 5.0]
    assert [s.label for s in clips] == ["hard_clip", "near_clip"]


def test_mix_scalars_round_trip(project):
    run = create_mix_run(project, HASH_A, 0.0, 10.0, 48000, "mix-v1",
                        ["scalars"], None, _now())
    set_mix_scalars(project, run.id, {
        "peak_db": -0.8,
        "true_peak_db": -0.3,
        "lufs_integrated": -14.0,
        "dynamic_range": 8.2,
        "clip_count": 3,
    })
    scalars = get_mix_scalars(project, run.id)
    assert scalars == {
        "peak_db": -0.8,
        "true_peak_db": -0.3,
        "lufs_integrated": -14.0,
        "dynamic_range": 8.2,
        "clip_count": 3.0,
    }

    # Upsert same metric replaces value.
    set_mix_scalars(project, run.id, {"peak_db": -1.5})
    assert get_mix_scalars(project, run.id)["peak_db"] == -1.5


def test_list_mix_runs_for_hash_newest_first(project):
    r1 = create_mix_run(project, HASH_A, 0.0, 10.0, 48000, "mix-v1",
                       ["rms"], None, "2026-01-01T00:00:00Z")
    r2 = create_mix_run(project, HASH_A, 0.0, 10.0, 48000, "mix-v2",
                       ["rms"], None, "2026-02-01T00:00:00Z")
    # Different hash → not returned.
    create_mix_run(project, HASH_B, 0.0, 10.0, 48000, "mix-v1",
                  ["rms"], None, _now())

    runs = list_mix_runs_for_hash(project, HASH_A)
    assert [r.id for r in runs] == [r2.id, r1.id]
