"""Regression tests for local.engine-db-undo-redo.md.

One test per named entry in the spec's Base Cases + Edge Cases sections.
Docstrings open with `covers Rn[, Rm, OQ-K]`. Target-state tests (behaviors
that depend on R18 API rename, R20 per-group cap, R21 schema_version,
R22 completed_at sweep, R23 lock audit, R24 savepoint-rollback) are marked
`@pytest.mark.xfail(reason="target-state; awaits <X>", strict=False)`.

E2E section at bottom exercises the live HTTP undo/redo endpoints.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path

import pytest

from scenecraft import db as scdb


# ---------------------------------------------------------------------------
# Domain-scoped seed helpers (inline; undo_-prefixed).
# ---------------------------------------------------------------------------

ROW_ID_TRACKED_TABLES = [
    "keyframes", "transitions", "suppressions", "effects", "tracks",
    "transition_effects", "markers", "audio_tracks", "audio_clips",
    "audio_isolations", "track_effects", "effect_curves",
    "project_send_buses", "project_frequency_labels",
]
COMPOSITE_PK_TRACKED_TABLES = ["isolation_stems", "track_sends"]


def undo_seed_keyframe(project_dir: Path, kf_id: str, *, timestamp: str = "0:00",
                       track_id: str = "track_1", **extra) -> str:
    kf = {"id": kf_id, "timestamp": timestamp, "candidates": [], "track_id": track_id}
    kf.update(extra)
    scdb.add_keyframe(project_dir, kf)
    return kf_id


def undo_count_log(conn, group_id: int | None = None) -> int:
    if group_id is None:
        return conn.execute("SELECT COUNT(*) FROM undo_log").fetchone()[0]
    return conn.execute(
        "SELECT COUNT(*) FROM undo_log WHERE undo_group = ?", (group_id,)
    ).fetchone()[0]


def undo_count_groups(conn) -> int:
    return conn.execute("SELECT COUNT(*) FROM undo_groups").fetchone()[0]


# ---------------------------------------------------------------------------
# Base Cases
# ---------------------------------------------------------------------------


def test_schema_initialized(project_dir: Path, db_conn):
    """covers R1 — undo_log, redo_log, undo_groups, undo_state created."""
    tables = {r[0] for r in db_conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()}
    assert "undo_log" in tables, "tables-exist: undo_log missing"
    assert "redo_log" in tables, "tables-exist: redo_log missing"
    assert "undo_groups" in tables, "tables-exist: undo_groups missing"
    assert "undo_state" in tables, "tables-exist: undo_state missing"

    ul_cols = {r[1]: (r[2], r[3]) for r in db_conn.execute(
        "PRAGMA table_info(undo_log)"
    ).fetchall()}
    assert "seq" in ul_cols and ul_cols["seq"][0].upper() == "INTEGER", "undo-log-columns: seq"
    assert ul_cols["undo_group"][1] == 1, "undo-log-columns: undo_group NOT NULL"
    assert ul_cols["sql_text"][1] == 1, "undo-log-columns: sql_text NOT NULL"

    ug_cols = {r[1]: r for r in db_conn.execute(
        "PRAGMA table_info(undo_groups)"
    ).fetchall()}
    assert "id" in ug_cols and ug_cols["id"][5] == 1, "undo-groups-columns: id is PK"
    assert "description" in ug_cols, "undo-groups-columns: description"
    assert "timestamp" in ug_cols, "undo-groups-columns: timestamp"
    assert "undone" in ug_cols, "undo-groups-columns: undone"


def test_undo_state_seeds_present(project_dir: Path, db_conn):
    """covers R2 — seed rows current_group=0 and active=1."""
    rows = dict(db_conn.execute("SELECT key, value FROM undo_state").fetchall())
    assert rows.get("current_group") == 0, "current-group-seeded"
    assert rows.get("active") == 1, "active-seeded"

    # Re-run initialization (close, re-open) must not duplicate seeds.
    scdb.close_db(project_dir)
    conn2 = scdb.get_db(project_dir)
    cnt = conn2.execute(
        "SELECT COUNT(*) FROM undo_state WHERE key IN ('current_group','active')"
    ).fetchone()[0]
    assert cnt == 2, f"idempotent-on-reopen: expected 2 seed rows, got {cnt}"


def test_triggers_exist_for_all_tracked_tables(project_dir: Path, db_conn):
    """covers R3 — insert/update/delete triggers for each row-id tracked table."""
    trigs = {r[0] for r in db_conn.execute(
        "SELECT name FROM sqlite_master WHERE type='trigger'"
    ).fetchall()}
    for table in ROW_ID_TRACKED_TABLES:
        for kind in ("insert", "update", "delete"):
            name = f"{table}_{kind}_undo"
            assert name in trigs, f"trigger-{kind}: expected {name!r} for {table}"


def test_triggers_exist_for_composite_pk_tables(project_dir: Path, db_conn):
    """covers R8 — composite-PK triggers for isolation_stems + track_sends."""
    trigs = {r[0] for r in db_conn.execute(
        "SELECT name FROM sqlite_master WHERE type='trigger'"
    ).fetchall()}
    for table in COMPOSITE_PK_TRACKED_TABLES:
        for kind in ("insert", "update", "delete"):
            assert f"{table}_{kind}_undo" in trigs, f"composite-trigger: {table}_{kind}_undo"


def test_insert_capture_emits_delete(project_dir: Path, db_conn):
    """covers R3, R5, R11 — INSERT trigger emits DELETE inverse."""
    g = scdb.undo_begin(project_dir, "add keyframe")
    before = undo_count_log(db_conn, g)
    undo_seed_keyframe(project_dir, "k1", timestamp="0:01")
    after = undo_count_log(db_conn, g)
    assert after == before + 1, f"one-undo-row: got {after - before}"
    row = db_conn.execute(
        "SELECT sql_text FROM undo_log WHERE undo_group=? ORDER BY seq DESC LIMIT 1",
        (g,),
    ).fetchone()
    assert row["sql_text"] == "DELETE FROM keyframes WHERE id='k1'", (
        f"inverse-sql-is-delete: got {row['sql_text']!r}"
    )


def test_update_capture_emits_old_values(project_dir: Path, db_conn):
    """covers R6, R11 — UPDATE trigger emits inverse UPDATE carrying OLD values."""
    # Seed outside undo group so the INSERT isn't captured.
    undo_seed_keyframe(project_dir, "k1", timestamp="0:01", prompt="old_prompt")
    g = scdb.undo_begin(project_dir, "edit k1")
    db_conn.execute("UPDATE keyframes SET prompt='new_prompt' WHERE id='k1'")
    db_conn.commit()
    row = db_conn.execute(
        "SELECT sql_text FROM undo_log WHERE undo_group=? ORDER BY seq DESC LIMIT 1",
        (g,),
    ).fetchone()
    sql = row["sql_text"]
    assert sql.startswith("UPDATE keyframes SET "), f"inverse-sql-is-update: {sql!r}"
    assert "'old_prompt'" in sql, f"carries-OLD-value: {sql!r}"
    assert sql.endswith("WHERE id='k1'"), f"where-clause: {sql!r}"


def test_delete_capture_emits_insert(project_dir: Path, db_conn):
    """covers R7, R11 — DELETE trigger emits inverse INSERT with all OLD values."""
    undo_seed_keyframe(project_dir, "k1", timestamp="0:01")
    g = scdb.undo_begin(project_dir, "delete k1")
    db_conn.execute("DELETE FROM keyframes WHERE id='k1'")
    db_conn.commit()
    row = db_conn.execute(
        "SELECT sql_text FROM undo_log WHERE undo_group=? ORDER BY seq DESC LIMIT 1",
        (g,),
    ).fetchone()
    sql = row["sql_text"]
    assert sql.startswith("INSERT INTO keyframes ("), f"inverse-sql-is-insert: {sql!r}"
    assert "'k1'" in sql, f"includes-id: {sql!r}"


def test_capture_gated_off_writes_nothing(project_dir: Path, db_conn):
    """covers R4 — mutations while active=0 do not append to undo_log."""
    g = scdb.undo_begin(project_dir, "op")
    db_conn.execute("UPDATE undo_state SET value=0 WHERE key='active'")
    db_conn.commit()
    before = undo_count_log(db_conn, g)
    undo_seed_keyframe(project_dir, "k1", timestamp="0:01")
    after = undo_count_log(db_conn, g)
    assert after == before, f"no-undo-row: got delta {after - before}"
    # Restore for cleanup.
    db_conn.execute("UPDATE undo_state SET value=1 WHERE key='active'")
    db_conn.commit()


def test_non_tracked_table_not_captured(project_dir: Path, db_conn):
    """covers R16 — mutations on audio_bounces (not tracked) do not write undo_log."""
    g = scdb.undo_begin(project_dir, "op")
    before = undo_count_log(db_conn, g)
    db_conn.execute(
        "INSERT INTO audio_bounces (id, composite_hash, start_time_s, end_time_s, "
        "mode, selection_json, sample_rate, bit_depth, created_at) "
        "VALUES ('b1','hash1',0,1,'mix','{}',48000,24,'2026-01-01T00:00:00Z')"
    )
    db_conn.commit()
    after = undo_count_log(db_conn, g)
    assert after == before, (
        f"no-undo-row: audio_bounces is not tracked, got delta {after - before}"
    )


def test_undo_begin_allocates_group(project_dir: Path, db_conn):
    """covers R10 — allocates monotonically increasing ids."""
    g1 = scdb.undo_begin(project_dir, "op A")
    g2 = scdb.undo_begin(project_dir, "op B")
    assert g1 == 1, f"first-id-returned: got {g1}"
    assert g2 == g1 + 1, f"second-id-monotonic: got {g2}"
    row = db_conn.execute(
        "SELECT id, description, undone, timestamp FROM undo_groups WHERE id=?",
        (g1,),
    ).fetchone()
    assert row is not None, "row-written"
    assert row["description"] == "op A", "description"
    assert row["undone"] == 0, "undone=0"
    assert row["timestamp"] and "T" in row["timestamp"], "iso-timestamp"


def test_undo_begin_clears_redo_stack(project_dir: Path, db_conn):
    """covers R10, R14 — undone groups and their logs deleted on new undo_begin."""
    g1 = scdb.undo_begin(project_dir, "op A")
    undo_seed_keyframe(project_dir, "k1", timestamp="0:01")
    scdb.undo_execute(project_dir)  # marks g1 undone; populates redo_log
    assert db_conn.execute(
        "SELECT undone FROM undo_groups WHERE id=?", (g1,)
    ).fetchone()["undone"] == 1, "precondition: g1 undone"

    g2 = scdb.undo_begin(project_dir, "op B")
    assert db_conn.execute(
        "SELECT 1 FROM undo_groups WHERE id=?", (g1,)
    ).fetchone() is None, "undone-group-deleted"
    assert undo_count_log(db_conn, g1) == 0, "undone-undo-log-deleted"
    assert db_conn.execute(
        "SELECT COUNT(*) FROM redo_log WHERE undo_group=?", (g1,)
    ).fetchone()[0] == 0, "undone-redo-log-deleted"
    assert db_conn.execute(
        "SELECT 1 FROM undo_groups WHERE id=?", (g2,)
    ).fetchone() is not None, "new-group-inserted"


def test_undo_begin_no_branch_point_retained(project_dir: Path, db_conn):
    """covers R14 — all undone groups wiped on undo_begin (linear undo/redo)."""
    g1 = scdb.undo_begin(project_dir, "A")
    undo_seed_keyframe(project_dir, "k1")
    scdb.undo_execute(project_dir)
    # Re-create via begin (g1 already undone — but we need a fresh one undone too)
    g2 = scdb.undo_begin(project_dir, "B")
    undo_seed_keyframe(project_dir, "k2")
    scdb.undo_execute(project_dir)
    # Now at least one undone group exists; verify begin nukes it.
    live_undone = db_conn.execute(
        "SELECT COUNT(*) FROM undo_groups WHERE undone=1"
    ).fetchone()[0]
    assert live_undone >= 1, "precondition: ≥1 undone group exists"
    scdb.undo_begin(project_dir, "C")
    assert db_conn.execute(
        "SELECT COUNT(*) FROM undo_groups WHERE undone=1"
    ).fetchone()[0] == 0, "both-undone-groups-gone"


def test_undo_replays_inverse_desc(project_dir: Path, db_conn):
    """covers R12 — inverse SQL replayed in DESC seq; group marked undone."""
    g = scdb.undo_begin(project_dir, "op")
    undo_seed_keyframe(project_dir, "k1", timestamp="0:01", prompt="v1")
    db_conn.execute("UPDATE keyframes SET prompt='v2' WHERE id='k1'")
    db_conn.commit()
    undo_seed_keyframe(project_dir, "k2", timestamp="0:02")
    # Precondition: 3 rows in undo_log.
    assert undo_count_log(db_conn, g) == 3, "precondition: 3 captured"

    result = scdb.undo_execute(project_dir)
    assert result is not None, "return-not-none"
    assert result["id"] == g, "return-id-matches"
    assert "description" in result and "timestamp" in result, "return-shape"

    kfs = db_conn.execute("SELECT id FROM keyframes").fetchall()
    ids = {r["id"] for r in kfs}
    assert "k1" not in ids and "k2" not in ids, f"final-state: keyframes gone, got {ids}"

    undone = db_conn.execute(
        "SELECT undone FROM undo_groups WHERE id=?", (g,)
    ).fetchone()["undone"]
    assert undone == 1, "group-marked-undone"


def test_undo_captures_redo_log(project_dir: Path, db_conn):
    """covers R12 — trigger-captured forward SQL lands in redo_log, negative scratch cleared."""
    g = scdb.undo_begin(project_dir, "op")
    undo_seed_keyframe(project_dir, "k1", timestamp="0:01")
    undo_seed_keyframe(project_dir, "k2", timestamp="0:02")
    scdb.undo_execute(project_dir)

    redo_rows = db_conn.execute(
        "SELECT COUNT(*) FROM redo_log WHERE undo_group=?", (g,)
    ).fetchone()[0]
    assert redo_rows == 2, f"redo-log-has-two-rows: got {redo_rows}"
    scratch = db_conn.execute(
        "SELECT COUNT(*) FROM undo_log WHERE undo_group=?", (-g,)
    ).fetchone()[0]
    assert scratch == 0, f"negative-scratch-empty: got {scratch}"
    cg = db_conn.execute(
        "SELECT value FROM undo_state WHERE key='current_group'"
    ).fetchone()[0]
    assert cg == g, f"current-group-restored: got {cg}"


def test_redo_replays_forward_asc(project_dir: Path, db_conn):
    """covers R13 — redo re-applies forward SQL in ASC seq; group flag cleared."""
    g = scdb.undo_begin(project_dir, "op")
    undo_seed_keyframe(project_dir, "k1", timestamp="0:01")
    undo_seed_keyframe(project_dir, "k2", timestamp="0:02")
    scdb.undo_execute(project_dir)
    assert db_conn.execute("SELECT COUNT(*) FROM keyframes").fetchone()[0] == 0, (
        "precondition: keyframes gone"
    )

    result = scdb.redo_execute(project_dir)
    assert result is not None, "return-not-none"

    ids = {r["id"] for r in db_conn.execute("SELECT id FROM keyframes").fetchall()}
    assert "k1" in ids and "k2" in ids, f"final-state: both back, got {ids}"
    undone = db_conn.execute(
        "SELECT undone FROM undo_groups WHERE id=?", (g,)
    ).fetchone()["undone"]
    assert undone == 0, "group-flag"
    remaining = db_conn.execute(
        "SELECT COUNT(*) FROM redo_log WHERE undo_group=?", (g,)
    ).fetchone()[0]
    assert remaining == 0, "redo-log-cleaned"


def test_redo_disables_capture_during_replay(project_dir: Path, db_conn):
    """covers R13 — active=0 during redo; no new undo_log rows as a side effect."""
    g = scdb.undo_begin(project_dir, "op")
    undo_seed_keyframe(project_dir, "k1", timestamp="0:01")
    scdb.undo_execute(project_dir)

    before = undo_count_log(db_conn)
    scdb.redo_execute(project_dir)
    after = undo_count_log(db_conn)
    assert after == before, f"no-new-undo-rows: delta {after - before}"

    # After replay, active restored to 1.
    active = db_conn.execute(
        "SELECT value FROM undo_state WHERE key='active'"
    ).fetchone()[0]
    assert active == 1, f"active-restored: got {active}"


def test_redo_cleans_redo_log(project_dir: Path, db_conn):
    """covers R13 — consumed redo_log rows deleted."""
    g = scdb.undo_begin(project_dir, "op")
    undo_seed_keyframe(project_dir, "k1", timestamp="0:01")
    scdb.undo_execute(project_dir)
    scdb.redo_execute(project_dir)
    cnt = db_conn.execute(
        "SELECT COUNT(*) FROM redo_log WHERE undo_group=?", (g,)
    ).fetchone()[0]
    assert cnt == 0, f"empty-for-group: got {cnt}"


def test_undo_empty_returns_none(project_dir: Path, db_conn):
    """covers R12 fallback — no groups to undo returns None, no state change.

    Note: get_db seeds default tracks/buses which leaves undo_log rows under
    undo_group=0 (see R17 target: those mutations should not be captured).
    We assert invariance across the undo_execute call, not absolute emptiness.
    """
    state_before = dict(db_conn.execute(
        "SELECT key, value FROM undo_state"
    ).fetchall())
    log_before = undo_count_log(db_conn)
    groups_before = undo_count_groups(db_conn)
    result = scdb.undo_execute(project_dir)
    assert result is None, f"returns-none: got {result!r}"
    state_after = dict(db_conn.execute(
        "SELECT key, value FROM undo_state"
    ).fetchall())
    assert state_before == state_after, "no-state-change"
    assert undo_count_log(db_conn) == log_before, "undo-log-untouched"
    assert undo_count_groups(db_conn) == groups_before, "undo-groups-untouched"


def test_redo_empty_returns_none(project_dir: Path, db_conn):
    """covers R13 fallback — no undone groups returns None."""
    result = scdb.redo_execute(project_dir)
    assert result is None, "returns-none"


def test_undo_history_shape_and_order(project_dir: Path, db_conn):
    """covers R15 — newest first, shape {id, description, timestamp, undone:bool}, limit respected."""
    g1 = scdb.undo_begin(project_dir, "first")
    undo_seed_keyframe(project_dir, "k1")
    g2 = scdb.undo_begin(project_dir, "second")
    undo_seed_keyframe(project_dir, "k2")
    g3 = scdb.undo_begin(project_dir, "third")
    undo_seed_keyframe(project_dir, "k3")
    # Mark g2 undone via direct flag flip (undo_execute of g3 would mark g3).
    db_conn.execute("UPDATE undo_groups SET undone=1 WHERE id=?", (g2,))
    db_conn.commit()

    hist = scdb.undo_history(project_dir, limit=10)
    assert [h["id"] for h in hist] == sorted([g1, g2, g3], reverse=True), "newest-first"
    for h in hist:
        assert set(h.keys()) == {"id", "description", "timestamp", "undone"}, f"row-shape: {h.keys()}"
        assert isinstance(h["undone"], bool), f"undone-is-bool: {type(h['undone'])}"
    by_id = {h["id"]: h for h in hist}
    assert by_id[g2]["undone"] is True, "g2-undone-true"
    assert by_id[g1]["undone"] is False, "g1-undone-false"
    assert by_id[g3]["undone"] is False, "g3-undone-false"

    one = scdb.undo_history(project_dir, limit=1)
    assert len(one) == 1, f"limit-respected: got {len(one)}"


# ---------------------------------------------------------------------------
# Edge Cases
# ---------------------------------------------------------------------------


def test_composite_pk_insert_capture(project_dir: Path, db_conn):
    """covers R8 — isolation_stems insert records composite-key DELETE inverse."""
    # Seed parent isolation + pool_segment outside undo group (FKs).
    db_conn.execute(
        "INSERT INTO audio_isolations (id, entity_type, entity_id, model, range_mode, "
        "status, created_at) VALUES ('iso1','audio_clip','c1','demucs','full','pending','2026-01-01T00:00:00Z')"
    )
    seg = scdb.add_pool_segment(project_dir, kind="generated",
                                 created_by="test", pool_path="pool/x.wav")
    db_conn.commit()
    g = scdb.undo_begin(project_dir, "op")
    db_conn.execute(
        "INSERT INTO isolation_stems (isolation_id, pool_segment_id, stem_type) "
        "VALUES ('iso1', ?, 'vocals')", (seg,),
    )
    db_conn.commit()
    row = db_conn.execute(
        "SELECT sql_text FROM undo_log WHERE undo_group=? ORDER BY seq DESC LIMIT 1",
        (g,),
    ).fetchone()
    sql = row["sql_text"]
    assert "DELETE FROM isolation_stems" in sql, f"delete-form: {sql!r}"
    assert "isolation_id='iso1'" in sql, f"composite-key-isolation-id: {sql!r}"
    assert f"pool_segment_id='{seg}'" in sql, f"composite-key-pool-segment-id: {sql!r}"


def test_track_sends_composite_capture(project_dir: Path, db_conn):
    """covers R8 — track_sends insert records composite-key DELETE inverse."""
    # Seed audio_track + project_send_buses row outside undo group.
    db_conn.execute(
        "INSERT INTO audio_tracks (id, name, display_order) VALUES ('at1','at1',0)"
    )
    db_conn.execute(
        "INSERT INTO project_send_buses (id, bus_type, label, order_index, static_params) "
        "VALUES ('bus1','aux','Bus 1',0,'{}')"
    )
    db_conn.commit()

    g = scdb.undo_begin(project_dir, "op")
    db_conn.execute(
        "INSERT INTO track_sends (track_id, bus_id, level) VALUES ('at1','bus1',0.5)"
    )
    db_conn.commit()
    row = db_conn.execute(
        "SELECT sql_text FROM undo_log WHERE undo_group=? ORDER BY seq DESC LIMIT 1",
        (g,),
    ).fetchone()
    sql = row["sql_text"]
    assert "DELETE FROM track_sends" in sql, f"delete-form: {sql!r}"
    assert "track_id='at1'" in sql, f"composite-track-id: {sql!r}"
    assert "bus_id='bus1'" in sql, f"composite-bus-id: {sql!r}"


def test_deferred_fk_declared_on_isolation_stems(project_dir: Path, db_conn):
    """covers R9 — isolation_stems.isolation_id FK is DEFERRABLE INITIALLY DEFERRED.

    PRAGMA foreign_key_list exposes each FK; the deferrability flag is in
    sqlite_master's CREATE TABLE text.
    """
    sql = db_conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='isolation_stems'"
    ).fetchone()[0]
    assert "DEFERRABLE INITIALLY DEFERRED" in sql.upper(), (
        f"deferred-fk-declared: got {sql!r}"
    )


def test_deferred_fk_allows_replay_ordering(project_dir: Path, db_conn):
    """covers R9 — inserting child-before-parent succeeds inside a transaction."""
    seg = scdb.add_pool_segment(project_dir, kind="generated", created_by="test",
                                 pool_path="pool/x.wav")
    # Ensure FKs are on for this connection (mirror prod).
    db_conn.execute("PRAGMA foreign_keys = ON")
    try:
        db_conn.execute("BEGIN")
        # Child first: references a nonexistent isolation yet.
        db_conn.execute(
            "INSERT INTO isolation_stems (isolation_id, pool_segment_id, stem_type) "
            "VALUES ('iso_late', ?, 'vocals')", (seg,),
        )
        # Parent after: satisfies the deferred FK.
        db_conn.execute(
            "INSERT INTO audio_isolations (id, entity_type, entity_id, model, "
            "range_mode, status, created_at) "
            "VALUES ('iso_late','audio_clip','c1','demucs','full','pending',"
            "'2026-01-01T00:00:00Z')"
        )
        db_conn.execute("COMMIT")
    except sqlite3.IntegrityError as e:
        pytest.fail(f"no-fk-violation: deferred FK did not defer: {e}")
    # rows-restored
    assert db_conn.execute(
        "SELECT 1 FROM audio_isolations WHERE id='iso_late'"
    ).fetchone() is not None, "parent-present"
    assert db_conn.execute(
        "SELECT 1 FROM isolation_stems WHERE isolation_id='iso_late'"
    ).fetchone() is not None, "child-present"


def test_history_pruned_to_1000(project_dir: Path, db_conn):
    """covers R10 — undo_groups trimmed to 1000 on undo_begin; orphan undo_log rows deleted."""
    # Bulk-seed 1005 groups to simulate overflow.
    from datetime import datetime, timezone
    ts = datetime.now(timezone.utc).isoformat()
    db_conn.executemany(
        "INSERT INTO undo_groups (id, description, timestamp, undone) VALUES (?,?,?,0)",
        [(i, f"bulk_{i}", ts) for i in range(1, 1006)],
    )
    # Orphan undo_log rows under id=1 (will be pruned).
    db_conn.execute(
        "INSERT INTO undo_log (undo_group, sql_text) VALUES (1, 'SELECT 1')"
    )
    db_conn.execute(
        "UPDATE undo_state SET value=1005 WHERE key='current_group'"
    )
    db_conn.commit()

    scdb.undo_begin(project_dir, "new")
    total_groups = undo_count_groups(db_conn)
    assert total_groups <= 1001, f"groups-trimmed: got {total_groups}"
    # Orphan undo_log for the pruned group id=1 (which is outside the top 1000)
    # must be deleted by the per-R10 orphan sweep.
    orphans_for_pruned = db_conn.execute(
        "SELECT COUNT(*) FROM undo_log WHERE undo_group=1"
    ).fetchone()[0]
    assert orphans_for_pruned == 0, (
        f"orphan-undo-log-gone: group id=1 was pruned but its log survived ({orphans_for_pruned})"
    )


def test_undo_begin_bumps_past_collision(project_dir: Path, db_conn):
    """covers R10 — stale current_group counter bumps past MAX(undo_groups.id)."""
    from datetime import datetime, timezone
    ts = datetime.now(timezone.utc).isoformat()
    # Seed a fake group id=9 and set counter to 5 to simulate stale state.
    db_conn.execute(
        "INSERT INTO undo_groups (id, description, timestamp) VALUES (9,'stale',?)",
        (ts,),
    )
    db_conn.execute("UPDATE undo_state SET value=5 WHERE key='current_group'")
    db_conn.commit()

    new_id = scdb.undo_begin(project_dir, "x")
    assert new_id == 10, f"returned-id-greater: got {new_id}"
    cg = db_conn.execute(
        "SELECT value FROM undo_state WHERE key='current_group'"
    ).fetchone()[0]
    assert cg == 10, f"state-updated: got {cg}"


def test_single_writer_assumption_no_named_lock(project_dir: Path, db_conn):
    """covers R23, OQ-7 (INV-1 negative-assertion) — no named lock in undo APIs.

    Scans scdb module for module-level `threading.Lock`/`asyncio.Lock` that
    wraps undo calls. `_conn_lock` protects the connection pool only, not
    the undo API path.
    """
    import inspect
    src_begin = inspect.getsource(scdb.undo_begin)
    src_exec = inspect.getsource(scdb.undo_execute)
    src_redo = inspect.getsource(scdb.redo_execute)
    for src in (src_begin, src_exec, src_redo):
        assert "threading.Lock" not in src, f"no-internal-lock: {src[:120]}"
        assert "asyncio.Lock" not in src, f"no-asyncio-lock: {src[:120]}"
        assert ".acquire(" not in src, f"no-named-acquire: {src[:120]}"


def test_current_group_zero_skips_capture(project_dir: Path, db_conn):
    """covers R17, resolves OQ-2 — mutation with current_group=0 currently writes
    a row with undo_group=0 (today's behavior).

    TRANSITIONAL: Today the trigger does NOT gate on current_group != 0 — it
    writes to undo_group=0. Spec R17 says this should skip capture entirely.
    Until the trigger predicate is updated, this test codifies today's
    behavior as a TARGET xfail.
    """
    # Precondition: current_group=0, active=1, no undo_begin called.
    cg = db_conn.execute(
        "SELECT value FROM undo_state WHERE key='current_group'"
    ).fetchone()[0]
    assert cg == 0, "precondition: current_group=0"

    undo_seed_keyframe(project_dir, "k1", timestamp="0:01")
    # row-inserted (mutation proceeds regardless).
    assert db_conn.execute(
        "SELECT 1 FROM keyframes WHERE id='k1'"
    ).fetchone() is not None, "row-inserted"

    # no-undo-row (target). Today this xfails because the trigger writes
    # with undo_group=0 anyway.
    rows_for_zero = db_conn.execute(
        "SELECT COUNT(*) FROM undo_log WHERE undo_group=0"
    ).fetchone()[0]
    if rows_for_zero > 0:
        pytest.xfail("target-state; awaits R17 trigger predicate `current_group != 0`")
    assert rows_for_zero == 0, f"no-undo-row: got {rows_for_zero}"


@pytest.mark.xfail(
    reason="target-state; awaits R19 trigger-level redo-stack discard on current_group=0 mutation",
    strict=False,
)
def test_redo_discarded_on_new_mutation(project_dir: Path, db_conn):
    """covers R19, resolves OQ-3 — new tracked mutation outside a group discards
    the redo stack before proceeding. TARGET; current code only discards in undo_begin."""
    g1 = scdb.undo_begin(project_dir, "op A")
    undo_seed_keyframe(project_dir, "k1", timestamp="0:01")
    scdb.undo_execute(project_dir)
    # Sanity: redo_log populated, g1 undone.
    assert db_conn.execute(
        "SELECT COUNT(*) FROM redo_log WHERE undo_group=?", (g1,)
    ).fetchone()[0] >= 1, "precondition: redo-log-populated"

    # Issue new mutation WITHOUT undo_begin — current_group is still g1 after
    # undo_execute restored it, so technically not zero. Force zero to match spec.
    db_conn.execute("UPDATE undo_state SET value=0 WHERE key='current_group'")
    db_conn.commit()
    undo_seed_keyframe(project_dir, "k2", timestamp="0:02")

    # Target: redo stack wiped.
    redo_left = db_conn.execute("SELECT COUNT(*) FROM redo_log").fetchone()[0]
    assert redo_left == 0, f"redo-stack-cleared: got {redo_left}"
    undone_left = db_conn.execute(
        "SELECT COUNT(*) FROM undo_groups WHERE undone=1"
    ).fetchone()[0]
    assert undone_left == 0, f"undone-groups-gone: got {undone_left}"
    assert db_conn.execute(
        "SELECT 1 FROM keyframes WHERE id='k2'"
    ).fetchone() is not None, "mutation-applied"


@pytest.mark.xfail(
    reason="target-state; awaits R20 per-group cap enforcement (10,000 rows)",
    strict=False,
)
def test_undo_log_capped_per_group(project_dir: Path, db_conn):
    """covers R20, resolves OQ-4 — per-group undo_log cap at 10,000; oldest dropped.

    TARGET; current code has no per-group cap.
    """
    g = scdb.undo_begin(project_dir, "bulk")
    # Fake 10,000 rows for g via direct insert to skip real mutation cost.
    db_conn.executemany(
        "INSERT INTO undo_log (undo_group, sql_text) VALUES (?, ?)",
        [(g, f"SELECT {i}") for i in range(10_000)],
    )
    db_conn.commit()
    first_seq = db_conn.execute(
        "SELECT MIN(seq) FROM undo_log WHERE undo_group=?", (g,)
    ).fetchone()[0]

    # One real tracked mutation to trigger the cap.
    undo_seed_keyframe(project_dir, "k_overflow", timestamp="0:01")

    count = undo_count_log(db_conn, g)
    assert count == 10_000, f"cap-enforced: got {count}"
    # oldest-dropped
    assert db_conn.execute(
        "SELECT 1 FROM undo_log WHERE undo_group=? AND seq=?", (g, first_seq)
    ).fetchone() is None, "oldest-dropped"
    # newest-present
    assert db_conn.execute(
        "SELECT 1 FROM undo_log WHERE undo_group=? AND sql_text LIKE '%keyframes%'",
        (g,),
    ).fetchone() is not None, "newest-present"


@pytest.mark.xfail(
    reason="target-state; awaits R21 schema_version column + schema_migrations table",
    strict=False,
)
def test_undo_replay_schema_mismatch_fails(project_dir: Path, db_conn):
    """covers R21, resolves OQ-5 — replay across schema migration raises
    UndoReplaySchemaVersionMismatch. TARGET; depends on migrations-framework."""
    # Target API: db.UndoReplaySchemaVersionMismatch must exist.
    exc_cls = getattr(scdb, "UndoReplaySchemaVersionMismatch", None)
    assert exc_cls is not None, "UndoReplaySchemaVersionMismatch-defined"

    # Seed a group with schema_version=3 in undo_log; DB at schema_version=4.
    g = scdb.undo_begin(project_dir, "old op")
    db_conn.execute(
        "INSERT INTO undo_log (undo_group, sql_text, schema_version) VALUES (?, ?, 3)",
        (g, "SELECT 1"),
    )
    db_conn.execute(
        "INSERT OR REPLACE INTO schema_migrations (version) VALUES (4)"
    )
    db_conn.commit()

    with pytest.raises(exc_cls):
        scdb.undo_execute(project_dir)

    assert db_conn.execute(
        "SELECT undone FROM undo_groups WHERE id=?", (g,)
    ).fetchone()["undone"] == 0, "group-unchanged"


@pytest.mark.xfail(
    reason="target-state; awaits R22 completed_at column + startup sweep",
    strict=False,
)
def test_startup_sweep_closes_orphan_groups(project_dir: Path, db_conn):
    """covers R22, resolves OQ-6 — startup sweep closes >1h-old completed_at=NULL groups."""
    from datetime import datetime, timezone, timedelta
    # Target: undo_groups has completed_at column.
    cols = {r[1] for r in db_conn.execute(
        "PRAGMA table_info(undo_groups)"
    ).fetchall()}
    assert "completed_at" in cols, "completed_at-column-exists"

    old_ts = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    recent_ts = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
    db_conn.execute(
        "INSERT INTO undo_groups (id, description, timestamp, undone, completed_at) "
        "VALUES (100,'old',?,0,NULL)", (old_ts,),
    )
    db_conn.execute(
        "INSERT INTO undo_groups (id, description, timestamp, undone, completed_at) "
        "VALUES (101,'recent',?,0,NULL)", (recent_ts,),
    )
    db_conn.commit()

    # Target: an explicit startup sweep function.
    sweep = getattr(scdb, "undo_startup_sweep", None)
    assert sweep is not None, "undo_startup_sweep-defined"
    sweep(project_dir)

    old_ca = db_conn.execute(
        "SELECT completed_at FROM undo_groups WHERE id=100"
    ).fetchone()["completed_at"]
    assert old_ca is not None and "T" in old_ca, f"old-closed: {old_ca!r}"
    recent_ca = db_conn.execute(
        "SELECT completed_at FROM undo_groups WHERE id=101"
    ).fetchone()["completed_at"]
    assert recent_ca is None, f"recent-kept-open: {recent_ca!r}"


@pytest.mark.xfail(
    reason="target-state; awaits R24 SAVEPOINT-wrapped replay + replay_failed column",
    strict=False,
)
def test_undo_replay_failure_rolls_back(project_dir: Path, db_conn):
    """covers R24, resolves OQ-8 — SAVEPOINT rolled back on replay failure;
    replay_failed=1 set; group excluded from future undo. TARGET."""
    cols = {r[1] for r in db_conn.execute(
        "PRAGMA table_info(undo_groups)"
    ).fetchall()}
    assert "replay_failed" in cols, "replay_failed-column-exists"

    # Seed a group whose inverse SQL will fail (references a table that doesn't exist).
    g = scdb.undo_begin(project_dir, "broken")
    db_conn.execute(
        "INSERT INTO undo_log (undo_group, sql_text) VALUES (?, 'UPDATE __missing__ SET x=1')",
        (g,),
    )
    db_conn.commit()

    with pytest.raises(sqlite3.Error):
        scdb.undo_execute(project_dir)

    # savepoint-rolled-back: group still undone=0.
    assert db_conn.execute(
        "SELECT undone FROM undo_groups WHERE id=?", (g,)
    ).fetchone()["undone"] == 0, "savepoint-rolled-back"
    assert db_conn.execute(
        "SELECT replay_failed FROM undo_groups WHERE id=?", (g,)
    ).fetchone()["replay_failed"] == 1, "group-flagged"
    # excluded-from-future
    next_call = scdb.undo_execute(project_dir)
    assert next_call is None, f"excluded-from-future: got {next_call!r}"


@pytest.mark.xfail(
    reason="target-state; awaits R18 begin_undo_group/end_undo_group/is_undo_capturing rename + aliases",
    strict=False,
)
def test_target_api_names_exposed():
    """covers R18, resolves OQ-1 — target public API names exist in scenecraft.db."""
    assert callable(getattr(scdb, "begin_undo_group", None)), "begin_undo_group-callable"
    assert callable(getattr(scdb, "end_undo_group", None)), "end_undo_group-callable"
    assert callable(getattr(scdb, "is_undo_capturing", None)), "is_undo_capturing-callable"
    # Legacy aliases preserved.
    assert callable(getattr(scdb, "undo_begin", None)), "undo_begin-alias-preserved"


def test_is_undo_capturing_semantics_transitional(project_dir: Path, db_conn):
    """covers R18 — is_undo_capturing returns True iff active=1 AND current_group!=0.

    TRANSITIONAL: if the target function doesn't exist yet, the test xfails.
    """
    fn = getattr(scdb, "is_undo_capturing", None)
    if fn is None:
        pytest.xfail("target-state; awaits R18 is_undo_capturing()")
    # Fresh DB: current_group=0 → False.
    assert fn(project_dir) is False, "false-when-current-group-zero"
    scdb.undo_begin(project_dir, "op")
    assert fn(project_dir) is True, "true-after-undo-begin"
    db_conn.execute("UPDATE undo_state SET value=0 WHERE key='active'")
    db_conn.commit()
    assert fn(project_dir) is False, "false-when-active-zero"


# ---------------------------------------------------------------------------
# Task-93 — get_db bootstrap sweeps undo_log group=0 orphans
# ---------------------------------------------------------------------------


def test_get_db_bootstrap_sweeps_undo_group_zero(tmp_path: Path):
    """covers task-93 — `get_db` bootstrap purges `undo_log` rows under
    `undo_group=0` (seed-insert orphans) so they don't accumulate forever
    across engine reboots.

    Witness path:
      1. First `get_db(project_dir)` runs `_ensure_schema`, which seeds the
         default audio_track + 4 send buses. The per-table undo triggers
         fire under `current_group=0` and write rows to `undo_log` with
         `undo_group=0`. After bootstrap our sweep removes them, so the
         post-`get_db` count must be 0.
      2. Simulating a reboot (`close_db` + clear the process-level
         `_migrated_dbs` cache) and calling `get_db` again must keep the
         group-0 count at 0 (idempotent + sweeps any prior session's junk
         that landed before this fix shipped).
      3. A normal `undo_begin` + tracked mutation captures rows under a
         group_id > 0; the bootstrap sweep must not touch them.
    """
    project = tmp_path / "task93"
    project.mkdir()

    # --- (1) First bootstrap: schema + seeds run, sweep should clear group-0.
    conn = scdb.get_db(project)
    group_zero_after_first = conn.execute(
        "SELECT COUNT(*) FROM undo_log WHERE undo_group = 0"
    ).fetchone()[0]
    assert group_zero_after_first == 0, (
        f"first-bootstrap-sweeps-group-zero: got {group_zero_after_first}"
    )

    # --- Inject prior-session junk to verify the sweep handles pre-existing
    # group-0 rows on the next bootstrap (i.e., a DB created by older code
    # before this fix shipped).
    conn.execute(
        "INSERT INTO undo_log (undo_group, sql_text) VALUES (0, 'DELETE FROM keyframes WHERE id=42')"
    )
    conn.execute(
        "INSERT INTO undo_log (undo_group, sql_text) VALUES (0, 'DELETE FROM keyframes WHERE id=43')"
    )
    conn.commit()
    assert conn.execute(
        "SELECT COUNT(*) FROM undo_log WHERE undo_group = 0"
    ).fetchone()[0] == 2, "junk-injected"

    # --- (2) Simulate reboot: close conn, drop migration-cache marker.
    scdb.close_db(project)
    scdb._migrated_dbs.discard(str(project / "project.db"))

    conn2 = scdb.get_db(project)
    group_zero_after_reboot = conn2.execute(
        "SELECT COUNT(*) FROM undo_log WHERE undo_group = 0"
    ).fetchone()[0]
    assert group_zero_after_reboot == 0, (
        f"reboot-sweeps-prior-junk: got {group_zero_after_reboot}"
    )

    # --- (3) Normal undo_begin + tracked mutation: rows captured under
    # group_id > 0 must survive. The bootstrap sweep ran already; no new
    # bootstrap is triggered by undo_begin.
    g = scdb.undo_begin(project, "task93 op")
    assert g > 0, f"group-id-positive: got {g}"
    undo_seed_keyframe(project, "k_task93", timestamp="0:01")

    captured = conn2.execute(
        "SELECT COUNT(*) FROM undo_log WHERE undo_group = ?", (g,)
    ).fetchone()[0]
    assert captured >= 1, f"group-N-rows-captured: got {captured}"

    # And group-0 still empty — capture under group g doesn't leak to group 0.
    assert conn2.execute(
        "SELECT COUNT(*) FROM undo_log WHERE undo_group = 0"
    ).fetchone()[0] == 0, "group-zero-still-empty-after-tracked-mutation"

    scdb.close_db(project)
    scdb._migrated_dbs.discard(str(project / "project.db"))


# ---------------------------------------------------------------------------
# E2E — HTTP round-trip through live api_server
# ---------------------------------------------------------------------------


class TestEndToEnd:
    """Comprehensive e2e — undo/redo through the live HTTP surface."""

    def test_e2e_undo_roundtrip_add_keyframe(self, engine_server, project_name):
        """covers R10, R11, R12 (row 2, row 7, row 9) via HTTP add-keyframe → undo → GET."""
        status, body = engine_server.json(
            "POST", f"/api/projects/{project_name}/add-keyframe",
            {"timestamp": "0:05", "prompt": "orig"},
        )
        assert status == 200, f"add-keyframe: {status} {body!r}"

        status, kfs_before = engine_server.json(
            "GET", f"/api/projects/{project_name}/keyframes"
        )
        assert status == 200
        kf_list_before = kfs_before.get("keyframes", kfs_before) if isinstance(kfs_before, dict) else kfs_before
        count_before = len(kf_list_before) if isinstance(kf_list_before, list) else 0
        assert count_before >= 1, f"precondition: keyframe added, got {count_before}"

        status, undo_body = engine_server.json(
            "POST", f"/api/projects/{project_name}/undo"
        )
        assert status == 200, f"undo: {status} {undo_body!r}"

        status, kfs_after = engine_server.json(
            "GET", f"/api/projects/{project_name}/keyframes"
        )
        kf_list_after = kfs_after.get("keyframes", kfs_after) if isinstance(kfs_after, dict) else kfs_after
        count_after = len(kf_list_after) if isinstance(kf_list_after, list) else 0
        assert count_after < count_before, (
            f"undo-removed-keyframe: before={count_before}, after={count_after}"
        )

    def test_e2e_redo_restores_keyframe(self, engine_server, project_name):
        """covers R13 (row 10) — POST undo then POST redo restores state via HTTP."""
        engine_server.json(
            "POST", f"/api/projects/{project_name}/add-keyframe",
            {"timestamp": "0:07"},
        )
        engine_server.json("POST", f"/api/projects/{project_name}/undo")
        status, redo_body = engine_server.json(
            "POST", f"/api/projects/{project_name}/redo"
        )
        assert status == 200, f"redo: {status} {redo_body!r}"
        status, kfs = engine_server.json(
            "GET", f"/api/projects/{project_name}/keyframes"
        )
        kf_list = kfs.get("keyframes", kfs) if isinstance(kfs, dict) else kfs
        count = len(kf_list) if isinstance(kf_list, list) else 0
        assert count >= 1, f"redo-restored: got {count} keyframes"

    def test_e2e_undo_empty_stack(self, engine_server, project_name):
        """covers R12 fallback (row 11) — undo on fresh project returns success=False."""
        status, body = engine_server.json(
            "POST", f"/api/projects/{project_name}/undo"
        )
        # Current code returns 200 {success: False, message: 'Nothing to undo'}.
        assert status == 200, f"empty-undo-status: {status}"
        assert isinstance(body, dict), f"empty-undo-body-dict: {body!r}"
        assert body.get("success") is False, f"empty-undo-success-false: {body!r}"

    def test_e2e_redo_empty_stack(self, engine_server, project_name):
        """covers R13 fallback (row 12) — redo with no undone groups."""
        status, body = engine_server.json(
            "POST", f"/api/projects/{project_name}/redo"
        )
        assert status == 200, f"empty-redo-status: {status}"
        assert body.get("success") is False, f"empty-redo-success-false: {body!r}"

    def test_e2e_undo_history_endpoint_shape(self, engine_server, project_name):
        """covers R15 — GET /undo-history returns list of {id, description, timestamp, undone}."""
        # Create 2 groups via add-keyframe calls.
        engine_server.json(
            "POST", f"/api/projects/{project_name}/add-keyframe",
            {"timestamp": "0:01"},
        )
        engine_server.json(
            "POST", f"/api/projects/{project_name}/add-keyframe",
            {"timestamp": "0:02"},
        )
        status, body = engine_server.json(
            "GET", f"/api/projects/{project_name}/undo-history"
        )
        assert status == 200, f"history: {status} {body!r}"
        hist = body.get("history", []) if isinstance(body, dict) else []
        assert isinstance(hist, list), f"history-is-list: {hist!r}"
        if hist:
            sample = hist[0]
            assert set(sample.keys()) >= {"id", "description", "timestamp", "undone"}, (
                f"row-shape: {sample.keys()}"
            )
            # newest-first: first id >= last id.
            assert hist[0]["id"] >= hist[-1]["id"], "newest-first"

    def test_e2e_non_tracked_table_not_undone(self, engine_server, project_name):
        """covers R16 — mutations to audio_bounces (not tracked) persist across undo.

        We mutate audio_bounces directly via the engine's DB layer (there's no
        REST endpoint for bounces yet) and then call POST /undo. The row MUST
        remain because audio_bounces is not in the tracked list.
        """
        work_dir = engine_server.work_dir
        project_dir = work_dir / project_name
        assert project_dir.exists(), f"project-dir-exists: {project_dir}"

        # Write directly via scdb (this mimics what the bounce worker does).
        # This is a DB-side verification within e2e: the persistence layer is
        # shared between the test and the server since both point at the same
        # project.db file.
        conn = sqlite3.connect(project_dir / "project.db")
        conn.execute(
            "INSERT INTO audio_bounces (id, composite_hash, start_time_s, end_time_s, "
            "mode, selection_json, sample_rate, bit_depth, created_at) "
            "VALUES ('b_e2e','h_e2e',0,1,'mix','{}',48000,24,'2026-01-01T00:00:00Z')"
        )
        conn.commit()
        conn.close()

        # Also create a tracked mutation so there IS something to undo.
        engine_server.json(
            "POST", f"/api/projects/{project_name}/add-keyframe",
            {"timestamp": "0:09"},
        )
        status, _ = engine_server.json(
            "POST", f"/api/projects/{project_name}/undo"
        )
        assert status == 200

        conn = sqlite3.connect(project_dir / "project.db")
        row = conn.execute(
            "SELECT 1 FROM audio_bounces WHERE id='b_e2e'"
        ).fetchone()
        conn.close()
        assert row is not None, "non-tracked-row-survives-undo"

    @pytest.mark.xfail(
        reason="target-state; awaits WS undo_stack_changed broadcast (not yet implemented)",
        strict=False,
    )
    def test_e2e_ws_broadcast_on_undo(self, engine_server, project_name):
        """covers row 7 target — WS subscribers see an undo_stack_changed event."""
        # Placeholder: the current api_server HTTP fixture does NOT boot the WS
        # server; a full WS e2e needs a separate fixture. Marked xfail.
        pytest.fail("WS broadcast fixture not available in this test harness")

    @pytest.mark.xfail(
        reason="target-state; awaits REST endpoint /api/projects/:name/undo-state exposing group sequence",
        strict=False,
    )
    def test_e2e_undo_state_endpoint(self, engine_server, project_name):
        """covers R18 target — GET /undo-state returns is_undo_capturing + current_group."""
        status, body = engine_server.json(
            "GET", f"/api/projects/{project_name}/undo-state"
        )
        assert status == 200, f"undo-state-endpoint: {status}"
        assert isinstance(body, dict), f"undo-state-body-dict: {body!r}"
        assert "is_capturing" in body or "isCapturing" in body, f"is-capturing-key: {body!r}"
