"""Tests for the chat tool ``update_volume_curve`` (Phase 2).

Covers validation + happy paths for both track and clip targets, JSON-string
input handling, and verifies the undo trigger captures the mutation (undo_log
row appears in the same group id returned by the tool).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest


# ── Fixtures ──────────────────────────────────────────────────────────────


@pytest.fixture
def project_with_track_and_clip(tmp_path: Path) -> Path:
    """Bare project with one audio track and one audio clip on it."""
    from scenecraft.db import add_audio_clip, add_audio_track, get_db

    p = tmp_path / "p"
    p.mkdir()
    get_db(p)
    add_audio_track(p, {"id": "at1", "name": "Track 1", "display_order": 0})
    add_audio_clip(
        p,
        {
            "id": "ac1",
            "track_id": "at1",
            "source_path": "pool/segments/a.wav",
            "start_time": 0.0,
            "end_time": 10.0,
        },
    )
    return p


def _track_curve(project_dir: Path, track_id: str) -> list[list[float]]:
    from scenecraft.db import get_audio_tracks

    for t in get_audio_tracks(project_dir):
        if t["id"] == track_id:
            return t["volume_curve"]
    raise AssertionError(f"track {track_id} not found")


def _clip_curve(project_dir: Path, clip_id: str) -> list[list[float]]:
    from scenecraft.db import get_audio_clips

    for c in get_audio_clips(project_dir):
        if c["id"] == clip_id:
            return c["volume_curve"]
    raise AssertionError(f"clip {clip_id} not found")


def _undo_log_count(project_dir: Path, undo_group_id: int) -> int:
    from scenecraft.db import get_db

    conn = get_db(project_dir)
    row = conn.execute(
        "SELECT COUNT(*) FROM undo_log WHERE undo_group = ?", (undo_group_id,)
    ).fetchone()
    return row[0] if row else 0


# ── Happy paths ───────────────────────────────────────────────────────────


def test_valid_three_point_curve_on_track(project_with_track_and_clip: Path) -> None:
    from scenecraft.chat import _exec_update_volume_curve

    points = [[0.0, 1.0], [0.5, 0.2], [1.0, 0.8]]
    result = _exec_update_volume_curve(
        project_with_track_and_clip,
        {"target_type": "track", "target_id": "at1", "points": points},
    )
    assert result.get("ok") is True
    assert result["target_type"] == "track"
    assert result["target_id"] == "at1"
    assert result["points_written"] == 3
    assert isinstance(result["undo_group_id"], int) and result["undo_group_id"] > 0

    stored = _track_curve(project_with_track_and_clip, "at1")
    assert stored == [[0.0, 1.0], [0.5, 0.2], [1.0, 0.8]]


def test_valid_two_point_curve_on_clip(project_with_track_and_clip: Path) -> None:
    from scenecraft.chat import _exec_update_volume_curve

    points = [[0.0, 0.0], [1.0, 1.0]]
    result = _exec_update_volume_curve(
        project_with_track_and_clip,
        {"target_type": "clip", "target_id": "ac1", "points": points},
    )
    assert result.get("ok") is True
    assert result["target_type"] == "clip"
    assert result["target_id"] == "ac1"
    assert result["points_written"] == 2

    stored = _clip_curve(project_with_track_and_clip, "ac1")
    assert stored == [[0.0, 0.0], [1.0, 1.0]]


def test_accepts_points_as_json_string(project_with_track_and_clip: Path) -> None:
    from scenecraft.chat import _exec_update_volume_curve

    points_str = json.dumps([[0.0, 0.5], [0.5, 0.75], [1.0, 1.0]])
    result = _exec_update_volume_curve(
        project_with_track_and_clip,
        {"target_type": "track", "target_id": "at1", "points": points_str},
    )
    assert result.get("ok") is True
    assert result["points_written"] == 3

    stored = _track_curve(project_with_track_and_clip, "at1")
    assert stored == [[0.0, 0.5], [0.5, 0.75], [1.0, 1.0]]


def test_undo_group_captures_mutation(project_with_track_and_clip: Path) -> None:
    from scenecraft.chat import _exec_update_volume_curve

    result = _exec_update_volume_curve(
        project_with_track_and_clip,
        {
            "target_type": "clip",
            "target_id": "ac1",
            "points": [[0.0, 1.0], [1.0, 0.0]],
        },
    )
    assert result.get("ok") is True
    group_id = result["undo_group_id"]

    # The AFTER-UPDATE trigger on audio_clips should have inserted exactly one
    # row into undo_log under our group (the trigger captures the pre-image
    # UPDATE statement).
    assert _undo_log_count(project_with_track_and_clip, group_id) == 1


def test_interpolation_arg_is_accepted_but_noted(project_with_track_and_clip: Path) -> None:
    from scenecraft.chat import _exec_update_volume_curve

    result = _exec_update_volume_curve(
        project_with_track_and_clip,
        {
            "target_type": "track",
            "target_id": "at1",
            "points": [[0.0, 0.0], [1.0, 1.0]],
            "interpolation": "linear",
        },
    )
    assert result.get("ok") is True
    # Note must be present since interpolation can't be persisted yet.
    assert "interpolation_note" in result
    assert "linear" in result["interpolation_note"]


# ── Validation errors (no DB write) ───────────────────────────────────────


def test_invalid_target_type_is_rejected(project_with_track_and_clip: Path) -> None:
    from scenecraft.chat import _exec_update_volume_curve

    before = _track_curve(project_with_track_and_clip, "at1")
    result = _exec_update_volume_curve(
        project_with_track_and_clip,
        {"target_type": "invalid", "target_id": "at1", "points": [[0.0, 0.0], [1.0, 1.0]]},
    )
    assert "error" in result
    assert "target_type" in result["error"]
    # Track unchanged.
    assert _track_curve(project_with_track_and_clip, "at1") == before


def test_missing_target_id_is_rejected(project_with_track_and_clip: Path) -> None:
    from scenecraft.chat import _exec_update_volume_curve

    before = _track_curve(project_with_track_and_clip, "at1")
    result = _exec_update_volume_curve(
        project_with_track_and_clip,
        {"target_type": "track", "points": [[0.0, 0.0], [1.0, 1.0]]},
    )
    assert "error" in result
    assert "target_id" in result["error"]
    assert _track_curve(project_with_track_and_clip, "at1") == before


def test_unknown_target_id_is_rejected(project_with_track_and_clip: Path) -> None:
    from scenecraft.chat import _exec_update_volume_curve

    result = _exec_update_volume_curve(
        project_with_track_and_clip,
        {
            "target_type": "track",
            "target_id": "does_not_exist",
            "points": [[0.0, 0.0], [1.0, 1.0]],
        },
    )
    assert "error" in result
    assert "not found" in result["error"]


def test_non_monotonic_times_rejected(project_with_track_and_clip: Path) -> None:
    from scenecraft.chat import _exec_update_volume_curve

    before = _track_curve(project_with_track_and_clip, "at1")
    result = _exec_update_volume_curve(
        project_with_track_and_clip,
        {
            "target_type": "track",
            "target_id": "at1",
            "points": [[0.0, 0.0], [0.5, 0.5], [0.3, 0.7]],
        },
    )
    assert "error" in result
    assert "increasing" in result["error"]
    assert _track_curve(project_with_track_and_clip, "at1") == before


def test_first_time_non_zero_rejected(project_with_track_and_clip: Path) -> None:
    from scenecraft.chat import _exec_update_volume_curve

    before = _track_curve(project_with_track_and_clip, "at1")
    result = _exec_update_volume_curve(
        project_with_track_and_clip,
        {
            "target_type": "track",
            "target_id": "at1",
            "points": [[0.1, 0.0], [1.0, 1.0]],
        },
    )
    assert "error" in result
    assert "0.0" in result["error"]
    assert _track_curve(project_with_track_and_clip, "at1") == before


def test_empty_points_rejected(project_with_track_and_clip: Path) -> None:
    from scenecraft.chat import _exec_update_volume_curve

    result = _exec_update_volume_curve(
        project_with_track_and_clip,
        {"target_type": "track", "target_id": "at1", "points": []},
    )
    assert "error" in result
    assert "at least 2" in result["error"]


def test_single_point_rejected(project_with_track_and_clip: Path) -> None:
    from scenecraft.chat import _exec_update_volume_curve

    result = _exec_update_volume_curve(
        project_with_track_and_clip,
        {"target_type": "track", "target_id": "at1", "points": [[0.0, 1.0]]},
    )
    assert "error" in result
    assert "at least 2" in result["error"]


def test_non_finite_value_rejected(project_with_track_and_clip: Path) -> None:
    from scenecraft.chat import _exec_update_volume_curve

    result = _exec_update_volume_curve(
        project_with_track_and_clip,
        {
            "target_type": "track",
            "target_id": "at1",
            "points": [[0.0, 0.0], [1.0, float("inf")]],
        },
    )
    assert "error" in result
    assert "finite" in result["error"]


def test_invalid_json_string_points_rejected(project_with_track_and_clip: Path) -> None:
    from scenecraft.chat import _exec_update_volume_curve

    result = _exec_update_volume_curve(
        project_with_track_and_clip,
        {"target_type": "track", "target_id": "at1", "points": "not-json"},
    )
    assert "error" in result
    assert "JSON" in result["error"]
