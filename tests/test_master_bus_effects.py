"""Tests for the master-bus effect chain (backend half).

Covers:
  - Schema migration: legacy DB with ``track_id NOT NULL`` migrates cleanly,
    existing rows survive, future rows can pass NULL.
  - ``db.add_master_bus_effect`` inserts with ``track_id IS NULL`` and appends
    to the end of the master-bus chain by default.
  - Explicit ``order_index`` on master-bus effects shifts existing master
    effects (same semantics as ``add_track_effect``).
  - ``db.list_master_bus_effects`` returns only master-bus rows (NULL
    track_id), not track-scoped rows.
  - ``db.get_master_bus_effect`` scopes to NULL (returns None for
    track-scoped ids).
  - Chat tool ``add_master_bus_effect`` happy paths (append, shift,
    static_params, validation).
  - Chat tool ``remove_master_bus_effect`` deletes + cascades to
    effect_curves; refuses track-scoped effect ids.
  - ``compute_mix_graph_hash`` includes master-bus effects in its canonical
    payload: adding, removing, and reordering changes the hash.
  - ``update_effect_param_curve`` works when passed a master-bus effect id.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest


# ── Fixtures ──────────────────────────────────────────────────────────────


@pytest.fixture
def project(tmp_path: Path) -> Path:
    """Bare project with a single audio track and no effects."""
    from scenecraft.db import add_audio_track, get_db

    p = tmp_path / "proj"
    p.mkdir()
    get_db(p)
    add_audio_track(p, {"id": "at1", "name": "Track 1", "display_order": 0})
    return p


# ── Schema migration ──────────────────────────────────────────────────────


def test_legacy_notnull_schema_migrates_cleanly(tmp_path: Path) -> None:
    """Simulate an OLD project DB (track_effects.track_id NOT NULL + legacy
    row), then open it via ``get_db`` and verify the migration: the NOT NULL
    constraint is relaxed, legacy rows survive, and future NULL inserts work.
    """
    import scenecraft.db as _db
    from scenecraft.db import (
        add_master_bus_effect,
        close_db,
        get_db,
        list_master_bus_effects,
        list_track_effects,
    )

    p = tmp_path / "legacy"
    p.mkdir()

    # 1. Create a DB with the OLD schema by hand. We bypass ``get_db`` to
    #    avoid triggering the migration, then write a legacy row.
    db_path = p / "project.db"
    conn = sqlite3.connect(str(db_path))
    conn.executescript("""
        CREATE TABLE audio_tracks (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            display_order INTEGER NOT NULL,
            muted INTEGER NOT NULL DEFAULT 0,
            solo INTEGER NOT NULL DEFAULT 0,
            volume_curve TEXT,
            hidden INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE track_effects (
            id TEXT PRIMARY KEY,
            track_id TEXT NOT NULL REFERENCES audio_tracks(id) ON DELETE CASCADE,
            effect_type TEXT NOT NULL,
            order_index INTEGER NOT NULL,
            enabled INTEGER NOT NULL DEFAULT 1,
            static_params TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        INSERT INTO audio_tracks (id, name, display_order) VALUES ('at1', 'T', 0);
        INSERT INTO track_effects (id, track_id, effect_type, order_index, enabled, static_params, created_at)
            VALUES ('legacy_eff', 'at1', 'compressor', 0, 1, '{"threshold": -12}', '2024-01-01T00:00:00Z');
    """)
    conn.commit()
    conn.close()

    # Purge any cached connections + migration flag for this path so the
    # next get_db() call actually runs _ensure_schema on this legacy DB.
    close_db(p)
    _db._migrated_dbs.discard(str(p / "project.db"))

    # 2. Now open via get_db — this MUST run the migration and leave the
    #    legacy row intact while allowing NULL track_id going forward.
    get_db(p)

    # Verify legacy row survived.
    effects = list_track_effects(p, "at1")
    assert len(effects) == 1
    assert effects[0].id == "legacy_eff"
    assert effects[0].effect_type == "compressor"
    assert effects[0].static_params == {"threshold": -12}

    # Verify NULL insert now works (migration relaxed the constraint).
    master = add_master_bus_effect(p, effect_type="limiter")
    assert master.track_id is None
    assert master.effect_type == "limiter"

    masters = list_master_bus_effects(p)
    assert len(masters) == 1
    assert masters[0].id == master.id

    # Verify the track-scoped row is still track-scoped (migration preserved
    # track_id values; it didn't blank them out).
    still_track = list_track_effects(p, "at1")
    assert len(still_track) == 1
    assert still_track[0].id == "legacy_eff"


def test_migration_idempotent(project: Path) -> None:
    """Opening an already-migrated DB twice must not re-run the migration or
    destroy data. The NOT-NULL detection short-circuits."""
    import scenecraft.db as _db
    from scenecraft.db import (
        add_master_bus_effect,
        close_db,
        get_db,
        list_master_bus_effects,
    )

    add_master_bus_effect(project, effect_type="limiter")
    # Force-reopen so _ensure_schema runs again on an already-migrated DB.
    close_db(project)
    _db._migrated_dbs.discard(str(project / "project.db"))
    get_db(project)
    masters = list_master_bus_effects(project)
    assert len(masters) == 1
    assert masters[0].effect_type == "limiter"


# ── db.add_master_bus_effect ──────────────────────────────────────────────


def test_add_master_bus_effect_null_track_id(project: Path) -> None:
    from scenecraft.db import add_master_bus_effect, get_db

    eff = add_master_bus_effect(project, effect_type="limiter")
    assert eff.track_id is None
    assert eff.order_index == 0
    assert eff.effect_type == "limiter"

    # Direct SQL check for paranoia — ensure the column really is NULL.
    conn = get_db(project)
    row = conn.execute(
        "SELECT track_id FROM track_effects WHERE id = ?", (eff.id,)
    ).fetchone()
    assert row["track_id"] is None


def test_add_master_bus_effect_auto_order(project: Path) -> None:
    from scenecraft.db import add_master_bus_effect

    a = add_master_bus_effect(project, effect_type="compressor")
    b = add_master_bus_effect(project, effect_type="limiter")
    c = add_master_bus_effect(project, effect_type="eq_band")
    assert (a.order_index, b.order_index, c.order_index) == (0, 1, 2)


def test_master_bus_effects_separate_from_tracks(project: Path) -> None:
    """list_track_effects scoped by track_id must NOT return master-bus rows,
    and list_master_bus_effects must NOT return track-scoped rows."""
    from scenecraft.db import (
        add_master_bus_effect,
        add_track_effect,
        list_master_bus_effects,
        list_track_effects,
    )

    track_eff = add_track_effect(
        project, track_id="at1", effect_type="compressor"
    )
    master_eff = add_master_bus_effect(project, effect_type="limiter")

    masters = list_master_bus_effects(project)
    assert [m.id for m in masters] == [master_eff.id]

    tracks = list_track_effects(project, "at1")
    assert [t.id for t in tracks] == [track_eff.id]


def test_get_master_bus_effect_scoped(project: Path) -> None:
    """get_master_bus_effect returns None for a track-scoped id."""
    from scenecraft.db import (
        add_master_bus_effect,
        add_track_effect,
        get_master_bus_effect,
    )

    track_eff = add_track_effect(
        project, track_id="at1", effect_type="compressor"
    )
    master_eff = add_master_bus_effect(project, effect_type="limiter")

    assert get_master_bus_effect(project, master_eff.id) is not None
    assert get_master_bus_effect(project, track_eff.id) is None
    assert get_master_bus_effect(project, "no_such_id") is None


# ── Chat tool: add_master_bus_effect ──────────────────────────────────────


def test_chat_add_master_bus_effect_happy(project: Path) -> None:
    from scenecraft.chat import _exec_add_master_bus_effect

    result = _exec_add_master_bus_effect(project, {"effect_type": "limiter"})
    assert "error" not in result
    assert result["effect_type"] == "limiter"
    assert result["order_index"] == 0
    assert isinstance(result["effect_id"], str) and result["effect_id"]


def test_chat_add_master_bus_effect_appends_in_order(project: Path) -> None:
    from scenecraft.chat import _exec_add_master_bus_effect

    first = _exec_add_master_bus_effect(project, {"effect_type": "compressor"})
    second = _exec_add_master_bus_effect(project, {"effect_type": "limiter"})
    assert first["order_index"] == 0
    assert second["order_index"] == 1


def test_chat_add_master_bus_effect_shift_on_explicit_index(project: Path) -> None:
    from scenecraft.chat import _exec_add_master_bus_effect
    from scenecraft.db import list_master_bus_effects

    a = _exec_add_master_bus_effect(project, {"effect_type": "compressor"})
    b = _exec_add_master_bus_effect(project, {"effect_type": "limiter"})
    new = _exec_add_master_bus_effect(
        project, {"effect_type": "eq_band", "order_index": 0}
    )
    assert "error" not in new
    assert new["order_index"] == 0

    effects = list_master_bus_effects(project)
    by_id = {e.id: e.order_index for e in effects}
    assert by_id[new["effect_id"]] == 0
    assert by_id[a["effect_id"]] == 1
    assert by_id[b["effect_id"]] == 2
    # No stray track-scoped effects leaked into the master chain.
    assert all(e.track_id is None for e in effects)


def test_chat_add_master_bus_effect_invalid_type(project: Path) -> None:
    from scenecraft.chat import _exec_add_master_bus_effect
    from scenecraft.db import list_master_bus_effects

    result = _exec_add_master_bus_effect(project, {"effect_type": "bogus"})
    assert "error" in result
    assert "unknown effect_type" in result["error"]
    # No DB write.
    assert list_master_bus_effects(project) == []


def test_chat_add_master_bus_effect_missing_type(project: Path) -> None:
    from scenecraft.chat import _exec_add_master_bus_effect

    result = _exec_add_master_bus_effect(project, {})
    assert "error" in result
    assert "effect_type" in result["error"]


def test_chat_add_master_bus_effect_creates_undo_group(project: Path) -> None:
    from scenecraft.chat import _exec_add_master_bus_effect
    from scenecraft.db import get_db

    conn = get_db(project)
    before = conn.execute("SELECT COUNT(*) FROM undo_groups").fetchone()[0]
    result = _exec_add_master_bus_effect(project, {"effect_type": "limiter"})
    assert "error" not in result
    after = conn.execute("SELECT COUNT(*) FROM undo_groups").fetchone()[0]
    assert after == before + 1


def test_chat_add_master_bus_effect_static_params(project: Path) -> None:
    from scenecraft.chat import _exec_add_master_bus_effect
    from scenecraft.db import get_master_bus_effect

    result = _exec_add_master_bus_effect(
        project,
        {"effect_type": "limiter", "static_params": {"threshold": -1.0}},
    )
    assert "error" not in result
    eff = get_master_bus_effect(project, result["effect_id"])
    assert eff is not None
    assert eff.static_params == {"threshold": -1.0}


# ── Chat tool: remove_master_bus_effect ───────────────────────────────────


def test_chat_remove_master_bus_effect_happy(project: Path) -> None:
    from scenecraft.chat import (
        _exec_add_master_bus_effect,
        _exec_remove_master_bus_effect,
    )
    from scenecraft.db import list_master_bus_effects

    added = _exec_add_master_bus_effect(project, {"effect_type": "limiter"})
    assert "error" not in added

    removed = _exec_remove_master_bus_effect(
        project, {"effect_id": added["effect_id"]}
    )
    assert removed == {"ok": True}
    assert list_master_bus_effects(project) == []


def test_chat_remove_master_bus_effect_rejects_track_effect(project: Path) -> None:
    """Calling remove_master_bus_effect with a track-effect id must error
    with a clear message — don't silently delete a track effect."""
    from scenecraft.chat import _exec_remove_master_bus_effect
    from scenecraft.db import add_track_effect, list_track_effects

    track_eff = add_track_effect(project, track_id="at1", effect_type="compressor")
    result = _exec_remove_master_bus_effect(project, {"effect_id": track_eff.id})
    assert "error" in result
    assert "not the master bus" in result["error"] or "track" in result["error"].lower()
    # Track effect still exists — no stealth delete.
    assert len(list_track_effects(project, "at1")) == 1


def test_chat_remove_master_bus_effect_missing(project: Path) -> None:
    from scenecraft.chat import _exec_remove_master_bus_effect

    result = _exec_remove_master_bus_effect(project, {"effect_id": "nope"})
    assert "error" in result
    assert "not found" in result["error"]


def test_chat_remove_master_bus_effect_cascades_curves(project: Path) -> None:
    """Deleting a master-bus effect must cascade to effect_curves."""
    from scenecraft.chat import (
        _exec_add_master_bus_effect,
        _exec_remove_master_bus_effect,
        _exec_update_effect_param_curve,
    )
    from scenecraft.db import get_db, list_curves_for_effect

    added = _exec_add_master_bus_effect(project, {"effect_type": "limiter"})
    effect_id = added["effect_id"]
    curve = _exec_update_effect_param_curve(
        project,
        {
            "effect_id": effect_id,
            "param_name": "threshold",
            "points": [[0.0, -3.0], [1.0, -1.0]],
        },
    )
    assert curve.get("ok") is True
    assert len(list_curves_for_effect(project, effect_id)) == 1

    _exec_remove_master_bus_effect(project, {"effect_id": effect_id})

    # Cascade: the curve row should be gone.
    conn = get_db(project)
    row = conn.execute(
        "SELECT COUNT(*) FROM effect_curves WHERE effect_id = ?", (effect_id,)
    ).fetchone()
    assert row[0] == 0


def test_chat_remove_master_bus_effect_missing_id(project: Path) -> None:
    from scenecraft.chat import _exec_remove_master_bus_effect

    result = _exec_remove_master_bus_effect(project, {})
    assert "error" in result
    assert "effect_id" in result["error"]


# ── update_effect_param_curve on master-bus effects ───────────────────────


def test_update_effect_param_curve_on_master_bus(project: Path) -> None:
    """Automation curves on master-bus effects work exactly like track
    effects — effect_id is opaque, same code path."""
    from scenecraft.chat import (
        _exec_add_master_bus_effect,
        _exec_update_effect_param_curve,
    )
    from scenecraft.db import list_curves_for_effect

    added = _exec_add_master_bus_effect(project, {"effect_type": "limiter"})
    assert "error" not in added
    effect_id = added["effect_id"]

    result = _exec_update_effect_param_curve(
        project,
        {
            "effect_id": effect_id,
            "param_name": "threshold",
            "points": [[0.0, -6.0], [0.5, -3.0], [1.0, -1.0]],
        },
    )
    assert result.get("ok") is True
    curves = list_curves_for_effect(project, effect_id)
    assert len(curves) == 1
    assert curves[0].param_name == "threshold"
    assert curves[0].points == [[0.0, -6.0], [0.5, -3.0], [1.0, -1.0]]


# ── mix_graph_hash includes master-bus effects ────────────────────────────


def test_hash_changes_when_master_bus_effect_added(project: Path) -> None:
    from scenecraft.db import add_master_bus_effect
    from scenecraft.mix_graph_hash import compute_mix_graph_hash

    h0 = compute_mix_graph_hash(project)
    add_master_bus_effect(project, effect_type="limiter")
    h1 = compute_mix_graph_hash(project)
    assert h0 != h1


def test_hash_changes_when_master_bus_effect_removed(project: Path) -> None:
    from scenecraft.db import add_master_bus_effect, delete_track_effect
    from scenecraft.mix_graph_hash import compute_mix_graph_hash

    eff = add_master_bus_effect(project, effect_type="limiter")
    h1 = compute_mix_graph_hash(project)
    delete_track_effect(project, eff.id)
    h2 = compute_mix_graph_hash(project)
    assert h1 != h2


def test_hash_changes_when_master_bus_chain_reordered(project: Path) -> None:
    from scenecraft.db import (
        add_master_bus_effect,
        list_master_bus_effects,
        update_track_effect,
    )
    from scenecraft.mix_graph_hash import compute_mix_graph_hash

    a = add_master_bus_effect(project, effect_type="compressor")
    b = add_master_bus_effect(project, effect_type="limiter")
    h_before = compute_mix_graph_hash(project)

    # Swap order_index of a and b (via a temp index to avoid UNIQUE-ish
    # collisions; there's no UNIQUE constraint, but it's cleaner).
    update_track_effect(project, a.id, order_index=99)
    update_track_effect(project, b.id, order_index=0)
    update_track_effect(project, a.id, order_index=1)

    ordered = list_master_bus_effects(project)
    assert [e.id for e in ordered] == [b.id, a.id]

    h_after = compute_mix_graph_hash(project)
    assert h_before != h_after


def test_hash_stable_across_repeat_calls_with_master_bus(project: Path) -> None:
    from scenecraft.db import add_master_bus_effect
    from scenecraft.mix_graph_hash import compute_mix_graph_hash

    add_master_bus_effect(project, effect_type="limiter")
    h1 = compute_mix_graph_hash(project)
    h2 = compute_mix_graph_hash(project)
    assert h1 == h2


def test_hash_distinguishes_master_bus_from_track_effect(project: Path) -> None:
    """Two projects with the same effect, one on a track and one on the
    master bus, must produce different hashes — the signal paths differ."""
    from scenecraft.db import (
        add_audio_track,
        add_master_bus_effect,
        add_track_effect,
        get_db,
    )
    from scenecraft.mix_graph_hash import compute_mix_graph_hash

    tmp_root = project.parent
    p_track = tmp_root / "p_track"
    p_track.mkdir()
    get_db(p_track)
    add_audio_track(p_track, {"id": "at1", "name": "T", "display_order": 0})
    add_track_effect(
        p_track,
        track_id="at1",
        effect_type="limiter",
        static_params={"threshold": -1.0},
    )

    p_master = tmp_root / "p_master"
    p_master.mkdir()
    get_db(p_master)
    add_audio_track(p_master, {"id": "at1", "name": "T", "display_order": 0})
    add_master_bus_effect(
        p_master,
        effect_type="limiter",
        static_params={"threshold": -1.0},
    )

    assert compute_mix_graph_hash(p_track) != compute_mix_graph_hash(p_master)
