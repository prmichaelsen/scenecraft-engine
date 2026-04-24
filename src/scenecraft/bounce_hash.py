"""Composite cache hash for ``bounce_audio`` (M16).

The ``composite_hash`` is the primary cache key for ``audio_bounces`` rows:
SHA-256 over the project's **mix-graph hash** (every factor that affects
mix output — tracks, clips, effects, curves, sends) plus the bounce-specific
selection (mode + track_ids/clip_ids + time window) and output format
(sample_rate, bit_depth, channels).

This layering is deliberate:

- The mix-graph hash guarantees two bounces against the same underlying
  mix state always produce the same hash. Any edit that would alter the
  rendered master bus (volume automation, an added effect, a reorder) also
  changes the mix-graph hash and therefore this composite.
- The selection terms ensure a full-mix bounce, a tracks-only bounce over
  the same tracks, and a clips-only bounce over the same clips each map
  to distinct cache entries — they have different rendered output even
  though the underlying project state is identical.
- The format terms mean a 16-bit 44.1 kHz stereo bounce is cached
  independently of a 24-bit 48 kHz stereo bounce of the same selection.

The function is deterministic: identical inputs → identical hex digest,
for the life of the project (subject to ``compute_mix_graph_hash`` also
being stable, which it is — see ``scenecraft/mix_graph_hash.py``).
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path


def compute_bounce_hash(
    project_dir: Path,
    *,
    start_time_s: float,
    end_time_s: float,
    mode: str,
    track_ids: list[str] | None,
    clip_ids: list[str] | None,
    sample_rate: int,
    bit_depth: int,
    channels: int,
) -> str:
    """SHA-256 hex over the mix graph + selection + format.

    Deterministic: identical inputs yield identical output. Sorts
    ``track_ids`` / ``clip_ids`` before hashing so callers that pass the
    same set in a different order still collide onto the same cache row.
    """
    # Local import avoids a module-load cycle through db.py at import time —
    # this module sits underneath chat.py, which sits over mix_graph_hash.
    from scenecraft.mix_graph_hash import compute_mix_graph_hash

    base = compute_mix_graph_hash(project_dir)
    selection = {
        "mode": mode,
        "track_ids": sorted(track_ids) if track_ids else None,
        "clip_ids": sorted(clip_ids) if clip_ids else None,
        "start": float(start_time_s),
        "end": float(end_time_s),
        "sr": int(sample_rate),
        "bd": int(bit_depth),
        "ch": int(channels),
    }
    payload = base + json.dumps(selection, sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


__all__ = ["compute_bounce_hash"]
