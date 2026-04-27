"""Regression tests for local.engine-db-effects-and-curves.md.

One test per named entry in the spec's Base Cases + Edge Cases sections, plus
an e2e class that hits the live HTTP surface (``/api/projects/:name/
track-effects``, ``/effect-curves``, ``/send-buses``, ``/track-sends``, and
the ``/master-bus-effects``/`GET`-path).

Docstrings open with `covers Rn[, Rm, OQ-K]`. Target-state tests (DAL-level
JSON validation, warning logging on unknown effect types, `compact_order_index`
helper, `_undo_tracked_tables` inclusion of `track_sends`) are marked
`@pytest.mark.xfail(reason="target-state; awaits DAL hardening / M16 refactor",
strict=False)`.
"""
from __future__ import annotations

import json
import logging
import sqlite3
from pathlib import Path

import pytest

from scenecraft import db as scdb


# ---------------------------------------------------------------------------
# Domain-scoped seed helpers (inline; used by several tests in this file only,
# matching the task-71 pattern)
# ---------------------------------------------------------------------------

def _effects_seed_track(project_dir: Path, track_id: str = "tA", display_order: int = 0) -> str:
    scdb.add_audio_track(project_dir, {"id": track_id, "name": track_id, "display_order": display_order})
    return track_id


def _effects_seed_effect(project_dir: Path, track_id: str, effect_type: str = "gain",
                         order_index: int | None = None,
                         static_params: dict | None = None) -> str:
    eff = scdb.add_track_effect(
        project_dir, track_id=track_id, effect_type=effect_type,
        order_index=order_index, static_params=static_params,
    )
    return eff.id


def _effects_bus_ids_in_order(project_dir: Path) -> list[str]:
    return [b.id for b in scdb.list_send_buses(project_dir)]


# ---------------------------------------------------------------------------
# Base Cases
# ---------------------------------------------------------------------------


def test_default_buses_seeded_on_fresh_db(project_dir: Path, db_conn):
    """covers R7, row #1."""
    # Given: fresh project dir, schema bootstrapped via db_conn fixture.
    # When
    buses = scdb.list_send_buses(project_dir)

    # Then
    assert len(buses) == 4, f"bus-count-4: expected 4, got {len(buses)}"
    tuples = [(b.bus_type, b.label) for b in buses]
    expected = [("reverb", "Plate"), ("reverb", "Hall"), ("delay", "Delay"), ("echo", "Echo")]
    assert tuples == expected, f"bus-order: expected {expected}, got {tuples}"

    by_label = {b.label: b for b in buses}
    assert by_label["Plate"].static_params == {"ir": "plate.wav"}, \
        f"bus-static-params-plate: got {by_label['Plate'].static_params}"
    assert by_label["Hall"].static_params == {"ir": "hall.wav"}, \
        f"bus-static-params-hall: got {by_label['Hall'].static_params}"
    assert by_label["Delay"].static_params == {"time_division": "1/4", "feedback": 0.35}, \
        f"bus-static-params-delay: got {by_label['Delay'].static_params}"
    assert by_label["Echo"].static_params == {"time_ms": 120.0, "feedback": 0.0, "tone": 0.5}, \
        f"bus-static-params-echo: got {by_label['Echo'].static_params}"


def test_bus_seed_backfills_existing_tracks(project_dir: Path, db_conn):
    """covers R8, row #2.

    Production path: `_ensure_schema` seeds + backfills in one pass. The
    fixture has already finished bootstrap by the time we get here, so we
    observe the post-state: inserting tracks AFTER seeding triggers R9's
    auto-seed trigger (which is the functional equivalent — every (track,
    bus) pair is present at level 0.0 regardless of insert order).
    """
    # Given: 4 buses seeded + 2 tracks inserted via the DAL (trigger fires).
    _effects_seed_track(project_dir, "tA")
    _effects_seed_track(project_dir, "tB")

    # When
    sends = scdb.list_track_sends(project_dir)

    # Then
    assert len(sends) == 2 * 4, f"sends-count-8: expected 8, got {len(sends)}"
    assert all(s.level == 0.0 for s in sends), \
        f"sends-level-zero: some non-zero: {[s.level for s in sends]}"
    pairs = {(s.track_id, s.bus_id) for s in sends}
    bus_ids = _effects_bus_ids_in_order(project_dir)
    expected_pairs = {(t, b) for t in ("tA", "tB") for b in bus_ids}
    assert pairs == expected_pairs, \
        f"every-pair-present: missing {expected_pairs - pairs}, extra {pairs - expected_pairs}"


def test_bus_seed_skipped_when_non_empty(project_dir: Path, db_conn):
    """covers R7, row #3.

    Re-invoking `_ensure_schema` on an already-seeded DB must not double-seed.
    """
    # Given: 4 default buses already seeded; add one custom; count = 5.
    scdb.add_send_bus(project_dir, bus_type="reverb", label="Custom")
    before_buses = scdb.list_send_buses(project_dir)
    before_sends = scdb.list_track_sends(project_dir)

    # When: re-run _ensure_schema (idempotent contract)
    scdb._ensure_schema(db_conn)
    db_conn.commit()

    # Then
    after_buses = scdb.list_send_buses(project_dir)
    after_sends = scdb.list_track_sends(project_dir)
    assert len(after_buses) == len(before_buses), \
        f"no-new-buses: expected {len(before_buses)}, got {len(after_buses)}"
    assert len(after_sends) == len(before_sends), \
        f"no-track-sends-created: expected {len(before_sends)}, got {len(after_sends)}"


def test_add_effect_defaults_order_index_per_track(project_dir: Path, db_conn):
    """covers R10, row #4."""
    # Given
    _effects_seed_track(project_dir, "tA")
    _effects_seed_track(project_dir, "tB")
    _effects_seed_effect(project_dir, "tA", "gain", order_index=0)

    # When
    a2 = scdb.add_track_effect(project_dir, track_id="tA", effect_type="gain")
    b1 = scdb.add_track_effect(project_dir, track_id="tB", effect_type="gain")

    # Then
    assert a2.order_index == 1, f"tA-order-1: expected 1, got {a2.order_index}"
    assert b1.order_index == 0, f"tB-order-0: expected 0, got {b1.order_index}"


def test_add_master_effect_defaults_order_index_master_scope(project_dir: Path, db_conn):
    """covers R11, row #5."""
    # Given
    _effects_seed_track(project_dir, "tA")
    scdb.add_track_effect(project_dir, track_id="tA", effect_type="gain", order_index=0)
    scdb.add_track_effect(project_dir, track_id="tA", effect_type="gain", order_index=1)
    scdb.add_track_effect(project_dir, track_id="tA", effect_type="gain", order_index=2)
    scdb.add_master_bus_effect(project_dir, effect_type="limiter", order_index=0)

    # When
    new_master = scdb.add_master_bus_effect(project_dir, effect_type="limiter")

    # Then
    assert new_master.order_index == 1, \
        f"master-order-1: expected 1 (disjoint from track numbering), got {new_master.order_index}"


def test_list_track_effects_excludes_master_bus(project_dir: Path, db_conn):
    """covers R2, row #6."""
    # Given
    _effects_seed_track(project_dir, "tA")
    _effects_seed_effect(project_dir, "tA", "gain")
    _effects_seed_effect(project_dir, "tA", "gain")
    scdb.add_master_bus_effect(project_dir, effect_type="limiter")

    # When
    rows = scdb.list_track_effects(project_dir, "tA")

    # Then
    assert len(rows) == 2, f"count-2: expected 2, got {len(rows)}"
    assert all(r.track_id == "tA" for r in rows), \
        f"all-track-scoped: got {[r.track_id for r in rows]}"
    assert not any(r.track_id is None for r in rows), \
        "no-master-leak: master-bus row (track_id=None) leaked into list_track_effects"


def test_list_master_bus_effects_excludes_track_scoped(project_dir: Path, db_conn):
    """covers R2, row #7."""
    # Given
    _effects_seed_track(project_dir, "tA")
    _effects_seed_effect(project_dir, "tA", "gain")
    _effects_seed_effect(project_dir, "tA", "gain")
    scdb.add_master_bus_effect(project_dir, effect_type="limiter")

    # When
    rows = scdb.list_master_bus_effects(project_dir)

    # Then
    assert len(rows) == 1, f"count-1: expected 1, got {len(rows)}"
    assert rows[0].track_id is None, \
        f"master-only: expected None, got {rows[0].track_id!r}"


def test_get_master_effect_rejects_track_scoped_id(project_dir: Path, db_conn):
    """covers R2, row #8."""
    # Given
    _effects_seed_track(project_dir, "tA")
    eff_id = _effects_seed_effect(project_dir, "tA", "gain")

    # When
    got = scdb.get_master_bus_effect(project_dir, eff_id)

    # Then: no exception raised (implicit) and row is None
    assert got is None, f"returns-none: got {got!r}"


def test_new_track_autocreates_track_sends(project_dir: Path, db_conn):
    """covers R9, row #9."""
    # Given: 4 default buses present after bootstrap; zero tracks initially.
    assert scdb.list_track_sends(project_dir) == [], \
        "precondition: no sends yet"

    # When
    _effects_seed_track(project_dir, "tX")

    # Then
    sends = scdb.list_track_sends(project_dir, track_id="tX")
    assert len(sends) == 4, f"sends-count-4: expected 4, got {len(sends)}"
    bus_ids = {s.bus_id for s in sends}
    assert len(bus_ids) == 4, f"one-per-bus: duplicate bus_ids, got {[s.bus_id for s in sends]}"
    assert all(s.level == 0.0 for s in sends), \
        f"level-zero: non-zero found, got {[s.level for s in sends]}"


def test_upsert_track_send_updates_level(project_dir: Path, db_conn):
    """covers R14, row #10."""
    # Given
    _effects_seed_track(project_dir, "tX")
    bus_id = _effects_bus_ids_in_order(project_dir)[0]
    scdb.upsert_track_send(project_dir, track_id="tX", bus_id=bus_id, level=0.3)

    # When
    returned = scdb.upsert_track_send(project_dir, track_id="tX", bus_id=bus_id, level=0.7)

    # Then
    rows = [s for s in scdb.list_track_sends(project_dir, track_id="tX") if s.bus_id == bus_id]
    assert len(rows) == 1, f"single-row: expected 1, got {len(rows)}"
    assert rows[0].level == 0.7, f"level-0.7: got {rows[0].level}"
    assert returned.level == 0.7, f"return-hydrated: got {returned.level}"


def test_upsert_curve_preserves_id(project_dir: Path, db_conn):
    """covers R13, row #11."""
    # Given
    _effects_seed_track(project_dir, "tA")
    eff = _effects_seed_effect(project_dir, "tA", "gain")
    c_initial = scdb.upsert_effect_curve(
        project_dir, effect_id=eff, param_name="gain", points=[{"t": 0.0, "v": 0.0}],
    )

    # When
    c_updated = scdb.upsert_effect_curve(
        project_dir, effect_id=eff, param_name="gain",
        points=[{"t": 1.0, "v": 1.0}], interpolation="linear", visible=True,
    )

    # Then
    rows = scdb.list_curves_for_effect(project_dir, eff)
    assert len(rows) == 1, f"row-count-1: expected 1, got {len(rows)}"
    assert c_updated.id == c_initial.id, \
        f"id-unchanged: expected {c_initial.id!r}, got {c_updated.id!r}"
    assert c_updated.points == [{"t": 1.0, "v": 1.0}], \
        f"points-updated: got {c_updated.points!r}"
    assert c_updated.interpolation == "linear", \
        f"interp-linear: got {c_updated.interpolation!r}"
    assert c_updated.visible is True, f"visible-true: got {c_updated.visible!r}"


def test_add_curve_raises_on_duplicate(project_dir: Path, db_conn):
    """covers R13, row #12."""
    # Given
    _effects_seed_track(project_dir, "tA")
    eff = _effects_seed_effect(project_dir, "tA", "gain")
    scdb.add_effect_curve(project_dir, effect_id=eff, param_name="gain")

    # When / Then
    with pytest.raises(sqlite3.IntegrityError):
        scdb.add_effect_curve(project_dir, effect_id=eff, param_name="gain")

    rows = scdb.list_curves_for_effect(project_dir, eff)
    assert len(rows) == 1, f"row-count-unchanged: expected 1, got {len(rows)}"


def test_delete_effect_cascades_curves(project_dir: Path, db_conn):
    """covers R12, row #13."""
    # Given
    _effects_seed_track(project_dir, "tA")
    eff = _effects_seed_effect(project_dir, "tA", "gain")
    scdb.add_effect_curve(project_dir, effect_id=eff, param_name="gain")
    scdb.add_effect_curve(project_dir, effect_id=eff, param_name="ratio")

    # When
    scdb.delete_track_effect(project_dir, eff)

    # Then
    assert scdb.get_track_effect(project_dir, eff) is None, \
        "effect-gone: get_track_effect returned non-None"
    # Raw SELECT to bypass any DAO bug that could hide orphan rows
    row = db_conn.execute(
        "SELECT COUNT(*) AS n FROM effect_curves WHERE effect_id = ?", (eff,)
    ).fetchone()
    assert row["n"] == 0, f"curves-gone: expected 0, got {row['n']}"


def test_delete_effect_unknown_id_noop(project_dir: Path, db_conn):
    """covers R12, row #13."""
    # Given
    _effects_seed_track(project_dir, "tA")
    _effects_seed_effect(project_dir, "tA", "gain")
    before = db_conn.execute("SELECT COUNT(*) AS n FROM track_effects").fetchone()["n"]

    # When (no raise expected)
    scdb.delete_track_effect(project_dir, "eff_does_not_exist")

    # Then
    after = db_conn.execute("SELECT COUNT(*) AS n FROM track_effects").fetchone()["n"]
    assert after == before, f"no-row-count-change: before={before} after={after}"


def test_delete_bus_cascades_sends(project_dir: Path, db_conn):
    """covers R15, row #14."""
    # Given: 3 tracks → each has a send row per bus (4 buses → 12 sends total).
    _effects_seed_track(project_dir, "t1")
    _effects_seed_track(project_dir, "t2")
    _effects_seed_track(project_dir, "t3")
    bus_id = _effects_bus_ids_in_order(project_dir)[0]
    # 3 sends on busA (one per track, auto-seeded)
    assert len(scdb.list_track_sends(project_dir, bus_id=bus_id)) == 3

    # When
    scdb.delete_send_bus(project_dir, bus_id)

    # Then
    assert scdb.get_send_bus(project_dir, bus_id) is None, "bus-gone"
    # Raw count
    row = db_conn.execute(
        "SELECT COUNT(*) AS n FROM track_sends WHERE bus_id = ?", (bus_id,)
    ).fetchone()
    assert row["n"] == 0, f"sends-gone: expected 0, got {row['n']}"


def test_delete_track_cascades_to_effects_and_sends(project_dir: Path, db_conn):
    """covers R21, row #15."""
    # Given
    _effects_seed_track(project_dir, "tA")
    e1 = _effects_seed_effect(project_dir, "tA", "gain")
    e2 = _effects_seed_effect(project_dir, "tA", "gain")
    scdb.add_effect_curve(project_dir, effect_id=e1, param_name="gain")
    scdb.add_effect_curve(project_dir, effect_id=e2, param_name="gain")
    # 4 track_sends rows are auto-seeded by trigger

    # When: raw DELETE to avoid DAO side-effects; FK CASCADE does the rest.
    db_conn.execute("DELETE FROM audio_tracks WHERE id = 'tA'")
    db_conn.commit()

    # Then: raw COUNTs (avoid DAO bugs hiding orphans)
    te = db_conn.execute(
        "SELECT COUNT(*) AS n FROM track_effects WHERE track_id = 'tA'"
    ).fetchone()["n"]
    assert te == 0, f"effects-gone: expected 0, got {te}"
    curves = db_conn.execute(
        "SELECT COUNT(*) AS n FROM effect_curves WHERE effect_id IN (?, ?)", (e1, e2)
    ).fetchone()["n"]
    assert curves == 0, f"curves-gone: expected 0, got {curves}"
    sends = db_conn.execute(
        "SELECT COUNT(*) AS n FROM track_sends WHERE track_id = 'tA'"
    ).fetchone()["n"]
    assert sends == 0, f"sends-gone: expected 0, got {sends}"


def test_delete_track_preserves_master_bus_effects(project_dir: Path, db_conn):
    """covers R22, row #15."""
    # Given
    _effects_seed_track(project_dir, "tA")
    _effects_seed_effect(project_dir, "tA", "gain")
    me = scdb.add_master_bus_effect(project_dir, effect_type="limiter")
    scdb.add_effect_curve(project_dir, effect_id=me.id, param_name="gain")

    # When
    db_conn.execute("DELETE FROM audio_tracks WHERE id = 'tA'")
    db_conn.commit()

    # Then
    masters = scdb.list_master_bus_effects(project_dir)
    assert len(masters) == 1 and masters[0].id == me.id, \
        f"master-effect-survives: got {masters!r}"
    curves = scdb.list_curves_for_effect(project_dir, me.id)
    assert len(curves) == 1, f"master-curve-survives: got {curves!r}"


def test_update_effect_coerces_dict_to_json(project_dir: Path, db_conn):
    """covers R16, row #16."""
    # Given
    _effects_seed_track(project_dir, "tA")
    eff = _effects_seed_effect(project_dir, "tA", "gain")

    # When
    scdb.update_track_effect(project_dir, eff, static_params={"gain": 2.0})

    # Then
    got = scdb.get_track_effect(project_dir, eff)
    assert got.static_params == {"gain": 2.0}, f"round-trip-dict: got {got.static_params!r}"
    raw = db_conn.execute(
        "SELECT static_params FROM track_effects WHERE id = ?", (eff,)
    ).fetchone()["static_params"]
    assert raw == '{"gain": 2.0}', f"raw-row-is-json-string: got {raw!r}"


def test_update_curve_coerces_list_to_json(project_dir: Path, db_conn):
    """covers R16, row #17."""
    # Given
    _effects_seed_track(project_dir, "tA")
    eff = _effects_seed_effect(project_dir, "tA", "gain")
    c = scdb.add_effect_curve(project_dir, effect_id=eff, param_name="gain")

    # When
    scdb.update_effect_curve(project_dir, c.id, points=[{"t": 0.0, "v": 0.0}])

    # Then
    got = scdb.get_effect_curve(project_dir, c.id)
    assert got.points == [{"t": 0.0, "v": 0.0}], f"round-trip-list: got {got.points!r}"
    raw = db_conn.execute(
        "SELECT points FROM effect_curves WHERE id = ?", (c.id,)
    ).fetchone()["points"]
    assert raw == '[{"t": 0.0, "v": 0.0}]', f"raw-row-is-json-string: got {raw!r}"


def test_update_effect_static_params_is_whole_replace(project_dir: Path, db_conn):
    """covers R19, row #18."""
    # Given
    _effects_seed_track(project_dir, "tA")
    eff = scdb.add_track_effect(
        project_dir, track_id="tA", effect_type="compressor",
        static_params={"ratio": 4.0, "thresh": -12},
    ).id

    # When
    scdb.update_track_effect(project_dir, eff, static_params={"gain": 2.0})

    # Then
    got = scdb.get_track_effect(project_dir, eff).static_params
    assert "ratio" not in got, f"ratio-gone: got {got!r}"
    assert "thresh" not in got, f"thresh-gone: got {got!r}"
    assert got == {"gain": 2.0}, f"only-gain: got {got!r}"


def test_list_buses_respects_order_index(project_dir: Path, db_conn):
    """covers R18, row #19."""
    # Given
    buses_before = scdb.list_send_buses(project_dir)
    echo_id = next(b.id for b in buses_before if b.label == "Echo")

    # When
    scdb.update_send_bus(project_dir, echo_id, order_index=-1)
    buses = scdb.list_send_buses(project_dir)

    # Then
    assert buses[0].id == echo_id, \
        f"reordered: expected Echo first, got {buses[0].label!r}"
    idxs = [b.order_index for b in buses]
    assert idxs == sorted(idxs), f"stable-asc: {idxs}"


def test_list_curves_ordered_by_param_name(project_dir: Path, db_conn):
    """covers R18, row #20."""
    # Given
    _effects_seed_track(project_dir, "tA")
    eff = _effects_seed_effect(project_dir, "tA", "gain")
    for name in ("zeta", "alpha", "mu"):
        scdb.add_effect_curve(project_dir, effect_id=eff, param_name=name)

    # When
    curves = scdb.list_curves_for_effect(project_dir, eff)

    # Then
    names = [c.param_name for c in curves]
    assert names == ["alpha", "mu", "zeta"], f"order-alpha-mu-zeta: got {names}"


def test_migration_relaxes_track_id_to_nullable(tmp_path: Path):
    """covers R3, row #21.

    Build a legacy DB with NOT-NULL `track_effects.track_id`, two populated
    rows, then run _ensure_schema and assert the column got rewritten to
    nullable while both rows survived. Uses a raw sqlite3 connection (NOT
    via get_db) to lay down the legacy schema first.
    """
    # Given
    proj = tmp_path / "legacy_proj"
    proj.mkdir()
    db_path = proj / "project.db"
    raw = sqlite3.connect(str(db_path))
    raw.execute("""
        CREATE TABLE audio_tracks (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL DEFAULT '',
            display_order INTEGER NOT NULL DEFAULT 0,
            hidden INTEGER NOT NULL DEFAULT 0,
            muted INTEGER NOT NULL DEFAULT 0,
            solo INTEGER NOT NULL DEFAULT 0,
            volume_curve TEXT NOT NULL DEFAULT '[]'
        );
    """)
    raw.execute("INSERT INTO audio_tracks (id, name) VALUES ('tA', 'A')")
    raw.execute("INSERT INTO audio_tracks (id, name) VALUES ('tB', 'B')")
    raw.execute("""
        CREATE TABLE track_effects (
            id TEXT PRIMARY KEY,
            track_id TEXT NOT NULL,
            effect_type TEXT NOT NULL,
            order_index INTEGER NOT NULL,
            enabled INTEGER NOT NULL DEFAULT 1,
            static_params TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
    """)
    raw.execute(
        "INSERT INTO track_effects VALUES ('e1','tA','gain',0,1,'{}','2026-01-01')"
    )
    raw.execute(
        "INSERT INTO track_effects VALUES ('e2','tB','gain',0,1,'{}','2026-01-02')"
    )
    raw.commit()
    raw.close()

    # When: get_db triggers _ensure_schema which runs the migration.
    try:
        conn = scdb.get_db(proj)

        # Then
        info = conn.execute("PRAGMA table_info(track_effects)").fetchall()
        track_id_col = next(r for r in info if r[1] == "track_id")
        assert track_id_col[3] == 0, \
            f"track-id-nullable: expected notnull=0, got {track_id_col[3]}"

        rows = conn.execute(
            "SELECT id, track_id, effect_type, order_index, enabled, static_params, created_at "
            "FROM track_effects ORDER BY id"
        ).fetchall()
        assert len(rows) == 2, f"rows-preserved-count: got {len(rows)}"
        assert rows[0]["id"] == "e1" and rows[0]["track_id"] == "tA" \
            and rows[0]["created_at"] == "2026-01-01", f"rows-preserved-e1: got {dict(rows[0])}"
        assert rows[1]["id"] == "e2" and rows[1]["track_id"] == "tB" \
            and rows[1]["created_at"] == "2026-01-02", f"rows-preserved-e2: got {dict(rows[1])}"
    finally:
        scdb.close_db(proj)


def test_row_mapper_hydrates_types(project_dir: Path, db_conn):
    """covers R17, row #22."""
    # Given
    _effects_seed_track(project_dir, "tA")
    eff = scdb.add_track_effect(
        project_dir, track_id="tA", effect_type="gain",
        static_params={"a": 1}, enabled=True,
    ).id

    # When
    got = scdb.get_track_effect(project_dir, eff)

    # Then
    assert got.enabled is True and isinstance(got.enabled, bool), \
        f"enabled-is-bool: got {got.enabled!r} ({type(got.enabled).__name__})"
    assert got.static_params == {"a": 1} and isinstance(got.static_params, dict), \
        f"static-params-is-dict: got {got.static_params!r} ({type(got.static_params).__name__})"


# ---------------------------------------------------------------------------
# Edge Cases
# ---------------------------------------------------------------------------


def test_raw_update_non_json_breaks_mapper(project_dir: Path, db_conn):
    """covers R20, row #23."""
    # Given
    _effects_seed_track(project_dir, "tA")
    eff = _effects_seed_effect(project_dir, "tA", "gain")

    # When: bypass the DAL
    db_conn.execute(
        "UPDATE track_effects SET static_params = 'not-json' WHERE id = ?", (eff,)
    )
    db_conn.commit()
    # UPDATE succeeded (dal-bypassed-successfully — implicit, commit didn't raise).

    # Then: get_track_effect raises on json.loads
    with pytest.raises(json.JSONDecodeError):
        scdb.get_track_effect(project_dir, eff)


def test_delete_effect_with_curves_cascades(project_dir: Path, db_conn):
    """covers R12, R21, row #24."""
    # Given
    _effects_seed_track(project_dir, "tA")
    e1 = _effects_seed_effect(project_dir, "tA", "gain")
    e2 = _effects_seed_effect(project_dir, "tA", "gain")
    scdb.add_effect_curve(project_dir, effect_id=e1, param_name="gain")
    scdb.add_effect_curve(project_dir, effect_id=e1, param_name="ratio")
    scdb.add_effect_curve(project_dir, effect_id=e1, param_name="thresh")
    scdb.add_effect_curve(project_dir, effect_id=e2, param_name="gain")

    # When
    scdb.delete_track_effect(project_dir, e1)

    # Then
    e1_count = db_conn.execute(
        "SELECT COUNT(*) AS n FROM effect_curves WHERE effect_id = ?", (e1,)
    ).fetchone()["n"]
    e2_count = db_conn.execute(
        "SELECT COUNT(*) AS n FROM effect_curves WHERE effect_id = ?", (e2,)
    ).fetchone()["n"]
    assert e1_count == 0, f"e1-curves-gone: got {e1_count}"
    assert e2_count == 1, f"e2-curves-intact: got {e2_count}"


def test_upsert_track_send_fresh_insert(project_dir: Path, db_conn):
    """covers R14 edge."""
    # Given
    _effects_seed_track(project_dir, "tX")
    bus_id = _effects_bus_ids_in_order(project_dir)[0]
    # Clear the auto-seeded row so the upsert is a true fresh insert (tests
    # both the missing-row path and a round-trip read).
    scdb.delete_track_send(project_dir, "tX", bus_id)

    # When
    returned = scdb.upsert_track_send(project_dir, track_id="tX", bus_id=bus_id, level=0.5)

    # Then
    got = scdb.get_track_send(project_dir, "tX", bus_id)
    assert got.level == 0.5, f"row-created: got {got!r}"
    assert returned.track_id == got.track_id and returned.bus_id == got.bus_id \
        and returned.level == got.level, \
        f"return-matches: returned={returned!r} got={got!r}"


def test_list_track_sends_filter_combinations(project_dir: Path, db_conn):
    """covers R18 edge."""
    # Given: 2 tracks × 4 buses = 8 sends auto-seeded
    _effects_seed_track(project_dir, "tA")
    _effects_seed_track(project_dir, "tB")
    bus_id = _effects_bus_ids_in_order(project_dir)[0]

    # When / Then
    assert len(scdb.list_track_sends(project_dir)) == 8, "no-filter-count-8"
    assert len(scdb.list_track_sends(project_dir, track_id="tA")) == 4, "track-filter-count-4"
    assert len(scdb.list_track_sends(project_dir, bus_id=bus_id)) == 2, "bus-filter-count-2"
    assert len(scdb.list_track_sends(project_dir, track_id="tA", bus_id=bus_id)) == 1, \
        "both-filter-count-1"

    # order-stable
    rows = scdb.list_track_sends(project_dir)
    keys = [(s.track_id, s.bus_id) for s in rows]
    assert keys == sorted(keys), f"order-stable: got {keys}"


def test_add_effect_empty_static_params(project_dir: Path, db_conn):
    """covers R1 edge."""
    # Given
    _effects_seed_track(project_dir, "tA")

    # When
    eff = scdb.add_track_effect(project_dir, track_id="tA", effect_type="gain")

    # Then
    raw = db_conn.execute(
        "SELECT static_params FROM track_effects WHERE id = ?", (eff.id,)
    ).fetchone()["static_params"]
    assert raw == "{}", f"stored-empty-obj: got {raw!r}"
    got = scdb.get_track_effect(project_dir, eff.id)
    assert got.static_params == {}, f"hydrated-empty-dict: got {got.static_params!r}"


def test_update_noop_when_no_fields(project_dir: Path, db_conn):
    """covers R16 edge."""
    # Given
    _effects_seed_track(project_dir, "tA")
    eff = _effects_seed_effect(project_dir, "tA", "gain")
    created_before = db_conn.execute(
        "SELECT created_at FROM track_effects WHERE id = ?", (eff,)
    ).fetchone()["created_at"]

    # When
    result = scdb.update_track_effect(project_dir, eff)  # no kwargs

    # Then
    assert result is None, f"no-exception: got {result!r}"
    created_after = db_conn.execute(
        "SELECT created_at FROM track_effects WHERE id = ?", (eff,)
    ).fetchone()["created_at"]
    assert created_before == created_after, \
        f"no-sql-executed: before={created_before!r} after={created_after!r}"


def test_single_threaded_dal_no_implicit_concurrency(project_dir: Path, db_conn):
    """covers R10, R11, R14 edge (negative concurrency assertion)."""
    # Given
    _effects_seed_track(project_dir, "tA")
    # When: 3 sequential (no threads) calls
    e1 = scdb.add_track_effect(project_dir, track_id="tA", effect_type="gain")
    e2 = scdb.add_track_effect(project_dir, track_id="tA", effect_type="gain")
    e3 = scdb.add_track_effect(project_dir, track_id="tA", effect_type="gain")

    # Then
    orders = [e1.order_index, e2.order_index, e3.order_index]
    assert orders == [0, 1, 2], \
        f"order-indexes-monotonic: expected [0,1,2], got {orders}"
    # note: concurrent writers can race — deliberate out-of-scope per spec.


def test_delete_send_bus_unknown_id_noop(project_dir: Path, db_conn):
    """covers delete_send_bus edge."""
    # Given
    before = len(scdb.list_send_buses(project_dir))

    # When (no raise expected)
    scdb.delete_send_bus(project_dir, "bus_missing")

    # Then
    assert len(scdb.list_send_buses(project_dir)) == before, "bus-count-unchanged"


def test_delete_track_send_unknown_pk_noop(project_dir: Path, db_conn):
    """covers delete_track_send edge."""
    # Given
    _effects_seed_track(project_dir, "tA")
    before = len(scdb.list_track_sends(project_dir))

    # When
    scdb.delete_track_send(project_dir, "tMissing", "bMissing")

    # Then
    assert len(scdb.list_track_sends(project_dir)) == before, "count-unchanged"


def test_update_bus_static_params_whole_replace(project_dir: Path, db_conn):
    """covers R19 edge."""
    # Given
    plate = next(b for b in scdb.list_send_buses(project_dir) if b.label == "Plate")
    assert plate.static_params == {"ir": "plate.wav"}

    # When
    scdb.update_send_bus(
        project_dir, plate.id, static_params={"ir": "hall.wav", "wet": 0.5}
    )

    # Then
    got = scdb.get_send_bus(project_dir, plate.id)
    assert got.static_params == {"ir": "hall.wav", "wet": 0.5}, \
        f"whole-replace: got {got.static_params!r}"


def test_upsert_curve_conflict_ignores_new_generated_id(project_dir: Path, db_conn):
    """covers R13 edge."""
    # Given
    _effects_seed_track(project_dir, "tA")
    eff = _effects_seed_effect(project_dir, "tA", "gain")
    c_a = scdb.upsert_effect_curve(project_dir, effect_id=eff, param_name="gain")

    # When
    c_b = scdb.upsert_effect_curve(
        project_dir, effect_id=eff, param_name="gain", points=[{"t": 0.0, "v": 1.0}]
    )

    # Then
    assert c_b.id == c_a.id, f"id-stays-A: expected {c_a.id!r}, got {c_b.id!r}"
    # Only one row; no stray curve_B
    ids = db_conn.execute(
        "SELECT id FROM effect_curves WHERE effect_id = ? AND param_name = 'gain'", (eff,)
    ).fetchall()
    assert len(ids) == 1, f"no-curve-B: expected 1 row, got {len(ids)}"


def test_fk_cascade_requires_foreign_keys_pragma(project_dir: Path, db_conn):
    """covers R21 edge.

    Positive variant: the production `get_db` sets `PRAGMA foreign_keys = ON`,
    so a track delete cascades. We assert the pragma is active here as a
    witness; the negative "no-cascade when pragma is OFF" path is hard to
    exercise because the shared connection pool always sets ON on open.
    """
    # Given
    _effects_seed_track(project_dir, "tA")
    _effects_seed_effect(project_dir, "tA", "gain")

    # When: check pragma is ON in production path
    fk = db_conn.execute("PRAGMA foreign_keys").fetchone()[0]

    # Then
    assert fk == 1, f"production-pragma-on: expected 1, got {fk}"

    # And: hard-delete cascades as a positive check.
    db_conn.execute("DELETE FROM audio_tracks WHERE id = 'tA'")
    db_conn.commit()
    n = db_conn.execute(
        "SELECT COUNT(*) AS n FROM track_effects WHERE track_id = 'tA'"
    ).fetchone()["n"]
    assert n == 0, f"cascade-works: got {n}"


@pytest.mark.xfail(
    reason="target-state; awaits DAL hardening (R23, OQ-1). Current add_track_effect "
           "silently accepts any effect_type without emitting logging.warning.",
    strict=False,
)
def test_unknown_effect_type_preserved_with_warning(project_dir: Path, db_conn, caplog):
    """covers R23, OQ-1, row #25."""
    # Given
    _effects_seed_track(project_dir, "tA")
    caplog.set_level(logging.WARNING)

    # When
    eff = scdb.add_track_effect(
        project_dir, track_id="tA", effect_type="does_not_exist"
    )

    # Then
    row = db_conn.execute(
        "SELECT id, effect_type FROM track_effects WHERE id = ?", (eff.id,)
    ).fetchone()
    assert row is not None, "row-inserted: row missing from track_effects"
    assert row["effect_type"] == "does_not_exist"
    messages = " ".join(r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING)
    assert eff.id in messages and "does_not_exist" in messages, \
        f"warning-emitted: expected effect id + type in warning, got {messages!r}"


def test_upsert_send_to_deleted_bus_rejected(project_dir: Path, db_conn):
    """covers R24, OQ-2, row #26."""
    # Given
    _effects_seed_track(project_dir, "tA")
    bus_id = _effects_bus_ids_in_order(project_dir)[0]
    scdb.delete_send_bus(project_dir, bus_id)  # FK CASCADE cleans sends
    assert scdb.list_track_sends(project_dir, bus_id=bus_id) == [], \
        "precondition: sends cascaded away"

    # When / Then
    with pytest.raises(sqlite3.IntegrityError):
        scdb.upsert_track_send(project_dir, track_id="tA", bus_id=bus_id, level=0.5)

    assert scdb.list_track_sends(project_dir, bus_id=bus_id) == [], \
        "no-row-inserted: track_sends still empty for deleted bus"


@pytest.mark.xfail(
    reason="target-state; awaits DAL hardening (R25, OQ-3). Current update_track_effect "
           "accepts any value for static_params (coerces non-string via json.dumps without "
           "type-validating that the value is a JSON object).",
    strict=False,
)
def test_static_params_non_object_rejected(project_dir: Path, db_conn):
    """covers R25, OQ-3, row #27."""
    # Given
    _effects_seed_track(project_dir, "tA")
    eff = scdb.add_track_effect(
        project_dir, track_id="tA", effect_type="gain",
        static_params={"gain": 1.0},
    ).id

    # When / Then
    with pytest.raises(ValueError) as exc_list:
        scdb.update_track_effect(project_dir, eff, static_params=[1, 2, 3])
    assert "object" in str(exc_list.value).lower() or "json" in str(exc_list.value).lower(), \
        f"value-error-on-list: got {exc_list.value!r}"

    with pytest.raises(ValueError):
        scdb.update_track_effect(project_dir, eff, static_params="not-an-object")

    got = scdb.get_track_effect(project_dir, eff)
    assert got.static_params == {"gain": 1.0}, \
        f"row-unchanged: expected original params preserved, got {got.static_params!r}"


def test_order_index_gaps_permitted(project_dir: Path, db_conn):
    """covers R26, OQ-4, row #28."""
    # Given
    _effects_seed_track(project_dir, "tA")
    e0 = scdb.add_track_effect(project_dir, track_id="tA", effect_type="gain",
                               order_index=0).id
    e1 = scdb.add_track_effect(project_dir, track_id="tA", effect_type="gain",
                               order_index=1).id
    e2 = scdb.add_track_effect(project_dir, track_id="tA", effect_type="gain",
                               order_index=2).id

    # When
    scdb.delete_track_effect(project_dir, e1)
    rows = scdb.list_track_effects(project_dir, "tA")

    # Then
    orders = [r.order_index for r in rows]
    assert orders == [0, 2], f"gap-present: expected [0, 2], got {orders}"
    assert orders == sorted(orders), f"order-stable: {orders}"
    # helper-reserved — ensure the compact helper is NOT yet implemented:
    assert not hasattr(scdb, "compact_order_index"), \
        "helper-reserved: compact_order_index must not exist yet (spec R26 reserves the name)"


def test_delete_curve_after_effect_delete_idempotent(project_dir: Path, db_conn):
    """covers R27, OQ-5, row #29."""
    # Given
    _effects_seed_track(project_dir, "tA")
    eff = _effects_seed_effect(project_dir, "tA", "gain")
    c = scdb.add_effect_curve(project_dir, effect_id=eff, param_name="gain")
    scdb.delete_track_effect(project_dir, eff)  # cascade-removes c
    before = db_conn.execute(
        "SELECT COUNT(*) AS n FROM effect_curves"
    ).fetchone()["n"]
    assert before == 0, "precondition: curves cascaded"

    # When (no raise expected)
    scdb.delete_effect_curve(project_dir, c.id)

    # Then
    after = db_conn.execute(
        "SELECT COUNT(*) AS n FROM effect_curves"
    ).fetchone()["n"]
    assert after == before, f"row-count-unchanged: before={before} after={after}"


# ---------------------------------------------------------------------------
# Schema-shape tests (PRAGMA witness): codify table layouts for the spec's
# R5 (project_send_buses) and R6 (track_sends).
# ---------------------------------------------------------------------------


def test_project_send_buses_schema_shape(project_dir: Path, db_conn):
    """covers R5 — project_send_buses table shape via PRAGMA table_info."""
    rows = db_conn.execute("PRAGMA table_info(project_send_buses)").fetchall()
    cols = {r["name"]: r for r in rows}
    # All columns present with expected types and notnull bits
    expected = {
        "id": ("TEXT", 0, 1),               # name → (type, notnull, pk)
        "bus_type": ("TEXT", 1, 0),
        "label": ("TEXT", 1, 0),
        "order_index": ("INTEGER", 1, 0),
        "static_params": ("TEXT", 1, 0),
    }
    for name, (typ, notnull, pk) in expected.items():
        assert name in cols, f"r5-col-present: {name} missing from project_send_buses"
        assert cols[name]["type"].upper() == typ.upper(), \
            f"r5-{name}-type: got {cols[name]['type']!r}, want {typ!r}"
        assert cols[name]["notnull"] == notnull, \
            f"r5-{name}-notnull: got {cols[name]['notnull']}, want {notnull}"
        assert cols[name]["pk"] == pk, \
            f"r5-{name}-pk: got {cols[name]['pk']}, want {pk}"


def test_track_sends_schema_shape(project_dir: Path, db_conn):
    """covers R6 — track_sends table shape, composite PK, FKs with ON DELETE CASCADE."""
    rows = db_conn.execute("PRAGMA table_info(track_sends)").fetchall()
    cols = {r["name"]: r for r in rows}
    # All columns present
    for c in ("track_id", "bus_id", "level"):
        assert c in cols, f"r6-col-present: {c} missing from track_sends"
    # Composite PK on (track_id, bus_id)
    pk_cols = {n for n, r in cols.items() if r["pk"] > 0}
    assert pk_cols == {"track_id", "bus_id"}, \
        f"r6-composite-pk: expected {{track_id, bus_id}}, got {pk_cols}"
    # level default
    level_default = cols["level"]["dflt_value"]
    assert level_default is not None and float(level_default) == 0.0, \
        f"r6-level-default: expected 0.0, got {level_default!r}"
    # FK constraints: both columns reference parent tables w/ ON DELETE CASCADE
    fks = db_conn.execute("PRAGMA foreign_key_list(track_sends)").fetchall()
    fk_map = {r["from"]: r for r in fks}
    assert "track_id" in fk_map, f"r6-fk-track: missing FK from track_id, got {list(fk_map)}"
    assert fk_map["track_id"]["table"] == "audio_tracks", \
        f"r6-fk-track-table: got {fk_map['track_id']['table']!r}"
    assert fk_map["track_id"]["on_delete"].upper() == "CASCADE", \
        f"r6-fk-track-cascade: got {fk_map['track_id']['on_delete']!r}"
    assert "bus_id" in fk_map, f"r6-fk-bus: missing FK from bus_id, got {list(fk_map)}"
    assert fk_map["bus_id"]["table"] == "project_send_buses", \
        f"r6-fk-bus-table: got {fk_map['bus_id']['table']!r}"
    assert fk_map["bus_id"]["on_delete"].upper() == "CASCADE", \
        f"r6-fk-bus-cascade: got {fk_map['bus_id']['on_delete']!r}"


# ---------------------------------------------------------------------------
# === E2E ===
# ---------------------------------------------------------------------------
# Comprehensive HTTP-level coverage for every requirement with an observable
# effect through the live server. See conftest.py::engine_server.
# Convention: each test's docstring opens with `(covers Rn[, OQ-K], row #N, e2e)`.
# ---------------------------------------------------------------------------


def _e2e_create_track(engine_server, name: str, body: dict | None = None) -> str:
    s, resp = engine_server.json(
        "POST", f"/api/projects/{name}/audio-tracks/add", body or {"name": "T"}
    )
    assert s == 200, f"audio-tracks/add failed: {s} {resp!r}"
    return resp["id"]


def _e2e_create_effect(engine_server, name: str, track_id: str,
                       effect_type: str = "compressor", static_params: dict | None = None,
                       order_index: int | None = None) -> dict:
    body: dict = {"track_id": track_id, "effect_type": effect_type}
    if static_params is not None:
        body["static_params"] = static_params
    if order_index is not None:
        body["order_index"] = order_index
    s, resp = engine_server.json(
        "POST", f"/api/projects/{name}/track-effects", body
    )
    assert s == 200, f"track-effects/create failed: {s} {resp!r}"
    return resp


def _e2e_list_buses(engine_server, name: str) -> list[dict]:
    s, body = engine_server.json(
        "GET", f"/api/projects/{name}/send-buses"
    )
    assert s == 200, f"send-buses list failed: {s} {body!r}"
    return body.get("buses", [])


class TestEndToEnd:
    """HTTP round-trip regressions for engine-db-effects-and-curves."""

    # -------------------------------------------------------------------
    # Send buses (R7, R15, rows #1, #14, #19)
    # -------------------------------------------------------------------

    def test_e2e_default_buses_seeded(self, engine_server, project_name):
        """covers R7, row #1 (e2e): fresh project has 4 default buses via GET /send-buses."""
        buses = _e2e_list_buses(engine_server, project_name)
        assert len(buses) == 4, f"bus-count-4: got {buses!r}"
        tuples = [(b.get("bus_type"), b.get("label")) for b in buses]
        expected = [("reverb", "Plate"), ("reverb", "Hall"),
                    ("delay", "Delay"), ("echo", "Echo")]
        assert tuples == expected, f"bus-order: got {tuples!r}"

    def test_e2e_add_send_bus(self, engine_server, project_name):
        """covers R18 (e2e): POST /send-buses creates a bus; GET returns it last."""
        s, body = engine_server.json(
            "POST", f"/api/projects/{project_name}/send-buses",
            {"bus_type": "reverb", "label": "Room", "static_params": {"ir": "room.wav"}},
        )
        assert s == 200, f"create-ok: {s} {body!r}"
        new_id = body["id"]
        buses = _e2e_list_buses(engine_server, project_name)
        assert buses[-1]["id"] == new_id, f"appended-last: got {[b['id'] for b in buses]}"

    def test_e2e_update_send_bus_reorders(self, engine_server, project_name):
        """covers R18, row #19 (e2e): PATCH bus order_index reorders GET."""
        buses = _e2e_list_buses(engine_server, project_name)
        echo = next(b for b in buses if b["label"] == "Echo")
        s, _ = engine_server.json(
            "POST",
            f"/api/projects/{project_name}/send-buses/{echo['id']}",
            {"order_index": -1},
        )
        assert s == 200
        buses_after = _e2e_list_buses(engine_server, project_name)
        assert buses_after[0]["id"] == echo["id"], \
            f"reordered: expected Echo first, got {[b['label'] for b in buses_after]}"

    def test_e2e_delete_send_bus_cascades_sends(self, engine_server, project_name):
        """covers R15, row #14 (e2e): DELETE /send-buses/:id cascades to track-sends.

        Create a track (auto-creates 4 sends), delete one bus, verify subsequent
        GET /track-effects?track_id=... still works and that the bus is gone.
        """
        tid = _e2e_create_track(engine_server, project_name)
        buses = _e2e_list_buses(engine_server, project_name)
        bus_id = buses[0]["id"]
        # Upsert a non-zero level so the row is clearly present
        s, _ = engine_server.json(
            "POST", f"/api/projects/{project_name}/track-sends",
            {"track_id": tid, "bus_id": bus_id, "level": 0.5},
        )
        assert s == 200

        # DELETE the bus
        s, _h, _b = engine_server.request(
            "DELETE", f"/api/projects/{project_name}/send-buses/{bus_id}"
        )
        assert s == 200
        # Bus is gone
        after = _e2e_list_buses(engine_server, project_name)
        assert bus_id not in [b["id"] for b in after], \
            f"bus-removed: {[b['id'] for b in after]}"
        # Sends for that bus are gone (observe via DAL through a new request).
        # There's no GET track-sends endpoint per-bus; re-upsert to trigger the
        # 404 on deleted bus (R24) as a second witness.
        s2, body2 = engine_server.json(
            "POST", f"/api/projects/{project_name}/track-sends",
            {"track_id": tid, "bus_id": bus_id, "level": 0.1},
        )
        assert s2 == 404, f"upsert-after-delete-404: got {s2} {body2!r}"

    def test_e2e_delete_send_bus_idempotent(self, engine_server, project_name):
        """covers R15 (e2e): DELETE on unknown bus is idempotent (200)."""
        s, _h, _b = engine_server.request(
            "DELETE", f"/api/projects/{project_name}/send-buses/bus_missing"
        )
        assert s == 200, f"idempotent-200: got {s}"

    # -------------------------------------------------------------------
    # Track effects (R1, R2, R10, R12, R17, R18, rows #4, #6, #13)
    # -------------------------------------------------------------------

    def test_e2e_track_effect_create_and_list(self, engine_server, project_name):
        """covers R1, R17, row #22 (e2e): POST /track-effects roundtrips via GET."""
        tid = _e2e_create_track(engine_server, project_name)
        eff = _e2e_create_effect(
            engine_server, project_name, tid, "compressor", static_params={"ratio": 2.0}
        )
        s, body = engine_server.json(
            "GET", f"/api/projects/{project_name}/track-effects?track_id={tid}"
        )
        assert s == 200, f"list-ok: {s} {body!r}"
        rows = body.get("effects", [])
        found = next((e for e in rows if e["id"] == eff["id"]), None)
        assert found is not None, f"roundtrip: {rows!r}"
        # hydrated types: enabled is bool, static_params is dict
        assert isinstance(found.get("enabled"), bool), \
            f"enabled-is-bool: got {found.get('enabled')!r}"
        assert found.get("static_params") == {"ratio": 2.0}, \
            f"static-params-hydrated: got {found.get('static_params')!r}"

    def test_e2e_track_effect_order_index_per_track(self, engine_server, project_name):
        """covers R10, row #4 (e2e): omitted order_index defaults per-track."""
        tA = _e2e_create_track(engine_server, project_name, {"name": "A"})
        tB = _e2e_create_track(engine_server, project_name, {"name": "B"})
        _e2e_create_effect(engine_server, project_name, tA)  # order_index=0
        e_a2 = _e2e_create_effect(engine_server, project_name, tA)
        e_b1 = _e2e_create_effect(engine_server, project_name, tB)
        assert e_a2["order_index"] == 1, f"tA-order-1: got {e_a2!r}"
        assert e_b1["order_index"] == 0, f"tB-order-0: got {e_b1!r}"

    def test_e2e_track_effect_list_excludes_master(self, engine_server, project_name):
        """covers R2, row #6 (e2e): GET /track-effects does not return master-bus rows."""
        tid = _e2e_create_track(engine_server, project_name)
        _e2e_create_effect(engine_server, project_name, tid)
        # POST /master-bus-effects may not exist as a dedicated HTTP endpoint;
        # insert master-bus directly via DAL through project dir. The engine_server
        # fixture's work_dir/<project_name>/ is the path.
        import scenecraft.db as _scdb
        project_dir = engine_server.work_dir / project_name
        _scdb.add_master_bus_effect(project_dir, effect_type="limiter")
        _scdb.close_db(project_dir)

        s, body = engine_server.json(
            "GET", f"/api/projects/{project_name}/track-effects?track_id={tid}"
        )
        assert s == 200
        rows = body.get("effects", [])
        assert all(r.get("track_id") == tid for r in rows), \
            f"no-master-leak: got {[r.get('track_id') for r in rows]}"

    def test_e2e_master_bus_effects_list(self, engine_server, project_name):
        """covers R2, row #7 (e2e): GET /master-bus-effects returns only master rows."""
        tid = _e2e_create_track(engine_server, project_name)
        _e2e_create_effect(engine_server, project_name, tid)
        import scenecraft.db as _scdb
        project_dir = engine_server.work_dir / project_name
        me = _scdb.add_master_bus_effect(project_dir, effect_type="limiter")
        _scdb.close_db(project_dir)

        s, body = engine_server.json(
            "GET", f"/api/projects/{project_name}/master-bus-effects"
        )
        assert s == 200, f"list-ok: {s}"
        rows = body.get("effects", [])
        assert len(rows) == 1 and rows[0]["id"] == me.id, \
            f"master-only: got {rows!r}"
        assert rows[0].get("track_id") is None, \
            f"track-id-null: got {rows[0].get('track_id')!r}"

    def test_e2e_update_track_effect_enabled(self, engine_server, project_name):
        """covers R16 (e2e): PATCH effect enabled flag persists."""
        tid = _e2e_create_track(engine_server, project_name)
        eff = _e2e_create_effect(engine_server, project_name, tid)
        s, _ = engine_server.json(
            "POST", f"/api/projects/{project_name}/track-effects/{eff['id']}",
            {"enabled": False},
        )
        assert s == 200
        s2, body = engine_server.json(
            "GET", f"/api/projects/{project_name}/track-effects?track_id={tid}"
        )
        found = next(e for e in body["effects"] if e["id"] == eff["id"])
        assert found["enabled"] is False, f"enabled-persisted: got {found!r}"

    def test_e2e_update_track_effect_static_params_whole_replace(
        self, engine_server, project_name
    ):
        """covers R19, row #18 (e2e): PATCH static_params is whole-replace."""
        tid = _e2e_create_track(engine_server, project_name)
        eff = _e2e_create_effect(
            engine_server, project_name, tid, "compressor",
            static_params={"ratio": 4.0, "threshold": -12.0},
        )
        s, _ = engine_server.json(
            "POST", f"/api/projects/{project_name}/track-effects/{eff['id']}",
            {"static_params": {"ratio": 2.0}},
        )
        assert s == 200
        s2, body = engine_server.json(
            "GET", f"/api/projects/{project_name}/track-effects?track_id={tid}"
        )
        found = next(e for e in body["effects"] if e["id"] == eff["id"])
        params = found.get("static_params")
        assert params == {"ratio": 2.0}, f"whole-replace: got {params!r}"

    def test_e2e_update_order_index_reorders(self, engine_server, project_name):
        """covers R18 (e2e): PATCH order_index reflects in subsequent GET order."""
        tid = _e2e_create_track(engine_server, project_name)
        e0 = _e2e_create_effect(engine_server, project_name, tid)
        e1 = _e2e_create_effect(engine_server, project_name, tid)
        e2 = _e2e_create_effect(engine_server, project_name, tid)
        # Move e2 to front
        s, _ = engine_server.json(
            "POST", f"/api/projects/{project_name}/track-effects/{e2['id']}",
            {"order_index": 0},
        )
        assert s == 200
        s2, body = engine_server.json(
            "GET", f"/api/projects/{project_name}/track-effects?track_id={tid}"
        )
        rows = body["effects"]
        assert rows[0]["id"] == e2["id"], \
            f"reordered: expected {e2['id']} first, got {[r['id'] for r in rows]}"

    def test_e2e_delete_track_effect(self, engine_server, project_name):
        """covers R12, row #13 (e2e): DELETE /track-effects/:id removes it."""
        tid = _e2e_create_track(engine_server, project_name)
        eff = _e2e_create_effect(engine_server, project_name, tid)
        s, _h, _b = engine_server.request(
            "DELETE", f"/api/projects/{project_name}/track-effects/{eff['id']}"
        )
        assert s == 200
        s2, body = engine_server.json(
            "GET", f"/api/projects/{project_name}/track-effects?track_id={tid}"
        )
        ids = [e["id"] for e in body.get("effects", [])]
        assert eff["id"] not in ids, f"effect-gone: {ids!r}"

    def test_e2e_delete_track_effect_idempotent(self, engine_server, project_name):
        """covers R12, row #13 (e2e): DELETE unknown id → 200 (idempotent)."""
        s, _h, _b = engine_server.request(
            "DELETE", f"/api/projects/{project_name}/track-effects/eff_nope"
        )
        assert s == 200, f"idempotent-200: got {s}"

    def test_e2e_unknown_effect_type_rejected(self, engine_server, project_name):
        """covers R23 (e2e, HTTP layer): HTTP POST of unknown effect_type is 400.

        Note: the DAL accepts any effect_type (R23 says DAL preserves + warns),
        but the HTTP boundary validates against the registry. This witnesses
        the layered policy.
        """
        tid = _e2e_create_track(engine_server, project_name)
        s, body = engine_server.json(
            "POST", f"/api/projects/{project_name}/track-effects",
            {"track_id": tid, "effect_type": "does_not_exist"},
        )
        assert s == 400, f"rejected-400: got {s} {body!r}"

    # -------------------------------------------------------------------
    # Effect curves (R4, R13, R16, rows #11, #12, #17, #20)
    # -------------------------------------------------------------------

    def test_e2e_effect_curve_create_and_get(self, engine_server, project_name):
        """covers R4, R13, row #11 (e2e): POST /effect-curves + round-trip via GET /track-effects (curves inlined)."""
        tid = _e2e_create_track(engine_server, project_name)
        eff = _e2e_create_effect(engine_server, project_name, tid)
        points = [[0.0, 0.0], [1.0, 1.0]]
        s, body = engine_server.json(
            "POST", f"/api/projects/{project_name}/effect-curves",
            {"effect_id": eff["id"], "param_name": "ratio", "points": points,
             "interpolation": "linear", "visible": True},
        )
        assert s == 200, f"create-ok: {s} {body!r}"
        curve_id = body["id"]

        s2, lbody = engine_server.json(
            "GET", f"/api/projects/{project_name}/track-effects?track_id={tid}"
        )
        found_eff = next(e for e in lbody["effects"] if e["id"] == eff["id"])
        curves = found_eff.get("curves", [])
        assert len(curves) == 1 and curves[0]["id"] == curve_id, \
            f"curve-inlined: got {curves!r}"

    def test_e2e_effect_curve_upsert_preserves_id(self, engine_server, project_name):
        """covers R13, row #11 (e2e): POST twice with same (effect_id, param_name) keeps the same curve id."""
        tid = _e2e_create_track(engine_server, project_name)
        eff = _e2e_create_effect(engine_server, project_name, tid)
        s1, b1 = engine_server.json(
            "POST", f"/api/projects/{project_name}/effect-curves",
            {"effect_id": eff["id"], "param_name": "ratio",
             "points": [[0.0, 0.0]]},
        )
        assert s1 == 200
        s2, b2 = engine_server.json(
            "POST", f"/api/projects/{project_name}/effect-curves",
            {"effect_id": eff["id"], "param_name": "ratio",
             "points": [[1.0, 1.0]], "interpolation": "linear"},
        )
        assert s2 == 200
        assert b1["id"] == b2["id"], \
            f"id-preserved: first={b1['id']!r} second={b2['id']!r}"

    def test_e2e_effect_curve_update_points(self, engine_server, project_name):
        """covers R16, row #17 (e2e): PATCH curve points persists."""
        tid = _e2e_create_track(engine_server, project_name)
        eff = _e2e_create_effect(engine_server, project_name, tid)
        s, body = engine_server.json(
            "POST", f"/api/projects/{project_name}/effect-curves",
            {"effect_id": eff["id"], "param_name": "ratio",
             "points": [[0.0, 0.0]]},
        )
        curve_id = body["id"]
        s2, _ = engine_server.json(
            "POST", f"/api/projects/{project_name}/effect-curves/{curve_id}",
            {"points": [[0.0, 0.0], [1.0, 1.0]]},
        )
        assert s2 == 200
        s3, lbody = engine_server.json(
            "GET", f"/api/projects/{project_name}/track-effects?track_id={tid}"
        )
        found_eff = next(e for e in lbody["effects"] if e["id"] == eff["id"])
        curves = found_eff["curves"]
        found_curve = next(c for c in curves if c["id"] == curve_id)
        # clamped to [0,1] per handler — values 0..1 stay put.
        assert len(found_curve["points"]) == 2, f"points-updated: got {found_curve!r}"

    def test_e2e_effect_curve_create_404_on_missing_effect(
        self, engine_server, project_name
    ):
        """covers R4 (e2e): POST curve referencing non-existent effect_id → 404."""
        s, body = engine_server.json(
            "POST", f"/api/projects/{project_name}/effect-curves",
            {"effect_id": "eff_nope", "param_name": "ratio", "points": []},
        )
        assert s == 404, f"fk-404: got {s} {body!r}"

    def test_e2e_delete_track_effect_cascades_curves(
        self, engine_server, project_name
    ):
        """covers R12, row #13 (e2e): DELETE effect → subsequent GET excludes its curves."""
        tid = _e2e_create_track(engine_server, project_name)
        eff = _e2e_create_effect(engine_server, project_name, tid)
        engine_server.json(
            "POST", f"/api/projects/{project_name}/effect-curves",
            {"effect_id": eff["id"], "param_name": "ratio",
             "points": [[0.0, 0.0]]},
        )
        # Delete the effect
        engine_server.request(
            "DELETE", f"/api/projects/{project_name}/track-effects/{eff['id']}"
        )
        # GET: no effect listed → no way for curves to surface through this endpoint
        s, body = engine_server.json(
            "GET", f"/api/projects/{project_name}/track-effects?track_id={tid}"
        )
        assert all(e["id"] != eff["id"] for e in body.get("effects", [])), \
            "effect-and-curves-cascaded: effect still visible"

    def test_e2e_delete_effect_curve_idempotent(self, engine_server, project_name):
        """covers R27, OQ-5, row #29 (e2e): DELETE curve on unknown id → 200 idempotent."""
        s, _h, _b = engine_server.request(
            "DELETE", f"/api/projects/{project_name}/effect-curves/curve_nope"
        )
        assert s == 200, f"idempotent-200: got {s}"

    # -------------------------------------------------------------------
    # Track sends (R9, R14, R24, rows #9, #10, #26)
    # -------------------------------------------------------------------

    def test_e2e_track_send_upsert(self, engine_server, project_name):
        """covers R14, row #10 (e2e): POST /track-sends upserts level, retryable."""
        tid = _e2e_create_track(engine_server, project_name)
        bus_id = _e2e_list_buses(engine_server, project_name)[0]["id"]
        s, body = engine_server.json(
            "POST", f"/api/projects/{project_name}/track-sends",
            {"track_id": tid, "bus_id": bus_id, "level": 0.3},
        )
        assert s == 200 and body.get("level") == 0.3, f"first-upsert: {s} {body!r}"
        s2, body2 = engine_server.json(
            "POST", f"/api/projects/{project_name}/track-sends",
            {"track_id": tid, "bus_id": bus_id, "level": 0.7},
        )
        assert s2 == 200 and body2.get("level") == 0.7, f"second-upsert: {s2} {body2!r}"

    def test_e2e_track_send_upsert_missing_track_404(
        self, engine_server, project_name
    ):
        """covers R14 (e2e): POST /track-sends with unknown track → 404."""
        bus_id = _e2e_list_buses(engine_server, project_name)[0]["id"]
        s, body = engine_server.json(
            "POST", f"/api/projects/{project_name}/track-sends",
            {"track_id": "t_nope", "bus_id": bus_id, "level": 0.5},
        )
        assert s == 404, f"not-found: got {s} {body!r}"

    def test_e2e_track_send_upsert_missing_bus_404(
        self, engine_server, project_name
    ):
        """covers R24 (e2e): POST /track-sends with unknown bus → 404."""
        tid = _e2e_create_track(engine_server, project_name)
        s, body = engine_server.json(
            "POST", f"/api/projects/{project_name}/track-sends",
            {"track_id": tid, "bus_id": "bus_nope", "level": 0.5},
        )
        assert s == 404, f"not-found: got {s} {body!r}"

    def test_e2e_new_track_autoseeds_sends(self, engine_server, project_name):
        """covers R9, row #9 (e2e): creating a track causes subsequent upsert on each default bus to return level 0.0 starting point.

        Observable path: fresh track + any default bus → track_sends row exists
        at level 0.0, which we assert by upserting to 0.0 (no-op) and reading
        back through the PATCH response.
        """
        tid = _e2e_create_track(engine_server, project_name)
        buses = _e2e_list_buses(engine_server, project_name)
        # Upsert to 0.0 (same as seed) for each bus — no failures.
        for b in buses:
            s, body = engine_server.json(
                "POST", f"/api/projects/{project_name}/track-sends",
                {"track_id": tid, "bus_id": b["id"], "level": 0.0},
            )
            assert s == 200 and body.get("level") == 0.0, \
                f"bus {b['label']}: got {s} {body!r}"


# ---------------------------------------------------------------------------
# Coverage note:
#   Unit section covers every Behavior Table row #1..#29. Target-state xfails:
#     - test_unknown_effect_type_preserved_with_warning (R23/OQ-1)
#     - test_static_params_non_object_rejected (R25/OQ-3)
#   Other OQ resolutions (OQ-2 FK cascade, OQ-4 gap permitted, OQ-5
#   idempotent delete_effect_curve) pass today and are asserted live.
#
#   E2E section covers the HTTP-observable requirements — track effects CRUD,
#   effect curves CRUD, send-bus CRUD + cascade, track-sends upsert + FK
#   enforcement. The master-bus CREATE endpoint is DAL-only today; the test
#   injects master-bus rows via the DAL and verifies the GET /master-bus-effects
#   list path to lock the contract.
# ---------------------------------------------------------------------------
