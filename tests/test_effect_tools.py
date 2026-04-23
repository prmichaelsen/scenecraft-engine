"""Tests for the Phase 2 chat tools ``add_audio_effect`` and
``update_effect_param_curve`` (M13 effect chain).

Covers:
  - append semantics (no order_index) and monotonically-growing indices
  - explicit order_index that shifts existing effects by +1
  - effect_type / track_id validation (no DB write)
  - curve upsert (INSERT + REPLACE on duplicate param_name)
  - curve point validation (monotonic, first-time-zero, min-2-points)
  - undo_log rows exist for both tools' mutations
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest


# ── Fixtures ──────────────────────────────────────────────────────────────


@pytest.fixture
def project_with_track(tmp_path: Path) -> Path:
    """Bare project with a single empty audio track, no effects."""
    from scenecraft.db import add_audio_track, get_db

    p = tmp_path / "p"
    p.mkdir()
    get_db(p)
    add_audio_track(p, {"id": "at1", "name": "Track 1", "display_order": 0})
    return p


def _undo_log_count_at_least(project_dir: Path, undo_group_id: int) -> int:
    """Return COUNT(*) in undo_log for a given group. Zero if no rows."""
    from scenecraft.db import get_db

    conn = get_db(project_dir)
    row = conn.execute(
        "SELECT COUNT(*) FROM undo_log WHERE undo_group = ?", (undo_group_id,)
    ).fetchone()
    return row[0] if row else 0


def _latest_undo_group_id(project_dir: Path) -> int:
    """Return the most recently created undo_groups id."""
    from scenecraft.db import get_db

    conn = get_db(project_dir)
    row = conn.execute(
        "SELECT id FROM undo_groups ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert row is not None, "no undo_groups rows"
    return row[0]


# ── add_audio_effect: happy paths ─────────────────────────────────────────


def test_add_effect_on_empty_track_gets_order_zero(project_with_track: Path) -> None:
    from scenecraft.chat import _exec_add_audio_effect

    result = _exec_add_audio_effect(
        project_with_track,
        {"track_id": "at1", "effect_type": "compressor"},
    )
    assert "error" not in result
    assert result["order_index"] == 0
    assert result["track_id"] == "at1"
    assert result["effect_type"] == "compressor"
    assert isinstance(result["effect_id"], str) and result["effect_id"]


def test_add_effect_twice_second_gets_order_one(project_with_track: Path) -> None:
    from scenecraft.chat import _exec_add_audio_effect

    first = _exec_add_audio_effect(
        project_with_track,
        {"track_id": "at1", "effect_type": "compressor"},
    )
    second = _exec_add_audio_effect(
        project_with_track,
        {"track_id": "at1", "effect_type": "gate"},
    )
    assert first["order_index"] == 0
    assert second["order_index"] == 1


def test_add_effect_with_explicit_index_shifts_existing(project_with_track: Path) -> None:
    from scenecraft.chat import _exec_add_audio_effect
    from scenecraft.db import list_track_effects

    # Seed two effects at 0 and 1.
    a = _exec_add_audio_effect(
        project_with_track,
        {"track_id": "at1", "effect_type": "compressor"},
    )
    b = _exec_add_audio_effect(
        project_with_track,
        {"track_id": "at1", "effect_type": "gate"},
    )
    assert a["order_index"] == 0
    assert b["order_index"] == 1

    # Insert a new effect at index 0 — should shift the existing two by +1.
    new = _exec_add_audio_effect(
        project_with_track,
        {"track_id": "at1", "effect_type": "limiter", "order_index": 0},
    )
    assert "error" not in new
    assert new["order_index"] == 0

    effects = list_track_effects(project_with_track, "at1")
    # list_track_effects orders by order_index ASC.
    by_id = {e.id: e.order_index for e in effects}
    assert by_id[new["effect_id"]] == 0
    assert by_id[a["effect_id"]] == 1
    assert by_id[b["effect_id"]] == 2
    # Index uniqueness within track.
    assert sorted(e.order_index for e in effects) == [0, 1, 2]


def test_add_effect_with_static_params_stored(project_with_track: Path) -> None:
    from scenecraft.chat import _exec_add_audio_effect
    from scenecraft.db import get_track_effect

    result = _exec_add_audio_effect(
        project_with_track,
        {
            "track_id": "at1",
            "effect_type": "compressor",
            "static_params": {"threshold": -12, "ratio": 4},
        },
    )
    assert "error" not in result
    eff = get_track_effect(project_with_track, result["effect_id"])
    assert eff is not None
    assert eff.static_params == {"threshold": -12, "ratio": 4}


# ── add_audio_effect: validation (no DB write) ────────────────────────────


def test_add_effect_invalid_effect_type_no_write(project_with_track: Path) -> None:
    from scenecraft.chat import _exec_add_audio_effect
    from scenecraft.db import list_track_effects

    result = _exec_add_audio_effect(
        project_with_track,
        {"track_id": "at1", "effect_type": "bogus_effect"},
    )
    assert "error" in result
    assert "unknown effect_type" in result["error"]
    # No DB write.
    assert list_track_effects(project_with_track, "at1") == []


def test_add_effect_invalid_track_id_errors(project_with_track: Path) -> None:
    from scenecraft.chat import _exec_add_audio_effect

    result = _exec_add_audio_effect(
        project_with_track,
        {"track_id": "does_not_exist", "effect_type": "compressor"},
    )
    assert "error" in result
    assert "not found" in result["error"]


def test_add_effect_missing_track_id_rejected(project_with_track: Path) -> None:
    from scenecraft.chat import _exec_add_audio_effect

    result = _exec_add_audio_effect(
        project_with_track,
        {"effect_type": "compressor"},
    )
    assert "error" in result
    assert "track_id" in result["error"]


def test_add_effect_missing_effect_type_rejected(project_with_track: Path) -> None:
    from scenecraft.chat import _exec_add_audio_effect

    result = _exec_add_audio_effect(
        project_with_track,
        {"track_id": "at1"},
    )
    assert "error" in result
    assert "effect_type" in result["error"]


def test_add_effect_static_params_wrong_type_rejected(project_with_track: Path) -> None:
    from scenecraft.chat import _exec_add_audio_effect

    result = _exec_add_audio_effect(
        project_with_track,
        {"track_id": "at1", "effect_type": "compressor", "static_params": "not a dict"},
    )
    assert "error" in result
    assert "static_params" in result["error"]


# ── add_audio_effect: undo plumbing ───────────────────────────────────────


def test_add_effect_creates_undo_group(project_with_track: Path) -> None:
    from scenecraft.chat import _exec_add_audio_effect
    from scenecraft.db import get_db

    conn = get_db(project_with_track)
    before = conn.execute("SELECT COUNT(*) FROM undo_groups").fetchone()[0]
    result = _exec_add_audio_effect(
        project_with_track,
        {"track_id": "at1", "effect_type": "compressor"},
    )
    assert "error" not in result
    after = conn.execute("SELECT COUNT(*) FROM undo_groups").fetchone()[0]
    assert after == before + 1


# ── update_effect_param_curve: happy paths + upsert ───────────────────────


@pytest.fixture
def project_with_effect(project_with_track: Path) -> tuple[Path, str]:
    """Project with a track AND a seeded effect — returns (project_dir, effect_id)."""
    from scenecraft.chat import _exec_add_audio_effect

    result = _exec_add_audio_effect(
        project_with_track,
        {"track_id": "at1", "effect_type": "compressor"},
    )
    assert "error" not in result
    return project_with_track, result["effect_id"]


def test_update_effect_param_curve_creates_row(project_with_effect) -> None:
    project_dir, effect_id = project_with_effect
    from scenecraft.chat import _exec_update_effect_param_curve
    from scenecraft.db import list_curves_for_effect

    points = [[0.0, -20.0], [0.5, -10.0], [1.0, 0.0]]
    result = _exec_update_effect_param_curve(
        project_dir,
        {"effect_id": effect_id, "param_name": "threshold", "points": points},
    )
    assert result.get("ok") is True
    assert result["points_written"] == 3
    assert result["effect_id"] == effect_id
    assert result["param_name"] == "threshold"
    assert isinstance(result["effect_curve_id"], str) and result["effect_curve_id"]
    assert isinstance(result["undo_group_id"], int) and result["undo_group_id"] > 0

    curves = list_curves_for_effect(project_dir, effect_id)
    assert len(curves) == 1
    assert curves[0].param_name == "threshold"
    assert curves[0].points == [[0.0, -20.0], [0.5, -10.0], [1.0, 0.0]]


def test_update_effect_param_curve_upsert_replaces(project_with_effect) -> None:
    project_dir, effect_id = project_with_effect
    from scenecraft.chat import _exec_update_effect_param_curve
    from scenecraft.db import list_curves_for_effect

    first = _exec_update_effect_param_curve(
        project_dir,
        {
            "effect_id": effect_id,
            "param_name": "ratio",
            "points": [[0.0, 2.0], [1.0, 4.0]],
        },
    )
    assert first.get("ok") is True

    second = _exec_update_effect_param_curve(
        project_dir,
        {
            "effect_id": effect_id,
            "param_name": "ratio",
            "points": [[0.0, 8.0], [0.3, 12.0], [1.0, 16.0]],
            "interpolation": "linear",
            "visible": True,
        },
    )
    assert second.get("ok") is True
    # Same curve id across upserts (spec R2 — ON CONFLICT preserves id).
    assert second["effect_curve_id"] == first["effect_curve_id"]

    curves = list_curves_for_effect(project_dir, effect_id)
    assert len(curves) == 1  # still one row for the param_name
    assert curves[0].points == [[0.0, 8.0], [0.3, 12.0], [1.0, 16.0]]
    assert curves[0].interpolation == "linear"
    assert curves[0].visible is True


def test_update_effect_param_curve_accepts_json_string(project_with_effect) -> None:
    project_dir, effect_id = project_with_effect
    from scenecraft.chat import _exec_update_effect_param_curve

    points_str = json.dumps([[0.0, 0.0], [1.0, 1.0]])
    result = _exec_update_effect_param_curve(
        project_dir,
        {"effect_id": effect_id, "param_name": "gain", "points": points_str},
    )
    assert result.get("ok") is True
    assert result["points_written"] == 2


# ── update_effect_param_curve: validation (no DB write) ───────────────────


def test_update_effect_param_curve_unknown_effect_id(project_with_track: Path) -> None:
    from scenecraft.chat import _exec_update_effect_param_curve
    from scenecraft.db import get_db

    conn = get_db(project_with_track)
    before = conn.execute("SELECT COUNT(*) FROM effect_curves").fetchone()[0]

    result = _exec_update_effect_param_curve(
        project_with_track,
        {
            "effect_id": "no_such_effect",
            "param_name": "threshold",
            "points": [[0.0, 0.0], [1.0, 1.0]],
        },
    )
    assert "error" in result
    assert "not found" in result["error"]
    after = conn.execute("SELECT COUNT(*) FROM effect_curves").fetchone()[0]
    assert after == before


def test_update_effect_param_curve_missing_effect_id(project_with_track: Path) -> None:
    from scenecraft.chat import _exec_update_effect_param_curve

    result = _exec_update_effect_param_curve(
        project_with_track,
        {"param_name": "threshold", "points": [[0.0, 0.0], [1.0, 1.0]]},
    )
    assert "error" in result
    assert "effect_id" in result["error"]


def test_update_effect_param_curve_missing_param_name(project_with_effect) -> None:
    project_dir, effect_id = project_with_effect
    from scenecraft.chat import _exec_update_effect_param_curve

    result = _exec_update_effect_param_curve(
        project_dir,
        {"effect_id": effect_id, "points": [[0.0, 0.0], [1.0, 1.0]]},
    )
    assert "error" in result
    assert "param_name" in result["error"]


def test_update_effect_param_curve_non_monotonic_rejected(project_with_effect) -> None:
    project_dir, effect_id = project_with_effect
    from scenecraft.chat import _exec_update_effect_param_curve

    result = _exec_update_effect_param_curve(
        project_dir,
        {
            "effect_id": effect_id,
            "param_name": "threshold",
            "points": [[0.0, 0.0], [0.5, 0.5], [0.3, 0.7]],
        },
    )
    assert "error" in result
    assert "increasing" in result["error"]


def test_update_effect_param_curve_first_time_nonzero_rejected(project_with_effect) -> None:
    project_dir, effect_id = project_with_effect
    from scenecraft.chat import _exec_update_effect_param_curve

    result = _exec_update_effect_param_curve(
        project_dir,
        {
            "effect_id": effect_id,
            "param_name": "threshold",
            "points": [[0.1, 0.0], [1.0, 1.0]],
        },
    )
    assert "error" in result
    assert "0.0" in result["error"]


def test_update_effect_param_curve_too_few_points_rejected(project_with_effect) -> None:
    project_dir, effect_id = project_with_effect
    from scenecraft.chat import _exec_update_effect_param_curve

    result_empty = _exec_update_effect_param_curve(
        project_dir,
        {"effect_id": effect_id, "param_name": "threshold", "points": []},
    )
    assert "error" in result_empty
    assert "at least 2" in result_empty["error"]

    result_one = _exec_update_effect_param_curve(
        project_dir,
        {"effect_id": effect_id, "param_name": "threshold", "points": [[0.0, 1.0]]},
    )
    assert "error" in result_one
    assert "at least 2" in result_one["error"]


def test_update_effect_param_curve_invalid_interpolation(project_with_effect) -> None:
    project_dir, effect_id = project_with_effect
    from scenecraft.chat import _exec_update_effect_param_curve

    result = _exec_update_effect_param_curve(
        project_dir,
        {
            "effect_id": effect_id,
            "param_name": "threshold",
            "points": [[0.0, 0.0], [1.0, 1.0]],
            "interpolation": "cubic",
        },
    )
    assert "error" in result
    assert "interpolation" in result["error"]


# ── Undo visibility for both tools ────────────────────────────────────────


def test_both_tools_create_undo_groups(project_with_track: Path) -> None:
    from scenecraft.chat import _exec_add_audio_effect, _exec_update_effect_param_curve
    from scenecraft.db import get_db

    conn = get_db(project_with_track)
    before = conn.execute("SELECT COUNT(*) FROM undo_groups").fetchone()[0]

    add_result = _exec_add_audio_effect(
        project_with_track,
        {"track_id": "at1", "effect_type": "compressor"},
    )
    assert "error" not in add_result
    effect_id = add_result["effect_id"]

    curve_result = _exec_update_effect_param_curve(
        project_with_track,
        {
            "effect_id": effect_id,
            "param_name": "threshold",
            "points": [[0.0, -20.0], [1.0, 0.0]],
        },
    )
    assert curve_result.get("ok") is True

    after = conn.execute("SELECT COUNT(*) FROM undo_groups").fetchone()[0]
    # Exactly two new undo groups: one per tool call.
    assert after == before + 2
