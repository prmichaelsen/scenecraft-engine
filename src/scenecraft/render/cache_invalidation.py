"""Cache invalidation chokepoint for mutating API endpoints.

A mutating endpoint calls `invalidate_frames_for_mutation(project_dir,
ranges)` after its DB write commits. The helper does three things:

1. Drops matching entries from the L1 preview frame cache
   (`scenecraft.render.frame_cache.global_cache`) — so the next scrub
   request re-renders rather than serving stale pixels.
2. Drops matching entries from the fMP4 fragment cache
   (`scenecraft.render.fragment_cache.global_fragment_cache`) — so the
   next playback of the affected range re-renders rather than serving
   stale encoded bytes.
3. Tells the `RenderCoordinator` the project changed so any active
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
) -> tuple[int, int]:
    """Invalidate preview caches + notify playback worker.

    Returns (scrub_frames_dropped, fragments_dropped). Safe to call even
    if nothing is cached (returns (0, 0)). Never raises — designed to be
    called at the tail of an endpoint handler without risking a 500 on
    the write path.
    """
    frames_dropped = 0
    fragments_dropped = 0

    # Materialize ranges once — we need them twice (frame + fragment cache).
    range_list: list[tuple[float, float]] | None
    if ranges is None:
        range_list = None
    else:
        rl = [(a, b) for a, b in ranges if b >= a]
        range_list = rl if rl else None  # empty list → wholesale

    try:
        from scenecraft.render.frame_cache import global_cache
        if range_list is None:
            frames_dropped = global_cache.invalidate_project(project_dir)
        else:
            frames_dropped = global_cache.invalidate_ranges(project_dir, range_list)
    except Exception:
        pass

    try:
        from scenecraft.render.fragment_cache import global_fragment_cache
        if range_list is None:
            fragments_dropped = global_fragment_cache.invalidate_project(project_dir)
        else:
            fragments_dropped = global_fragment_cache.invalidate_ranges(
                project_dir, range_list,
            )
    except Exception:
        pass

    # Notify the playback worker (if any) so its schedule rebuilds.
    # Failing here is non-fatal: the worker will pick up the new DB
    # state eventually on the next explicit seek/restart.
    try:
        from scenecraft.render.preview_worker import RenderCoordinator
        coord = RenderCoordinator.instance()
        coord.invalidate_project(project_dir)
        # Nudge the background renderer to re-enqueue buckets inside the
        # invalidated ranges. When ``ranges`` is None (wholesale
        # invalidation) we skip the bg requeue — its queue is already
        # cheap to repopulate from play()/seek() and a full requeue
        # would be wasteful for a full-project invalidation (e.g.,
        # schedule rebuild triggers re-priming on the next play).
        if range_list is not None:
            coord.invalidate_ranges_in_background(project_dir, range_list)
    except Exception:
        pass

    return (frames_dropped, fragments_dropped)
