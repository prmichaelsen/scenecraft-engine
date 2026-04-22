"""Tests for scenecraft.audio.align — raw-waveform cross-correlation.

We synthesise test audio inline (pseudo-random clicks) so tests don't depend
on external sound files. Each test fabricates two signals with a known
ground-truth offset and asserts the detected offset matches to within one
sample's-worth of tolerance at the operating sample rate.
"""

from __future__ import annotations

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
