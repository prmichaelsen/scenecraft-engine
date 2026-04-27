"""M16 T62 tests for mix-render-upload, bounce-upload, bounce download, and
audio intelligence stubs (FastAPI).

Mirrors ``tests/test_mix_render_upload.py`` + ``tests/test_bounce_audio.py``
but drives the FastAPI app through ``TestClient`` instead of a real
``HTTPServer`` — that's the T62 entry point.
"""

from __future__ import annotations

import hashlib
import io
import wave
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from scenecraft.api.app import create_app
from scenecraft.db import close_db, get_db
from scenecraft.db_bounces import create_bounce


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def project(tmp_path: Path):
    work_dir = tmp_path / "work"
    name = "P"
    pd = work_dir / name
    pd.mkdir(parents=True)
    get_db(pd)
    yield (work_dir, name, pd)
    close_db(pd)


@pytest.fixture
def client(project):
    work_dir, _, _ = project
    return TestClient(create_app(work_dir=work_dir))


# ---------------------------------------------------------------------------
# WAV helper
# ---------------------------------------------------------------------------


def _wav(duration_s: float, sample_rate: int = 48000, channels: int = 2) -> bytes:
    n_frames = int(round(duration_s * sample_rate))
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(channels)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(b"\x00" * (n_frames * channels * 2))
    return buf.getvalue()


def _hash(n: int = 0) -> str:
    return hashlib.sha256(f"t-{n}".encode()).hexdigest()


# ---------------------------------------------------------------------------
# mix-render-upload
# ---------------------------------------------------------------------------


def test_mix_render_upload_valid(client, project):
    _, name, pd = project
    sr, ch, d = 48000, 2, 1.0
    h = _hash(1)
    files = {"audio": ("mix.wav", _wav(d), "audio/wav")}
    data = {
        "mix_graph_hash": h,
        "start_time_s": "0.0",
        "end_time_s": str(d),
        "sample_rate": str(sr),
        "channels": str(ch),
    }
    r = client.post(
        f"/api/projects/{name}/mix-render-upload", files=files, data=data
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["rendered_path"] == f"pool/mixes/{h}.wav"
    assert body["channels"] == ch
    assert body["sample_rate"] == sr

    dest = pd / "pool" / "mixes" / f"{h}.wav"
    assert dest.exists()


def test_mix_render_upload_missing_hash_400(client, project):
    _, name, _ = project
    r = client.post(
        f"/api/projects/{name}/mix-render-upload",
        files={"audio": ("m.wav", _wav(1.0), "audio/wav")},
        data={
            "start_time_s": "0",
            "end_time_s": "1",
            "sample_rate": "48000",
            "channels": "2",
        },
    )
    assert r.status_code == 400


def test_mix_render_upload_bad_hex_400(client, project):
    _, name, _ = project
    r = client.post(
        f"/api/projects/{name}/mix-render-upload",
        files={"audio": ("m.wav", _wav(1.0), "audio/wav")},
        data={
            "mix_graph_hash": "abc123",
            "start_time_s": "0",
            "end_time_s": "1",
            "sample_rate": "48000",
            "channels": "2",
        },
    )
    assert r.status_code == 400
    assert "64 hex" in r.json().get("message", "")


def test_mix_render_upload_duration_mismatch_deletes(client, project):
    _, name, pd = project
    h = _hash(2)
    r = client.post(
        f"/api/projects/{name}/mix-render-upload",
        files={"audio": ("m.wav", _wav(1.0), "audio/wav")},
        data={
            "mix_graph_hash": h,
            "start_time_s": "0",
            "end_time_s": "5",  # claims 5s, WAV is 1s
            "sample_rate": "48000",
            "channels": "2",
        },
    )
    assert r.status_code == 400
    dest = pd / "pool" / "mixes" / f"{h}.wav"
    assert not dest.exists()


# ---------------------------------------------------------------------------
# bounce-upload
# ---------------------------------------------------------------------------


def test_bounce_upload_valid(client, project):
    _, name, pd = project
    sr, ch, d = 48000, 2, 1.0
    h = _hash(3)
    r = client.post(
        f"/api/projects/{name}/bounce-upload",
        files={"audio": ("b.wav", _wav(d), "audio/wav")},
        data={
            "composite_hash": h,
            "start_time_s": "0",
            "end_time_s": str(d),
            "sample_rate": str(sr),
            "bit_depth": "16",
            "channels": str(ch),
        },
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["rendered_path"] == f"pool/bounces/{h}.wav"
    assert body["bit_depth"] == 16
    dest = pd / "pool" / "bounces" / f"{h}.wav"
    assert dest.exists()


def test_bounce_upload_invalid_bit_depth_400(client, project):
    _, name, _ = project
    r = client.post(
        f"/api/projects/{name}/bounce-upload",
        files={"audio": ("b.wav", _wav(0.5), "audio/wav")},
        data={
            "composite_hash": _hash(4),
            "start_time_s": "0",
            "end_time_s": "0.5",
            "sample_rate": "48000",
            "bit_depth": "17",  # not 16/24/32
            "channels": "2",
        },
    )
    assert r.status_code == 400


# ---------------------------------------------------------------------------
# bounce download
# ---------------------------------------------------------------------------


def test_bounce_download_happy_path(client, project):
    _, name, pd = project
    sr, ch, d = 48000, 2, 0.5
    h = _hash(5)
    # First upload
    r = client.post(
        f"/api/projects/{name}/bounce-upload",
        files={"audio": ("b.wav", _wav(d), "audio/wav")},
        data={
            "composite_hash": h,
            "start_time_s": "0",
            "end_time_s": str(d),
            "sample_rate": str(sr),
            "bit_depth": "16",
            "channels": str(ch),
        },
    )
    assert r.status_code == 201, r.text

    # Register a bounce row pointing at the uploaded file so the download
    # endpoint can resolve ID→file.
    bounce = create_bounce(
        pd,
        composite_hash=h,
        start_time_s=0.0,
        end_time_s=d,
        mode="full",
        selection={},
        bit_depth=16,
        sample_rate=sr,
        channels=ch,
        rendered_path=f"pool/bounces/{h}.wav",
    )

    r = client.get(f"/api/projects/{name}/bounces/{bounce.id}.wav")
    assert r.status_code == 200
    assert r.headers.get("content-type", "").startswith("audio/")
    cd = r.headers.get("content-disposition", "")
    assert "attachment" in cd
    assert f"{name}-{bounce.id}.wav" in cd
    assert len(r.content) > 0


def test_bounce_download_not_found_404(client, project):
    _, name, _ = project
    r = client.get(f"/api/projects/{name}/bounces/does_not_exist.wav")
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# audio intelligence stubs + isolations
# ---------------------------------------------------------------------------


def test_audio_intelligence_stub(client, project):
    _, name, _ = project
    r = client.get(f"/api/projects/{name}/audio-intelligence")
    assert r.status_code == 200
    body = r.json()
    assert body["activeFile"] is None
    assert body["events"] == []


def test_update_rules_stub(client, project):
    _, name, _ = project
    r = client.post(f"/api/projects/{name}/update-rules", json={})
    assert r.status_code == 200
    assert r.json()["success"] is True


def test_reapply_rules_stub(client, project):
    _, name, _ = project
    r = client.post(f"/api/projects/{name}/reapply-rules", json={})
    assert r.status_code == 200
    assert r.json()["eventCount"] == 0


def test_audio_isolations_requires_query_params(client, project):
    _, name, _ = project
    r = client.get(f"/api/projects/{name}/audio-isolations")
    assert r.status_code == 400


def test_audio_isolations_list_empty(client, project):
    _, name, _ = project
    r = client.get(
        f"/api/projects/{name}/audio-isolations?entityType=audio_clip&entityId=c1"
    )
    assert r.status_code == 200
    body = r.json()
    assert body == {"isolations": []}


# ---------------------------------------------------------------------------
# Operation IDs
# ---------------------------------------------------------------------------


def test_mix_bounce_operation_ids(client):
    spec = client.get("/openapi.json").json()
    ops = {
        op["operationId"]
        for path in spec["paths"].values()
        for op in path.values()
        if isinstance(op, dict) and "operationId" in op
    }
    assert "mix_render_upload" in ops
    assert "bounce_upload" in ops
    assert "download_bounce" in ops
    assert "list_master_bus_effects" in ops
    assert "get_audio_intelligence" in ops
    assert "list_audio_isolations" in ops
