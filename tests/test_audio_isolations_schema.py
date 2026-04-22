"""Tests for the M11 task-100b audio_isolations + isolation_stems schema/helpers.

Covers:
  * _ensure_schema idempotency (runs twice without error)
  * add_audio_isolation roundtrip + returned id shape
  * update_audio_isolation_status transitions (+ error storage)
  * add_isolation_stem + idempotency on duplicate (isolation_id, pool_segment_id)
  * get_isolations_for_entity ordering (created_at DESC) and nested stems
  * get_isolation_stems joined with pool_segments (pool_path + duration_seconds)
  * Undo/redo round-trip across a new isolation + 2 stems inside one undo_group
"""

from __future__ import annotations

import pytest

from scenecraft.db import (
    get_db,
    add_pool_segment,
    add_audio_clip,
    add_audio_isolation,
    update_audio_isolation_status,
    add_isolation_stem,
    get_isolations_for_entity,
    get_isolation_stems,
    _ensure_schema,
    undo_begin,
    undo_execute,
    redo_execute,
)


@pytest.fixture
def project(tmp_path):
    project_dir = tmp_path / "iso_project"
    project_dir.mkdir()
    # Force schema creation on first access
    get_db(project_dir)
    return project_dir


@pytest.fixture
def setup(project):
    """Create one audio_clip + two pool_segments (vocal + background stems)."""
    clip_id = "clip_iso_1"
    add_audio_clip(project, {
        "id": clip_id,
        "track_id": "track_1",
        "source_path": "/audio/source.wav",
        "start_time": 0.0,
        "end_time": 10.0,
    })
    seg_vocal = add_pool_segment(
        project,
        kind="generated",
        created_by="audio-isolation-plugin",
        pool_path="pool/segments/vocal.wav",
        label="vocal stem",
        duration_seconds=10.0,
    )
    seg_bg = add_pool_segment(
        project,
        kind="generated",
        created_by="audio-isolation-plugin",
        pool_path="pool/segments/background.wav",
        label="background stem",
        duration_seconds=10.0,
    )
    return {
        "project": project,
        "clip_id": clip_id,
        "seg_vocal": seg_vocal,
        "seg_bg": seg_bg,
    }


# ── Schema ────────────────────────────────────────────────────────

def test_schema_creates_audio_isolations_table(project):
    conn = get_db(project)
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='audio_isolations'"
    ).fetchall()
    assert len(rows) == 1


def test_schema_creates_isolation_stems_table(project):
    conn = get_db(project)
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='isolation_stems'"
    ).fetchall()
    assert len(rows) == 1


def test_schema_creates_isolation_indexes(project):
    conn = get_db(project)
    idx_names = {
        r["name"]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index'"
        ).fetchall()
    }
    assert "idx_isolations_entity" in idx_names
    assert "idx_isolations_created" in idx_names
    assert "idx_isolation_stems_run" in idx_names
    assert "idx_isolation_stems_segment" in idx_names


def test_ensure_schema_is_idempotent(project):
    """Running _ensure_schema a second time on an already-migrated db must not raise."""
    conn = get_db(project)
    # First call already happened via get_db; run a second pass explicitly.
    _ensure_schema(conn)
    # And a third, for good measure.
    _ensure_schema(conn)
    # Sanity: tables still present and queryable.
    conn.execute("SELECT COUNT(*) FROM audio_isolations").fetchone()
    conn.execute("SELECT COUNT(*) FROM isolation_stems").fetchone()


def test_schema_creates_undo_triggers(project):
    conn = get_db(project)
    triggers = {
        r["name"]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger'"
        ).fetchall()
    }
    for t in (
        "audio_isolations_insert_undo",
        "audio_isolations_update_undo",
        "audio_isolations_delete_undo",
        "isolation_stems_insert_undo",
        "isolation_stems_update_undo",
        "isolation_stems_delete_undo",
    ):
        assert t in triggers, f"missing trigger: {t}"


# ── add_audio_isolation ───────────────────────────────────────────

def test_add_audio_isolation_returns_id_and_roundtrips(setup):
    iso_id = add_audio_isolation(
        setup["project"],
        entity_type="audio_clip",
        entity_id=setup["clip_id"],
        model="deepfilternet3",
        range_mode="subset",
        trim_in=1.5,
        trim_out=4.25,
    )
    assert isinstance(iso_id, str) and iso_id
    assert iso_id.startswith("iso_")
    conn = get_db(setup["project"])
    row = conn.execute(
        "SELECT * FROM audio_isolations WHERE id = ?", (iso_id,)
    ).fetchone()
    assert row is not None
    assert row["entity_type"] == "audio_clip"
    assert row["entity_id"] == setup["clip_id"]
    assert row["model"] == "deepfilternet3"
    assert row["range_mode"] == "subset"
    assert row["trim_in"] == 1.5
    assert row["trim_out"] == 4.25
    assert row["status"] == "pending"
    assert row["error"] is None
    assert row["created_at"]


def test_add_audio_isolation_rejects_bad_entity_type(setup):
    with pytest.raises(AssertionError):
        add_audio_isolation(
            setup["project"],
            entity_type="bogus",
            entity_id=setup["clip_id"],
            model="deepfilternet3",
            range_mode="full",
            trim_in=None,
            trim_out=None,
        )


def test_add_audio_isolation_rejects_bad_range_mode(setup):
    with pytest.raises(AssertionError):
        add_audio_isolation(
            setup["project"],
            entity_type="audio_clip",
            entity_id=setup["clip_id"],
            model="deepfilternet3",
            range_mode="whole",  # not 'full' or 'subset'
            trim_in=None,
            trim_out=None,
        )


# ── update_audio_isolation_status ─────────────────────────────────

def test_update_audio_isolation_status_transitions(setup):
    iso_id = add_audio_isolation(
        setup["project"],
        entity_type="audio_clip",
        entity_id=setup["clip_id"],
        model="deepfilternet3",
        range_mode="full",
        trim_in=None,
        trim_out=None,
    )
    conn = get_db(setup["project"])
    # pending → running
    update_audio_isolation_status(setup["project"], iso_id, "running")
    row = conn.execute("SELECT status, error FROM audio_isolations WHERE id = ?", (iso_id,)).fetchone()
    assert row["status"] == "running"
    assert row["error"] is None
    # running → completed
    update_audio_isolation_status(setup["project"], iso_id, "completed")
    row = conn.execute("SELECT status, error FROM audio_isolations WHERE id = ?", (iso_id,)).fetchone()
    assert row["status"] == "completed"


def test_update_audio_isolation_status_stores_error(setup):
    iso_id = add_audio_isolation(
        setup["project"],
        entity_type="audio_clip",
        entity_id=setup["clip_id"],
        model="deepfilternet3",
        range_mode="full",
        trim_in=None,
        trim_out=None,
    )
    update_audio_isolation_status(
        setup["project"], iso_id, "failed", error="ffmpeg: input not found"
    )
    conn = get_db(setup["project"])
    row = conn.execute("SELECT status, error FROM audio_isolations WHERE id = ?", (iso_id,)).fetchone()
    assert row["status"] == "failed"
    assert row["error"] == "ffmpeg: input not found"


def test_update_audio_isolation_status_rejects_bad_status(setup):
    iso_id = add_audio_isolation(
        setup["project"],
        entity_type="audio_clip",
        entity_id=setup["clip_id"],
        model="deepfilternet3",
        range_mode="full",
        trim_in=None,
        trim_out=None,
    )
    with pytest.raises(AssertionError):
        update_audio_isolation_status(setup["project"], iso_id, "bogus")


# ── add_isolation_stem ────────────────────────────────────────────

def test_add_isolation_stem_creates_junction_row(setup):
    iso_id = add_audio_isolation(
        setup["project"],
        entity_type="audio_clip",
        entity_id=setup["clip_id"],
        model="deepfilternet3",
        range_mode="full",
        trim_in=None,
        trim_out=None,
    )
    add_isolation_stem(setup["project"], iso_id, setup["seg_vocal"], "vocal")
    conn = get_db(setup["project"])
    rows = conn.execute(
        "SELECT * FROM isolation_stems WHERE isolation_id = ?", (iso_id,)
    ).fetchall()
    assert len(rows) == 1
    assert rows[0]["pool_segment_id"] == setup["seg_vocal"]
    assert rows[0]["stem_type"] == "vocal"


def test_add_isolation_stem_is_idempotent(setup):
    """Second insert of same (isolation_id, pool_segment_id) pair is a no-op."""
    iso_id = add_audio_isolation(
        setup["project"],
        entity_type="audio_clip",
        entity_id=setup["clip_id"],
        model="deepfilternet3",
        range_mode="full",
        trim_in=None,
        trim_out=None,
    )
    add_isolation_stem(setup["project"], iso_id, setup["seg_vocal"], "vocal")
    # Same pair, different stem_type — INSERT OR IGNORE keeps first row.
    add_isolation_stem(setup["project"], iso_id, setup["seg_vocal"], "background")
    conn = get_db(setup["project"])
    rows = conn.execute(
        "SELECT * FROM isolation_stems WHERE isolation_id = ?", (iso_id,)
    ).fetchall()
    assert len(rows) == 1
    assert rows[0]["stem_type"] == "vocal"


# ── get_isolations_for_entity ─────────────────────────────────────

def test_get_isolations_for_entity_orders_newest_first_with_stems(setup):
    project = setup["project"]
    clip_id = setup["clip_id"]
    # Insert two runs; patch created_at via UPDATE so ordering is deterministic.
    iso_old = add_audio_isolation(
        project, entity_type="audio_clip", entity_id=clip_id,
        model="deepfilternet3", range_mode="full",
        trim_in=None, trim_out=None,
    )
    iso_new = add_audio_isolation(
        project, entity_type="audio_clip", entity_id=clip_id,
        model="deepfilternet3", range_mode="subset",
        trim_in=2.0, trim_out=6.0,
    )
    conn = get_db(project)
    conn.execute("UPDATE audio_isolations SET created_at = ? WHERE id = ?",
                 ("2026-01-01T00:00:00+00:00", iso_old))
    conn.execute("UPDATE audio_isolations SET created_at = ? WHERE id = ?",
                 ("2026-03-01T00:00:00+00:00", iso_new))
    conn.commit()

    add_isolation_stem(project, iso_new, setup["seg_vocal"], "vocal")
    add_isolation_stem(project, iso_new, setup["seg_bg"], "background")
    add_isolation_stem(project, iso_old, setup["seg_vocal"], "vocal")

    runs = get_isolations_for_entity(project, "audio_clip", clip_id)
    assert len(runs) == 2
    # Newest first
    assert runs[0]["id"] == iso_new
    assert runs[0]["range_mode"] == "subset"
    assert runs[0]["trim_in"] == 2.0
    assert runs[0]["trim_out"] == 6.0
    # Stems nested + joined with pool_segments
    stems = runs[0]["stems"]
    assert len(stems) == 2
    stems_by_type = {s["stem_type"]: s for s in stems}
    assert "vocal" in stems_by_type and "background" in stems_by_type
    assert stems_by_type["vocal"]["pool_path"] == "pool/segments/vocal.wav"
    assert stems_by_type["vocal"]["duration_seconds"] == 10.0
    assert stems_by_type["background"]["pool_path"] == "pool/segments/background.wav"
    # Older run still has its single stem.
    assert runs[1]["id"] == iso_old
    assert len(runs[1]["stems"]) == 1


def test_get_isolations_for_entity_returns_empty_when_none(setup):
    assert get_isolations_for_entity(setup["project"], "audio_clip", "missing_clip") == []


def test_get_isolations_for_entity_filters_by_entity(setup):
    """Runs for other entities don't leak into the result."""
    project = setup["project"]
    iso_a = add_audio_isolation(
        project, entity_type="audio_clip", entity_id=setup["clip_id"],
        model="deepfilternet3", range_mode="full",
        trim_in=None, trim_out=None,
    )
    add_audio_isolation(
        project, entity_type="audio_clip", entity_id="some_other_clip",
        model="deepfilternet3", range_mode="full",
        trim_in=None, trim_out=None,
    )
    add_audio_isolation(
        project, entity_type="transition", entity_id=setup["clip_id"],
        model="deepfilternet3", range_mode="full",
        trim_in=None, trim_out=None,
    )
    runs = get_isolations_for_entity(project, "audio_clip", setup["clip_id"])
    assert [r["id"] for r in runs] == [iso_a]


# ── get_isolation_stems ───────────────────────────────────────────

def test_get_isolation_stems_joins_pool_segments(setup):
    project = setup["project"]
    iso_id = add_audio_isolation(
        project, entity_type="audio_clip", entity_id=setup["clip_id"],
        model="deepfilternet3", range_mode="full",
        trim_in=None, trim_out=None,
    )
    add_isolation_stem(project, iso_id, setup["seg_vocal"], "vocal")
    add_isolation_stem(project, iso_id, setup["seg_bg"], "background")
    stems = get_isolation_stems(project, iso_id)
    assert len(stems) == 2
    by_type = {s["stem_type"]: s for s in stems}
    assert by_type["vocal"]["pool_segment_id"] == setup["seg_vocal"]
    assert by_type["vocal"]["pool_path"] == "pool/segments/vocal.wav"
    assert by_type["vocal"]["duration_seconds"] == 10.0
    assert by_type["background"]["pool_segment_id"] == setup["seg_bg"]
    assert by_type["background"]["pool_path"] == "pool/segments/background.wav"


def test_get_isolation_stems_empty(setup):
    iso_id = add_audio_isolation(
        setup["project"], entity_type="audio_clip", entity_id=setup["clip_id"],
        model="deepfilternet3", range_mode="full",
        trim_in=None, trim_out=None,
    )
    assert get_isolation_stems(setup["project"], iso_id) == []


# ── Undo/Redo round-trip ──────────────────────────────────────────

def test_undo_redo_round_trip_for_isolation_and_stems(setup):
    """Inside a single undo_group: insert isolation + 2 stems; undo removes all,
    redo restores all."""
    project = setup["project"]
    clip_id = setup["clip_id"]

    undo_begin(project, "test: add isolation + stems")
    iso_id = add_audio_isolation(
        project, entity_type="audio_clip", entity_id=clip_id,
        model="deepfilternet3", range_mode="full",
        trim_in=None, trim_out=None,
    )
    add_isolation_stem(project, iso_id, setup["seg_vocal"], "vocal")
    add_isolation_stem(project, iso_id, setup["seg_bg"], "background")

    conn = get_db(project)
    # Sanity before undo
    assert conn.execute(
        "SELECT COUNT(*) FROM audio_isolations WHERE id = ?", (iso_id,)
    ).fetchone()[0] == 1
    assert conn.execute(
        "SELECT COUNT(*) FROM isolation_stems WHERE isolation_id = ?", (iso_id,)
    ).fetchone()[0] == 2

    # Undo → gone
    group = undo_execute(project)
    assert group is not None
    assert conn.execute(
        "SELECT COUNT(*) FROM audio_isolations WHERE id = ?", (iso_id,)
    ).fetchone()[0] == 0
    assert conn.execute(
        "SELECT COUNT(*) FROM isolation_stems WHERE isolation_id = ?", (iso_id,)
    ).fetchone()[0] == 0

    # Redo → restored
    group2 = redo_execute(project)
    assert group2 is not None
    assert conn.execute(
        "SELECT COUNT(*) FROM audio_isolations WHERE id = ?", (iso_id,)
    ).fetchone()[0] == 1
    assert conn.execute(
        "SELECT COUNT(*) FROM isolation_stems WHERE isolation_id = ?", (iso_id,)
    ).fetchone()[0] == 2


# ── Plugin API re-exports ─────────────────────────────────────────

def test_plugin_api_reexports_isolation_helpers():
    """All five helpers must be importable from scenecraft.plugin_api."""
    from scenecraft import plugin_api

    for name in (
        "add_audio_isolation",
        "update_audio_isolation_status",
        "add_isolation_stem",
        "get_isolations_for_entity",
        "get_isolation_stems",
    ):
        assert hasattr(plugin_api, name), f"plugin_api missing {name}"
        assert name in plugin_api.__all__, f"plugin_api.__all__ missing {name}"
