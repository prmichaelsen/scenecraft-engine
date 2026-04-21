"""ffmpeg-based audio-stream extraction for linked-audio inserts.

Given a video file, probe whether it carries an audio stream. If so, extract
to a file in the project's audio_staging directory. Extraction is idempotent
by content-hash so re-calling for the same (path, mtime) is a no-op.

Used by:
    api_server insert-linked-audio handler (M9 task 84)
    Veo auto-link hook (M9 task 92)
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path


def _log(msg: str):
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] [audio.extract] {msg}", file=sys.stderr, flush=True)


# Codecs we can stream-copy into a container without re-encoding. Anything else
# (or a missing codec_name) falls back to PCM WAV.
_STREAM_COPY_MAP = {
    "aac": ".m4a",
    "mp3": ".mp3",
    "flac": ".flac",
    "opus": ".opus",
    "vorbis": ".ogg",
}


def probe_audio_stream(video_path: Path) -> dict | None:
    """Return stream info for the first audio stream of `video_path`, or None.

    Result keys:
        codec_name: str   (e.g. 'aac', 'mp3', 'opus')
        channels: int     (defaults 2 if missing)
        sample_rate: int  (defaults 48000 if missing)
        duration: float   (seconds; 0 if unknown)
    """
    try:
        result = subprocess.run(
            [
                "ffprobe", "-v", "error",
                "-select_streams", "a:0",
                "-show_entries", "stream=codec_name,channels,sample_rate,duration",
                "-of", "json",
                str(video_path),
            ],
            capture_output=True, text=True, timeout=15, check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        _log(f"ffprobe failed for {video_path}: {e}")
        return None
    if result.returncode != 0:
        _log(f"ffprobe rc={result.returncode} for {video_path}: {result.stderr.strip()[:200]}")
        return None
    try:
        data = json.loads(result.stdout or "{}")
    except json.JSONDecodeError:
        return None
    streams = data.get("streams") or []
    if not streams:
        return None
    s = streams[0]
    try:
        duration = float(s.get("duration") or 0)
    except (TypeError, ValueError):
        duration = 0.0
    try:
        sr = int(s.get("sample_rate") or 48000)
    except (TypeError, ValueError):
        sr = 48000
    try:
        ch = int(s.get("channels") or 2)
    except (TypeError, ValueError):
        ch = 2
    return {
        "codec_name": s.get("codec_name") or "",
        "channels": ch,
        "sample_rate": sr,
        "duration": duration,
    }


def _staging_dir(project_dir: Path) -> Path:
    d = project_dir / "audio_staging"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _content_hash(video_path: Path) -> str:
    """Hash identifying this file version. Uses path + mtime + size for speed."""
    stat = video_path.stat()
    key = f"{video_path.resolve()}|{stat.st_mtime_ns}|{stat.st_size}".encode()
    return hashlib.sha1(key).hexdigest()[:12]


def extract_audio(video_path: Path, project_dir: Path) -> Path | None:
    """Extract the first audio stream of `video_path` to `audio_staging/`.

    Returns the extracted audio path (relative filename under audio_staging),
    or None if the video has no audio stream. Idempotent: re-calling for the
    same (path, mtime, size) returns the existing file without re-running ffmpeg.

    Raises RuntimeError only if extraction fails unexpectedly with an audio
    stream confirmed present.
    """
    info = probe_audio_stream(video_path)
    if info is None:
        return None

    staging = _staging_dir(project_dir)
    codec = info["codec_name"]
    ext = _STREAM_COPY_MAP.get(codec, ".wav")
    dest = staging / f"{_content_hash(video_path)}{ext}"
    if dest.exists() and dest.stat().st_size > 0:
        return dest

    if ext != ".wav":
        # Stream-copy path: no re-encode, very fast
        cmd = [
            "ffmpeg", "-y", "-loglevel", "error",
            "-i", str(video_path),
            "-vn",            # no video
            "-acodec", "copy",
            str(dest),
        ]
    else:
        # Fallback: decode to PCM WAV 48kHz (mix to stereo if source is mono)
        cmd = [
            "ffmpeg", "-y", "-loglevel", "error",
            "-i", str(video_path),
            "-vn",
            "-acodec", "pcm_s16le",
            "-ar", "48000",
            "-ac", str(info["channels"] or 2),
            str(dest),
        ]

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=120, check=False)
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        raise RuntimeError(f"ffmpeg extraction failed for {video_path}: {e}") from e
    if result.returncode != 0:
        # Clean up partial dest if any
        try:
            if dest.exists():
                dest.unlink()
        except OSError:
            pass
        raise RuntimeError(f"ffmpeg extraction failed (rc={result.returncode}): {result.stderr.strip()[:300]}")

    if not dest.exists() or dest.stat().st_size == 0:
        raise RuntimeError(f"ffmpeg produced empty output for {video_path}")

    _log(f"extracted {video_path.name} ({codec or 'wav'}) -> {dest.relative_to(project_dir)}")
    return dest
