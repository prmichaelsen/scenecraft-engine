"""Tests for ``compute_mix_graph_hash``.

Every factor that alters mix output must alter the hash; equivalent mix
states (even with dict reordering inside JSON-valued columns) must produce
the same hash.
"""

from __future__ import annotations

import json

import pytest

from scenecraft.db import (
    add_audio_clip,
    add_audio_track,
    add_track_effect,
    get_db,
    upsert_effect_curve,
    update_audio_clip,
    update_track_effect,
    upsert_track_send,
)
from scenecraft.mix_graph_hash import compute_mix_graph_hash


@pytest.fixture
def project(tmp_path):
    p = tmp_path / "hash_project"
    p.mkdir()
    get_db(p)  # force schema + default send buses
    return p


@pytest.fixture
def project2(tmp_path):
    p = tmp_path / "hash_project2"
    p.mkdir()
    get_db(p)
    return p


def _populate_basic(project_dir) -> tuple[str, str, str]:
    """Seed a track + clip + effect. Returns (track_id, clip_id, effect_id)."""
    track_id = "track_abc123"
    add_audio_track(project_dir, {
        "id": track_id,
        "name": "Main",
        "display_order": 0,
        "muted": False,
        "solo": False,
        "volume_curve": [[0.0, 0.8], [1.0, 0.8]],
    })
    clip_id = "clip_deadbeef"
    add_audio_clip(project_dir, {
        "id": clip_id,
        "track_id": track_id,
        "source_path": "pool/segments/a.wav",
        "start_time": 0.0,
        "end_time": 4.0,
        "source_offset": 0.0,
        "volume_curve": [[0.0, 0.7], [1.0, 0.7]],
        "muted": False,
    })
    eff = add_track_effect(
        project_dir,
        track_id=track_id,
        effect_type="drive",
        static_params={"gain_db": 6.0, "tone": 0.5},
    )
    return track_id, clip_id, eff.id


# -- Stability -----------------------------------------------------------

def test_stable_on_unchanged_db(project):
    _populate_basic(project)
    a = compute_mix_graph_hash(project)
    b = compute_mix_graph_hash(project)
    assert a == b
    assert len(a) == 64


# -- Sensitivity ---------------------------------------------------------

def test_track_add_changes_hash(project):
    _populate_basic(project)
    before = compute_mix_graph_hash(project)
    add_audio_track(project, {
        "id": "track_second",
        "name": "Second",
        "display_order": 1,
        "muted": False,
        "solo": False,
        "volume_curve": [[0.0, 0.5], [1.0, 0.5]],
    })
    after = compute_mix_graph_hash(project)
    assert before != after


def test_clip_volume_curve_edit_changes_hash(project):
    _track, clip_id, _eff = _populate_basic(project)
    before = compute_mix_graph_hash(project)
    update_audio_clip(project, clip_id, volume_curve=[[0.0, 0.2], [1.0, 0.2]])
    after = compute_mix_graph_hash(project)
    assert before != after


def test_send_level_add_changes_hash(project):
    track_id, _clip, _eff = _populate_basic(project)
    # Pick any default send bus seeded during schema init.
    bus_row = get_db(project).execute(
        "SELECT id FROM project_send_buses ORDER BY order_index LIMIT 1"
    ).fetchone()
    assert bus_row is not None
    bus_id = bus_row["id"]

    # Adding a send at level 0.4 bumps the hash. (Note: a zero-level send
    # row is auto-created when the track is added — every factor matters, so
    # even bumping level 0 → 0.4 via UPSERT must change the hash.)
    before = compute_mix_graph_hash(project)
    upsert_track_send(project, track_id=track_id, bus_id=bus_id, level=0.4)
    after_add = compute_mix_graph_hash(project)
    assert before != after_add

    # Changing the level on the same (track, bus) must bump the hash.
    upsert_track_send(project, track_id=track_id, bus_id=bus_id, level=0.9)
    after_edit = compute_mix_graph_hash(project)
    assert after_add != after_edit

    # Removing the send row entirely must bump the hash again.
    get_db(project).execute(
        "DELETE FROM track_sends WHERE track_id = ? AND bus_id = ?",
        (track_id, bus_id),
    )
    get_db(project).commit()
    after_remove = compute_mix_graph_hash(project)
    assert after_remove != after_edit


# -- Canonicalization ----------------------------------------------------

def test_static_params_key_reorder_preserves_hash(project):
    _track, _clip, eff_id = _populate_basic(project)
    # Initial static_params = {"gain_db": 6.0, "tone": 0.5}.
    baseline = compute_mix_graph_hash(project)

    # Rewrite the raw JSON column with keys in a different insertion order.
    # json.loads will reconstruct the dict; json.dumps(sort_keys=True) inside
    # the hash helper MUST produce the same canonical form.
    reordered = json.dumps({"tone": 0.5, "gain_db": 6.0})  # keys in opposite order
    get_db(project).execute(
        "UPDATE track_effects SET static_params = ? WHERE id = ?",
        (reordered, eff_id),
    )
    get_db(project).commit()

    after = compute_mix_graph_hash(project)
    assert after == baseline


def test_effect_param_change_changes_hash(project):
    _track, _clip, eff_id = _populate_basic(project)
    before = compute_mix_graph_hash(project)
    update_track_effect(project, eff_id, static_params={"gain_db": 12.0, "tone": 0.5})
    after = compute_mix_graph_hash(project)
    assert before != after


def test_effect_curve_edit_changes_hash(project):
    _track, _clip, eff_id = _populate_basic(project)
    upsert_effect_curve(
        project, effect_id=eff_id, param_name="gain_db",
        points=[[0.0, 0.0], [1.0, 1.0]],
    )
    before = compute_mix_graph_hash(project)
    upsert_effect_curve(
        project, effect_id=eff_id, param_name="gain_db",
        points=[[0.0, 0.0], [0.5, 0.5], [1.0, 1.0]],
    )
    after = compute_mix_graph_hash(project)
    assert before != after


# -- Reproducibility -----------------------------------------------------

def test_identical_state_across_projects(project, project2):
    """Two freshly-initialized projects with identical mix state produce the
    same hash. IDs are deterministic in this seed, so the hash should match
    bit-for-bit."""
    for p in (project, project2):
        add_audio_track(p, {
            "id": "track_abc",
            "name": "X",
            "display_order": 0,
            "muted": False,
            "solo": False,
            "volume_curve": [[0.0, 0.75], [1.0, 0.75]],
        })
        add_audio_clip(p, {
            "id": "clip_abc",
            "track_id": "track_abc",
            "source_path": "pool/segments/s.wav",
            "start_time": 1.0,
            "end_time": 5.0,
            "source_offset": 0.0,
            "volume_curve": [[0.0, 1.0], [1.0, 1.0]],
            "muted": False,
        })

    # Default send buses seeded by get_db would differ in their random IDs
    # between the two projects. Wipe them so the test compares only the
    # deterministically-seeded track/clip state.
    for p in (project, project2):
        conn = get_db(p)
        conn.execute("DELETE FROM track_sends")
        conn.execute("DELETE FROM project_send_buses")
        conn.commit()

    h1 = compute_mix_graph_hash(project)
    h2 = compute_mix_graph_hash(project2)
    assert h1 == h2
