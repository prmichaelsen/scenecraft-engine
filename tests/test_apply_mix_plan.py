"""Tests for the chat tool ``apply_mix_plan`` (Phase 3 batch wrapper).

Covers the atomic-undo-group invariant, partial-failure tolerance, unknown
ops, rejection of generate_dsp, and end-to-end undo of a multi-op plan.
"""

from __future__ import annotations

from pathlib import Path

import pytest


# ── Fixtures ──────────────────────────────────────────────────────────────


@pytest.fixture
def bare_project(tmp_path: Path) -> Path:
    """Empty scenecraft project with schema initialised."""
    from scenecraft.db import get_db

    p = tmp_path / "p"
    p.mkdir()
    get_db(p)
    return p


@pytest.fixture
def project_with_track(bare_project: Path) -> tuple[Path, str]:
    """Project seeded with one audio track."""
    from scenecraft.db import add_audio_track

    track_id = "t_seed"
    add_audio_track(bare_project, {"id": track_id, "name": "Seed Track", "display_order": 0})
    return bare_project, track_id


@pytest.fixture
def project_with_segment(bare_project: Path) -> tuple[Path, str]:
    """Project seeded with a pool_segment (disk file + DB row)."""
    import wave

    import numpy as np

    from scenecraft.db import add_pool_segment

    pool_dir = bare_project / "pool" / "segments"
    pool_dir.mkdir(parents=True)
    rel = "pool/segments/tone.wav"
    abs_ = bare_project / rel
    sr = 22050
    n = int(2.0 * sr)
    t = np.linspace(0, 2.0, n, endpoint=False)
    samples = (0.2 * np.sin(2 * np.pi * 220 * t)).astype(np.float32)
    as_int16 = (np.clip(samples, -1.0, 1.0) * 32767).astype(np.int16)
    with wave.open(str(abs_), "wb") as f:
        f.setnchannels(1)
        f.setsampwidth(2)
        f.setframerate(sr)
        f.writeframes(as_int16.tobytes())
    seg_id = add_pool_segment(
        bare_project,
        kind="imported",
        created_by="test",
        pool_path=rel,
        original_filename="tone.wav",
        label="Tone",
        duration_seconds=2.0,
    )
    return bare_project, seg_id


# ── Helpers ───────────────────────────────────────────────────────────────


def _undo_group_count(project_dir: Path) -> int:
    from scenecraft.db import get_db

    conn = get_db(project_dir)
    row = conn.execute("SELECT COUNT(*) FROM undo_groups").fetchone()
    return row[0] if row else 0


def _undo_log_count(project_dir: Path, group_id: int) -> int:
    from scenecraft.db import get_db

    conn = get_db(project_dir)
    row = conn.execute(
        "SELECT COUNT(*) FROM undo_log WHERE undo_group = ?", (group_id,)
    ).fetchone()
    return row[0] if row else 0


# ── Tests ─────────────────────────────────────────────────────────────────


def test_empty_operations_still_opens_one_undo_group(bare_project: Path) -> None:
    from scenecraft.chat import _exec_apply_mix_plan

    before = _undo_group_count(bare_project)
    result = _exec_apply_mix_plan(
        bare_project,
        {"description": "empty plan", "operations": []},
    )
    after = _undo_group_count(bare_project)

    assert "error" not in result
    assert result["applied"] == 0
    assert result["skipped"] == 0
    assert result["results"] == []
    assert result["errors"] == []
    assert isinstance(result["undo_group_id"], int)
    assert after - before == 1, "empty plan should still create exactly one undo group"
    # No SQL captured because no mutations ran
    assert _undo_log_count(bare_project, result["undo_group_id"]) == 0


def test_single_add_audio_track(bare_project: Path) -> None:
    from scenecraft.chat import _exec_apply_mix_plan
    from scenecraft.db import get_audio_tracks

    before = _undo_group_count(bare_project)
    result = _exec_apply_mix_plan(
        bare_project,
        {
            "description": "add one track",
            "operations": [
                {"op": "add_audio_track", "args": {"name": "Solo Track"}},
            ],
        },
    )
    after = _undo_group_count(bare_project)

    assert "error" not in result
    assert result["applied"] == 1
    assert result["skipped"] == 0
    assert result["errors"] == []
    assert after - before == 1

    track_id = result["results"][0]["track_id"]
    assert track_id.startswith("audio_track")
    tracks = get_audio_tracks(bare_project)
    assert any(t["id"] == track_id and t["name"] == "Solo Track" for t in tracks)


def test_add_track_then_clip_in_one_plan(project_with_segment: tuple[Path, str]) -> None:
    """Add a track AND place a clip on it in one plan.

    IDs must be known ahead of time — v1 does not do cross-op resolution.
    So the caller pre-generates the track id (or adds the track in a previous
    turn). Here we seed an existing track to exercise the same flow: first op
    creates a second track, second op references the seeded track (id known).
    """
    from scenecraft.chat import _exec_apply_mix_plan
    from scenecraft.db import add_audio_track, get_audio_clips, get_audio_tracks

    project_dir, seg_id = project_with_segment
    seed_track_id = "t_existing"
    add_audio_track(project_dir, {"id": seed_track_id, "name": "Existing", "display_order": 0})

    before = _undo_group_count(project_dir)
    result = _exec_apply_mix_plan(
        project_dir,
        {
            "description": "two-step plan",
            "operations": [
                {"op": "add_audio_track", "args": {"name": "New Track"}},
                {
                    "op": "add_audio_clip",
                    "args": {
                        "track_id": seed_track_id,
                        "source_segment_id": seg_id,
                        "start_time": 0.0,
                    },
                },
            ],
        },
    )
    after = _undo_group_count(project_dir)

    assert "error" not in result
    assert result["applied"] == 2
    assert result["skipped"] == 0
    assert after - before == 1, "two ops must share a single undo group"

    tracks = get_audio_tracks(project_dir)
    assert any(t["name"] == "New Track" for t in tracks)
    clips = get_audio_clips(project_dir, track_id=seed_track_id)
    assert len(clips) == 1


def test_partial_failure_middle_op_fails(project_with_track: tuple[Path, str]) -> None:
    """Three ops where the middle one has bad args. Remaining ops still apply.
    The undo group covers the two successful ones."""
    from scenecraft.chat import _exec_apply_mix_plan
    from scenecraft.db import get_audio_tracks

    project_dir, track_id = project_with_track

    before = _undo_group_count(project_dir)
    result = _exec_apply_mix_plan(
        project_dir,
        {
            "description": "partial failure",
            "operations": [
                {"op": "add_audio_track", "args": {"name": "First"}},
                # Bad: points missing — should fail validation
                {
                    "op": "update_volume_curve",
                    "args": {"target_type": "track", "target_id": track_id},
                },
                {"op": "add_audio_track", "args": {"name": "Third"}},
            ],
        },
    )
    after = _undo_group_count(project_dir)

    assert result["applied"] == 2
    assert result["skipped"] == 1
    assert after - before == 1
    assert len(result["errors"]) == 1
    assert "update_volume_curve" in result["errors"][0]

    # Results array is len 3 — failed op has error entry at index 1
    assert len(result["results"]) == 3
    assert "error" in result["results"][1]
    assert "track_id" in result["results"][0]
    assert "track_id" in result["results"][2]

    # Both successful tracks are persisted
    names = {t["name"] for t in get_audio_tracks(project_dir)}
    assert "First" in names and "Third" in names


def test_one_undo_group_regardless_of_op_count(project_with_track: tuple[Path, str]) -> None:
    """Count undo_groups before and after a 5-op plan; must increment by 1."""
    from scenecraft.chat import _exec_apply_mix_plan

    project_dir, track_id = project_with_track

    before = _undo_group_count(project_dir)
    result = _exec_apply_mix_plan(
        project_dir,
        {
            "description": "big plan",
            "operations": [
                {"op": "add_audio_track", "args": {"name": f"T{i}"}}
                for i in range(5)
            ],
        },
    )
    after = _undo_group_count(project_dir)

    assert result["applied"] == 5
    assert result["skipped"] == 0
    assert after - before == 1, "5 ops should produce exactly 1 undo group"


def test_unknown_op_is_skipped_plan_continues(bare_project: Path) -> None:
    from scenecraft.chat import _exec_apply_mix_plan
    from scenecraft.db import get_audio_tracks

    result = _exec_apply_mix_plan(
        bare_project,
        {
            "description": "with unknown",
            "operations": [
                {"op": "add_audio_track", "args": {"name": "Before"}},
                {"op": "fly_to_the_moon", "args": {}},
                {"op": "add_audio_track", "args": {"name": "After"}},
            ],
        },
    )

    assert result["applied"] == 2
    assert result["skipped"] == 1
    assert len(result["errors"]) == 1
    assert "unknown op" in result["errors"][0]
    names = {t["name"] for t in get_audio_tracks(bare_project)}
    assert "Before" in names and "After" in names


def test_generate_dsp_op_is_explicit_error(bare_project: Path) -> None:
    from scenecraft.chat import _exec_apply_mix_plan

    result = _exec_apply_mix_plan(
        bare_project,
        {
            "description": "misuse",
            "operations": [
                {"op": "generate_dsp", "args": {"source_segment_id": "seg_1"}},
            ],
        },
    )

    assert result["applied"] == 0
    assert result["skipped"] == 1
    assert len(result["errors"]) == 1
    assert "not a mix operation" in result["errors"][0]


def test_undo_reverts_entire_plan(project_with_track: tuple[Path, str]) -> None:
    """Undoing the single plan group reverts EVERY successful op atomically."""
    from scenecraft.chat import _exec_apply_mix_plan
    from scenecraft.db import get_audio_tracks, undo_execute

    project_dir, _seed_track_id = project_with_track
    tracks_before = {t["id"] for t in get_audio_tracks(project_dir)}

    result = _exec_apply_mix_plan(
        project_dir,
        {
            "description": "three new tracks",
            "operations": [
                {"op": "add_audio_track", "args": {"name": "A"}},
                {"op": "add_audio_track", "args": {"name": "B"}},
                {"op": "add_audio_track", "args": {"name": "C"}},
            ],
        },
    )
    assert result["applied"] == 3

    tracks_after = {t["id"] for t in get_audio_tracks(project_dir)}
    assert len(tracks_after - tracks_before) == 3

    # Undo the most recent group (which IS our plan group)
    undone = undo_execute(project_dir)
    assert undone is not None
    assert undone["id"] == result["undo_group_id"]
    assert "mix plan" in undone["description"]

    tracks_after_undo = {t["id"] for t in get_audio_tracks(project_dir)}
    assert tracks_after_undo == tracks_before, (
        "undoing the plan should revert ALL three track inserts atomically"
    )


def test_missing_description_errors(bare_project: Path) -> None:
    from scenecraft.chat import _exec_apply_mix_plan

    result = _exec_apply_mix_plan(bare_project, {"operations": []})
    assert "error" in result
    assert "description" in result["error"]


def test_missing_operations_errors(bare_project: Path) -> None:
    from scenecraft.chat import _exec_apply_mix_plan

    result = _exec_apply_mix_plan(bare_project, {"description": "x"})
    assert "error" in result
    assert "operations" in result["error"]
