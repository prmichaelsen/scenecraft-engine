"""Tests for M13 task-45: effect-curves + macro-panel schema & helpers.

Covers (spec local.effect-curves-macro-panel.md):
  * R1-R5 schema creation + idempotent re-apply of _ensure_schema
  * R2 UNIQUE(effect_id, param_name) blocks duplicate INSERT at SQL layer
    (spec test `effect-curves-unique-constraint`)
  * R3/R12 default buses seed exactly 4 rows in order (spec test
    `send-bus-defaults-on-new-project`)
  * R4 track_sends auto-insert on audio_track create + CASCADE on delete
    (spec test `track-sends-row-per-track-per-bus`)
  * R1/R14 CASCADE DELETE from track_effects → effect_curves (spec test
    `orphan-curve-cleaned-on-effect-delete`)
  * CRUD helper round-trip for each of the 5 tables
"""

from __future__ import annotations

import json
import sqlite3

import pytest

from scenecraft.db import (
    _ensure_schema,
    add_audio_track,
    add_effect_curve,
    add_frequency_label,
    add_send_bus,
    add_track_effect,
    delete_audio_track,
    delete_frequency_label,
    delete_send_bus,
    delete_track_effect,
    delete_track_send,
    get_db,
    get_effect_curve,
    get_frequency_label,
    get_send_bus,
    get_track_effect,
    get_track_send,
    list_curves_for_effect,
    list_frequency_labels,
    list_send_buses,
    list_track_effects,
    list_track_sends,
    update_effect_curve,
    update_frequency_label,
    update_send_bus,
    update_track_effect,
    upsert_effect_curve,
    upsert_track_send,
)
from scenecraft.db_models import (
    EffectCurve,
    FrequencyLabel,
    SendBus,
    TrackEffect,
)


@pytest.fixture
def project(tmp_path):
    project_dir = tmp_path / "fx_project"
    project_dir.mkdir()
    # Force schema creation on first access.
    get_db(project_dir)
    return project_dir


@pytest.fixture
def track(project):
    """Insert a baseline audio track; returns its id. The track_sends auto-seed
    trigger fires here, producing one row per default bus."""
    track_id = "track_fx_1"
    add_audio_track(project, {"id": track_id, "name": "Vox", "display_order": 0})
    return track_id


# -- Schema shape ---------------------------------------------------------

def test_schema_creates_all_five_tables(project):
    conn = get_db(project)
    names = {
        r["name"]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    for t in (
        "track_effects",
        "effect_curves",
        "project_send_buses",
        "track_sends",
        "project_frequency_labels",
    ):
        assert t in names, f"missing table: {t}"


def test_schema_creates_expected_indexes(project):
    conn = get_db(project)
    idx = {
        r["name"]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index'"
        ).fetchall()
    }
    assert "track_effects_track_order" in idx
    assert "idx_effect_curves_effect" in idx
    assert "idx_send_buses_order" in idx
    assert "idx_track_sends_bus" in idx


def test_ensure_schema_is_idempotent(project):
    """Re-applying _ensure_schema on a migrated DB must not raise + must not
    double-seed buses."""
    conn = get_db(project)
    _ensure_schema(conn)
    _ensure_schema(conn)
    # Default buses unchanged -- still exactly 4.
    assert conn.execute(
        "SELECT COUNT(*) FROM project_send_buses"
    ).fetchone()[0] == 4


def test_schema_creates_undo_triggers_for_new_tables(project):
    conn = get_db(project)
    trigs = {
        r["name"]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger'"
        ).fetchall()
    }
    for t in (
        "track_effects_insert_undo",
        "track_effects_update_undo",
        "track_effects_delete_undo",
        "effect_curves_insert_undo",
        "effect_curves_update_undo",
        "effect_curves_delete_undo",
        "project_send_buses_insert_undo",
        "project_send_buses_update_undo",
        "project_send_buses_delete_undo",
        "project_frequency_labels_insert_undo",
        "project_frequency_labels_update_undo",
        "project_frequency_labels_delete_undo",
        "track_sends_insert_undo",
        "track_sends_update_undo",
        "track_sends_delete_undo",
        "audio_tracks_seed_sends",
    ):
        assert t in trigs, f"missing trigger: {t}"


# -- Default bus seeding (spec test `send-bus-defaults-on-new-project`) --

def test_default_buses_seeded_in_order(project):
    """Fresh project should have Plate, Hall, Delay, Echo at order_index 0..3."""
    buses = list_send_buses(project)
    assert len(buses) == 4
    labels = [b.label for b in buses]
    assert labels == ["Plate", "Hall", "Delay", "Echo"]
    types = [b.bus_type for b in buses]
    assert types == ["reverb", "reverb", "delay", "echo"]
    orders = [b.order_index for b in buses]
    assert orders == [0, 1, 2, 3]
    # Static params carry the IR references + delay/echo params.
    assert buses[0].static_params == {"ir": "plate.wav"}
    assert buses[1].static_params == {"ir": "hall.wav"}
    assert buses[2].static_params["time_division"] == "1/4"
    assert buses[2].static_params["feedback"] == 0.35
    assert buses[3].static_params["time_ms"] == 120.0


def test_default_buses_backfill_for_existing_tracks(tmp_path):
    """Migration path: when a track is added post-schema, the auto-seed trigger
    should fill in one track_sends row per default bus."""
    project_dir = tmp_path / "pre_m13"
    project_dir.mkdir()
    conn = get_db(project_dir)
    conn.execute(
        "INSERT INTO audio_tracks (id, name, display_order) VALUES (?, ?, ?)",
        ("t_pre", "Pre-M13 track", 0),
    )
    conn.commit()
    sends = list_track_sends(project_dir, track_id="t_pre")
    # The trigger fires on INSERT, so we get 4 rows (one per already-seeded bus).
    assert len(sends) == 4


# -- Track-sends auto-insert (spec test `track-sends-row-per-track-per-bus`) --

def test_track_sends_auto_insert_on_audio_track_create(project, track):
    sends = list_track_sends(project, track_id=track)
    assert len(sends) == 4
    # All start at level 0.
    assert all(s.level == 0.0 for s in sends)
    # Each bus gets exactly one send for this track.
    bus_ids = {b.id for b in list_send_buses(project)}
    sent_bus_ids = {s.bus_id for s in sends}
    assert sent_bus_ids == bus_ids


def test_track_sends_cascade_on_audio_track_delete(project, track):
    conn = get_db(project)
    assert conn.execute(
        "SELECT COUNT(*) FROM track_sends WHERE track_id = ?", (track,)
    ).fetchone()[0] == 4
    delete_audio_track(project, track)
    assert conn.execute(
        "SELECT COUNT(*) FROM track_sends WHERE track_id = ?", (track,)
    ).fetchone()[0] == 0


def test_track_sends_cascade_on_bus_delete(project, track):
    buses = list_send_buses(project)
    conn = get_db(project)
    bus_to_delete = buses[0].id
    assert conn.execute(
        "SELECT COUNT(*) FROM track_sends WHERE bus_id = ?", (bus_to_delete,)
    ).fetchone()[0] == 1
    delete_send_bus(project, bus_to_delete)
    assert conn.execute(
        "SELECT COUNT(*) FROM track_sends WHERE bus_id = ?", (bus_to_delete,)
    ).fetchone()[0] == 0


def test_track_sends_composite_pk_blocks_duplicate(project, track):
    """PK (track_id, bus_id) enforced at SQL layer."""
    conn = get_db(project)
    bus_id = list_send_buses(project)[0].id
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO track_sends (track_id, bus_id, level) VALUES (?, ?, ?)",
            (track, bus_id, 0.5),
        )


# -- effect_curves UNIQUE constraint (spec test `effect-curves-unique-constraint`) --

def test_effect_curves_unique_constraint_blocks_duplicate_insert(project, track):
    """Spec R2: raw INSERT attempting a second curve on the same
    (effect_id, param_name) pair MUST fail at the SQL layer before reaching
    application code."""
    eff = add_track_effect(
        project, track_id=track, effect_type="highpass",
        static_params={"cutoff": 0.5},
    )
    # First curve inserts fine.
    add_effect_curve(
        project, effect_id=eff.id, param_name="cutoff",
        points=[[0.0, 0.2], [1.0, 0.8]],
    )
    conn = get_db(project)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO effect_curves (id, effect_id, param_name, points, interpolation, visible) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("curve_dupe", eff.id, "cutoff", "[]", "bezier", 0),
        )
    # Still exactly one row for the pair.
    rows = conn.execute(
        "SELECT COUNT(*) FROM effect_curves WHERE effect_id = ? AND param_name = ?",
        (eff.id, "cutoff"),
    ).fetchone()[0]
    assert rows == 1

    # The UPSERT path (upsert_effect_curve) still works -- it replaces points.
    updated = upsert_effect_curve(
        project, effect_id=eff.id, param_name="cutoff",
        points=[[0.0, 0.1], [1.0, 0.9]], interpolation="linear", visible=True,
    )
    assert updated.points == [[0.0, 0.1], [1.0, 0.9]]
    assert updated.interpolation == "linear"
    assert updated.visible is True
    rows_after = conn.execute(
        "SELECT COUNT(*) FROM effect_curves WHERE effect_id = ? AND param_name = ?",
        (eff.id, "cutoff"),
    ).fetchone()[0]
    assert rows_after == 1, "UPSERT must not create a second row"


# -- CASCADE: track_effects -> effect_curves (spec test `orphan-curve-cleaned-on-effect-delete`) --

def test_cascade_effect_delete_clears_curves(project, track):
    eff = add_track_effect(project, track_id=track, effect_type="drive")
    add_effect_curve(project, effect_id=eff.id, param_name="amount", points=[[0, 0.3]])
    add_effect_curve(project, effect_id=eff.id, param_name="tone", points=[[0, 0.7]])
    assert len(list_curves_for_effect(project, eff.id)) == 2
    delete_track_effect(project, eff.id)
    conn = get_db(project)
    assert conn.execute(
        "SELECT COUNT(*) FROM effect_curves WHERE effect_id = ?", (eff.id,)
    ).fetchone()[0] == 0


def test_cascade_audio_track_delete_clears_effects_and_curves(project, track):
    eff1 = add_track_effect(project, track_id=track, effect_type="compressor")
    eff2 = add_track_effect(project, track_id=track, effect_type="limiter")
    add_effect_curve(project, effect_id=eff1.id, param_name="threshold", points=[[0, 0.4]])
    add_effect_curve(project, effect_id=eff2.id, param_name="ceiling", points=[[0, 0.9]])
    delete_audio_track(project, track)
    conn = get_db(project)
    assert conn.execute(
        "SELECT COUNT(*) FROM track_effects WHERE track_id = ?", (track,)
    ).fetchone()[0] == 0
    # Curves cascade-deleted via the track_effects row going away.
    assert conn.execute(
        "SELECT COUNT(*) FROM effect_curves WHERE effect_id IN (?, ?)",
        (eff1.id, eff2.id),
    ).fetchone()[0] == 0


# -- Helper round-trips ---------------------------------------------------

def test_track_effect_round_trip(project, track):
    eff = add_track_effect(
        project, track_id=track, effect_type="eq_band",
        static_params={"q": 0.707}, enabled=True,
    )
    assert isinstance(eff, TrackEffect)
    assert eff.track_id == track
    assert eff.effect_type == "eq_band"
    assert eff.static_params == {"q": 0.707}
    assert eff.enabled is True
    assert eff.order_index == 0
    # get + list
    got = get_track_effect(project, eff.id)
    assert got is not None
    assert got.id == eff.id
    # order_index auto-increments.
    eff2 = add_track_effect(project, track_id=track, effect_type="pan")
    assert eff2.order_index == 1
    all_fx = list_track_effects(project, track)
    assert [e.effect_type for e in all_fx] == ["eq_band", "pan"]


def test_track_effect_update_toggles_enabled(project, track):
    eff = add_track_effect(project, track_id=track, effect_type="drive")
    update_track_effect(project, eff.id, enabled=False)
    got = get_track_effect(project, eff.id)
    assert got is not None
    assert got.enabled is False
    update_track_effect(project, eff.id, static_params={"character": "tube"})
    got2 = get_track_effect(project, eff.id)
    assert got2 is not None
    assert got2.static_params == {"character": "tube"}


def test_effect_curve_round_trip(project, track):
    eff = add_track_effect(project, track_id=track, effect_type="highpass")
    curve = add_effect_curve(
        project, effect_id=eff.id, param_name="cutoff",
        points=[[0.0, 0.2], [2.0, 0.8]],
        interpolation="bezier", visible=True,
    )
    assert isinstance(curve, EffectCurve)
    assert curve.points == [[0.0, 0.2], [2.0, 0.8]]
    assert curve.visible is True
    got = get_effect_curve(project, curve.id)
    assert got is not None
    assert got.param_name == "cutoff"
    update_effect_curve(project, curve.id, visible=False, interpolation="step")
    got2 = get_effect_curve(project, curve.id)
    assert got2 is not None
    assert got2.visible is False
    assert got2.interpolation == "step"


def test_upsert_effect_curve_preserves_row_id(project, track):
    """Repeated upsert on same (effect_id, param_name) should keep the same id."""
    eff = add_track_effect(project, track_id=track, effect_type="lowpass")
    c1 = upsert_effect_curve(
        project, effect_id=eff.id, param_name="cutoff", points=[[0, 0.2]],
    )
    c2 = upsert_effect_curve(
        project, effect_id=eff.id, param_name="cutoff",
        points=[[0, 0.9], [1, 0.1]], interpolation="linear",
    )
    assert c1.id == c2.id
    assert c2.points == [[0, 0.9], [1, 0.1]]
    # Exactly one row for the pair.
    assert len(list_curves_for_effect(project, eff.id)) == 1


def test_send_bus_round_trip(project):
    """Add a 5th custom bus on top of the 4 defaults."""
    bus = add_send_bus(
        project, bus_type="reverb", label="Chamber",
        static_params={"ir": "chamber.wav"},
    )
    assert isinstance(bus, SendBus)
    assert bus.order_index == 4
    all_buses = list_send_buses(project)
    assert len(all_buses) == 5
    assert all_buses[-1].label == "Chamber"
    # Update label.
    update_send_bus(project, bus.id, label="Small Chamber")
    got = get_send_bus(project, bus.id)
    assert got is not None
    assert got.label == "Small Chamber"
    delete_send_bus(project, bus.id)
    assert get_send_bus(project, bus.id) is None


def test_upsert_track_send_updates_level(project, track):
    buses = list_send_buses(project)
    bus_id = buses[0].id
    # Auto-seeded row starts at 0.0.
    got = get_track_send(project, track, bus_id)
    assert got is not None
    assert got.level == 0.0
    upsert_track_send(project, track_id=track, bus_id=bus_id, level=0.75)
    got2 = get_track_send(project, track, bus_id)
    assert got2 is not None
    assert got2.level == 0.75
    # A brand-new track gets auto-seeded rows too; upsert on one of them.
    add_audio_track(project, {"id": "track_fx_2", "name": "Drums", "display_order": 1})
    sends2 = list_track_sends(project, track_id="track_fx_2")
    assert len(sends2) == 4
    upsert_track_send(project, track_id="track_fx_2", bus_id=buses[1].id, level=0.5)
    got3 = get_track_send(project, "track_fx_2", buses[1].id)
    assert got3 is not None
    assert got3.level == 0.5


def test_delete_track_send(project, track):
    buses = list_send_buses(project)
    delete_track_send(project, track, buses[0].id)
    assert get_track_send(project, track, buses[0].id) is None
    # Others untouched.
    assert len(list_track_sends(project, track_id=track)) == 3


def test_frequency_label_round_trip(project):
    assert list_frequency_labels(project) == []
    lbl = add_frequency_label(
        project, label="Hat sparkle", freq_min_hz=11000, freq_max_hz=13000,
    )
    assert isinstance(lbl, FrequencyLabel)
    got = get_frequency_label(project, lbl.id)
    assert got is not None
    assert got.label == "Hat sparkle"
    assert got.freq_min_hz == 11000
    update_frequency_label(project, lbl.id, label="Hat air")
    got2 = get_frequency_label(project, lbl.id)
    assert got2 is not None
    assert got2.label == "Hat air"
    all_labels = list_frequency_labels(project)
    assert len(all_labels) == 1
    delete_frequency_label(project, lbl.id)
    assert list_frequency_labels(project) == []


# -- FK enforcement (effect_curves.effect_id -> track_effects.id) --------

def test_effect_curves_fk_enforced(project, track):
    """Sanity: PRAGMA foreign_keys=ON (set in get_db). Inserting a curve for a
    non-existent effect_id must raise. This is belt-and-braces for the
    `orphan-curve-cleaned-on-effect-delete` contract -- orphans cannot exist
    to begin with."""
    conn = get_db(project)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO effect_curves (id, effect_id, param_name, points) "
            "VALUES (?, ?, ?, ?)",
            ("c_orphan", "eff_does_not_exist", "gain", "[]"),
        )


def test_static_params_stored_as_json(project, track):
    """static_params round-trips complex dicts."""
    sp = {
        "character": "tube",
        "mix": 0.42,
        "nested": {"a": 1, "b": [1, 2, 3]},
    }
    eff = add_track_effect(project, track_id=track, effect_type="drive", static_params=sp)
    got = get_track_effect(project, eff.id)
    assert got is not None
    assert got.static_params == sp
    # Raw row stores it as TEXT.
    conn = get_db(project)
    raw = conn.execute(
        "SELECT static_params FROM track_effects WHERE id = ?", (eff.id,)
    ).fetchone()
    assert json.loads(raw["static_params"]) == sp
