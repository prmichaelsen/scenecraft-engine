"""Schema migration tests for M19 scene editor (R1, R2, R3 + CHECK constraints)."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from scenecraft.db import get_db


def test_schema_migration_creates_tables(tmp_project_dir: Path) -> None:
    """R1, R2, R3: All three new tables exist with the documented columns."""
    conn = get_db(tmp_project_dir)
    table_names = {
        r[0]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    assert "light_show__scenes" in table_names
    assert "light_show__scene_placements" in table_names
    assert "light_show__live_override" in table_names

    # Verify scenes columns (R1)
    scene_cols = {row[1] for row in conn.execute("PRAGMA table_info(light_show__scenes)")}
    assert {"id", "label", "type", "params_json", "created_at", "updated_at"}.issubset(scene_cols)

    # Verify placements columns + index (R2)
    place_cols = {row[1] for row in conn.execute("PRAGMA table_info(light_show__scene_placements)")}
    assert {"id", "scene_id", "start_time", "end_time", "display_order", "fade_in_sec", "fade_out_sec"}.issubset(place_cols)
    indexes = {row[1] for row in conn.execute("PRAGMA index_list(light_show__scene_placements)")}
    assert any("time" in i for i in indexes), f"expected time index, got {indexes}"

    # Verify live_override CHECK on id (R3)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO light_show__live_override (id, scene_id, label) "
            "VALUES ('not_current', NULL, 'x')"
        )
    conn.rollback()


def test_live_override_xor_check(tmp_project_dir: Path) -> None:
    """R3: CHECK constraint enforces scene_id XOR (inline_type AND inline_params_json).

    Uses one persistent scene + commits between rejection probes so each
    pytest.raises operates on a clean transaction state.
    """
    conn = get_db(tmp_project_dir)
    # Seed a scene we can FK to. Commit first so it survives a rollback below.
    conn.execute(
        "INSERT INTO light_show__scenes (id, label, type) VALUES ('s1', 'x', 'static_color')"
    )
    conn.commit()

    # Both null → reject (CHECK)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO light_show__live_override "
            "(id, scene_id, inline_type, inline_params_json, label) "
            "VALUES ('current', NULL, NULL, NULL, 'x')"
        )
    conn.rollback()

    # Both set → reject (CHECK)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO light_show__live_override "
            "(id, scene_id, inline_type, inline_params_json, label) "
            "VALUES ('current', 's1', 'rotating_head', '{}', 'x')"
        )
    conn.rollback()

    # Only scene_id → ok
    conn.execute(
        "INSERT INTO light_show__live_override "
        "(id, scene_id, inline_type, inline_params_json, label) "
        "VALUES ('current', 's1', NULL, NULL, 'x')"
    )
    conn.commit()
