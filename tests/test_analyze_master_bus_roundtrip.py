"""Integration tests for the ``analyze_master_bus`` WS round-trip (M15 task-7).

Exercises the full flow through ``_exec_analyze_master_bus``:

  1. The tool emits a ``mix_render_request`` message on the provided ws.
  2. A stand-in "frontend" coroutine writes the rendered WAV to
     ``pool/mixes/<hash>.wav`` and calls ``set_mix_render_event(request_id)``.
  3. The tool unblocks, loads the WAV, runs analyses, and returns.

Covers:
  - Happy path: the event fires in time → analysis completes.
  - Timeout path: no frontend reply → error after the short test timeout.
  - Upload handler's event-release: the api_server path sets the event
    when ``request_id`` is present in the multipart body.

We short-circuit real WebSocket IO with a tiny ``FakeWs`` that records all
``send()`` payloads. The round-trip is deterministic — we snoop the
request_id from the recorded message and hand it to the fake frontend
coroutine.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf

from scenecraft.chat import (
    MIX_RENDER_TIMEOUT_S,
    _exec_analyze_master_bus,
    set_mix_render_event,
)
from scenecraft.db import get_db


STUB_HASH = "a" * 64


# ── Test infra ──────────────────────────────────────────────────────────────


class FakeWs:
    """Minimal async-ws stand-in. Captures every send() payload so tests can
    assert message shape + extract the server-assigned request_id."""

    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send(self, data: str) -> None:
        self.sent.append(json.loads(data))


@pytest.fixture(autouse=True)
def pin_mix_graph_hash(monkeypatch):
    import scenecraft.mix_graph_hash as mgh_mod
    monkeypatch.setattr(mgh_mod, "compute_mix_graph_hash", lambda _project_dir: STUB_HASH)
    yield


@pytest.fixture
def project(tmp_path) -> Path:
    project_dir = tmp_path / "mix_project"
    project_dir.mkdir()
    get_db(project_dir)
    # End-time 2.0s for a short, snappy test render.
    conn = get_db(project_dir)
    conn.execute(
        "INSERT INTO audio_clips (id, track_id, source_path, start_time, end_time) "
        "VALUES (?, ?, ?, ?, ?)",
        ("c1", "t1", "dummy.wav", 0.0, 2.0),
    )
    conn.commit()
    return project_dir


def _write_sine_wav(path: Path, duration_s: float = 2.0, sr: int = 48000) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    n = int(sr * duration_s)
    t = np.arange(n, dtype=np.float64) / sr
    y = (0.5 * np.sin(2 * np.pi * 440.0 * t)).astype(np.float32)
    sf.write(str(path), y, sr, subtype="PCM_16")


# ── Happy path ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_ws_roundtrip_completes_when_frontend_uploads(project):
    """The analyze tool awaits the upload event; a fake frontend writes the
    WAV and signals — the tool must unblock and return scalars."""
    ws = FakeWs()

    async def fake_frontend() -> None:
        # Wait until the tool has emitted its request (with the request_id).
        for _ in range(100):
            if ws.sent:
                break
            await asyncio.sleep(0.005)
        assert ws.sent, "tool never emitted mix_render_request"

        msg = ws.sent[-1]
        assert msg["type"] == "mix_render_request"
        assert "request_id" in msg
        assert msg["mix_graph_hash"] == STUB_HASH
        assert msg["sample_rate"] == 48000
        assert msg["start_time_s"] == 0.0
        assert msg["end_time_s"] == 2.0

        # "Render" — write the WAV where the tool expects it.
        wav_path = project / "pool" / "mixes" / f"{STUB_HASH}.wav"
        _write_sine_wav(wav_path, duration_s=2.0)

        # "Upload" — release the waiting tool.
        ok = set_mix_render_event(msg["request_id"])
        assert ok, "event should be pending when set_mix_render_event is called"

    # Run analyzer and fake frontend concurrently. Use a short timeout so a
    # bug manifests as a fast test failure, not a 60s hang.
    analyze = _exec_analyze_master_bus(project, {}, ws=ws, timeout_s=5.0)
    result, _ = await asyncio.gather(analyze, fake_frontend())

    assert "error" not in result, result
    assert result["mix_graph_hash"] == STUB_HASH
    assert result["start_time_s"] == 0.0
    assert result["end_time_s"] == 2.0
    # Sanity: analyses actually ran.
    assert result["scalars"].get("peak_db") is not None
    assert "peak" in result["analyses_written"]


# ── Timeout path ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_ws_roundtrip_times_out_when_frontend_silent(project):
    """Without a frontend upload the tool must time out cleanly with an
    error — not hang, not raise."""
    ws = FakeWs()

    result = await _exec_analyze_master_bus(
        project, {}, ws=ws, timeout_s=0.15,
    )

    assert "error" in result
    assert "timeout" in result["error"].lower()
    # Should have still emitted the request (so the frontend had a chance).
    assert len(ws.sent) == 1
    assert ws.sent[0]["type"] == "mix_render_request"
    assert ws.sent[0]["mix_graph_hash"] == STUB_HASH
    # Event for this request_id must be cleaned up — no leaks in the registry.
    from scenecraft.chat import _MIX_RENDER_EVENTS
    assert ws.sent[0]["request_id"] not in _MIX_RENDER_EVENTS


# ── Upload handler event-release ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_upload_handler_releases_pending_event():
    """Directly exercise set_mix_render_event — the behavior the api_server's
    mix-render-upload handler relies on."""
    from scenecraft.chat import _MIX_RENDER_EVENTS

    rid = "release-test-request-id"
    event = asyncio.Event()
    _MIX_RENDER_EVENTS[rid] = event
    try:
        assert not event.is_set()
        assert set_mix_render_event(rid) is True
        assert event.is_set()
        # Unknown id is a no-op.
        assert set_mix_render_event("not-a-real-request") is False
    finally:
        _MIX_RENDER_EVENTS.pop(rid, None)


# ── Constant is configurable ────────────────────────────────────────────────


def test_default_timeout_is_60s():
    """Defensive: the 60s wait is intentional. Changing it is a design
    decision that should surface as a failing test."""
    assert MIX_RENDER_TIMEOUT_S == 60.0
