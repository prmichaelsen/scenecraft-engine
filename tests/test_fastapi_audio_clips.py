"""M16 T62 audio-clips router tests (FastAPI)."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from scenecraft.api.app import create_app
from scenecraft.db import (
    add_audio_clip as db_add_audio_clip,
    add_audio_track as db_add_audio_track,
    close_db,
    get_audio_clips as db_get_audio_clips,
    get_db,
)


@pytest.fixture
def project(tmp_path: Path):
    work_dir = tmp_path / "work"
    name = "P"
    pd = work_dir / name
    pd.mkdir(parents=True)
    get_db(pd)
    db_add_audio_track(pd, {"id": "t1", "name": "T1", "display_order": 0})
    yield (work_dir, name)
    close_db(pd)


@pytest.fixture
def client(project):
    work_dir, _ = project
    return TestClient(create_app(work_dir=work_dir))


def test_list_audio_clips_empty(client, project):
    _, name = project
    r = client.get(f"/api/projects/{name}/audio-clips")
    assert r.status_code == 200
    assert r.json() == {"audioClips": []}


def test_add_audio_clip(client, project):
    work_dir, name = project
    r = client.post(
        f"/api/projects/{name}/audio-clips/add",
        json={
            "trackId": "t1",
            "sourcePath": "a.wav",
            "startTime": 0.0,
            "endTime": 1.0,
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["success"] is True
    cid = body["id"]
    assert cid.startswith("audio_clip_")

    clips = db_get_audio_clips(work_dir / name)
    assert any(c["id"] == cid for c in clips)


def test_add_audio_clip_missing_track_400(client, project):
    _, name = project
    r = client.post(
        f"/api/projects/{name}/audio-clips/add",
        json={"sourcePath": "x.wav", "startTime": 0, "endTime": 1},
    )
    assert r.status_code == 400
    assert r.json()["error"] == "BAD_REQUEST"


def test_update_audio_clip_missing_id_400(client, project):
    _, name = project
    r = client.post(
        f"/api/projects/{name}/audio-clips/update", json={"startTime": 1.0}
    )
    assert r.status_code == 400


def test_delete_audio_clip(client, project):
    work_dir, name = project
    add = client.post(
        f"/api/projects/{name}/audio-clips/add",
        json={
            "trackId": "t1",
            "sourcePath": "a.wav",
            "startTime": 0,
            "endTime": 1,
        },
    )
    cid = add.json()["id"]

    r = client.post(f"/api/projects/{name}/audio-clips/delete", json={"id": cid})
    assert r.status_code == 200
    assert r.json()["success"] is True


def test_batch_ops_insert(client, project):
    _, name = project
    r = client.post(
        f"/api/projects/{name}/audio-clips/batch-ops",
        json={
            "label": "insert new",
            "ops": [
                {
                    "op": "insert",
                    "clip": {
                        "id": "c_new",
                        "track_id": "t1",
                        "source_path": "b.wav",
                        "start_time": 0.0,
                        "end_time": 1.0,
                    },
                }
            ],
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["success"] is True
    assert body["ops_applied"] == 1


def test_batch_ops_empty_400(client, project):
    _, name = project
    r = client.post(
        f"/api/projects/{name}/audio-clips/batch-ops",
        json={"ops": []},
    )
    assert r.status_code == 400


def test_operation_ids(client):
    spec = client.get("/openapi.json").json()
    ops = {
        op["operationId"]
        for path in spec["paths"].values()
        for op in path.values()
        if isinstance(op, dict) and "operationId" in op
    }
    assert "list_audio_clips" in ops
    assert "add_audio_clip_core" in ops
    assert "add_audio_clip" in ops  # chat-tool alignment (from-pool)
    assert "update_audio_clip" in ops
    assert "delete_audio_clip" in ops
    assert "apply_mix_plan" in ops  # chat-tool alignment (batch-ops)
    assert "align_audio_clips" in ops
    assert "get_audio_clip_peaks" in ops
