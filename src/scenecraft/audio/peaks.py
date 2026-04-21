"""Server-side waveform peak computation.

For each audio clip, we decode the relevant slice of the source file via
ffmpeg (streaming stdin → stdout, no intermediate file), window it into
equal-time buckets, and emit a single absolute peak per bucket as float16
bytes. Result is cached on disk keyed by (source_path + source_offset +
duration + resolution) so repeat fetches are O(1).

Used by the /api/projects/:name/audio-clips/:id/peaks endpoint (task 88).
"""

from __future__ import annotations

import hashlib
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import numpy as np


_SAMPLE_RATE = 16000   # enough fidelity for peak display; cheap to decode
_DEFAULT_RESOLUTION = 400   # peaks per second


def _log(msg: str):
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] [audio.peaks] {msg}", file=sys.stderr, flush=True)


def _cache_key(source_path: Path, source_offset: float, duration: float, resolution: int) -> str:
    stat = source_path.stat()
    raw = f"{source_path.resolve()}|{stat.st_mtime_ns}|{stat.st_size}|{source_offset:.6f}|{duration:.6f}|{resolution}"
    return hashlib.sha1(raw.encode()).hexdigest()[:16]


def _cache_dir(project_dir: Path) -> Path:
    d = project_dir / "audio_staging" / ".peaks"
    d.mkdir(parents=True, exist_ok=True)
    return d


def compute_peaks(
    source_path: Path,
    source_offset: float,
    duration: float,
    resolution: int = _DEFAULT_RESOLUTION,
    project_dir: Path | None = None,
) -> bytes:
    """Return a float16 little-endian byte buffer of absolute peaks.

    Length: `ceil(duration * resolution)` peaks, each in [0, 1]. Mixed-down
    to mono if the source is multi-channel.

    Raises RuntimeError on decode failure.
    """
    if duration <= 0:
        return b""
    resolution = max(50, min(resolution, 2000))

    # Disk cache by content key
    if project_dir is not None:
        key = _cache_key(source_path, source_offset, duration, resolution)
        cache_file = _cache_dir(project_dir) / f"{key}.f16"
        if cache_file.exists():
            return cache_file.read_bytes()
    else:
        cache_file = None

    n_peaks = max(1, int(np.ceil(duration * resolution)))
    # Total samples decoded for this slice, at _SAMPLE_RATE
    total_samples = int(round(duration * _SAMPLE_RATE))
    if total_samples <= 0:
        return b""

    # ffmpeg: seek to source_offset, decode `duration` seconds of mono s16le at 16kHz.
    # Stream the output through a pipe and bucket it as we read, so memory stays
    # bounded by one bucket's worth of samples rather than the full decode —
    # important for multi-hour clips (a 2.5h file decodes to ~300 MB of s16 PCM
    # which crashed subprocess.run with capture_output=True).
    cmd = [
        "ffmpeg", "-nostdin", "-loglevel", "error",
        "-ss", f"{source_offset:.6f}",
        "-t", f"{duration:.6f}",
        "-i", str(source_path),
        "-vn",
        "-ac", "1",
        "-ar", str(_SAMPLE_RATE),
        "-f", "s16le",
        "-",
    ]
    # Bucket size in samples: how many consecutive samples collapse into one peak.
    # floor division matches the original reshape behaviour; we drop the trailing
    # partial bucket the same way.
    bucket_samples = max(1, total_samples // n_peaks)
    bucket_bytes = bucket_samples * 2   # 2 bytes per int16 sample
    peaks_out = np.zeros(n_peaks, dtype=np.float32)

    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except FileNotFoundError as e:
        raise RuntimeError(f"ffmpeg not found: {e}") from e

    try:
        wrote_any = False
        for i in range(n_peaks):
            # Read exactly one bucket — retry until we have `bucket_bytes` or EOF
            buf = b""
            while len(buf) < bucket_bytes:
                chunk = proc.stdout.read(bucket_bytes - len(buf))
                if not chunk:
                    break
                buf += chunk
            if not buf:
                break
            # Align to even byte count (should already be, but be defensive)
            if len(buf) % 2:
                buf = buf[:-1]
            if not buf:
                break
            arr = np.frombuffer(buf, dtype=np.int16)
            if arr.size:
                peaks_out[i] = float(np.abs(arr.astype(np.float32) / 32768.0).max())
                wrote_any = True
        # Drain any remaining stdout (we intentionally ignore it — trailing bucket)
        try:
            proc.stdout.read()
        except Exception:
            pass
        try:
            rc = proc.wait(timeout=60)
        except subprocess.TimeoutExpired:
            proc.kill()
            raise RuntimeError("ffmpeg timed out during peak decode")
        if rc != 0 and not wrote_any:
            err = proc.stderr.read().decode("utf-8", errors="replace")[:300]
            raise RuntimeError(f"ffmpeg rc={rc}: {err}")
    finally:
        try:
            proc.stdout.close()
        except Exception:
            pass
        try:
            proc.stderr.close()
        except Exception:
            pass

    data = peaks_out.astype(np.float16).tobytes()

    if cache_file is not None:
        try:
            cache_file.write_bytes(data)
        except OSError as e:
            _log(f"cache write failed: {e}")

    return data
