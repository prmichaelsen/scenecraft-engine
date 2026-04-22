"""Narrow host API surface for scenecraft plugins.

Plugins MUST import from this module rather than scenecraft internals. When the
dynamic plugin loader lands, this surface becomes the stable public API.

This is intentionally a thin re-export + a couple of plugin-specific helpers. It
does NOT wrap existing APIs with new semantics; additions here are deliberate
surface expansions.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

# --- DB helpers ------------------------------------------------------------
# Plugins go through these named re-exports only; direct imports of
# scenecraft.db are considered off-surface.
from scenecraft.db import (
    get_audio_clips,
    add_pool_segment,
    get_pool_segment,
    add_audio_candidate,
    assign_audio_candidate,
    get_audio_clip_effective_path,
    undo_begin,
)

# --- Job infrastructure ---------------------------------------------------
from scenecraft.ws_server import job_manager


__all__ = [
    "get_audio_clips",
    "add_pool_segment",
    "get_pool_segment",
    "add_audio_candidate",
    "assign_audio_candidate",
    "get_audio_clip_effective_path",
    "undo_begin",
    "job_manager",
    "extract_audio_as_wav",
    "register_rest_endpoint",
]


def extract_audio_as_wav(
    source_path: Path,
    out_path: Path,
    sample_rate: int = 48000,
) -> Path:
    """Transcode any ffmpeg-readable audio/video to PCM WAV at ``sample_rate``.

    Used by plugins that need a standardized input format (e.g. a vocal-isolation
    model that expects 48kHz mono WAV).

    Raises ``subprocess.CalledProcessError`` if ffmpeg exits non-zero, or
    ``subprocess.TimeoutExpired`` if transcoding takes longer than 60s.
    """
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-i",
            str(source_path),
            "-ac",
            "1",
            "-ar",
            str(sample_rate),
            str(out_path),
        ],
        capture_output=True,
        check=True,
        timeout=60,
    )
    return out_path


def register_rest_endpoint(path_regex: str, handler) -> None:
    """Route a handler on the shared scenecraft REST server.

    For MVP this populates a dict that ``api_server.py`` consults during
    request dispatch via ``PluginHost.dispatch_rest``. The ``handler`` signature
    is ``handler(path: str, *args, **kwargs) -> Any``; ``api_server.py`` is
    responsible for calling it with whatever positional/keyword context the host
    provides at dispatch time.
    """
    from scenecraft.plugin_host import PluginHost

    PluginHost._rest_routes[path_regex] = handler
