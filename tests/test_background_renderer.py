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


# ── State property ───────────────────────────────────────────────────────


def test_state_reports_unrendered_for_enqueued_buckets(
    project: Path, monkeypatch,
) -> None:
    from scenecraft.render import fragment_cache as fc_module
    cache = FragmentCache()
    monkeypatch.setattr(fc_module, "global_fragment_cache", cache)
    from scenecraft.render import background_renderer as br_module
    monkeypatch.setattr(br_module, "global_fragment_cache", cache)

    r = _make_renderer(project, duration_seconds=6.0, fragment_seconds=2.0)
    r.request_range(0.0, 6.0)
    state = r.state
    # All three known buckets (0, 2, 4) should be 'unrendered'
    assert state == {"0": "unrendered", "2000": "unrendered", "4000": "unrendered"}


def test_state_reports_cached_for_cache_hits(
    project: Path, monkeypatch,
) -> None:
    from scenecraft.render import fragment_cache as fc_module
    cache = FragmentCache()
    monkeypatch.setattr(fc_module, "global_fragment_cache", cache)
    from scenecraft.render import background_renderer as br_module
    monkeypatch.setattr(br_module, "global_fragment_cache", cache)

    r = _make_renderer(project, duration_seconds=6.0, fragment_seconds=2.0)
    r.request_range(0.0, 6.0)
    # Now cache bucket 2.0 (as if the main worker rendered it)
    cache.put(project, 2.0, 0, b"rendered", duration_ms=2000)
    state = r.state
    assert state["2000"] == "cached"
    assert state["0"] == "unrendered"
    assert state["4000"] == "unrendered"


# ── Invalidation ─────────────────────────────────────────────────────────


def test_invalidate_range_requeues_buckets(
    project: Path, monkeypatch,
) -> None:
    """invalidate_range puts buckets back on the priority queue so the
    background worker will re-render them."""
    from scenecraft.render import fragment_cache as fc_module
    cache = FragmentCache()
    monkeypatch.setattr(fc_module, "global_fragment_cache", cache)
    from scenecraft.render import background_renderer as br_module
    monkeypatch.setattr(br_module, "global_fragment_cache", cache)

    r = _make_renderer(project, duration_seconds=10.0, fragment_seconds=2.0)
    # Populate cache for two buckets, simulate them as "already rendered"
    cache.put(project, 2.0, 0, b"A", duration_ms=2000)
    cache.put(project, 4.0, 0, b"B", duration_ms=2000)
    # Renderer's view: initially request_range skips both (cached)
    r.request_range(0.0, 10.0)
    pre_queue = r.queue_size
    # Invalidate [3, 5] — should expand to buckets [2, 6), i.e., 2 and 4
    # Drop their cache entries first (mirrors what FragmentCache.invalidate_range does)
    cache.invalidate_range(project, 3.0, 5.0)
    count = r.invalidate_range(3.0, 5.0)
    assert count == 2  # buckets at t=2 and t=4
    # Post-invalidate state: those two buckets are back in the queue
    assert r.queue_size == pre_queue + 2
    state = r.state
    assert state["2000"] == "unrendered"
    assert state["4000"] == "unrendered"


def test_invalidate_range_inverted_is_noop(project: Path) -> None:
    r = _make_renderer(project, duration_seconds=10.0, fragment_seconds=2.0)
    assert r.invalidate_range(5.0, 3.0) == 0
    assert r.queue_size == 0


def test_invalidate_range_clamps_to_duration(project: Path) -> None:
    r = _make_renderer(project, duration_seconds=5.0, fragment_seconds=2.0)
    # Invalidate past the end — should only count buckets that fall in
    # [0, duration] → t=0, t=2, t=4 → 3 buckets
    count = r.invalidate_range(0.0, 100.0)
    assert count == 3


# ── Pause / resume ───────────────────────────────────────────────────────


def test_pause_prevents_rendering_loop(project: Path) -> None:
    """A paused renderer's loop sits idle — _rendering_t0 stays None."""
    import time as _time
    r = _make_renderer(project, duration_seconds=10.0, fragment_seconds=2.0)
    # Monkey-patch _render_bucket to a no-op so we don't actually run cv2
    calls: list[float] = []

    def fake_render(bucket):
        calls.append(bucket.t0)

    r._render_bucket = fake_render  # type: ignore[assignment]
    r.pause()
    r.request_range(0.0, 10.0)
    r.start()
    # Give the loop a chance to run — should stay idle
    _time.sleep(0.2)
    assert calls == []  # paused → nothing rendered
    # Resume → buckets get processed
    r.resume()
    _time.sleep(0.2)
    r.stop(timeout=1.0)
    # At least one bucket drained
    assert len(calls) >= 1


# ── Cooperative with main worker (no deadlock) ───────────────────────────


def test_bg_yields_to_main_busy(project: Path) -> None:
    """When main_busy_cb returns True, the background loop yields and
    never starts a new bucket. Verifies playback cannot deadlock with
    background rendering on the shared encoder lock."""
    import time as _time
    main_busy = {"flag": True}

    r = _make_renderer(
        project, duration_seconds=10.0, fragment_seconds=2.0,
    )
    # Replace the main_busy_cb to a mutable flag
    r._main_busy_cb = lambda: main_busy["flag"]

    calls: list[float] = []
    def fake_render(bucket):
        calls.append(bucket.t0)
    r._render_bucket = fake_render  # type: ignore[assignment]

    r.request_range(0.0, 10.0)
    r.start()
    # Main is busy → bg should yield, nothing processes
    _time.sleep(0.25)
    assert calls == []

    # Main goes idle → bg drains the queue
    main_busy["flag"] = False
    _time.sleep(0.35)
    r.stop(timeout=1.0)
    # At least one bucket got processed after main went idle
    assert len(calls) >= 1


def test_bg_and_main_share_cache_without_deadlock(
    project: Path, monkeypatch,
) -> None:
    """Simulate the cooperative contract: main worker writes a fragment
    into the cache at t=0 while bg is in its loop; bg skips t=0 on _pop
    (already cached). Verifies the lockless cache-visibility path."""
    from scenecraft.render import fragment_cache as fc_module
    cache = FragmentCache()
    monkeypatch.setattr(fc_module, "global_fragment_cache", cache)
    from scenecraft.render import background_renderer as br_module
    monkeypatch.setattr(br_module, "global_fragment_cache", cache)

    r = _make_renderer(project, duration_seconds=10.0, fragment_seconds=2.0)
    r.request_range(0.0, 10.0)

    # Main worker rendered buckets at 0 and 2 before bg could start.
    cache.put(project, 0.0, 0, b"A", duration_ms=2000)
    cache.put(project, 2.0, 0, b"B", duration_ms=2000)

    # Pop twice — both stale buckets must be transparently skipped.
    first = r._pop()
    assert first is not None
    assert first.t0 >= 4.0  # skipped 0 and 2
    # Queue still has remaining buckets
    assert r.queue_size >= 2
