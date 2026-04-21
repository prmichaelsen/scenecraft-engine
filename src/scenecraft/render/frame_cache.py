"""Frame cache for the scrub/playback renderer.

L1 (in-memory) only for now. Keyed on `(project_dir, t_ms, quality)` —
entries survive arbitrary DB writes. Callers (the mutating API endpoints)
explicitly call `invalidate_range(project, t_start, t_end)` to drop
entries whose time falls inside the range that the edit actually
affects. Wholesale invalidation is still available via
`invalidate_project` as an escape hatch for operations where computing a
tight range would be error-prone (e.g., undo/redo).
"""

from __future__ import annotations

import threading
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import Tuple


# Cache key: (project_dir_str, t_ms, quality)
CacheKey = Tuple[str, int, int]


@dataclass
class _Entry:
    jpeg: bytes
    bytes_size: int


class FrameCache:
    """Thread-safe LRU cache of encoded JPEG frames."""

    def __init__(self, max_frames: int = 500, max_bytes: int = 250 * 1024 * 1024):
        self.max_frames = max_frames
        self.max_bytes = max_bytes
        self._lock = threading.Lock()
        self._store: OrderedDict[CacheKey, _Entry] = OrderedDict()
        self._total_bytes = 0
        self.hits = 0
        self.misses = 0

    def _key(self, project_dir: Path, t: float, quality: int) -> CacheKey:
        # Bucket t at millisecond precision — subframe-level determinism isn't
        # needed and would blow the cache on every mouse-pixel movement.
        return (str(project_dir), int(round(t * 1000)), quality)

    def get(self, project_dir: Path, t: float, quality: int) -> bytes | None:
        key = self._key(project_dir, t, quality)
        with self._lock:
            entry = self._store.get(key)
            if entry is None:
                self.misses += 1
                return None
            # Mark as most-recently-used
            self._store.move_to_end(key)
            self.hits += 1
            return entry.jpeg

    def put(self, project_dir: Path, t: float, quality: int, jpeg: bytes) -> None:
        key = self._key(project_dir, t, quality)
        entry = _Entry(jpeg=jpeg, bytes_size=len(jpeg))
        with self._lock:
            if key in self._store:
                self._total_bytes -= self._store[key].bytes_size
                del self._store[key]
            self._store[key] = entry
            self._total_bytes += entry.bytes_size
            self._evict()

    def _evict(self) -> None:
        # Evict LRU until both caps are satisfied
        while (
            self._store
            and (
                len(self._store) > self.max_frames
                or self._total_bytes > self.max_bytes
            )
        ):
            _, evicted = self._store.popitem(last=False)
            self._total_bytes -= evicted.bytes_size

    def invalidate_project(self, project_dir: Path) -> int:
        """Drop all entries for a project. Returns how many were evicted."""
        prefix = str(project_dir)
        with self._lock:
            to_drop = [k for k in self._store if k[0] == prefix]
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
        """Drop cache entries for `project_dir` whose time falls in [t_start, t_end].

        Bounds are inclusive at both ends (seconds). `t_start > t_end`
        returns 0 without raising. Returns count of entries evicted.
        """
        if t_end < t_start:
            return 0
        prefix = str(project_dir)
        t_start_ms = int(round(t_start * 1000))
        t_end_ms = int(round(t_end * 1000))
        with self._lock:
            to_drop = [
                k for k in self._store
                if k[0] == prefix and t_start_ms <= k[1] <= t_end_ms
            ]
            for k in to_drop:
                self._total_bytes -= self._store[k].bytes_size
                del self._store[k]
            return len(to_drop)

    def invalidate_ranges(
        self,
        project_dir: Path,
        ranges: list[tuple[float, float]],
    ) -> int:
        """Invalidate multiple ranges in one locked pass. Returns total evicted."""
        if not ranges:
            return 0
        prefix = str(project_dir)
        ranges_ms = [
            (int(round(a * 1000)), int(round(b * 1000)))
            for a, b in ranges
            if b >= a
        ]
        if not ranges_ms:
            return 0
        with self._lock:
            to_drop = []
            for k in self._store:
                if k[0] != prefix:
                    continue
                t_ms = k[1]
                for a, b in ranges_ms:
                    if a <= t_ms <= b:
                        to_drop.append(k)
                        break
            for k in to_drop:
                self._total_bytes -= self._store[k].bytes_size
                del self._store[k]
            return len(to_drop)

    def stats(self) -> dict:
        with self._lock:
            total = self.hits + self.misses
            return {
                "frames": len(self._store),
                "bytes": self._total_bytes,
                "max_frames": self.max_frames,
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


# Module-global cache shared across all API handler instances.
# Per-user session caches are a later refinement (design doc 2.2).
global_cache = FrameCache()
