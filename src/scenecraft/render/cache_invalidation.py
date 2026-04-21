"""Cache invalidation chokepoint for mutating API endpoints.

A mutating endpoint calls `invalidate_frames_for_mutation(project_dir,
ranges)` after its DB write commits. The helper does two things:

1. Drops matching entries from the L1 preview frame cache
   (`scenecraft.render.frame_cache.global_cache`) — so the next scrub
   request re-renders rather than serving stale pixels.
2. Tells the `RenderCoordinator` the project changed so any active
   playback worker rebuilds its schedule on the next fragment cycle.

Ranges are a list of `[t_start, t_end]` seconds tuples describing the
time span the edit affected. Pass `None` or `[]` for wholesale
invalidation (conservative fallback for operations whose affected
range is hard to compute — e.g., undo/redo).
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterable


def invalidate_frames_for_mutation(
    project_dir: Path,
    ranges: Iterable[tuple[float, float]] | None = None,
) -> int:
    """Invalidate preview frame cache + notify playback worker.

    Returns the number of cache entries dropped. Safe to call even if
    nothing is cached (returns 0). Never raises — designed to be called
    at the tail of an endpoint handler without risking a 500 on the
    write path.
    """
    dropped = 0
    try:
        from scenecraft.render.frame_cache import global_cache
        if ranges is None:
            dropped = global_cache.invalidate_project(project_dir)
        else:
            range_list = [(a, b) for a, b in ranges if b >= a]
            if not range_list:
                # Empty ranges — treat as wholesale. Callers should use
                # `None` to mean wholesale; explicit empty list likely
                # indicates "no ranges computed" which is a bug, but
                # be defensive.
                dropped = global_cache.invalidate_project(project_dir)
            else:
                dropped = global_cache.invalidate_ranges(project_dir, range_list)
    except Exception:
        pass

    # Notify the playback worker (if any) so its schedule rebuilds.
    # Failing here is non-fatal: the worker will pick up the new DB
    # state eventually on the next explicit seek/restart.
    try:
        from scenecraft.render.preview_worker import RenderCoordinator
        RenderCoordinator.instance().invalidate_project(project_dir)
    except Exception:
        pass

    return dropped
