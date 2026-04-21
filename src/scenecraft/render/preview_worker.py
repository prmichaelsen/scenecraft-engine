"""Per-session playback renderer for backend-rendered preview streaming.

A RenderWorker owns a Schedule, a FragmentEncoder, and a background thread.
The thread pre-renders fragments ahead of the playhead and queues encoded
bytes for delivery over the WebSocket. External callers drive it with
play/seek/pause/stop commands and consume fragments via fragments().

A RenderCoordinator caps concurrent workers at (cpu_count - 1) (minimum 1)
and evicts idle workers after 5 minutes so long-running servers don't
accumulate memory.

The compositor's source-video cache (the dict passed to render_frame_at
as frame_cache) is worker-local. Encoded frames are additionally written
into the scrub L1 cache (frame_cache.global_cache) opportunistically so
scrub and playback share warmed data.
"""

from __future__ import annotations

import logging
import math
import os
import queue
import sys
import threading
import time
from pathlib import Path
from typing import Iterator

import cv2
import numpy as np

from scenecraft.render.compositor import render_frame_at
from scenecraft.render.frame_cache import global_cache
from scenecraft.render.preview_stream import FragmentEncoder
from scenecraft.render.schedule import build_schedule


logger = logging.getLogger(__name__)


# ── Module-level tuning knobs ────────────────────────────────────────────

BUFFER_SECONDS = 10          # how far ahead of the playhead to pre-render
FRAGMENT_SECONDS = 1.0       # one fMP4 media segment per second
IDLE_TIMEOUT_S = 300         # tear down workers idle for this long
SCRUB_JPEG_QUALITY = 85      # JPEG quality used when opportunistically warming the scrub cache


def _log(msg: str) -> None:
    from datetime import datetime
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] [preview-worker] {msg}", file=sys.stderr, flush=True)


# ── RenderWorker ─────────────────────────────────────────────────────────


class RenderWorker:
    """One per active playback session. Owns its Schedule, encoder, and thread."""

    def __init__(
        self,
        project_dir: Path,
        fragment_encoder: FragmentEncoder | None = None,
        queue_capacity: int | None = None,
    ) -> None:
        self.project_dir = Path(project_dir)
        # Build schedule (may raise if project has no renderable content).
        self._schedule = build_schedule(self.project_dir)
        self._fps = self._schedule.fps or 24.0
        self._frames_per_fragment = max(1, int(round(FRAGMENT_SECONDS * self._fps)))
        if queue_capacity is None:
            queue_capacity = max(1, math.ceil(BUFFER_SECONDS / FRAGMENT_SECONDS))
        self._queue: queue.Queue[bytes] = queue.Queue(maxsize=queue_capacity)
        self._encoder = fragment_encoder or FragmentEncoder(
            width=self._schedule.width,
            height=self._schedule.height,
            fps=self._fps,
        )

        # Control flags.
        self._playing = threading.Event()          # set while render loop should produce
        self._stop_flag = threading.Event()        # set to terminate worker entirely
        self._invalidated = threading.Event()      # set when schedule needs rebuilding
        self._seek_lock = threading.Lock()
        self._playhead_t = 0.0                     # next time to render
        self._last_activity_ts = time.monotonic()

        # Init-segment handshake: consumers read it first.
        self._init_emitted = False

        # Worker-local frame cache (source-video handles, not JPEGs).
        self._frame_cache: dict = {}

        self._thread: threading.Thread | None = None

    # ── Public API ────────────────────────────────────────────────────

    def play(self, start_t: float = 0.0) -> None:
        """Begin pre-rendering from start_t. Non-blocking."""
        with self._seek_lock:
            self._playhead_t = max(0.0, float(start_t))
            self._drain_queue()
        self._last_activity_ts = time.monotonic()
        if self._thread is None or not self._thread.is_alive():
            self._stop_flag.clear()
            self._thread = threading.Thread(target=self._render_loop, daemon=True)
            self._thread.start()
        self._playing.set()

    def seek(self, t: float) -> None:
        """Flush pending fragments past the current playhead, resume rendering from t."""
        with self._seek_lock:
            self._playhead_t = max(0.0, float(t))
            self._drain_queue()
            # New encoder is required — tfdt continuity breaks across seeks; the
            # client is expected to stop/reopen the MediaSource or abort() the
            # SourceBuffer. We rebuild and emit a fresh init.
            try:
                self._encoder.close()
            except Exception:
                pass
            self._encoder = FragmentEncoder(
                width=self._schedule.width,
                height=self._schedule.height,
                fps=self._fps,
            )
            self._init_emitted = False
        self._last_activity_ts = time.monotonic()
        self._playing.set()

    def pause(self) -> None:
        """Stop rendering. Queued fragments remain available."""
        self._playing.clear()
        self._last_activity_ts = time.monotonic()

    def stop(self) -> None:
        """Halt and release all resources."""
        self._stop_flag.set()
        self._playing.set()  # unblock any wait
        try:
            self._drain_queue()
        except Exception:
            pass
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=3.0)
        try:
            self._encoder.close()
        except Exception:
            pass
        # Release any source-video handles.
        for seg in self._schedule.segments:
            for key in ("_cap", "_img", "_frames"):
                if key in seg:
                    try:
                        if key == "_cap" and hasattr(seg["_cap"], "release"):
                            seg["_cap"].release()
                    except Exception:
                        pass
                    seg.pop(key, None)

    def fragments(self) -> Iterator[bytes]:
        """Blocking iterator yielding init segment first, then media segments.

        Returns when the worker is stopped. Safe to call exactly once per
        consumer — a second consumer would starve the first.
        """
        # Emit init segment synchronously.
        init = self._encoder.encode_init()
        self._init_emitted = True
        yield init
        while not self._stop_flag.is_set():
            try:
                chunk = self._queue.get(timeout=0.25)
            except queue.Empty:
                continue
            if chunk is None:  # sentinel
                break
            yield chunk

    def on_project_invalidate(self) -> None:
        """Called when the project's data changes; flush queued fragments and restart rendering from the playhead."""
        self._invalidated.set()
        self._drain_queue()

    @property
    def last_activity_ts(self) -> float:
        return self._last_activity_ts

    @property
    def is_idle(self) -> bool:
        return not self._playing.is_set()

    @property
    def duration(self) -> float:
        return self._schedule.duration_seconds

    # ── Internals ─────────────────────────────────────────────────────

    def _drain_queue(self) -> None:
        try:
            while True:
                self._queue.get_nowait()
        except queue.Empty:
            pass

    def _render_loop(self) -> None:
        _log(f"render loop started for {self.project_dir.name}")
        try:
            while not self._stop_flag.is_set():
                if not self._playing.is_set():
                    self._playing.wait(timeout=0.5)
                    continue
                if self._invalidated.is_set():
                    self._invalidated.clear()
                    try:
                        new_sched = build_schedule(self.project_dir)
                        self._schedule = new_sched
                        self._frame_cache = {}
                        _log(f"schedule rebuilt for {self.project_dir.name}")
                    except Exception as exc:
                        _log(f"rebuild failed: {exc}")
                        self._playing.clear()
                        continue

                # End-of-timeline?
                if self._playhead_t >= self._schedule.duration_seconds - 1e-6:
                    # Nothing left to render; idle.
                    self._playing.clear()
                    continue

                # Render one fragment's worth of frames.
                frames_to_render = self._frames_per_fragment
                t0 = self._playhead_t
                frames: list[np.ndarray] = []
                for i in range(frames_to_render):
                    t = t0 + i / self._fps
                    if t >= self._schedule.duration_seconds:
                        break
                    if self._stop_flag.is_set():
                        return
                    try:
                        frame = render_frame_at(self._schedule, t, frame_cache=self._frame_cache)
                    except Exception as exc:
                        _log(f"render_frame_at({t:.3f}) failed: {exc}")
                        frame = np.zeros(
                            (self._schedule.height, self._schedule.width, 3),
                            dtype=np.uint8,
                        )
                    # Ensure encoder-expected shape (may require even dims).
                    if (
                        frame.shape[0] != self._encoder.height
                        or frame.shape[1] != self._encoder.width
                    ):
                        frame = cv2.resize(
                            frame,
                            (self._encoder.width, self._encoder.height),
                            interpolation=cv2.INTER_LINEAR,
                        )
                    frames.append(frame)

                    # Opportunistically warm the scrub cache.
                    try:
                        ok, buf = cv2.imencode(
                            ".jpg", frame,
                            [int(cv2.IMWRITE_JPEG_QUALITY), SCRUB_JPEG_QUALITY],
                        )
                        if ok:
                            global_cache.put(self.project_dir, t, SCRUB_JPEG_QUALITY, bytes(buf))
                    except Exception:
                        pass

                if not frames:
                    self._playing.clear()
                    continue

                # Ensure init has been emitted for the encoder; if a consumer
                # hasn't called fragments() yet we still can (encode_init sets
                # internal state but returning the bytes is a no-op for the queue).
                if not self._init_emitted:
                    try:
                        self._encoder.encode_init()
                    except Exception as exc:
                        _log(f"encode_init failed: {exc}")

                try:
                    segment = self._encoder.encode_range(frames)
                except Exception as exc:
                    _log(f"encode_range failed: {exc}")
                    self._playing.clear()
                    continue

                # Backpressure: block if queue is full.
                try:
                    while not self._stop_flag.is_set():
                        try:
                            self._queue.put(segment, timeout=0.25)
                            break
                        except queue.Full:
                            continue
                except Exception:
                    break

                # Advance playhead by however many frames we actually rendered.
                self._playhead_t = t0 + len(frames) / self._fps
                self._last_activity_ts = time.monotonic()
        finally:
            _log(f"render loop exiting for {self.project_dir.name}")


# ── RenderCoordinator ────────────────────────────────────────────────────


class RenderCoordinator:
    """Process-global. Caps concurrent workers at (cpu_count - 1), keyed by project_dir.

    MVP policy: workers are keyed by resolved project_dir (not per-session).
    New get_worker() calls beyond the cap evict the least-recently-used worker.
    """

    _instance: "RenderCoordinator | None" = None
    _class_lock = threading.Lock()

    @classmethod
    def instance(cls) -> "RenderCoordinator":
        with cls._class_lock:
            if cls._instance is None:
                cls._instance = cls()
            return cls._instance

    @classmethod
    def _reset_instance(cls) -> None:
        """Test-only: replace the singleton to give each test a clean coordinator."""
        with cls._class_lock:
            if cls._instance is not None:
                try:
                    cls._instance.shutdown()
                except Exception:
                    pass
            cls._instance = None

    def __init__(self, max_workers: int | None = None) -> None:
        if max_workers is None:
            max_workers = max(1, (os.cpu_count() or 2) - 1)
        self.max_workers = max_workers
        self._workers: "dict[str, RenderWorker]" = {}
        self._lock = threading.Lock()

    def get_worker(self, project_dir: Path) -> RenderWorker:
        """Lazily spawn or return an existing worker for a project."""
        key = str(Path(project_dir).resolve())
        with self._lock:
            worker = self._workers.get(key)
            if worker is not None:
                return worker

            # Enforce cap: evict LRU idle worker if we're at the limit.
            if len(self._workers) >= self.max_workers:
                self._evict_lru_locked()

            worker = RenderWorker(Path(project_dir))
            self._workers[key] = worker
            return worker

    def release_worker(self, project_dir: Path) -> None:
        """Explicitly tear down a worker. Does not raise if absent."""
        key = str(Path(project_dir).resolve())
        with self._lock:
            worker = self._workers.pop(key, None)
        if worker is not None:
            try:
                worker.stop()
            except Exception:
                pass

    def evict_idle(self, idle_timeout_s: int = IDLE_TIMEOUT_S) -> int:
        """Tear down any workers with no activity for at least idle_timeout_s seconds."""
        cutoff = time.monotonic() - idle_timeout_s
        evicted: list[RenderWorker] = []
        with self._lock:
            stale_keys = [
                k for k, w in self._workers.items()
                if w.last_activity_ts < cutoff and w.is_idle
            ]
            for k in stale_keys:
                evicted.append(self._workers.pop(k))
        for w in evicted:
            try:
                w.stop()
            except Exception:
                pass
        return len(evicted)

    def shutdown(self) -> None:
        """Tear down all workers. Safe to call multiple times."""
        with self._lock:
            workers = list(self._workers.values())
            self._workers.clear()
        for w in workers:
            try:
                w.stop()
            except Exception:
                pass

    def invalidate_project(self, project_dir: Path) -> bool:
        """Mark a worker as invalidated (called by DB write hooks). Returns True if a worker was affected."""
        key = str(Path(project_dir).resolve())
        with self._lock:
            worker = self._workers.get(key)
        if worker is None:
            return False
        worker.on_project_invalidate()
        return True

    def _evict_lru_locked(self) -> None:
        """Evict the least-recently-used worker. Caller holds self._lock."""
        if not self._workers:
            return
        lru_key = min(
            self._workers.keys(),
            key=lambda k: self._workers[k].last_activity_ts,
        )
        victim = self._workers.pop(lru_key)
        # Release lock briefly to stop (which joins a thread).
        # We currently hold the lock — since stop() joins, call it synchronously
        # but without holding the lock is nicer. We already popped from dict
        # so a subsequent get_worker() for the same key will spawn a fresh one.
        try:
            victim.stop()
        except Exception:
            pass

    @property
    def worker_count(self) -> int:
        with self._lock:
            return len(self._workers)
