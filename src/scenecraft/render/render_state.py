"""Render-state snapshot derived from the fragment cache + background queue.

The UI shows a render-state bar above the timeline playhead. This module
produces the data that bar renders from: one entry per time-bucket with
state ∈ {unrendered, rendering, cached, stale}.

Design:
    * No new state of record — derive purely from
      ``global_fragment_cache`` and the project's BackgroundRenderer.
    * ``cached`` = entry present in cache for this (project, gen)
    * ``rendering`` = bucket is on the background queue (pending)
    * ``unrendered`` = neither cached nor queued
    * ``stale`` = was cached, then invalidated — currently we can't
      distinguish stale from unrendered without a separate tombstone log;
      for now we return ``unrendered`` (task-42 followup: track
      invalidation history so the UI can show dark-red-striped stale).

Snapshot + delta subscription:
    * HTTP GET /api/projects/:name/render-state → full snapshot (JSON
      list of bucket entries)
    * Subscribers receive (project, [changed buckets]) callbacks to
      stream deltas; wired into task-43's frontend live updates once
      task-37 (unified WS) lands. For now, the subscribe mechanism is
      exposed but callbacks need an external dispatcher.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Literal


BucketState = Literal["unrendered", "rendering", "cached", "stale"]


@dataclass
class BucketEntry:
    t_start: float
    t_end: float
    state: BucketState
    updated_at: float = 0.0  # reserved — filled by dispatcher when wired


# ── Coalescing delta dispatcher ───────────────────────────────────────────


class _DeltaDispatcher:
    """Small helper that coalesces rapid state transitions over a window.

    Background renderer may flip a bucket unrendered → rendering →
    cached within ~1s. If we pushed each transition, clients would see
    three updates for one piece of work. Instead, pending deltas sit in
    a dict and get flushed in a bounded-latency window (default 100ms).
    """

    def __init__(self, coalesce_window_s: float = 0.1) -> None:
        self._window = coalesce_window_s
        self._pending: dict[tuple[str, int], BucketEntry] = {}
        self._subscribers: list[Callable[[list[BucketEntry]], None]] = []
        self._lock = threading.Lock()
        self._flush_timer: threading.Timer | None = None

    def subscribe(
        self, callback: Callable[[list[BucketEntry]], None],
    ) -> Callable[[], None]:
        with self._lock:
            self._subscribers.append(callback)

        def unsubscribe() -> None:
            with self._lock:
                try:
                    self._subscribers.remove(callback)
                except ValueError:
                    pass

        return unsubscribe

    def record(self, project_dir: str, entry: BucketEntry) -> None:
        key = (project_dir, int(round(entry.t_start * 1000)))
        with self._lock:
            # Later entries overwrite earlier ones in the same key —
            # if a bucket went rendering→cached within the window, we
            # only emit the final state.
            self._pending[key] = entry
            if self._flush_timer is None:
                self._flush_timer = threading.Timer(self._window, self._flush)
                self._flush_timer.daemon = True
                self._flush_timer.start()

    def _flush(self) -> None:
        with self._lock:
            self._flush_timer = None
            if not self._pending:
                return
            batch = list(self._pending.values())
            self._pending.clear()
            subs = list(self._subscribers)
        for cb in subs:
            try:
                cb(batch)
            except Exception:
                pass


# Module-level dispatcher — one per process, keyed by project inside the
# callbacks (the subscriber filters project_dir as needed).
_dispatcher = _DeltaDispatcher()


def subscribe(
    callback: Callable[[list[BucketEntry]], None],
) -> Callable[[], None]:
    """Register a delta subscriber. Returns an unsubscribe function."""
    return _dispatcher.subscribe(callback)


def notify_bucket_change(project_dir: Path, entry: BucketEntry) -> None:
    """Record a state transition. Coalesced and flushed on a timer."""
    _dispatcher.record(str(project_dir), entry)


# ── Snapshot builder ──────────────────────────────────────────────────────


def build_snapshot(
    project_dir: Path,
    duration_seconds: float,
    fragment_seconds: float,
    encoder_generation: int,
    background_queue_t0s: set[float] | None = None,
) -> list[BucketEntry]:
    """Compute full bucket list for a project.

    Walks [0, duration) at fragment_seconds intervals, marks each bucket
    ``cached`` if the fragment cache has an entry for it under the given
    generation, ``rendering`` if the background queue has it, else
    ``unrendered``.

    ``background_queue_t0s`` is an optional set of t0 values currently in
    the queue — pass it from RenderCoordinator. Without it, everything
    that's not cached shows as ``unrendered``.
    """
    from scenecraft.render.fragment_cache import global_fragment_cache

    queued: set[float] = background_queue_t0s or set()
    out: list[BucketEntry] = []
    t = 0.0
    # Allow a half-bucket slop so the last bucket lands when duration
    # isn't an exact multiple of fragment_seconds.
    eps = fragment_seconds / 2
    while t < duration_seconds + eps:
        t_end = min(t + fragment_seconds, duration_seconds)
        if t >= duration_seconds:
            break
        if global_fragment_cache.get(project_dir, t, encoder_generation) is not None:
            state: BucketState = "cached"
        elif t in queued:
            state = "rendering"
        else:
            state = "unrendered"
        out.append(BucketEntry(t_start=t, t_end=t_end, state=state))
        t += fragment_seconds
    return out


def snapshot_for_worker(project_dir: Path) -> dict:
    """Full snapshot ready to return from an HTTP endpoint.

    Returns a JSON-serializable dict:
    ``{bucket_seconds, duration_seconds, buckets: [{t_start, t_end, state}]}``
    """
    # Late import — preview_worker pulls a lot and we don't want to
    # import it at module load time.
    from scenecraft.render.preview_worker import RenderCoordinator, FRAGMENT_SECONDS

    coord = RenderCoordinator.instance()
    worker = coord._workers.get(str(Path(project_dir).resolve()))  # type: ignore[attr-defined]
    if worker is None:
        return {
            "bucket_seconds": FRAGMENT_SECONDS,
            "duration_seconds": 0.0,
            "buckets": [],
        }

    # Pull background queue's t0 set for "rendering" state detection.
    queue_t0s: set[float] = set()
    try:
        bg = worker._background_renderer  # type: ignore[attr-defined]
        with bg._lock:  # type: ignore[attr-defined]
            queue_t0s = {b.t0 for b in bg._queue}  # type: ignore[attr-defined]
    except Exception:
        pass

    buckets = build_snapshot(
        project_dir=worker.project_dir,
        duration_seconds=worker._schedule.duration_seconds,
        fragment_seconds=FRAGMENT_SECONDS,
        encoder_generation=worker._encoder_generation,
        background_queue_t0s=queue_t0s,
    )
    return {
        "bucket_seconds": FRAGMENT_SECONDS,
        "duration_seconds": worker._schedule.duration_seconds,
        "buckets": [
            {"t_start": b.t_start, "t_end": b.t_end, "state": b.state}
            for b in buckets
        ],
    }
