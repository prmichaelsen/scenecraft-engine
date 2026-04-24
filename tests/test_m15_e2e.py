"""End-to-end integration test for the M15 master-bus analysis pipeline (task-8).

Drives the FULL backend flow:
    chat-tool invocation → mix-render-upload HTTP → pool/mixes/<hash>.wav →
    _exec_analyze_master_bus → mix_analysis_runs / datapoints / sections / scalars.

The frontend's OfflineAudioContext render is *mocked* by POSTing a synthesized
WAV to the real ``/api/projects/<name>/mix-render-upload`` endpoint running on
a spawned HTTPServer (mirroring ``tests/test_mix_render_upload.py``). The WS
round-trip and stub value of ``compute_mix_graph_hash`` are intentionally
NOT monkeypatched here — this test exercises the real hash over the real
schema, which is exactly what task-8 asks for.

NB: on this branch, ``_exec_analyze_master_bus`` still has the TODO for the
WS round-trip (sibling branch ``m15-ws-wiring-be`` ships the async wiring);
when the WAV is missing, the tool returns ``{"error": "rendered mix WAV not
found..."}`` — we use that as a positive signal in the invalidation test.
"""

from __future__ import annotations

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

from scenecraft.api_server import make_handler
from scenecraft.chat import _exec_analyze_master_bus
from scenecraft.db import (
    add_audio_clip,
    add_audio_track,
    add_track_effect,
    close_db,
    get_db,
)
from scenecraft.db_mix_cache import (
    delete_mix_run,
    query_mix_datapoints,
    query_mix_sections,
)
from scenecraft.mix_graph_hash import compute_mix_graph_hash


# ── HTTP harness (mirrors test_mix_render_upload.py) ───────────────────────


@pytest.fixture
def server(tmp_path):
    """Spawn api_server on a random port; yield {port, work_dir, base}."""
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


def _build_multipart(*, audio: bytes, mix_graph_hash: str,
                     start_time_s: float, end_time_s: float,
                     sample_rate: int, channels: int,
                     boundary: str = "----e2eboundary9999") -> tuple[bytes, str]:
    chunks: list[bytes] = []
    chunks.append(
        (f"--{boundary}\r\n"
         f'Content-Disposition: form-data; name="audio"; filename="mix.wav"\r\n'
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

    _text_field("mix_graph_hash", mix_graph_hash)
    _text_field("start_time_s", str(start_time_s))
    _text_field("end_time_s", str(end_time_s))
    _text_field("sample_rate", str(sample_rate))
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
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        raw = e.read().decode()
        try:
            return e.code, json.loads(raw)
        except Exception:
            return e.code, {"raw": raw}


# ── WAV helpers ────────────────────────────────────────────────────────────


def _sine_stereo(duration_s: float, freq: float = 440.0, amp: float = 0.5,
                 sr: int = 48000) -> np.ndarray:
    """Generate a stereo float32 sine-wave array shape (N, 2)."""
    n = int(sr * duration_s)
    t = np.arange(n, dtype=np.float64) / sr
    y = (amp * np.sin(2 * np.pi * freq * t)).astype(np.float32)
    return np.stack([y, y], axis=1)


def _float_to_pcm16_wav_bytes(y: np.ndarray, sr: int) -> bytes:
    """Encode (N,) or (N, C) float32 audio to 16-bit PCM WAV bytes.

    Uses Python's stdlib ``wave`` module because the mix-render-upload
    endpoint reads the file with ``wave`` — matching encodings avoids any
    subtle pyloudnorm / soundfile bit-depth surprises.
    """
    if y.ndim == 1:
        channels = 1
        interleaved = y
    else:
        channels = y.shape[1]
        interleaved = y.reshape(-1)  # already interleaved because axis=1
    # Clip & convert to int16
    clipped = np.clip(interleaved, -1.0, 0.999969).astype(np.float64)
    pcm = (clipped * 32767.0).astype(np.int16).tobytes()

    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(channels)
        wf.setsampwidth(2)
        wf.setframerate(sr)
        wf.writeframes(pcm)
    return buf.getvalue()


def _upload_wav(server: dict, project_name: str, y: np.ndarray, *,
                mix_graph_hash: str, duration_s: float, sr: int = 48000,
                channels: int = 2) -> dict:
    """Post a synthesized WAV to the endpoint; assert 201 and return resp."""
    wav_bytes = _float_to_pcm16_wav_bytes(y, sr)
    body, boundary = _build_multipart(
        audio=wav_bytes, mix_graph_hash=mix_graph_hash,
        start_time_s=0.0, end_time_s=duration_s,
        sample_rate=sr, channels=channels,
    )
    status, resp = _post_multipart(
        server["base"],
        f"/api/projects/{project_name}/mix-render-upload",
        body, boundary,
    )
    assert status == 201, f"upload failed {status} {resp}"
    return resp


# ── Project seeding ────────────────────────────────────────────────────────


def _make_project(work_dir: Path, name: str) -> Path:
    p = work_dir / name
    p.mkdir()
    get_db(p)  # force schema + default send buses
    # Close the connection immediately; get_db caches by path and a lingering
    # cached handle can hold the file open when the HTTP server opens its own.
    close_db(p)
    return p


def _seed_basic_mix(project_dir: Path) -> tuple[str, str]:
    """Seed one track + one clip (+ one effect) so the hash is non-trivial."""
    track_id = "track_e2e_1"
    add_audio_track(project_dir, {
        "id": track_id,
        "name": "Main",
        "display_order": 0,
        "muted": False,
        "solo": False,
        "volume_curve": [[0.0, 0.8], [1.0, 0.8]],
    })
    clip_id = "clip_e2e_1"
    add_audio_clip(project_dir, {
        "id": clip_id,
        "track_id": track_id,
        "source_path": "pool/segments/placeholder.wav",
        "start_time": 0.0,
        "end_time": 3.0,  # drives default end_time_s resolution
        "source_offset": 0.0,
        "volume_curve": [[0.0, 0.7], [1.0, 0.7]],
        "muted": False,
    })
    add_track_effect(
        project_dir,
        track_id=track_id,
        effect_type="drive",
        static_params={"gain_db": 3.0, "tone": 0.5},
    )
    close_db(project_dir)
    return track_id, clip_id


# ═══════════════════════════════════════════════════════════════════════════
# Scenario 1 — Happy path: full pipeline
# ═══════════════════════════════════════════════════════════════════════════


def test_happy_path_full_pipeline(server):
    project_name = "e2e_p1"
    project_dir = _make_project(server["work_dir"], project_name)
    _seed_basic_mix(project_dir)

    # 1) Compute the real hash over the real mix graph.
    mix_hash = compute_mix_graph_hash(project_dir)
    assert isinstance(mix_hash, str) and len(mix_hash) == 64

    # 2) Synthesize 3s 440Hz sine @ 0.5 amp stereo 48k and upload it through
    # the real HTTP endpoint — this is the "frontend rendered and sent" stub.
    y = _sine_stereo(3.0, freq=440.0, amp=0.5, sr=48000)
    upload_resp = _upload_wav(
        server, project_name, y,
        mix_graph_hash=mix_hash, duration_s=3.0, sr=48000, channels=2,
    )
    assert upload_resp["rendered_path"] == f"pool/mixes/{mix_hash}.wav"

    # 3) File landed on disk where the analyzer expects it.
    dest = project_dir / "pool" / "mixes" / f"{mix_hash}.wav"
    assert dest.exists()

    # 4) Call the chat tool directly (no WS); it should read the WAV and
    # persist results.
    result = _exec_analyze_master_bus(project_dir, {})
    assert "error" not in result, result
    assert result["cached"] is False
    assert result["mix_graph_hash"] == mix_hash
    assert result["rendered_path"] == f"pool/mixes/{mix_hash}.wav"

    # Scalars — amp=0.5 sine → peak ~ -6dB; pyloudnorm defined; no clipping.
    scalars = result["scalars"]
    assert scalars["peak_db"] == pytest.approx(-6.02, abs=0.3)
    assert "lufs_integrated" in scalars
    assert -20.0 < scalars["lufs_integrated"] < 0.0
    assert scalars["clip_count"] == 0.0
    assert "dynamic_range_db" in scalars
    assert scalars["dynamic_range_db"] > 0

    # No clipping sections were persisted.
    run_id = result["run_id"]
    clips = query_mix_sections(project_dir, run_id, "clipping_event")
    assert clips == []

    # RMS + spectral_centroid datapoints were persisted.
    rms_rows = query_mix_datapoints(project_dir, run_id, "rms")
    cent_rows = query_mix_datapoints(project_dir, run_id, "spectral_centroid")
    assert len(rms_rows) > 10
    assert len(cent_rows) > 10

    # Exactly one run row with rendered_path set.
    conn = get_db(project_dir)
    rows = conn.execute(
        "SELECT id, rendered_path FROM mix_analysis_runs"
    ).fetchall()
    assert len(rows) == 1
    assert rows[0]["rendered_path"] == f"pool/mixes/{mix_hash}.wav"
    assert rows[0]["id"] == run_id


# ═══════════════════════════════════════════════════════════════════════════
# Scenario 2 — Cache hit on second call
# ═══════════════════════════════════════════════════════════════════════════


def test_cache_hit_on_second_call(server):
    project_name = "e2e_p2"
    project_dir = _make_project(server["work_dir"], project_name)
    _seed_basic_mix(project_dir)
    mix_hash = compute_mix_graph_hash(project_dir)

    y = _sine_stereo(3.0, freq=440.0, amp=0.5, sr=48000)
    _upload_wav(server, project_name, y,
                mix_graph_hash=mix_hash, duration_s=3.0)

    first = _exec_analyze_master_bus(project_dir, {})
    assert first["cached"] is False
    second = _exec_analyze_master_bus(project_dir, {})
    assert second["cached"] is True
    assert second["run_id"] == first["run_id"]

    # Only one run row in mix_analysis_runs.
    conn = get_db(project_dir)
    count = conn.execute("SELECT COUNT(*) FROM mix_analysis_runs").fetchone()[0]
    assert count == 1


# ═══════════════════════════════════════════════════════════════════════════
# Scenario 3 — Hash invalidation on project mutation
# ═══════════════════════════════════════════════════════════════════════════


def test_hash_invalidates_on_project_mutation(server):
    project_name = "e2e_p3"
    project_dir = _make_project(server["work_dir"], project_name)
    _seed_basic_mix(project_dir)

    hash_a = compute_mix_graph_hash(project_dir)
    y = _sine_stereo(3.0, freq=440.0, amp=0.5, sr=48000)
    _upload_wav(server, project_name, y,
                mix_graph_hash=hash_a, duration_s=3.0)

    r1 = _exec_analyze_master_bus(project_dir, {})
    assert r1["cached"] is False
    run_id_a = r1["run_id"]

    # Mutate the mix graph — add a new audio track. This MUST shift the hash.
    add_audio_track(project_dir, {
        "id": "track_e2e_2",
        "name": "Second",
        "display_order": 1,
        "muted": False,
        "solo": False,
        "volume_curve": [[0.0, 0.5], [1.0, 0.5]],
    })
    close_db(project_dir)
    hash_b = compute_mix_graph_hash(project_dir)
    assert hash_b != hash_a

    # Without uploading a new WAV, the analyzer resolves the NEW hash,
    # finds no WAV at pool/mixes/<hash_b>.wav, and returns an error. This
    # proves the cache key correctly invalidates on project mutation.
    r2 = _exec_analyze_master_bus(project_dir, {})
    assert "error" in r2, r2
    assert "rendered mix WAV not found" in r2["error"]
    assert r2["mix_graph_hash"] == hash_b

    # Upload a fresh WAV at the new hash and re-analyze.
    _upload_wav(server, project_name, y,
                mix_graph_hash=hash_b, duration_s=3.0)
    r3 = _exec_analyze_master_bus(project_dir, {})
    assert "error" not in r3, r3
    assert r3["cached"] is False
    assert r3["mix_graph_hash"] == hash_b
    assert r3["run_id"] != run_id_a

    # Now there should be two runs — one per hash.
    conn = get_db(project_dir)
    rows = conn.execute(
        "SELECT mix_graph_hash FROM mix_analysis_runs ORDER BY created_at"
    ).fetchall()
    assert [r["mix_graph_hash"] for r in rows] == [hash_a, hash_b]


# ═══════════════════════════════════════════════════════════════════════════
# Scenario 4 — Clipping detection roundtrip
# ═══════════════════════════════════════════════════════════════════════════


def test_clipping_detection_roundtrip(server):
    project_name = "e2e_p4"
    project_dir = _make_project(server["work_dir"], project_name)
    _seed_basic_mix(project_dir)
    mix_hash = compute_mix_graph_hash(project_dir)

    # 3s of -12dB sine + ~10 full-scale samples near t=1.0s.
    sr = 48000
    y = _sine_stereo(3.0, freq=440.0, amp=0.25, sr=sr).copy()
    injection_start = sr  # t ≈ 1.0s
    y[injection_start:injection_start + 10, :] = 0.999
    _upload_wav(server, project_name, y,
                mix_graph_hash=mix_hash, duration_s=3.0, sr=sr)

    result = _exec_analyze_master_bus(project_dir, {})
    assert "error" not in result, result
    assert result["clipping_events"] >= 1
    assert result["scalars"]["clip_count"] >= 1.0

    # Section in the DB should overlap t=1.0s.
    sections = query_mix_sections(project_dir, result["run_id"], "clipping_event")
    assert len(sections) >= 1
    assert any(s.start_s < 1.05 and s.end_s > 0.95 for s in sections), [
        (s.start_s, s.end_s) for s in sections
    ]


# ═══════════════════════════════════════════════════════════════════════════
# Scenario 5 — force_rerun via chat-tool input
# ═══════════════════════════════════════════════════════════════════════════


def test_force_rerun_replaces_run(server):
    project_name = "e2e_p5"
    project_dir = _make_project(server["work_dir"], project_name)
    _seed_basic_mix(project_dir)
    mix_hash = compute_mix_graph_hash(project_dir)

    y = _sine_stereo(3.0, freq=440.0, amp=0.5, sr=48000)
    _upload_wav(server, project_name, y,
                mix_graph_hash=mix_hash, duration_s=3.0)

    first = _exec_analyze_master_bus(project_dir, {})
    assert first["cached"] is False
    run_id_a = first["run_id"]

    second = _exec_analyze_master_bus(project_dir, {"force_rerun": True})
    assert second["cached"] is False
    run_id_b = second["run_id"]
    assert run_id_a != run_id_b

    # A was CASCADE-deleted; only B remains.
    conn = get_db(project_dir)
    ids = {r["id"] for r in conn.execute(
        "SELECT id FROM mix_analysis_runs").fetchall()}
    assert ids == {run_id_b}

    # And its child tables reference B, not A.
    assert conn.execute(
        "SELECT COUNT(*) FROM mix_datapoints WHERE run_id = ?", (run_id_a,)
    ).fetchone()[0] == 0
    assert conn.execute(
        "SELECT COUNT(*) FROM mix_datapoints WHERE run_id = ?", (run_id_b,)
    ).fetchone()[0] > 0


# ═══════════════════════════════════════════════════════════════════════════
# Scenario 6 — Sample-rate mismatch yields a clean error
# ═══════════════════════════════════════════════════════════════════════════


def test_sample_rate_mismatch_errors_cleanly(server):
    project_name = "e2e_p6"
    project_dir = _make_project(server["work_dir"], project_name)
    _seed_basic_mix(project_dir)
    mix_hash = compute_mix_graph_hash(project_dir)

    # Upload at 44100 — the upload endpoint rejects SR-mismatched form fields,
    # so we must match the form to the WAV. Then we ask the analyzer for
    # 48000, forcing the mismatch on the analyzer side only.
    y = _sine_stereo(2.0, freq=440.0, amp=0.5, sr=44100)
    _upload_wav(server, project_name, y,
                mix_graph_hash=mix_hash, duration_s=2.0, sr=44100, channels=2)

    result = _exec_analyze_master_bus(
        project_dir, {"sample_rate": 48000, "end_time_s": 2.0},
    )
    assert "error" in result, result
    assert "sample rate" in result["error"].lower()


# ═══════════════════════════════════════════════════════════════════════════
# Scenario 7 — CASCADE verification end-to-end
# ═══════════════════════════════════════════════════════════════════════════


def test_cascade_delete_clears_all_child_tables(server):
    project_name = "e2e_p7"
    project_dir = _make_project(server["work_dir"], project_name)
    _seed_basic_mix(project_dir)
    mix_hash = compute_mix_graph_hash(project_dir)

    # Inject a clip so there's at least one clipping section to cascade.
    sr = 48000
    y = _sine_stereo(3.0, freq=440.0, amp=0.25, sr=sr).copy()
    y[sr:sr + 10, :] = 0.999
    _upload_wav(server, project_name, y,
                mix_graph_hash=mix_hash, duration_s=3.0, sr=sr)

    result = _exec_analyze_master_bus(project_dir, {})
    run_id = result["run_id"]

    conn = get_db(project_dir)
    # Pre-delete: all three child tables populated.
    assert conn.execute(
        "SELECT COUNT(*) FROM mix_datapoints WHERE run_id = ?", (run_id,)
    ).fetchone()[0] > 0
    assert conn.execute(
        "SELECT COUNT(*) FROM mix_sections WHERE run_id = ?", (run_id,)
    ).fetchone()[0] > 0
    assert conn.execute(
        "SELECT COUNT(*) FROM mix_scalars WHERE run_id = ?", (run_id,)
    ).fetchone()[0] > 0

    delete_mix_run(project_dir, run_id)

    # Post-delete: all zero (CASCADE from mix_analysis_runs).
    assert conn.execute(
        "SELECT COUNT(*) FROM mix_analysis_runs WHERE id = ?", (run_id,)
    ).fetchone()[0] == 0
    assert conn.execute(
        "SELECT COUNT(*) FROM mix_datapoints WHERE run_id = ?", (run_id,)
    ).fetchone()[0] == 0
    assert conn.execute(
        "SELECT COUNT(*) FROM mix_sections WHERE run_id = ?", (run_id,)
    ).fetchone()[0] == 0
    assert conn.execute(
        "SELECT COUNT(*) FROM mix_scalars WHERE run_id = ?", (run_id,)
    ).fetchone()[0] == 0
