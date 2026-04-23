"""Integration tests for the mix-render-upload endpoint.

Spins up the API server on a random port and exercises the WAV-upload flow
used by the frontend's OfflineAudioContext renderer (M15). The endpoint
stores content-addressable WAVs under pool/mixes/<hash>.wav so the
analyze_master_bus tool can read them back.
"""

from __future__ import annotations

import hashlib
import io
import json
import socket
import threading
import time
import urllib.error
import urllib.request
import wave
from http.server import HTTPServer
from pathlib import Path

import pytest

from scenecraft.api_server import make_handler


# ── Fixtures ────────────────────────────────────────────────────────


@pytest.fixture
def server(tmp_path):
    work_dir = tmp_path / "work"
    work_dir.mkdir()

    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()

    Handler = make_handler(work_dir)
    httpd = HTTPServer(("127.0.0.1", port), Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    time.sleep(0.05)

    yield {"port": port, "work_dir": work_dir, "base": f"http://127.0.0.1:{port}"}

    httpd.shutdown()
    httpd.server_close()


def _make_project(work_dir: Path, name: str = "mixproj") -> Path:
    p = work_dir / name
    p.mkdir()
    from scenecraft.db import close_db, get_db
    get_db(p)
    close_db(p)
    return p


# ── WAV + multipart helpers ─────────────────────────────────────────


def _make_wav_bytes(duration_s: float, sample_rate: int = 48000,
                    channels: int = 2, sample_width: int = 2) -> bytes:
    """Build a valid PCM WAV file in memory. Silence (all zeros) is fine —
    the endpoint only inspects the header and duration, not the samples.
    """
    n_frames = int(round(duration_s * sample_rate))
    buf = io.BytesIO()
    with wave.open(buf, 'wb') as wf:
        wf.setnchannels(channels)
        wf.setsampwidth(sample_width)
        wf.setframerate(sample_rate)
        wf.writeframes(b"\x00" * (n_frames * channels * sample_width))
    return buf.getvalue()


def _hex_hash(n: int = 0) -> str:
    """Deterministic 64-char hex SHA-256 for test keys."""
    return hashlib.sha256(f"mix-{n}".encode()).hexdigest()


def _build_multipart(*, audio: bytes | None, mix_graph_hash: str | None,
                     start_time_s: float | None, end_time_s: float | None,
                     sample_rate: int | None, channels: int | None,
                     boundary: str = "----mixboundary9999",
                     audio_filename: str = "mix.wav") -> tuple[bytes, str]:
    """Assemble a multipart/form-data body. Any field set to None is omitted,
    letting tests assert 400 behavior for missing fields.
    """
    chunks: list[bytes] = []
    if audio is not None:
        chunks.append(
            (f"--{boundary}\r\n"
             f'Content-Disposition: form-data; name="audio"; filename="{audio_filename}"\r\n'
             "Content-Type: audio/wav\r\n"
             "\r\n").encode()
        )
        chunks.append(audio)
        chunks.append(b"\r\n")

    def _text_field(name: str, value: str):
        chunks.append(
            (f"--{boundary}\r\n"
             f'Content-Disposition: form-data; name="{name}"\r\n'
             "\r\n"
             f"{value}"
             "\r\n").encode()
        )

    if mix_graph_hash is not None:
        _text_field("mix_graph_hash", mix_graph_hash)
    if start_time_s is not None:
        _text_field("start_time_s", str(start_time_s))
    if end_time_s is not None:
        _text_field("end_time_s", str(end_time_s))
    if sample_rate is not None:
        _text_field("sample_rate", str(sample_rate))
    if channels is not None:
        _text_field("channels", str(channels))

    chunks.append(f"--{boundary}--\r\n".encode())
    return b"".join(chunks), boundary


def _post_multipart(base: str, path: str, body: bytes,
                    boundary: str) -> tuple[int, dict]:
    req = urllib.request.Request(
        f"{base}{path}",
        data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        raw = e.read().decode()
        try:
            return e.code, json.loads(raw)
        except Exception:
            return e.code, {"raw": raw}


# ── Tests ───────────────────────────────────────────────────────────


def test_valid_upload_writes_file_and_returns_201(server):
    project = _make_project(server["work_dir"], "p1")
    sr, ch = 48000, 2
    duration = 1.5
    wav = _make_wav_bytes(duration, sample_rate=sr, channels=ch)
    h = _hex_hash(1)

    body, boundary = _build_multipart(
        audio=wav, mix_graph_hash=h,
        start_time_s=10.0, end_time_s=10.0 + duration,
        sample_rate=sr, channels=ch,
    )
    status, resp = _post_multipart(
        server["base"], "/api/projects/p1/mix-render-upload", body, boundary,
    )

    assert status == 201, resp
    assert resp["rendered_path"] == f"pool/mixes/{h}.wav"
    assert resp["channels"] == ch
    assert resp["sample_rate"] == sr
    assert resp["bytes"] == len(wav)
    assert abs(resp["duration_s"] - duration) < 0.01

    # File landed on disk at the expected content-addressable location
    dest = project / "pool" / "mixes" / f"{h}.wav"
    assert dest.exists()
    assert dest.stat().st_size == len(wav)


def test_missing_mix_graph_hash_returns_400(server):
    _make_project(server["work_dir"], "p2")
    wav = _make_wav_bytes(1.0)
    body, boundary = _build_multipart(
        audio=wav, mix_graph_hash=None,
        start_time_s=0.0, end_time_s=1.0,
        sample_rate=48000, channels=2,
    )
    status, resp = _post_multipart(
        server["base"], "/api/projects/p2/mix-render-upload", body, boundary,
    )
    assert status == 400, resp
    assert "mix_graph_hash" in resp.get("error", "").lower()


def test_bad_hex_hash_returns_400(server):
    _make_project(server["work_dir"], "p3")
    wav = _make_wav_bytes(1.0)

    # Too short
    body, boundary = _build_multipart(
        audio=wav, mix_graph_hash="abc123",
        start_time_s=0.0, end_time_s=1.0,
        sample_rate=48000, channels=2,
    )
    status, resp = _post_multipart(
        server["base"], "/api/projects/p3/mix-render-upload", body, boundary,
    )
    assert status == 400, resp
    assert "64 hex" in resp.get("error", "")

    # Right length but non-hex characters
    bad_hash = "z" * 64
    body, boundary = _build_multipart(
        audio=wav, mix_graph_hash=bad_hash,
        start_time_s=0.0, end_time_s=1.0,
        sample_rate=48000, channels=2,
    )
    status, resp = _post_multipart(
        server["base"], "/api/projects/p3/mix-render-upload", body, boundary,
    )
    assert status == 400, resp


def test_duration_mismatch_rejects_and_deletes(server):
    project = _make_project(server["work_dir"], "p4")
    wav = _make_wav_bytes(1.0, sample_rate=48000, channels=2)
    h = _hex_hash(4)

    # Claim 5s but send 1s — should be rejected and NOT persisted
    body, boundary = _build_multipart(
        audio=wav, mix_graph_hash=h,
        start_time_s=0.0, end_time_s=5.0,
        sample_rate=48000, channels=2,
    )
    status, resp = _post_multipart(
        server["base"], "/api/projects/p4/mix-render-upload", body, boundary,
    )
    assert status == 400, resp
    assert "duration" in resp.get("error", "").lower()

    dest = project / "pool" / "mixes" / f"{h}.wav"
    assert not dest.exists(), "file should be deleted on duration mismatch"


def test_overwrite_same_hash_is_idempotent(server):
    project = _make_project(server["work_dir"], "p5")
    sr, ch = 48000, 2
    duration = 0.5
    wav = _make_wav_bytes(duration, sample_rate=sr, channels=ch)
    h = _hex_hash(5)

    for _ in range(2):
        body, boundary = _build_multipart(
            audio=wav, mix_graph_hash=h,
            start_time_s=0.0, end_time_s=duration,
            sample_rate=sr, channels=ch,
        )
        status, resp = _post_multipart(
            server["base"], "/api/projects/p5/mix-render-upload", body, boundary,
        )
        assert status == 201, resp

    dest = project / "pool" / "mixes" / f"{h}.wav"
    assert dest.exists()


def test_creates_mixes_dir_on_first_upload(server):
    project = _make_project(server["work_dir"], "p6")
    # Confirm the directory does not pre-exist — the handler must create it.
    assert not (project / "pool" / "mixes").exists()

    sr, ch = 48000, 2
    duration = 0.25
    wav = _make_wav_bytes(duration, sample_rate=sr, channels=ch)
    h = _hex_hash(6)

    body, boundary = _build_multipart(
        audio=wav, mix_graph_hash=h,
        start_time_s=0.0, end_time_s=duration,
        sample_rate=sr, channels=ch,
    )
    status, resp = _post_multipart(
        server["base"], "/api/projects/p6/mix-render-upload", body, boundary,
    )
    assert status == 201, resp
    assert (project / "pool" / "mixes").is_dir()


def test_sample_rate_mismatch_rejects(server):
    project = _make_project(server["work_dir"], "p7")
    # WAV is 48000 but form claims 44100
    wav = _make_wav_bytes(0.5, sample_rate=48000, channels=2)
    h = _hex_hash(7)

    body, boundary = _build_multipart(
        audio=wav, mix_graph_hash=h,
        start_time_s=0.0, end_time_s=0.5,
        sample_rate=44100, channels=2,
    )
    status, resp = _post_multipart(
        server["base"], "/api/projects/p7/mix-render-upload", body, boundary,
    )
    assert status == 400, resp
    assert "sample_rate" in resp.get("error", "").lower()

    dest = project / "pool" / "mixes" / f"{h}.wav"
    assert not dest.exists(), "file should be deleted on sample_rate mismatch"
