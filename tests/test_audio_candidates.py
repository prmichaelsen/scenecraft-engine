"""Tests for the M11 audio_candidates junction + audio_clips.selected helpers.

Covers add/get/assign/remove + the get_audio_clip_effective_path read helper,
including idempotency on duplicate inserts and NULL-clearing of `selected`
when the currently-selected segment is removed.
"""

import pytest

from scenecraft.db import (
    get_db,
    add_pool_segment,
    add_audio_clip,
    get_audio_clips,
    add_audio_candidate,
    get_audio_candidates,
    assign_audio_candidate,
    remove_audio_candidate,
    get_audio_clip_effective_path,
)


@pytest.fixture
def project(tmp_path):
    project_dir = tmp_path / "myproject"
    project_dir.mkdir()
    # Force schema creation (and the audio_clips.selected migration)
    get_db(project_dir)
    return project_dir


@pytest.fixture
def setup(project):
    """Create one audio_clip + two pool_segments and return their ids."""
    clip_id = "clip_1"
    add_audio_clip(project, {
        "id": clip_id,
        "track_id": "track_1",
        "source_path": "/audio/source.wav",
        "start_time": 0.0,
        "end_time": 5.0,
    })
    seg_a = add_pool_segment(
        project,
        kind="generated",
        created_by="alice",
        pool_path="pool/segments/audio_a.wav",
        label="isolated vocals",
        duration_seconds=5.0,
    )
    seg_b = add_pool_segment(
        project,
        kind="imported",
        created_by="bob",
        pool_path="pool/segments/audio_b.wav",
        label="alt mix",
        duration_seconds=5.0,
    )
    return {"clip_id": clip_id, "seg_a": seg_a, "seg_b": seg_b, "project": project}


# ── Schema migration ──────────────────────────────────────────────

def test_schema_creates_audio_candidates_table(project):
    conn = get_db(project)
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='audio_candidates'"
    ).fetchall()
    assert len(rows) == 1


def test_schema_creates_audio_candidates_indexes(project):
    conn = get_db(project)
    names = {
        r["name"]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='audio_candidates'"
        ).fetchall()
    }
    assert "idx_audio_cand_clip" in names
    assert "idx_audio_cand_seg" in names


def test_migration_adds_selected_column(project):
    conn = get_db(project)
    cols = {row[1] for row in conn.execute("PRAGMA table_info(audio_clips)").fetchall()}
    assert "selected" in cols


# ── add_audio_candidate ───────────────────────────────────────────

def test_add_audio_candidate_basic(setup):
    add_audio_candidate(
        setup["project"],
        audio_clip_id=setup["clip_id"],
        pool_segment_id=setup["seg_a"],
        source="generated",
    )
    conn = get_db(setup["project"])
    rows = conn.execute("SELECT * FROM audio_candidates").fetchall()
    assert len(rows) == 1
    assert rows[0]["audio_clip_id"] == setup["clip_id"]
    assert rows[0]["pool_segment_id"] == setup["seg_a"]
    assert rows[0]["source"] == "generated"
    assert rows[0]["added_at"]  # iso string present


def test_add_audio_candidate_rejects_bad_source(setup):
    with pytest.raises(AssertionError):
        add_audio_candidate(
            setup["project"],
            audio_clip_id=setup["clip_id"],
            pool_segment_id=setup["seg_a"],
            source="bogus_source",
        )


def test_add_audio_candidate_idempotent(setup):
    """Calling add twice on the same (clip, segment) is a no-op (INSERT OR IGNORE)."""
    add_audio_candidate(
        setup["project"],
        audio_clip_id=setup["clip_id"],
        pool_segment_id=setup["seg_a"],
        source="generated",
        added_at="2026-04-21T00:00:00+00:00",
    )
    add_audio_candidate(
        setup["project"],
        audio_clip_id=setup["clip_id"],
        pool_segment_id=setup["seg_a"],
        source="plugin",  # different source — still ignored
        added_at="2026-04-21T01:00:00+00:00",
    )
    conn = get_db(setup["project"])
    rows = conn.execute("SELECT * FROM audio_candidates").fetchall()
    assert len(rows) == 1
    # First insert wins
    assert rows[0]["source"] == "generated"
    assert rows[0]["added_at"] == "2026-04-21T00:00:00+00:00"


def test_add_audio_candidate_accepts_all_documented_sources(setup):
    """All four documented source values are valid; using each on a distinct
    (clip, segment) pair should succeed without assertion."""
    project = setup["project"]
    clip_id = setup["clip_id"]
    # Need 4 distinct segments
    seg_ids = [setup["seg_a"], setup["seg_b"]]
    seg_ids.append(add_pool_segment(project, kind="generated", created_by="x", pool_path="pool/c.wav"))
    seg_ids.append(add_pool_segment(project, kind="generated", created_by="x", pool_path="pool/d.wav"))
    for seg, source in zip(seg_ids, ("generated", "imported", "chat_generation", "plugin")):
        add_audio_candidate(project, audio_clip_id=clip_id, pool_segment_id=seg, source=source)
    conn = get_db(project)
    sources = {r["source"] for r in conn.execute("SELECT source FROM audio_candidates").fetchall()}
    assert sources == {"generated", "imported", "chat_generation", "plugin"}


# ── get_audio_candidates ──────────────────────────────────────────

def test_get_audio_candidates_returns_newest_first(setup):
    """Ordered by added_at DESC, joined with pool_segments, with addedAt + junctionSource."""
    project = setup["project"]
    clip_id = setup["clip_id"]
    add_audio_candidate(
        project, audio_clip_id=clip_id, pool_segment_id=setup["seg_a"],
        source="generated", added_at="2026-01-01T00:00:00+00:00",
    )
    add_audio_candidate(
        project, audio_clip_id=clip_id, pool_segment_id=setup["seg_b"],
        source="imported", added_at="2026-02-01T00:00:00+00:00",
    )
    cands = get_audio_candidates(project, clip_id)
    assert len(cands) == 2
    # Newest first
    assert cands[0]["id"] == setup["seg_b"]
    assert cands[0]["addedAt"] == "2026-02-01T00:00:00+00:00"
    assert cands[0]["junctionSource"] == "imported"
    assert cands[0]["poolPath"] == "pool/segments/audio_b.wav"
    assert cands[1]["id"] == setup["seg_a"]
    assert cands[1]["junctionSource"] == "generated"


def test_get_audio_candidates_empty(setup):
    assert get_audio_candidates(setup["project"], setup["clip_id"]) == []


# ── assign_audio_candidate ────────────────────────────────────────

def test_assign_audio_candidate_sets_selected(setup):
    project = setup["project"]
    add_audio_candidate(project, audio_clip_id=setup["clip_id"],
                        pool_segment_id=setup["seg_a"], source="generated")
    assign_audio_candidate(project, setup["clip_id"], setup["seg_a"])
    conn = get_db(project)
    row = conn.execute("SELECT selected FROM audio_clips WHERE id = ?", (setup["clip_id"],)).fetchone()
    assert row["selected"] == setup["seg_a"]


def test_assign_audio_candidate_none_reverts(setup):
    project = setup["project"]
    add_audio_candidate(project, audio_clip_id=setup["clip_id"],
                        pool_segment_id=setup["seg_a"], source="generated")
    assign_audio_candidate(project, setup["clip_id"], setup["seg_a"])
    assign_audio_candidate(project, setup["clip_id"], None)
    conn = get_db(project)
    row = conn.execute("SELECT selected FROM audio_clips WHERE id = ?", (setup["clip_id"],)).fetchone()
    assert row["selected"] is None


# ── remove_audio_candidate ────────────────────────────────────────

def test_remove_audio_candidate_deletes_row(setup):
    project = setup["project"]
    add_audio_candidate(project, audio_clip_id=setup["clip_id"],
                        pool_segment_id=setup["seg_a"], source="generated")
    add_audio_candidate(project, audio_clip_id=setup["clip_id"],
                        pool_segment_id=setup["seg_b"], source="imported")
    remove_audio_candidate(project, setup["clip_id"], setup["seg_a"])
    cands = get_audio_candidates(project, setup["clip_id"])
    assert len(cands) == 1
    assert cands[0]["id"] == setup["seg_b"]


def test_remove_audio_candidate_clears_selected_when_pointed(setup):
    """If the removed segment was the selected one, selected should become NULL."""
    project = setup["project"]
    add_audio_candidate(project, audio_clip_id=setup["clip_id"],
                        pool_segment_id=setup["seg_a"], source="generated")
    assign_audio_candidate(project, setup["clip_id"], setup["seg_a"])
    remove_audio_candidate(project, setup["clip_id"], setup["seg_a"])
    conn = get_db(project)
    row = conn.execute("SELECT selected FROM audio_clips WHERE id = ?", (setup["clip_id"],)).fetchone()
    assert row["selected"] is None


def test_remove_audio_candidate_preserves_selected_when_pointing_elsewhere(setup):
    """Removing seg_a should NOT clear selected if selected currently points at seg_b."""
    project = setup["project"]
    add_audio_candidate(project, audio_clip_id=setup["clip_id"],
                        pool_segment_id=setup["seg_a"], source="generated")
    add_audio_candidate(project, audio_clip_id=setup["clip_id"],
                        pool_segment_id=setup["seg_b"], source="imported")
    assign_audio_candidate(project, setup["clip_id"], setup["seg_b"])
    remove_audio_candidate(project, setup["clip_id"], setup["seg_a"])
    conn = get_db(project)
    row = conn.execute("SELECT selected FROM audio_clips WHERE id = ?", (setup["clip_id"],)).fetchone()
    assert row["selected"] == setup["seg_b"]


def test_remove_audio_candidate_missing_is_noop(setup):
    """Removing a (clip, segment) that doesn't exist should not raise."""
    project = setup["project"]
    remove_audio_candidate(project, setup["clip_id"], setup["seg_a"])
    assert get_audio_candidates(project, setup["clip_id"]) == []


# ── get_audio_clip_effective_path ─────────────────────────────────

def test_effective_path_falls_back_to_source_when_no_selection(setup):
    project = setup["project"]
    clips = get_audio_clips(project, "track_1")
    assert len(clips) == 1
    assert clips[0]["selected"] is None
    assert get_audio_clip_effective_path(project, clips[0]) == "/audio/source.wav"


def test_effective_path_prefers_selected_pool_segment(setup):
    project = setup["project"]
    add_audio_candidate(project, audio_clip_id=setup["clip_id"],
                        pool_segment_id=setup["seg_a"], source="generated")
    assign_audio_candidate(project, setup["clip_id"], setup["seg_a"])
    clips = get_audio_clips(project, "track_1")
    assert clips[0]["selected"] == setup["seg_a"]
    assert get_audio_clip_effective_path(project, clips[0]) == "pool/segments/audio_a.wav"


def test_effective_path_returns_empty_when_clip_dict_has_neither(setup):
    """Defensive: if a malformed dict is passed, return ''."""
    assert get_audio_clip_effective_path(setup["project"], {}) == ""


def test_effective_path_falls_back_when_selected_segment_missing(setup):
    """If audio_clips.selected points at an id that no longer exists in
    pool_segments (e.g. hard-deleted), fall back to source_path."""
    project = setup["project"]
    fake_clip = {"selected": "nonexistent_seg_id", "source_path": "/audio/source.wav"}
    assert get_audio_clip_effective_path(project, fake_clip) == "/audio/source.wav"
