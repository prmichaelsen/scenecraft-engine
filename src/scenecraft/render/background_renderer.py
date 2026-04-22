"""Proactive preview-fragment renderer.

Keeps the preview fragment cache populated around the playhead while the
player is idle or paused. Priority queue: buckets nearest the playhead
first, expanding outward.

Cooperates with real-time playback: playback renders at its own rate
(via the main RenderWorker) and whatever it produces gets written into
the same ``global_fragment_cache``. Background renders populate the
same cache. Both paths race through the cache; whichever gets there
first wins. The "conflict" is cheap because worst case is rendering the
same bucket twice.

Shutdown: ``stop()`` sets the halt flag and waits for the thread. Called
as part of RenderWorker.stop().

This is intentionally single-threaded — cv2 decoders already saturate
cores via the main RenderWorker's thread pool. Adding more background
parallelism would just thrash caches. If the main worker is idle, this
thread uses it by calling render_frame_at directly. If the main worker
is active, this thread yields.
"""

from __future__ import annotations

import heapq
import logging
import math
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import cv2
import numpy as np

from scenecraft.render.compositor import render_frame_at
from scenecraft.render.fragment_cache import global_fragment_cache
from scenecraft.render.preview_stream import FragmentEncoder
from scenecraft.render.schedule import Schedule


logger = logging.getLogger(__name__)


def _log(msg: str) -> None:
    from datetime import datetime
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] [bg-render] {msg}", file=sys.stderr, flush=True)


# ── Tuning knobs ─────────────────────────────────────────────────────────

BACKGROUND_IDLE_SLEEP_S = 0.5
"""When the queue is empty, sleep this long before re-checking."""

BACKGROUND_BLOCKED_YIELD_S = 0.1
"""When main render is in progress, yield this long before retrying."""


# ── Work units ────────────────────────────────────────────────────────────


@dataclass(order=True)
class _Bucket:
    priority: float
    # Avoid comparing t0 on priority ties — fall back to insertion order
    # via a monotonic sequence counter.
    seq: int = field(compare=True)
    t0: float = field(compare=False)
    fragment_seconds: float = field(compare=False)


# ── BackgroundRenderer ────────────────────────────────────────────────────


class BackgroundRenderer:
    """One per project — renders uncached buckets toward the playhead."""

    def __init__(
        self,
        project_dir: Path,
        schedule: Schedule,
        encoder: FragmentEncoder,
        encoder_generation_cb: Callable[[], int],
        main_busy_cb: Callable[[], bool],
        fragment_seconds: float,
        fps: float,
    ) -> None:
        self.project_dir = Path(project_dir)
        self._schedule = schedule
        self._encoder = encoder
        self._encoder_generation_cb = encoder_generation_cb
        """Returns the main RenderWorker's current encoder_generation — we
        write into the cache under this key so fragments we produce are
        usable by the live MediaSource."""
        self._main_busy_cb = main_busy_cb
        """Returns True if the main RenderWorker is currently rendering a
        fragment. We yield while that's happening to avoid thrashing the
        shared encoder (the ffmpeg subprocess only accepts stdin from one
        producer at a time, so we share)."""
        self._fragment_seconds = fragment_seconds
        self._fps = fps
        self._frames_per_fragment = max(1, int(round(fragment_seconds * fps)))

        self._lock = threading.Lock()
        self._queue: list[_Bucket] = []
        self._seq = 0
        self._playhead_t: float = 0.0
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None

        # Encoder lock — shared with main worker to prevent simultaneous
        # feeds into the ffmpeg stdin (interleaves frames). Passed via
        # update_encoder_lock from outside.
        self._encoder_lock: threading.Lock | None = None

    # ── Public API ────────────────────────────────────────────────────────

    def set_encoder_lock(self, lock: threading.Lock) -> None:
        self._encoder_lock = lock

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="bg-renderer", daemon=True,
        )
        self._thread.start()

    def stop(self, timeout: float = 2.0) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=timeout)

    def update_playhead(self, t: float) -> None:
        """Reprioritize queue around ``t``. Safe to call often."""
        with self._lock:
            self._playhead_t = float(t)
            # Requeue everything with priorities relative to new playhead.
            # Cheap — at most duration_seconds / fragment_seconds entries.
            new_q: list[_Bucket] = []
            for b in self._queue:
                new_q.append(_Bucket(
                    priority=abs(b.t0 - self._playhead_t),
                    seq=b.seq,
                    t0=b.t0,
                    fragment_seconds=b.fragment_seconds,
                ))
            heapq.heapify(new_q)
            self._queue = new_q
        self._wake.set()

    def request_range(
        self,
        t_start: float,
        t_end: float,
        priority_bias: float = 0.0,
    ) -> None:
        """Enqueue every uncached bucket in [t_start, t_end].

        ``priority_bias`` adds to the distance-from-playhead score; pass
        a negative value to force a range to the front (e.g., user just
        loaded a specific section they want rendered first).

        Clamps ``t_end`` to the schedule duration so callers can pass
        "everything from here on" without flooding the queue with
        past-the-end buckets.
        """
        fs = self._fragment_seconds
        t_end = min(t_end, self._schedule.duration_seconds)
        t = math.floor(max(0.0, t_start) / fs) * fs
        with self._lock:
            while t < t_end and not self._stop.is_set():
                # Only enqueue if cache miss — cheap check, avoids
                # re-rendering already-cached buckets.
                if global_fragment_cache.get(
                    self.project_dir, t, self._encoder_generation_cb(),
                ) is None:
                    self._seq += 1
                    prio = abs(t - self._playhead_t) + priority_bias
                    heapq.heappush(
                        self._queue,
                        _Bucket(priority=prio, seq=self._seq, t0=t, fragment_seconds=fs),
                    )
                t += fs
        self._wake.set()

    def prime_around_playhead(self, radius_s: float = 20.0) -> None:
        """Enqueue ``[playhead - radius, playhead + radius]`` — call after
        `update_playhead` or on worker spawn to prefill around the user's
        current position."""
        with self._lock:
            playhead = self._playhead_t
        self.request_range(
            max(0.0, playhead - radius_s),
            min(self._schedule.duration_seconds, playhead + radius_s),
        )

    @property
    def queue_size(self) -> int:
        with self._lock:
            return len(self._queue)

    # ── Internals ─────────────────────────────────────────────────────────

    def _pop(self) -> _Bucket | None:
        with self._lock:
            while self._queue:
                b = heapq.heappop(self._queue)
                # Skip stale entries — already-cached buckets ( possibly
                # filled by the main worker while we were waiting).
                if global_fragment_cache.get(
                    self.project_dir, b.t0, self._encoder_generation_cb(),
                ) is not None:
                    continue
                return b
        return None

    def _render_bucket(self, bucket: _Bucket) -> None:
        """Render one bucket, encode, push into fragment cache."""
        schedule = self._schedule
        t0 = bucket.t0
        fps = self._fps
        dur = schedule.duration_seconds
        frames_to_render = self._frames_per_fragment

        # Clamp against end of timeline.
        effective = frames_to_render
        for i in range(frames_to_render):
            if t0 + i / fps >= dur:
                effective = i
                break
        if effective == 0:
            return

        # Fresh stream_caps per bucket — this thread doesn't share a
        # persistent pool with the main worker; opens are expensive but
        # we're idle-time work, not realtime.
        local_cache: dict = {"stream_caps": {}}
        frames: list[np.ndarray] = []
        try:
            for i in range(effective):
                if self._stop.is_set():
                    return
                t = t0 + i / fps
                try:
                    f = render_frame_at(
                        schedule, t,
                        frame_cache=local_cache,
                        prefer_proxy=True,
                    )
                except Exception as exc:
                    _log(f"render_frame_at({t:.3f}) failed: {exc}")
                    f = np.zeros(
                        (schedule.height, schedule.width, 3), dtype=np.uint8,
                    )
                if f.shape[0] != self._encoder.height or f.shape[1] != self._encoder.width:
                    f = cv2.resize(
                        f, (self._encoder.width, self._encoder.height),
                        interpolation=cv2.INTER_LINEAR,
                    )
                frames.append(f)
        finally:
            for entry in local_cache.get("stream_caps", {}).values():
                try:
                    entry["cap"].release()
                except Exception:
                    pass

        if not frames or self._stop.is_set():
            return

        # Share the encoder with the main worker via the external lock.
        # Worth noting: this means background rendering of any bucket
        # blocks if playback is mid-encode. Acceptable — playback is
        # priority.
        lock = self._encoder_lock
        if lock is None:
            _log("encoder_lock missing; skipping bucket")
            return
        acquired = lock.acquire(timeout=5.0)
        if not acquired:
            # Busy for 5s — abort this bucket, try again later
            return
        try:
            if self._stop.is_set():
                return
            segment = self._encoder.encode_range(frames)
        except Exception as exc:
            _log(f"encode_range failed: {exc}")
            return
        finally:
            lock.release()

        if not segment:
            return

        duration_ms = int(round(len(frames) / fps * 1000))
        global_fragment_cache.put(
            self.project_dir, t0, self._encoder_generation_cb(),
            segment, duration_ms,
        )

    def _run(self) -> None:
        _log(f"started for {self.project_dir.name}")
        try:
            while not self._stop.is_set():
                # If the main worker is busy, back off briefly.
                if self._main_busy_cb():
                    time.sleep(BACKGROUND_BLOCKED_YIELD_S)
                    continue

                bucket = self._pop()
                if bucket is None:
                    # Nothing queued; sleep until kicked or stop.
                    self._wake.clear()
                    self._wake.wait(timeout=BACKGROUND_IDLE_SLEEP_S)
                    continue

                _t0 = time.monotonic()
                self._render_bucket(bucket)
                elapsed = time.monotonic() - _t0
                if elapsed > 0.1:
                    _log(
                        f"rendered bucket t0={bucket.t0:.3f} in {elapsed:.2f}s "
                        f"(queue now {self.queue_size})"
                    )
        except Exception as exc:
            logger.exception("background_renderer crashed: %s", exc)
        finally:
            _log(f"stopped for {self.project_dir.name}")
