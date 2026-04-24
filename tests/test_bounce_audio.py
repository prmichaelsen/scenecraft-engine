"""Tests for the ``bounce_audio`` chat tool + bounce-upload / bounces
download endpoints (M16).

Covers:
- Tool registration (TOOLS + schema + allowlist).
- Composite-hash determinism + sensitivity to every input factor.
- Validation (invalid bit_depth, mutually-exclusive selection, missing ids).
- Cache-miss → WS request → timeout path.
- Cache-miss → fake-frontend upload → tool completes.
- Cache-hit path: identical args return ``cached: True`` + same bounce_id.
- Mode detection (full / tracks / clips).
- GET /bounces/<id>.wav happy path, 404 on bad id, 404 on orphaned row.

HTTP round-trip tests mirror ``tests/test_mix_render_upload.py`` (real
HTTPServer + urllib). Chat-tool WS tests mirror
``tests/test_analyze_master_bus_roundtrip.py`` (FakeWs capturing ``send``
payloads).
"""

from __future__ import annotations

import asyncio
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

import numpy as np
import pytest
import soundfile as sf

from scenecraft.api_server import make_handler
from scenecraft.bounce_hash import compute_bounce_hash
from scenecraft.chat import (
    BOUNCE_AUDIO_TOOL,
    TOOLS,
    _exec_bounce_audio,
    _is_destructive,
    set_bounce_render_event,
)
from scenecraft.db import add_audio_track, close_db, get_db
from scenecraft.db_bounces import (
    create_bounce,
    get_bounce_by_hash,
    get_bounce_by_id,
)


STUB_HASH = "a" * 64


# ── Infrastructure ──────────────────────────────────────────────────


class FakeWs:
    """Async-ws stand-in capturing every send() payload."""

    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send(self, data: str) -> None:
        self.sent.append(json.loads(data))


@pytest.fixture(autouse=True)
def pin_mix_graph_hash(monkeypatch):
    """Pin compute_mix_graph_hash → fixed value so composite_hash is
    deterministic across tests without real project state."""
    import scenecraft.mix_graph_hash as mgh_mod
    monkeypatch.setattr(
        mgh_mod, "compute_mix_graph_hash", lambda _project_dir: STUB_HASH,
    )
    yield


@pytest.fixture
def project(tmp_path) -> Path:
    """Bare project with one track + one clip so end-time resolution works."""
    project_dir = tmp_path / "bounce_project"
    project_dir.mkdir()
    get_db(project_dir)
    add_audio_track(project_dir, {"id": "t1", "display_order": 0})
    conn = get_db(project_dir)
    conn.execute(
        "INSERT INTO audio_clips (id, track_id, source_path, start_time, end_time) "
        "VALUES (?, ?, ?, ?, ?)",
        ("c1", "t1", "dummy.wav", 0.0, 2.0),
    )
    conn.commit()
    yield project_dir
    close_db(project_dir)


# ── HTTP test server ────────────────────────────────────────────────


@pytest.fixture
def http_server(tmp_path):
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


def _make_http_project(work_dir: Path, name: str) -> Path:
    p = work_dir / name
    p.mkdir()
    get_db(p)
    close_db(p)
    return p


# ── WAV helpers ─────────────────────────────────────────────────────


def _write_sine_wav(path: Path, duration_s: float = 2.0, sr: int = 48000,
                    channels: int = 2) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    n = int(sr * duration_s)
    t = np.arange(n, dtype=np.float64) / sr
    tone = (0.2 * np.sin(2 * np.pi * 440.0 * t)).astype(np.float32)
    if channels == 2:
        y = np.stack([tone, tone], axis=1)
    else:
        y = tone
    sf.write(str(path), y, sr, subtype="PCM_16")


def _make_wav_bytes(duration_s: float, sample_rate: int = 48000,
                    channels: int = 2, sample_width: int = 2) -> bytes:
    n_frames = int(round(duration_s * sample_rate))
    buf = io.BytesIO()
    with wave.open(buf, 'wb') as wf:
        wf.setnchannels(channels)
        wf.setsampwidth(sample_width)
        wf.setframerate(sample_rate)
        wf.writeframes(b"\x00" * (n_frames * channels * sample_width))
    return buf.getvalue()


def _build_multipart(*, audio: bytes, composite_hash: str,
                     start_time_s: float, end_time_s: float,
                     sample_rate: int, bit_depth: int, channels: int,
                     request_id: str | None = None,
                     boundary: str = "----bounceboundary1234") -> tuple[bytes, str]:
    chunks: list[bytes] = []
    chunks.append(
        (f"--{boundary}\r\n"
         f'Content-Disposition: form-data; name="audio"; filename="b.wav"\r\n'
         "Content-Type: audio/wav\r\n"
         "\r\n").encode()
    )
    chunks.append(audio)
    chunks.append(b"\r\n")

    def _text_field(name: str, value: str) -> None:
        chunks.append(
            (f"--{boundary}\r\n"
             f'Content-Disposition: form-data; name="{name}"\r\n'
             "\r\n"
             f"{value}"
             "\r\n").encode()
        )

    _text_field("composite_hash", composite_hash)
    _text_field("start_time_s", str(start_time_s))
    _text_field("end_time_s", str(end_time_s))
    _text_field("sample_rate", str(sample_rate))
    _text_field("bit_depth", str(bit_depth))
    _text_field("channels", str(channels))
    if request_id is not None:
        _text_field("request_id", request_id)

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


def _get_raw(base: str, path: str) -> tuple[int, bytes, dict]:
    """GET returning (status, body_bytes, headers)."""
    req = urllib.request.Request(f"{base}{path}", method="GET")
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, r.read(), dict(r.headers)
    except urllib.error.HTTPError as e:
        return e.code, e.read(), dict(e.headers)


# ── 1. Registration ─────────────────────────────────────────────────


def test_bounce_audio_registered_in_tools():
    assert BOUNCE_AUDIO_TOOL["name"] == "bounce_audio"
    assert BOUNCE_AUDIO_TOOL in TOOLS

    schema = BOUNCE_AUDIO_TOOL["input_schema"]
    assert schema["type"] == "object"
    # Every parameter is optional — the tool resolves sensible defaults.
    assert schema["required"] == []
    props = schema["properties"]
    for key in ("start_time_s", "end_time_s", "track_ids", "clip_ids",
                "sample_rate", "bit_depth", "channels"):
        assert key in props, f"missing schema property: {key}"


# ── 2. Destructive check ────────────────────────────────────────────


def test_bounce_audio_is_not_destructive():
    # Despite the "bounce" substring not matching any destructive pattern,
    # we explicitly allowlist it so a future destructive pattern addition
    # doesn't silently start gating chat-tool calls.
    assert _is_destructive("bounce_audio") is False


# ── 3. Composite hash determinism ───────────────────────────────────


def test_compute_bounce_hash_deterministic(project):
    a = compute_bounce_hash(
        project, start_time_s=0.0, end_time_s=2.0, mode="full",
        track_ids=None, clip_ids=None,
        sample_rate=48000, bit_depth=24, channels=2,
    )
    b = compute_bounce_hash(
        project, start_time_s=0.0, end_time_s=2.0, mode="full",
        track_ids=None, clip_ids=None,
        sample_rate=48000, bit_depth=24, channels=2,
    )
    assert a == b
    assert len(a) == 64


@pytest.mark.parametrize("override", [
    {"start_time_s": 0.5},
    {"end_time_s": 3.0},
    {"mode": "tracks", "track_ids": ["t1"]},
    {"sample_rate": 44100},
    {"bit_depth": 16},
    {"channels": 1},
])
def test_compute_bounce_hash_changes_on_every_factor(project, override):
    base = dict(
        start_time_s=0.0, end_time_s=2.0, mode="full",
        track_ids=None, clip_ids=None,
        sample_rate=48000, bit_depth=24, channels=2,
    )
    changed = dict(base)
    changed.update(override)
    h1 = compute_bounce_hash(project, **base)
    h2 = compute_bounce_hash(project, **changed)
    assert h1 != h2, f"override {override} did not change the hash"


# ── 4. Mutual exclusion ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_mutual_exclusion_error(project):
    result = await _exec_bounce_audio(
        project,
        {"track_ids": ["t1"], "clip_ids": ["c1"]},
        ws=None, timeout_s=0.5,
    )
    assert "error" in result
    assert "track_ids" in result["error"] and "clip_ids" in result["error"]


# ── 5. Invalid bit depth ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_invalid_bit_depth(project):
    result = await _exec_bounce_audio(
        project, {"bit_depth": 8}, ws=None, timeout_s=0.5,
    )
    assert "error" in result
    assert "bit_depth" in result["error"]


@pytest.mark.asyncio
async def test_invalid_sample_rate(project):
    result = await _exec_bounce_audio(
        project, {"sample_rate": 22050}, ws=None, timeout_s=0.5,
    )
    assert "error" in result
    assert "sample_rate" in result["error"]


# ── 6. Nonexistent selection ids ────────────────────────────────────


@pytest.mark.asyncio
async def test_nonexistent_track_id(project):
    result = await _exec_bounce_audio(
        project, {"track_ids": ["t_does_not_exist"]}, ws=None, timeout_s=0.5,
    )
    assert "error" in result
    assert "t_does_not_exist" in result["error"]


@pytest.mark.asyncio
async def test_nonexistent_clip_id(project):
    result = await _exec_bounce_audio(
        project, {"clip_ids": ["c_bogus"]}, ws=None, timeout_s=0.5,
    )
    assert "error" in result
    assert "c_bogus" in result["error"]


# ── 7. Cache miss → WS → timeout ────────────────────────────────────


@pytest.mark.asyncio
async def test_cache_miss_emits_ws_and_times_out(project):
    ws = FakeWs()
    result = await _exec_bounce_audio(
        project, {}, ws=ws, project_name="bounce_project", timeout_s=0.15,
    )
    assert "error" in result
    assert "timeout" in result["error"].lower()

    # WS request was emitted with the expected shape.
    assert len(ws.sent) == 1
    msg = ws.sent[0]
    assert msg["type"] == "bounce_audio_request"
    assert msg["composite_hash"] == result["composite_hash"]
    assert msg["mode"] == "full"
    assert msg["sample_rate"] == 48000
    assert msg["bit_depth"] == 24
    assert msg["channels"] == 2
    assert "request_id" in msg
    assert "bounce_id" in msg

    # Event cleaned up (no leak in the registry).
    from scenecraft.chat import _BOUNCE_RENDER_EVENTS
    assert msg["request_id"] not in _BOUNCE_RENDER_EVENTS

    # Timed-out bounce row is cleaned up so the UNIQUE hash is available
    # for a retry. (We can't look it up by id — the id was only in the
    # WS message — so lookup by hash.)
    assert get_bounce_by_hash(project, msg["composite_hash"]) is None


# ── 8. Happy path: fake frontend uploads ────────────────────────────


@pytest.mark.asyncio
async def test_cache_miss_then_upload_completes(project):
    ws = FakeWs()

    async def fake_frontend() -> None:
        # Wait until the tool has emitted its request.
        for _ in range(100):
            if ws.sent:
                break
            await asyncio.sleep(0.005)
        assert ws.sent, "tool never emitted bounce_audio_request"
        msg = ws.sent[-1]
        assert msg["type"] == "bounce_audio_request"

        # "Render" — write the WAV where the tool expects it.
        wav_path = project / "pool" / "bounces" / f"{msg['composite_hash']}.wav"
        _write_sine_wav(wav_path, duration_s=2.0)

        ok = set_bounce_render_event(msg["request_id"])
        assert ok

    bounce_coro = _exec_bounce_audio(
        project, {}, ws=ws, project_name="bounce_project", timeout_s=5.0,
    )
    result, _ = await asyncio.gather(bounce_coro, fake_frontend())

    assert "error" not in result, result
    assert result["cached"] is False
    assert result["mode"] == "full"
    assert result["tracks_requested"] == []
    assert result["clips_requested"] == []
    assert result["size_bytes"] > 0
    assert abs(result["duration_s"] - 2.0) < 0.1
    assert result["rendered_path"].startswith("pool/bounces/")
    # download_url format: /api/projects/<name>/bounces/<id>.wav
    assert result["download_url"].startswith("/api/projects/bounce_project/bounces/")
    assert result["download_url"].endswith(".wav")
    assert result["bounce_id"] in result["download_url"]

    # The DB row should now have rendered_path populated.
    bounce = get_bounce_by_id(project, result["bounce_id"])
    assert bounce is not None
    assert bounce.rendered_path is not None
    assert bounce.size_bytes == result["size_bytes"]


# ── 9. Cache hit ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_cache_hit_returns_same_bounce_id(project):
    """After one successful render, the next call with identical args
    returns ``cached: True`` and the same bounce_id — no WS emitted."""
    ws1 = FakeWs()

    async def fake_frontend() -> None:
        for _ in range(100):
            if ws1.sent:
                break
            await asyncio.sleep(0.005)
        msg = ws1.sent[-1]
        wav_path = project / "pool" / "bounces" / f"{msg['composite_hash']}.wav"
        _write_sine_wav(wav_path, duration_s=2.0)
        set_bounce_render_event(msg["request_id"])

    first_coro = _exec_bounce_audio(
        project, {}, ws=ws1, project_name="bounce_project", timeout_s=5.0,
    )
    first, _ = await asyncio.gather(first_coro, fake_frontend())
    assert first["cached"] is False
    bid = first["bounce_id"]

    # Second call: cache hit. WS must NOT be emitted.
    ws2 = FakeWs()
    second = await _exec_bounce_audio(
        project, {}, ws=ws2, project_name="bounce_project", timeout_s=5.0,
    )
    assert second["cached"] is True
    assert second["bounce_id"] == bid
    assert ws2.sent == []


# ── 10. Mode detection ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_mode_full_with_no_selection(project):
    ws = FakeWs()
    # Don't supply a ws; we just want the mode to surface in the error path.
    result = await _exec_bounce_audio(
        project, {}, ws=None, project_name="bounce_project", timeout_s=0.1,
    )
    # ws=None + missing WAV returns an error, but composite_hash tells us
    # the mode that was hashed. Recompute for mode=full and compare.
    expected = compute_bounce_hash(
        project,
        start_time_s=0.0, end_time_s=2.0, mode="full",
        track_ids=None, clip_ids=None,
        sample_rate=48000, bit_depth=24, channels=2,
    )
    assert result.get("composite_hash") == expected


@pytest.mark.asyncio
async def test_mode_tracks_when_track_ids_given(project):
    result = await _exec_bounce_audio(
        project, {"track_ids": ["t1"]},
        ws=None, project_name="bounce_project", timeout_s=0.1,
    )
    expected = compute_bounce_hash(
        project,
        start_time_s=0.0, end_time_s=2.0, mode="tracks",
        track_ids=["t1"], clip_ids=None,
        sample_rate=48000, bit_depth=24, channels=2,
    )
    assert result.get("composite_hash") == expected


@pytest.mark.asyncio
async def test_mode_clips_when_clip_ids_given(project):
    result = await _exec_bounce_audio(
        project, {"clip_ids": ["c1"]},
        ws=None, project_name="bounce_project", timeout_s=0.1,
    )
    expected = compute_bounce_hash(
        project,
        start_time_s=0.0, end_time_s=2.0, mode="clips",
        track_ids=None, clip_ids=["c1"],
        sample_rate=48000, bit_depth=24, channels=2,
    )
    assert result.get("composite_hash") == expected


# ── 11. Download endpoint happy path ────────────────────────────────


def test_download_returns_wav_with_correct_headers(http_server):
    project = _make_http_project(http_server["work_dir"], "dl")
    h = hashlib.sha256(b"dl-test").hexdigest()
    # Write the WAV directly to disk and insert a bounce row pointing at it.
    wav_path = project / "pool" / "bounces" / f"{h}.wav"
    _write_sine_wav(wav_path, duration_s=1.0, sr=48000, channels=2)
    size = wav_path.stat().st_size

    from scenecraft.db_bounces import update_bounce_rendered
    bounce = create_bounce(
        project, composite_hash=h,
        start_time_s=0.0, end_time_s=1.0, mode="full",
        selection={}, sample_rate=48000, bit_depth=16, channels=2,
    )
    update_bounce_rendered(
        project, bounce.id, f"pool/bounces/{h}.wav", size, 1.0,
    )
    close_db(project)

    status, body, headers = _get_raw(
        http_server["base"], f"/api/projects/dl/bounces/{bounce.id}.wav",
    )
    assert status == 200
    assert headers.get("Content-Type") == "audio/wav"
    assert int(headers.get("Content-Length", "0")) == size
    disp = headers.get("Content-Disposition", "")
    assert "attachment" in disp
    assert f"dl-{bounce.id}.wav" in disp
    # Bytes match disk.
    assert body == wav_path.read_bytes()


# ── 12. Download endpoint 404s ──────────────────────────────────────


def test_download_bogus_id_returns_404(http_server):
    _make_http_project(http_server["work_dir"], "dl2")
    status, body, _ = _get_raw(
        http_server["base"], "/api/projects/dl2/bounces/bounce_nosuch.wav",
    )
    assert status == 404


def test_download_missing_file_returns_404(http_server):
    """Bounce row exists (e.g. rendered_path set) but the WAV was
    deleted from disk — the download endpoint must 404 rather than 500."""
    project = _make_http_project(http_server["work_dir"], "dl3")
    h = hashlib.sha256(b"orphan").hexdigest()
    bounce = create_bounce(
        project, composite_hash=h,
        start_time_s=0.0, end_time_s=1.0, mode="full",
        selection={}, sample_rate=48000, bit_depth=24, channels=2,
        rendered_path=f"pool/bounces/{h}.wav",
        size_bytes=1234, duration_s=1.0,
    )
    close_db(project)
    # Note: we intentionally did NOT write the WAV to pool/bounces/.

    status, body, _ = _get_raw(
        http_server["base"], f"/api/projects/dl3/bounces/{bounce.id}.wav",
    )
    assert status == 404


# ── 13. Upload endpoint end-to-end ──────────────────────────────────


def test_upload_endpoint_writes_wav_and_returns_201(http_server):
    project = _make_http_project(http_server["work_dir"], "up1")
    sr, ch = 48000, 2
    duration = 0.5
    wav = _make_wav_bytes(duration, sample_rate=sr, channels=ch)
    h = hashlib.sha256(b"upload-test").hexdigest()

    body, boundary = _build_multipart(
        audio=wav, composite_hash=h,
        start_time_s=0.0, end_time_s=duration,
        sample_rate=sr, bit_depth=16, channels=ch,
    )
    status, resp = _post_multipart(
        http_server["base"], "/api/projects/up1/bounce-upload", body, boundary,
    )
    assert status == 201, resp
    assert resp["rendered_path"] == f"pool/bounces/{h}.wav"
    assert resp["channels"] == ch
    assert resp["sample_rate"] == sr
    assert resp["bit_depth"] == 16
    assert resp["bytes"] == len(wav)
    # File landed on disk.
    assert (project / "pool" / "bounces" / f"{h}.wav").exists()
