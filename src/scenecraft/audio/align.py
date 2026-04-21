"""Audio-clip alignment via onset-envelope cross-correlation (librosa-based).

Feature: "Align waveforms" — given an anchor clip and N other clips that
should be time-synced to it (multi-mic shoots, re-dubbed takes, music +
room capture, etc.), compute per-clip signed time offsets that would shift
each non-anchor clip so its waveform lines up with the anchor's.

Approach:
  1. Load a bounded slice of each clip via librosa (which decodes + down-
     samples + mono-ifies in one call, using ffmpeg/soundfile under the
     hood). We pass offset + duration so a clip sourced from a multi-hour
     file decodes only the seconds we need, not the whole file.
  2. Compute onset-strength envelope for each signal (spectral flux). This
     representation is invariant to gain/EQ/mic differences — what matters
     is WHERE peaks happen, not their absolute magnitude.
  3. FFT cross-correlate the anchor's envelope against each other clip's
     envelope. The argmax gives the lag in frames; multiply by hop_length /
     sr to convert to seconds.
  4. Derive a confidence score from the peak height relative to the 95th
     percentile of the correlation — a sharp clear peak reads high, a
     diffuse plateau reads low.

This is the same broad approach professional sync tools (Acoustica,
PluralEyes, Premiere's Synchronize) use internally.
"""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path
from typing import Iterable

import librosa  # type: ignore[import-not-found]
import numpy as np
from scipy.signal import correlate  # type: ignore[import-not-found]


_SAMPLE_RATE = 4000   # 4 kHz mono is enough for onset-envelope sync
_HOP_LENGTH = 512     # onset envelope frame rate: ~8 fps at 4 kHz
_MAX_SYNC_SECONDS = 90.0  # cap per-clip decode to bound memory
_MAX_LAG_SECONDS = 30.0   # reject offsets > this (likely spurious)


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


def _onset_envelope(y: np.ndarray, *, sr: int = _SAMPLE_RATE,
                    hop_length: int = _HOP_LENGTH) -> np.ndarray:
    """Onset-strength envelope — amplitude-invariant temporal fingerprint."""
    if len(y) < hop_length:
        # Signal shorter than one frame; return a zero envelope so the
        # correlation will yield a peak of 0 (→ confidence 0, offset 0).
        return np.zeros(1, dtype=np.float32)
    env = librosa.onset.onset_strength(y=y, sr=sr, hop_length=hop_length)
    return env.astype(np.float32)


def _cross_correlate_offset(anchor_env: np.ndarray, clip_env: np.ndarray,
                            *, sr: int = _SAMPLE_RATE,
                            hop_length: int = _HOP_LENGTH,
                            max_lag_seconds: float = _MAX_LAG_SECONDS,
                            ) -> tuple[float, float]:
    """Return (lag_seconds, confidence).

    Sign convention: positive lag_seconds means the clip should shift LATER
    on the timeline to align with the anchor (clip's first-onset is currently
    BEFORE anchor's first-onset; pushing clip forward by `lag` puts them
    together).

    Confidence ∈ [0, 1]: 1 = peak towers over baseline, 0 = no clear peak.
    """
    if len(anchor_env) == 0 or len(clip_env) == 0:
        return 0.0, 0.0

    # mode='full' gives correlation at every possible lag; center index is
    # len(clip_env) - 1 (where 0-lag sits).
    corr = correlate(anchor_env, clip_env, mode="full", method="fft")

    # Cap the search window to ±max_lag_seconds
    max_lag_frames = int(max_lag_seconds * sr / hop_length)
    center = len(clip_env) - 1
    lo = max(0, center - max_lag_frames)
    hi = min(len(corr), center + max_lag_frames + 1)
    windowed = corr[lo:hi]
    if len(windowed) == 0:
        return 0.0, 0.0

    peak_idx_rel = int(np.argmax(windowed))
    peak_idx_abs = lo + peak_idx_rel
    lag_frames = peak_idx_abs - center
    lag_seconds = float(lag_frames * hop_length / sr)

    # Confidence: peak height versus the 95th-percentile of the correlation.
    # Normalise by peak so the result is bounded and scale-invariant.
    peak_val = float(corr[peak_idx_abs])
    background = float(np.percentile(np.abs(corr), 95))
    if peak_val <= 0.0:
        confidence = 0.0
    else:
        confidence = float(max(0.0, min(1.0, (peak_val - background) / (peak_val + 1e-9))))

    return lag_seconds, confidence


def detect_offsets(
    project_dir: Path,
    clips: Iterable[dict],
    anchor_id: str,
    *,
    sr: int = _SAMPLE_RATE,
    hop_length: int = _HOP_LENGTH,
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

    # Load + envelope-extract each clip
    envs: dict[str, np.ndarray] = {}
    for c in clips_list:
        try:
            y = _load_clip_signal(
                project_dir,
                c["source_path"],
                float(c.get("source_offset") or 0.0),
                float(c["end_time"]) - float(c["start_time"]),
                sr=sr, max_sync_seconds=max_sync_seconds,
            )
            envs[c["id"]] = _onset_envelope(y, sr=sr, hop_length=hop_length)
        except FileNotFoundError as e:
            _log(f"skipping {c['id']}: {e}")
            envs[c["id"]] = np.zeros(1, dtype=np.float32)

    offsets: dict[str, float] = {anchor_id: 0.0}
    confidence: dict[str, float] = {anchor_id: 1.0}
    anchor_env = envs[anchor_id]

    for c in clips_list:
        cid = c["id"]
        if cid == anchor_id:
            continue
        lag, conf = _cross_correlate_offset(
            anchor_env, envs[cid],
            sr=sr, hop_length=hop_length, max_lag_seconds=max_lag_seconds,
        )
        offsets[cid] = lag
        confidence[cid] = conf

    _log(
        f"detect_offsets: anchor={anchor_id} n={len(clips_list)} "
        f"offsets={ {k: round(v, 3) for k, v in offsets.items()} } "
        f"confidence={ {k: round(v, 2) for k, v in confidence.items()} }"
    )
    return offsets, confidence
