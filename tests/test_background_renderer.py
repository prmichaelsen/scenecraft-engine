"""Tests for the background renderer's priority queue + scheduling logic.

Doesn't spin up a real encoder / do real rendering — those paths need
full project fixtures and ffmpeg. Focuses on the unit that matters most
for correctness: priority ordering, queue deduplication, and the
playhead-reprioritization behavior.
"""

from __future__ import annotations

import threading
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from scenecraft.render.background_renderer import BackgroundRenderer
from scenecraft.render.fragment_cache import FragmentCache


@pytest.fixture
def project(tmp_path: Path) -> Path:
    p = tmp_path / "project"
    p.mkdir()
    return p


def _make_renderer(
    project: Path,
    duration_seconds: float = 100.0,
    fragment_seconds: float = 2.0,
    encoder_generation: int = 0,
    main_busy: bool = False,
) -> BackgroundRenderer:
    schedule = MagicMock()
    schedule.duration_seconds = duration_seconds
    schedule.height = 540
    schedule.width = 960

    encoder = MagicMock()
    encoder.height = 540
    encoder.width = 960

    r = BackgroundRenderer(
        project_dir=project,
        schedule=schedule,
        encoder=encoder,
        encoder_generation_cb=lambda: encoder_generation,
        main_busy_cb=lambda: main_busy,
        fragment_seconds=fragment_seconds,
        fps=24.0,
    )
    r.set_encoder_lock(threading.Lock())
    return r


# ── Queue management ──────────────────────────────────────────────────────


def test_request_range_enqueues_every_uncached_bucket(project: Path, monkeypatch) -> None:
    r = _make_renderer(project, duration_seconds=10.0, fragment_seconds=2.0)
    # No cache entries exist → every bucket should be enqueued
    r.request_range(0.0, 10.0)
    # Buckets at 0, 2, 4, 6, 8 → 5 items
    assert r.queue_size == 5


def test_request_range_skips_already_cached_buckets(
    project: Path, monkeypatch,
) -> None:
    from scenecraft.render import fragment_cache as fc_module
    cache = FragmentCache()
    monkeypatch.setattr(fc_module, "global_fragment_cache", cache)
    # background_renderer imports global_fragment_cache at module level;
    # monkeypatch the symbol there too.
    from scenecraft.render import background_renderer as br_module
    monkeypatch.setattr(br_module, "global_fragment_cache", cache)

    # Prepopulate cache for bucket t=4.0 (the project's state)
    cache.put(project, 4.0, 0, b"already-rendered", duration_ms=2000)

    r = _make_renderer(project, duration_seconds=10.0, fragment_seconds=2.0)
    r.request_range(0.0, 10.0)
    # Buckets at 0, 2, 6, 8 — four entries (t=4 skipped)
    assert r.queue_size == 4


def test_priority_puts_near_playhead_first(project: Path) -> None:
    r = _make_renderer(project, duration_seconds=20.0, fragment_seconds=2.0)
    r.update_playhead(10.0)
    r.request_range(0.0, 20.0)

    # Pop three; all should be near playhead
    popped: list[float] = []
    for _ in range(3):
        b = r._pop()
        assert b is not None
        popped.append(b.t0)

    # First pop should be 10.0 (distance 0); then 8 or 12 (distance 2); etc.
    assert popped[0] == 10.0
    assert set(popped[1:3]) <= {8.0, 12.0}


def test_update_playhead_reprioritizes(project: Path) -> None:
    r = _make_renderer(project, duration_seconds=20.0, fragment_seconds=2.0)
    r.update_playhead(2.0)
    r.request_range(0.0, 20.0)

    # Now jump playhead to 16.0
    r.update_playhead(16.0)

    # Next pop should be nearest to 16.0
    b = r._pop()
    assert b is not None
    assert b.t0 == 16.0


def test_priority_bias_applies(project: Path) -> None:
    r = _make_renderer(project, duration_seconds=20.0, fragment_seconds=2.0)
    r.update_playhead(10.0)
    # Queue two ranges — second with a strong negative bias so it wins
    r.request_range(10.0, 12.0)                    # t=10, priority = 0
    r.request_range(0.0, 2.0, priority_bias=-100)  # t=0, priority = 10 - 100 = -90

    b = r._pop()
    assert b is not None
    assert b.t0 == 0.0  # biased request wins


def test_prime_around_playhead_bounded_by_duration(project: Path) -> None:
    r = _make_renderer(project, duration_seconds=5.0, fragment_seconds=2.0)
    r.update_playhead(2.0)
    r.prime_around_playhead(radius_s=10.0)
    # Duration is only 5s → should enqueue 0, 2, 4 (bucket alignment)
    assert r.queue_size == 3


def test_request_range_inverted_is_noop(project: Path) -> None:
    r = _make_renderer(project)
    r.request_range(10.0, 5.0)  # inverted
    assert r.queue_size == 0


def test_request_range_clamps_to_duration(project: Path) -> None:
    r = _make_renderer(project, duration_seconds=8.0, fragment_seconds=2.0)
    # Request past the end — buckets 0, 2, 4, 6 (8 is at the boundary,
    # stops there)
    r.request_range(0.0, 100.0)
    assert r.queue_size == 4


def test_start_stop_is_idempotent(project: Path) -> None:
    r = _make_renderer(project)
    r.start()
    # Calling again while already running is a no-op
    r.start()
    r.stop(timeout=1.0)
    # Stop when already stopped is safe
    r.stop(timeout=1.0)


# ── Integration-ish: fragment cache deduplication in the pop path ─────────


def test_pop_skips_buckets_cached_since_enqueue(project: Path, monkeypatch) -> None:
    """If a bucket was enqueued uncached but the main worker rendered it
    before background got to it, _pop should skip over the stale entry."""
    from scenecraft.render import fragment_cache as fc_module
    cache = FragmentCache()
    monkeypatch.setattr(fc_module, "global_fragment_cache", cache)
    from scenecraft.render import background_renderer as br_module
    monkeypatch.setattr(br_module, "global_fragment_cache", cache)

    r = _make_renderer(project, duration_seconds=10.0, fragment_seconds=2.0)
    r.request_range(0.0, 10.0)
    assert r.queue_size == 5

    # Main worker "rendered" t=0.0 while background was waiting
    cache.put(project, 0.0, 0, b"rendered-by-main", duration_ms=2000)

    # _pop should return the next-closest bucket, not t=0.0
    b = r._pop()
    assert b is not None
    assert b.t0 != 0.0
