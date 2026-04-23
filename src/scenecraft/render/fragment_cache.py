"""fMP4 fragment cache for preview playback.

Stores encoded fMP4 media-segment bytes keyed on
``(project_dir, t_ms_bucket, encoder_generation)`` so that replaying a
region served-from-cache skips render + encode entirely. Mirrors the
FrameCache (JPEG / scrub) pattern but for fragments.

Key rationale:
    * ``project_dir`` — isolates per-project state
    * ``t_ms_bucket`` — timeline time in milliseconds of the fragment's
      first sample; fragments are produced at FRAGMENT_SECONDS intervals
      so the bucket aligns naturally
    * ``encoder_generation`` — a counter the worker bumps whenever it
      rebuilds its encoder (seek, settings change, etc.). Each
      MediaSource on the client is initialized with a specific init
      segment's SPS/PPS — fragments from a later encoder generation have
      different SPS/PPS and would fail to decode if served to the older
      client. The generation key prevents cross-generation serving.

Cache is LRU by both entry count and total byte size; evict on whichever
hits first. Invalidation is driven by the same range-based path as
FrameCache (task-38): mutating endpoints compute their affected span and
call ``invalidate_range`` / ``invalidate_ranges``.
"""

from __future__ import annotations

import threading
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Tuple


# ``(project_dir_str, t0_ms, encoder_generation)``
CacheKey = Tuple[str, int, int]


@dataclass
class _FragmentEntry:
    fmp4: bytes
    duration_ms: int  # how much content this fragment represents
    bytes_size: int


class FragmentCache:
    """Thread-safe LRU cache of encoded fMP4 media segments."""

    def __init__(
        self,
        max_fragments: int = 200,
        max_bytes: int = 500 * 1024 * 1024,  # 500 MB
    ) -> None:
        self.max_fragments = max_fragments
        self.max_bytes = max_bytes
        self._lock = threading.Lock()
        self._store: "OrderedDict[CacheKey, _FragmentEntry]" = OrderedDict()
        self._total_bytes = 0
        self.hits = 0
        self.misses = 0

    # ── Key helpers ───────────────────────────────────────────────────────

    @staticmethod
    def _bucket_ms(t0: float) -> int:
        return int(round(t0 * 1000))

    def _key(self, project_dir: Path, t0: float, encoder_generation: int) -> CacheKey:
        return (str(project_dir), self._bucket_ms(t0), int(encoder_generation))

    # ── Get / put ─────────────────────────────────────────────────────────

    def get(
        self,
        project_dir: Path,
        t0: float,
        encoder_generation: int,
    ) -> bytes | None:
        key = self._key(project_dir, t0, encoder_generation)
        with self._lock:
            entry = self._store.get(key)
            if entry is None:
                self.misses += 1
                return None
            # LRU: touch
            self._store.move_to_end(key)
            self.hits += 1
            return entry.fmp4

    def put(
        self,
        project_dir: Path,
        t0: float,
        encoder_generation: int,
        fmp4: bytes,
        duration_ms: int,
    ) -> None:
        key = self._key(project_dir, t0, encoder_generation)
        entry = _FragmentEntry(
            fmp4=fmp4, duration_ms=int(duration_ms), bytes_size=len(fmp4),
        )
        with self._lock:
            existing = self._store.get(key)
            if existing is not None:
                self._total_bytes -= existing.bytes_size
                del self._store[key]
            self._store[key] = entry
            self._total_bytes += entry.bytes_size
            self._evict()

    def _evict(self) -> None:
        """Drop LRU entries until both caps are satisfied."""
        while (
            self._store
            and (
                len(self._store) > self.max_fragments
                or self._total_bytes > self.max_bytes
            )
        ):
            _, evicted = self._store.popitem(last=False)
            self._total_bytes -= evicted.bytes_size

    # ── Invalidation ──────────────────────────────────────────────────────

    def invalidate_project(self, project_dir: Path) -> int:
        """Drop all entries for ``project_dir``. Returns count evicted.

        Escape hatch for mutations whose affected range is hard to
        compute (e.g., full project reload, undo/redo).
        """
        prefix = str(project_dir)
        with self._lock:
            to_drop = [k for k in self._store if k[0] == prefix]
            for k in to_drop:
                self._total_bytes -= self._store[k].bytes_size
                del self._store[k]
            return len(to_drop)

    def invalidate_generation(
        self,
        project_dir: Path,
        encoder_generation: int,
    ) -> int:
        """Drop all entries for one (project, generation) pair.

        Called when a client's MediaSource is being torn down — the new
        session bumps encoder_generation, and the old gen's fragments are
        never going to be useful again.
        """
        prefix = str(project_dir)
        with self._lock:
            to_drop = [
                k for k in self._store
                if k[0] == prefix and k[2] == encoder_generation
            ]
            for k in to_drop:
                self._total_bytes -= self._store[k].bytes_size
                del self._store[k]
            return len(to_drop)

    def invalidate_range(
        self,
        project_dir: Path,
        t_start: float,
        t_end: float,
    ) -> int:
        """Drop entries whose bucket is inside [t_start, t_end].

        Semantics match FrameCache.invalidate_range: any fragment whose
        ``t0_ms`` falls in the closed range — or whose content range
        *overlaps* the invalidated range — gets dropped. We only have
        ``t0_ms`` in the key, so check if the entry's ``[t0, t0 + duration)``
        overlaps ``[t_start, t_end]``.
        """
        if t_end < t_start:
            return 0
        prefix = str(project_dir)
        t_start_ms = int(round(t_start * 1000))
        t_end_ms = int(round(t_end * 1000))
        with self._lock:
            to_drop: list[CacheKey] = []
            for k, entry in self._store.items():
                if k[0] != prefix:
                    continue
                t0 = k[1]
                t1 = t0 + entry.duration_ms
                # overlap: [t0, t1] ∩ [t_start_ms, t_end_ms] non-empty
                if t1 >= t_start_ms and t0 <= t_end_ms:
                    to_drop.append(k)
            for k in to_drop:
                self._total_bytes -= self._store[k].bytes_size
                del self._store[k]
            return len(to_drop)

    def invalidate_ranges(
        self,
        project_dir: Path,
        ranges: Iterable[tuple[float, float]],
    ) -> int:
        """Multiple ranges in one locked pass. Total evictions."""
        ranges_ms = [
            (int(round(a * 1000)), int(round(b * 1000)))
            for a, b in ranges
            if b >= a
        ]
        if not ranges_ms:
            return 0
        prefix = str(project_dir)
        with self._lock:
            to_drop: list[CacheKey] = []
            for k, entry in self._store.items():
                if k[0] != prefix:
                    continue
                t0 = k[1]
                t1 = t0 + entry.duration_ms
                for a, b in ranges_ms:
                    if t1 >= a and t0 <= b:
                        to_drop.append(k)
                        break
            for k in to_drop:
                self._total_bytes -= self._store[k].bytes_size
                del self._store[k]
            return len(to_drop)

    # ── Introspection ─────────────────────────────────────────────────────

    def cached_spans_for_gen(
        self,
        project_dir: Path,
        encoder_generation: int,
    ) -> list[tuple[int, int]]:
        """Return ``[(t0_ms, t1_ms), ...]`` for every cached fragment in
        (project, gen). Used by render_state.build_snapshot to mark
        display buckets as cached even when fragment t0 doesn't land on
        the display grid (happens at non-integer fps — e.g. 23.976fps
        advances by 2.002s per fragment).
        """
        prefix = str(project_dir)
        gen = int(encoder_generation)
        with self._lock:
            return [
                (k[1], k[1] + entry.duration_ms)
                for k, entry in self._store.items()
                if k[0] == prefix and k[2] == gen
            ]

    def stats(self) -> dict:
        with self._lock:
            total = self.hits + self.misses
            return {
                "fragments": len(self._store),
                "bytes": self._total_bytes,
                "max_fragments": self.max_fragments,
                "max_bytes": self.max_bytes,
                "hits": self.hits,
                "misses": self.misses,
                "hit_rate": (self.hits / total) if total else 0.0,
            }

    def clear(self) -> None:
        with self._lock:
            self._store.clear()
            self._total_bytes = 0
            self.hits = 0
            self.misses = 0


# Module-level cache, shared across all preview workers in the process.
# Per-session caches are a later refinement (would need per-user
# isolation policy — see design doc §per-user caches).
global_fragment_cache = FragmentCache()
