"""STUB — to be replaced by sibling branch ``m15-mix-schema``.

Real implementation will compute a deterministic SHA-256 over the full mix
graph: every audio_track (volume_curve, effects + their param curves, mute/
solo state), every audio_clip on those tracks (pool_segment id, start/end,
source_offset, selected candidate, volume_curve, remap), ordered
deterministically. Any change to the audible mix should change the hash.

This stub returns a single constant so the ``analyze_master_bus`` tool can
compile and be unit-tested on this branch. Tests monkeypatch this function
with a stable-per-test-case value.
"""

# TODO(M15): replace with real impl from sibling branch m15-mix-schema.

from __future__ import annotations

from pathlib import Path


def compute_mix_graph_hash(project_dir: Path) -> str:  # noqa: ARG001 — stub
    """Return a deterministic hash of the project's mix graph.

    STUB: always returns 64 zeros. The real implementation will hash all the
    mix-relevant tables so a caller can tell when any knob has changed.
    """
    return "0" * 64
