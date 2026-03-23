"""Audio analysis module — beat tracking, onset detection, and feature extraction."""

from __future__ import annotations

from pathlib import Path

import librosa
import numpy as np

SUPPORTED_EXTENSIONS = {".wav", ".mp3", ".flac", ".ogg", ".m4a"}


def load_audio(path: str, sr: int = 22050) -> tuple[np.ndarray, int]:
    """Load an audio file and return (samples, sample_rate).

    Args:
        path: Path to audio file (WAV, MP3, FLAC, OGG, M4A).
        sr: Target sample rate. Defaults to 22050.

    Returns:
        Tuple of (audio time series as numpy array, sample rate).

    Raises:
        FileNotFoundError: If the file does not exist.
        ValueError: If the file format is not supported.
    """
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"Audio file not found: {path}")
    if p.suffix.lower() not in SUPPORTED_EXTENSIONS:
        raise ValueError(
            f"Unsupported format '{p.suffix}'. "
            f"Supported: {', '.join(sorted(SUPPORTED_EXTENSIONS))}"
        )
    y, sr_out = librosa.load(str(p), sr=sr, mono=True)
    return y, sr_out


def analyze_audio(path: str, sr: int = 22050) -> dict:
    """Analyze an audio file and return beat data.

    Returns a dict with:
        tempo: float — estimated BPM
        duration: float — audio duration in seconds
        sample_rate: int
        beats: list of {time: float, intensity: float}
        onsets: list of {time: float, strength: float}
    """
    y, sr_out = load_audio(path, sr=sr)
    duration = librosa.get_duration(y=y, sr=sr_out)

    # Beat tracking
    tempo, beat_frames = librosa.beat.beat_track(y=y, sr=sr_out)
    beat_times = librosa.frames_to_time(beat_frames, sr=sr_out)

    # Onset envelope for beat intensity
    onset_env = librosa.onset.onset_strength(y=y, sr=sr_out)

    # Sample onset envelope at beat positions and normalize
    beat_strengths = onset_env[beat_frames] if len(beat_frames) > 0 else np.array([])
    if len(beat_strengths) > 0 and beat_strengths.max() > 0:
        beat_intensities = (beat_strengths - beat_strengths.min()) / (
            beat_strengths.max() - beat_strengths.min()
        )
    else:
        beat_intensities = beat_strengths

    beats = [
        {"time": float(t), "intensity": float(i)}
        for t, i in zip(beat_times, beat_intensities)
    ]

    # Onset detection
    onset_frames = librosa.onset.onset_detect(y=y, sr=sr_out)
    onset_times = librosa.frames_to_time(onset_frames, sr=sr_out)
    onset_strengths = onset_env[onset_frames] if len(onset_frames) > 0 else np.array([])
    if len(onset_strengths) > 0 and onset_strengths.max() > 0:
        onset_strengths_norm = onset_strengths / onset_strengths.max()
    else:
        onset_strengths_norm = onset_strengths

    onsets = [
        {"time": float(t), "strength": float(s)}
        for t, s in zip(onset_times, onset_strengths_norm)
    ]

    # tempo may be an ndarray with one element in newer librosa
    tempo_val = float(tempo) if np.ndim(tempo) == 0 else float(tempo[0])

    return {
        "tempo": tempo_val,
        "duration": float(duration),
        "sample_rate": sr_out,
        "beats": beats,
        "onsets": onsets,
    }
