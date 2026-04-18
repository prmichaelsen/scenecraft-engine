"""Tests for M7 schema migrations: trim_in, trim_out, source_video_duration on transitions."""

import shutil
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from scenecraft import db as db_mod
from scenecraft.db import (
    _ensure_schema,
    add_keyframe,
    add_transition,
    backfill_transition_trim,
    close_db,
    get_db,
    get_transition,
    update_transition,
    _migrated_dbs,
)


# ── Fixtures ────────────────────────────────────────────────────────


@pytest.fixture
def project_dir():
    work_dir = Path(tempfile.mkdtemp())
    pdir = work_dir / "proj"
    pdir.mkdir()
    yield pdir
    close_db(pdir)
    _migrated_dbs.discard(str(pdir / "project.db"))
    shutil.rmtree(work_dir)


def _tr_cols(conn):
    return {row[1] for row in conn.execute("PRAGMA table_info(transitions)").fetchall()}


def _seed_tr(project_dir, tr_id="tr_aaa0001", from_kf="kf_aaa0001", to_kf="kf_aaa0002"):
    add_keyframe(project_dir, {"id": from_kf, "timestamp": "0:00"})
    add_keyframe(project_dir, {"id": to_kf, "timestamp": "0:05"})
    add_transition(project_dir, {
        "id": tr_id,
        "from": from_kf,
        "to": to_kf,
        "duration_seconds": 5.0,
        "slots": 1,
    })
    return tr_id


# ── Schema migration ────────────────────────────────────────────────


class TestSchemaMigration:
    def test_columns_exist_on_fresh_db(self, project_dir):
        conn = get_db(project_dir)
        cols = _tr_cols(conn)
        assert "trim_in" in cols
        assert "trim_out" in cols
        assert "source_video_duration" in cols

    def test_migration_is_idempotent(self, project_dir):
        conn = get_db(project_dir)
        cols_before = _tr_cols(conn)
        _ensure_schema(conn)  # re-run
        cols_after = _tr_cols(conn)
        assert cols_before == cols_after

    def test_trim_in_default_zero(self, project_dir):
        tr_id = _seed_tr(project_dir)
        tr = get_transition(project_dir, tr_id)
        assert tr["trim_in"] == 0.0
        assert tr["trim_out"] is None
        assert tr["source_video_duration"] is None

    def test_duration_seconds_retained(self, project_dir):
        # deprecated but not dropped
        conn = get_db(project_dir)
        assert "duration_seconds" in _tr_cols(conn)


# ── Backfill ────────────────────────────────────────────────────────


class TestBackfill:
    def test_probes_selected_video(self, project_dir):
        tr_id = _seed_tr(project_dir)
        # Create a fake selected video file
        sel_dir = project_dir / "selected_transitions"
        sel_dir.mkdir()
        (sel_dir / f"{tr_id}_slot_0.mp4").write_bytes(b"fake-mp4")

        with patch.object(db_mod, "_probe_video_duration", return_value=6.02):
            stats = backfill_transition_trim(project_dir)

        assert stats["probed"] == 1
        tr = get_transition(project_dir, tr_id)
        assert tr["source_video_duration"] == pytest.approx(6.02)
        assert tr["trim_in"] == 0.0
        assert tr["trim_out"] == pytest.approx(6.02)

    def test_skips_already_populated(self, project_dir):
        tr_id = _seed_tr(project_dir)
        update_transition(project_dir, tr_id, source_video_duration=5.0, trim_in=0.5, trim_out=4.5)

        sel_dir = project_dir / "selected_transitions"
        sel_dir.mkdir()
        (sel_dir / f"{tr_id}_slot_0.mp4").write_bytes(b"fake-mp4")

        with patch.object(db_mod, "_probe_video_duration", return_value=99.0):
            stats = backfill_transition_trim(project_dir)

        assert stats["skipped"] == 1
        assert stats["probed"] == 0
        tr = get_transition(project_dir, tr_id)
        assert tr["source_video_duration"] == pytest.approx(5.0)
        assert tr["trim_in"] == pytest.approx(0.5)

    def test_missing_video_counted(self, project_dir):
        _seed_tr(project_dir)
        stats = backfill_transition_trim(project_dir)
        assert stats["missing"] == 1
        assert stats["probed"] == 0

    def test_is_idempotent(self, project_dir):
        tr_id = _seed_tr(project_dir)
        sel_dir = project_dir / "selected_transitions"
        sel_dir.mkdir()
        (sel_dir / f"{tr_id}_slot_0.mp4").write_bytes(b"fake-mp4")

        with patch.object(db_mod, "_probe_video_duration", return_value=3.14):
            s1 = backfill_transition_trim(project_dir)
            s2 = backfill_transition_trim(project_dir)

        assert s1["probed"] == 1
        assert s2["probed"] == 0
        assert s2["skipped"] == 1


# ── Undo triggers ───────────────────────────────────────────────────


class TestUndoTriggers:
    def test_update_trim_restored_via_undo(self, project_dir):
        tr_id = _seed_tr(project_dir)
        update_transition(project_dir, tr_id, trim_in=1.0, trim_out=4.0, source_video_duration=5.0)

        conn = get_db(project_dir)
        # capture the undo SQL that was logged
        row = conn.execute(
            "SELECT sql_text FROM undo_log WHERE sql_text LIKE 'UPDATE transitions%' ORDER BY seq DESC LIMIT 1"
        ).fetchone()
        assert row is not None, "no UPDATE undo entry captured"
        sql = row["sql_text"]
        assert "trim_in=" in sql
        assert "trim_out=" in sql
        assert "source_video_duration=" in sql

        # Executing the captured undo should restore prior values (trim_in=0, trim_out=NULL, svd=NULL)
        # Temporarily disable undo tracking so executing the reversal doesn't re-log
        conn.execute("UPDATE undo_state SET value = 0 WHERE key = 'active'")
        conn.execute(sql)
        conn.commit()
        conn.execute("UPDATE undo_state SET value = 1 WHERE key = 'active'")

        tr = get_transition(project_dir, tr_id)
        assert tr["trim_in"] == 0.0
        assert tr["trim_out"] is None
        assert tr["source_video_duration"] is None

    def test_delete_undo_includes_trim_columns(self, project_dir):
        tr_id = _seed_tr(project_dir)
        update_transition(project_dir, tr_id, trim_in=2.0, trim_out=7.5, source_video_duration=8.0)

        conn = get_db(project_dir)
        # Hard delete to trigger delete_undo
        conn.execute("DELETE FROM transitions WHERE id = ?", (tr_id,))
        conn.commit()

        row = conn.execute(
            "SELECT sql_text FROM undo_log WHERE sql_text LIKE 'INSERT INTO transitions%' ORDER BY seq DESC LIMIT 1"
        ).fetchone()
        assert row is not None
        sql = row["sql_text"]
        assert "trim_in" in sql
        assert "trim_out" in sql
        assert "source_video_duration" in sql
