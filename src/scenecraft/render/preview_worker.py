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

import concurrent.futures
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
FRAGMENT_SECONDS = 2.0       # one fMP4 media segment per 2s — aligns with 2s GOP
IDLE_TIMEOUT_S = 300         # tear down workers idle for this long
SCRUB_JPEG_QUALITY = 85      # JPEG quality used when opportunistically warming the scrub cache
# Max number of parallel threads rendering frames within a single fragment.
# cv2 releases the GIL during decode/compose so real parallelism holds.
# Each thread keeps its own VideoCapture handles (stream_caps) for the
# duration of its chunk — opened on first frame, released after chunk.
#
# At fragment boundaries each thread pays a fixed cost (open cap + seek
# to chunk-start frame ≈ 50-100ms at 1080p). If chunks shrink below ~2
# frames the open/seek overhead dominates decode, so the actual
# parallelism used is min(RENDER_MAX_PARALLELISM, frames_to_render // 2).
RENDER_MAX_PARALLELISM = max(2, os.cpu_count() or 2)
MIN_FRAMES_PER_CHUNK = 2


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
        # Force the ffmpeg-subprocess backend: the PyAV path opens a fresh
        # container per fragment, which forces an IDR every fragment and
        # bloats bitrate 5-8x. The subprocess keeps a single encoder alive
        # across fragments (IDR only at GOP boundaries).
        self._encoder = fragment_encoder or FragmentEncoder(
            width=self._schedule.width,
            height=self._schedule.height,
            fps=self._fps,
            force_backend="ffmpeg",
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
        # `stream_caps` lets the compositor keep a long-lived VideoCapture per
        # segment and advance sequentially instead of batch-loading every
        # frame of the segment into RAM — O(1) memory per open source, which
        # is the only survivable strategy for multi-hour base segments.
        self._frame_cache: dict = {"stream_caps": {}}

        # Persistent thread pool for parallel fragment rendering. Threads in
        # this pool hold thread-local `stream_caps` that survive across
        # fragments — opening cv2.VideoCapture on a 2.4h 1080p file costs
        # ~100-200ms each time; doing that 16× per fragment (once per chunk)
        # was dominating the render phase. With persistence, the open/seek
        # happens once per (thread × source), after which we do sequential
        # cap.read() calls which are cheap.
        self._render_pool: concurrent.futures.ThreadPoolExecutor | None = None
        self._render_pool_thread_local = threading.local()

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
        """Flush pending fragments past the current playhead, resume rendering from t.

        Does NOT rebuild the encoder. The client uses sb.mode = 'sequence'
        so DTS continuity doesn't matter — fragments are appended in order
        and video.currentTime tracks the sequence-of-delivery, not the
        project timecode. Keeping the encoder alive also keeps SPS/PPS
        identical, so the client's existing SourceBuffer can accept the
        post-seek fragments without needing a fresh init segment.
        """
        with self._seek_lock:
            self._playhead_t = max(0.0, float(t))
            self._drain_queue()
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
        # Release streaming caps owned by this worker's frame cache.
        for entry in self._frame_cache.get("stream_caps", {}).values():
            try:
                entry["cap"].release()
            except Exception:
                pass
        self._frame_cache["stream_caps"] = {}

        # Release thread-local stream_caps inside each render pool worker,
        # then shut the pool down. We submit release tasks equal to 2× the
        # pool size to reasonably cover every worker thread (ThreadPoolExecutor
        # may have spun fewer than max_workers if the pool was lightly used).
        pool = self._render_pool
        if pool is not None:
            tl = self._render_pool_thread_local

            def _release_tl_caps() -> None:
                caps = getattr(tl, "stream_caps", None)
                if caps is None:
                    return
                for entry in caps.values():
                    try:
                        entry["cap"].release()
                    except Exception:
                        pass
                tl.stream_caps = {}

            try:
                futures = [
                    pool.submit(_release_tl_caps)
                    for _ in range(RENDER_MAX_PARALLELISM * 2)
                ]
                for f in futures:
                    try:
                        f.result(timeout=1.0)
                    except Exception:
                        pass
            except Exception:
                pass
            try:
                pool.shutdown(wait=False)
            except Exception:
                pass
            self._render_pool = None

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
        _tick = 0
        try:
            while not self._stop_flag.is_set():
                _tick += 1
                if not self._playing.is_set():
                    if _tick % 20 == 1:
                        _log(f"loop idle (not playing), tick={_tick}, playhead={self._playhead_t:.2f}, dur={self._schedule.duration_seconds:.2f}")
                    self._playing.wait(timeout=0.5)
                    continue
                _log(f"loop active tick={_tick} playhead={self._playhead_t:.3f} dur={self._schedule.duration_seconds:.2f}")
                if self._invalidated.is_set():
                    self._invalidated.clear()
                    try:
                        # Release any open stream caps before swapping the schedule —
                        # the new one may have different segment indices.
                        for entry in self._frame_cache.get("stream_caps", {}).values():
                            try:
                                entry["cap"].release()
                            except Exception:
                                pass
                        new_sched = build_schedule(self.project_dir)
                        self._schedule = new_sched
                        self._frame_cache = {"stream_caps": {}}
                        _log(f"schedule rebuilt for {self.project_dir.name}")
                    except Exception as exc:
                        _log(f"rebuild failed: {exc}")
                        self._playing.clear()
                        continue

                # End-of-timeline?
                if self._playhead_t >= self._schedule.duration_seconds - 1e-6:
                    _log(f"loop: playhead {self._playhead_t:.2f} >= duration {self._schedule.duration_seconds:.2f} — idling")
                    self._playing.clear()
                    continue

                # Render one fragment's worth of frames in parallel chunks.
                # Sequential rendering at 1080p runs at ~1.4x realtime which
                # has no headroom for hiccups → stutter. N threads each with
                # their own stream_caps decode in parallel (cv2 releases the
                # GIL during decode/compose).
                frames_to_render = self._frames_per_fragment
                t0 = self._playhead_t
                parallelism = max(
                    1,
                    min(RENDER_MAX_PARALLELISM, frames_to_render // MIN_FRAMES_PER_CHUNK),
                )
                _log(
                    f"rendering fragment: t0={t0:.3f} "
                    f"frames_to_render={frames_to_render} parallelism={parallelism}"
                )

                schedule = self._schedule
                dur = schedule.duration_seconds
                fps = self._fps
                enc_h = self._encoder.height
                enc_w = self._encoder.width

                # Clamp frames_to_render to schedule end so we don't ask
                # render_frame_at for out-of-range times.
                effective = frames_to_render
                for i in range(frames_to_render):
                    if t0 + i / fps >= dur:
                        effective = i
                        break
                if effective == 0:
                    self._playing.clear()
                    continue

                chunk_size = max(1, math.ceil(effective / parallelism))

                def render_chunk(start_i: int, end_i: int) -> tuple[list[np.ndarray], dict]:
                    # Thread-local stream_caps: persist across fragments so
                    # cv2.VideoCapture opens happen at most once per
                    # (thread × source) instead of once per fragment.
                    tl = self._render_pool_thread_local
                    if not hasattr(tl, "stream_caps"):
                        tl.stream_caps = {}
                    chunk_timing: dict = {}
                    local_cache: dict = {"stream_caps": tl.stream_caps, "_timing": chunk_timing}
                    out: list[np.ndarray] = []
                    for i in range(start_i, end_i):
                        if self._stop_flag.is_set():
                            return out, chunk_timing
                        t = t0 + i / fps
                        try:
                            f = render_frame_at(schedule, t, frame_cache=local_cache)
                        except Exception as exc:
                            import traceback
                            _log(
                                f"render_frame_at({t:.3f}) failed: {exc}\n{traceback.format_exc()}"
                            )
                            f = np.zeros((schedule.height, schedule.width, 3), dtype=np.uint8)
                        if f.shape[0] != enc_h or f.shape[1] != enc_w:
                            _rs = time.monotonic()
                            f = cv2.resize(f, (enc_w, enc_h), interpolation=cv2.INTER_LINEAR)
                            chunk_timing["resize"] = chunk_timing.get("resize", 0.0) + (time.monotonic() - _rs)
                        out.append(f)
                    return out, chunk_timing

                ranges = [
                    (i, min(i + chunk_size, effective))
                    for i in range(0, effective, chunk_size)
                ]
                # Lazy-init the persistent pool on first fragment. max_workers
                # is fixed at RENDER_MAX_PARALLELISM so thread-local caps
                # settle and stay warm across the session.
                if self._render_pool is None:
                    self._render_pool = concurrent.futures.ThreadPoolExecutor(
                        max_workers=RENDER_MAX_PARALLELISM,
                        thread_name_prefix="render-chunk",
                    )
                render_t0 = time.monotonic()
                chunk_results = list(
                    self._render_pool.map(lambda ab: render_chunk(*ab), ranges)
                )
                frames = [f for chunk, _t in chunk_results for f in chunk]
                # Merge per-phase timings across all chunks for aggregate
                # per-fragment view. Parallel chunks overlap in wall time so
                # summed phase times will exceed render_elapsed — the ratio
                # tells us which phase is the hot spot.
                merged_timing: dict[str, float] = {}
                for _frames, t_dict in chunk_results:
                    for k, v in t_dict.items():
                        if k == "_frames":
                            merged_timing[k] = merged_timing.get(k, 0) + v
                        else:
                            merged_timing[k] = merged_timing.get(k, 0.0) + v
                render_elapsed = time.monotonic() - render_t0
                black_count = sum(1 for f in frames if f is None or not f.any())

                if not frames:
                    self._playing.clear()
                    continue
                _log(
                    f"parallel render done: {len(frames)} frames in "
                    f"{render_elapsed:.2f}s ({len(frames) / max(render_elapsed, 1e-6):.1f} fps)"
                )
                if merged_timing:
                    # Print summed phase times (across parallel chunks) so we
                    # can see which phase dominates.
                    phase_report = " ".join(
                        f"{k}={v:.2f}s"
                        for k, v in sorted(merged_timing.items(), key=lambda kv: -kv[1] if isinstance(kv[1], float) else 0)
                        if isinstance(v, float) and v >= 0.01
                    )
                    frames_summed = merged_timing.get("_frames", 0)
                    _log(f"phase times (summed across chunks, {frames_summed} frames): {phase_report}")

                _log(
                    f"fragment t0={t0:.3f} frames={len(frames)} black={black_count} "
                    f"first_shape={frames[0].shape} dtype={frames[0].dtype}"
                )

                # Ensure init has been emitted for the encoder; if a consumer
                # hasn't called fragments() yet we still can (encode_init sets
                # internal state but returning the bytes is a no-op for the queue).
                if not self._init_emitted:
                    try:
                        self._encoder.encode_init()
                    except Exception as exc:
                        _log(f"encode_init failed: {exc}")

                _log(f"encode_range: submitting {len(frames)} frames")
                _t0 = time.monotonic()
                try:
                    segment = self._encoder.encode_range(frames)
                except Exception as exc:
                    import traceback
                    _log(f"encode_range failed: {exc}\n{traceback.format_exc()}")
                    self._playing.clear()
                    continue
                enc_timing = getattr(self._encoder, "last_encode_timing", {})
                enc_detail = (
                    f" feed={enc_timing.get('feed', 0.0):.2f}s drain={enc_timing.get('drain', 0.0):.2f}s"
                    if "feed" in enc_timing else ""
                )
                _log(
                    f"fragment encoded: {len(segment)} bytes in "
                    f"{(time.monotonic() - _t0):.2f}s{enc_detail}"
                )
                # Mark when fragment was produced — the queue.put + pump
                # pickup latency below shows how long it sits before reaching
                # the client.
                produced_at = time.monotonic()

                # Backpressure: block if queue is full. Duration here shows
                # how much the worker stalled waiting for the client to
                # consume — if it's large, pump/client is the bottleneck.
                _q_wait = time.monotonic()
                try:
                    while not self._stop_flag.is_set():
                        try:
                            self._queue.put(segment, timeout=0.25)
                            break
                        except queue.Full:
                            continue
                except Exception:
                    break
                q_wait = time.monotonic() - _q_wait
                if q_wait > 0.1:
                    _log(f"queue.put waited {q_wait:.2f}s (backpressure from pump/client)")

                total_cycle = time.monotonic() - render_t0
                _log(
                    f"fragment cycle total: {total_cycle:.2f}s "
                    f"(render {render_elapsed:.2f}s + encode+queue {total_cycle - render_elapsed:.2f}s) "
                    f"target ≤ {FRAGMENT_SECONDS:.1f}s for realtime"
                )
                _ = produced_at  # keep variable for future pump-pickup tracking

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
        """Lazily spawn or return an existing worker for a project.

        A worker whose stop_flag is set has already had its encoder closed and
        thread joined — reusing it would make fragments() throw on
        encode_init(). Evict it and spawn a fresh one.
        """
        key = str(Path(project_dir).resolve())
        with self._lock:
            worker = self._workers.get(key)
            if worker is not None:
                if not worker._stop_flag.is_set():
                    return worker
                # Stopped worker lingered in the dict (e.g., client sent
                # action=stop then reconnected). Drop it.
                self._workers.pop(key, None)

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
