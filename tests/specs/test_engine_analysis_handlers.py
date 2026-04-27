"""Regression tests for local.engine-analysis-handlers.md.

Covers the five engine-internal analysis handlers:

  - ``_exec_bounce_audio``         (R-B1..R-B23)
  - ``_exec_analyze_master_bus``   (R-M1..R-M23)
  - ``_exec_generate_dsp``         (R-D1..R-D17)
  - ``_exec_generate_descriptions``(R-G1..R-G15)
  - ``compute_peaks``              (R-P1..R-P15)

Conventions:
- Inline helpers are prefixed ``_analysis_`` (per task-82 conftest directive).
- Each test docstring opens with ``covers Rn[, OQ-K]``.
- Target-state behaviours (per OQ resolutions) use
  ``@pytest.mark.xfail(reason="target-state; awaits …", strict=False)`` so they
  light up the moment the engine ships the resolved behavior.
- Provider / heavy-lib calls (librosa, pyloudnorm, google.genai, ffmpeg) are
  mocked at the import-site so the suite stays fast and deterministic.
- E2E section (``TestEndToEnd``) exercises the HTTP boundaries that exist today
  (``/peaks``, ``/bounce-upload``, ``/mix-render-upload``). Chat-driven analysis
  handlers are WS-only — their HTTP coverage is ``xfail(strict=False)`` pending
  the M16 FastAPI refactor.
"""
from __future__ import annotations

import asyncio
import json
import os
import struct
import subprocess
import sys
import threading
import time
import wave
from pathlib import Path
from unittest import mock

import pytest

from scenecraft import db as scdb
from scenecraft import db_bounces as dbc
from scenecraft import db_mix_cache as dmc
from scenecraft import db_analysis_cache as dac
from scenecraft import chat as chat_mod
from scenecraft.audio import peaks as peaks_mod


# ---------------------------------------------------------------------------
# Inline helpers (prefixed `_analysis_`).
# ---------------------------------------------------------------------------


def _analysis_write_wav(
    path: Path,
    *,
    seconds: float = 0.10,
    sr: int = 48000,
    sample_value: int = 0x1000,
    channels: int = 1,
) -> Path:
    """Write a tiny PCM-16 mono/stereo WAV at the requested sample rate."""
    path.parent.mkdir(parents=True, exist_ok=True)
    n_frames = max(1, int(sr * seconds))
    sample_bytes = struct.pack("<h", sample_value)
    pcm = sample_bytes * n_frames * channels
    with wave.open(str(path), "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(pcm)
    return path


def _analysis_seed_clip(
    project_dir: Path,
    *,
    end_time: float = 1.0,
    track_id: str = "tr-1",
    clip_id: str = "cl-1",
):
    """Insert one audio_track + one audio_clip so `_resolve_mix_end_time` works."""
    scdb.add_audio_track(project_dir, {
        "id": track_id, "name": "T",
        "display_order": 0, "hidden": False,
        "muted": False, "solo": False,
        "volume_curve": json.dumps([[0, 1.0], [1, 1.0]]),
    })
    scdb.add_audio_clip(project_dir, {
        "id": clip_id, "track_id": track_id,
        "source_path": "x.wav", "start_time": 0.0,
        "end_time": float(end_time), "source_offset": 0.0,
    })
    return track_id, clip_id


def _analysis_seed_pool_segment(
    project_dir: Path, *, pool_path: str = "pool/seg.wav",
) -> str:
    return scdb.add_pool_segment(
        project_dir, kind="generated", created_by="test", pool_path=pool_path,
    )


def _analysis_make_mock_ws_with_uploader(
    project_dir: Path, *, mode: str, write_file_after_ws: bool,
):
    """Build a mock WS whose ``send`` schedules a background uploader.

    The uploader writes the rendered WAV (if ``write_file_after_ws``) and
    fires the matching render-event so the awaiting handler unblocks.
    """
    ws = mock.MagicMock()
    sent = []

    async def _send(msg: str):
        sent.append(msg)
        parsed = json.loads(msg)
        request_id = parsed["request_id"]

        async def _release():
            await asyncio.sleep(0.005)
            if write_file_after_ws:
                if mode == "bounce":
                    h = parsed["composite_hash"]
                    dest = project_dir / "pool" / "bounces" / f"{h}.wav"
                    _analysis_write_wav(dest, sr=parsed["sample_rate"])
                else:
                    h = parsed["mix_graph_hash"]
                    dest = project_dir / "pool" / "mixes" / f"{h}.wav"
                    _analysis_write_wav(dest, sr=parsed["sample_rate"])
            if mode == "bounce":
                chat_mod.set_bounce_render_event(request_id)
            else:
                chat_mod.set_mix_render_event(request_id)

        asyncio.create_task(_release())

    ws.send = _send
    ws._sent = sent
    return ws


# ===========================================================================
# UNIT — TestBounceHandler
# ===========================================================================


class TestBounceHandler:
    """`_exec_bounce_audio` — R-B1..R-B23, Behavior Table rows 1-13, 53-54, 58."""

    @pytest.mark.asyncio
    async def test_happy_path_full_mode(self, project_dir, db_conn):
        """covers R-B8, R-B9, R-B11, R-B14, R-B17, R-B19, R-B20.

        Background uploader fires the event after writing the WAV; handler
        finalizes the row and returns the success payload.
        """
        _analysis_seed_clip(project_dir, end_time=0.5)
        ws = _analysis_make_mock_ws_with_uploader(
            project_dir, mode="bounce", write_file_after_ws=True,
        )

        result = await chat_mod._exec_bounce_audio(
            project_dir,
            {"sample_rate": 48000, "bit_depth": 16, "channels": 1},
            ws=ws, project_name="p", timeout_s=2.0,
        )

        assert "error" not in result, result
        assert result["mode"] == "full", "mode-full"
        assert len(result["composite_hash"]) == 64, "composite-hash-hex-64"
        assert all(c in "0123456789abcdef" for c in result["composite_hash"])
        assert result["rendered_path"] == f"pool/bounces/{result['composite_hash']}.wav"
        assert result["download_url"] == f"/api/projects/p/bounces/{result['bounce_id']}.wav"
        assert result["duration_s"] > 0
        assert result["cached"] is False
        # Row finalized in DB
        row = dbc.get_bounce_by_id(project_dir, result["bounce_id"])
        assert row is not None and row.rendered_path is not None
        assert row.size_bytes is not None and row.duration_s is not None

    @pytest.mark.asyncio
    async def test_emits_ws_request(self, project_dir, db_conn):
        """covers R-B14 — bounce-emits-ws-request."""
        _analysis_seed_clip(project_dir, end_time=0.5)
        ws = _analysis_make_mock_ws_with_uploader(
            project_dir, mode="bounce", write_file_after_ws=True,
        )
        await chat_mod._exec_bounce_audio(
            project_dir, {"sample_rate": 48000},
            ws=ws, project_name="p", timeout_s=2.0,
        )
        assert len(ws._sent) == 1, "ws-send-called-once"
        msg = json.loads(ws._sent[0])
        assert msg["type"] == "bounce_audio_request"
        assert len(msg["request_id"]) == 32, "uuid4-hex-32-chars"
        for k in ("bounce_id", "composite_hash", "start_time_s", "end_time_s",
                  "mode", "track_ids", "clip_ids", "sample_rate", "bit_depth",
                  "channels"):
            assert k in msg, f"payload-field-{k}-present"

    @pytest.mark.asyncio
    async def test_cache_hit_short_circuits(self, project_dir, db_conn):
        """covers R-B12 — bounce-cache-hit-short-circuits."""
        _analysis_seed_clip(project_dir, end_time=0.5)
        # First call to populate cache.
        ws = _analysis_make_mock_ws_with_uploader(
            project_dir, mode="bounce", write_file_after_ws=True,
        )
        first = await chat_mod._exec_bounce_audio(
            project_dir, {"sample_rate": 48000},
            ws=ws, project_name="p", timeout_s=2.0,
        )
        # Second call — different ws to verify it's never invoked.
        ws2 = mock.MagicMock()
        ws2.send = mock.AsyncMock()
        second = await chat_mod._exec_bounce_audio(
            project_dir, {"sample_rate": 48000},
            ws=ws2, project_name="p", timeout_s=2.0,
        )
        assert second["cached"] is True, "cached-true"
        ws2.send.assert_not_awaited(), "no-ws-send"
        assert second["bounce_id"] == first["bounce_id"], "returns-existing-id"

    @pytest.mark.asyncio
    async def test_stale_row_deleted_before_retry(self, project_dir, db_conn):
        """covers R-B13 — bounce-stale-row-deleted-before-retry."""
        _analysis_seed_clip(project_dir, end_time=0.5)
        # Pre-seed a stale (rendered_path NULL) row matching the hash we'll compute.
        from scenecraft.bounce_hash import compute_bounce_hash
        h = compute_bounce_hash(
            project_dir, start_time_s=0.0, end_time_s=0.5, mode="full",
            track_ids=None, clip_ids=None,
            sample_rate=48000, bit_depth=24, channels=2,
        )
        stale = dbc.create_bounce(
            project_dir, composite_hash=h, start_time_s=0.0, end_time_s=0.5,
            mode="full", selection={}, sample_rate=48000, bit_depth=24,
            channels=2, rendered_path=None,
        )
        ws = _analysis_make_mock_ws_with_uploader(
            project_dir, mode="bounce", write_file_after_ws=True,
        )
        result = await chat_mod._exec_bounce_audio(
            project_dir, {}, ws=ws, project_name="p", timeout_s=2.0,
        )
        assert "error" not in result, result
        assert result["bounce_id"] != stale.id, "stale-row-deleted-fresh-inserted"
        assert dbc.get_bounce_by_id(project_dir, stale.id) is None

    @pytest.mark.asyncio
    @pytest.mark.parametrize("override,bad_field", [
        ({"sample_rate": 22050}, "sample_rate"),
        ({"bit_depth": 12}, "bit_depth"),
        ({"channels": 3}, "channels"),
    ])
    async def test_rejects_invalid_format_fields(
        self, project_dir, db_conn, override, bad_field,
    ):
        """covers R-B5, R-B6, R-B7 — bounce-rejects-invalid-format-fields."""
        _analysis_seed_clip(project_dir, end_time=0.5)
        ws = mock.MagicMock(); ws.send = mock.AsyncMock()
        result = await chat_mod._exec_bounce_audio(
            project_dir, override, ws=ws, project_name="p",
        )
        assert "error" in result and bad_field in result["error"]
        assert dbc.list_bounces(project_dir) == [], "no-row-inserted"
        ws.send.assert_not_awaited(), "no-ws-send"

    @pytest.mark.asyncio
    async def test_rejects_dual_selection(self, project_dir, db_conn):
        """covers R-B3 — bounce-rejects-dual-selection."""
        _analysis_seed_clip(project_dir, end_time=0.5)
        result = await chat_mod._exec_bounce_audio(
            project_dir,
            {"track_ids": ["a"], "clip_ids": ["b"]},
            ws=None, project_name="p",
        )
        assert "error" in result and "either" in result["error"]

    @pytest.mark.asyncio
    async def test_rejects_missing_track_ids(self, project_dir, db_conn):
        """covers R-B10 — bounce-rejects-missing-ids."""
        _analysis_seed_clip(project_dir, end_time=0.5)
        result = await chat_mod._exec_bounce_audio(
            project_dir, {"track_ids": ["ghost-track"]},
            ws=None, project_name="p",
        )
        assert "error" in result and "ghost-track" in result["error"]

    @pytest.mark.asyncio
    async def test_empty_project_errors(self, project_dir, db_conn):
        """covers R-B9 — bounce-empty-project-errors (no clips, end=None)."""
        # Note: db_conn here ensures schema migration has run.
        result = await chat_mod._exec_bounce_audio(
            project_dir, {}, ws=None, project_name="p",
        )
        assert "error" in result and "end_time_s" in result["error"]
        assert dbc.list_bounces(project_dir) == []

    @pytest.mark.asyncio
    async def test_no_ws_cleans_up_row(self, project_dir, db_conn):
        """covers R-B14 — bounce-no-ws-cleans-up-row.

        ws=None + no WAV on disk → row is inserted then deleted.
        """
        _analysis_seed_clip(project_dir, end_time=0.5)
        result = await chat_mod._exec_bounce_audio(
            project_dir, {}, ws=None, project_name="p",
        )
        assert "error" in result
        assert dbc.list_bounces(project_dir) == [], "row-cleaned-up"

    @pytest.mark.asyncio
    async def test_ws_send_failure_cleans_up(self, project_dir, db_conn):
        """covers R-B15, R-B17 — bounce-ws-send-failure-cleans-up."""
        _analysis_seed_clip(project_dir, end_time=0.5)
        ws = mock.MagicMock()

        async def _boom(_msg):
            raise RuntimeError("conn closed")

        ws.send = _boom
        result = await chat_mod._exec_bounce_audio(
            project_dir, {}, ws=ws, project_name="p", timeout_s=2.0,
        )
        assert "error" in result and "failed to send" in result["error"]
        assert dbc.list_bounces(project_dir) == []
        assert chat_mod._BOUNCE_RENDER_EVENTS == {}

    @pytest.mark.asyncio
    async def test_timeout_cleans_up_row(self, project_dir, db_conn):
        """covers R-B16, R-B17 — bounce-timeout-cleans-up-row."""
        _analysis_seed_clip(project_dir, end_time=0.5)
        ws = mock.MagicMock()
        ws.send = mock.AsyncMock()  # never fires the event
        result = await chat_mod._exec_bounce_audio(
            project_dir, {}, ws=ws, project_name="p", timeout_s=0.05,
        )
        assert "error" in result and "timeout" in result["error"]
        assert dbc.list_bounces(project_dir) == []
        assert chat_mod._BOUNCE_RENDER_EVENTS == {}

    @pytest.mark.asyncio
    async def test_event_set_but_file_absent(self, project_dir, db_conn):
        """covers R-B18 — bounce-event-set-but-file-absent.

        Background task fires the event WITHOUT writing the WAV.
        """
        _analysis_seed_clip(project_dir, end_time=0.5)
        ws = _analysis_make_mock_ws_with_uploader(
            project_dir, mode="bounce", write_file_after_ws=False,
        )
        result = await chat_mod._exec_bounce_audio(
            project_dir, {}, ws=ws, project_name="p", timeout_s=2.0,
        )
        assert "error" in result and "still missing" in result["error"]
        assert dbc.list_bounces(project_dir) == []

    def test_set_bounce_event_unknown_id(self):
        """covers R-B21 — bounce-set-event-unknown-id."""
        assert chat_mod.set_bounce_render_event("deadbeef") is False

    @pytest.mark.asyncio
    async def test_reads_32bit_float_via_soundfile(
        self, project_dir, db_conn, monkeypatch,
    ):
        """covers R-B19 — bounce-reads-32bit-float-via-soundfile.

        Patch ``wave.open`` to raise — handler must fall back to soundfile.
        """
        _analysis_seed_clip(project_dir, end_time=0.5)
        ws = _analysis_make_mock_ws_with_uploader(
            project_dir, mode="bounce", write_file_after_ws=True,
        )
        import wave as _wave
        original = _wave.open

        # Only fail the handler's READ path ("rb"); the uploader helper writes
        # the WAV via wave.open(..., "wb") and must keep working so the
        # render-event delivers a real file for the soundfile fallback to read.
        def _broken_wave_open(*a, **kw):
            mode = kw.get("mode")
            if mode is None and len(a) >= 2:
                mode = a[1]
            if mode == "rb":
                raise RuntimeError(
                    "simulated 32-bit-float WAV unsupported by stdlib"
                )
            return original(*a, **kw)

        monkeypatch.setattr(_wave, "open", _broken_wave_open)
        result = await chat_mod._exec_bounce_audio(
            project_dir, {"sample_rate": 48000},
            ws=ws, project_name="p", timeout_s=2.0,
        )
        # Restore so finalizer/teardown can reuse wave if needed.
        monkeypatch.setattr(_wave, "open", original)
        assert "error" not in result, result
        assert result["duration_s"] > 0, "soundfile-fallback-yielded-duration"

    # --- Target-state xfails (OQ resolutions) ---

    @pytest.mark.asyncio
    @pytest.mark.xfail(
        reason="target-state R-B22; awaits cache-hit stat-check (OQ-2)",
        strict=False,
    )
    async def test_cache_hit_missing_file_refetches(self, project_dir, db_conn):
        """covers R-B22, OQ-2 — bounce-cache-hit-missing-file-refetches.

        Row says rendered_path set but file is gone from disk → handler
        SHOULD treat as cache miss and re-render via WS.
        """
        _analysis_seed_clip(project_dir, end_time=0.5)
        from scenecraft.bounce_hash import compute_bounce_hash
        h = compute_bounce_hash(
            project_dir, start_time_s=0.0, end_time_s=0.5, mode="full",
            track_ids=None, clip_ids=None,
            sample_rate=48000, bit_depth=24, channels=2,
        )
        # Pretend a row exists with a rendered_path but the WAV is missing.
        dbc.create_bounce(
            project_dir, composite_hash=h, start_time_s=0.0, end_time_s=0.5,
            mode="full", selection={}, sample_rate=48000, bit_depth=24,
            channels=2, rendered_path=f"pool/bounces/{h}.wav",
            size_bytes=999, duration_s=0.5,
        )
        ws = _analysis_make_mock_ws_with_uploader(
            project_dir, mode="bounce", write_file_after_ws=True,
        )
        result = await chat_mod._exec_bounce_audio(
            project_dir, {}, ws=ws, project_name="p", timeout_s=2.0,
        )
        # TARGET behavior: NOT cached — re-fetched.
        assert result.get("cached") is False, "stat-check-on-cache-hit-detected-missing-file"
        assert len(ws._sent) == 1, "ws-send-invoked"

    @pytest.mark.asyncio
    @pytest.mark.xfail(
        reason="target-state R-B23; awaits explicit cancellation event (OQ-6)",
        strict=False,
    )
    async def test_ws_close_mid_wait_disconnect_result(self, project_dir, db_conn):
        """covers R-B23, OQ-6 — bounce-ws-close-mid-wait-disconnect-result.

        WS client disconnects mid-wait → handler SHOULD return a result with
        ``reason='client_disconnected'`` (distinct from timeout).
        """
        _analysis_seed_clip(project_dir, end_time=0.5)
        ws = mock.MagicMock()
        ws.send = mock.AsyncMock()
        # Today: this just times out. Target: cancellation event resolves earlier
        # with reason='client_disconnected'. Test asserts the target shape.
        result = await chat_mod._exec_bounce_audio(
            project_dir, {}, ws=ws, project_name="p", timeout_s=0.05,
        )
        assert result.get("reason") == "client_disconnected"

    @pytest.mark.xfail(
        reason="target-state R-A1; awaits server-boot orphan sweep (OQ-1)",
        strict=False,
    )
    def test_late_upload_orphan_swept(self, project_dir, db_conn):
        """covers R-A1, OQ-1 — bounce-late-upload-orphan-swept."""
        # Pretend the engine has a startup_sweep symbol; if absent → xfail naturally.
        from scenecraft import startup_sweep  # noqa: F401 — TARGET symbol
        # Drop an orphan WAV.
        bounces = project_dir / "pool" / "bounces"
        bounces.mkdir(parents=True, exist_ok=True)
        orphan = bounces / ("a" * 64 + ".wav")
        orphan.write_bytes(b"RIFFstub")
        startup_sweep.run(project_dir)  # type: ignore[attr-defined]
        assert not orphan.exists(), "orphan-removed"


# ===========================================================================
# UNIT — TestAnalyzeMasterBus
# ===========================================================================


class TestAnalyzeMasterBus:
    """`_exec_analyze_master_bus` — R-M1..R-M23, Behavior rows 14-26."""

    @pytest.mark.asyncio
    async def test_happy_path_default_analyses(
        self, project_dir, db_conn, monkeypatch,
    ):
        """covers R-M3, R-M5, R-M16, R-M21, R-M22.

        Pre-place WAV at pool/mixes/<hash>.wav; ws=None path.
        Mock the heavy librosa-backed scalar helpers so the test is fast and
        deterministic.
        """
        _analysis_seed_clip(project_dir, end_time=0.5)
        from scenecraft.mix_graph_hash import compute_mix_graph_hash
        h = compute_mix_graph_hash(project_dir)
        wav = project_dir / "pool" / "mixes" / f"{h}.wav"
        _analysis_write_wav(wav, sr=48000)

        # Patch deterministic scalars; rms/spectral return sparse datapoints.
        monkeypatch.setattr(chat_mod, "_mix_peak_db", lambda y: -3.0)
        monkeypatch.setattr(chat_mod, "_mix_true_peak_db", lambda y, sr: -2.5)
        monkeypatch.setattr(chat_mod, "_mix_rms_envelope", lambda y, sr: [(0.0, 0.1)])
        monkeypatch.setattr(chat_mod, "_mix_lufs", lambda y, sr: -14.0)
        monkeypatch.setattr(chat_mod, "_mix_clipping_events", lambda y, sr: [])
        monkeypatch.setattr(chat_mod, "_mix_spectral_centroid",
                            lambda y, sr, target_hz=10.0: [(0.0, 1500.0)])

        result = await chat_mod._exec_analyze_master_bus(
            project_dir, {"sample_rate": 48000}, ws=None,
        )
        assert "error" not in result, result
        assert result["cached"] is False
        assert "peak_db" in result["scalars"]
        assert "lufs_integrated" in result["scalars"]
        assert "dynamic_range_db" in result["scalars"]
        assert result["mix_graph_hash"] == h
        assert result["rendered_path"] == f"pool/mixes/{h}.wav"
        runs = dmc.list_mix_runs_for_hash(project_dir, h)
        assert len(runs) == 1, "exactly-one-run-row"

    @pytest.mark.asyncio
    async def test_cache_hit_returns_cached_scalars(
        self, project_dir, db_conn, monkeypatch,
    ):
        """covers R-M7 — analyze-cache-hit-returns-cached-scalars."""
        _analysis_seed_clip(project_dir, end_time=0.5)
        from scenecraft.mix_graph_hash import compute_mix_graph_hash
        import librosa as _lr
        h = compute_mix_graph_hash(project_dir)
        wav = project_dir / "pool" / "mixes" / f"{h}.wav"
        _analysis_write_wav(wav, sr=48000)
        # Pre-seed a successful run row for the 5-tuple key.
        analyzer_version = f"mix-librosa-{_lr.__version__}"
        run = dmc.create_mix_run(
            project_dir, h, 0.0, 0.5, 48000, analyzer_version,
            analyses=["peak"], rendered_path=f"pool/mixes/{h}.wav",
            created_at="2026-04-27T00:00:00Z",
        )
        dmc.set_mix_scalars(project_dir, run.id, {"peak_db": -1.0})

        called = mock.MagicMock(return_value=-9.0)
        monkeypatch.setattr(chat_mod, "_mix_peak_db", called)

        result = await chat_mod._exec_analyze_master_bus(
            project_dir, {"sample_rate": 48000, "end_time_s": 0.5}, ws=None,
        )
        assert result["cached"] is True
        assert result["run_id"] == run.id
        called.assert_not_called(), "no-librosa-call-on-cache-hit"

    @pytest.mark.asyncio
    async def test_force_rerun_deletes_prior(
        self, project_dir, db_conn, monkeypatch,
    ):
        """covers R-M8 — analyze-force-rerun-deletes-prior."""
        _analysis_seed_clip(project_dir, end_time=0.5)
        from scenecraft.mix_graph_hash import compute_mix_graph_hash
        import librosa as _lr
        h = compute_mix_graph_hash(project_dir)
        wav = project_dir / "pool" / "mixes" / f"{h}.wav"
        _analysis_write_wav(wav, sr=48000)
        analyzer_version = f"mix-librosa-{_lr.__version__}"
        old = dmc.create_mix_run(
            project_dir, h, 0.0, 0.5, 48000, analyzer_version,
            analyses=["peak"], rendered_path=f"pool/mixes/{h}.wav",
            created_at="2026-04-27T00:00:00Z",
        )

        monkeypatch.setattr(chat_mod, "_mix_peak_db", lambda y: -3.0)
        monkeypatch.setattr(chat_mod, "_mix_true_peak_db", lambda y, sr: -2.5)
        monkeypatch.setattr(chat_mod, "_mix_rms_envelope", lambda y, sr: [])
        monkeypatch.setattr(chat_mod, "_mix_lufs", lambda y, sr: -14.0)
        monkeypatch.setattr(chat_mod, "_mix_clipping_events", lambda y, sr: [])
        monkeypatch.setattr(chat_mod, "_mix_spectral_centroid",
                            lambda y, sr, target_hz=10.0: [])

        result = await chat_mod._exec_analyze_master_bus(
            project_dir, {"sample_rate": 48000, "end_time_s": 0.5,
                          "force_rerun": True}, ws=None,
        )
        assert result["cached"] is False
        assert result["run_id"] != old.id

    @pytest.mark.asyncio
    async def test_missing_wav_no_ws_no_row_inserted(self, project_dir, db_conn):
        """covers R-M10 — analyze-missing-wav-no-ws-no-row-inserted."""
        _analysis_seed_clip(project_dir, end_time=0.5)
        result = await chat_mod._exec_analyze_master_bus(
            project_dir, {"sample_rate": 48000}, ws=None,
        )
        assert "error" in result
        from scenecraft.mix_graph_hash import compute_mix_graph_hash
        h = compute_mix_graph_hash(project_dir)
        assert dmc.list_mix_runs_for_hash(project_dir, h) == [], "no-row-inserted"

    @pytest.mark.asyncio
    async def test_ws_send_failure_no_row(self, project_dir, db_conn):
        """covers R-M11 — analyze-ws-send-failure-no-row."""
        _analysis_seed_clip(project_dir, end_time=0.5)
        ws = mock.MagicMock()

        async def _boom(_):
            raise RuntimeError("conn closed")

        ws.send = _boom
        result = await chat_mod._exec_analyze_master_bus(
            project_dir, {"sample_rate": 48000}, ws=ws, timeout_s=2.0,
        )
        assert "error" in result and "failed to send" in result["error"]
        from scenecraft.mix_graph_hash import compute_mix_graph_hash
        h = compute_mix_graph_hash(project_dir)
        assert dmc.list_mix_runs_for_hash(project_dir, h) == []

    @pytest.mark.asyncio
    async def test_ws_timeout_no_row(self, project_dir, db_conn):
        """covers R-M12 — analyze-ws-timeout-no-row."""
        _analysis_seed_clip(project_dir, end_time=0.5)
        ws = mock.MagicMock()
        ws.send = mock.AsyncMock()
        result = await chat_mod._exec_analyze_master_bus(
            project_dir, {"sample_rate": 48000}, ws=ws, timeout_s=0.05,
        )
        assert "error" in result and "timeout" in result["error"]
        assert chat_mod._MIX_RENDER_EVENTS == {}

    @pytest.mark.asyncio
    async def test_sample_rate_mismatch_errors(self, project_dir, db_conn):
        """covers R-M15 — analyze-sample-rate-mismatch-errors."""
        _analysis_seed_clip(project_dir, end_time=0.5)
        from scenecraft.mix_graph_hash import compute_mix_graph_hash
        h = compute_mix_graph_hash(project_dir)
        wav = project_dir / "pool" / "mixes" / f"{h}.wav"
        _analysis_write_wav(wav, sr=44100)  # mismatched
        result = await chat_mod._exec_analyze_master_bus(
            project_dir, {"sample_rate": 48000}, ws=None,
        )
        assert "error" in result and "does not match" in result["error"]
        assert dmc.list_mix_runs_for_hash(project_dir, h) == []

    @pytest.mark.asyncio
    async def test_per_analysis_exception_is_skipped(
        self, project_dir, db_conn, monkeypatch,
    ):
        """covers R-M18 (transitional) — analyze-per-analysis-exception-is-skipped.

        Patched ``_mix_lufs`` raises → "lufs" skipped, others continue.
        Today: `_mix_lufs` is wrapped in inner try/except; this test holds.
        """
        _analysis_seed_clip(project_dir, end_time=0.5)
        from scenecraft.mix_graph_hash import compute_mix_graph_hash
        h = compute_mix_graph_hash(project_dir)
        _analysis_write_wav(project_dir / "pool" / "mixes" / f"{h}.wav", sr=48000)

        monkeypatch.setattr(chat_mod, "_mix_peak_db", lambda y: -3.0)
        monkeypatch.setattr(chat_mod, "_mix_true_peak_db", lambda y, sr: -2.5)
        monkeypatch.setattr(chat_mod, "_mix_rms_envelope", lambda y, sr: [])

        def _bad_lufs(y, sr):
            raise ValueError("lufs synthetic failure")

        monkeypatch.setattr(chat_mod, "_mix_lufs", _bad_lufs)
        monkeypatch.setattr(chat_mod, "_mix_clipping_events", lambda y, sr: [])
        monkeypatch.setattr(chat_mod, "_mix_spectral_centroid",
                            lambda y, sr, target_hz=10.0: [])

        result = await chat_mod._exec_analyze_master_bus(
            project_dir, {"sample_rate": 48000, "end_time_s": 0.5}, ws=None,
        )
        assert "error" not in result, result
        assert "lufs" not in result["analyses_written"]
        assert "peak" in result["analyses_written"]
        assert dmc.list_mix_runs_for_hash(project_dir, h) != [], "row-not-rolled-back"

    @pytest.mark.asyncio
    async def test_toplevel_exception_rolls_back_row(
        self, project_dir, db_conn, monkeypatch,
    ):
        """covers R-M19 — analyze-toplevel-exception-rolls-back-row.

        ``_mix_peak_db`` is NOT wrapped in inner try/except in current code →
        raising it triggers the outer except, which deletes the row.
        """
        _analysis_seed_clip(project_dir, end_time=0.5)
        from scenecraft.mix_graph_hash import compute_mix_graph_hash
        h = compute_mix_graph_hash(project_dir)
        _analysis_write_wav(project_dir / "pool" / "mixes" / f"{h}.wav", sr=48000)

        def _boom(_y):
            raise RuntimeError("synthetic top-level explosion")

        monkeypatch.setattr(chat_mod, "_mix_peak_db", _boom)

        result = await chat_mod._exec_analyze_master_bus(
            project_dir, {"sample_rate": 48000, "end_time_s": 0.5}, ws=None,
        )
        assert "error" in result and "analysis failed" in result["error"]
        assert dmc.list_mix_runs_for_hash(project_dir, h) == [], "row-rolled-back"

    @pytest.mark.asyncio
    async def test_unknown_analysis_silently_skipped(
        self, project_dir, db_conn, monkeypatch,
    ):
        """covers R-M17 — analyze-unknown-analysis-silently-skipped."""
        _analysis_seed_clip(project_dir, end_time=0.5)
        from scenecraft.mix_graph_hash import compute_mix_graph_hash
        h = compute_mix_graph_hash(project_dir)
        _analysis_write_wav(project_dir / "pool" / "mixes" / f"{h}.wav", sr=48000)
        monkeypatch.setattr(chat_mod, "_mix_peak_db", lambda y: -3.0)
        result = await chat_mod._exec_analyze_master_bus(
            project_dir, {"sample_rate": 48000, "end_time_s": 0.5,
                          "analyses": ["peak", "does_not_exist"]}, ws=None,
        )
        assert "does_not_exist" not in result["analyses_written"]
        assert "error" not in result

    @pytest.mark.asyncio
    async def test_silence_skips_dynamic_range_scalar(
        self, project_dir, db_conn, monkeypatch,
    ):
        """covers R-M20 — analyze-silence-skips-dynamic-range-scalar."""
        _analysis_seed_clip(project_dir, end_time=0.5)
        from scenecraft.mix_graph_hash import compute_mix_graph_hash
        h = compute_mix_graph_hash(project_dir)
        _analysis_write_wav(project_dir / "pool" / "mixes" / f"{h}.wav", sr=48000)
        monkeypatch.setattr(chat_mod, "_mix_peak_db", lambda y: float("-inf"))
        monkeypatch.setattr(chat_mod, "_mix_lufs", lambda y, sr: float("-inf"))
        monkeypatch.setattr(chat_mod, "_mix_true_peak_db", lambda y, sr: float("-inf"))
        monkeypatch.setattr(chat_mod, "_mix_rms_envelope", lambda y, sr: [])
        monkeypatch.setattr(chat_mod, "_mix_clipping_events", lambda y, sr: [])
        monkeypatch.setattr(chat_mod, "_mix_spectral_centroid",
                            lambda y, sr, target_hz=10.0: [])
        result = await chat_mod._exec_analyze_master_bus(
            project_dir, {"sample_rate": 48000, "end_time_s": 0.5}, ws=None,
        )
        assert "dynamic_range_db" not in result["scalars"]
        assert "dynamic_range" not in result["analyses_written"]

    @pytest.mark.asyncio
    async def test_dynamic_range_computes_missing_inputs(
        self, project_dir, db_conn, monkeypatch,
    ):
        """covers R-M20 — analyze-dynamic-range-computes-missing-inputs."""
        _analysis_seed_clip(project_dir, end_time=0.5)
        from scenecraft.mix_graph_hash import compute_mix_graph_hash
        h = compute_mix_graph_hash(project_dir)
        _analysis_write_wav(project_dir / "pool" / "mixes" / f"{h}.wav", sr=48000)
        monkeypatch.setattr(chat_mod, "_mix_peak_db", lambda y: -3.0)
        monkeypatch.setattr(chat_mod, "_mix_lufs", lambda y, sr: -14.0)
        result = await chat_mod._exec_analyze_master_bus(
            project_dir, {"sample_rate": 48000, "end_time_s": 0.5,
                          "analyses": ["dynamic_range"]}, ws=None,
        )
        assert "error" not in result, result
        assert result["scalars"].get("dynamic_range_db") == pytest.approx(11.0)
        assert "dynamic_range" in result["analyses_written"]

    def test_set_mix_render_event_unknown_id(self):
        """covers R-M23 — analyze-set-event-unknown-id."""
        assert chat_mod.set_mix_render_event("deadbeef") is False

    @pytest.mark.asyncio
    @pytest.mark.xfail(
        reason="target-state R-M18; awaits inner try/except for rms/peak/clipping (OQ-3)",
        strict=False,
    )
    async def test_inner_try_per_analysis_rms_peak_clipping(
        self, project_dir, db_conn, monkeypatch,
    ):
        """covers R-M18, OQ-3 — analyze-inner-try-per-analysis-rms-peak-clipping.

        Today: `_mix_rms_envelope` raising propagates to outer except and
        deletes the row. TARGET: per-analysis inner try/except keeps the row
        and skips just the failing analysis.
        """
        _analysis_seed_clip(project_dir, end_time=0.5)
        from scenecraft.mix_graph_hash import compute_mix_graph_hash
        h = compute_mix_graph_hash(project_dir)
        _analysis_write_wav(project_dir / "pool" / "mixes" / f"{h}.wav", sr=48000)
        monkeypatch.setattr(chat_mod, "_mix_peak_db", lambda y: -3.0)
        monkeypatch.setattr(chat_mod, "_mix_true_peak_db", lambda y, sr: -2.5)

        def _boom(*a, **kw):
            raise RuntimeError("rms synthetic failure")

        monkeypatch.setattr(chat_mod, "_mix_rms_envelope", _boom)
        monkeypatch.setattr(chat_mod, "_mix_lufs", lambda y, sr: -14.0)
        monkeypatch.setattr(chat_mod, "_mix_clipping_events", lambda y, sr: [])
        monkeypatch.setattr(chat_mod, "_mix_spectral_centroid",
                            lambda y, sr, target_hz=10.0: [])
        result = await chat_mod._exec_analyze_master_bus(
            project_dir, {"sample_rate": 48000, "end_time_s": 0.5}, ws=None,
        )
        # TARGET: error absent, peak still written, row preserved.
        assert "error" not in result
        assert "rms" not in result["analyses_written"]
        assert "peak" in result["analyses_written"]
        assert dmc.list_mix_runs_for_hash(project_dir, h) != []


# ===========================================================================
# UNIT — TestGenerateDsp
# ===========================================================================


class TestGenerateDsp:
    """`_exec_generate_dsp` — R-D1..R-D17, Behavior rows 27-33."""

    def test_rejects_missing_segment_id(self, project_dir, db_conn):
        """covers R-D1 — dsp-rejects-missing-segment."""
        result = chat_mod._exec_generate_dsp(project_dir, {})
        assert "error" in result and "missing" in result["error"]

    def test_rejects_unknown_segment(self, project_dir, db_conn):
        """covers R-D3 — dsp-rejects-missing-segment-and-file (unknown id)."""
        result = chat_mod._exec_generate_dsp(
            project_dir, {"source_segment_id": "ghost"},
        )
        assert "error" in result and "not found" in result["error"]

    def test_rejects_segment_with_missing_file(self, project_dir, db_conn):
        """covers R-D3 — dsp file missing from disk."""
        seg_id = _analysis_seed_pool_segment(project_dir, pool_path="pool/missing.wav")
        result = chat_mod._exec_generate_dsp(
            project_dir, {"source_segment_id": seg_id},
        )
        assert "error" in result and "source file not found" in result["error"]

    def test_rejects_non_list_analyses(self, project_dir, db_conn):
        """covers R-D2 — dsp-rejects-non-list-analyses."""
        result = chat_mod._exec_generate_dsp(
            project_dir,
            {"source_segment_id": "seg-1", "analyses": "not-a-list"},
        )
        assert "error" in result

    def test_happy_path_default_analyses(
        self, project_dir, db_conn, tmp_path, monkeypatch,
    ):
        """covers R-D2, R-D4, R-D14, R-D15 — dsp-happy-path-default-analyses."""
        rel = "pool/seg.wav"
        wav_path = project_dir / rel
        _analysis_write_wav(wav_path, sr=22050, seconds=0.20)
        seg_id = _analysis_seed_pool_segment(project_dir, pool_path=rel)

        # Mock heavy analyzers — return fast, deterministic shapes.
        from scenecraft import audio_intelligence as ai_mod
        from scenecraft import analyzer as an_mod
        monkeypatch.setattr(
            ai_mod, "_compute_rms_envelope",
            lambda y, sr, hop_length=512: [{"time": 0.0, "energy": 0.1}],
        )
        monkeypatch.setattr(
            ai_mod, "_detect_onsets",
            lambda y, sr, hop_length=512: [{"time": 0.05, "strength": 0.5}],
        )
        monkeypatch.setattr(
            an_mod, "detect_presence",
            lambda y, sr, hop_length=512: [{"start_time": 0.0, "end_time": 0.1}],
        )
        monkeypatch.setattr(
            an_mod, "load_audio",
            lambda path, sr: ([0.0] * (sr // 100), sr),
        )

        # tempo: stub librosa.beat.beat_track via attribute patch
        import librosa as _lr
        monkeypatch.setattr(
            _lr.beat, "beat_track",
            lambda y, sr, hop_length=512: (120.0, []),
        )

        result = chat_mod._exec_generate_dsp(
            project_dir, {"source_segment_id": seg_id},
        )
        assert "error" not in result, result
        assert result["cached"] is False
        assert set(result["analyses_written"]).issubset(
            {"onsets", "rms", "vocal_presence", "tempo"}
        )
        assert result["datapoint_count"] >= 1

    def test_cache_hit_short_circuits(
        self, project_dir, db_conn, monkeypatch,
    ):
        """covers R-D5 — dsp-cache-hit-short-circuits."""
        rel = "pool/seg.wav"
        _analysis_write_wav(project_dir / rel, sr=22050)
        seg_id = _analysis_seed_pool_segment(project_dir, pool_path=rel)
        # Pre-seed a run row.
        import librosa as _lr
        analyzer_version = f"librosa-{_lr.__version__}"
        analyses = ["onsets", "rms", "vocal_presence", "tempo"]
        params_hash = chat_mod._dsp_params_hash(analyses, 22050, 512)
        run = dac.create_dsp_run(
            project_dir, seg_id, analyzer_version, params_hash,
            analyses=analyses, created_at="2026-04-27T00:00:00Z",
        )
        called = mock.MagicMock()
        from scenecraft import analyzer as an_mod
        monkeypatch.setattr(an_mod, "load_audio", called)

        result = chat_mod._exec_generate_dsp(
            project_dir, {"source_segment_id": seg_id},
        )
        assert result["cached"] is True
        assert result["run_id"] == run.id
        called.assert_not_called(), "no-load-audio-on-hit"

    def test_force_rerun_deletes_prior(
        self, project_dir, db_conn, monkeypatch,
    ):
        """covers R-D6 — dsp-force-rerun-deletes-prior."""
        rel = "pool/seg.wav"
        _analysis_write_wav(project_dir / rel, sr=22050)
        seg_id = _analysis_seed_pool_segment(project_dir, pool_path=rel)
        import librosa as _lr
        analyzer_version = f"librosa-{_lr.__version__}"
        analyses = ["rms"]
        params_hash = chat_mod._dsp_params_hash(analyses, 22050, 512)
        old = dac.create_dsp_run(
            project_dir, seg_id, analyzer_version, params_hash,
            analyses=analyses, created_at="2026-04-27T00:00:00Z",
        )
        from scenecraft import analyzer as an_mod
        from scenecraft import audio_intelligence as ai_mod
        monkeypatch.setattr(
            an_mod, "load_audio", lambda path, sr: ([0.0] * 100, sr),
        )
        monkeypatch.setattr(
            ai_mod, "_compute_rms_envelope",
            lambda y, sr, hop_length=512: [{"time": 0.0, "energy": 0.1}],
        )

        result = chat_mod._exec_generate_dsp(
            project_dir,
            {"source_segment_id": seg_id, "analyses": ["rms"], "force_rerun": True},
        )
        assert result["cached"] is False
        assert result["run_id"] != old.id

    def test_unknown_analysis_silently_skipped(
        self, project_dir, db_conn, monkeypatch,
    ):
        """covers R-D8 — dsp-unknown-analysis-silently-skipped."""
        rel = "pool/seg.wav"
        _analysis_write_wav(project_dir / rel, sr=22050)
        seg_id = _analysis_seed_pool_segment(project_dir, pool_path=rel)
        from scenecraft import analyzer as an_mod
        from scenecraft import audio_intelligence as ai_mod
        monkeypatch.setattr(an_mod, "load_audio", lambda path, sr: ([0.0] * 100, sr))
        monkeypatch.setattr(
            ai_mod, "_compute_rms_envelope",
            lambda y, sr, hop_length=512: [],
        )
        result = chat_mod._exec_generate_dsp(
            project_dir,
            {"source_segment_id": seg_id, "analyses": ["rms", "nonexistent"]},
        )
        assert "nonexistent" not in result["analyses_written"]
        assert "error" not in result

    def test_audio_load_failure_no_row(
        self, project_dir, db_conn, monkeypatch,
    ):
        """covers R-D7 — dsp-audio-load-failure-no-row."""
        rel = "pool/seg.wav"
        _analysis_write_wav(project_dir / rel, sr=22050)
        seg_id = _analysis_seed_pool_segment(project_dir, pool_path=rel)

        from scenecraft import analyzer as an_mod

        def _bad(*a, **kw):
            raise ValueError("synthetic load failure")

        monkeypatch.setattr(an_mod, "load_audio", _bad)
        result = chat_mod._exec_generate_dsp(
            project_dir, {"source_segment_id": seg_id},
        )
        assert "error" in result and "failed to load audio" in result["error"]
        assert dac.list_dsp_runs(project_dir, seg_id) == []

    def test_per_analysis_exception_is_skipped(
        self, project_dir, db_conn, monkeypatch,
    ):
        """covers R-D12, R-D13 — dsp-per-analysis-exception-is-skipped."""
        rel = "pool/seg.wav"
        _analysis_write_wav(project_dir / rel, sr=22050)
        seg_id = _analysis_seed_pool_segment(project_dir, pool_path=rel)
        from scenecraft import analyzer as an_mod
        from scenecraft import audio_intelligence as ai_mod
        import librosa as _lr
        monkeypatch.setattr(an_mod, "load_audio",
                            lambda path, sr: ([0.0] * 100, sr))
        monkeypatch.setattr(
            ai_mod, "_compute_rms_envelope",
            lambda y, sr, hop_length=512: [{"time": 0.0, "energy": 0.1}],
        )

        def _boom(y, sr, hop_length=512):
            raise RuntimeError("synthetic tempo failure")

        monkeypatch.setattr(_lr.beat, "beat_track", _boom)
        result = chat_mod._exec_generate_dsp(
            project_dir,
            {"source_segment_id": seg_id, "analyses": ["tempo", "rms"]},
        )
        assert "tempo" not in result["analyses_written"]
        assert "rms" in result["analyses_written"]


# ===========================================================================
# UNIT — TestGenerateDescriptions
# ===========================================================================


class TestGenerateDescriptions:
    """`_exec_generate_descriptions` — R-G1..R-G15, Behavior rows 34-40, 56."""

    def test_rejects_missing_segment_id(self, project_dir, db_conn):
        """covers R-G1 — descriptions-rejects-missing-segment-id."""
        result = chat_mod._exec_generate_descriptions(project_dir, {})
        assert "error" in result and "missing" in result["error"]

    def test_rejects_unknown_segment(self, project_dir, db_conn):
        """covers R-G3 — descriptions-rejects-unknown-segment."""
        result = chat_mod._exec_generate_descriptions(
            project_dir, {"source_segment_id": "ghost"},
        )
        assert "error" in result and "not found" in result["error"]

    def test_happy_path_multiple_chunks(
        self, project_dir, db_conn, monkeypatch,
    ):
        """covers R-G7, R-G11, R-G12 — descriptions-happy-path-multiple-chunks."""
        rel = "pool/seg.wav"
        _analysis_write_wav(project_dir / rel, sr=22050)
        seg_id = _analysis_seed_pool_segment(project_dir, pool_path=rel)
        from scenecraft import audio_intelligence as ai_mod

        chunks = [
            {"path": f"/tmp/c{i}.wav", "start_time": float(i * 30),
             "end_time": float((i + 1) * 30)}
            for i in range(3)
        ]
        monkeypatch.setattr(
            ai_mod, "_chunk_audio_for_gemini",
            lambda path, chunk_duration: chunks,
        )

        def _stub(path, start, end, model, prompt_version):
            return {"section_type": "verse", "mood": "calm", "energy": 0.5}

        monkeypatch.setattr(ai_mod, "_gemini_describe_chunk_structured", _stub)
        result = chat_mod._exec_generate_descriptions(
            project_dir, {"source_segment_id": seg_id},
        )
        assert "error" not in result, result
        assert result["chunks_analyzed"] == 3
        assert result["chunks_failed"] == 0
        assert result["descriptions_written"] > 0

    def test_cache_hit_counts_distinct_chunks(
        self, project_dir, db_conn, monkeypatch,
    ):
        """covers R-G5 — descriptions-cache-hit-counts-distinct-chunks."""
        rel = "pool/seg.wav"
        _analysis_write_wav(project_dir / rel, sr=22050)
        seg_id = _analysis_seed_pool_segment(project_dir, pool_path=rel)
        # Pre-seed a run with rows spanning 2 distinct (start_s, end_s) pairs.
        run = dac.create_audio_description_run(
            project_dir, seg_id, "gemini-2.5-pro", "v1", 30.0,
            "2026-04-27T00:00:00Z",
        )
        dac.bulk_insert_audio_descriptions(project_dir, run.id, [
            (0.0, 30.0, "mood", "calm", None, None, None),
            (30.0, 60.0, "mood", "uplifting", None, None, None),
        ])
        result = chat_mod._exec_generate_descriptions(
            project_dir, {"source_segment_id": seg_id},
        )
        assert result["cached"] is True
        assert result["chunks_analyzed"] == 2
        assert result["chunks_failed"] == 0

    def test_chunk_none_is_failure(
        self, project_dir, db_conn, monkeypatch,
    ):
        """covers R-G8 — descriptions-chunk-none-is-failure."""
        rel = "pool/seg.wav"
        _analysis_write_wav(project_dir / rel, sr=22050)
        seg_id = _analysis_seed_pool_segment(project_dir, pool_path=rel)
        from scenecraft import audio_intelligence as ai_mod
        chunks = [
            {"path": f"/tmp/c{i}.wav", "start_time": float(i * 30),
             "end_time": float((i + 1) * 30)}
            for i in range(3)
        ]
        monkeypatch.setattr(
            ai_mod, "_chunk_audio_for_gemini",
            lambda path, chunk_duration: chunks,
        )
        calls = {"n": 0}

        def _maybe_none(path, start, end, model, prompt_version):
            calls["n"] += 1
            if calls["n"] == 2:
                return None
            return {"section_type": "verse"}

        monkeypatch.setattr(ai_mod, "_gemini_describe_chunk_structured", _maybe_none)
        result = chat_mod._exec_generate_descriptions(
            project_dir, {"source_segment_id": seg_id},
        )
        assert result["chunks_analyzed"] == 2
        assert result["chunks_failed"] == 1

    def test_empty_dict_is_failure(
        self, project_dir, db_conn, monkeypatch,
    ):
        """covers R-G9 — descriptions-empty-dict-is-failure."""
        rel = "pool/seg.wav"
        _analysis_write_wav(project_dir / rel, sr=22050)
        seg_id = _analysis_seed_pool_segment(project_dir, pool_path=rel)
        from scenecraft import audio_intelligence as ai_mod
        chunks = [{"path": "/tmp/c.wav", "start_time": 0.0, "end_time": 30.0}]
        monkeypatch.setattr(
            ai_mod, "_chunk_audio_for_gemini",
            lambda path, chunk_duration: chunks,
        )
        monkeypatch.setattr(
            ai_mod, "_gemini_describe_chunk_structured",
            lambda *a, **kw: {},
        )
        result = chat_mod._exec_generate_descriptions(
            project_dir, {"source_segment_id": seg_id},
        )
        assert result["chunks_analyzed"] == 0
        assert result["chunks_failed"] == 1

    def test_chunking_failure_no_row(
        self, project_dir, db_conn, monkeypatch,
    ):
        """covers R-G7 — descriptions-chunking-failure-no-row."""
        rel = "pool/seg.wav"
        _analysis_write_wav(project_dir / rel, sr=22050)
        seg_id = _analysis_seed_pool_segment(project_dir, pool_path=rel)
        from scenecraft import audio_intelligence as ai_mod

        def _boom(*a, **kw):
            raise RuntimeError("synthetic chunk failure")

        monkeypatch.setattr(ai_mod, "_chunk_audio_for_gemini", _boom)
        result = chat_mod._exec_generate_descriptions(
            project_dir, {"source_segment_id": seg_id},
        )
        assert "error" in result and "failed to chunk audio" in result["error"]
        assert dac.list_audio_description_runs(project_dir, seg_id) == []

    def test_row_conversion_shapes(self):
        """covers R-G10 — descriptions-row-conversion-shapes."""
        # All branches.
        desc = {
            "section_type": "chorus",
            "mood": "uplifting",
            "energy": 1.5,  # clamped
            "vocal_style": "belt",
            "instrumentation": ["drums", "bass"],
            "notes": "  big build  ",
        }
        rows = chat_mod._rows_from_description(desc, 0.0, 30.0)
        by_prop = {r[2]: r for r in rows}
        assert by_prop["section_type"][3] == "chorus"
        assert by_prop["mood"][3] == "uplifting"
        assert by_prop["energy"][4] == pytest.approx(1.0), "energy-clamped-high"
        assert by_prop["vocal_style"][3] == "belt"
        assert by_prop["instrumentation"][3] == "drums,bass"
        assert by_prop["instrumentation"][6] == {"instruments": ["drums", "bass"]}
        assert by_prop["notes"][3] == "  big build  "  # raw text preserved (per current impl)

        # vocal_style explicit-None branch.
        rows2 = chat_mod._rows_from_description({"vocal_style": None}, 0.0, 30.0)
        assert any(r[2] == "vocal_style" and r[3] is None for r in rows2)

        # Energy clamp low.
        rows3 = chat_mod._rows_from_description({"energy": -0.5}, 0.0, 30.0)
        en = [r for r in rows3 if r[2] == "energy"][0]
        assert en[4] == pytest.approx(0.0)

        # Empty notes after strip — no row.
        rows4 = chat_mod._rows_from_description({"notes": "   "}, 0.0, 30.0)
        assert all(r[2] != "notes" for r in rows4)

    @pytest.mark.xfail(
        reason="target-state R-G15; awaits per-chunk try/except + rate-limit fail-fast (OQ-4)",
        strict=False,
    )
    def test_rate_limit_aborts_run_no_partials(
        self, project_dir, db_conn, monkeypatch,
    ):
        """covers R-G15, OQ-4 — descriptions-rate-limit-aborts-run-no-partials.

        Today: rate-limit exceptions bubble out (R-G14 violation). TARGET:
        per-chunk try/except returns ``{"error": "rate limit: ..."}`` with
        no partial rows persisted.
        """
        rel = "pool/seg.wav"
        _analysis_write_wav(project_dir / rel, sr=22050)
        seg_id = _analysis_seed_pool_segment(project_dir, pool_path=rel)
        from scenecraft import audio_intelligence as ai_mod

        chunks = [{"path": f"/tmp/c{i}.wav", "start_time": float(i * 30),
                   "end_time": float((i + 1) * 30)} for i in range(3)]
        monkeypatch.setattr(
            ai_mod, "_chunk_audio_for_gemini",
            lambda path, chunk_duration: chunks,
        )

        class _RateLimited(Exception):
            pass

        calls = {"n": 0}

        def _maybe_429(path, start, end, model, prompt_version):
            calls["n"] += 1
            if calls["n"] == 2:
                raise _RateLimited("429 quota exhausted")
            return {"section_type": "verse"}

        monkeypatch.setattr(ai_mod, "_gemini_describe_chunk_structured", _maybe_429)
        result = chat_mod._exec_generate_descriptions(
            project_dir, {"source_segment_id": seg_id},
        )
        # TARGET: returned error, no run row, no partial description rows.
        assert "error" in result and "rate limit" in result["error"]
        assert dac.list_audio_description_runs(project_dir, seg_id) == []


# ===========================================================================
# UNIT — TestWaveformPeaks
# ===========================================================================


class TestWaveformPeaks:
    """`compute_peaks` — R-P1..R-P15, Behavior rows 41-52, 57, 59."""

    def test_zero_duration_short_circuits(self, project_dir, tmp_path):
        """covers R-P1 — peaks-zero-duration-short-circuits."""
        src = tmp_path / "x.wav"
        _analysis_write_wav(src, seconds=0.05)
        with mock.patch("scenecraft.audio.peaks.subprocess.Popen") as popen:
            got = peaks_mod.compute_peaks(src, 0.0, 0.0, 400, project_dir=project_dir)
        assert got == b""
        popen.assert_not_called()

    def test_resolution_clamped_low(self, project_dir, tmp_path):
        """covers R-P2 — peaks-resolution-clamped (low end)."""
        src = tmp_path / "x.wav"
        _analysis_write_wav(src, seconds=0.5)
        out = peaks_mod.compute_peaks(src, 0.0, 0.5, 5, project_dir=project_dir)
        # Clamped to 50 → ceil(0.5 * 50) * 2 = 50 bytes
        assert len(out) == 50

    def test_resolution_clamped_high(self, project_dir, tmp_path):
        """covers R-P2 — peaks-resolution-clamped (high end)."""
        src = tmp_path / "x.wav"
        _analysis_write_wav(src, seconds=0.5)
        out = peaks_mod.compute_peaks(src, 0.0, 0.5, 5000, project_dir=project_dir)
        # Clamped to 2000 → ceil(0.5 * 2000) * 2 = 2000 bytes
        assert len(out) == 2000

    def test_cache_hit_skips_ffmpeg(self, project_dir, tmp_path):
        """covers R-P4 — peaks-cache-hit-skips-ffmpeg."""
        src = tmp_path / "x.wav"
        _analysis_write_wav(src)
        key = peaks_mod._cache_key(src, 0.0, 0.1, 400)
        cache_dir = project_dir / "audio_staging" / ".peaks"
        cache_dir.mkdir(parents=True, exist_ok=True)
        cache_file = cache_dir / f"{key}.f16"
        payload = b"\x01\x00" * 40
        cache_file.write_bytes(payload)
        with mock.patch("scenecraft.audio.peaks.subprocess.Popen") as popen:
            got = peaks_mod.compute_peaks(src, 0.0, 0.1, 400, project_dir=project_dir)
        assert got == payload
        popen.assert_not_called()

    def test_cache_miss_decodes_and_writes(self, project_dir, tmp_path):
        """covers R-P5, R-P6 — peaks-cache-miss-decodes-and-writes (real ffmpeg)."""
        if not _has_ffmpeg():
            pytest.skip("ffmpeg not available on PATH")
        src = tmp_path / "x.wav"
        _analysis_write_wav(src, seconds=0.25)
        out = peaks_mod.compute_peaks(src, 0.0, 0.25, 400, project_dir=project_dir)
        # ceil(0.25 * 400) = 100 peaks, 2 bytes each
        assert len(out) == 200
        import numpy as np
        arr = np.frombuffer(out, dtype=np.float16)
        assert arr.size == 100
        assert (arr >= 0.0).all() and (arr <= 1.0).all()
        cache_file = (
            project_dir / "audio_staging" / ".peaks"
            / f"{peaks_mod._cache_key(src, 0.0, 0.25, 400)}.f16"
        )
        assert cache_file.exists()

    def test_ffmpeg_missing_raises(self, project_dir, tmp_path):
        """covers R-P7 — peaks-ffmpeg-missing-raises."""
        src = tmp_path / "x.wav"
        _analysis_write_wav(src, seconds=0.1)
        with mock.patch(
            "scenecraft.audio.peaks.subprocess.Popen",
            side_effect=FileNotFoundError("no ffmpeg"),
        ):
            with pytest.raises(RuntimeError, match="ffmpeg not found"):
                peaks_mod.compute_peaks(src, 0.0, 0.1, 400, project_dir=project_dir)

    def test_ffmpeg_nonzero_exit_raises(self, project_dir, tmp_path):
        """covers R-P8 — peaks-ffmpeg-nonzero-exit-raises."""
        src = tmp_path / "x.wav"
        _analysis_write_wav(src, seconds=0.1)

        class _FakeProc:
            def __init__(self):
                self.stdout = mock.MagicMock()
                self.stdout.read = mock.MagicMock(return_value=b"")
                self.stdout.close = mock.MagicMock()
                self.stderr = mock.MagicMock()
                self.stderr.read = mock.MagicMock(return_value=b"corrupt input")
                self.stderr.close = mock.MagicMock()

            def wait(self, timeout=None):
                return 1

        with mock.patch(
            "scenecraft.audio.peaks.subprocess.Popen", return_value=_FakeProc(),
        ):
            with pytest.raises(RuntimeError, match=r"ffmpeg rc=1"):
                peaks_mod.compute_peaks(src, 0.0, 0.1, 400, project_dir=project_dir)

    def test_ffmpeg_timeout_kills_and_raises(self, project_dir, tmp_path):
        """covers R-P9 — peaks-ffmpeg-timeout-kills-and-raises."""
        src = tmp_path / "x.wav"
        _analysis_write_wav(src, seconds=0.1)

        class _HangProc:
            def __init__(self):
                self.stdout = mock.MagicMock()
                self.stdout.read = mock.MagicMock(return_value=b"")
                self.stdout.close = mock.MagicMock()
                self.stderr = mock.MagicMock()
                self.stderr.read = mock.MagicMock(return_value=b"")
                self.stderr.close = mock.MagicMock()
                self.killed = False

            def wait(self, timeout=None):
                raise subprocess.TimeoutExpired(cmd="ffmpeg", timeout=timeout or 60)

            def kill(self):
                self.killed = True

        proc = _HangProc()
        with mock.patch(
            "scenecraft.audio.peaks.subprocess.Popen", return_value=proc,
        ):
            with pytest.raises(RuntimeError, match="ffmpeg timed out"):
                peaks_mod.compute_peaks(src, 0.0, 0.1, 400, project_dir=project_dir)
        assert proc.killed is True

    def test_cache_write_failure_still_returns(self, project_dir, tmp_path):
        """covers R-P10 — peaks-cache-write-failure-still-returns."""
        if not _has_ffmpeg():
            pytest.skip("ffmpeg not available on PATH")
        src = tmp_path / "x.wav"
        _analysis_write_wav(src, seconds=0.25)
        with mock.patch(
            "pathlib.Path.write_bytes", side_effect=OSError("disk full"),
        ):
            got = peaks_mod.compute_peaks(src, 0.0, 0.25, 400, project_dir=project_dir)
        assert isinstance(got, bytes) and len(got) > 0

    def test_mtime_bump_busts_cache(self, project_dir, tmp_path):
        """covers R-P3 — peaks-mtime-bump-busts-cache."""
        src = tmp_path / "x.wav"
        _analysis_write_wav(src, seconds=0.25)
        old_key = peaks_mod._cache_key(src, 0.0, 0.25, 400)
        time.sleep(0.01)
        _analysis_write_wav(src, seconds=0.30)
        new_key = peaks_mod._cache_key(src, 0.0, 0.25, 400)
        assert new_key != old_key

    def test_no_source_file_watcher(self):
        """covers R-P15 — analysis-no-source-file-watcher."""
        import scenecraft.audio.peaks as p
        import scenecraft.chat as c
        for mod in (p, c):
            src_text = Path(mod.__file__).read_text()
            assert "import watchdog" not in src_text
            assert "import inotify" not in src_text
            assert "import fsevents" not in src_text
            assert "fcntl.flock" not in src_text

    @pytest.mark.xfail(
        reason="target-state R-P14; awaits atomic tmp+rename writes (OQ-5)",
        strict=False,
    )
    def test_concurrent_write_atomic_via_rename(self, project_dir, tmp_path):
        """covers R-P14, OQ-5 — peaks-concurrent-write-atomic-via-rename.

        TARGET: peaks cache writes go through ``<key>.f16.tmp`` then
        ``os.rename``, never via direct ``write_bytes`` to the final path.
        """
        if not _has_ffmpeg():
            pytest.skip("ffmpeg not available on PATH")
        src = tmp_path / "x.wav"
        _analysis_write_wav(src, seconds=0.25)
        observed_paths: list[str] = []
        original_write_bytes = Path.write_bytes

        def _spy(self, data):
            observed_paths.append(str(self))
            return original_write_bytes(self, data)

        with mock.patch.object(Path, "write_bytes", _spy):
            peaks_mod.compute_peaks(src, 0.0, 0.25, 400, project_dir=project_dir)
        # TARGET: at least one observed path ends with ".tmp"
        assert any(p.endswith(".tmp") for p in observed_paths), \
            f"expected atomic .tmp + rename; saw {observed_paths}"


# ===========================================================================
# E2E — TestEndToEnd  (HTTP-level coverage)
# ===========================================================================


def _has_ffmpeg() -> bool:
    try:
        subprocess.run(
            ["ffmpeg", "-version"], capture_output=True, check=False, timeout=2,
        )
        return True
    except (FileNotFoundError, subprocess.SubprocessError):
        return False


class TestEndToEnd:
    """HTTP-level e2e for the parts of the spec that have a REST surface today.

    Today: peaks (`/peaks`) is real REST. The bounce + mix render uploads also
    have HTTP routes (``/bounce-upload``, ``/mix-render-upload``). The four
    chat-driven analysis handlers (``analyze_master_bus``, ``generate_dsp``,
    ``generate_descriptions``, the bounce dispatch) are WS-only — those are
    marked ``xfail(strict=False)`` pending the M16 FastAPI refactor that gives
    them dedicated POST endpoints.
    """

    def test_peaks_route_clip_not_found_404(self, engine_server, project_name):
        """covers R-P11 — peaks-route-error-responses (unknown clip)."""
        status, body = engine_server.json(
            "GET",
            f"/api/projects/{project_name}/audio-clips/ghost/peaks?resolution=400",
        )
        assert status == 404, (status, body)

    def test_peaks_route_pool_seg_not_found_404(self, engine_server, project_name):
        """covers R-P11, R-P13 — peaks pool-route 404 on unknown seg id."""
        status, body = engine_server.json(
            "GET",
            f"/api/projects/{project_name}/pool/ghost/peaks?resolution=400",
        )
        assert status == 404, (status, body)

    @pytest.mark.xfail(
        reason="target-state; awaits dedicated REST endpoints for analyze handlers (M16 FastAPI refactor)",
        strict=False,
    )
    def test_analyze_master_bus_rest_endpoint(self, engine_server, project_name):
        """covers TARGET — POST /api/projects/:name/analyze-master-bus.

        The chat-driven handler currently has no REST surface. Once M16 lands,
        this should be a 200 cache-miss → 200 cache-hit round-trip.
        """
        status, body = engine_server.json(
            "POST",
            f"/api/projects/{project_name}/analyze-master-bus",
            {"sample_rate": 48000, "end_time_s": 1.0},
        )
        assert status == 200, (status, body)

    @pytest.mark.xfail(
        reason="target-state; awaits dedicated REST endpoint for generate-dsp",
        strict=False,
    )
    def test_generate_dsp_rest_endpoint(self, engine_server, project_name):
        """covers TARGET — POST /api/projects/:name/generate-dsp."""
        status, body = engine_server.json(
            "POST",
            f"/api/projects/{project_name}/generate-dsp",
            {"source_segment_id": "any"},
        )
        assert status == 200

    @pytest.mark.xfail(
        reason="target-state; awaits dedicated REST endpoint for generate-descriptions",
        strict=False,
    )
    def test_generate_descriptions_rest_endpoint(self, engine_server, project_name):
        """covers TARGET — POST /api/projects/:name/generate-descriptions."""
        status, body = engine_server.json(
            "POST",
            f"/api/projects/{project_name}/generate-descriptions",
            {"source_segment_id": "any"},
        )
        assert status == 200

    @pytest.mark.xfail(
        reason="target-state; awaits dedicated REST endpoint for bounce-audio (today: chat-WS only)",
        strict=False,
    )
    def test_bounce_audio_rest_endpoint(self, engine_server, project_name):
        """covers TARGET — POST /api/projects/:name/bounce-audio."""
        status, body = engine_server.json(
            "POST",
            f"/api/projects/{project_name}/bounce-audio",
            {"sample_rate": 48000, "end_time_s": 1.0},
        )
        assert status == 200


# pytest-asyncio is installed (pyproject.toml dependency); `@pytest.mark.asyncio`
# is honored without further setup.
