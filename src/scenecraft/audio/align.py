"""Audio-clip alignment via raw-waveform cross-correlation.

Feature: "Align waveforms" — given an anchor clip and N other clips that
should be time-synced to it (multi-mic shoots, re-dubbed takes, music +
room capture, etc.), compute per-clip signed time offsets that would shift
each non-anchor clip so its waveform lines up with the anchor's.

Approach (matches Premiere / Resolve / PluralEyes):
  1. Load a bounded slice of each clip as mono PCM at 16 kHz via librosa
     (which streams decode + downsample + mono-ify in one call). We pass
     offset + duration so a clip sourced from a multi-hour file decodes
     only the seconds we need, not the whole file.
  2. High-pass filter at 80 Hz to remove DC and low-frequency rumble that
     would dominate the correlation without contributing to sync signal.
  3. Zero-mean + L2-normalize each signal (normalized cross-correlation).
     Without this the louder clip's raw magnitude pulls the peak.
  4. FFT cross-correlate anchor vs each other clip on the RAW waveform
     (not an onset envelope). When two mics capture the same event the
     PCM is highly similar, so the correlation peak is narrow and
     sample-accurate. Onset envelopes discard this detail and only give
     hop-length (~23 ms) granularity — too coarse for proper sync.
  5. Argmax gives the lag in samples; divide by sr to get seconds.
  6. Confidence = strength × sharpness, where strength is the peak value
     (0..1 after NCC) and sharpness is (peak - median_abs) / peak.

Prior implementation used onset-envelope cross-correlation, which was
robust to amplitude/EQ differences but too low-resolution to align
recordings of the same source properly.
"""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path
from typing import Iterable

import librosa  # type: ignore[import-not-found]
import numpy as np
from scipy.signal import butter, correlate, sosfiltfilt  # type: ignore[import-not-found]


# 16 kHz mono is enough for sync — Nyquist of 8 kHz covers speech +
# music transients. Decode + correlate time stays modest for multi-minute
# windows. 4 kHz (original) was too aliased; 22 kHz (previous) wasted compute
# without improving peak quality meaningfully.
_SAMPLE_RATE = 16000
_HIGHPASS_HZ = 80.0          # cut DC + low-freq rumble before correlating
_MAX_SYNC_SECONDS = 180.0    # per-clip decode cap. ~11 MB per clip at 16 kHz.
_MAX_LAG_SECONDS = 60.0      # reject offsets > this (likely spurious)


def _log(msg: str):
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] [audio.align] {msg}", file=sys.stderr, flush=True)


def _load_clip_signal(
    project_dir: Path,
    source_path: str,
    source_offset: float,
    playback_duration: float,
    *,
    sr: int = _SAMPLE_RATE,
    max_sync_seconds: float = _MAX_SYNC_SECONDS,
) -> np.ndarray:
    """Decode just the sync window of a clip's source file.

    `playback_duration` is the clip's timeline length; we cap at
    `max_sync_seconds` so one giant clip can't blow memory. librosa
    seeks to `offset` before decoding so the full source is never
    materialised.
    """
    abs_path = (project_dir / source_path).resolve()
    if not abs_path.exists():
        raise FileNotFoundError(f"Audio source not found: {abs_path}")
    sync_dur = max(0.1, min(playback_duration, max_sync_seconds))
    y, _ = librosa.load(
        str(abs_path),
        sr=sr,
        mono=True,
        offset=max(0.0, source_offset),
        duration=sync_dur,
    )
    return y


def _preprocess(y: np.ndarray, *, sr: int = _SAMPLE_RATE,
                highpass_hz: float = _HIGHPASS_HZ) -> np.ndarray:
    """High-pass → zero-mean → L2-normalize. Output norm = 1 (or zero vector)."""
    if len(y) < 16:
        return np.zeros(1, dtype=np.float32)
    # 2nd-order Butterworth high-pass. Zero-phase via filtfilt so no
    # time shift is introduced.
    nyq = sr * 0.5
    if highpass_hz > 0 and highpass_hz < nyq:
        sos = butter(2, highpass_hz / nyq, btype="high", output="sos")
        y = sosfiltfilt(sos, y).astype(np.float32)
    y = y - float(np.mean(y))
    n = float(np.linalg.norm(y))
    return (y / n).astype(np.float32) if n > 1e-9 else y.astype(np.float32)


def _cross_correlate_offset(anchor: np.ndarray, clip: np.ndarray,
                            *, sr: int = _SAMPLE_RATE,
                            max_lag_seconds: float = _MAX_LAG_SECONDS,
                            ) -> tuple[float, float]:
    """Return (lag_seconds, confidence) — raw-waveform normalized cross-correlation.

    Inputs are expected to be already preprocessed (high-pass + zero-mean +
    L2-normalized). Correlates at the full sample rate, so the resulting lag
    is sample-accurate (1/sr seconds precision).

    Sign convention: positive lag_seconds means the clip should shift LATER
    on the timeline to align with the anchor (clip's content is currently
    BEFORE anchor's; pushing clip forward by `lag` lines them up).

    Confidence ∈ [0, 1]: 1 = sharp tall peak, 0 = no clear peak.
    """
    if len(anchor) == 0 or len(clip) == 0:
        return 0.0, 0.0

    # FFT cross-correlation. mode='full' output length = N+M-1; center index
    # (zero lag) is at len(clip) - 1.
    corr = correlate(anchor, clip, mode="full", method="fft")

    # Cap search window to ±max_lag_seconds (in samples now, not frames).
    max_lag_samples = int(max_lag_seconds * sr)
    center = len(clip) - 1
    lo = max(0, center - max_lag_samples)
    hi = min(len(corr), center + max_lag_samples + 1)
    windowed = corr[lo:hi]
    if len(windowed) == 0:
        return 0.0, 0.0

    peak_idx_rel = int(np.argmax(windowed))
    peak_idx_abs = lo + peak_idx_rel
    lag_samples = peak_idx_abs - center
    lag_seconds = float(lag_samples / sr)

    # Confidence = strength × sharpness. After NCC the peak value is in
    # [-1, 1]; strength = clamp(peak, 0, 1). Sharpness = how much it stands
    # above the median absolute value of the correlation.
    peak_val = float(corr[peak_idx_abs])
    median_abs = float(np.median(np.abs(corr)))
    strength = max(0.0, min(1.0, peak_val))
    sharpness = max(0.0, min(1.0, (peak_val - median_abs) / (peak_val + 1e-9))) if peak_val > 0 else 0.0
    confidence = float(strength * sharpness)

    return lag_seconds, confidence


def detect_offsets(
    project_dir: Path,
    clips: Iterable[dict],
    anchor_id: str,
    *,
    sr: int = _SAMPLE_RATE,
    max_sync_seconds: float = _MAX_SYNC_SECONDS,
    max_lag_seconds: float = _MAX_LAG_SECONDS,
    highpass_hz: float = _HIGHPASS_HZ,
) -> tuple[dict[str, float], dict[str, float]]:
    """Compute signed-seconds offsets to align each non-anchor clip to anchor.

    Args:
      project_dir: Project root for resolving clip source paths.
      clips: Iterable of DB rows — each needs `id`, `source_path`,
             `source_offset`, `start_time`, `end_time`.
      anchor_id: The clip that stays put; other clips shift.

    Returns:
      (offsets, confidence) — maps clip_id to signed seconds / [0,1] score.
      Anchor's own entry is always (0.0, 1.0).
    """
    clips_list = list(clips)
    clip_by_id = {c["id"]: c for c in clips_list}
    if anchor_id not in clip_by_id:
        raise ValueError(f"Anchor clip {anchor_id!r} not in clips list")

    # Load + preprocess each clip's raw PCM
    signals: dict[str, np.ndarray] = {}
    for c in clips_list:
        try:
            y = _load_clip_signal(
                project_dir,
                c["source_path"],
                float(c.get("source_offset") or 0.0),
                float(c["end_time"]) - float(c["start_time"]),
                sr=sr, max_sync_seconds=max_sync_seconds,
            )
            signals[c["id"]] = _preprocess(y, sr=sr, highpass_hz=highpass_hz)
        except FileNotFoundError as e:
            _log(f"skipping {c['id']}: {e}")
            signals[c["id"]] = np.zeros(1, dtype=np.float32)

    offsets: dict[str, float] = {anchor_id: 0.0}
    confidence: dict[str, float] = {anchor_id: 1.0}
    anchor_sig = signals[anchor_id]

    for c in clips_list:
        cid = c["id"]
        if cid == anchor_id:
            continue
        lag, conf = _cross_correlate_offset(
            anchor_sig, signals[cid],
            sr=sr, max_lag_seconds=max_lag_seconds,
        )
        offsets[cid] = lag
        confidence[cid] = conf

    _log(
        f"detect_offsets: anchor={anchor_id} n={len(clips_list)} "
        f"offsets={ {k: round(v, 3) for k, v in offsets.items()} } "
        f"confidence={ {k: round(v, 2) for k, v in confidence.items()} }"
    )
    return offsets, confidence
