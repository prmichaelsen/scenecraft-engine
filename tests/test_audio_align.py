"""Tests for scenecraft.audio.align — band-limited envelope cross-correlation.

Two kinds of coverage:
  - Synthetic signals with known ground-truth offsets (identity + shift)
  - Real comedy-show audio from the oktoberfest_show_01 project, with
    applied shifts and mic-style distortions, to verify the algorithm
    handles the cross-mic case it exists to solve.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from scenecraft.audio.align import (
    _SAMPLE_RATE,
    _cross_correlate_offset,
    _preprocess,
    detect_offsets,
)


# Detection tolerance: a few samples at the analysis sample rate.
SAMPLE_TOLERANCE_SECONDS = 4.0 / _SAMPLE_RATE


def _make_signal(
    duration_s: float = 10.0,
    sr: int = _SAMPLE_RATE,
    seed: int = 42,
    n_beats: int = 12,
) -> np.ndarray:
    """Synthesise a deterministic "beat track" signal of given duration.

    Uses pseudo-random impulse positions so each run is reproducible and the
    resulting waveform has clear, detectable transients.
    """
    rng = np.random.default_rng(seed)
    n = int(duration_s * sr)
    y = np.zeros(n, dtype=np.float32)
    positions = rng.integers(int(0.1 * sr), int((duration_s - 0.1) * sr), size=n_beats)
    for p in positions:
        burst = int(0.01 * sr)
        y[p:p + burst] = 0.8 * np.hanning(burst).astype(np.float32)
    y += rng.normal(0, 0.005, size=n).astype(np.float32)
    return y


def test_preprocess_produces_unit_norm():
    y = _make_signal()
    s = _preprocess(y)
    assert abs(float(np.linalg.norm(s)) - 1.0) < 1e-4


def test_zero_offset_when_signals_identical():
    y = _make_signal(duration_s=10.0)
    s = _preprocess(y)
    lag, conf = _cross_correlate_offset(s, s)
    assert abs(lag) < SAMPLE_TOLERANCE_SECONDS, f"expected ~0 lag, got {lag}s"
    # Identical normalized signals give a peak of 1 (or very close).
    assert conf > 0.8, f"expected high confidence, got {conf}"


def test_detects_known_shift():
    """clip = anchor with the first 2s trimmed off. The correlation peak
    should land at |lag| ≈ 2.0 s (sign depends on correlate convention).
    """
    y = _make_signal(duration_s=10.0)
    shift_s = 2.0
    shift_samples = int(shift_s * _SAMPLE_RATE)
    clip = y[shift_samples:]
    anchor_sig = _preprocess(y)
    clip_sig = _preprocess(clip)
    lag, conf = _cross_correlate_offset(anchor_sig, clip_sig)
    assert abs(abs(lag) - shift_s) < SAMPLE_TOLERANCE_SECONDS * 4, \
        f"expected ~±{shift_s}s, got {lag}s"
    assert conf > 0.1, f"expected nonzero confidence, got {conf}"


FIXTURE_MIC = Path(__file__).parent / "fixtures" / "oktoberfest_mic_slice_15min.ogg"


def _load_fixture_mic() -> np.ndarray:
    """Load the 15 min oktoberfest_show_01 comedian-mic fixture (16 kHz mono).

    Real stand-up-comedy audio — dense continuous speech with laughter
    breaks — from a 2.4 h live recording. Multi-minute length exercises
    larger shifts and realistic decode windows, matching the real workflow
    where source mics have minute-scale start-time differences.
    """
    import soundfile as sf  # type: ignore[import-not-found]
    y, sr = sf.read(str(FIXTURE_MIC), dtype="float32")
    if sr != _SAMPLE_RATE:
        raise RuntimeError(f"fixture sr {sr} != expected {_SAMPLE_RATE}")
    if y.ndim > 1:
        y = y.mean(axis=1)
    return y.astype(np.float32)


@pytest.mark.skipif(not FIXTURE_MIC.exists(), reason="mic fixture missing")
def test_aligns_real_comedian_mic_with_small_shift():
    """Real comedy audio, 3.5 s known shift. Baseline sanity."""
    y = _load_fixture_mic()
    shift_s = 3.5
    shift_samples = int(shift_s * _SAMPLE_RATE)
    shifted = y[shift_samples:]
    a = _preprocess(y)
    b = _preprocess(shifted)
    lag, conf = _cross_correlate_offset(a, b)
    assert abs(abs(lag) - shift_s) < 0.05, f"expected ~±{shift_s}s, got {lag}s"
    assert conf > 0.3, f"confidence too low: {conf}"


@pytest.mark.skipif(not FIXTURE_MIC.exists(), reason="mic fixture missing")
def test_aligns_real_comedian_mic_with_large_shift():
    """Real comedy audio, 90 s known shift — minute-scale start offsets are
    typical for multi-recorder live shoots. Verifies max_lag_seconds default
    accommodates real workflows.
    """
    y = _load_fixture_mic()
    shift_s = 90.0
    shift_samples = int(shift_s * _SAMPLE_RATE)
    shifted = y[shift_samples:]
    a = _preprocess(y)
    b = _preprocess(shifted)
    lag, conf = _cross_correlate_offset(a, b)
    assert abs(abs(lag) - shift_s) < 0.1, f"expected ~±{shift_s}s, got {lag}s"
    assert conf > 0.2, f"confidence too low: {conf}"


@pytest.mark.skipif(not FIXTURE_MIC.exists(), reason="mic fixture missing")
def test_aligns_real_audio_through_cross_mic_distortions():
    """Simulate cross-mic case on REAL comedy audio: apply a mic-like
    band-pass (500–3000 Hz), add 10 ms room delay, add ambience noise,
    then a known 2.0 s sync shift. Envelope correlation must still recover
    the shift — this is the failure mode raw-PCM correlation hits in
    multi-mic shoots (comedian lav vs crowd shotgun vs camera audio).
    """
    from scipy.signal import butter as _butter, sosfiltfilt as _sosfiltfilt

    y = _load_fixture_mic()
    sr = _SAMPLE_RATE
    nyq = sr * 0.5

    # Different-mic EQ: narrower band-pass than our analyzer uses
    sos_bp = _butter(2, [500 / nyq, 3000 / nyq], btype="band", output="sos")
    b_eq = _sosfiltfilt(sos_bp, y).astype(np.float32)
    # Room reflection / distance delay
    delay_samples = int(0.01 * sr)
    b_phase = np.concatenate([np.zeros(delay_samples, dtype=np.float32),
                               b_eq[:-delay_samples]])
    # Crowd ambience
    rng = np.random.default_rng(7)
    b_noisy = b_phase + rng.normal(0, 0.02, size=len(b_phase)).astype(np.float32)

    shift_s = 2.0
    shift_samples = int(shift_s * sr)
    mic_b = b_noisy[shift_samples:]

    a = _preprocess(y)
    b = _preprocess(mic_b)
    lag, conf = _cross_correlate_offset(a, b)
    assert abs(abs(lag) - shift_s) < 0.10, f"expected ~±{shift_s}s, got {lag}s"
    assert conf > 0.1, f"confidence too low: {conf}"


def test_detect_offsets_end_to_end_with_real_file(tmp_path):
    """End-to-end: write two WAV files where one is a time-shifted version
    of the other, call detect_offsets, verify the offset matches.
    """
    try:
        import soundfile as sf  # type: ignore[import-not-found]
    except ImportError:
        pytest.skip("soundfile not installed")

    sr = _SAMPLE_RATE
    y = _make_signal(duration_s=10.0, sr=sr)

    anchor_path = tmp_path / "anchor.wav"
    sf.write(str(anchor_path), y, sr)
    clip_path = tmp_path / "clip.wav"
    sf.write(str(clip_path), y, sr)

    clips = [
        {"id": "A", "source_path": "anchor.wav", "source_offset": 0.0,
         "start_time": 0.0, "end_time": 10.0},
        {"id": "B", "source_path": "clip.wav", "source_offset": 2.0,
         "start_time": 0.0, "end_time": 8.0},
    ]

    offsets, conf = detect_offsets(tmp_path, clips, "A")
    assert offsets["A"] == 0.0
    assert conf["A"] == 1.0
    assert abs(abs(offsets["B"]) - 2.0) < 0.01, \
        f"expected ~±2.0s (sample-accurate), got {offsets['B']}s"
    assert conf["B"] > 0.1
