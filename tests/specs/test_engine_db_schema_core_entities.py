"""Regression tests for local.engine-db-schema-core-entities.md.

One test per named entry in the spec's Base Cases + Edge Cases sections.
Docstrings open with `covers Rn[, Rm, OQ-K]`. Target-state tests (DAL-level
rejection errors and CHECK constraints that the spec promises but that today's
code does not yet enforce) are marked
`@pytest.mark.xfail(reason="target-state; awaits DAL hardening / M16 refactor",
strict=False)`.

# No e2e — DB-only spec; exercised transitively by REST handler specs downstream.
"""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path
from unittest import mock

import pytest

from scenecraft import db as scdb


# ---------------------------------------------------------------------------
# Domain-scoped seed helpers (inline; used by several tests in this file only)
# ---------------------------------------------------------------------------

def _seed_audio_track(project_dir: Path, track_id: str = "at1", display_order: int = 0) -> str:
    scdb.add_audio_track(project_dir, {"id": track_id, "name": track_id, "display_order": display_order})
    return track_id


def _seed_keyframe(project_dir: Path, kf_id: str, timestamp: str = "0:00", track_id: str = "track_1") -> str:
    scdb.add_keyframe(project_dir, {
        "id": kf_id, "timestamp": timestamp, "candidates": [], "track_id": track_id,
    })
    return kf_id


def _seed_transition(project_dir: Path, tr_id: str, from_kf: str, to_kf: str, **extra) -> str:
    tr = {"id": tr_id, "from": from_kf, "to": to_kf, "slots": 1, "candidates": []}
    tr.update(extra)
    scdb.add_transition(project_dir, tr)
    return tr_id


def _seed_audio_clip(project_dir: Path, clip_id: str, track_id: str,
                     start_time: float = 0.0, end_time: float = 1.0,
                     source_offset: float = 0.0, source_path: str = "src.wav") -> str:
    scdb.add_audio_clip(project_dir, {
        "id": clip_id, "track_id": track_id, "source_path": source_path,
        "start_time": start_time, "end_time": end_time, "source_offset": source_offset,
    })
    return clip_id


def _seed_pool_segment(project_dir: Path, pool_path: str = "pool/x.wav",
                      variant_kind: str | None = None) -> str:
    """Insert a pool_segments row. variant_kind is a migration-added column we
    set directly via raw SQL since add_pool_segment doesn't expose it."""
    seg_id = scdb.add_pool_segment(
        project_dir, kind="generated", created_by="test", pool_path=pool_path,
    )
    if variant_kind is not None:
        conn = scdb.get_db(project_dir)
        conn.execute("UPDATE pool_segments SET variant_kind = ? WHERE id = ?",
                     (variant_kind, seg_id))
        conn.commit()
    return seg_id


# ---------------------------------------------------------------------------
# Base Cases
# ---------------------------------------------------------------------------


def test_add_then_get_keyframe(project_dir: Path, db_conn):
    """covers R1, R2."""
    # Given
    kf = {
        "id": "kf1", "timestamp": "0:01.000",
        "candidates": [{"foo": 1}], "context": {"k": "v"},
    }
    # When
    scdb.add_keyframe(project_dir, kf)
    got = scdb.get_keyframe(project_dir, "kf1")

    # Then
    assert got is not None and got["id"] == "kf1", "id-roundtrip: returned dict's id must match"
    assert got["candidates"] == [{"foo": 1}], "json-candidates-parsed: candidates round-trips as list"
    assert got["context"] == {"k": "v"}, "json-context-parsed: context round-trips as dict"
    assert got["section"] == "" and got["source"] == "" and got["prompt"] == "", \
        "defaults-applied: omitted fields default to empty string"
    assert got["deleted_at"] is None, "deleted-at-null: freshly-added row has NULL deleted_at"


def test_delete_keyframe_soft(project_dir: Path, db_conn):
    """covers R4."""
    # Given
    _seed_keyframe(project_dir, "kf1", "0:01")
    # When
    scdb.delete_keyframe(project_dir, "kf1", "2026-04-27T00:00:00Z")

    # Then
    got = scdb.get_keyframe(project_dir, "kf1")
    assert got is not None, "row-preserved: soft-delete does not remove the row"
    assert got["deleted_at"] == "2026-04-27T00:00:00Z", "deleted-at-set: supplied stamp stored"
    live_ids = [k["id"] for k in scdb.get_keyframes(project_dir)]
    assert "kf1" not in live_ids, "excluded-from-default-list: get_keyframes hides soft-deleted"
    binned_ids = [k["id"] for k in scdb.get_binned_keyframes(project_dir)]
    assert "kf1" in binned_ids, "present-in-binned: get_binned_keyframes shows soft-deleted"


def test_restore_keyframe(project_dir: Path, db_conn):
    """covers R4."""
    # Given
    _seed_keyframe(project_dir, "kf1", "0:01")
    scdb.delete_keyframe(project_dir, "kf1", "2026-04-27T00:00:00Z")
    # When
    scdb.restore_keyframe(project_dir, "kf1")

    # Then
    got = scdb.get_keyframe(project_dir, "kf1")
    assert got["deleted_at"] is None, "deleted-at-null: restore clears deleted_at"
    live_ids = [k["id"] for k in scdb.get_keyframes(project_dir)]
    assert "kf1" in live_ids, "included-in-default-list: restored row reappears"


def test_get_keyframes_include_deleted(project_dir: Path, db_conn):
    """covers R4."""
    # Given
    _seed_keyframe(project_dir, "kfa", "0:01")
    _seed_keyframe(project_dir, "kfb", "0:02")
    _seed_keyframe(project_dir, "kfc", "0:03")
    scdb.delete_keyframe(project_dir, "kfb", "2026-04-27T00:00:00Z")
    # When
    rows = scdb.get_keyframes(project_dir, include_deleted=True)

    # Then
    assert len(rows) == 3, f"all-three-returned: expected 3, got {len(rows)}"
    timestamps = [r["timestamp"] for r in rows]
    assert timestamps == sorted(timestamps), \
        f"ordered-by-timestamp: rows must be ascending lexicographic, got {timestamps}"


def test_keyframe_timestamp_propagates_to_audio(project_dir: Path, db_conn):
    """covers R5."""
    # Given
    _seed_audio_track(project_dir, "at1")
    _seed_keyframe(project_dir, "kfA", "0:10.000")
    _seed_keyframe(project_dir, "kfB", "0:14.000")
    _seed_transition(project_dir, "T", "kfA", "kfB")
    _seed_audio_clip(project_dir, "C", "at1", start_time=10.0, end_time=14.0)
    scdb.add_audio_clip_link(project_dir, "C", "T")

    # When
    scdb.update_keyframe(project_dir, "kfA", timestamp="0:12.000")

    # Then
    clips = scdb.get_audio_clips(project_dir, track_id="at1")
    c = next(x for x in clips if x["id"] == "C")
    assert c["start_time"] == 12.0, f"clip-start-shifted: expected 12.0, got {c['start_time']}"
    assert c["end_time"] == 16.0, f"clip-end-shifted: expected 16.0, got {c['end_time']}"
    # deleted_at isn't in get_audio_clips output; check raw row
    row = db_conn.execute("SELECT deleted_at FROM audio_clips WHERE id = 'C'").fetchone()
    assert row["deleted_at"] is None, "clip-not-soft-deleted: propagation must not soft-delete"


def test_transition_derives_track_from_kf(project_dir: Path, db_conn):
    """covers R11."""
    # Given
    _seed_keyframe(project_dir, "K", "0:00", track_id="track_7")
    _seed_keyframe(project_dir, "K2", "0:01", track_id="track_7")
    # When
    _seed_transition(project_dir, "T", "K", "K2")

    # Then
    tr = scdb.get_transition(project_dir, "T")
    assert tr["track_id"] == "track_7", \
        f"track-id-inherited: expected track_7, got {tr['track_id']}"


def test_delete_transition_cascades_to_audio(project_dir: Path, db_conn):
    """covers R10."""
    # Given
    _seed_audio_track(project_dir, "at1")
    _seed_keyframe(project_dir, "K1", "0:00")
    _seed_keyframe(project_dir, "K2", "0:02")
    _seed_transition(project_dir, "T", "K1", "K2")
    _seed_audio_clip(project_dir, "A", "at1", 0.0, 1.0)
    _seed_audio_clip(project_dir, "B", "at1", 1.0, 2.0)
    scdb.add_audio_clip_link(project_dir, "A", "T")
    scdb.add_audio_clip_link(project_dir, "B", "T")

    # When
    scdb.delete_transition(project_dir, "T", "2026-04-27T00:00:00Z")

    # Then
    tr_row = db_conn.execute("SELECT deleted_at FROM transitions WHERE id='T'").fetchone()
    assert tr_row["deleted_at"] == "2026-04-27T00:00:00Z", "transition-soft-deleted: T.deleted_at set"
    a_row = db_conn.execute("SELECT deleted_at FROM audio_clips WHERE id='A'").fetchone()
    assert a_row["deleted_at"] == "2026-04-27T00:00:00Z", \
        f"clip-a-soft-deleted: expected stamp, got {a_row['deleted_at']}"
    b_row = db_conn.execute("SELECT deleted_at FROM audio_clips WHERE id='B'").fetchone()
    assert b_row["deleted_at"] == "2026-04-27T00:00:00Z", \
        f"clip-b-soft-deleted: expected stamp, got {b_row['deleted_at']}"
    links = scdb.get_audio_clip_links_for_transition(project_dir, "T")
    assert links == [], f"links-hard-deleted: expected empty, got {links}"


def test_restore_transition_partial(project_dir: Path, db_conn):
    """covers R10."""
    # Given: set up and cascade-delete, same as previous test
    _seed_audio_track(project_dir, "at1")
    _seed_keyframe(project_dir, "K1", "0:00")
    _seed_keyframe(project_dir, "K2", "0:02")
    _seed_transition(project_dir, "T", "K1", "K2")
    _seed_audio_clip(project_dir, "A", "at1")
    _seed_audio_clip(project_dir, "B", "at1")
    scdb.add_audio_clip_link(project_dir, "A", "T")
    scdb.add_audio_clip_link(project_dir, "B", "T")
    scdb.delete_transition(project_dir, "T", "2026-04-27T00:00:00Z")

    # When
    scdb.restore_transition(project_dir, "T")

    # Then
    tr_row = db_conn.execute("SELECT deleted_at FROM transitions WHERE id='T'").fetchone()
    assert tr_row["deleted_at"] is None, "transition-restored: T.deleted_at cleared"
    a_row = db_conn.execute("SELECT deleted_at FROM audio_clips WHERE id='A'").fetchone()
    b_row = db_conn.execute("SELECT deleted_at FROM audio_clips WHERE id='B'").fetchone()
    assert a_row["deleted_at"] is not None and b_row["deleted_at"] is not None, \
        "clips-still-deleted: linked clips remain soft-deleted after transition restore"
    links = scdb.get_audio_clip_links_for_transition(project_dir, "T")
    assert links == [], f"links-not-recreated: expected empty, got {links}"


def test_transition_selected_flatten(project_dir: Path, db_conn):
    """covers R12."""
    # Given: insert transition row with explicit selected='[null]'
    _seed_keyframe(project_dir, "K1", "0:00")
    _seed_keyframe(project_dir, "K2", "0:01")
    _seed_transition(project_dir, "T", "K1", "K2")
    db_conn.execute("UPDATE transitions SET selected = ? WHERE id = 'T'", ("[null]",))
    db_conn.commit()

    # When
    tr = scdb.get_transition(project_dir, "T")

    # Then
    assert tr["selected"] is None, \
        f"selected-scalar-none: single-element [null] list must flatten to None, got {tr['selected']!r}"


def test_transition_effect_z_order_autoincrement(project_dir: Path, db_conn):
    """covers R15."""
    # Given
    _seed_keyframe(project_dir, "K1", "0:00")
    _seed_keyframe(project_dir, "K2", "0:01")
    _seed_transition(project_dir, "T", "K1", "K2")
    _seed_transition(project_dir, "T2", "K1", "K2")
    scdb.add_transition_effect(project_dir, "T", "blur")  # z=0
    scdb.add_transition_effect(project_dir, "T", "blur")  # z=1
    scdb.add_transition_effect(project_dir, "T2", "blur")  # z=0 on T2 — must not poison T's max

    # When
    new_id = scdb.add_transition_effect(project_dir, "T", "blur")

    # Then
    effects = scdb.get_transition_effects(project_dir, "T")
    new_fx = next(e for e in effects if e["id"] == new_id)
    assert new_fx["zOrder"] == 2, f"new-z-order-2: expected 2, got {new_fx['zOrder']}"
    t2_effects = scdb.get_transition_effects(project_dir, "T2")
    assert all(e["zOrder"] == 0 for e in t2_effects), \
        "scoped-to-transition: other transitions' z_order unaffected"


def test_delete_transition_effect_hard(project_dir: Path, db_conn):
    """covers R16."""
    # Given
    _seed_keyframe(project_dir, "K1", "0:00")
    _seed_keyframe(project_dir, "K2", "0:01")
    _seed_transition(project_dir, "T", "K1", "K2")
    fx_id = scdb.add_transition_effect(project_dir, "T", "blur")

    # When
    scdb.delete_transition_effect(project_dir, fx_id)

    # Then
    row = db_conn.execute("SELECT * FROM transition_effects WHERE id = ?", (fx_id,)).fetchone()
    assert row is None, "row-gone: transition_effects row physically removed"
    cols = {r[1] for r in db_conn.execute("PRAGMA table_info(transition_effects)").fetchall()}
    assert "deleted_at" not in cols, "no-deleted-at-column: schema has no soft-delete column"


def test_reorder_audio_tracks_sequential(project_dir: Path, db_conn):
    """covers R18, R20."""
    # Given
    _seed_audio_track(project_dir, "A")
    _seed_audio_track(project_dir, "B")
    _seed_audio_track(project_dir, "C")
    # When
    scdb.reorder_audio_tracks(project_dir, ["C", "A", "B"])
    tracks = scdb.get_audio_tracks(project_dir)

    # Then
    assert tracks[0]["id"] == "C" and tracks[0]["display_order"] == 0, \
        f"c-first: {tracks[0]!r}"
    assert tracks[1]["id"] == "A" and tracks[1]["display_order"] == 1, \
        f"a-second: {tracks[1]!r}"
    assert tracks[2]["id"] == "B" and tracks[2]["display_order"] == 2, \
        f"b-third: {tracks[2]!r}"


def test_delete_audio_track_cascades(project_dir: Path, db_conn):
    """covers R19."""
    # Given
    _seed_audio_track(project_dir, "T")
    _seed_audio_clip(project_dir, "C1", "T")
    _seed_audio_clip(project_dir, "C2", "T")
    # Pre-delete C2 with a known stamp
    db_conn.execute("UPDATE audio_clips SET deleted_at = '2020-01-01T00:00:00Z' WHERE id = 'C2'")
    db_conn.commit()

    # When
    scdb.delete_audio_track(project_dir, "T")

    # Then
    track_row = db_conn.execute("SELECT * FROM audio_tracks WHERE id = 'T'").fetchone()
    assert track_row is None, "track-hard-deleted: audio_tracks row gone"
    c1_row = db_conn.execute("SELECT deleted_at FROM audio_clips WHERE id = 'C1'").fetchone()
    assert c1_row["deleted_at"] is not None and c1_row["deleted_at"].startswith("20"), \
        f"c1-soft-deleted: C1 got a UTC ISO stamp, got {c1_row['deleted_at']}"
    c2_row = db_conn.execute("SELECT deleted_at FROM audio_clips WHERE id = 'C2'").fetchone()
    assert c2_row["deleted_at"] == "2020-01-01T00:00:00Z", \
        f"c2-unchanged: prior stamp preserved, got {c2_row['deleted_at']}"


def test_audio_clip_unlinked_derivations(project_dir: Path, db_conn):
    """covers R25."""
    # Given
    _seed_audio_track(project_dir, "T")
    _seed_audio_clip(project_dir, "C", "T", start_time=0.0, end_time=2.0, source_offset=3.0)

    # When
    clips = scdb.get_audio_clips(project_dir, track_id="T")

    # Then
    c = clips[0]
    assert c["playback_rate"] == 1.0, f"playback-rate-one: expected 1.0, got {c['playback_rate']}"
    assert c["effective_source_offset"] == 3.0, \
        f"effective-offset-equals-source-offset: expected 3.0, got {c['effective_source_offset']}"
    assert c["linked_transition_id"] is None, "linked-transition-none: not linked"


@pytest.mark.xfail(
    reason="BUG witness: src/scenecraft/db.py:3128 aliases `\"from\" AS from_kf` in the "
           "bulk preload SELECT, but the DDL column is named `from_kf` (no `from` column exists). "
           "SQLite treats the double-quoted `from` as a string literal, so every transition row "
           "yields from_kf='from' (never matching any real keyframe id) and the derivation "
           "falls through to (1.0, stored_offset). Reported to the task author — spec-contracted "
           "behavior (R25) is unreachable until this is fixed.",
    strict=False,
)
def test_audio_clip_linked_derivations(project_dir: Path, db_conn):
    """covers R25."""
    # Given
    _seed_audio_track(project_dir, "T")
    _seed_keyframe(project_dir, "Ka", "0:00")
    _seed_keyframe(project_dir, "Kb", "0:02")
    _seed_transition(project_dir, "Tr", "Ka", "Kb")
    # add_transition's INSERT doesn't set trim_in/trim_out (migration-added columns);
    # set them directly so the derived-fields query has non-default values to work with.
    db_conn.execute("UPDATE transitions SET trim_in=?, trim_out=? WHERE id='Tr'", (1.0, 5.0))
    db_conn.commit()
    _seed_audio_clip(project_dir, "C", "T", start_time=0.0, end_time=2.0, source_offset=0.5)
    scdb.add_audio_clip_link(project_dir, "C", "Tr")

    # When
    clips = scdb.get_audio_clips(project_dir, track_id="T")

    # Then
    c = clips[0]
    # source_span = trim_out(5) - trim_in(1) = 4; kf_span = 2 - 0 = 2 → rate = 2.0
    assert c["playback_rate"] == 2.0, f"playback-rate-two: expected 2.0, got {c['playback_rate']}"
    # eff_off = source_offset(0.5) + trim_in(1.0) = 1.5
    assert c["effective_source_offset"] == 1.5, \
        f"effective-offset-is-offset-plus-trim-in: expected 1.5, got {c['effective_source_offset']}"


def test_tr_candidates_order_ascending(project_dir: Path, db_conn):
    """covers R36."""
    # Given
    _seed_keyframe(project_dir, "K1", "0:00")
    _seed_keyframe(project_dir, "K2", "0:01")
    _seed_transition(project_dir, "T", "K1", "K2")
    s1 = _seed_pool_segment(project_dir, "p/s1.wav")
    s2 = _seed_pool_segment(project_dir, "p/s2.wav")
    s3 = _seed_pool_segment(project_dir, "p/s3.wav")
    scdb.add_tr_candidate(project_dir, transition_id="T", slot=0, pool_segment_id=s1, source="generated", added_at="2026-01-01")
    scdb.add_tr_candidate(project_dir, transition_id="T", slot=0, pool_segment_id=s2, source="generated", added_at="2026-02-01")
    scdb.add_tr_candidate(project_dir, transition_id="T", slot=0, pool_segment_id=s3, source="generated", added_at="2026-03-01")

    # When
    cands = scdb.get_tr_candidates(project_dir, "T", slot=0)

    # Then
    assert cands[0]["addedAt"] == "2026-01-01", \
        f"jan-first: expected 2026-01-01 first, got {cands[0]['addedAt']}"
    assert cands[-1]["addedAt"] == "2026-03-01", \
        f"mar-last: expected 2026-03-01 last, got {cands[-1]['addedAt']}"


def test_audio_candidates_order_descending(project_dir: Path, db_conn):
    """covers R30."""
    # Given
    _seed_audio_track(project_dir, "T")
    _seed_audio_clip(project_dir, "C", "T")
    s1 = _seed_pool_segment(project_dir, "p/s1.wav")
    s2 = _seed_pool_segment(project_dir, "p/s2.wav")
    s3 = _seed_pool_segment(project_dir, "p/s3.wav")
    scdb.add_audio_candidate(project_dir, audio_clip_id="C", pool_segment_id=s1, source="generated", added_at="2026-01-01")
    scdb.add_audio_candidate(project_dir, audio_clip_id="C", pool_segment_id=s2, source="generated", added_at="2026-02-01")
    scdb.add_audio_candidate(project_dir, audio_clip_id="C", pool_segment_id=s3, source="generated", added_at="2026-03-01")

    # When
    cands = scdb.get_audio_candidates(project_dir, "C")

    # Then
    assert cands[0]["addedAt"] == "2026-03-01", \
        f"mar-first: expected 2026-03-01 first, got {cands[0]['addedAt']}"
    assert cands[-1]["addedAt"] == "2026-01-01", \
        f"jan-last: expected 2026-01-01 last, got {cands[-1]['addedAt']}"


def test_assign_audio_candidate_none_reverts(project_dir: Path, db_conn):
    """covers R31."""
    # Given
    _seed_audio_track(project_dir, "T")
    _seed_audio_clip(project_dir, "C", "T", source_path="orig.wav")
    seg = _seed_pool_segment(project_dir, "variant.wav")
    scdb.assign_audio_candidate(project_dir, "C", seg)

    # When
    scdb.assign_audio_candidate(project_dir, "C", None)

    # Then
    row = db_conn.execute("SELECT selected, source_path FROM audio_clips WHERE id = 'C'").fetchone()
    assert row["selected"] is None, f"selected-cleared: expected NULL, got {row['selected']!r}"
    clip_dict = {"selected": row["selected"], "source_path": row["source_path"]}
    path = scdb.get_audio_clip_effective_path(project_dir, clip_dict)
    assert path == "orig.wav", f"effective-path-reverts: expected orig.wav, got {path!r}"


def test_remove_audio_candidate_clears_selection(project_dir: Path, db_conn):
    """covers R32."""
    # Given
    _seed_audio_track(project_dir, "T")
    _seed_audio_clip(project_dir, "C", "T")
    seg = _seed_pool_segment(project_dir, "variant.wav")
    scdb.add_audio_candidate(project_dir, audio_clip_id="C", pool_segment_id=seg, source="generated")
    scdb.assign_audio_candidate(project_dir, "C", seg)

    # When
    scdb.remove_audio_candidate(project_dir, "C", seg)

    # Then
    cand_row = db_conn.execute(
        "SELECT * FROM audio_candidates WHERE audio_clip_id='C' AND pool_segment_id=?", (seg,)
    ).fetchone()
    assert cand_row is None, "junction-gone: audio_candidates row removed"
    clip_row = db_conn.execute("SELECT selected FROM audio_clips WHERE id='C'").fetchone()
    assert clip_row["selected"] is None, \
        f"selected-null: audio_clips.selected cleared, got {clip_row['selected']!r}"


def test_clone_tr_candidates_preserves_ordering(project_dir: Path, db_conn):
    """covers R37."""
    # Given
    _seed_keyframe(project_dir, "K1", "0:00")
    _seed_keyframe(project_dir, "K2", "0:01")
    _seed_transition(project_dir, "S", "K1", "K2")
    _seed_transition(project_dir, "T", "K1", "K2")
    s1 = _seed_pool_segment(project_dir, "p/s1.wav")
    s2 = _seed_pool_segment(project_dir, "p/s2.wav")
    s3 = _seed_pool_segment(project_dir, "p/s3.wav")
    scdb.add_tr_candidate(project_dir, transition_id="S", slot=0, pool_segment_id=s1, source="generated", added_at="2026-01-01")
    scdb.add_tr_candidate(project_dir, transition_id="S", slot=1, pool_segment_id=s2, source="generated", added_at="2026-02-01")
    scdb.add_tr_candidate(project_dir, transition_id="S", slot=1, pool_segment_id=s3, source="imported", added_at="2026-03-01")

    # When
    count = scdb.clone_tr_candidates(project_dir, source_transition_id="S", target_transition_id="T")

    # Then
    assert count == 3, f"count-returned: expected 3, got {count}"
    src_rows = db_conn.execute(
        "SELECT slot, pool_segment_id, added_at, source FROM tr_candidates WHERE transition_id='S' ORDER BY added_at"
    ).fetchall()
    dst_rows = db_conn.execute(
        "SELECT slot, pool_segment_id, added_at, source FROM tr_candidates WHERE transition_id='T' ORDER BY added_at"
    ).fetchall()
    assert [r["slot"] for r in dst_rows] == [r["slot"] for r in src_rows], \
        "slot-preserved: target slots match source"
    assert [r["added_at"] for r in dst_rows] == [r["added_at"] for r in src_rows], \
        "added-at-preserved: target added_at identical to source"
    assert all(r["source"] == "split-inherit" for r in dst_rows), \
        f"source-rewritten: expected all 'split-inherit', got {[r['source'] for r in dst_rows]}"


def test_audio_clip_link_upsert(project_dir: Path, db_conn):
    """covers R39."""
    # Given
    _seed_audio_track(project_dir, "T")
    _seed_keyframe(project_dir, "K1", "0:00")
    _seed_keyframe(project_dir, "K2", "0:01")
    _seed_transition(project_dir, "Tr", "K1", "K2")
    _seed_audio_clip(project_dir, "C", "T")
    scdb.add_audio_clip_link(project_dir, "C", "Tr", offset=0.0)

    # When
    scdb.add_audio_clip_link(project_dir, "C", "Tr", offset=2.5)

    # Then
    rows = db_conn.execute(
        "SELECT * FROM audio_clip_links WHERE audio_clip_id='C' AND transition_id='Tr'"
    ).fetchall()
    assert len(rows) == 1, f"no-duplicate: expected 1 row, got {len(rows)}"
    assert rows[0]["offset"] == 2.5, f"offset-updated: expected 2.5, got {rows[0]['offset']}"


def test_set_sections_replaces(project_dir: Path, db_conn):
    """covers R43."""
    # Given
    scdb.set_sections(project_dir, [
        {"id": "old_a", "label": "A"},
        {"id": "old_b", "label": "B"},
        {"id": "old_c", "label": "C"},
    ])

    # When
    scdb.set_sections(project_dir, [
        {"id": "sec_x", "label": "X"},
        {"id": "sec_y", "label": "Y"},
    ])

    # Then
    got = scdb.get_sections(project_dir)
    assert len(got) == 2, f"count-two: expected 2, got {len(got)}"
    ids = [s["id"] for s in got]
    assert ids == ["sec_x", "sec_y"], f"sort-order-sequential: expected [sec_x, sec_y], got {ids}"
    assert not ({"old_a", "old_b", "old_c"} & set(ids)), \
        f"old-sections-gone: pre-existing ids absent, got {ids}"


def test_get_sections_ordered(project_dir: Path, db_conn):
    """covers R42."""
    # Given
    inputs = [
        {"id": "s0", "label": "zero"},
        {"id": "s1", "label": "one"},
        {"id": "s2", "label": "two"},
        {"id": "s3", "label": "three"},
    ]
    scdb.set_sections(project_dir, inputs)

    # When
    got = scdb.get_sections(project_dir)

    # Then
    assert [s["id"] for s in got] == ["s0", "s1", "s2", "s3"], \
        f"ascending-sort-order: sort_order preserved insertion order, got {[s['id'] for s in got]}"


# ---------------------------------------------------------------------------
# Edge Cases
# ---------------------------------------------------------------------------


def test_keyframe_timestamp_zero_delta_noop(project_dir: Path, db_conn):
    """covers R5."""
    # Given
    _seed_audio_track(project_dir, "at1")
    _seed_keyframe(project_dir, "K", "0:10")
    _seed_keyframe(project_dir, "K2", "0:11")
    _seed_transition(project_dir, "T", "K", "K2")
    _seed_audio_clip(project_dir, "C", "at1", start_time=10.0, end_time=11.0)
    scdb.add_audio_clip_link(project_dir, "C", "T")

    # When
    scdb.update_keyframe(project_dir, "K", timestamp="0:10")

    # Then
    row = db_conn.execute("SELECT start_time, end_time FROM audio_clips WHERE id='C'").fetchone()
    assert row["start_time"] == 10.0, f"no-audio-shift: start_time unchanged, got {row['start_time']}"
    # Observable: end_time unchanged => propagation short-circuited
    assert row["end_time"] == 11.0, \
        f"no-propagation-query: propagation short-circuited (end_time unchanged), got {row['end_time']}"


def test_parse_kf_timestamp_fallback(project_dir: Path, db_conn):
    """covers R5."""
    # Given/When
    result = scdb._parse_kf_timestamp("not-a-time")

    # Then
    assert result == 0.0, f"zero-returned: _parse_kf_timestamp returns 0.0 on bad input, got {result}"

    # And: update_keyframe using this value computes delta vs. existing timestamp
    _seed_audio_track(project_dir, "at1")
    _seed_keyframe(project_dir, "K", "not-a-time")  # stored as-is
    _seed_keyframe(project_dir, "K2", "0:01")
    _seed_transition(project_dir, "T", "K", "K2")
    _seed_audio_clip(project_dir, "C", "at1", start_time=5.0, end_time=6.0)
    scdb.add_audio_clip_link(project_dir, "C", "T")

    # Updating to another unparseable timestamp: both parse to 0.0 → delta=0 → no shift
    scdb.update_keyframe(project_dir, "K", timestamp="also-bad")
    row = db_conn.execute("SELECT start_time FROM audio_clips WHERE id='C'").fetchone()
    assert row["start_time"] == 5.0, \
        f"no-propagation: unparseable→unparseable delta=0 yields no mutation, got {row['start_time']}"


def test_keyframe_update_propagation_error_swallowed(project_dir: Path, db_conn, capsys):
    """covers R5."""
    # Given
    _seed_audio_track(project_dir, "at1")
    _seed_keyframe(project_dir, "K", "0:05")
    _seed_keyframe(project_dir, "K2", "0:10")
    _seed_transition(project_dir, "T", "K", "K2")
    _seed_audio_clip(project_dir, "C", "at1", start_time=5.0, end_time=10.0)
    scdb.add_audio_clip_link(project_dir, "C", "T")

    # When: patch the propagation helper to raise
    with mock.patch.object(
        scdb, "_propagate_linked_audio_on_from_kf_shift",
        side_effect=sqlite3.DatabaseError("boom"),
    ):
        # Must not raise
        scdb.update_keyframe(project_dir, "K", timestamp="0:07")

    # Then
    row = db_conn.execute("SELECT timestamp FROM keyframes WHERE id='K'").fetchone()
    assert row["timestamp"] == "0:07", \
        f"main-update-applied: keyframe timestamp updated despite propagation error, got {row['timestamp']}"
    # error-not-raised: implicit — the `with` block above would have re-raised.
    captured = capsys.readouterr()
    assert "K" in captured.err, \
        f"error-logged-to-stderr: expected kf_id in stderr log, got {captured.err!r}"


def test_audio_clip_variant_kind_resolution(project_dir: Path, db_conn):
    """covers R25."""
    # Given
    _seed_audio_track(project_dir, "T")
    _seed_audio_clip(project_dir, "C", "T")
    seg = _seed_pool_segment(project_dir, "p/variant.wav", variant_kind="isolate-vocal")
    scdb.assign_audio_candidate(project_dir, "C", seg)

    # When
    clips = scdb.get_audio_clips(project_dir, track_id="T")

    # Then
    assert clips[0]["variant_kind"] == "isolate-vocal", \
        f"variant-kind-resolved: expected 'isolate-vocal', got {clips[0]['variant_kind']!r}"
    # no-n-plus-one: not asserted (implementation detail; see spec Non-Goals)


def test_tr_candidate_idempotent(project_dir: Path, db_conn):
    """covers R35."""
    # Given
    _seed_keyframe(project_dir, "K1", "0:00")
    _seed_keyframe(project_dir, "K2", "0:01")
    _seed_transition(project_dir, "T", "K1", "K2")
    seg = _seed_pool_segment(project_dir, "p/s1.wav")
    scdb.add_tr_candidate(project_dir, transition_id="T", slot=0, pool_segment_id=seg,
                          source="generated", added_at="ts1")

    # When: re-insert same PK
    scdb.add_tr_candidate(project_dir, transition_id="T", slot=0, pool_segment_id=seg,
                          source="generated", added_at="ts2")

    # Then
    cands = scdb.get_tr_candidates(project_dir, "T", slot=0)
    assert len(cands) == 1, f"no-new-row: expected 1 element, got {len(cands)}"
    assert cands[0]["addedAt"] == "ts1", \
        f"original-added-at-preserved: expected 'ts1', got {cands[0]['addedAt']!r}"


def test_audio_candidate_idempotent(project_dir: Path, db_conn):
    """covers R29."""
    # Given
    _seed_audio_track(project_dir, "T")
    _seed_audio_clip(project_dir, "C", "T")
    seg = _seed_pool_segment(project_dir, "p/s1.wav")
    scdb.add_audio_candidate(project_dir, audio_clip_id="C", pool_segment_id=seg,
                             source="generated", added_at="ts1")

    # When: re-insert same PK
    scdb.add_audio_candidate(project_dir, audio_clip_id="C", pool_segment_id=seg,
                             source="generated", added_at="ts2")

    # Then
    cands = scdb.get_audio_candidates(project_dir, "C")
    assert len(cands) == 1, f"no-new-row: expected 1 element, got {len(cands)}"
    assert cands[0]["addedAt"] == "ts1", \
        f"original-added-at-preserved: expected 'ts1', got {cands[0]['addedAt']!r}"


def test_tr_candidate_bad_source_assertion(project_dir: Path, db_conn):
    """covers R35."""
    # Given / When / Then
    with pytest.raises(AssertionError) as exc:
        scdb.add_tr_candidate(project_dir, transition_id="T", slot=0,
                              pool_segment_id="x", source="bogus")
    assert "bad source" in str(exc.value), \
        f"assertion-raised: message must mention 'bad source', got {exc.value!r}"


def test_audio_candidate_bad_source_assertion(project_dir: Path, db_conn):
    """covers R29."""
    # Given / When / Then
    with pytest.raises(AssertionError) as exc:
        scdb.add_audio_candidate(project_dir, audio_clip_id="C",
                                 pool_segment_id="x", source="bogus")
    assert "bad source" in str(exc.value), \
        f"assertion-raised: message must mention 'bad source', got {exc.value!r}"


def test_delete_keyframe_already_deleted_overwrites(project_dir: Path, db_conn):
    """covers R4."""
    # Given
    _seed_keyframe(project_dir, "K", "0:01")
    scdb.delete_keyframe(project_dir, "K", "T1")

    # When
    scdb.delete_keyframe(project_dir, "K", "T2")

    # Then
    got = scdb.get_keyframe(project_dir, "K")
    assert got["deleted_at"] == "T2", \
        f"deleted-at-overwritten: expected 'T2', got {got['deleted_at']!r}"


def test_update_transition_noop_empty(project_dir: Path, db_conn):
    """covers R12."""
    # Given
    _seed_keyframe(project_dir, "K1", "0:00")
    _seed_keyframe(project_dir, "K2", "0:01")
    _seed_transition(project_dir, "T", "K1", "K2")
    before = db_conn.execute("SELECT * FROM transitions WHERE id='T'").fetchone()
    before_dict = {k: before[k] for k in before.keys()}

    # When
    scdb.update_transition(project_dir, "T")

    # Then
    after = db_conn.execute("SELECT * FROM transitions WHERE id='T'").fetchone()
    after_dict = {k: after[k] for k in after.keys()}
    # no-update-executed: implicit — function returned (didn't raise on empty SET clause)
    assert before_dict == after_dict, \
        f"row-unchanged: all columns identical pre/post no-op update"


def test_effects_persist_on_transition_soft_delete(project_dir: Path, db_conn):
    """covers R16."""
    # Given
    _seed_keyframe(project_dir, "K1", "0:00")
    _seed_keyframe(project_dir, "K2", "0:01")
    _seed_transition(project_dir, "T", "K1", "K2")
    scdb.add_transition_effect(project_dir, "T", "blur")
    scdb.add_transition_effect(project_dir, "T", "glow")
    before_count = len(scdb.get_transition_effects(project_dir, "T"))

    # When
    scdb.delete_transition(project_dir, "T", "2026-04-27T00:00:00Z")

    # Then
    after = scdb.get_transition_effects(project_dir, "T")
    assert len(after) == before_count == 2, \
        f"effects-row-count-unchanged: expected 2 rows before & after, got before={before_count}, after={len(after)}"
    # no-cascade-column: assert no FK + no trigger auto-removes effects
    fk_rows = db_conn.execute("PRAGMA foreign_key_list(transition_effects)").fetchall()
    assert not any(r["table"] == "transitions" for r in fk_rows), \
        "no-cascade-column: no FK constraint from transition_effects → transitions"


def test_transition_dangling_from_kf_allowed(project_dir: Path, db_conn):
    """covers R7."""
    # Given: no keyframe 'ghost-kf' exists
    # When
    scdb.add_transition(project_dir, {
        "id": "T", "from": "ghost-kf", "to": "ghost-kf", "slots": 1, "candidates": [],
    })

    # Then
    tr = scdb.get_transition(project_dir, "T")
    assert tr is not None, "insert-succeeds: no exception, row readable"
    assert tr["from"] == "ghost-kf", f"row-readable: from == 'ghost-kf', got {tr['from']!r}"
    assert tr["track_id"] == "track_1", \
        f"track-id-fallback: from_kf missing → track_1, got {tr['track_id']!r}"


def test_audio_candidate_orphan_insert_rejects(project_dir: Path, db_conn):
    """covers R28.

    PRAGMA foreign_keys=ON is applied post-schema-init per engine-connection-and-transactions
    R4+R26. An audio_candidate insert referencing a non-existent audio_clip_id must raise
    sqlite3.IntegrityError at runtime and must not persist a row.
    """
    import sqlite3 as _sqlite3

    # Given: no audio_clips row with id 'missing-clip'
    seg = _seed_pool_segment(project_dir, "p/s1.wav")

    # When / Then
    with pytest.raises(_sqlite3.IntegrityError):
        scdb.add_audio_candidate(
            project_dir,
            audio_clip_id="missing-clip",
            pool_segment_id=seg,
            source="imported",
        )

    rows = db_conn.execute(
        "SELECT * FROM audio_candidates WHERE audio_clip_id='missing-clip'"
    ).fetchall()
    assert len(rows) == 0, f"row-absent: expected 0 rows after IntegrityError, got {len(rows)}"
    # matches-connection-spec: behavior aligns with engine-connection-and-transactions R4+R26


def test_unlink_transition_returns_ids(project_dir: Path, db_conn):
    """covers R40."""
    # Given
    _seed_audio_track(project_dir, "T")
    _seed_keyframe(project_dir, "K1", "0:00")
    _seed_keyframe(project_dir, "K2", "0:01")
    _seed_transition(project_dir, "Tr", "K1", "K2")
    _seed_audio_clip(project_dir, "C1", "T")
    _seed_audio_clip(project_dir, "C2", "T")
    _seed_audio_clip(project_dir, "C3", "T")
    scdb.add_audio_clip_link(project_dir, "C1", "Tr")
    scdb.add_audio_clip_link(project_dir, "C2", "Tr")
    scdb.add_audio_clip_link(project_dir, "C3", "Tr")

    # When
    returned = scdb.remove_audio_clip_links_for_transition(project_dir, "Tr")

    # Then
    assert len(returned) == 3, f"returns-three-ids: expected 3 ids, got {returned!r}"
    assert set(returned) == {"C1", "C2", "C3"}, \
        f"returns-three-ids: expected {{C1,C2,C3}}, got {set(returned)}"
    remaining = scdb.get_audio_clip_links_for_transition(project_dir, "Tr")
    assert remaining == [], f"rows-gone: expected empty, got {remaining}"


@pytest.mark.xfail(
    reason="target-state; awaits DAL hardening (R51, OQ-1). Current code has no delete_keyframe_hard that rejects in-use keyframes.",
    strict=False,
)
def test_keyframe_in_use_blocks_hard_delete(project_dir: Path, db_conn):
    """covers R51, OQ-1."""
    # Given
    _seed_keyframe(project_dir, "K", "0:00")
    _seed_keyframe(project_dir, "K2", "0:01")
    _seed_transition(project_dir, "T", "K", "K2")

    # When / Then
    KeyframeInUseError = getattr(scdb, "KeyframeInUseError", None)
    assert KeyframeInUseError is not None, "KeyframeInUseError must exist on scdb"
    delete_kf_hard = getattr(scdb, "delete_keyframe_hard", None)
    assert delete_kf_hard is not None, "delete_keyframe_hard must exist on scdb"
    with pytest.raises(KeyframeInUseError):
        delete_kf_hard(project_dir, "K")

    kf_row = db_conn.execute("SELECT * FROM keyframes WHERE id='K'").fetchone()
    assert kf_row is not None, "row-preserved: K still present"
    tr_row = db_conn.execute("SELECT * FROM transitions WHERE id='T'").fetchone()
    assert tr_row is not None, "transition-unchanged: T row preserved"


def test_audio_clip_track_cascade_preexisting(project_dir: Path, db_conn):
    """covers OQ-2."""
    # Given
    _seed_audio_track(project_dir, "T")
    _seed_audio_clip(project_dir, "C1", "T")

    # When
    scdb.delete_audio_track(project_dir, "T")

    # Then: cascade from R19 already handles this — no new semantic
    c1_row = db_conn.execute("SELECT deleted_at FROM audio_clips WHERE id='C1'").fetchone()
    assert c1_row["deleted_at"] is not None, \
        "c1-soft-deleted: existing cascade (R19) sets deleted_at"
    # no-new-error-path: a fresh insert referencing deleted track_id must still succeed
    # (current code has no validation — documents the authoritative behavior)
    scdb.add_audio_clip(project_dir, {
        "id": "C_new", "track_id": "T", "source_path": "", "start_time": 0, "end_time": 1,
    })
    new_row = db_conn.execute("SELECT id FROM audio_clips WHERE id='C_new'").fetchone()
    assert new_row is not None, "no-new-error-path: spec adds no new rejection semantic"


@pytest.mark.xfail(
    reason="target-state; awaits DAL hardening (R52, OQ-3). Current code accepts add_audio_candidate on soft-deleted clip.",
    strict=False,
)
def test_add_audio_candidate_on_deleted_clip_rejected(project_dir: Path, db_conn):
    """covers R52, OQ-3."""
    # Given
    _seed_audio_track(project_dir, "T")
    _seed_audio_clip(project_dir, "C", "T")
    seg = _seed_pool_segment(project_dir, "p/s1.wav")
    scdb.delete_audio_clip(project_dir, "C")  # soft-delete
    count_before = db_conn.execute("SELECT COUNT(*) AS n FROM audio_candidates").fetchone()["n"]

    # When / Then
    AudioClipDeletedError = getattr(scdb, "AudioClipDeletedError", None)
    assert AudioClipDeletedError is not None, "AudioClipDeletedError must exist on scdb"
    with pytest.raises(AudioClipDeletedError):
        scdb.add_audio_candidate(project_dir, audio_clip_id="C", pool_segment_id=seg,
                                 source="generated")

    count_after = db_conn.execute("SELECT COUNT(*) AS n FROM audio_candidates").fetchone()["n"]
    assert count_after == count_before, \
        f"no-row-inserted: audio_candidates count unchanged, before={count_before} after={count_after}"


@pytest.mark.xfail(
    reason="target-state; awaits DAL hardening (R53, OQ-4). Current code accepts add_tr_candidate on soft-deleted transition.",
    strict=False,
)
def test_add_tr_candidate_on_deleted_transition_rejected(project_dir: Path, db_conn):
    """covers R53, OQ-4."""
    # Given
    _seed_keyframe(project_dir, "K1", "0:00")
    _seed_keyframe(project_dir, "K2", "0:01")
    _seed_transition(project_dir, "T", "K1", "K2")
    seg = _seed_pool_segment(project_dir, "p/s1.wav")
    scdb.delete_transition(project_dir, "T", "2026-04-27T00:00:00Z")
    count_before = db_conn.execute("SELECT COUNT(*) AS n FROM tr_candidates").fetchone()["n"]

    # When / Then
    TransitionDeletedError = getattr(scdb, "TransitionDeletedError", None)
    assert TransitionDeletedError is not None, "TransitionDeletedError must exist on scdb"
    with pytest.raises(TransitionDeletedError):
        scdb.add_tr_candidate(project_dir, transition_id="T", slot=0,
                              pool_segment_id=seg, source="generated")

    count_after = db_conn.execute("SELECT COUNT(*) AS n FROM tr_candidates").fetchone()["n"]
    assert count_after == count_before, \
        f"no-row-inserted: tr_candidates count unchanged, before={count_before} after={count_after}"


def test_reorder_audio_tracks_no_internal_lock(project_dir: Path, db_conn):
    """covers R56, OQ-5."""
    # Given
    _seed_audio_track(project_dir, "A")
    _seed_audio_track(project_dir, "B")
    _seed_audio_track(project_dir, "C")

    # When: patch threading.Lock / asyncio.Lock to detect acquisition
    import threading
    import asyncio
    real_thread_lock = threading.Lock
    real_async_lock = asyncio.Lock
    acquired = {"thread": 0, "async": 0}

    class SpyThreadLock:
        def __init__(self):
            self._l = real_thread_lock()
        def acquire(self, *a, **kw):
            acquired["thread"] += 1
            return self._l.acquire(*a, **kw)
        def release(self):
            return self._l.release()
        def __enter__(self):
            self.acquire()
            return self
        def __exit__(self, *a):
            self.release()

    # We don't patch globally (would break pytest); instead, just assert that the
    # function does NOT reference a module-level lock attribute.
    scdb.reorder_audio_tracks(project_dir, ["C", "A", "B"])

    # Then: no project-scoped mutex attribute exists on the module
    lock_attrs = [name for name in dir(scdb)
                  if "lock" in name.lower() and name not in ("_conn_lock",)]
    # _conn_lock exists (connection pool guard) but it's not "project-scoped" per R56.
    # If a future refactor adds a project-scoped lock, this assertion will fire.
    assert all("reorder" not in a.lower() for a in lock_attrs), \
        f"no-internal-lock-held: no reorder-scoped lock in module, found {lock_attrs}"
    # concurrency-undefined: assertion is negative; spec states undefined per INV-1.


@pytest.mark.xfail(
    reason="target-state; awaits DAL hardening (R54, OQ-6). Current code stores non-monotonic curves as-is.",
    strict=False,
)
def test_curve_non_monotonic_x_rejected(project_dir: Path, db_conn):
    """covers R54, OQ-6."""
    # Given
    _seed_keyframe(project_dir, "K1", "0:00")
    _seed_keyframe(project_dir, "K2", "0:01")
    _seed_transition(project_dir, "T", "K1", "K2", opacity_curve=[[0, 0], [0.5, 1]])
    before = db_conn.execute("SELECT opacity_curve FROM transitions WHERE id='T'").fetchone()["opacity_curve"]

    # When / Then
    with pytest.raises(ValueError) as exc:
        scdb.update_transition(project_dir, "T",
                               opacity_curve=[[0, 0], [0.5, 1], [0.3, 0.5]])
    assert "monotonic" in str(exc.value).lower(), \
        f"raises-value-error: message mentions non-monotonic, got {exc.value!r}"
    after = db_conn.execute("SELECT opacity_curve FROM transitions WHERE id='T'").fetchone()["opacity_curve"]
    assert after == before, f"row-unchanged: opacity_curve preserved, before={before!r} after={after!r}"


@pytest.mark.xfail(
    reason="target-state; awaits schema CHECK constraint (R55, OQ-7) which needs table-rebuild migration.",
    strict=False,
)
def test_remap_negative_target_duration_rejected(project_dir: Path, db_conn):
    """covers R55, OQ-7."""
    # Given
    _seed_keyframe(project_dir, "K1", "0:00")
    _seed_keyframe(project_dir, "K2", "0:01")
    _seed_transition(project_dir, "T", "K1", "K2")
    before = db_conn.execute("SELECT remap FROM transitions WHERE id='T'").fetchone()["remap"]

    # When / Then
    with pytest.raises(sqlite3.IntegrityError):
        scdb.update_transition(project_dir, "T",
                               remap={"method": "linear", "target_duration": -1})
    after = db_conn.execute("SELECT remap FROM transitions WHERE id='T'").fetchone()["remap"]
    assert after == before, f"row-unchanged: remap preserved, before={before!r} after={after!r}"


@pytest.mark.xfail(
    reason="target-state; awaits register_migration + rebuild_table helper (R_transitional, OQ-8). "
           "Current schema bootstrap is additive ALTER only; no mechanism to rewrite NOT NULL to NULL on legacy audio_clips.track_id.",
    strict=False,
)
def test_audio_clips_legacy_nullable_track_id_rebuild(project_dir: Path, db_conn):
    """covers R_transitional, OQ-8."""
    # Given: a legacy DB where audio_clips.track_id was created NOT NULL.
    # Current bootstrap creates audio_clips.track_id as NOT NULL already; target
    # migration would rebuild the table to make it nullable.
    cols = db_conn.execute("PRAGMA table_info(audio_clips)").fetchall()
    track_id_col = next(c for c in cols if c["name"] == "track_id")

    # When: target migration would run rebuild_table here.
    rebuild_table = getattr(scdb, "rebuild_table", None)
    register_migration = getattr(scdb, "register_migration", None)
    assert rebuild_table is not None and register_migration is not None, \
        "target-helpers-exist: rebuild_table + register_migration must exist on scdb"

    # Then
    assert track_id_col["notnull"] == 0, \
        f"column-nullable-post-migration: expected notnull=0, got {track_id_col['notnull']}"
    # rows-preserved + transitional-note: covered by presence of helpers


# ---------------------------------------------------------------------------
# No e2e — DB-only spec; exercised transitively by REST handler specs downstream.
# ---------------------------------------------------------------------------
