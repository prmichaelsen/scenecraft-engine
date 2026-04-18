"""Tests for the candidate pool schema and DB helpers.

Covers pool_segments, pool_segment_tags, and tr_candidates — the three tables
introduced by the candidate pool migration design.
"""

from pathlib import Path

import pytest

from scenecraft.db import (
    get_db,
    add_pool_segment,
    get_pool_segment,
    list_pool_segments,
    update_pool_segment_label,
    delete_pool_segment,
    add_pool_segment_tag,
    remove_pool_segment_tag,
    get_pool_segment_tags,
    list_all_tags,
    find_segments_by_tag,
    add_tr_candidate,
    remove_tr_candidate,
    get_tr_candidates,
    clone_tr_candidates,
    count_tr_candidate_refs,
    find_gc_candidates,
)


@pytest.fixture
def project(tmp_path):
    project_dir = tmp_path / "myproject"
    project_dir.mkdir()
    # Force schema creation by opening a connection
    get_db(project_dir)
    return project_dir


# ── pool_segments ─────────────────────────────────────────────────

def test_add_pool_segment_generated(project):
    seg_id = add_pool_segment(
        project,
        kind="generated",
        created_by="alice",
        pool_path="pool/segments/cand_abc.mp4",
        label="",
        generation_params={
            "provider": "google-veo",
            "model": "veo-3",
            "prompt": "sunset over mountains",
            "seed": 42,
        },
        duration_seconds=5.0,
        width=1920,
        height=1080,
        byte_size=5_000_000,
    )
    assert len(seg_id) == 32  # uuid4 hex

    seg = get_pool_segment(project, seg_id)
    assert seg is not None
    assert seg["kind"] == "generated"
    assert seg["createdBy"] == "alice"
    assert seg["poolPath"] == "pool/segments/cand_abc.mp4"
    assert seg["generationParams"]["prompt"] == "sunset over mountains"
    assert seg["generationParams"]["seed"] == 42
    assert seg["durationSeconds"] == 5.0


def test_add_pool_segment_imported(project):
    seg_id = add_pool_segment(
        project,
        kind="imported",
        created_by="bob",
        pool_path="pool/segments/import_xyz.mov",
        original_filename="drone_shot.mov",
        original_filepath="/Volumes/RAID/footage/drone_shot.mov",
        label="drone_shot.mov",
    )
    seg = get_pool_segment(project, seg_id)
    assert seg["kind"] == "imported"
    assert seg["originalFilename"] == "drone_shot.mov"
    assert seg["originalFilepath"] == "/Volumes/RAID/footage/drone_shot.mov"
    assert seg["generationParams"] is None


def test_list_pool_segments(project):
    gen_id = add_pool_segment(project, kind="generated", created_by="a", pool_path="pool/segments/cand_1.mp4")
    imp_id = add_pool_segment(project, kind="imported", created_by="b", pool_path="pool/segments/import_1.mp4",
                               original_filename="foo.mp4")

    all_segs = list_pool_segments(project)
    assert len(all_segs) == 2

    just_gen = list_pool_segments(project, kind="generated")
    assert len(just_gen) == 1
    assert just_gen[0]["id"] == gen_id

    just_imp = list_pool_segments(project, kind="imported")
    assert len(just_imp) == 1
    assert just_imp[0]["id"] == imp_id


def test_update_pool_segment_label(project):
    seg_id = add_pool_segment(project, kind="imported", created_by="alice",
                               pool_path="pool/segments/import_1.mp4",
                               original_filename="raw.mov", label="raw.mov")
    update_pool_segment_label(project, seg_id, "opening drone shot")
    seg = get_pool_segment(project, seg_id)
    assert seg["label"] == "opening drone shot"
    # Immutable fields untouched
    assert seg["createdBy"] == "alice"
    assert seg["originalFilename"] == "raw.mov"


def test_pool_path_unique(project):
    add_pool_segment(project, kind="generated", created_by="a", pool_path="pool/segments/dup.mp4")
    import sqlite3
    with pytest.raises(sqlite3.IntegrityError):
        add_pool_segment(project, kind="generated", created_by="b", pool_path="pool/segments/dup.mp4")


# ── pool_segment_tags ──────────────────────────────────────────────

def test_add_and_get_tags(project):
    seg_id = add_pool_segment(project, kind="generated", created_by="alice",
                               pool_path="pool/segments/cand_1.mp4")
    add_pool_segment_tag(project, seg_id, "keeper", tagged_by="alice")
    add_pool_segment_tag(project, seg_id, "sunset", tagged_by="bob")

    tags = get_pool_segment_tags(project, seg_id)
    tag_names = {t["tag"] for t in tags}
    assert tag_names == {"keeper", "sunset"}

    # Attribution preserved
    by_name = {t["tag"]: t["taggedBy"] for t in tags}
    assert by_name["keeper"] == "alice"
    assert by_name["sunset"] == "bob"


def test_add_tag_idempotent(project):
    seg_id = add_pool_segment(project, kind="generated", created_by="a",
                               pool_path="pool/segments/cand_1.mp4")
    add_pool_segment_tag(project, seg_id, "keeper", tagged_by="alice")
    # Same user, same tag — no-op
    add_pool_segment_tag(project, seg_id, "keeper", tagged_by="alice")
    tags = get_pool_segment_tags(project, seg_id)
    assert len(tags) == 1


def test_remove_tag(project):
    seg_id = add_pool_segment(project, kind="generated", created_by="a",
                               pool_path="pool/segments/cand_1.mp4")
    add_pool_segment_tag(project, seg_id, "keeper", tagged_by="alice")
    add_pool_segment_tag(project, seg_id, "reject", tagged_by="alice")
    remove_pool_segment_tag(project, seg_id, "reject")
    tags = get_pool_segment_tags(project, seg_id)
    assert {t["tag"] for t in tags} == {"keeper"}


def test_find_segments_by_tag(project):
    s1 = add_pool_segment(project, kind="generated", created_by="a",
                          pool_path="pool/segments/cand_1.mp4")
    s2 = add_pool_segment(project, kind="generated", created_by="a",
                          pool_path="pool/segments/cand_2.mp4")
    s3 = add_pool_segment(project, kind="generated", created_by="a",
                          pool_path="pool/segments/cand_3.mp4")
    add_pool_segment_tag(project, s1, "keeper", tagged_by="alice")
    add_pool_segment_tag(project, s2, "keeper", tagged_by="alice")
    add_pool_segment_tag(project, s3, "reject", tagged_by="alice")

    keepers = find_segments_by_tag(project, "keeper")
    keeper_ids = {s["id"] for s in keepers}
    assert keeper_ids == {s1, s2}


def test_list_all_tags_with_counts(project):
    s1 = add_pool_segment(project, kind="generated", created_by="a",
                          pool_path="pool/segments/cand_1.mp4")
    s2 = add_pool_segment(project, kind="generated", created_by="a",
                          pool_path="pool/segments/cand_2.mp4")
    add_pool_segment_tag(project, s1, "keeper", tagged_by="alice")
    add_pool_segment_tag(project, s2, "keeper", tagged_by="alice")
    add_pool_segment_tag(project, s1, "sunset", tagged_by="alice")

    all_tags = list_all_tags(project)
    by_name = {t["tag"]: t["count"] for t in all_tags}
    assert by_name["keeper"] == 2
    assert by_name["sunset"] == 1


# ── tr_candidates ──────────────────────────────────────────────────

def test_add_and_get_tr_candidate(project):
    seg_id = add_pool_segment(project, kind="generated", created_by="alice",
                               pool_path="pool/segments/cand_1.mp4")
    add_tr_candidate(project, transition_id="tr_001", slot=0,
                     pool_segment_id=seg_id, source="generated")

    cands = get_tr_candidates(project, "tr_001", slot=0)
    assert len(cands) == 1
    assert cands[0]["id"] == seg_id
    assert cands[0]["junctionSource"] == "generated"


def test_tr_candidates_ordered_by_added_at(project):
    # Three candidates added in sequence — ordering must reflect added_at
    import time
    s1 = add_pool_segment(project, kind="generated", created_by="a",
                          pool_path="pool/segments/c1.mp4")
    add_tr_candidate(project, transition_id="tr_001", slot=0,
                     pool_segment_id=s1, source="generated")
    time.sleep(0.01)
    s2 = add_pool_segment(project, kind="generated", created_by="a",
                          pool_path="pool/segments/c2.mp4")
    add_tr_candidate(project, transition_id="tr_001", slot=0,
                     pool_segment_id=s2, source="generated")
    time.sleep(0.01)
    s3 = add_pool_segment(project, kind="generated", created_by="a",
                          pool_path="pool/segments/c3.mp4")
    add_tr_candidate(project, transition_id="tr_001", slot=0,
                     pool_segment_id=s3, source="generated")

    cands = get_tr_candidates(project, "tr_001", slot=0)
    assert [c["id"] for c in cands] == [s1, s2, s3]


def test_tr_candidates_slot_isolation(project):
    s1 = add_pool_segment(project, kind="generated", created_by="a",
                          pool_path="pool/segments/c1.mp4")
    s2 = add_pool_segment(project, kind="generated", created_by="a",
                          pool_path="pool/segments/c2.mp4")
    add_tr_candidate(project, transition_id="tr_001", slot=0,
                     pool_segment_id=s1, source="generated")
    add_tr_candidate(project, transition_id="tr_001", slot=1,
                     pool_segment_id=s2, source="generated")

    slot0 = get_tr_candidates(project, "tr_001", slot=0)
    slot1 = get_tr_candidates(project, "tr_001", slot=1)
    assert len(slot0) == 1 and slot0[0]["id"] == s1
    assert len(slot1) == 1 and slot1[0]["id"] == s2


def test_clone_tr_candidates(project):
    """Split/duplicate semantics: same segments, preserved added_at."""
    s1 = add_pool_segment(project, kind="generated", created_by="a",
                          pool_path="pool/segments/c1.mp4")
    s2 = add_pool_segment(project, kind="generated", created_by="a",
                          pool_path="pool/segments/c2.mp4")
    add_tr_candidate(project, transition_id="tr_orig", slot=0,
                     pool_segment_id=s1, source="generated")
    add_tr_candidate(project, transition_id="tr_orig", slot=0,
                     pool_segment_id=s2, source="generated")

    # Simulate split: clone to tr1 and tr2
    n1 = clone_tr_candidates(project, source_transition_id="tr_orig",
                              target_transition_id="tr_1", new_source="split-inherit")
    n2 = clone_tr_candidates(project, source_transition_id="tr_orig",
                              target_transition_id="tr_2", new_source="split-inherit")
    assert n1 == 2 and n2 == 2

    tr1 = get_tr_candidates(project, "tr_1", slot=0)
    tr2 = get_tr_candidates(project, "tr_2", slot=0)
    assert {c["id"] for c in tr1} == {s1, s2}
    assert {c["id"] for c in tr2} == {s1, s2}
    # source tag reflects split-inherit
    assert all(c["junctionSource"] == "split-inherit" for c in tr1)
    assert all(c["junctionSource"] == "split-inherit" for c in tr2)

    # Original still intact
    orig = get_tr_candidates(project, "tr_orig", slot=0)
    assert len(orig) == 2


def test_count_tr_candidate_refs(project):
    s1 = add_pool_segment(project, kind="generated", created_by="a",
                          pool_path="pool/segments/c1.mp4")
    assert count_tr_candidate_refs(project, s1) == 0
    add_tr_candidate(project, transition_id="tr_001", slot=0,
                     pool_segment_id=s1, source="generated")
    assert count_tr_candidate_refs(project, s1) == 1
    add_tr_candidate(project, transition_id="tr_002", slot=0,
                     pool_segment_id=s1, source="cross-tr-copy")
    assert count_tr_candidate_refs(project, s1) == 2


def test_remove_tr_candidate(project):
    s1 = add_pool_segment(project, kind="generated", created_by="a",
                          pool_path="pool/segments/c1.mp4")
    add_tr_candidate(project, transition_id="tr_001", slot=0,
                     pool_segment_id=s1, source="generated")
    remove_tr_candidate(project, "tr_001", 0, s1)
    assert get_tr_candidates(project, "tr_001") == []


# ── GC ─────────────────────────────────────────────────────────────

def test_find_gc_candidates(project):
    # Generated, unreferenced — should be GC'd
    s_orphan = add_pool_segment(project, kind="generated", created_by="a",
                                 pool_path="pool/segments/orphan.mp4")
    # Generated, referenced — should NOT be GC'd
    s_used = add_pool_segment(project, kind="generated", created_by="a",
                               pool_path="pool/segments/used.mp4")
    add_tr_candidate(project, transition_id="tr_001", slot=0,
                     pool_segment_id=s_used, source="generated")
    # Imported, unreferenced — should NOT be GC'd (user asset preserved)
    s_imported = add_pool_segment(project, kind="imported", created_by="a",
                                   pool_path="pool/segments/import_1.mp4",
                                   original_filename="stuff.mov")

    gc = find_gc_candidates(project)
    gc_ids = {s["id"] for s in gc}
    assert gc_ids == {s_orphan}
    assert s_used not in gc_ids
    assert s_imported not in gc_ids


def test_delete_pool_segment_also_removes_tags(project):
    s1 = add_pool_segment(project, kind="generated", created_by="a",
                          pool_path="pool/segments/c1.mp4")
    add_pool_segment_tag(project, s1, "keeper", tagged_by="alice")
    add_pool_segment_tag(project, s1, "sunset", tagged_by="alice")

    delete_pool_segment(project, s1)
    assert get_pool_segment(project, s1) is None
    assert get_pool_segment_tags(project, s1) == []


# ── Cross-branch merge behavior (simulation) ──────────────────────

def test_multi_slot_selection_merges(project):
    """Updating slot_1 must not overwrite slot_0's selection and vice versa.

    Simulates what select-transitions does when merging slot updates into the
    transitions.selected array.
    """
    import json
    from scenecraft.db import get_db, add_transition, update_transition, get_transition

    s0 = add_pool_segment(project, kind="generated", created_by="a",
                          pool_path="pool/segments/c0.mp4")
    s1 = add_pool_segment(project, kind="generated", created_by="a",
                          pool_path="pool/segments/c1.mp4")

    add_transition(project, {
        "id": "tr_multi", "from": "kf_a", "to": "kf_b",
        "duration_seconds": 4.0, "slots": 2, "action": "", "use_global_prompt": 1,
        "selected": [None, None], "remap": {"method": "linear", "target_duration": 4.0},
    })

    # Apply slot_0 first
    tr = get_transition(project, "tr_multi") or {}
    current = tr.get("selected") if isinstance(tr.get("selected"), list) else [None, None]
    current[0] = s0
    update_transition(project, "tr_multi", selected=current)

    # Apply slot_1 — must preserve slot_0
    tr = get_transition(project, "tr_multi")
    current = tr["selected"] if isinstance(tr["selected"], list) else [None, None]
    while len(current) < 2:
        current.append(None)
    current[1] = s1
    update_transition(project, "tr_multi", selected=current)

    # Verify both slots retained
    tr = get_transition(project, "tr_multi")
    assert tr["selected"] == [s0, s1]


def test_append_only_generation_is_merge_safe(project):
    """Two users on two branches generate candidates for the same tr.

    Simulates by inserting rows with different UUIDs — both should coexist
    with no PK conflict (this is the core payoff of the pool model).
    """
    # Alice generates
    alice_seg = add_pool_segment(project, kind="generated", created_by="alice",
                                  pool_path="pool/segments/cand_alice.mp4")
    add_tr_candidate(project, transition_id="tr_001", slot=0,
                     pool_segment_id=alice_seg, source="generated")

    # Bob generates (simulated as a separate insert with a different UUID)
    bob_seg = add_pool_segment(project, kind="generated", created_by="bob",
                                pool_path="pool/segments/cand_bob.mp4")
    add_tr_candidate(project, transition_id="tr_001", slot=0,
                     pool_segment_id=bob_seg, source="generated")

    # Both candidates coexist in the junction — no rank collision
    cands = get_tr_candidates(project, "tr_001", slot=0)
    assert len(cands) == 2
    created_by = {c["id"]: c["createdBy"] for c in cands}
    assert created_by[alice_seg] == "alice"
    assert created_by[bob_seg] == "bob"
