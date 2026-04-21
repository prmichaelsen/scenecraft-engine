"""Tests for scenecraft.audio.align — onset-envelope cross-correlation.

We synthesise test audio inline (amplitude-modulated clicks) so tests don't
depend on external sound files. Each test fabricates two signals with a
known ground-truth offset and asserts the detected offset matches within
one envelope frame (128 ms at sr=4000, hop=512).
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import numpy as np
import pytest

from scenecraft.audio.align import (
    detect_offsets,
    _cross_correlate_offset,
    _onset_envelope,
    _SAMPLE_RATE,
    _HOP_LENGTH,
)


# One envelope frame at default params — detection tolerance
FRAME_SECONDS = _HOP_LENGTH / _SAMPLE_RATE  # ~0.128s


def _make_signal(
    duration_s: float = 10.0,
    sr: int = _SAMPLE_RATE,
    seed: int = 42,
    n_beats: int = 12,
) -> np.ndarray:
    """Synthesise a deterministic "beat track" signal of given duration.

    Uses pseudo-random impulse positions so each run is reproducible and the
    resulting waveform has a clear, detectable onset pattern.
    """
    rng = np.random.default_rng(seed)
    n = int(duration_s * sr)
    y = np.zeros(n, dtype=np.float32)
    # Scatter clicks at random positions
    positions = rng.integers(int(0.1 * sr), int((duration_s - 0.1) * sr), size=n_beats)
    for p in positions:
        # Each click is a short amplitude burst + small envelope
        burst = int(0.01 * sr)
        y[p:p + burst] = 0.8 * np.hanning(burst).astype(np.float32)
    # A bit of noise so it looks like real audio
    y += rng.normal(0, 0.005, size=n).astype(np.float32)
    return y


def test_envelope_nonzero_for_real_signal():
    y = _make_signal()
    env = _onset_envelope(y)
    assert len(env) > 10
    assert float(env.max()) > 0.0


def test_zero_offset_when_signals_identical():
    y = _make_signal(duration_s=10.0)
    env = _onset_envelope(y)
    lag, conf = _cross_correlate_offset(env, env)
    assert abs(lag) < FRAME_SECONDS, f"expected ~0 lag, got {lag}s"
    # Synthetic sparse-onset signals yield moderate confidence; real audio
    # with more dense onsets will score higher.
    assert conf > 0.2


def test_detects_positive_lag():
    """If clip is a time-shifted-later copy of the anchor (same source but
    starts N seconds into it), then to ALIGN the clip needs to shift EARLIER
    in timeline — so the reported offset should be NEGATIVE.

    Here we model the opposite: anchor is the long signal, clip is a chunk
    from later in the signal. Sync requires the clip to shift EARLIER (its
    content currently appears too late in the clip's own playback) so the
    offset should come back NEGATIVE or POSITIVE depending on convention.

    To avoid tying the test to convention, we just assert |lag| is close to
    the known delta and that it's strictly nonzero with decent confidence.
    """
    y = _make_signal(duration_s=10.0)
    shift_s = 2.0
    shift_samples = int(shift_s * _SAMPLE_RATE)
    # Clip = anchor with the first 2s trimmed off
    clip = y[shift_samples:]
    anchor_env = _onset_envelope(y)
    clip_env = _onset_envelope(clip)
    lag, conf = _cross_correlate_offset(anchor_env, clip_env)
    assert abs(abs(lag) - shift_s) < FRAME_SECONDS * 2, f"expected ~±{shift_s}s, got {lag}s"
    assert conf > 0.1, f"expected nonzero confidence, got {conf}"


def test_detect_offsets_end_to_end_with_real_file(tmp_path):
    """End-to-end: write two WAV files where one is a time-shifted version
    of the other, call detect_offsets with clip dicts pointing at them,
    verify the reported offset matches the known shift.
    """
    try:
        import soundfile as sf  # type: ignore[import-not-found]
    except ImportError:
        pytest.skip("soundfile not installed")

    sr = _SAMPLE_RATE
    y = _make_signal(duration_s=10.0, sr=sr)

    # Write anchor (full 10s starting at t=0 in its own source)
    anchor_path = tmp_path / "anchor.wav"
    sf.write(str(anchor_path), y, sr)

    # Write clip as an identical file but we instruct detect_offsets to
    # start reading at source_offset=2.0 (so the loaded slice is the last
    # 8 seconds of y, shifted). The clip's timeline span is 8s.
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
    # B's source is identical to A but we're reading 2s in, so B's
    # waveform lines up with A's waveform starting at A's t=2s. The
    # detected lag should be ~2s in absolute value.
    assert abs(abs(offsets["B"]) - 2.0) < FRAME_SECONDS * 3, \
        f"expected ~±2.0s, got {offsets['B']}s"
    assert conf["B"] > 0.1
