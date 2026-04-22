"""Tests for the fMP4 fragment cache."""

from __future__ import annotations

from pathlib import Path

import pytest

from scenecraft.render.fragment_cache import FragmentCache


@pytest.fixture
def cache() -> FragmentCache:
    # Small caps so eviction behavior is easy to trigger.
    return FragmentCache(max_fragments=5, max_bytes=1024)


@pytest.fixture
def project(tmp_path: Path) -> Path:
    p = tmp_path / "project"
    p.mkdir()
    return p


def test_miss_then_hit_roundtrip(cache: FragmentCache, project: Path) -> None:
    assert cache.get(project, 0.0, 0) is None
    cache.put(project, 0.0, 0, b"fragment-bytes", duration_ms=2000)
    got = cache.get(project, 0.0, 0)
    assert got == b"fragment-bytes"

    stats = cache.stats()
    assert stats["fragments"] == 1
    assert stats["bytes"] == len(b"fragment-bytes")
    assert stats["hits"] == 1
    assert stats["misses"] == 1


def test_bucket_rounds_to_millisecond(cache: FragmentCache, project: Path) -> None:
    cache.put(project, 1.2344, 0, b"a", duration_ms=2000)
    # Same ms bucket (1234)
    assert cache.get(project, 1.2344, 0) == b"a"
    assert cache.get(project, 1.2343, 0) == b"a"
    # Different ms bucket
    assert cache.get(project, 1.236, 0) is None


def test_encoder_generation_isolates_entries(cache: FragmentCache, project: Path) -> None:
    cache.put(project, 0.0, encoder_generation=0, fmp4=b"old-gen", duration_ms=2000)
    cache.put(project, 0.0, encoder_generation=1, fmp4=b"new-gen", duration_ms=2000)

    assert cache.get(project, 0.0, 0) == b"old-gen"
    assert cache.get(project, 0.0, 1) == b"new-gen"
    assert cache.get(project, 0.0, 2) is None


def test_lru_eviction_by_count(cache: FragmentCache, project: Path) -> None:
    # max_fragments = 5; insert 6 → first evicted
    for i in range(6):
        cache.put(project, float(i), 0, b"x", duration_ms=2000)

    assert cache.get(project, 0.0, 0) is None  # evicted
    for i in range(1, 6):
        assert cache.get(project, float(i), 0) == b"x"


def test_lru_eviction_by_bytes(project: Path) -> None:
    c = FragmentCache(max_fragments=100, max_bytes=300)
    c.put(project, 0.0, 0, b"a" * 120, duration_ms=2000)
    c.put(project, 1.0, 0, b"b" * 120, duration_ms=2000)
    c.put(project, 2.0, 0, b"c" * 120, duration_ms=2000)  # total 360 > 300

    # Oldest (t=0) should be evicted
    assert c.get(project, 0.0, 0) is None
    assert c.get(project, 1.0, 0) is not None
    assert c.get(project, 2.0, 0) is not None


def test_invalidate_project(cache: FragmentCache, project: Path, tmp_path: Path) -> None:
    other = tmp_path / "other_project"
    other.mkdir()

    cache.put(project, 0.0, 0, b"p", duration_ms=2000)
    cache.put(project, 2.0, 0, b"p", duration_ms=2000)
    cache.put(other, 0.0, 0, b"o", duration_ms=2000)

    dropped = cache.invalidate_project(project)
    assert dropped == 2
    assert cache.get(project, 0.0, 0) is None
    assert cache.get(project, 2.0, 0) is None
    # Other project untouched
    assert cache.get(other, 0.0, 0) == b"o"


def test_invalidate_generation(cache: FragmentCache, project: Path) -> None:
    cache.put(project, 0.0, 0, b"a", duration_ms=2000)
    cache.put(project, 0.0, 1, b"b", duration_ms=2000)
    cache.put(project, 2.0, 1, b"c", duration_ms=2000)

    dropped = cache.invalidate_generation(project, 1)
    assert dropped == 2
    assert cache.get(project, 0.0, 0) == b"a"  # gen 0 survived
    assert cache.get(project, 0.0, 1) is None
    assert cache.get(project, 2.0, 1) is None


def test_invalidate_range_overlap(cache: FragmentCache, project: Path) -> None:
    # Fragments at 0-2s, 2-4s, 4-6s (duration_ms=2000 each)
    cache.put(project, 0.0, 0, b"a", duration_ms=2000)
    cache.put(project, 2.0, 0, b"b", duration_ms=2000)
    cache.put(project, 4.0, 0, b"c", duration_ms=2000)

    # Invalidate [3.0, 3.5] — overlaps fragment b (2-4s) only
    dropped = cache.invalidate_range(project, 3.0, 3.5)
    assert dropped == 1
    assert cache.get(project, 0.0, 0) == b"a"
    assert cache.get(project, 2.0, 0) is None
    assert cache.get(project, 4.0, 0) == b"c"


def test_invalidate_range_touching_boundary(cache: FragmentCache, project: Path) -> None:
    cache.put(project, 0.0, 0, b"a", duration_ms=2000)  # covers 0-2s
    cache.put(project, 2.0, 0, b"b", duration_ms=2000)  # covers 2-4s

    # Invalidate [2.0, 2.0] — touches both fragments at their boundary
    dropped = cache.invalidate_range(project, 2.0, 2.0)
    assert dropped == 2


def test_invalidate_range_empty(cache: FragmentCache, project: Path) -> None:
    cache.put(project, 0.0, 0, b"a", duration_ms=2000)
    # Inverted range (t_end < t_start) → no-op
    assert cache.invalidate_range(project, 5.0, 1.0) == 0
    assert cache.get(project, 0.0, 0) == b"a"


def test_invalidate_ranges_multiple(cache: FragmentCache, project: Path) -> None:
    for i in range(5):
        cache.put(project, float(i) * 2, 0, b"x", duration_ms=2000)
    # Fragments at 0-2, 2-4, 4-6, 6-8, 8-10

    # Invalidate [1, 3] and [7, 9]
    dropped = cache.invalidate_ranges(project, [(1.0, 3.0), (7.0, 9.0)])
    # Should drop fragments 0-2, 2-4, 6-8, 8-10 (4 total)
    assert dropped == 4
    assert cache.get(project, 4.0, 0) == b"x"  # 4-6 survived


def test_clear_resets_stats(cache: FragmentCache, project: Path) -> None:
    cache.put(project, 0.0, 0, b"a", duration_ms=2000)
    cache.get(project, 0.0, 0)  # +1 hit
    cache.get(project, 99.0, 0)  # +1 miss

    cache.clear()
    stats = cache.stats()
    assert stats["fragments"] == 0
    assert stats["bytes"] == 0
    assert stats["hits"] == 0
    assert stats["misses"] == 0


def test_put_overwrites_existing_key(cache: FragmentCache, project: Path) -> None:
    cache.put(project, 0.0, 0, b"v1", duration_ms=2000)
    cache.put(project, 0.0, 0, b"v2-longer", duration_ms=2000)

    assert cache.get(project, 0.0, 0) == b"v2-longer"
    stats = cache.stats()
    assert stats["fragments"] == 1
    # Byte count reflects only the current value
    assert stats["bytes"] == len(b"v2-longer")


def test_hit_rate_in_stats(cache: FragmentCache, project: Path) -> None:
    cache.put(project, 0.0, 0, b"a", duration_ms=2000)
    for _ in range(3):
        cache.get(project, 0.0, 0)  # hit
    cache.get(project, 99.0, 0)  # miss

    stats = cache.stats()
    assert stats["hits"] == 3
    assert stats["misses"] == 1
    assert stats["hit_rate"] == pytest.approx(3 / 4)
