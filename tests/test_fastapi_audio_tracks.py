"""M16 T62 audio-tracks router tests (FastAPI).

Mirrors the legacy audio-track HTTP endpoints (``/tracks`` +
``/audio-tracks``) and verifies parity: add/update/delete/reorder for
both surfaces plus the GET list endpoints.

TDD red-phase rules: every test must fail with either a ``ModuleNotFoundError``
(no router exists yet) or ``404`` (app has no matching route) until
``routers/audio_tracks.py`` lands.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from scenecraft.api.app import create_app
from scenecraft.db import (
    add_audio_track as db_add_audio_track,
    add_track as db_add_track,
    close_db,
    get_audio_tracks as db_get_audio_tracks,
    get_db,
    get_tracks as db_get_tracks,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def project(tmp_path: Path) -> tuple[Path, str]:
    work_dir = tmp_path / "work"
    project_name = "P"
    project_dir = work_dir / project_name
    project_dir.mkdir(parents=True)
    get_db(project_dir)
    yield (work_dir, project_name)
    close_db(project_dir)


@pytest.fixture
def client(project):
    work_dir, _name = project
    app = create_app(work_dir=work_dir)
    return TestClient(app)


# ---------------------------------------------------------------------------
# GET /tracks + /audio-tracks
# ---------------------------------------------------------------------------


def test_list_tracks_returns_default_track(client, project):
    _, name = project
    r = client.get(f"/api/projects/{name}/tracks")
    assert r.status_code == 200, r.text
    body = r.json()
    assert "tracks" in body
    assert isinstance(body["tracks"], list)
    # Default track is created by get_db schema bootstrap.
    assert len(body["tracks"]) >= 1


def test_list_audio_tracks_empty_by_default(client, project):
    _, name = project
    r = client.get(f"/api/projects/{name}/audio-tracks")
    assert r.status_code == 200
    body = r.json()
    assert "audioTracks" in body
    assert isinstance(body["audioTracks"], list)


def test_list_tracks_unknown_project_404(client):
    r = client.get("/api/projects/nope/tracks")
    assert r.status_code == 404
    assert r.json()["error"] == "NOT_FOUND"


# ---------------------------------------------------------------------------
# POST /tracks/add|update|delete|reorder
# ---------------------------------------------------------------------------


def test_add_track_creates_row(client, project):
    work_dir, name = project
    r = client.post(
        f"/api/projects/{name}/tracks/add",
        json={"name": "My Track", "blend_mode": "normal"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["success"] is True
    assert body["id"].startswith("track_")

    tracks = db_get_tracks(work_dir / name)
    assert any(t["id"] == body["id"] and t["name"] == "My Track" for t in tracks)


def test_update_track_missing_id_400(client, project):
    _, name = project
    r = client.post(f"/api/projects/{name}/tracks/update", json={"name": "X"})
    assert r.status_code == 400
    body = r.json()
    assert body["error"] == "BAD_REQUEST"


def test_delete_track(client, project):
    work_dir, name = project
    add = client.post(f"/api/projects/{name}/tracks/add", json={"name": "doomed"})
    track_id = add.json()["id"]

    r = client.post(f"/api/projects/{name}/tracks/delete", json={"id": track_id})
    assert r.status_code == 200
    assert r.json()["success"] is True

    assert not any(t["id"] == track_id for t in db_get_tracks(work_dir / name))


def test_reorder_tracks(client, project):
    work_dir, name = project
    a = client.post(f"/api/projects/{name}/tracks/add", json={"name": "A"}).json()["id"]
    b = client.post(f"/api/projects/{name}/tracks/add", json={"name": "B"}).json()["id"]
    r = client.post(
        f"/api/projects/{name}/tracks/reorder",
        json={"trackIds": [b, a]},
    )
    assert r.status_code == 200
    assert r.json()["success"] is True


# ---------------------------------------------------------------------------
# POST /audio-tracks/add|update|delete|reorder
# ---------------------------------------------------------------------------


def test_add_audio_track_creates_row(client, project):
    work_dir, name = project
    r = client.post(
        f"/api/projects/{name}/audio-tracks/add",
        json={"name": "Vox", "muted": False},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["success"] is True
    assert body["id"].startswith("audio_track_")

    tracks = db_get_audio_tracks(work_dir / name)
    assert any(t["id"] == body["id"] for t in tracks)


def test_update_audio_track_missing_id_400(client, project):
    _, name = project
    r = client.post(f"/api/projects/{name}/audio-tracks/update", json={"name": "x"})
    assert r.status_code == 400


def test_delete_audio_track(client, project):
    work_dir, name = project
    add = client.post(f"/api/projects/{name}/audio-tracks/add", json={"name": "rm"})
    tid = add.json()["id"]

    r = client.post(f"/api/projects/{name}/audio-tracks/delete", json={"id": tid})
    assert r.status_code == 200
    assert r.json()["success"] is True
    assert not any(t["id"] == tid for t in db_get_audio_tracks(work_dir / name))


def test_reorder_audio_tracks(client, project):
    _, name = project
    a = client.post(f"/api/projects/{name}/audio-tracks/add", json={"name": "A"}).json()["id"]
    b = client.post(f"/api/projects/{name}/audio-tracks/add", json={"name": "B"}).json()["id"]
    r = client.post(
        f"/api/projects/{name}/audio-tracks/reorder",
        json={"trackIds": [b, a]},
    )
    assert r.status_code == 200


# ---------------------------------------------------------------------------
# operation_id alignment (chat-tool parity)
# ---------------------------------------------------------------------------


def test_operation_ids_in_openapi(client):
    resp = client.get("/openapi.json")
    assert resp.status_code == 200
    spec = resp.json()
    op_ids = {
        op["operationId"]
        for path in spec["paths"].values()
        for op in path.values()
        if isinstance(op, dict) and "operationId" in op
    }
    assert "add_audio_track" in op_ids  # chat-tool alignment
    assert "list_tracks" in op_ids
    assert "list_audio_tracks" in op_ids
    assert "add_track" in op_ids
    assert "update_track" in op_ids
    assert "delete_track" in op_ids
    assert "reorder_tracks" in op_ids
    assert "update_audio_track" in op_ids
    assert "delete_audio_track" in op_ids
    assert "reorder_audio_tracks" in op_ids
    assert "update_volume_curve" in op_ids  # chat-tool alignment


# ---------------------------------------------------------------------------
# volume-curve (chat-tool alignment)
# ---------------------------------------------------------------------------


def test_update_volume_curve_track(client, project):
    work_dir, name = project
    add = client.post(f"/api/projects/{name}/audio-tracks/add", json={"name": "v"})
    tid = add.json()["id"]
    r = client.post(
        f"/api/projects/{name}/audio-tracks/{tid}/volume-curve",
        json={"points": [[0.0, 1.0], [1.0, 0.5]], "interpolation": "linear"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body.get("ok") is True
    assert body.get("target_type") == "track"
    assert body.get("target_id") == tid


def test_update_volume_curve_points_required(client, project):
    _, name = project
    add = client.post(f"/api/projects/{name}/audio-tracks/add", json={"name": "vv"})
    tid = add.json()["id"]
    r = client.post(
        f"/api/projects/{name}/audio-tracks/{tid}/volume-curve",
        json={"interpolation": "linear"},
    )
    # Legacy _exec path returns a 400 envelope with "error" wording. We wrap
    # that in the BAD_REQUEST envelope.
    assert r.status_code == 400
