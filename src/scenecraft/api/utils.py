"""Shared utility helpers for FastAPI routers (M16 T65).

Extracted from ``api_server.py`` during the hard cutover so routers
no longer import anything from the deleted legacy module.
"""

from __future__ import annotations

import re as _re
import sys
from pathlib import Path


# ---------------------------------------------------------------------------
# Logging — stderr + WS broadcast (mirrors legacy ``api_server._log``)
# ---------------------------------------------------------------------------


def _log(msg: str, level: str = "info") -> None:
    """Timestamped log line to stderr + best-effort WS broadcast."""
    from datetime import datetime

    now = datetime.now()
    ts = now.strftime("%H:%M:%S")
    print(f"[{ts}] {msg}", file=sys.stderr, flush=True)
    try:
        from scenecraft.ws_server import job_manager

        job_manager._broadcast(
            {
                "type": "log",
                "message": msg,
                "timestamp": now.isoformat(),
                "level": level,
            }
        )
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Media classification (pool assets)
# ---------------------------------------------------------------------------

_AUDIO_EXTS = frozenset({".wav", ".mp3", ".aac", ".m4a", ".flac", ".ogg", ".opus", ".aif", ".aiff"})
_VIDEO_EXTS = frozenset({".mp4", ".mov", ".webm", ".mkv", ".avi", ".m4v"})
_IMAGE_EXTS = frozenset({".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".tiff", ".avif"})


def _classify_media_type(path: str) -> str:
    """Classify a pool asset as 'audio' | 'video' | 'image' | 'other' from extension."""
    if not path:
        return "other"
    ext = Path(path).suffix.lower()
    if ext in _AUDIO_EXTS:
        return "audio"
    if ext in _VIDEO_EXTS:
        return "video"
    if ext in _IMAGE_EXTS:
        return "image"
    return "other"


# ---------------------------------------------------------------------------
# Variant numbering (candidates directory)
# ---------------------------------------------------------------------------


def _next_variant(directory: Path, ext: str = ".png") -> int:
    """Find the next available variant number in a directory (max existing + 1)."""
    max_v = 0
    for f in directory.glob(f"v*{ext}"):
        m = _re.match(r"v(\d+)", f.stem)
        if m:
            max_v = max(max_v, int(m.group(1)))
    return max_v + 1


# ---------------------------------------------------------------------------
# Project settings helpers
# ---------------------------------------------------------------------------


def _get_project_settings(project_dir: Path) -> dict:
    """Read project settings from the meta table in project.db."""
    import sqlite3

    db_path = project_dir / "project.db"
    if not db_path.exists():
        return {}
    try:
        conn = sqlite3.connect(str(db_path))
        rows = conn.execute("SELECT key, value FROM meta").fetchall()
        conn.close()
        return {k: v for k, v in rows}
    except Exception:
        return {}


def _get_image_backend(project_dir: Path) -> str:
    """Get image generation backend from project settings."""
    return _get_project_settings(project_dir).get("image_backend", "vertex")


def _get_video_backend(project_dir: Path) -> str:
    """Get video generation backend from project settings."""
    return _get_project_settings(project_dir).get("video_backend", "vertex")


__all__ = [
    "_log",
    "_classify_media_type",
    "_next_variant",
    "_get_project_settings",
    "_get_image_backend",
    "_get_video_backend",
]
