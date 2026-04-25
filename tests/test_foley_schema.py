"""Tests for M18 foley plugin schema + helpers.

Verifies:
- Schema applies cleanly on a fresh project DB
- generate_foley__generations CHECK constraints on mode + status
- generate_foley__tracks PK prevents duplicates
- plugin_api.add_foley_generation / add_foley_track / updates / queries
- idempotency via CREATE TABLE IF NOT EXISTS
"""

from __future__ import annotations

import sqlite3

import pytest

from scenecraft.db import (
    get_db,
    add_pool_segment as db_add_pool_segment,
    add_foley_generation,
    update_foley_generation_status,
    add_foley_track,
    get_foley_generation,
    get_foley_generations_for_entity,
    get_foley_generation_tracks,
)


@pytest.fixture
def project_dir(tmp_path):
    # get_db triggers _ensure_schema via first-call bootstrap
    get_db(tmp_path)
    return tmp_path


# --- Schema creation -------------------------------------------------------


def test_foley_tables_exist_after_ensure_db(project_dir):
    conn = get_db(project_dir)
    tables = {
        row["name"]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'generate_foley%'"
        ).fetchall()
    }
    assert tables == {"generate_foley__generations", "generate_foley__tracks"}


def test_foley_indexes_exist(project_dir):
    conn = get_db(project_dir)
    idx = {
        row["name"]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name LIKE 'idx_gf%'"
        ).fetchall()
    }
    assert {"idx_gf_gen_status", "idx_gf_gen_entity", "idx_gf_gen_created", "idx_gf_tracks_pool"} <= idx


def test_ensure_db_idempotent(project_dir):
    """Re-running get_db on an existing project is a no-op (idempotent CREATE TABLE IF NOT EXISTS)."""
    get_db(project_dir)
    get_db(project_dir)
    conn = get_db(project_dir)
    count = conn.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='generate_foley__generations'"
    ).fetchone()[0]
    assert count == 1


# --- Constraints -----------------------------------------------------------


def test_mode_check_constraint(project_dir):
    """mode must be 't2fx' or 'v2fx'."""
    with pytest.raises(sqlite3.IntegrityError):
        add_foley_generation(
            project_dir,
            generation_id="gen_bad",
            mode="invalid",  # violates CHECK
            model="zsxkib/mmaudio",
        )


def test_status_check_constraint(project_dir):
    with pytest.raises(sqlite3.IntegrityError):
        add_foley_generation(
            project_dir,
            generation_id="gen_bad",
            mode="t2fx",
            model="zsxkib/mmaudio",
            status="weird",  # violates CHECK
        )


def test_entity_type_check_constraint(project_dir):
    """entity_type must be 'transition' or NULL."""
    with pytest.raises(sqlite3.IntegrityError):
        add_foley_generation(
            project_dir,
            generation_id="gen_bad",
            mode="t2fx",
            model="zsxkib/mmaudio",
            entity_type="audio_clip",  # not in allowed list (foley attaches to transition only)
        )


def test_variant_count_default_is_1(project_dir):
    gen_id = add_foley_generation(
        project_dir,
        generation_id="gen_default_vc",
        mode="t2fx",
        model="zsxkib/mmaudio",
    )
    gen = get_foley_generation(project_dir, gen_id)
    assert gen["variant_count"] == 1


# --- Write + read round-trip ----------------------------------------------


def test_add_and_get_generation(project_dir):
    gen_id = add_foley_generation(
        project_dir,
        generation_id="gen_t2fx_1",
        mode="t2fx",
        model="zsxkib/mmaudio",
        prompt="footsteps on gravel",
        duration_seconds=2.0,
        negative_prompt="music",
        cfg_strength=4.5,
    )
    assert gen_id == "gen_t2fx_1"

    gen = get_foley_generation(project_dir, gen_id)
    assert gen is not None
    assert gen["mode"] == "t2fx"
    assert gen["prompt"] == "footsteps on gravel"
    assert gen["duration_seconds"] == 2.0
    assert gen["negative_prompt"] == "music"
    assert gen["cfg_strength"] == 4.5
    assert gen["status"] == "pending"
    assert gen["variant_count"] == 1


def test_v2fx_generation_with_full_range(project_dir):
    gen_id = add_foley_generation(
        project_dir,
        generation_id="gen_v2fx_1",
        mode="v2fx",
        model="zsxkib/mmaudio",
        prompt="door slam",
        source_candidate_id="trc_xyz",
        source_in_seconds=12.3,
        source_out_seconds=14.3,
        entity_type="transition",
        entity_id="tr_abc",
    )
    gen = get_foley_generation(project_dir, gen_id)
    assert gen["mode"] == "v2fx"
    assert gen["source_candidate_id"] == "trc_xyz"
    assert gen["source_in_seconds"] == 12.3
    assert gen["source_out_seconds"] == 14.3
    assert gen["entity_type"] == "transition"
    assert gen["entity_id"] == "tr_abc"


def test_update_status_transitions(project_dir):
    add_foley_generation(
        project_dir,
        generation_id="gen_lifecycle",
        mode="t2fx",
        model="zsxkib/mmaudio",
    )
    update_foley_generation_status(
        project_dir, "gen_lifecycle", "running",
        started_at="2026-04-24T23:00:00Z",
    )
    gen = get_foley_generation(project_dir, "gen_lifecycle")
    assert gen["status"] == "running"
    assert gen["started_at"] == "2026-04-24T23:00:00Z"

    update_foley_generation_status(
        project_dir, "gen_lifecycle", "completed",
        completed_at="2026-04-24T23:01:00Z",
    )
    gen = get_foley_generation(project_dir, "gen_lifecycle")
    assert gen["status"] == "completed"
    assert gen["completed_at"] == "2026-04-24T23:01:00Z"


def test_update_status_failed_sets_error(project_dir):
    add_foley_generation(
        project_dir,
        generation_id="gen_fail",
        mode="t2fx",
        model="zsxkib/mmaudio",
    )
    update_foley_generation_status(
        project_dir, "gen_fail", "failed",
        error="prediction charged (ledger_42), download failed",
    )
    gen = get_foley_generation(project_dir, "gen_fail")
    assert gen["status"] == "failed"
    assert "ledger_42" in gen["error"]


# --- Tracks junction -------------------------------------------------------


def test_add_track_and_fk_to_pool(project_dir):
    gen_id = add_foley_generation(
        project_dir,
        generation_id="gen_with_track",
        mode="t2fx",
        model="zsxkib/mmaudio",
    )
    # Create a pool_segment so the FK resolves
    pool_id = db_add_pool_segment(
        project_dir,
        pool_path="pool/segments/foley_test.wav",
        kind="generated",
        created_by="plugin:generate-foley",
    )
    add_foley_track(
        project_dir,
        generation_id=gen_id,
        pool_segment_id=pool_id,
        variant_index=0,
        replicate_prediction_id="pred_real",
        duration_seconds=2.0,
        spend_ledger_id="ledger_xyz",
    )
    tracks = get_foley_generation_tracks(project_dir, gen_id)
    assert len(tracks) == 1
    assert tracks[0]["pool_segment_id"] == pool_id
    assert tracks[0]["variant_index"] == 0
    assert tracks[0]["replicate_prediction_id"] == "pred_real"
    assert tracks[0]["spend_ledger_id"] == "ledger_xyz"


def test_tracks_pk_rejects_duplicate_pool_segment_per_generation(project_dir):
    gen_id = add_foley_generation(
        project_dir,
        generation_id="gen_dup",
        mode="t2fx",
        model="zsxkib/mmaudio",
    )
    pool_id = db_add_pool_segment(
        project_dir,
        pool_path="pool/segments/dup.wav",
        kind="generated",
        created_by="plugin:generate-foley",
    )
    add_foley_track(
        project_dir, generation_id=gen_id, pool_segment_id=pool_id,
        variant_index=0, replicate_prediction_id="pred_a",
    )
    with pytest.raises(sqlite3.IntegrityError):
        # Same (generation_id, pool_segment_id) → PK violation
        add_foley_track(
            project_dir, generation_id=gen_id, pool_segment_id=pool_id,
            variant_index=1, replicate_prediction_id="pred_b",
        )


def test_multi_variant_tracks_ordered_by_variant_index(project_dir):
    """Forward-looking: store multiple tracks per generation, ordered by variant_index."""
    gen_id = add_foley_generation(
        project_dir,
        generation_id="gen_multi",
        mode="t2fx",
        model="zsxkib/mmaudio",
        variant_count=3,
    )
    for i in range(3):
        pool_id = db_add_pool_segment(
            project_dir,
            pool_path=f"pool/segments/variant_{i}.wav",
            kind="generated",
            created_by="plugin:generate-foley",
        )
        add_foley_track(
            project_dir, generation_id=gen_id, pool_segment_id=pool_id,
            variant_index=i, replicate_prediction_id=f"pred_{i}",
        )
    tracks = get_foley_generation_tracks(project_dir, gen_id)
    assert [t["variant_index"] for t in tracks] == [0, 1, 2]


# --- Listing ---------------------------------------------------------------


def test_list_filters_by_entity(project_dir):
    # t2fx — no context
    add_foley_generation(
        project_dir,
        generation_id="gen_freeform",
        mode="t2fx",
        model="zsxkib/mmaudio",
    )
    # v2fx — context = transition tr_1
    add_foley_generation(
        project_dir,
        generation_id="gen_for_tr1",
        mode="v2fx",
        model="zsxkib/mmaudio",
        entity_type="transition",
        entity_id="tr_1",
    )
    # v2fx — context = transition tr_2
    add_foley_generation(
        project_dir,
        generation_id="gen_for_tr2",
        mode="v2fx",
        model="zsxkib/mmaudio",
        entity_type="transition",
        entity_id="tr_2",
    )

    all_gens = get_foley_generations_for_entity(project_dir)
    assert {g["id"] for g in all_gens} == {"gen_freeform", "gen_for_tr1", "gen_for_tr2"}

    tr1 = get_foley_generations_for_entity(project_dir, entity_type="transition", entity_id="tr_1")
    assert [g["id"] for g in tr1] == ["gen_for_tr1"]


def test_list_includes_tracks_joined(project_dir):
    gen_id = add_foley_generation(
        project_dir,
        generation_id="gen_with_tracks",
        mode="t2fx",
        model="zsxkib/mmaudio",
    )
    pool_id = db_add_pool_segment(
        project_dir,
        pool_path="pool/segments/foo.wav",
        kind="generated",
        created_by="plugin:generate-foley",
    )
    add_foley_track(
        project_dir, generation_id=gen_id, pool_segment_id=pool_id,
        variant_index=0, replicate_prediction_id="pred_zzz",
        duration_seconds=2.0,
    )
    gens = get_foley_generations_for_entity(project_dir)
    g = next(g for g in gens if g["id"] == gen_id)
    assert len(g["tracks"]) == 1
    assert g["tracks"][0]["replicate_prediction_id"] == "pred_zzz"
    assert g["tracks"][0]["pool_path"] == "pool/segments/foo.wav"


# --- plugin_api surface ----------------------------------------------------


def test_plugin_api_exposes_foley_helpers():
    from scenecraft import plugin_api

    assert hasattr(plugin_api, "add_foley_generation")
    assert hasattr(plugin_api, "update_foley_generation_status")
    assert hasattr(plugin_api, "add_foley_track")
    assert hasattr(plugin_api, "get_foley_generation")
    assert hasattr(plugin_api, "get_foley_generations_for_entity")
    assert hasattr(plugin_api, "get_foley_generation_tracks")
