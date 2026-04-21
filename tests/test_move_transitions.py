"""E2E tests for POST /api/projects/:name/move-transitions (Task 93 scope).

Task 93 scope: same-track, single-delta time-shift only.
- mode="copy" -> 501
- trackDelta != 0 -> 501
- new_from_time < 0 -> 400
- single tr timeDelta=+3 -> from/to shifted +3s
- batch of 2 trs -> both shifted; undo reverts together

Downstream tasks (94/95/96) extend this endpoint with cross-track, overlap,
and copy-mode support.
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
    add_keyframe, add_transition, close_db, get_keyframe, get_keyframes,
    get_transition, get_transitions, set_meta, _migrated_dbs,
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
    def test_copy_mode_rejected_with_501(self, project_env):
        _seed_three_clip_track(project_env["project_dir"])
        status, body = api(project_env, "POST",
            f"/api/projects/{project_env['project_name']}/move-transitions",
            {"mode": "copy", "trackDelta": 0, "timeDeltaSeconds": 1.0, "transitionIds": ["tr_001"]})
        assert status == 501, f"expected 501, got {status}: {body}"

    def test_cross_track_rejected_with_501(self, project_env):
        _seed_three_clip_track(project_env["project_dir"])
        status, body = api(project_env, "POST",
            f"/api/projects/{project_env['project_name']}/move-transitions",
            {"mode": "move", "trackDelta": 1, "timeDeltaSeconds": 0.0, "transitionIds": ["tr_001"]})
        assert status == 501, f"expected 501, got {status}: {body}"

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
        assert body["splitTransitionIds"] == []

        # Verify tr_002's new from/to timestamps are shifted by +3.
        tr_002 = get_transition(project_dir, "tr_002")
        from_kf = get_keyframe(project_dir, tr_002["from"])
        to_kf = get_keyframe(project_dir, tr_002["to"])
        assert abs(parse_ts(from_kf["timestamp"]) - 18.0) < 0.01
        assert abs(parse_ts(to_kf["timestamp"]) - 23.0) < 0.01
        # Duration preserved.
        assert abs(tr_002["duration_seconds"] - 5.0) < 0.01

        # Verify kf_002 and kf_003 (original boundaries) still exist at original times
        # (they were shared with tr_001 / tr_003).
        kf_002 = get_keyframe(project_dir, "kf_002")
        kf_003 = get_keyframe(project_dir, "kf_003")
        assert abs(parse_ts(kf_002["timestamp"]) - 15.0) < 0.01
        assert abs(parse_ts(kf_003["timestamp"]) - 20.0) < 0.01

        # tr_001 and tr_003 untouched (still reference original kfs).
        tr_001 = get_transition(project_dir, "tr_001")
        tr_003 = get_transition(project_dir, "tr_003")
        assert tr_001["to"] == "kf_002"
        assert tr_003["from"] == "kf_003"

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

        kfs_before = {k["id"] for k in get_keyframes(project_dir)}

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

        # tr_003 untouched: still from kf_003(0:20) to kf_004(0:25).
        assert tr_003["from"] == "kf_003"
        assert tr_003["to"] == "kf_004"
        kf_003 = get_keyframe(project_dir, "kf_003")
        assert abs(parse_ts(kf_003["timestamp"]) - 20.0) < 0.01

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
