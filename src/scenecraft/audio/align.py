"""Audio-clip alignment via band-limited amplitude envelope cross-correlation.

Feature: "Align waveforms" — given an anchor clip and N other clips that
should be time-synced to it (multi-mic shoots, comedy/concert/event
recordings with vocals mic + crowd mics + camera audio, re-dubbed takes,
etc.), compute per-clip signed time offsets that would shift each
non-anchor clip so its content lines up with the anchor's.

Approach (matches PluralEyes / Premiere's Synchronize for cross-mic sync):

  1. Load a bounded slice of each clip as mono PCM at 16 kHz via librosa.
     Streams the decode from `source_offset`; multi-hour source files
     never fully materialise.
  2. Band-pass 200-4000 Hz. Voice fundamentals + first two formants, and
     the strongest energy of laughter / applause, live here. Rejecting
     sub-200 Hz kills room rumble, stage thump, and HVAC that would
     otherwise dominate the envelope. Rejecting above 4 kHz kills mic
     hiss and cymbal splash that differ wildly between mics.
  3. Rectify + low-pass at 50 Hz → smooth amplitude envelope. This
     represents WHEN energy spikes happen (a laugh, a consonant onset, a
     punchline), which is identical across all mics recording the same
     acoustic event. HOW the energy looks in the raw waveform is not —
     phase, EQ, proximity effect, AGC all mangle raw PCM differently at
     each mic, which is why raw-waveform correlation fails on real-world
     multi-mic sets.
  4. Decimate to 200 Hz. Envelope precision is 5 ms per frame — well
     below the 20 ms threshold at which humans perceive AV sync error.
  5. Zero-mean + L2-normalize → normalized cross-correlation.
  6. FFT correlate; argmax in envelope samples → lag in seconds (÷ env_sr).
  7. Confidence = strength × sharpness; peak value after NCC ∈ [-1, 1].

Prior implementations tried raw-PCM cross-correlation (fails when mics
differ) and onset-envelope correlation (too coarse time-resolution and
too sensitive to absolute peak magnitudes). Band-limited amplitude
envelope is the sweet spot for real-world event audio.
"""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path
from typing import Iterable

import librosa  # type: ignore[import-not-found]
import numpy as np
from scipy.signal import butter, correlate, sosfiltfilt  # type: ignore[import-not-found]


_SAMPLE_RATE = 16000         # decode sample rate
_BAND_LOW_HZ = 200.0         # band-pass: remove rumble + DC
_BAND_HIGH_HZ = 4000.0       # band-pass: remove hiss + cymbal splash
_ENVELOPE_SR = 200           # envelope rate — 5 ms precision
_ENVELOPE_LP_HZ = 50.0       # lowpass cutoff for envelope smoothing
_MAX_SYNC_SECONDS = 600.0    # per-clip decode cap — raised for long event recordings
_MAX_LAG_SECONDS = 120.0     # reject offsets > this (likely spurious)


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


def _preprocess(y: np.ndarray, *, sr: int = _SAMPLE_RATE) -> np.ndarray:
    """Audio → band-limited amplitude envelope at _ENVELOPE_SR, L2-normalized.

    Pipeline: band-pass 200–4000 Hz → rectify → lowpass 50 Hz → decimate to
    200 Hz → zero-mean + L2-normalize. The resulting envelope represents
    WHEN acoustic energy spikes occur and is largely invariant to mic
    position, EQ, gain, AGC, and proximity effect — which is what makes it
    work for cross-mic sync where raw-PCM correlation fails.
    """
    if len(y) < sr // _ENVELOPE_SR:
        return np.zeros(1, dtype=np.float32)
    nyq = sr * 0.5

    # Band-pass to voice + laughter range. Zero-phase filtfilt leaves no
    # time shift. 4th-order total (2 passes of 2nd-order SOS).
    band_low = max(1.0, min(_BAND_LOW_HZ, nyq - 1))
    band_high = max(band_low + 1.0, min(_BAND_HIGH_HZ, nyq - 1))
    sos_bp = butter(2, [band_low / nyq, band_high / nyq], btype="band", output="sos")
    y_bp = sosfiltfilt(sos_bp, y)

    # Rectify (absolute value), then low-pass to get a smooth amplitude
    # envelope. 50 Hz cutoff preserves the 10–50 Hz modulation that
    # characterises laughter/speech, kills anything faster.
    y_rect = np.abs(y_bp)
    lp_hz = min(_ENVELOPE_LP_HZ, nyq - 1)
    sos_lp = butter(2, lp_hz / nyq, btype="low", output="sos")
    y_env = sosfiltfilt(sos_lp, y_rect).astype(np.float32)

    # Decimate to _ENVELOPE_SR via simple stride (signal is already
    # bandwidth-limited above, so no aliasing concern).
    step = max(1, sr // _ENVELOPE_SR)
    env = y_env[::step]

    env = env - float(np.mean(env))
    n = float(np.linalg.norm(env))
    return (env / n).astype(np.float32) if n > 1e-9 else env.astype(np.float32)


def _cross_correlate_offset(anchor: np.ndarray, clip: np.ndarray,
                            *, env_sr: int = _ENVELOPE_SR,
                            max_lag_seconds: float = _MAX_LAG_SECONDS,
                            ) -> tuple[float, float]:
    """Return (lag_seconds, confidence) — envelope normalized cross-correlation.

    Inputs are expected to be envelopes from `_preprocess` (already zero-mean
    and L2-normalized at `env_sr` samples/sec). Precision: 1/env_sr = 5 ms at
    default settings. Well under the 20 ms human threshold for AV sync error.

    Sign convention: positive lag_seconds means the clip should shift LATER
    on the timeline to align with the anchor (clip's content is currently
    BEFORE anchor's; pushing clip forward by `lag` lines them up).

    Confidence ∈ [0, 1]: 1 = sharp tall peak, 0 = no clear peak.
    """
    if len(anchor) == 0 or len(clip) == 0:
        return 0.0, 0.0

    corr = correlate(anchor, clip, mode="full", method="fft")

    # Cap search window to ±max_lag_seconds in envelope frames.
    max_lag_frames = int(max_lag_seconds * env_sr)
    center = len(clip) - 1
    lo = max(0, center - max_lag_frames)
    hi = min(len(corr), center + max_lag_frames + 1)
    windowed = corr[lo:hi]
    if len(windowed) == 0:
        return 0.0, 0.0

    peak_idx_rel = int(np.argmax(windowed))
    peak_idx_abs = lo + peak_idx_rel
    lag_frames = peak_idx_abs - center
    lag_seconds = float(lag_frames / env_sr)

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
            signals[c["id"]] = _preprocess(y, sr=sr)
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
            max_lag_seconds=max_lag_seconds,
        )
        offsets[cid] = lag
        confidence[cid] = conf

    _log(
        f"detect_offsets: anchor={anchor_id} n={len(clips_list)} "
        f"offsets={ {k: round(v, 3) for k, v in offsets.items()} } "
        f"confidence={ {k: round(v, 2) for k, v in confidence.items()} }"
    )
    return offsets, confidence
