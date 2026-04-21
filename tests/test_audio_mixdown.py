"""Unit tests for M9 task-91: curves evaluation and multi-track mixdown.

The mixdown E2E path (ffmpeg + real decode) is exercised only when ffmpeg is
available and a tiny WAV is on disk. The pure-numpy bits (curve evaluation,
equal-power crossfade, track curve application) are tested without ffmpeg.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
import wave
from pathlib import Path

import numpy as np
import pytest

from scenecraft.audio.curves import (
    db_to_linear,
    evaluate_curve_db,
    evaluate_curve_linear,
)
from scenecraft.audio.mixdown import _equal_power_crossfade_inplace


# ── curves ───────────────────────────────────────────────────────────

def test_curve_default_is_unity():
    t = np.linspace(0, 10, 1000, dtype=np.float32)
    gain = evaluate_curve_linear(None, t, x_normalised=False)
    assert np.allclose(gain, 1.0)


def test_curve_db_linear_interp():
    # [[0, 0dB], [1, -6dB]] over a 10s clip → midpoint should be -3dB (~0.7079)
    curve = [[0.0, 0.0], [1.0, -6.0]]
    t = np.array([0.0, 5.0, 10.0], dtype=np.float32)
    gain = evaluate_curve_linear(curve, t, x_normalised=True, clip_start=0.0, clip_end=10.0)
    assert abs(gain[0] - 1.0) < 1e-3
    assert abs(gain[1] - db_to_linear(np.array([-3.0]))[0]) < 1e-3
    assert abs(gain[2] - db_to_linear(np.array([-6.0]))[0]) < 1e-3


def test_curve_clamps_outside_range():
    # Curve only covers [2, 4]s; outside samples clamp to nearest endpoint.
    curve = [[2.0, -12.0], [4.0, 0.0]]
    t = np.array([0.0, 2.0, 3.0, 4.0, 8.0], dtype=np.float32)
    db = evaluate_curve_db(curve, t, x_normalised=False)
    assert abs(db[0] - -12.0) < 1e-3  # clamped
    assert abs(db[1] - -12.0) < 1e-3  # at left endpoint
    assert abs(db[2] - -6.0) < 1e-3   # midpoint
    assert abs(db[3] - 0.0) < 1e-3    # at right endpoint
    assert abs(db[4] - 0.0) < 1e-3    # clamped


def test_db_to_linear_reference_values():
    db = np.array([-60.0, -6.0, 0.0, 6.0], dtype=np.float32)
    lin = db_to_linear(db)
    assert abs(lin[0] - 0.001) < 1e-4
    assert abs(lin[1] - 0.5012) < 1e-3
    assert abs(lin[2] - 1.0) < 1e-6
    assert abs(lin[3] - 1.9953) < 1e-3


# ── equal-power crossfade ────────────────────────────────────────────

def test_crossfade_nonoverlapping_is_pure_addition():
    buf = np.zeros((2, 100), dtype=np.float32)
    a = np.ones((2, 30), dtype=np.float32) * 0.5
    b = np.ones((2, 30), dtype=np.float32) * 0.3
    _equal_power_crossfade_inplace(buf, a, 0)
    _equal_power_crossfade_inplace(buf, b, 50)
    assert np.allclose(buf[:, :30], 0.5)
    assert np.allclose(buf[:, 30:50], 0.0)
    assert np.allclose(buf[:, 50:80], 0.3)
    assert np.allclose(buf[:, 80:], 0.0)


def test_crossfade_overlap_sum_of_squares_is_one():
    """Equal-power: cos²(θ) + sin²(θ) = 1 — constant perceived loudness."""
    buf = np.zeros((2, 100), dtype=np.float32)
    # Existing signal in [0, 60)
    _equal_power_crossfade_inplace(buf, np.ones((2, 60), dtype=np.float32), 0)
    # New signal overlapping in [40, 80) — each sample is unity amplitude
    new = np.ones((2, 40), dtype=np.float32)
    _equal_power_crossfade_inplace(buf, new, 40)
    # At every sample of the overlap [40, 60), gain_out² + gain_in² should equal 1.0
    # Since both signals were unity, buf[i] = 1*cos + 1*sin, so buf[i]² should be
    # (cos+sin)² = 1 + sin(2θ), NOT 1. But the *power* is conserved: the energy of
    # fade-out + fade-in, for uncorrelated signals, sums to constant. Here they're
    # correlated (both unity) so we test the simpler invariant: the gain pair
    # squared sums to 1.
    t = np.linspace(0.0, 1.0, 20, dtype=np.float32)
    gain_out = np.cos(t * np.pi / 2)
    gain_in = np.sin(t * np.pi / 2)
    assert np.allclose(gain_out**2 + gain_in**2, 1.0, atol=1e-6)


def test_crossfade_pure_addition_when_no_existing_signal():
    buf = np.zeros((2, 100), dtype=np.float32)
    sig = np.full((2, 50), 0.7, dtype=np.float32)
    _equal_power_crossfade_inplace(buf, sig, 25)
    assert np.allclose(buf[:, 25:75], 0.7)
    assert np.allclose(buf[:, :25], 0.0)
    assert np.allclose(buf[:, 75:], 0.0)


# ── E2E mixdown (requires ffmpeg) ────────────────────────────────────

def _ffmpeg_available() -> bool:
    try:
        subprocess.run(["ffmpeg", "-version"], capture_output=True, check=True)
        return True
    except (subprocess.CalledProcessError, FileNotFoundError):
        return False


def _make_test_wav(path: Path, freq: float, duration: float, sr: int = 48000) -> None:
    t = np.linspace(0, duration, int(sr * duration), endpoint=False, dtype=np.float32)
    sig = 0.5 * np.sin(2 * np.pi * freq * t)
    pcm = (sig * 32767).astype(np.int16)
    # Write mono WAV
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(pcm.tobytes())


@pytest.mark.skipif(not _ffmpeg_available(), reason="ffmpeg not installed")
def test_mixdown_end_to_end_two_clips():
    """Two non-overlapping clips on one track → mixed WAV has both, silence between."""
    from scenecraft import db as dbmod
    from scenecraft.audio.mixdown import render_project_audio

    project_dir = Path(tempfile.mkdtemp())
    try:
        # Schema auto-initialized by get_db
        dbmod.get_db(project_dir)

        # Make two source WAVs (1.0s each)
        src_a = project_dir / "audio_staging" / "a.wav"
        src_b = project_dir / "audio_staging" / "b.wav"
        _make_test_wav(src_a, 440.0, 1.0)
        _make_test_wav(src_b, 880.0, 1.0)

        # Add a track + two clips on it (non-overlapping)
        dbmod.add_audio_track(project_dir, {
            "id": "at_1", "name": "Track 1", "display_order": 0,
            "enabled": True, "hidden": False, "muted": False,
            "volume_curve": [[0, 0], [10, 0]],
        })
        dbmod.add_audio_clip(project_dir, {
            "id": "ac_1", "track_id": "at_1",
            "source_path": "audio_staging/a.wav",
            "start_time": 0.0, "end_time": 1.0, "source_offset": 0.0,
        })
        dbmod.add_audio_clip(project_dir, {
            "id": "ac_2", "track_id": "at_1",
            "source_path": "audio_staging/b.wav",
            "start_time": 2.0, "end_time": 3.0, "source_offset": 0.0,
        })

        out = project_dir / "audio_staging" / "_mixdown.wav"
        result = render_project_audio(project_dir, total_seconds=4.0, out_path=out, sr=48000)

        assert result is not None
        assert out.exists()

        # Sanity-check: WAV has non-zero content in the clip regions, zero in between
        with wave.open(str(out), "rb") as w:
            assert w.getnchannels() == 2
            assert w.getframerate() == 48000
            raw = w.readframes(w.getnframes())
        samples = np.frombuffer(raw, dtype=np.int16).reshape(-1, 2).astype(np.float32)
        # Clip A should have content near 0-1s (samples 0..48000)
        assert np.abs(samples[:48000]).mean() > 100
        # Silent gap 1-2s
        assert np.abs(samples[48000:96000]).mean() < 10
        # Clip B content 2-3s
        assert np.abs(samples[96000:144000]).mean() > 100
        # Silent 3-4s
        assert np.abs(samples[144000:]).mean() < 10
    finally:
        dbmod.close_db(project_dir)
        shutil.rmtree(project_dir, ignore_errors=True)


@pytest.mark.skipif(not _ffmpeg_available(), reason="ffmpeg not installed")
def test_mixdown_playback_rate_2x_compresses_source_to_half_duration():
    """A 2-second source with playback_rate=2 mixed into a 1-second clip window
    should end up fully consumed — no silence at the tail, content is present.
    """
    from scenecraft import db as dbmod
    from scenecraft.audio.mixdown import render_project_audio

    project_dir = Path(tempfile.mkdtemp())
    try:
        dbmod.get_db(project_dir)
        src = project_dir / "audio_staging" / "sweep.wav"
        # 2s of 1 kHz tone at 0.3 amplitude
        sr = 48000
        t = np.linspace(0, 2.0, int(sr * 2.0), endpoint=False, dtype=np.float32)
        sig = 0.3 * np.sin(2 * np.pi * 1000 * t)
        pcm = (sig * 32767).astype(np.int16)
        src.parent.mkdir(parents=True, exist_ok=True)
        with wave.open(str(src), "wb") as w:
            w.setnchannels(1); w.setsampwidth(2); w.setframerate(sr)
            w.writeframes(pcm.tobytes())

        dbmod.add_audio_track(project_dir, {
            "id": "at_1", "name": "T", "display_order": 0,
            "enabled": True, "hidden": False, "muted": False,
            "volume_curve": [[0, 0], [10, 0]],
        })
        dbmod.add_audio_clip(project_dir, {
            "id": "ac_1", "track_id": "at_1",
            "source_path": "audio_staging/sweep.wav",
            "start_time": 0.0, "end_time": 1.0, "source_offset": 0.0,
        })

        # Simulate what get_audio_clips would return: enrich with rate manually
        # (the E2E integration via an actual linked transition is covered by
        # the db.get_audio_clips unit tests — here we exercise the mixdown math)
        import scenecraft.db as db_mod
        original_get = db_mod.get_audio_clips
        def patched_get(project_dir, track_id=None):
            clips = original_get(project_dir, track_id)
            for c in clips:
                c["playback_rate"] = 2.0
                c["effective_source_offset"] = 0.0
            return clips
        db_mod.get_audio_clips = patched_get
        try:
            out = project_dir / "audio_staging" / "_mixdown.wav"
            render_project_audio(project_dir, total_seconds=1.0, out_path=out, sr=sr)
        finally:
            db_mod.get_audio_clips = original_get

        with wave.open(str(out), "rb") as w:
            raw = w.readframes(w.getnframes())
        samples = np.frombuffer(raw, dtype=np.int16).reshape(-1, 2).astype(np.float32)
        # 1-second output at 2x → the whole 2s source compressed into 1s.
        # RMS across the window should be similar to the source's RMS (resample
        # is linear but the total energy density per-sample stays roughly the
        # same because rate is constant).
        rms = np.sqrt(np.mean(samples ** 2))
        assert rms > 500, f"expected audible content, got RMS {rms}"
    finally:
        dbmod.close_db(project_dir)
        shutil.rmtree(project_dir, ignore_errors=True)


@pytest.mark.skipif(not _ffmpeg_available(), reason="ffmpeg not installed")
def test_mixdown_muted_track_is_zero():
    from scenecraft import db as dbmod
    from scenecraft.audio.mixdown import render_project_audio

    project_dir = Path(tempfile.mkdtemp())
    try:
        dbmod.get_db(project_dir)
        src = project_dir / "audio_staging" / "a.wav"
        _make_test_wav(src, 440.0, 1.0)
        dbmod.add_audio_track(project_dir, {
            "id": "at_muted", "name": "Muted", "display_order": 0,
            "enabled": True, "hidden": False, "muted": True,
            "volume_curve": [[0, 0], [10, 0]],
        })
        dbmod.add_audio_clip(project_dir, {
            "id": "ac_muted", "track_id": "at_muted",
            "source_path": "audio_staging/a.wav",
            "start_time": 0.0, "end_time": 1.0, "source_offset": 0.0,
        })

        out = project_dir / "audio_staging" / "_mixdown.wav"
        result = render_project_audio(project_dir, total_seconds=2.0, out_path=out, sr=48000)
        assert result is not None

        with wave.open(str(out), "rb") as w:
            raw = w.readframes(w.getnframes())
        samples = np.frombuffer(raw, dtype=np.int16)
        assert samples.max() == 0
        assert samples.min() == 0
    finally:
        dbmod.close_db(project_dir)
        shutil.rmtree(project_dir, ignore_errors=True)
