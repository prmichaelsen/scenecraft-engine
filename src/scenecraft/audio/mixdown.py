"""Multi-track audio mixdown for the final render (M9 task-91).

Replaces the legacy single-file mux_audio path: for each non-hidden, non-muted
audio_track, loads every audio_clip, applies its source_offset + length, clip
volume curve (dB normalised over clip length), then the track volume curve
(dB over absolute seconds), then sums into the master. Same-track clip
overlaps get an equal-power crossfade (cos/sin pair, sum of squares = 1).

Usage:
    render_project_audio(project_dir, sr=48000, total_seconds=120.5, out_path=...)

The returned WAV (16-bit PCM, stereo, project_sr) is ready to mux with the
rendered video via ffmpeg's `-c:a aac`.
"""

from __future__ import annotations

import subprocess
import sys
import wave
from datetime import datetime
from pathlib import Path

import numpy as np

from scenecraft.audio.curves import evaluate_curve_linear


def _log(msg: str):
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] [audio.mixdown] {msg}", file=sys.stderr, flush=True)


def _decode_to_float32(source: Path, sr: int) -> np.ndarray:
    """Decode any audio file to a stereo float32 array at target sample rate.

    Returns shape (2, N). Mono inputs are broadcast to stereo.
    """
    proc = subprocess.run(
        [
            "ffmpeg",
            "-hide_banner", "-loglevel", "error",
            "-i", str(source),
            "-f", "f32le",
            "-ac", "2",
            "-ar", str(sr),
            "-",
        ],
        capture_output=True, check=True,
    )
    raw = np.frombuffer(proc.stdout, dtype=np.float32)
    if raw.size == 0:
        return np.zeros((2, 0), dtype=np.float32)
    # ffmpeg interleaves: L R L R ... → reshape to (N, 2) then transpose to (2, N)
    stereo = raw.reshape(-1, 2).T.astype(np.float32, copy=False)
    return stereo


def _equal_power_crossfade_inplace(buf: np.ndarray, new_samples: np.ndarray, start_idx: int) -> None:
    """Blend `new_samples` into `buf[:, start_idx : start_idx + n]` using equal-power.

    Overlap region = intersection of [start_idx, start_idx + len(new)] and the
    non-zero region already in buf. Outside the overlap, new_samples is added as-is.
    """
    n_new = new_samples.shape[1]
    if n_new == 0:
        return
    end_idx = start_idx + n_new
    buf_len = buf.shape[1]
    if start_idx >= buf_len:
        return
    end_idx = min(end_idx, buf_len)
    n_write = end_idx - start_idx
    if n_write <= 0:
        return

    existing = buf[:, start_idx:end_idx]
    incoming = new_samples[:, :n_write]

    # Detect overlap region — where existing is non-zero
    existing_mag = np.abs(existing).max(axis=0)
    overlap_mask = existing_mag > 1e-6

    if not overlap_mask.any():
        # Pure add (no prior content there)
        buf[:, start_idx:end_idx] = existing + incoming
        return

    # Find contiguous overlap range (first True to last True in mask)
    idxs = np.where(overlap_mask)[0]
    ov_start = idxs[0]
    ov_end = idxs[-1] + 1
    n_overlap = ov_end - ov_start

    out = existing + incoming  # default: addition outside overlap

    # Equal-power: existing (L) fades out via cos, incoming (R) fades in via sin.
    t = np.linspace(0.0, 1.0, n_overlap, dtype=np.float32)
    gain_out = np.cos(t * np.pi / 2).astype(np.float32)
    gain_in = np.sin(t * np.pi / 2).astype(np.float32)

    out[:, ov_start:ov_end] = (
        existing[:, ov_start:ov_end] * gain_out[np.newaxis, :]
        + incoming[:, ov_start:ov_end] * gain_in[np.newaxis, :]
    )
    buf[:, start_idx:end_idx] = out


def _render_track(
    project_dir: Path,
    track: dict,
    clips: list[dict],
    sr: int,
    total_samples: int,
) -> np.ndarray:
    """Render a single track to a stereo float32 buffer."""
    buf = np.zeros((2, total_samples), dtype=np.float32)

    # Sort clips by start_time so same-track overlap crossfades work deterministically
    for clip in sorted(clips, key=lambda c: c["start_time"]):
        if clip.get("muted"):
            continue
        source = project_dir / clip["source_path"]
        if not source.exists():
            _log(f"  skipping missing clip source: {source}")
            continue

        start_s = float(clip["start_time"])
        end_s = float(clip["end_time"])
        if end_s <= start_s:
            continue
        # Derived fields from get_audio_clips: linked clips carry the linear
        # remap factor and trim-adjusted source offset. Unlinked clips default
        # to rate=1 and effective_source_offset==source_offset.
        rate = float(clip.get("playback_rate") or 1.0)
        eff_offset_s = float(clip.get("effective_source_offset") or clip.get("source_offset") or 0.0)

        try:
            samples = _decode_to_float32(source, sr)
        except subprocess.CalledProcessError as e:
            _log(f"  decode failed for {source}: {e}")
            continue

        clip_len = int(round((end_s - start_s) * sr))
        src_start = int(round(eff_offset_s * sr))

        if abs(rate - 1.0) < 1e-4:
            # Fast path: no resample
            src_slice = samples[:, src_start : src_start + clip_len]
        else:
            # Linear remap: read `clip_len * rate` source samples, resample to `clip_len`
            src_needed = max(1, int(round(clip_len * rate)))
            src_block = samples[:, src_start : src_start + src_needed]
            if src_block.shape[1] == 0:
                src_slice = np.zeros((2, clip_len), dtype=np.float32)
            else:
                # np.interp on each channel → linear time-stretch/compress
                src_idx = np.linspace(0, src_block.shape[1] - 1, clip_len, dtype=np.float32)
                src_slice = np.stack([
                    np.interp(src_idx, np.arange(src_block.shape[1]), src_block[0]),
                    np.interp(src_idx, np.arange(src_block.shape[1]), src_block[1]),
                ]).astype(np.float32)
        if src_slice.shape[1] < clip_len:
            # Pad with silence if source shorter than requested length
            pad = np.zeros((2, clip_len - src_slice.shape[1]), dtype=np.float32)
            src_slice = np.concatenate([src_slice, pad], axis=1)

        # Clip volume curve (normalised x over [start_s, end_s])
        n = src_slice.shape[1]
        t_clip = np.linspace(start_s, end_s, n, endpoint=False, dtype=np.float32)
        clip_gain = evaluate_curve_linear(clip.get("volume_curve"), t_clip, x_normalised=True, clip_start=start_s, clip_end=end_s)
        src_slice = src_slice * clip_gain[np.newaxis, :]

        # Mix into track buf with equal-power on overlap
        start_idx = int(round(start_s * sr))
        _equal_power_crossfade_inplace(buf, src_slice, start_idx)

    # Apply track-wide volume curve (seconds x), then mute
    if track.get("muted"):
        buf[:] = 0.0
        return buf

    t_all = (np.arange(total_samples, dtype=np.float32) / sr)
    track_gain = evaluate_curve_linear(track.get("volume_curve"), t_all, x_normalised=False)
    buf = buf * track_gain[np.newaxis, :]

    return buf


def _write_wav(path: Path, samples: np.ndarray, sr: int) -> None:
    """Write a stereo float32 array (2, N) as a 16-bit PCM WAV."""
    path.parent.mkdir(parents=True, exist_ok=True)
    # Peak-limit at -0.1 dBFS to prevent clipping
    peak = float(np.max(np.abs(samples))) if samples.size else 0.0
    if peak > 0.0:
        limit = 10 ** (-0.1 / 20.0)  # ≈0.9886
        if peak > limit:
            samples = samples * (limit / peak)

    pcm = np.clip(samples * 32767.0, -32768.0, 32767.0).astype(np.int16)
    # Interleave L R L R ... for WAV
    interleaved = pcm.T.reshape(-1)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(interleaved.tobytes())


def render_project_audio(
    project_dir: Path,
    total_seconds: float,
    out_path: Path,
    sr: int = 48000,
) -> Path | None:
    """Render all audio tracks to a single stereo WAV.

    Returns the WAV path, or None if the project has no enabled non-hidden
    audio tracks with clips (caller falls back to legacy path).
    """
    from scenecraft import db as dbmod

    tracks = dbmod.get_audio_tracks(project_dir)
    if not tracks:
        return None

    total_samples = max(int(round(total_seconds * sr)), 1)
    master = np.zeros((2, total_samples), dtype=np.float32)
    contributed = 0

    for track in tracks:
        if track.get("hidden") or not track.get("enabled", True):
            continue
        clips = dbmod.get_audio_clips(project_dir, track["id"])
        if not clips:
            continue
        track_buf = _render_track(project_dir, track, clips, sr, total_samples)
        master += track_buf
        contributed += 1
        _log(f"  mixed track {track['id']} ({len(clips)} clips)")

    if contributed == 0:
        return None

    _write_wav(out_path, master, sr)
    _log(f"wrote {out_path} ({contributed} track(s), {total_seconds:.2f}s @ {sr}Hz)")
    return out_path
