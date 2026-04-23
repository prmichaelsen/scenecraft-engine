"""E2E tests for POST /api/projects/:name/move-transitions (Tasks 93 + 94).

Task 93 scope (same-track):
- mode="copy" -> 501
- new_from_time < 0 -> 400
- single tr timeDelta=+3 -> from/to shifted +3s
- batch of 2 trs -> both shifted; undo reverts together

Task 94 scope (cross-track + kf ownership + auto-create tracks):
- cross-track single clip (shared boundary kfs) -> duplicated on target, empty bridge on source
- cross-track single clip (orphan boundary kfs) -> migrated on target, source kfs moved
- multi-track source selection with uniform trackDelta -> per-clip target track preserved
- overflow with autoCreateTracks=True -> new track created, clips land on it
- overflow with autoCreateTracks=False -> 400 OUT_OF_RANGE_TRACK
- interior kf between two dragged clips -> migrates in a single row update
- empty-tr bridge has duration_seconds equal to span length

Downstream tasks (95/96) extend this endpoint with overlap resolution and copy mode.
"""

import json
import shutil
import tempfile
import threading
from http.server import HTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from scenecraft.api_server import make_handler
from scenecraft.db import (
    add_keyframe, add_transition, add_track, close_db, get_db,
    get_keyframe, get_keyframes, get_transition, get_transitions,
    get_tracks, set_meta, _migrated_dbs,
)


@pytest.fixture
def project_env():
    work_dir = Path(tempfile.mkdtemp())
    project_name = "test_project"
    project_dir = work_dir / project_name
    project_dir.mkdir()

    set_meta(project_dir, "title", "Test Project")
    set_meta(project_dir, "fps", 24)

    Handler = make_handler(work_dir)
    server = HTTPServer(("127.0.0.1", 0), Handler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    yield {
        "work_dir": work_dir,
        "project_dir": project_dir,
        "project_name": project_name,
        "base_url": f"http://127.0.0.1:{port}",
    }

    server.shutdown()
    close_db(project_dir)
    db_path = str(project_dir / "project.db")
    _migrated_dbs.discard(db_path)
    shutil.rmtree(work_dir)


def api(env, method, path, body=None):
    url = f"{env['base_url']}{path}"
    data = json.dumps(body).encode() if body is not None else None
    req = Request(url, data=data, method=method)
    if data:
        req.add_header("Content-Type", "application/json")
    try:
        resp = urlopen(req)
        return resp.getcode(), json.loads(resp.read())
    except HTTPError as e:
        body_text = e.read().decode()
        try:
            return e.code, json.loads(body_text)
        except Exception:
            return e.code, {"error": body_text}


def parse_ts(ts):
    parts = str(ts).split(":")
    if len(parts) == 2:
        return int(parts[0]) * 60 + float(parts[1])
    return float(ts)


def _ensure_track(project_dir, track_id: str, z_order: int, name: str | None = None):
    """Idempotently ensure a track exists at a given z_order."""
    tracks = {t["id"] for t in get_tracks(project_dir)}
    if track_id in tracks:
        return
    add_track(project_dir, {
        "id": track_id,
        "name": name or track_id.replace("_", " ").title(),
        "z_order": z_order,
        "blend_mode": "normal",
        "base_opacity": 1.0,
        "enabled": True,
    })


def _seed_three_clip_track(project_dir):
    """Seed a single track with 3 consecutive clips sharing boundary kfs.

    Timeline:  kf_001(0:10) - tr_001 - kf_002(0:15) - tr_002 - kf_003(0:20) - tr_003 - kf_004(0:25)
    """
    for kf_id, ts in [
        ("kf_001", "0:10.00"),
        ("kf_002", "0:15.00"),
        ("kf_003", "0:20.00"),
        ("kf_004", "0:25.00"),
    ]:
        add_keyframe(project_dir, {
            "id": kf_id, "timestamp": ts, "section": "", "source": "",
            "prompt": "", "selected": None, "candidates": [], "track_id": "track_1",
        })
    add_transition(project_dir, {
        "id": "tr_001", "from": "kf_001", "to": "kf_002",
        "duration_seconds": 5, "slots": 1, "action": "",
        "use_global_prompt": False, "selected": None,
        "remap": {"method": "linear", "target_duration": 5}, "track_id": "track_1",
    })
    add_transition(project_dir, {
        "id": "tr_002", "from": "kf_002", "to": "kf_003",
        "duration_seconds": 5, "slots": 1, "action": "",
        "use_global_prompt": False, "selected": None,
        "remap": {"method": "linear", "target_duration": 5}, "track_id": "track_1",
    })
    add_transition(project_dir, {
        "id": "tr_003", "from": "kf_003", "to": "kf_004",
        "duration_seconds": 5, "slots": 1, "action": "",
        "use_global_prompt": False, "selected": None,
        "remap": {"method": "linear", "target_duration": 5}, "track_id": "track_1",
    })


class TestMoveTransitionsValidation:
    def test_copy_mode_accepted(self, project_env):
        """Task 96: copy mode is now implemented. A valid copy request returns 200."""
        _seed_three_clip_track(project_env["project_dir"])
        status, body = api(project_env, "POST",
            f"/api/projects/{project_env['project_name']}/move-transitions",
            {"mode": "copy", "trackDelta": 0, "timeDeltaSeconds": 100.0, "transitionIds": ["tr_001"]})
        assert status == 200, f"expected 200, got {status}: {body}"

    def test_empty_transition_ids_rejected(self, project_env):
        _seed_three_clip_track(project_env["project_dir"])
        status, body = api(project_env, "POST",
            f"/api/projects/{project_env['project_name']}/move-transitions",
            {"mode": "move", "trackDelta": 0, "timeDeltaSeconds": 1.0, "transitionIds": []})
        assert status == 400

    def test_negative_time_rejected(self, project_env):
        """tr_001 starts at 0:10; timeDelta=-20 would push it to -10 -> 400."""
        _seed_three_clip_track(project_env["project_dir"])
        status, body = api(project_env, "POST",
            f"/api/projects/{project_env['project_name']}/move-transitions",
            {"mode": "move", "trackDelta": 0, "timeDeltaSeconds": -20.0, "transitionIds": ["tr_001"]})
        assert status == 400, f"expected 400, got {status}: {body}"


class TestMoveTransitionsSingle:
    def test_single_tr_time_delta_plus_three(self, project_env):
        """tr_002 is interior (kf_002 and kf_003 both shared with neighbors).

        Both boundary kfs are shared, so new kfs get created and tr_002's from/to
        are repointed. Neighbor trs' kf_002/kf_003 references are preserved.
        """
        project_dir = project_env["project_dir"]
        _seed_three_clip_track(project_dir)

        status, body = api(project_env, "POST",
            f"/api/projects/{project_env['project_name']}/move-transitions",
            {"mode": "move", "trackDelta": 0, "timeDeltaSeconds": 3.0, "transitionIds": ["tr_002"]})
        assert status == 200, f"got {status}: {body}"
        assert body["success"] is True
        assert body["movedTransitionIds"] == ["tr_002"]
        assert body["createdTrackIds"] == []
        assert body["consumedTransitionIds"] == []
        # tr_002 moves [15,20] -> [18,23] onto a back-to-back track.
        # Source cleanup inserts an empty bridge on track_1 at [15,20] (from kf_002→kf_003)
        # which overlaps the drop [18,23]; bridge gets Case B trim (to=new_from_kf at 18).
        # tr_003 at [20,25] straddles new_to=23 -> Case C trim.
        assert "tr_003" in body["splitTransitionIds"]
        assert len(body["splitTransitionIds"]) == 2  # tr_003 + the source bridge

        # Verify tr_002's new from/to timestamps are shifted by +3.
        tr_002 = get_transition(project_dir, "tr_002")
        from_kf = get_keyframe(project_dir, tr_002["from"])
        to_kf = get_keyframe(project_dir, tr_002["to"])
        assert abs(parse_ts(from_kf["timestamp"]) - 18.0) < 0.01
        assert abs(parse_ts(to_kf["timestamp"]) - 23.0) < 0.01
        # Duration preserved on tr_002.
        assert abs(tr_002["duration_seconds"] - 5.0) < 0.01

        # kf_002 still bounds tr_001 on source; tr_001 untouched.
        tr_001 = get_transition(project_dir, "tr_001")
        assert tr_001["to"] == "kf_002"
        kf_002 = get_keyframe(project_dir, "kf_002")
        assert abs(parse_ts(kf_002["timestamp"]) - 15.0) < 0.01

        # tr_003's from_kf was trimmed to tr_002's new_to_kf (at 23.0);
        # duration shrank from 5.0 to 2.0 (25 - 23).
        tr_003 = get_transition(project_dir, "tr_003")
        tr_003_from_kf = get_keyframe(project_dir, tr_003["from"])
        assert abs(parse_ts(tr_003_from_kf["timestamp"]) - 23.0) < 0.01
        assert abs(tr_003["duration_seconds"] - 2.0) < 0.01
        assert tr_003["to"] == "kf_004"

    def test_single_tr_unshared_kfs_update_in_place(self, project_env):
        """A tr whose boundary kfs are NOT shared: kfs update in place (no new kf)."""
        project_dir = project_env["project_dir"]
        # Seed a lone clip on its own track so both boundary kfs are unshared.
        add_keyframe(project_dir, {
            "id": "kf_a", "timestamp": "0:05.00", "section": "", "source": "",
            "prompt": "", "selected": None, "candidates": [], "track_id": "track_1",
        })
        add_keyframe(project_dir, {
            "id": "kf_b", "timestamp": "0:10.00", "section": "", "source": "",
            "prompt": "", "selected": None, "candidates": [], "track_id": "track_1",
        })
        add_transition(project_dir, {
            "id": "tr_solo", "from": "kf_a", "to": "kf_b",
            "duration_seconds": 5, "slots": 1, "action": "",
            "use_global_prompt": False, "selected": None,
            "remap": {"method": "linear", "target_duration": 5}, "track_id": "track_1",
        })

        kfs_before = {k["id"] for k in get_keyframes(project_dir)}

        status, body = api(project_env, "POST",
            f"/api/projects/{project_env['project_name']}/move-transitions",
            {"mode": "move", "trackDelta": 0, "timeDeltaSeconds": 2.0, "transitionIds": ["tr_solo"]})
        assert status == 200

        # No new kfs should have been created.
        kfs_after = {k["id"] for k in get_keyframes(project_dir)}
        assert kfs_before == kfs_after, "unshared kfs should have been updated in place, not duplicated"

        kf_a = get_keyframe(project_dir, "kf_a")
        kf_b = get_keyframe(project_dir, "kf_b")
        assert abs(parse_ts(kf_a["timestamp"]) - 7.0) < 0.01
        assert abs(parse_ts(kf_b["timestamp"]) - 12.0) < 0.01


class TestMoveTransitionsBatch:
    def test_batch_of_two_trs_shifted_together(self, project_env):
        """Batch-moving tr_001 and tr_002 together: kf_002 is internal to the moved set
        (only references tr_001 and tr_002, both being moved), so it migrates in place
        without duplication. kf_001 and kf_003 are boundaries with non-moved neighbors
        (kf_001 is only used by tr_001 -> moved with it; kf_003 is shared with tr_003
        -> duplicated).
        """
        project_dir = project_env["project_dir"]
        _seed_three_clip_track(project_dir)

        status, body = api(project_env, "POST",
            f"/api/projects/{project_env['project_name']}/move-transitions",
            {"mode": "move", "trackDelta": 0, "timeDeltaSeconds": 1.0,
             "transitionIds": ["tr_001", "tr_002"]})
        assert status == 200, f"got {status}: {body}"

        # tr_001 boundaries: kf_001 (unshared), kf_002 (shared only with tr_002 which is also moved,
        # so from the "other non-moved tr" perspective, it's unshared too).
        # tr_002 boundaries: kf_002 (shared with tr_001 in moved set -> unshared from the check),
        # kf_003 (shared with tr_003 NOT in moved set -> shared, duplicated).
        tr_001 = get_transition(project_dir, "tr_001")
        tr_002 = get_transition(project_dir, "tr_002")
        tr_003 = get_transition(project_dir, "tr_003")

        tr_001_from = get_keyframe(project_dir, tr_001["from"])
        tr_001_to = get_keyframe(project_dir, tr_001["to"])
        tr_002_from = get_keyframe(project_dir, tr_002["from"])
        tr_002_to = get_keyframe(project_dir, tr_002["to"])

        assert abs(parse_ts(tr_001_from["timestamp"]) - 11.0) < 0.01
        assert abs(parse_ts(tr_001_to["timestamp"]) - 16.0) < 0.01
        assert abs(parse_ts(tr_002_from["timestamp"]) - 16.0) < 0.01
        assert abs(parse_ts(tr_002_to["timestamp"]) - 21.0) < 0.01

        # tr_003 straddled by tr_002's new_to=21 -> Case C trim (Task 95).
        # Its from_kf is now tr_002's new_to_kf (at 21); duration shrank 5->4.
        tr_003 = get_transition(project_dir, "tr_003")
        tr_003_from_kf = get_keyframe(project_dir, tr_003["from"])
        assert abs(parse_ts(tr_003_from_kf["timestamp"]) - 21.0) < 0.01
        assert abs(tr_003["duration_seconds"] - 4.0) < 0.01
        assert tr_003["to"] == "kf_004"
        assert "tr_003" in body["splitTransitionIds"]

    def test_batch_undo_reverts_together(self, project_env):
        """Undo after a batch move should restore all trs to their original positions
        in a single undo entry.
        """
        project_dir = project_env["project_dir"]
        _seed_three_clip_track(project_dir)

        # Capture original tr pointers.
        tr_001_before = get_transition(project_dir, "tr_001")
        tr_002_before = get_transition(project_dir, "tr_002")
        from_kf_001_before = tr_001_before["from"]
        to_kf_001_before = tr_001_before["to"]
        from_kf_002_before = tr_002_before["from"]
        to_kf_002_before = tr_002_before["to"]
        kf_001_time_before = parse_ts(get_keyframe(project_dir, from_kf_001_before)["timestamp"])
        kf_002_time_before = parse_ts(get_keyframe(project_dir, from_kf_002_before)["timestamp"])

        # Move.
        status, _ = api(project_env, "POST",
            f"/api/projects/{project_env['project_name']}/move-transitions",
            {"mode": "move", "trackDelta": 0, "timeDeltaSeconds": 2.0,
             "transitionIds": ["tr_001", "tr_002"]})
        assert status == 200

        # Undo.
        status, body = api(project_env, "POST",
            f"/api/projects/{project_env['project_name']}/undo", {})
        assert status == 200, f"undo failed: {status} {body}"

        # Verify tr pointers + kf timestamps are restored.
        tr_001_after = get_transition(project_dir, "tr_001")
        tr_002_after = get_transition(project_dir, "tr_002")
        assert tr_001_after["from"] == from_kf_001_before
        assert tr_001_after["to"] == to_kf_001_before
        assert tr_002_after["from"] == from_kf_002_before
        assert tr_002_after["to"] == to_kf_002_before

        kf_001_time_after = parse_ts(get_keyframe(project_dir, from_kf_001_before)["timestamp"])
        kf_002_time_after = parse_ts(get_keyframe(project_dir, from_kf_002_before)["timestamp"])
        assert abs(kf_001_time_after - kf_001_time_before) < 0.01
        assert abs(kf_002_time_after - kf_002_time_before) < 0.01


# ── Task 94: cross-track moves ─────────────────────────────────────────


class TestMoveTransitionsCrossTrack:
    def test_cross_track_shared_boundary_kfs_duplicate_and_bridge(self, project_env):
        """Move tr_002 (middle of 3-clip track) to track_2 with trackDelta=+1.

        - kf_002 and kf_003 are boundary kfs (shared with tr_001 / tr_003 on source).
        - Source keeps kf_002 / kf_003 (non-moved neighbors still reference them).
        - Target gets a pair of fresh duplicated kfs on track_2.
        - An empty-tr bridge [kf_002 -> kf_003] is inserted on track_1 (source) so
          tr_001 and tr_003 stay joined by a bridge.
        """
        project_dir = project_env["project_dir"]
        _ensure_track(project_dir, "track_2", z_order=1, name="Track 2")
        _seed_three_clip_track(project_dir)

        trs_before = {t["id"] for t in get_transitions(project_dir)}
        kfs_before = {k["id"] for k in get_keyframes(project_dir)}

        status, body = api(project_env, "POST",
            f"/api/projects/{project_env['project_name']}/move-transitions",
            {"mode": "move", "trackDelta": 1, "timeDeltaSeconds": 0.0,
             "transitionIds": ["tr_002"]})
        assert status == 200, f"got {status}: {body}"
        assert body["createdTrackIds"] == []

        tr_002 = get_transition(project_dir, "tr_002")
        assert tr_002["track_id"] == "track_2"
        # New from/to kfs on track_2.
        new_from_kf = get_keyframe(project_dir, tr_002["from"])
        new_to_kf = get_keyframe(project_dir, tr_002["to"])
        assert new_from_kf["track_id"] == "track_2"
        assert new_to_kf["track_id"] == "track_2"
        assert new_from_kf["id"] not in kfs_before, "expected fresh from_kf on target"
        assert new_to_kf["id"] not in kfs_before, "expected fresh to_kf on target"
        # Timestamps unchanged (timeDelta=0).
        assert abs(parse_ts(new_from_kf["timestamp"]) - 15.0) < 0.01
        assert abs(parse_ts(new_to_kf["timestamp"]) - 20.0) < 0.01

        # Source kfs kf_002, kf_003 remain on track_1 with unchanged timestamps.
        src_kf_002 = get_keyframe(project_dir, "kf_002")
        src_kf_003 = get_keyframe(project_dir, "kf_003")
        assert src_kf_002["track_id"] == "track_1"
        assert src_kf_003["track_id"] == "track_1"
        assert abs(parse_ts(src_kf_002["timestamp"]) - 15.0) < 0.01
        assert abs(parse_ts(src_kf_003["timestamp"]) - 20.0) < 0.01

        # tr_001 / tr_003 still reference source kfs.
        tr_001 = get_transition(project_dir, "tr_001")
        tr_003 = get_transition(project_dir, "tr_003")
        assert tr_001["to"] == "kf_002"
        assert tr_003["from"] == "kf_003"

        # An empty bridge tr on track_1 from kf_002 to kf_003 should exist.
        bridge_trs = [
            t for t in get_transitions(project_dir)
            if t["id"] not in trs_before
            and t.get("track_id") == "track_1"
            and t.get("from") == "kf_002" and t.get("to") == "kf_003"
        ]
        assert len(bridge_trs) == 1, f"expected one bridge tr, got {len(bridge_trs)}"
        bridge = bridge_trs[0]
        # selected=[None] means "empty" — frontend flattens [None] to None.
        assert bridge["selected"] is None
        assert abs(bridge["duration_seconds"] - 5.0) < 0.01

    def test_cross_track_orphan_boundary_kfs_migrate(self, project_env):
        """Solo clip on track_1 cross-track moved to track_2: boundary kfs are
        "unshared" (no non-moved tr references them), so both are interior
        and migrate in place — no duplicate kfs, no bridge on source.
        """
        project_dir = project_env["project_dir"]
        _ensure_track(project_dir, "track_2", z_order=1, name="Track 2")
        # Seed a lone clip on track_1.
        add_keyframe(project_dir, {
            "id": "kf_a", "timestamp": "0:05.00", "section": "", "source": "",
            "prompt": "", "selected": None, "candidates": [], "track_id": "track_1",
        })
        add_keyframe(project_dir, {
            "id": "kf_b", "timestamp": "0:10.00", "section": "", "source": "",
            "prompt": "", "selected": None, "candidates": [], "track_id": "track_1",
        })
        add_transition(project_dir, {
            "id": "tr_solo", "from": "kf_a", "to": "kf_b",
            "duration_seconds": 5, "slots": 1, "action": "",
            "use_global_prompt": False, "selected": None,
            "remap": {"method": "linear", "target_duration": 5}, "track_id": "track_1",
        })

        kfs_before = {k["id"] for k in get_keyframes(project_dir)}
        trs_before = {t["id"] for t in get_transitions(project_dir)}

        status, body = api(project_env, "POST",
            f"/api/projects/{project_env['project_name']}/move-transitions",
            {"mode": "move", "trackDelta": 1, "timeDeltaSeconds": 0.0,
             "transitionIds": ["tr_solo"]})
        assert status == 200, f"got {status}: {body}"

        # No new kfs, no new trs (no bridge — no neighbors to bridge).
        kfs_after = {k["id"] for k in get_keyframes(project_dir) if not k.get("deleted_at")}
        trs_after = {t["id"] for t in get_transitions(project_dir)}
        assert kfs_after == kfs_before, f"expected no new kfs, got diff {kfs_after ^ kfs_before}"
        assert trs_after == trs_before, "expected no new trs (no bridge)"

        # kf_a and kf_b migrated to track_2.
        kf_a = get_keyframe(project_dir, "kf_a")
        kf_b = get_keyframe(project_dir, "kf_b")
        assert kf_a["track_id"] == "track_2"
        assert kf_b["track_id"] == "track_2"
        # Timestamps unchanged.
        assert abs(parse_ts(kf_a["timestamp"]) - 5.0) < 0.01
        assert abs(parse_ts(kf_b["timestamp"]) - 10.0) < 0.01

        # tr_solo lives on track_2 now.
        tr_solo = get_transition(project_dir, "tr_solo")
        assert tr_solo["track_id"] == "track_2"
        assert tr_solo["from"] == "kf_a"
        assert tr_solo["to"] == "kf_b"

    def test_interior_kf_between_two_dragged_clips_migrates_once(self, project_env):
        """Drag tr_001 + tr_002 together cross-track. kf_002 is interior to the
        moved set (only tr_001 and tr_002 reference it, both moved). kf_002
        should migrate in a single row update, ending up on track_2.

        kf_001 is referenced only by tr_001 (moved) -> interior -> migrates.
        kf_003 is shared with tr_003 (non-moved) -> boundary -> duplicated.
        """
        project_dir = project_env["project_dir"]
        _ensure_track(project_dir, "track_2", z_order=1, name="Track 2")
        _seed_three_clip_track(project_dir)

        status, body = api(project_env, "POST",
            f"/api/projects/{project_env['project_name']}/move-transitions",
            {"mode": "move", "trackDelta": 1, "timeDeltaSeconds": 2.0,
             "transitionIds": ["tr_001", "tr_002"]})
        assert status == 200, f"got {status}: {body}"

        # kf_001 (interior): migrated to track_2 with timestamp 12 (was 10 + 2).
        kf_001 = get_keyframe(project_dir, "kf_001")
        assert kf_001["track_id"] == "track_2"
        assert abs(parse_ts(kf_001["timestamp"]) - 12.0) < 0.01

        # kf_002 (interior): migrated to track_2 with timestamp 17 (was 15 + 2).
        kf_002 = get_keyframe(project_dir, "kf_002")
        assert kf_002["track_id"] == "track_2"
        assert abs(parse_ts(kf_002["timestamp"]) - 17.0) < 0.01

        # kf_003 (boundary with non-moved tr_003): source copy stays on track_1 at 20.
        src_kf_003 = get_keyframe(project_dir, "kf_003")
        assert src_kf_003["track_id"] == "track_1"
        assert abs(parse_ts(src_kf_003["timestamp"]) - 20.0) < 0.01

        # tr_002's to_kf is a NEW kf on track_2 at 22.
        tr_002 = get_transition(project_dir, "tr_002")
        assert tr_002["track_id"] == "track_2"
        new_to_kf = get_keyframe(project_dir, tr_002["to"])
        assert new_to_kf["id"] != "kf_003"
        assert new_to_kf["track_id"] == "track_2"
        assert abs(parse_ts(new_to_kf["timestamp"]) - 22.0) < 0.01

        # tr_001 and tr_002 both share kf_002 (interior, single row).
        tr_001 = get_transition(project_dir, "tr_001")
        assert tr_001["track_id"] == "track_2"
        assert tr_001["to"] == "kf_002"
        assert tr_002["from"] == "kf_002"

        # tr_003 untouched, still references kf_003 on track_1.
        tr_003 = get_transition(project_dir, "tr_003")
        assert tr_003["track_id"] == "track_1"
        assert tr_003["from"] == "kf_003"

    def test_multi_track_source_uniform_track_delta(self, project_env):
        """Select clips from T2 and T3 simultaneously with trackDelta=-1:
        T2 clips land on T1, T3 clips land on T2.
        """
        project_dir = project_env["project_dir"]
        _ensure_track(project_dir, "track_2", z_order=1, name="Track 2")
        _ensure_track(project_dir, "track_3", z_order=2, name="Track 3")

        # Solo clip on track_2.
        add_keyframe(project_dir, {
            "id": "kf_t2a", "timestamp": "0:05.00", "section": "", "source": "",
            "prompt": "", "selected": None, "candidates": [], "track_id": "track_2",
        })
        add_keyframe(project_dir, {
            "id": "kf_t2b", "timestamp": "0:08.00", "section": "", "source": "",
            "prompt": "", "selected": None, "candidates": [], "track_id": "track_2",
        })
        add_transition(project_dir, {
            "id": "tr_t2", "from": "kf_t2a", "to": "kf_t2b",
            "duration_seconds": 3, "slots": 1, "action": "",
            "use_global_prompt": False, "selected": None,
            "remap": {"method": "linear", "target_duration": 3}, "track_id": "track_2",
        })

        # Solo clip on track_3.
        add_keyframe(project_dir, {
            "id": "kf_t3a", "timestamp": "0:10.00", "section": "", "source": "",
            "prompt": "", "selected": None, "candidates": [], "track_id": "track_3",
        })
        add_keyframe(project_dir, {
            "id": "kf_t3b", "timestamp": "0:13.00", "section": "", "source": "",
            "prompt": "", "selected": None, "candidates": [], "track_id": "track_3",
        })
        add_transition(project_dir, {
            "id": "tr_t3", "from": "kf_t3a", "to": "kf_t3b",
            "duration_seconds": 3, "slots": 1, "action": "",
            "use_global_prompt": False, "selected": None,
            "remap": {"method": "linear", "target_duration": 3}, "track_id": "track_3",
        })

        status, body = api(project_env, "POST",
            f"/api/projects/{project_env['project_name']}/move-transitions",
            {"mode": "move", "trackDelta": -1, "timeDeltaSeconds": 0.0,
             "transitionIds": ["tr_t2", "tr_t3"]})
        assert status == 200, f"got {status}: {body}"

        tr_t2 = get_transition(project_dir, "tr_t2")
        tr_t3 = get_transition(project_dir, "tr_t3")
        assert tr_t2["track_id"] == "track_1"
        assert tr_t3["track_id"] == "track_2"

        # Both were orphan kfs -> migrated in place, now on new tracks.
        for kf_id, tgt in [("kf_t2a", "track_1"), ("kf_t2b", "track_1"),
                           ("kf_t3a", "track_2"), ("kf_t3b", "track_2")]:
            kf = get_keyframe(project_dir, kf_id)
            assert kf["track_id"] == tgt, f"{kf_id} should be on {tgt}, got {kf['track_id']}"

    def test_empty_bridge_duration_equals_span_length(self, project_env):
        """With tr_001 + tr_002 moved off track_1 together, the vacated span is
        [kf_001.ts=10, kf_003.ts=20] = 10.0s. But kf_001 is not a surviving left
        boundary (no non-moved tr uses it). Use a 4-clip track so the span has
        surviving left (kf_001) and right (kf_004) bounds.
        """
        project_dir = project_env["project_dir"]
        _ensure_track(project_dir, "track_2", z_order=1, name="Track 2")
        # Seed 4-clip chain: kf_001-tr_a-kf_002-tr_b-kf_003-tr_c-kf_004-tr_d-kf_005.
        for kf_id, ts in [
            ("kf_001", "0:05.00"),
            ("kf_002", "0:10.00"),
            ("kf_003", "0:15.00"),
            ("kf_004", "0:20.00"),
            ("kf_005", "0:25.00"),
        ]:
            add_keyframe(project_dir, {
                "id": kf_id, "timestamp": ts, "section": "", "source": "",
                "prompt": "", "selected": None, "candidates": [], "track_id": "track_1",
            })
        for tr_id, frm, to in [
            ("tr_a", "kf_001", "kf_002"),
            ("tr_b", "kf_002", "kf_003"),
            ("tr_c", "kf_003", "kf_004"),
            ("tr_d", "kf_004", "kf_005"),
        ]:
            add_transition(project_dir, {
                "id": tr_id, "from": frm, "to": to,
                "duration_seconds": 5, "slots": 1, "action": "",
                "use_global_prompt": False, "selected": None,
                "remap": {"method": "linear", "target_duration": 5}, "track_id": "track_1",
            })

        trs_before = {t["id"] for t in get_transitions(project_dir)}

        # Move tr_b and tr_c (middle two) cross-track. Vacated span on track_1 is
        # [kf_002.ts=10, kf_004.ts=20] -> expect bridge from kf_002 to kf_004 with
        # duration 10s. kf_003 is interior (only tr_b + tr_c reference it).
        status, body = api(project_env, "POST",
            f"/api/projects/{project_env['project_name']}/move-transitions",
            {"mode": "move", "trackDelta": 1, "timeDeltaSeconds": 0.0,
             "transitionIds": ["tr_b", "tr_c"]})
        assert status == 200, f"got {status}: {body}"

        # Find the new bridge tr.
        bridge_trs = [
            t for t in get_transitions(project_dir)
            if t["id"] not in trs_before
            and t.get("track_id") == "track_1"
            and t.get("from") == "kf_002" and t.get("to") == "kf_004"
        ]
        assert len(bridge_trs) == 1, f"expected one bridge, got {len(bridge_trs)}"
        assert abs(bridge_trs[0]["duration_seconds"] - 10.0) < 0.01

        # kf_003 was interior -> migrated to track_2.
        kf_003 = get_keyframe(project_dir, "kf_003")
        assert kf_003["track_id"] == "track_2"


class TestMoveTransitionsAutoCreateTracks:
    def test_overflow_auto_create_true_makes_new_track_above(self, project_env):
        """trackDelta=-1 when clip on track_1 (only track) => new track prepended
        above and clip lands on it.
        """
        project_dir = project_env["project_dir"]
        # Start with only track_1 (default).
        add_keyframe(project_dir, {
            "id": "kf_a", "timestamp": "0:05.00", "section": "", "source": "",
            "prompt": "", "selected": None, "candidates": [], "track_id": "track_1",
        })
        add_keyframe(project_dir, {
            "id": "kf_b", "timestamp": "0:10.00", "section": "", "source": "",
            "prompt": "", "selected": None, "candidates": [], "track_id": "track_1",
        })
        add_transition(project_dir, {
            "id": "tr_solo", "from": "kf_a", "to": "kf_b",
            "duration_seconds": 5, "slots": 1, "action": "",
            "use_global_prompt": False, "selected": None,
            "remap": {"method": "linear", "target_duration": 5}, "track_id": "track_1",
        })

        tracks_before = get_tracks(project_dir)
        assert len(tracks_before) == 1 and tracks_before[0]["id"] == "track_1"

        status, body = api(project_env, "POST",
            f"/api/projects/{project_env['project_name']}/move-transitions",
            {"mode": "move", "trackDelta": -1, "timeDeltaSeconds": 0.0,
             "transitionIds": ["tr_solo"], "autoCreateTracks": True})
        assert status == 200, f"got {status}: {body}"
        assert len(body["createdTrackIds"]) == 1
        new_track_id = body["createdTrackIds"][0]

        # tr_solo should live on the new track.
        tr_solo = get_transition(project_dir, "tr_solo")
        assert tr_solo["track_id"] == new_track_id

        # New track is at the top (z_order lower than track_1's 0 -> rendered below
        # in the compositor; per spec the new track has z_order extending the range).
        tracks_after = get_tracks(project_dir)
        assert len(tracks_after) == 2
        new_track = next(t for t in tracks_after if t["id"] == new_track_id)
        assert new_track["blend_mode"] == "normal"
        assert abs(new_track["base_opacity"] - 1.0) < 0.01
        assert new_track["enabled"] is True
        # Because we prepended above, the new track sorts first in get_tracks
        # (get_tracks ORDERs BY z_order).
        assert tracks_after[0]["id"] == new_track_id

    def test_overflow_auto_create_false_returns_400(self, project_env):
        """trackDelta=-1 on track_1-only project with autoCreateTracks=False => 400."""
        project_dir = project_env["project_dir"]
        add_keyframe(project_dir, {
            "id": "kf_a", "timestamp": "0:05.00", "section": "", "source": "",
            "prompt": "", "selected": None, "candidates": [], "track_id": "track_1",
        })
        add_keyframe(project_dir, {
            "id": "kf_b", "timestamp": "0:10.00", "section": "", "source": "",
            "prompt": "", "selected": None, "candidates": [], "track_id": "track_1",
        })
        add_transition(project_dir, {
            "id": "tr_solo", "from": "kf_a", "to": "kf_b",
            "duration_seconds": 5, "slots": 1, "action": "",
            "use_global_prompt": False, "selected": None,
            "remap": {"method": "linear", "target_duration": 5}, "track_id": "track_1",
        })

        status, body = api(project_env, "POST",
            f"/api/projects/{project_env['project_name']}/move-transitions",
            {"mode": "move", "trackDelta": -1, "timeDeltaSeconds": 0.0,
             "transitionIds": ["tr_solo"], "autoCreateTracks": False})
        assert status == 400, f"expected 400, got {status}: {body}"
        # Error code should surface OUT_OF_RANGE_TRACK in body.
        assert "OUT_OF_RANGE_TRACK" in json.dumps(body), f"expected OUT_OF_RANGE_TRACK in {body}"

    def test_overflow_below_auto_create_true(self, project_env):
        """trackDelta=+1 when clip on track_1 (only track) -> new track appended below."""
        project_dir = project_env["project_dir"]
        add_keyframe(project_dir, {
            "id": "kf_a", "timestamp": "0:05.00", "section": "", "source": "",
            "prompt": "", "selected": None, "candidates": [], "track_id": "track_1",
        })
        add_keyframe(project_dir, {
            "id": "kf_b", "timestamp": "0:10.00", "section": "", "source": "",
            "prompt": "", "selected": None, "candidates": [], "track_id": "track_1",
        })
        add_transition(project_dir, {
            "id": "tr_solo", "from": "kf_a", "to": "kf_b",
            "duration_seconds": 5, "slots": 1, "action": "",
            "use_global_prompt": False, "selected": None,
            "remap": {"method": "linear", "target_duration": 5}, "track_id": "track_1",
        })

        status, body = api(project_env, "POST",
            f"/api/projects/{project_env['project_name']}/move-transitions",
            {"mode": "move", "trackDelta": 1, "timeDeltaSeconds": 0.0,
             "transitionIds": ["tr_solo"], "autoCreateTracks": True})
        assert status == 200, f"got {status}: {body}"
        assert len(body["createdTrackIds"]) == 1
        new_track_id = body["createdTrackIds"][0]

        tr_solo = get_transition(project_dir, "tr_solo")
        assert tr_solo["track_id"] == new_track_id

        tracks_after = get_tracks(project_dir)
        assert len(tracks_after) == 2
        # Appended below => higher z_order => sorts last.
        assert tracks_after[-1]["id"] == new_track_id


def _seed_two_track_content(project_dir):
    """Seed two tracks. Track 1: a single content clip [5,15] (selected=0 -> content).
    Track 2: empty, for drop landing.

    Use for overlap-resolution tests where we need the dragged clip to land on
    a distinct track with a known content target.
    """
    add_track(project_dir, {"id": "track_2", "name": "Track 2", "z_order": 2,
                             "blend_mode": "normal", "base_opacity": 1.0, "enabled": 1})
    add_keyframe(project_dir, {"id": "kf_c1", "timestamp": "0:05.00", "section": "", "source": "",
                                "prompt": "", "selected": None, "candidates": [], "track_id": "track_2"})
    add_keyframe(project_dir, {"id": "kf_c2", "timestamp": "0:15.00", "section": "", "source": "",
                                "prompt": "", "selected": None, "candidates": [], "track_id": "track_2"})
    add_transition(project_dir, {
        "id": "tr_target", "from": "kf_c1", "to": "kf_c2",
        "duration_seconds": 10, "slots": 1, "action": "",
        "use_global_prompt": False, "selected": 0,
        "trim_in": 0.0, "trim_out": 10.0, "source_video_duration": 10.0,
        "remap": {"method": "linear", "target_duration": 10}, "track_id": "track_2",
    })


def _seed_single_clip_on_track_1(project_dir):
    """Seed one content clip tr_src [30, 35] on track_1. Matches _seed_two_track_content's
    track_2 target so we can drag tr_src onto track_2 at various positions.
    """
    add_keyframe(project_dir, {"id": "kf_s1", "timestamp": "0:30.00", "section": "", "source": "",
                                "prompt": "", "selected": None, "candidates": [], "track_id": "track_1"})
    add_keyframe(project_dir, {"id": "kf_s2", "timestamp": "0:35.00", "section": "", "source": "",
                                "prompt": "", "selected": None, "candidates": [], "track_id": "track_1"})
    add_transition(project_dir, {
        "id": "tr_src", "from": "kf_s1", "to": "kf_s2",
        "duration_seconds": 5, "slots": 1, "action": "",
        "use_global_prompt": False, "selected": 0,
        "trim_in": 0.0, "trim_out": 5.0, "source_video_duration": 5.0,
        "remap": {"method": "linear", "target_duration": 5}, "track_id": "track_1",
    })


class TestMoveTransitionsOverlapResolution:
    """Task 95 scope — target-track overlap resolution.

    Each test seeds tr_target on track_2 at [5,15] (content, trim [0,10]).
    Then drags tr_src (content, trim [0,5] at [30,35]) onto track_2 at various positions
    to exercise Case A (fully inside), B (straddles new_from), C (straddles new_to),
    and D (drop fully inside target).
    """

    def test_case_a_fully_consumes_target(self, project_env):
        """Drag tr_src (5s) onto [3,8] on track_2: target tr_target[5,15] is NOT fully inside.
        Use a different scenario: drag tr_src onto [5,15] (exactly target's span) -> Case A.
        """
        project_dir = project_env["project_dir"]
        _seed_two_track_content(project_dir)
        _seed_single_clip_on_track_1(project_dir)
        # tr_src [30,35] (5s) -> [5,10] would only partially overlap. Make tr_src 10s long.
        # Simpler: drag tr_src onto exactly tr_target's bounds [5,15]. Requires tr_src to be 10s.
        # Re-seed tr_src as a 10s clip at [30,40].
        from scenecraft.db import update_keyframe, update_transition
        update_keyframe(project_dir, "kf_s2", timestamp="0:40.00")
        update_transition(project_dir, "tr_src", duration_seconds=10, trim_out=10.0, source_video_duration=10.0)

        # timeDelta = -25 -> tr_src moves from [30,40] to [5,15], trackDelta = 1 -> track_2
        status, body = api(project_env, "POST",
            f"/api/projects/{project_env['project_name']}/move-transitions",
            {"mode": "move", "trackDelta": 1, "timeDeltaSeconds": -25.0, "transitionIds": ["tr_src"]})
        assert status == 200, f"got {status}: {body}"
        assert "tr_target" in body["consumedTransitionIds"]

        tr_target = get_transition(project_dir, "tr_target")
        assert tr_target["deleted_at"] is not None

    def test_case_b_straddles_new_from(self, project_env):
        """Drag tr_src onto track_2 starting at 10 (mid-target). tr_target [5,15] straddles new_from=10."""
        project_dir = project_env["project_dir"]
        _seed_two_track_content(project_dir)
        _seed_single_clip_on_track_1(project_dir)
        # tr_src [30,35] -> [10,15] on track_2. Drag delta: timeDelta=-20, trackDelta=1.
        # tr_target[5,15]: tf=5, tt=15, new_from=10, new_to=15. tf<new_from<tt AND tt<=new_to => Case B.
        status, body = api(project_env, "POST",
            f"/api/projects/{project_env['project_name']}/move-transitions",
            {"mode": "move", "trackDelta": 1, "timeDeltaSeconds": -20.0, "transitionIds": ["tr_src"]})
        assert status == 200, f"got {status}: {body}"
        assert "tr_target" in body["splitTransitionIds"]

        tr_target = get_transition(project_dir, "tr_target")
        # tr_target trimmed: to_kf now points to new_from_kf of tr_src (at 10);
        # trim_out shrunk to trim_in + (10 - 5) * factor. Factor = (10-0)/(15-5) = 1.0.
        # new trim_out = 0 + 5 * 1.0 = 5.0; new duration = 5.0.
        assert abs(tr_target["trim_out"] - 5.0) < 0.01
        assert abs(tr_target["duration_seconds"] - 5.0) < 0.01

    def test_case_c_straddles_new_to(self, project_env):
        """Drag tr_src onto track_2 ending mid-target. tr_target[5,15] straddles new_to."""
        project_dir = project_env["project_dir"]
        _seed_two_track_content(project_dir)
        _seed_single_clip_on_track_1(project_dir)
        # tr_src [30,35] -> [0,5] on track_2. Drag: timeDelta=-30, trackDelta=1.
        # tr_target[5,15]: tf=5, tt=15, new_from=0, new_to=5. tf>=new_from AND tt>new_to => Case C.
        status, body = api(project_env, "POST",
            f"/api/projects/{project_env['project_name']}/move-transitions",
            {"mode": "move", "trackDelta": 1, "timeDeltaSeconds": -30.0, "transitionIds": ["tr_src"]})
        assert status == 200, f"got {status}: {body}"
        # tr_target and tr_src land adjacent (new_to=5 == tr_target.tf=5) => no overlap by strict check.
        # Actual overlap is between dropped [0,5] and target [5,15]: tt(15) > new_to(5) + epsilon,
        # tf(5) >= new_from(0) - epsilon. But tf(5) == new_to(5), and our overlap detection skips
        # tf >= new_to - 0.0005 -> tf=5 >= 4.9995, which means it IS skipped (correctly, since
        # boundary-touching isn't overlap). So no split fires; this tests the non-overlap boundary case.
        # Switch to a real Case C: new_to lands strictly inside target.
        # Re-drag: tr_src -> [2, 7] on track_2. timeDelta from current position: tr_src is now at [0,5]
        # Move tr_src by +2s (still on track_2): new position [2,7]. tr_target[5,15]:
        # tf=5, tt=15, new_from=2, new_to=7. tf >= new_from, tt > new_to => Case C.
        status2, body2 = api(project_env, "POST",
            f"/api/projects/{project_env['project_name']}/move-transitions",
            {"mode": "move", "trackDelta": 0, "timeDeltaSeconds": 2.0, "transitionIds": ["tr_src"]})
        assert status2 == 200, f"got {status2}: {body2}"
        assert "tr_target" in body2["splitTransitionIds"]

        tr_target = get_transition(project_dir, "tr_target")
        # factor = 10/10 = 1.0. new trim_in = 0 + (7-5)*1 = 2.0. new duration = 15-7 = 8.0.
        assert abs(tr_target["trim_in"] - 2.0) < 0.01
        assert abs(tr_target["duration_seconds"] - 8.0) < 0.01

    def test_case_d_three_way_split(self, project_env):
        """Drag tr_src fully inside tr_target -> target splits into left + right remainders."""
        project_dir = project_env["project_dir"]
        _seed_two_track_content(project_dir)
        _seed_single_clip_on_track_1(project_dir)
        # tr_src [30,35] (5s) -> [8,13] on track_2, fully inside tr_target[5,15].
        # timeDelta=-22, trackDelta=1. tr_target: tf=5 < new_from=8, tt=15 > new_to=13 => Case D.
        status, body = api(project_env, "POST",
            f"/api/projects/{project_env['project_name']}/move-transitions",
            {"mode": "move", "trackDelta": 1, "timeDeltaSeconds": -22.0, "transitionIds": ["tr_src"]})
        assert status == 200, f"got {status}: {body}"
        assert "tr_target" in body["splitTransitionIds"]

        tr_target = get_transition(project_dir, "tr_target")
        assert tr_target["deleted_at"] is not None  # original soft-deleted after split

        # Two new tr rows should have been inserted on track_2 with proportional trim.
        all_trs = get_transitions(project_dir)
        track_2_content = [t for t in all_trs
                           if t["track_id"] == "track_2" and not t.get("deleted_at") and t["id"] != "tr_src"]
        # Expect two remainders + tr_src = 3 trs on track_2 after the split
        assert len(track_2_content) == 2

        # Left remainder: [5,8], trim [0, 3]; Right remainder: [13,15], trim [8,10]
        durs = sorted(round(t["duration_seconds"], 2) for t in track_2_content)
        assert durs == [2.0, 3.0]


# ── Task 96: copy mode ────────────────────────────────────────────────


def _seed_tr_candidate_for(project_dir, tr_id: str, slot: int = 0):
    """Seed one pool_segments row + tr_candidates junction row for tr_id/slot.

    Returns the pool_segment_id so tests can assert the copy references the same
    underlying pool file (no duplication).
    """
    from scenecraft.db import add_pool_segment, add_tr_candidate
    seg_id = add_pool_segment(
        project_dir,
        kind="generated",
        created_by="test",
        pool_path=f"pool/segments/seg_{tr_id}_{slot}.mp4",
        duration_seconds=5.0,
    )
    add_tr_candidate(
        project_dir,
        transition_id=tr_id,
        slot=slot,
        pool_segment_id=seg_id,
        source="generated",
    )
    return seg_id


def _seed_cache_file(project_dir, tr_id: str, slot: int = 0) -> Path:
    """Create a dummy selected_transitions/{tr}_slot_{N}.mp4 file so the cache-copy
    path has something to copy. Returns the created path.
    """
    sel_dir = project_dir / "selected_transitions"
    sel_dir.mkdir(exist_ok=True)
    p = sel_dir / f"{tr_id}_slot_{slot}.mp4"
    p.write_bytes(b"fake-mp4-bytes-for-" + tr_id.encode())
    return p


def _get_tr_candidates_raw(project_dir, tr_id: str) -> list[dict]:
    """Fetch raw tr_candidates rows for a transition (slot 0) without the pool join."""
    from scenecraft.db import get_db
    conn = get_db(project_dir)
    rows = conn.execute(
        "SELECT transition_id, slot, pool_segment_id, source FROM tr_candidates "
        "WHERE transition_id = ? ORDER BY slot, added_at",
        (tr_id,),
    ).fetchall()
    return [dict(r) for r in rows]


class TestMoveTransitionsCopyMode:
    def test_same_track_copy_leaves_source_intact(self, project_env):
        """Copy tr_002 in place +10s on track_1.

        The source tr_002 must remain active at its original position. A fresh tr
        copy with a new id must sit on track_1 at [25, 30]. The copy's tr_candidates
        must be a clone of the source's (same pool_segment_id).
        """
        project_dir = project_env["project_dir"]
        _seed_three_clip_track(project_dir)
        source_seg = _seed_tr_candidate_for(project_dir, "tr_002")
        _seed_cache_file(project_dir, "tr_002")

        trs_before = {t["id"] for t in get_transitions(project_dir)}
        tr_002_before = get_transition(project_dir, "tr_002")

        status, body = api(project_env, "POST",
            f"/api/projects/{project_env['project_name']}/move-transitions",
            {"mode": "copy", "trackDelta": 0, "timeDeltaSeconds": 10.0,
             "transitionIds": ["tr_002"]})
        assert status == 200, f"got {status}: {body}"

        # Source unchanged.
        tr_002_after = get_transition(project_dir, "tr_002")
        assert tr_002_after["deleted_at"] is None
        assert tr_002_after["from"] == tr_002_before["from"]
        assert tr_002_after["to"] == tr_002_before["to"]
        assert tr_002_after["track_id"] == "track_1"

        # Response returns the new tr id (not the source).
        assert len(body["movedTransitionIds"]) == 1
        new_tr_id = body["movedTransitionIds"][0]
        assert new_tr_id != "tr_002"
        assert new_tr_id not in trs_before

        # The copy sits on track_1 with shifted from/to.
        new_tr = get_transition(project_dir, new_tr_id)
        assert new_tr is not None
        assert new_tr["track_id"] == "track_1"
        new_from_kf = get_keyframe(project_dir, new_tr["from"])
        new_to_kf = get_keyframe(project_dir, new_tr["to"])
        assert abs(parse_ts(new_from_kf["timestamp"]) - 25.0) < 0.01
        assert abs(parse_ts(new_to_kf["timestamp"]) - 30.0) < 0.01
        # Cloned fields
        assert new_tr["duration_seconds"] == tr_002_before["duration_seconds"]
        assert new_tr["selected"] == tr_002_before["selected"]

        # tr_candidates references the SAME pool_segment_id (no file duplication).
        copy_junction = _get_tr_candidates_raw(project_dir, new_tr_id)
        assert len(copy_junction) == 1
        assert copy_junction[0]["pool_segment_id"] == source_seg
        assert copy_junction[0]["source"] == "copy-inherit"

        # Source junction still intact.
        src_junction = _get_tr_candidates_raw(project_dir, "tr_002")
        assert len(src_junction) == 1
        assert src_junction[0]["pool_segment_id"] == source_seg

    def test_cross_track_copy_source_untouched(self, project_env):
        """Copy tr_002 to track_2 (trackDelta=+1). Source stays on track_1."""
        project_dir = project_env["project_dir"]
        _ensure_track(project_dir, "track_2", z_order=1, name="Track 2")
        _seed_three_clip_track(project_dir)
        _seed_tr_candidate_for(project_dir, "tr_002")

        status, body = api(project_env, "POST",
            f"/api/projects/{project_env['project_name']}/move-transitions",
            {"mode": "copy", "trackDelta": 1, "timeDeltaSeconds": 0.0,
             "transitionIds": ["tr_002"]})
        assert status == 200, f"got {status}: {body}"

        # Source unchanged on track_1.
        tr_002_after = get_transition(project_dir, "tr_002")
        assert tr_002_after["deleted_at"] is None
        assert tr_002_after["track_id"] == "track_1"
        assert tr_002_after["from"] == "kf_002"
        assert tr_002_after["to"] == "kf_003"

        # Copy on track_2 with cloned timestamps.
        new_tr_id = body["movedTransitionIds"][0]
        new_tr = get_transition(project_dir, new_tr_id)
        assert new_tr["track_id"] == "track_2"
        new_from_kf = get_keyframe(project_dir, new_tr["from"])
        new_to_kf = get_keyframe(project_dir, new_tr["to"])
        assert new_from_kf["track_id"] == "track_2"
        assert new_to_kf["track_id"] == "track_2"
        assert abs(parse_ts(new_from_kf["timestamp"]) - 15.0) < 0.01
        assert abs(parse_ts(new_to_kf["timestamp"]) - 20.0) < 0.01

    def test_multi_clip_copy_creates_n_fresh_copies(self, project_env):
        """Copying tr_001 + tr_002 together: both sources stay put; two fresh copies appear."""
        project_dir = project_env["project_dir"]
        _ensure_track(project_dir, "track_2", z_order=1, name="Track 2")
        _seed_three_clip_track(project_dir)
        _seed_tr_candidate_for(project_dir, "tr_001")
        _seed_tr_candidate_for(project_dir, "tr_002")

        trs_before = {t["id"] for t in get_transitions(project_dir)}

        status, body = api(project_env, "POST",
            f"/api/projects/{project_env['project_name']}/move-transitions",
            {"mode": "copy", "trackDelta": 1, "timeDeltaSeconds": 100.0,
             "transitionIds": ["tr_001", "tr_002"]})
        assert status == 200, f"got {status}: {body}"

        # Source trs unchanged.
        tr_001_after = get_transition(project_dir, "tr_001")
        tr_002_after = get_transition(project_dir, "tr_002")
        assert tr_001_after["deleted_at"] is None
        assert tr_002_after["deleted_at"] is None
        assert tr_001_after["track_id"] == "track_1"
        assert tr_002_after["track_id"] == "track_1"
        assert tr_001_after["from"] == "kf_001" and tr_001_after["to"] == "kf_002"
        assert tr_002_after["from"] == "kf_002" and tr_002_after["to"] == "kf_003"

        # Two fresh copies, both on track_2.
        assert len(body["movedTransitionIds"]) == 2
        for new_id in body["movedTransitionIds"]:
            assert new_id not in trs_before
            new_tr = get_transition(project_dir, new_id)
            assert new_tr["track_id"] == "track_2"

        # Pair order: index i in request -> index i in response.
        new_tr_0 = get_transition(project_dir, body["movedTransitionIds"][0])
        new_tr_1 = get_transition(project_dir, body["movedTransitionIds"][1])
        k0_from = get_keyframe(project_dir, new_tr_0["from"])
        k1_from = get_keyframe(project_dir, new_tr_1["from"])
        # tr_001 source at 10 -> copy at 110; tr_002 source at 15 -> copy at 115.
        assert abs(parse_ts(k0_from["timestamp"]) - 110.0) < 0.01
        assert abs(parse_ts(k1_from["timestamp"]) - 115.0) < 0.01

    def test_copy_lands_on_overlap_triggers_resolution(self, project_env):
        """Copy tr_src onto track_2 fully inside tr_target -> Case D three-way split.

        Source tr_src stays intact on track_1. tr_target is soft-deleted, two
        remainder trs appear on track_2, and the copy itself is unaffected.
        """
        project_dir = project_env["project_dir"]
        _seed_two_track_content(project_dir)   # track_2: tr_target [5,15]
        _seed_single_clip_on_track_1(project_dir)  # track_1: tr_src [30,35]
        _seed_tr_candidate_for(project_dir, "tr_src")

        # Copy tr_src fully inside tr_target: [30,35] -> [8,13] on track_2.
        # timeDelta=-22, trackDelta=+1.
        status, body = api(project_env, "POST",
            f"/api/projects/{project_env['project_name']}/move-transitions",
            {"mode": "copy", "trackDelta": 1, "timeDeltaSeconds": -22.0,
             "transitionIds": ["tr_src"]})
        assert status == 200, f"got {status}: {body}"

        # Source unchanged on track_1.
        tr_src_after = get_transition(project_dir, "tr_src")
        assert tr_src_after["deleted_at"] is None
        assert tr_src_after["track_id"] == "track_1"

        # tr_target split fires.
        tr_target = get_transition(project_dir, "tr_target")
        assert tr_target["deleted_at"] is not None
        assert "tr_target" in body["splitTransitionIds"]

        # The copy itself is alive on track_2.
        new_tr_id = body["movedTransitionIds"][0]
        new_tr = get_transition(project_dir, new_tr_id)
        assert new_tr["deleted_at"] is None
        assert new_tr["track_id"] == "track_2"

    def test_no_pool_file_duplication(self, project_env):
        """Both the source and the copy reference the SAME pool_segment_id rows."""
        project_dir = project_env["project_dir"]
        _ensure_track(project_dir, "track_2", z_order=1, name="Track 2")
        _seed_three_clip_track(project_dir)
        src_seg_slot0 = _seed_tr_candidate_for(project_dir, "tr_001", slot=0)
        # Also seed a second candidate at slot 0 to verify multi-row cloning.
        from scenecraft.db import add_pool_segment, add_tr_candidate
        seg_b = add_pool_segment(project_dir, kind="generated", created_by="test",
                                 pool_path="pool/segments/seg_b.mp4", duration_seconds=5.0)
        add_tr_candidate(project_dir, transition_id="tr_001", slot=0,
                         pool_segment_id=seg_b, source="generated")

        status, body = api(project_env, "POST",
            f"/api/projects/{project_env['project_name']}/move-transitions",
            {"mode": "copy", "trackDelta": 1, "timeDeltaSeconds": 0.0,
             "transitionIds": ["tr_001"]})
        assert status == 200

        new_tr_id = body["movedTransitionIds"][0]
        copy_junction = _get_tr_candidates_raw(project_dir, new_tr_id)
        copy_seg_ids = {r["pool_segment_id"] for r in copy_junction}
        assert copy_seg_ids == {src_seg_slot0, seg_b}

        # All copy rows have source="copy-inherit"
        for r in copy_junction:
            assert r["source"] == "copy-inherit"

    def test_selected_transitions_cache_copied(self, project_env):
        """After a copy, selected_transitions/{new_tr_id}_slot_0.mp4 exists."""
        project_dir = project_env["project_dir"]
        _ensure_track(project_dir, "track_2", z_order=1, name="Track 2")
        _seed_three_clip_track(project_dir)
        src_cache = _seed_cache_file(project_dir, "tr_001", slot=0)
        assert src_cache.exists()

        status, body = api(project_env, "POST",
            f"/api/projects/{project_env['project_name']}/move-transitions",
            {"mode": "copy", "trackDelta": 1, "timeDeltaSeconds": 0.0,
             "transitionIds": ["tr_001"]})
        assert status == 200

        new_tr_id = body["movedTransitionIds"][0]
        copy_cache = project_dir / "selected_transitions" / f"{new_tr_id}_slot_0.mp4"
        assert copy_cache.exists(), f"expected cache file at {copy_cache}"
        # Source cache still present.
        assert src_cache.exists()
        # Contents match (shutil.copy2 preserves bytes).
        assert copy_cache.read_bytes() == src_cache.read_bytes()

    def test_response_contains_new_ids_not_source(self, project_env):
        """movedTransitionIds must be the NEW tr ids in copy mode."""
        project_dir = project_env["project_dir"]
        _seed_three_clip_track(project_dir)

        status, body = api(project_env, "POST",
            f"/api/projects/{project_env['project_name']}/move-transitions",
            {"mode": "copy", "trackDelta": 0, "timeDeltaSeconds": 100.0,
             "transitionIds": ["tr_001", "tr_002"]})
        assert status == 200

        # Neither tr_001 nor tr_002 should appear in the response.
        assert "tr_001" not in body["movedTransitionIds"]
        assert "tr_002" not in body["movedTransitionIds"]
        assert len(body["movedTransitionIds"]) == 2

        # All new ids point to active trs.
        for new_id in body["movedTransitionIds"]:
            t = get_transition(project_dir, new_id)
            assert t is not None
            assert t["deleted_at"] is None


# ── Linked-audio propagation ──────────────────────────────────────────


def _seed_audio_clip_linked_to_tr(project_dir, *, clip_id: str, tr_id: str,
                                  start: float, end: float):
    """Create an audio track, a clip on it, and a link row to `tr_id`."""
    from scenecraft.db import (
        add_audio_track, add_audio_clip, add_audio_clip_link,
    )
    add_audio_track(project_dir, {
        "id": "atrack_1", "name": "Audio 1", "display_order": 0,
    })
    add_audio_clip(project_dir, {
        "id": clip_id, "track_id": "atrack_1",
        "source_path": "pool/audio.wav",
        "start_time": start, "end_time": end, "source_offset": 0,
    })
    add_audio_clip_link(project_dir, clip_id, tr_id, offset=0.0)


class TestMoveTransitionsLinkedAudio:
    def test_dragged_tr_shifts_linked_audio_clip(self, project_env):
        """Regression: dragging a transition with shared-boundary kfs must still
        shift its linked audio clip. The boundary path in resolve_new_kf
        creates a fresh kf so update_keyframe's propagation never runs —
        the handler must shift the clip explicitly.
        """
        project_dir = project_env["project_dir"]
        _seed_three_clip_track(project_dir)  # tr_001/002/003 share boundary kfs
        # Clip sits under tr_002 (kf_002 @ 0:15 → kf_003 @ 0:20)
        _seed_audio_clip_linked_to_tr(
            project_dir, clip_id="audio_clip_01", tr_id="tr_002",
            start=15.0, end=20.0,
        )

        status, body = api(project_env, "POST",
            f"/api/projects/{project_env['project_name']}/move-transitions",
            {"mode": "move", "trackDelta": 0, "timeDeltaSeconds": 3.0,
             "transitionIds": ["tr_002"]})
        assert status == 200, body

        # Linked clip must have followed the transition by +3s.
        from scenecraft.db import get_audio_clips
        clips = {c["id"]: c for c in get_audio_clips(project_dir)}
        assert "audio_clip_01" in clips
        clip = clips["audio_clip_01"]
        assert abs(clip["start_time"] - 18.0) < 1e-4, \
            f"start_time should be 18.0, got {clip['start_time']}"
        assert abs(clip["end_time"] - 23.0) < 1e-4, \
            f"end_time should be 23.0, got {clip['end_time']}"

    def test_linked_audio_not_double_shifted_on_interior_kf_path(self, project_env):
        """Interior-kf path already shifts clips via update_keyframe's
        propagation. The handler's explicit shift must not fire in that case."""
        project_dir = project_env["project_dir"]
        # Isolated transition — kfs are NOT shared with anything else, so both
        # from and to are interior.
        add_keyframe(project_dir, {
            "id": "kf_solo_a", "timestamp": "0:10.00", "section": "",
            "source": "", "prompt": "", "selected": None, "candidates": [],
            "track_id": "track_1",
        })
        add_keyframe(project_dir, {
            "id": "kf_solo_b", "timestamp": "0:15.00", "section": "",
            "source": "", "prompt": "", "selected": None, "candidates": [],
            "track_id": "track_1",
        })
        add_transition(project_dir, {
            "id": "tr_solo", "from": "kf_solo_a", "to": "kf_solo_b",
            "duration_seconds": 5, "slots": 1, "action": "",
            "use_global_prompt": False, "selected": None,
            "remap": {"method": "linear", "target_duration": 5},
            "track_id": "track_1",
        })
        _seed_audio_clip_linked_to_tr(
            project_dir, clip_id="audio_clip_solo", tr_id="tr_solo",
            start=10.0, end=15.0,
        )

        status, body = api(project_env, "POST",
            f"/api/projects/{project_env['project_name']}/move-transitions",
            {"mode": "move", "trackDelta": 0, "timeDeltaSeconds": 2.5,
             "transitionIds": ["tr_solo"]})
        assert status == 200, body

        from scenecraft.db import get_audio_clips
        clips = {c["id"]: c for c in get_audio_clips(project_dir)}
        clip = clips["audio_clip_solo"]
        # Shifted exactly once by +2.5, not +5.
        assert abs(clip["start_time"] - 12.5) < 1e-4, \
            f"start_time should be 12.5 (single shift), got {clip['start_time']}"
        assert abs(clip["end_time"] - 17.5) < 1e-4, \
            f"end_time should be 17.5 (single shift), got {clip['end_time']}"
