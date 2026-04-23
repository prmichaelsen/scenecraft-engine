"""Tests for POST /audio-clips/batch-ops (M11 task-104b).

Exercises the handler directly via a FakeHandler harness (no live server)
so the batch-op dispatch, validation, single-undo-group guarantee, and
split semantics are covered end-to-end. The peaks endpoint uses the same
pattern.
"""

from __future__ import annotations

import io
import json

import pytest


# ── FakeHandler harness ─────────────────────────────────────────────────


class _FakeHeaders:
    def __init__(self, data=None):
        self._data = data or {}

    def get(self, k, default=None):
        return self._data.get(k, default)


class _FakeHandler:
    def __init__(self, project_dir, body):
        self._project_dir = project_dir
        self._body = json.dumps(body).encode("utf-8")
        self._body_pos = 0
        self.status = None
        self.headers_out = {}
        self.body_out = b""
        self._refreshed_cookie = None
        self.headers = _FakeHeaders({"Content-Length": str(len(self._body))})
        self.rfile = io.BytesIO(self._body)
        self.wfile = io.BytesIO()

    def send_response(self, status):
        self.status = status

    def send_header(self, k, v):
        self.headers_out[k] = v

    def end_headers(self):
        pass

    def _require_project_dir(self, _name):
        return self._project_dir

    def _cors_headers(self):
        pass

    def _error(self, status, code, message):
        self.status = status
        self.body_out = json.dumps({"error": message, "code": code}).encode()

    def _json_response(self, obj, status=200):
        self.status = status
        self.body_out = json.dumps(obj).encode()

    def _read_json_body(self):
        return json.loads(self._body)


def _invoke_batch_ops(project_dir, body):
    import scenecraft.api_server as api_mod

    handler_cls = api_mod.make_handler(project_dir.parent, no_auth=True)

    # Grab the private _do_POST method and run it against the FakeHandler.
    # We want the full dispatch-through-routes flow so the regex match is
    # exercised just like a real request.
    def _invoke(path):
        fh = _FakeHandler(project_dir=project_dir, body=body)
        fh.path = path
        fh.command = "POST"

        handler_cls._do_POST(fh, path)
        return fh

    return _invoke(
        f"/api/projects/{project_dir.name}/audio-clips/batch-ops"
    )


# ── Fixtures ─────────────────────────────────────────────────────────────


@pytest.fixture
def project(tmp_path):
    from scenecraft.db import get_db

    d = tmp_path / "batch_ops_proj"
    d.mkdir()
    get_db(d)
    return d


@pytest.fixture
def track_id(project):
    from scenecraft.db import add_audio_track

    t = "audio_track_test_1"
    add_audio_track(project, {"id": t, "name": "t", "display_order": 0})
    return t


def _make_clip(project, track, clip_id, start, end, src_offset=0.0):
    from scenecraft.db import add_audio_clip

    add_audio_clip(
        project,
        {
            "id": clip_id,
            "track_id": track,
            "source_path": "pool/segments/fake.wav",
            "start_time": start,
            "end_time": end,
            "source_offset": src_offset,
        },
    )


def _get_clip(project, clip_id):
    from scenecraft.db import get_audio_clips

    for c in get_audio_clips(project):
        if c["id"] == clip_id:
            return c
    return None


# ── Validation ──────────────────────────────────────────────────────────


def test_batch_ops_empty_list_rejected(project, track_id):
    fh = _invoke_batch_ops(project, {"label": "empty", "ops": []})
    assert fh.status == 400
    assert b"non-empty" in fh.body_out


def test_batch_ops_unknown_op_rejected(project, track_id):
    _make_clip(project, track_id, "ac_1", 0.0, 10.0)
    fh = _invoke_batch_ops(project, {
        "label": "bad",
        "ops": [{"op": "frobnicate", "id": "ac_1"}],
    })
    assert fh.status == 400
    assert b"unknown op" in fh.body_out
    # Clip unchanged.
    assert _get_clip(project, "ac_1")["end_time"] == 10.0


def test_batch_ops_missing_split_fields_rejected(project, track_id):
    _make_clip(project, track_id, "ac_1", 0.0, 10.0)
    fh = _invoke_batch_ops(project, {
        "label": "bad",
        "ops": [{"op": "split", "id": "ac_1"}],
    })
    assert fh.status == 400


def test_batch_ops_insert_missing_fields_rejected(project, track_id):
    fh = _invoke_batch_ops(project, {
        "label": "bad",
        "ops": [{"op": "insert", "clip": {"id": "ac_x"}}],
    })
    assert fh.status == 400
    assert b"missing" in fh.body_out


# ── Happy paths ─────────────────────────────────────────────────────────


def test_batch_ops_trim_updates_endpoint(project, track_id):
    _make_clip(project, track_id, "ac_1", 0.0, 10.0)
    fh = _invoke_batch_ops(project, {
        "label": "trim",
        "ops": [{"op": "trim", "id": "ac_1", "end_time": 4.5}],
    })
    assert fh.status == 200
    assert _get_clip(project, "ac_1")["end_time"] == 4.5


def test_batch_ops_trim_left_advances_source_offset(project, track_id):
    """Left-trim (start_time advances) must also advance source_offset so
    audio-sync stays correct. The endpoint itself doesn't compute this —
    the caller provides source_offset — but we verify the field wires
    through."""
    _make_clip(project, track_id, "ac_1", 0.0, 10.0, src_offset=2.0)
    fh = _invoke_batch_ops(project, {
        "label": "trim-left",
        "ops": [{
            "op": "trim", "id": "ac_1",
            "start_time": 3.0, "source_offset": 5.0,
        }],
    })
    assert fh.status == 200
    c = _get_clip(project, "ac_1")
    assert c["start_time"] == 3.0
    assert c["source_offset"] == 5.0


def test_batch_ops_split_creates_right_half_with_correct_offset(project, track_id):
    _make_clip(project, track_id, "ac_1", 0.0, 10.0, src_offset=2.0)
    fh = _invoke_batch_ops(project, {
        "label": "split",
        "ops": [{
            "op": "split", "id": "ac_1", "at": 6.0, "new_id": "ac_1_right",
        }],
    })
    assert fh.status == 200
    left = _get_clip(project, "ac_1")
    right = _get_clip(project, "ac_1_right")
    assert left["end_time"] == 6.0
    assert right is not None
    assert right["start_time"] == 6.0
    assert right["end_time"] == 10.0
    # Right half's source_offset = original_offset + (at - original_start) = 2.0 + 6.0 = 8.0
    assert right["source_offset"] == 8.0


def test_batch_ops_delete_soft_deletes(project, track_id):
    _make_clip(project, track_id, "ac_1", 0.0, 10.0)
    fh = _invoke_batch_ops(project, {
        "label": "del",
        "ops": [{"op": "delete", "id": "ac_1"}],
    })
    assert fh.status == 200
    # get_audio_clips filters by deleted_at IS NULL by default — the row
    # should no longer show up.
    assert _get_clip(project, "ac_1") is None


def test_batch_ops_insert_adds_clip(project, track_id):
    fh = _invoke_batch_ops(project, {
        "label": "ins",
        "ops": [{
            "op": "insert", "clip": {
                "id": "ac_new", "track_id": track_id,
                "source_path": "pool/segments/stem.wav",
                "start_time": 5.0, "end_time": 15.0, "source_offset": 0.0,
            },
        }],
    })
    assert fh.status == 200
    c = _get_clip(project, "ac_new")
    assert c is not None
    assert c["start_time"] == 5.0
    assert c["end_time"] == 15.0


def test_batch_ops_mixed_operations_land_in_one_undo_group(project, track_id):
    """The drop scenario: stem (5-15s) drops onto an existing clip (0-10s)
    that it partially overlaps on the left edge. Ops: trim the existing
    clip down to start=15s, insert the new stem clip. Both mutations
    should be one undo group."""
    _make_clip(project, track_id, "ac_existing", 0.0, 20.0, src_offset=0.0)

    from scenecraft.db import get_db
    conn = get_db(project)
    groups_before = conn.execute("SELECT COUNT(*) FROM undo_groups").fetchone()[0]

    fh = _invoke_batch_ops(project, {
        "label": "Drop vocal stem",
        "ops": [
            {"op": "trim", "id": "ac_existing", "start_time": 15.0, "source_offset": 15.0},
            {"op": "insert", "clip": {
                "id": "ac_stem", "track_id": track_id,
                "source_path": "pool/segments/stem.wav",
                "start_time": 5.0, "end_time": 15.0, "source_offset": 0.0,
            }},
        ],
    })
    assert fh.status == 200

    groups_after = conn.execute("SELECT COUNT(*) FROM undo_groups").fetchone()[0]
    assert groups_after == groups_before + 1

    existing = _get_clip(project, "ac_existing")
    stem = _get_clip(project, "ac_stem")
    assert existing["start_time"] == 15.0
    assert stem["start_time"] == 5.0
    assert stem["end_time"] == 15.0


def test_batch_ops_undo_reverts_the_whole_batch(project, track_id):
    """Single Ctrl+Z on a mixed-op drop should fully reverse the drop."""
    from scenecraft.db import undo_execute, get_audio_clips

    _make_clip(project, track_id, "ac_existing", 0.0, 20.0, src_offset=0.0)

    fh = _invoke_batch_ops(project, {
        "label": "Drop stem",
        "ops": [
            {"op": "trim", "id": "ac_existing", "start_time": 15.0, "source_offset": 15.0},
            {"op": "insert", "clip": {
                "id": "ac_stem", "track_id": track_id,
                "source_path": "pool/segments/stem.wav",
                "start_time": 5.0, "end_time": 15.0,
            }},
        ],
    })
    assert fh.status == 200

    undo_result = undo_execute(project)
    assert undo_result is not None

    # After undo: the inserted clip is gone, the trimmed one is restored.
    existing = _get_clip(project, "ac_existing")
    stem = _get_clip(project, "ac_stem")
    assert existing["start_time"] == 0.0
    assert stem is None
