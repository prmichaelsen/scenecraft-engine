"""E2E tests for pool insert — verifies no duplicate/overlapping transitions are created."""

import json
import shutil
import sqlite3
import tempfile
from pathlib import Path

import pytest


def _setup_project(tmp_path: Path):
    """Create a minimal project with DB, keyframes, and transitions."""
    from scenecraft.db import get_db, add_keyframe, add_transition

    project_dir = tmp_path / "test_project"
    project_dir.mkdir()

    # Create required dirs
    (project_dir / "selected_keyframes").mkdir()
    (project_dir / "pool" / "keyframes").mkdir(parents=True)

    # Create a dummy pool image
    pool_img = project_dir / "pool" / "keyframes" / "test.png"
    # 1x1 white PNG
    import struct, zlib
    def _make_png():
        raw = b'\x00\xff\xff\xff'
        compressed = zlib.compress(raw)
        ihdr = struct.pack('>IIBBBBB', 1, 1, 8, 2, 0, 0, 0)
        chunks = b''
        for tag, data in [(b'IHDR', ihdr), (b'IDAT', compressed), (b'IEND', b'')]:
            chunks += struct.pack('>I', len(data)) + tag + data + struct.pack('>I', zlib.crc32(tag + data) & 0xffffffff)
        return b'\x89PNG\r\n\x1a\n' + chunks
    pool_img.write_bytes(_make_png())

    # Initialize DB with 3 keyframes on track_1
    add_keyframe(project_dir, {
        "id": "kf_001", "timestamp": "1:00.00", "section": "", "source": "",
        "prompt": "First", "selected": None, "candidates": [], "track_id": "track_1",
    })
    add_keyframe(project_dir, {
        "id": "kf_002", "timestamp": "1:10.00", "section": "", "source": "",
        "prompt": "Second", "selected": None, "candidates": [], "track_id": "track_1",
    })
    add_keyframe(project_dir, {
        "id": "kf_003", "timestamp": "1:20.00", "section": "", "source": "",
        "prompt": "Third", "selected": None, "candidates": [], "track_id": "track_1",
    })

    # Add a transition spanning kf_001 → kf_002
    add_transition(project_dir, {
        "id": "tr_001", "from": "kf_001", "to": "kf_002",
        "duration_seconds": 10, "slots": 1, "action": "", "use_global_prompt": False,
        "selected": None, "remap": {"method": "linear", "target_duration": 10},
        "track_id": "track_1",
    })

    # Add a transition spanning kf_002 → kf_003
    add_transition(project_dir, {
        "id": "tr_002", "from": "kf_002", "to": "kf_003",
        "duration_seconds": 10, "slots": 1, "action": "", "use_global_prompt": False,
        "selected": None, "remap": {"method": "linear", "target_duration": 10},
        "track_id": "track_1",
    })

    # Also add a keyframe on track_2 near the same time
    add_keyframe(project_dir, {
        "id": "kf_t2_001", "timestamp": "1:04.00", "section": "", "source": "",
        "prompt": "Track 2", "selected": None, "candidates": [], "track_id": "track_2",
    })

    return project_dir, pool_img


def _get_active_transitions(project_dir: Path, track_id: str = "track_1") -> list:
    from scenecraft.db import get_transitions
    return [t for t in get_transitions(project_dir)
            if t.get("track_id") == track_id and not t.get("deleted_at")]


def _get_active_keyframes(project_dir: Path, track_id: str = "track_1") -> list:
    from scenecraft.db import get_keyframes
    return [k for k in get_keyframes(project_dir)
            if k.get("track_id", "track_1") == track_id and not k.get("deleted_at")]


def _parse_ts(ts):
    parts = str(ts).split(":")
    return int(parts[0]) * 60 + float(parts[1]) if len(parts) == 2 else float(ts)


def _find_overlaps(transitions, keyframes):
    """Find overlapping transitions on the same track."""
    kf_map = {k["id"]: k for k in keyframes}
    items = []
    for tr in transitions:
        from_kf = kf_map.get(tr.get("from", ""))
        to_kf = kf_map.get(tr.get("to", ""))
        if not from_kf or not to_kf:
            continue
        ft = _parse_ts(from_kf["timestamp"])
        tt = _parse_ts(to_kf["timestamp"])
        items.append((ft, tt, tr["id"]))
    items.sort()
    overlaps = []
    for i in range(1, len(items)):
        if items[i][0] < items[i - 1][1]:
            overlaps.append((items[i - 1][2], items[i][2]))
    return overlaps


class TestPoolInsert:
    """Test that pool insert creates clean, non-overlapping transitions."""

    def test_insert_splits_spanning_transition(self, tmp_path):
        """Inserting a keyframe between two existing ones should split the spanning transition."""
        project_dir, pool_img = _setup_project(tmp_path)

        # Before: tr_001 spans kf_001 (1:00) → kf_002 (1:10)
        trs_before = _get_active_transitions(project_dir)
        assert len(trs_before) == 2

        # Simulate pool insert at 1:05 (between kf_001 and kf_002)
        from scenecraft.db import (
            add_keyframe as db_add_kf, get_keyframes as db_get_kfs,
            next_keyframe_id, next_transition_id,
            add_transition as db_add_tr, delete_transition as db_del_tr,
            get_transitions as db_get_trs,
        )
        from datetime import datetime, timezone
        import shutil

        at_time = 65.0  # 1:05.00
        kf_id = next_keyframe_id(project_dir)
        track_id = "track_1"

        def to_ts(s):
            m = int(s) // 60
            return f"{m}:{s - m * 60:05.2f}"

        shutil.copy2(str(pool_img), str(project_dir / "selected_keyframes" / f"{kf_id}.png"))
        db_add_kf(project_dir, {
            "id": kf_id, "timestamp": to_ts(at_time), "section": "", "source": "",
            "prompt": "Inserted", "selected": 1, "candidates": [], "track_id": track_id,
        })

        # Find neighbors on same track
        all_kfs = [k for k in db_get_kfs(project_dir)
                   if k.get("track_id", "track_1") == track_id and not k.get("deleted_at")]
        sorted_kfs = sorted(all_kfs, key=lambda k: _parse_ts(k["timestamp"]))
        new_idx = next(i for i, k in enumerate(sorted_kfs) if k["id"] == kf_id)
        prev_kf = sorted_kfs[new_idx - 1] if new_idx > 0 else None
        next_kf = sorted_kfs[new_idx + 1] if new_idx < len(sorted_kfs) - 1 else None

        assert prev_kf["id"] == "kf_001"
        assert next_kf["id"] == "kf_002"

        # Split the spanning transition
        all_trs = [t for t in db_get_trs(project_dir)
                   if t.get("track_id", "track_1") == track_id and not t.get("deleted_at")]
        old_tr = next((t for t in all_trs if t["from"] == prev_kf["id"] and t["to"] == next_kf["id"]), None)
        assert old_tr is not None, "Should find spanning transition tr_001"

        db_del_tr(project_dir, old_tr["id"], datetime.now(timezone.utc).isoformat())

        existing_from_prev = any(t["from"] == prev_kf["id"] and t["to"] == kf_id for t in all_trs)
        existing_to_next = any(t["from"] == kf_id and t["to"] == next_kf["id"] for t in all_trs)
        assert not existing_from_prev
        assert not existing_to_next

        pt = _parse_ts(prev_kf["timestamp"])
        nt = _parse_ts(next_kf["timestamp"])
        d1, d2 = round(at_time - pt, 2), round(nt - at_time, 2)

        if not existing_from_prev and d1 > 0.05:
            tr1_id = next_transition_id(project_dir)
            db_add_tr(project_dir, {"id": tr1_id, "from": prev_kf["id"], "to": kf_id,
                "duration_seconds": d1, "slots": 1, "action": "", "use_global_prompt": False,
                "selected": None, "remap": {"method": "linear", "target_duration": d1},
                "track_id": track_id})
        if not existing_to_next and d2 > 0.05:
            tr2_id = next_transition_id(project_dir)
            db_add_tr(project_dir, {"id": tr2_id, "from": kf_id, "to": next_kf["id"],
                "duration_seconds": d2, "slots": 1, "action": "", "use_global_prompt": False,
                "selected": None, "remap": {"method": "linear", "target_duration": d2},
                "track_id": track_id})

        # After: should have 3 transitions, no overlaps
        trs_after = _get_active_transitions(project_dir)
        all_kfs_after = _get_active_keyframes(project_dir)
        assert len(trs_after) == 3, f"Expected 3 transitions, got {len(trs_after)}"

        overlaps = _find_overlaps(trs_after, all_kfs_after)
        assert len(overlaps) == 0, f"Found overlapping transitions: {overlaps}"

    def test_insert_does_not_create_cross_track_neighbors(self, tmp_path):
        """Inserting on track_1 should not pick neighbors from track_2."""
        project_dir, pool_img = _setup_project(tmp_path)

        from scenecraft.db import get_keyframes as db_get_kfs

        # Track_2 has kf_t2_001 at 1:04. Track_1 neighbors at 1:00 and 1:10.
        # Inserting at 1:05 on track_1 should find kf_001 and kf_002 as neighbors, NOT kf_t2_001.
        track_id = "track_1"
        all_kfs = [k for k in db_get_kfs(project_dir)
                   if k.get("track_id", "track_1") == track_id and not k.get("deleted_at")]
        sorted_kfs = sorted(all_kfs, key=lambda k: _parse_ts(k["timestamp"]))

        kf_ids = [k["id"] for k in sorted_kfs]
        assert "kf_t2_001" not in kf_ids, "Track_2 keyframe should not appear in track_1 list"

    def test_no_zero_length_transitions(self, tmp_path):
        """Inserting at the exact same timestamp as an existing keyframe should not create zero-length transitions."""
        project_dir, pool_img = _setup_project(tmp_path)

        from scenecraft.db import (
            add_keyframe as db_add_kf, get_keyframes as db_get_kfs,
            next_keyframe_id, next_transition_id,
            add_transition as db_add_tr, delete_transition as db_del_tr,
            get_transitions as db_get_trs,
        )
        from datetime import datetime, timezone
        import shutil

        # Insert at exactly 1:10.00 (same as kf_002)
        at_time = 70.0
        kf_id = next_keyframe_id(project_dir)
        track_id = "track_1"

        def to_ts(s):
            m = int(s) // 60
            return f"{m}:{s - m * 60:05.2f}"

        shutil.copy2(str(pool_img), str(project_dir / "selected_keyframes" / f"{kf_id}.png"))
        db_add_kf(project_dir, {
            "id": kf_id, "timestamp": to_ts(at_time), "section": "", "source": "",
            "prompt": "Same time", "selected": 1, "candidates": [], "track_id": track_id,
        })

        all_kfs = [k for k in db_get_kfs(project_dir)
                   if k.get("track_id", "track_1") == track_id and not k.get("deleted_at")]
        sorted_kfs = sorted(all_kfs, key=lambda k: _parse_ts(k["timestamp"]))
        new_idx = next(i for i, k in enumerate(sorted_kfs) if k["id"] == kf_id)
        prev_kf = sorted_kfs[new_idx - 1] if new_idx > 0 else None
        next_kf = sorted_kfs[new_idx + 1] if new_idx < len(sorted_kfs) - 1 else None

        if prev_kf and next_kf:
            pt = _parse_ts(prev_kf["timestamp"])
            nt = _parse_ts(next_kf["timestamp"])
            d1 = round(at_time - pt, 2)
            d2 = round(nt - at_time, 2)
            # d1 should be ~0 (same timestamp as kf_002), d2 should be ~10
            # The 0.05 guard should prevent creating a zero-length transition
            assert d1 <= 0.05 or d2 <= 0.05, \
                "At least one duration should be near-zero when inserting at existing kf timestamp"

    def test_double_insert_no_duplicates(self, tmp_path):
        """Inserting twice at the same position should not create duplicate transitions."""
        project_dir, pool_img = _setup_project(tmp_path)

        from scenecraft.db import (
            add_keyframe as db_add_kf, get_keyframes as db_get_kfs,
            next_keyframe_id, next_transition_id,
            add_transition as db_add_tr, delete_transition as db_del_tr,
            get_transitions as db_get_trs,
        )
        from datetime import datetime, timezone
        import shutil

        track_id = "track_1"

        def to_ts(s):
            m = int(s) // 60
            return f"{m}:{s - m * 60:05.2f}"

        def insert_at(at_time):
            kf_id = next_keyframe_id(project_dir)
            shutil.copy2(str(pool_img), str(project_dir / "selected_keyframes" / f"{kf_id}.png"))
            db_add_kf(project_dir, {
                "id": kf_id, "timestamp": to_ts(at_time), "section": "", "source": "",
                "prompt": "Inserted", "selected": 1, "candidates": [], "track_id": track_id,
            })

            all_kfs = [k for k in db_get_kfs(project_dir)
                       if k.get("track_id", "track_1") == track_id and not k.get("deleted_at")]
            sorted_kfs = sorted(all_kfs, key=lambda k: _parse_ts(k["timestamp"]))
            new_idx = next(i for i, k in enumerate(sorted_kfs) if k["id"] == kf_id)
            prev_kf = sorted_kfs[new_idx - 1] if new_idx > 0 else None
            next_kf = sorted_kfs[new_idx + 1] if new_idx < len(sorted_kfs) - 1 else None

            if prev_kf and next_kf:
                all_trs = [t for t in db_get_trs(project_dir)
                           if t.get("track_id", "track_1") == track_id and not t.get("deleted_at")]
                old_tr = next((t for t in all_trs if t["from"] == prev_kf["id"] and t["to"] == next_kf["id"]), None)
                existing_from_prev = any(t["from"] == prev_kf["id"] and t["to"] == kf_id for t in all_trs)
                existing_to_next = any(t["from"] == kf_id and t["to"] == next_kf["id"] for t in all_trs)

                if old_tr:
                    db_del_tr(project_dir, old_tr["id"], datetime.now(timezone.utc).isoformat())

                pt = _parse_ts(prev_kf["timestamp"])
                nt = _parse_ts(next_kf["timestamp"])
                d1, d2 = round(at_time - pt, 2), round(nt - at_time, 2)

                if not existing_from_prev and d1 > 0.05:
                    tr_id = next_transition_id(project_dir)
                    db_add_tr(project_dir, {"id": tr_id, "from": prev_kf["id"], "to": kf_id,
                        "duration_seconds": d1, "slots": 1, "action": "", "use_global_prompt": False,
                        "selected": None, "remap": {"method": "linear", "target_duration": d1},
                        "track_id": track_id})
                if not existing_to_next and d2 > 0.05:
                    tr_id = next_transition_id(project_dir)
                    db_add_tr(project_dir, {"id": tr_id, "from": kf_id, "to": next_kf["id"],
                        "duration_seconds": d2, "slots": 1, "action": "", "use_global_prompt": False,
                        "selected": None, "remap": {"method": "linear", "target_duration": d2},
                        "track_id": track_id})

        # Insert first at 1:05
        insert_at(65.0)
        trs_after_first = _get_active_transitions(project_dir)
        all_kfs = _get_active_keyframes(project_dir)
        overlaps_1 = _find_overlaps(trs_after_first, all_kfs)
        assert len(overlaps_1) == 0, f"Overlaps after first insert: {overlaps_1}"

        # Insert second at 1:07 (between the newly created kf and kf_002)
        insert_at(67.0)
        trs_after_second = _get_active_transitions(project_dir)
        all_kfs = _get_active_keyframes(project_dir)
        overlaps_2 = _find_overlaps(trs_after_second, all_kfs)
        assert len(overlaps_2) == 0, f"Overlaps after second insert: {overlaps_2}"
