"""Parity tests for render_frame_at.

Locks in the guarantee that `render_frame_at(schedule, t)` produces identical
pixels regardless of call order. This is the gate before the scrub/playback
endpoints — if random-access rendering diverges from sequential rendering,
the preview will disagree with the final export.
"""

from __future__ import annotations

import numpy as np
import pytest

from scenecraft.db import add_keyframe, add_transition, get_db, set_meta_bulk
from scenecraft.render.compositor import render_frame_at
from scenecraft.render.schedule import build_schedule


FPS = 24
WIDTH = 320
HEIGHT = 240


def _make_gradient_video(path, seconds: float = 1.0) -> None:
    """Write a deterministic video: each frame is a gradient whose hue encodes frame index."""
    import cv2

    n_frames = int(seconds * FPS)
    writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"mp4v"), FPS, (WIDTH, HEIGHT))
    for i in range(n_frames):
        frame = np.zeros((HEIGHT, WIDTH, 3), dtype=np.uint8)
        # Encode frame index into the BGR channels so we can detect per-frame divergence
        frame[:, :, 0] = (i * 7) % 256        # B
        frame[:, :, 1] = (i * 11) % 256       # G
        frame[:, :, 2] = (i * 13) % 256       # R
        writer.write(frame)
    writer.release()


@pytest.fixture
def project(tmp_path):
    """A minimal project: two keyframes + one transition on track_1, one source video."""
    project_dir = tmp_path / "parity_project"
    project_dir.mkdir()
    # schema init
    get_db(project_dir)

    set_meta_bulk(project_dir, {
        "title": "parity",
        "fps": FPS,
        "resolution": [WIDTH, HEIGHT],
        "motion_prompt": "",
        "default_transition_prompt": "",
    })

    add_keyframe(project_dir, {
        "id": "kf_001",
        "timestamp": "0:00.00",
        "section": "",
        "source": "",
        "prompt": "start",
        "selected": 0,
        "candidates": [],
    })
    add_keyframe(project_dir, {
        "id": "kf_002",
        "timestamp": "0:01.00",
        "section": "",
        "source": "",
        "prompt": "end",
        "selected": 0,
        "candidates": [],
    })

    add_transition(project_dir, {
        "id": "tr_001",
        "from": "kf_001",
        "to": "kf_002",
        "duration_seconds": 1.0,
        "slots": 1,
        "action": "",
        "selected": [0],
        "remap": {"method": "linear", "target_duration": 0},
    })

    # Video file where build_schedule expects it
    sel_dir = project_dir / "selected_transitions"
    sel_dir.mkdir(parents=True)
    _make_gradient_video(sel_dir / "tr_001_slot_0.mp4", seconds=1.0)

    return project_dir


def _render_sequence(schedule, times):
    """Render each t in order, sharing a single frame_cache (playback-style)."""
    cache: dict = {}
    return [render_frame_at(schedule, t, frame_cache=cache) for t in times]


def _render_cold(schedule, times):
    """Render each t with a fresh cache (worst-case scrub on cold memory)."""
    return [render_frame_at(schedule, t, frame_cache={}) for t in times]


def test_random_access_matches_sequential(project):
    """Scrubbing out of order must produce identical pixels to playing in order."""
    schedule = build_schedule(project)
    times = [i / FPS for i in range(int(schedule.duration_seconds * FPS))]
    assert times, "fixture didn't produce any frames"

    seq = _render_sequence(schedule, times)
    rnd_order = list(reversed(range(len(times))))
    rnd_cache: dict = {}
    rnd = [None] * len(times)
    for i in rnd_order:
        rnd[i] = render_frame_at(schedule, times[i], frame_cache=rnd_cache)

    for i, (a, b) in enumerate(zip(seq, rnd)):
        assert np.array_equal(a, b), f"parity broke at frame {i} (t={times[i]:.3f}s)"


def test_cold_cache_matches_warm_cache(project):
    """First-touch frames (cold scrub) must match subsequent warmed-up frames."""
    schedule = build_schedule(project)
    times = [i / FPS for i in range(int(schedule.duration_seconds * FPS))]

    warm = _render_sequence(schedule, times)
    cold = _render_cold(schedule, times)

    for i, (a, b) in enumerate(zip(warm, cold)):
        assert np.array_equal(a, b), f"cold/warm divergence at frame {i} (t={times[i]:.3f}s)"


def test_repeated_render_is_stable(project):
    """render_frame_at must be idempotent for the same (schedule, t, cache)."""
    schedule = build_schedule(project)
    cache: dict = {}
    t = 0.25  # arbitrary frame inside the transition
    a = render_frame_at(schedule, t, frame_cache=cache)
    b = render_frame_at(schedule, t, frame_cache=cache)
    c = render_frame_at(schedule, t, frame_cache=cache)
    assert np.array_equal(a, b)
    assert np.array_equal(b, c)
