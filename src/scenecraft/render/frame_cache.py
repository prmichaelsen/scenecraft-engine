"""Frame cache for the scrub/playback renderer.

L1 (in-memory) only for now. Keyed on the full project version (mtime of
project.db + meta hash of render inputs), so any DB write invalidates the
project's cache wholesale. Fine-grained range-based invalidation is a
future optimization — see agent/design/local.backend-rendered-preview-streaming.md.
"""

from __future__ import annotations

import os
import threading
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import Tuple


# Cache key: (project_dir_str, db_mtime_ns, t_ms, quality)
CacheKey = Tuple[str, int, int, int]


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

    def _key(self, project_dir: Path, t: float, quality: int) -> CacheKey | None:
        # SQLite WAL mode: the .db file's mtime only changes on checkpoint.
        # Every write touches .db-wal instead. Use the max of both so any
        # write is observed immediately.
        mtimes: list[int] = []
        for name in ("project.db", "project.db-wal"):
            try:
                mtimes.append(os.stat(project_dir / name).st_mtime_ns)
            except FileNotFoundError:
                continue
        if not mtimes:
            return None
        mtime = max(mtimes)
        # Bucket t at millisecond precision — subframe-level determinism isn't
        # needed and would blow the cache on every mouse-pixel movement.
        return (str(project_dir), mtime, int(round(t * 1000)), quality)

    def get(self, project_dir: Path, t: float, quality: int) -> bytes | None:
        key = self._key(project_dir, t, quality)
        if key is None:
            return None
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
        if key is None:
            return
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
