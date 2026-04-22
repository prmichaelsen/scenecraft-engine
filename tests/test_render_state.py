"""Tests for render_state — snapshot derivation + delta dispatcher."""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from scenecraft.render.fragment_cache import FragmentCache
from scenecraft.render.render_state import (
    BucketEntry,
    _DeltaDispatcher,
    build_snapshot,
)


@pytest.fixture
def project(tmp_path: Path) -> Path:
    p = tmp_path / "project"
    p.mkdir()
    return p


# ── Snapshot derivation ──────────────────────────────────────────────────


def test_snapshot_all_unrendered_when_cache_empty(
    project: Path, monkeypatch,
) -> None:
    from scenecraft.render import render_state as rs
    cache = FragmentCache()
    monkeypatch.setattr(rs, "global_fragment_cache", cache, raising=False)
    # build_snapshot does a late import — patch at the import site too
    import scenecraft.render.fragment_cache as fc_module
    monkeypatch.setattr(fc_module, "global_fragment_cache", cache)

    out = build_snapshot(
        project_dir=project,
        duration_seconds=10.0,
        fragment_seconds=2.0,
        encoder_generation=0,
    )
    # 5 buckets at 0, 2, 4, 6, 8 — all unrendered
    assert len(out) == 5
    assert all(b.state == "unrendered" for b in out)
    assert out[0].t_start == 0.0
    assert out[0].t_end == 2.0
    assert out[-1].t_start == 8.0
    assert out[-1].t_end == 10.0


def test_snapshot_marks_cached_buckets(project: Path, monkeypatch) -> None:
    cache = FragmentCache()
    import scenecraft.render.fragment_cache as fc_module
    monkeypatch.setattr(fc_module, "global_fragment_cache", cache)

    # Cache buckets at t=2 and t=6
    cache.put(project, 2.0, 0, b"a", duration_ms=2000)
    cache.put(project, 6.0, 0, b"b", duration_ms=2000)

    out = build_snapshot(
        project_dir=project,
        duration_seconds=10.0,
        fragment_seconds=2.0,
        encoder_generation=0,
    )
    states = {b.t_start: b.state for b in out}
    assert states[0.0] == "unrendered"
    assert states[2.0] == "cached"
    assert states[4.0] == "unrendered"
    assert states[6.0] == "cached"
    assert states[8.0] == "unrendered"


def test_snapshot_marks_rendering_from_queue(project: Path, monkeypatch) -> None:
    cache = FragmentCache()
    import scenecraft.render.fragment_cache as fc_module
    monkeypatch.setattr(fc_module, "global_fragment_cache", cache)

    out = build_snapshot(
        project_dir=project,
        duration_seconds=10.0,
        fragment_seconds=2.0,
        encoder_generation=0,
        background_queue_t0s={4.0, 8.0},
    )
    states = {b.t_start: b.state for b in out}
    assert states[4.0] == "rendering"
    assert states[8.0] == "rendering"
    assert states[0.0] == "unrendered"
    assert states[2.0] == "unrendered"
    assert states[6.0] == "unrendered"


def test_snapshot_cached_beats_rendering(project: Path, monkeypatch) -> None:
    """A cached bucket should never show as rendering even if it's also
    in the background queue (stale enqueue)."""
    cache = FragmentCache()
    import scenecraft.render.fragment_cache as fc_module
    monkeypatch.setattr(fc_module, "global_fragment_cache", cache)
    cache.put(project, 4.0, 0, b"a", duration_ms=2000)

    out = build_snapshot(
        project_dir=project,
        duration_seconds=10.0,
        fragment_seconds=2.0,
        encoder_generation=0,
        background_queue_t0s={4.0},
    )
    assert {b.t_start: b.state for b in out}[4.0] == "cached"


def test_snapshot_encoder_generation_isolates(project: Path, monkeypatch) -> None:
    cache = FragmentCache()
    import scenecraft.render.fragment_cache as fc_module
    monkeypatch.setattr(fc_module, "global_fragment_cache", cache)

    # Cached under gen=0; snapshot for gen=1 should NOT see it
    cache.put(project, 2.0, 0, b"a", duration_ms=2000)

    out_gen0 = build_snapshot(project, 10.0, 2.0, encoder_generation=0)
    out_gen1 = build_snapshot(project, 10.0, 2.0, encoder_generation=1)

    assert {b.t_start: b.state for b in out_gen0}[2.0] == "cached"
    assert {b.t_start: b.state for b in out_gen1}[2.0] == "unrendered"


def test_snapshot_last_bucket_clamps_to_duration(project: Path, monkeypatch) -> None:
    cache = FragmentCache()
    import scenecraft.render.fragment_cache as fc_module
    monkeypatch.setattr(fc_module, "global_fragment_cache", cache)

    # 9s duration with 2s buckets → buckets at 0, 2, 4, 6, 8; last bucket
    # covers [8, 9] not [8, 10]
    out = build_snapshot(project, 9.0, 2.0, encoder_generation=0)
    assert len(out) == 5
    assert out[-1].t_start == 8.0
    assert out[-1].t_end == 9.0


# ── Delta dispatcher ──────────────────────────────────────────────────────


def test_dispatcher_coalesces_rapid_transitions() -> None:
    d = _DeltaDispatcher(coalesce_window_s=0.05)
    received: list[list[BucketEntry]] = []
    unsub = d.subscribe(lambda batch: received.append(batch))

    d.record("/p", BucketEntry(t_start=0.0, t_end=2.0, state="rendering"))
    d.record("/p", BucketEntry(t_start=0.0, t_end=2.0, state="cached"))
    d.record("/p", BucketEntry(t_start=2.0, t_end=4.0, state="rendering"))

    # Wait past the window
    time.sleep(0.12)

    unsub()

    # Should see ONE batch with 2 entries (key=(/p, 0) and (/p, 2))
    assert len(received) == 1
    batch = received[0]
    states = {int(round(b.t_start * 1000)): b.state for b in batch}
    # (0, 0) only keeps the most recent state (cached), (0, 2) is rendering
    assert states[0] == "cached"
    assert states[2000] == "rendering"


def test_dispatcher_unsubscribe_stops_delivery() -> None:
    d = _DeltaDispatcher(coalesce_window_s=0.05)
    received: list[list[BucketEntry]] = []
    unsub = d.subscribe(lambda batch: received.append(batch))
    unsub()
    d.record("/p", BucketEntry(t_start=0.0, t_end=2.0, state="cached"))
    time.sleep(0.12)
    assert received == []


def test_dispatcher_multiple_subscribers_both_receive() -> None:
    d = _DeltaDispatcher(coalesce_window_s=0.05)
    a: list[list[BucketEntry]] = []
    b: list[list[BucketEntry]] = []
    d.subscribe(lambda batch: a.append(batch))
    d.subscribe(lambda batch: b.append(batch))
    d.record("/p", BucketEntry(t_start=0.0, t_end=2.0, state="cached"))
    time.sleep(0.12)
    assert len(a) == 1
    assert len(b) == 1
