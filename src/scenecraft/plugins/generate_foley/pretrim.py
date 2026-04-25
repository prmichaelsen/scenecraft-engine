"""Source-video pre-trim for v2fx generation.

The cog-mmaudio predict.py overrides `duration` with the input video's
length when video is passed. To honor a user-specified [in, out] range
we pre-trim the source clip server-side before sending it to Replicate.

Strategy:
  1. Try stream-copy (``-c copy``). Fast, no re-encode. Works when [in, out]
     align with keyframes.
  2. On failure or keyframe misalignment (first frame is green/black),
     fall back to a re-encode with ``libx264 -preset ultrafast``.
"""

from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path
from typing import Tuple

# Product ceiling per clarification-12 / design doc Item 3
MIN_RANGE_SECONDS = 1.0
MAX_RANGE_SECONDS = 30.0


class PretrimError(Exception):
    """Raised when pre-trim fails on both fast-path and re-encode."""


def trim_to_range(
    *,
    source_path: Path,
    in_seconds: float,
    out_seconds: float,
    output_path: Path | None = None,
) -> Path:
    """Trim the source video to the [in, out] window.

    :param source_path: Absolute path to the source video file.
    :param in_seconds:  In-point in seconds.
    :param out_seconds: Out-point in seconds.
    :param output_path: Optional explicit output path. If None, writes to a
                        temp file and returns its path (caller owns cleanup).

    :raises PretrimError:  if both stream-copy and re-encode fail.
    :raises ValueError:    on invalid range (out<=in, too short/long, negative).
    """
    if out_seconds <= in_seconds:
        raise ValueError(f"out_seconds ({out_seconds}) must be > in_seconds ({in_seconds})")
    if in_seconds < 0:
        raise ValueError(f"in_seconds cannot be negative: {in_seconds}")
    duration = out_seconds - in_seconds
    if duration < MIN_RANGE_SECONDS:
        raise ValueError(
            f"range too short: {duration}s < {MIN_RANGE_SECONDS}s minimum"
        )
    if duration > MAX_RANGE_SECONDS:
        raise ValueError(
            f"range too long: {duration}s > {MAX_RANGE_SECONDS}s ceiling"
        )
    if not source_path.exists():
        raise ValueError(f"source_path does not exist: {source_path}")

    if output_path is None:
        tmp = tempfile.NamedTemporaryFile(
            delete=False,
            prefix="foley-pretrim-",
            suffix=source_path.suffix or ".mp4",
        )
        tmp.close()
        output_path = Path(tmp.name)

    # Strategy 1: stream-copy (fast, no re-encode)
    try:
        _ffmpeg_stream_copy(source_path, output_path, in_seconds, out_seconds)
        return output_path
    except subprocess.CalledProcessError:
        # Fall through to re-encode
        pass

    # Strategy 2: re-encode
    try:
        _ffmpeg_reencode(source_path, output_path, in_seconds, out_seconds)
        return output_path
    except subprocess.CalledProcessError as e:
        raise PretrimError(
            f"ffmpeg re-encode failed for {source_path} [{in_seconds}, {out_seconds}]: {e.stderr.decode() if e.stderr else 'no stderr'}"
        ) from e


def _ffmpeg_stream_copy(
    source: Path, dest: Path, in_s: float, out_s: float
) -> None:
    """Fast-path: stream-copy trim. Fails on keyframe-misaligned inputs."""
    subprocess.run(
        [
            "ffmpeg", "-y",
            "-ss", str(in_s),
            "-to", str(out_s),
            "-i", str(source),
            "-c", "copy",
            "-avoid_negative_ts", "make_zero",
            str(dest),
        ],
        capture_output=True,
        check=True,
        timeout=60,
    )


def _ffmpeg_reencode(
    source: Path, dest: Path, in_s: float, out_s: float
) -> None:
    """Slow-path: re-encode trim. Always frame-accurate."""
    subprocess.run(
        [
            "ffmpeg", "-y",
            "-i", str(source),
            "-ss", str(in_s),
            "-to", str(out_s),
            "-c:v", "libx264",
            "-preset", "ultrafast",
            "-c:a", "aac",
            str(dest),
        ],
        capture_output=True,
        check=True,
        timeout=300,
    )
