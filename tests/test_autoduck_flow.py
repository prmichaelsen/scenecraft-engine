"""End-to-end auto-duck demo test.

Simulates the exact sub-agent flow for auto-duck:

  1. Two pool_segments on disk: a "vocal" (two bursts of speech-like energy with
     silence between) and a "music" (quiet sustained tone).
  2. An audio_track + audio_clip for the music so we have something to attach a
     volume curve to.
  3. Call the `generate_dsp` chat tool on the vocal segment with
     analyses=["rms", "vocal_presence"]. Verify it runs, writes sections, caches.
  4. Query `dsp_sections` for `section_type='vocal_presence'` — these are the
     ducking target regions.
  5. Compute a duck-curve from those regions: 1.0 baseline, ramp down to ~0.3
     during each vocal region with short fade in/out at the edges.
  6. Call the `update_volume_curve` chat tool on the music track with the
     computed curve.
  7. Read the music track back from DB and verify the curve shape matches.

If this test passes, the auto-duck chat flow is proven end-to-end.
"""

from __future__ import annotations

import json
import math
import wave
from pathlib import Path

import numpy as np
import pytest

from scenecraft.chat import _exec_generate_dsp, _exec_update_volume_curve
from scenecraft.db import (
    add_audio_clip as db_add_audio_clip,
    add_audio_track as db_add_audio_track,
    add_pool_segment,
    get_audio_tracks,
    get_db,
)


# ── Audio file fixtures ──────────────────────────────────────────────


def _write_wav(path: Path, samples: np.ndarray, sr: int = 22050) -> None:
    """Write mono float32 samples to a 16-bit PCM WAV."""
    clipped = np.clip(samples, -1.0, 1.0)
    as_int16 = (clipped * 32767).astype(np.int16)
    with wave.open(str(path), "wb") as f:
        f.setnchannels(1)
        f.setsampwidth(2)
        f.setframerate(sr)
        f.writeframes(as_int16.tobytes())


def _synth_vocal_like(duration_s: float, sr: int = 22050) -> np.ndarray:
    """Two loud 'vocal' bursts at 0.5–1.5s and 2.5–3.5s, silence elsewhere.

    Uses amplitude-modulated white noise (speech has broadband energy with
    natural amplitude variation). Bursts are loud enough for `detect_presence`
    to classify as above-threshold.
    """
    n = int(duration_s * sr)
    t = np.linspace(0, duration_s, n, endpoint=False)
    noise = np.random.default_rng(42).normal(0, 0.3, n)
    envelope = np.zeros(n)
    bursts = [(0.5, 1.5), (2.5, 3.5)]
    for start, end in bursts:
        idx = (t >= start) & (t < end)
        # Tiny fade-in/out so edges are smooth (real speech)
        local_t = (t[idx] - start) / (end - start)
        envelope[idx] = np.sin(np.pi * local_t) * 0.9
    return noise * envelope


def _synth_music_like(duration_s: float, sr: int = 22050) -> np.ndarray:
    """A quiet 220 Hz sustained tone — stands in for background music."""
    n = int(duration_s * sr)
    t = np.linspace(0, duration_s, n, endpoint=False)
    return 0.25 * np.sin(2 * math.pi * 220 * t)


@pytest.fixture
def autoduck_project(tmp_path: Path):
    """A project with a vocal pool_segment on disk + a music track+clip."""
    project_dir = tmp_path / "autoduck_project"
    project_dir.mkdir()
    get_db(project_dir)  # create schema

    pool_dir = project_dir / "pool" / "segments"
    pool_dir.mkdir(parents=True)

    # 1) Vocal segment (on disk + DB row)
    vocal_path_rel = "pool/segments/vocal.wav"
    vocal_abs = project_dir / vocal_path_rel
    _write_wav(vocal_abs, _synth_vocal_like(4.0))
    vocal_seg_id = add_pool_segment(
        project_dir,
        kind="imported",
        created_by="test",
        pool_path=vocal_path_rel,
        original_filename="vocal.wav",
        label="Vocal",
        duration_seconds=4.0,
    )

    # 2) Music track + clip (no on-disk audio needed for this path — the clip
    #    just carries the volume_curve we'll later rewrite)
    music_track_id = "music_track_1"
    db_add_audio_track(project_dir, {
        "id": music_track_id,
        "name": "Music",
        "display_order": 0,
    })
    music_clip_id = "music_clip_1"
    db_add_audio_clip(project_dir, {
        "id": music_clip_id,
        "track_id": music_track_id,
        "source_path": "pool/segments/music.wav",  # placeholder; not opened
        "start_time": 0.0,
        "end_time": 4.0,
        "source_offset": 0.0,
        "volume_curve": [[0, 1], [1, 1]],  # constant 1.0 before ducking
    })

    return {
        "project_dir": project_dir,
        "vocal_seg_id": vocal_seg_id,
        "music_track_id": music_track_id,
        "music_clip_id": music_clip_id,
    }


# ── The auto-duck flow ──────────────────────────────────────────────


def _compute_duck_curve(
    clip_duration_s: float,
    vocal_regions: list[tuple[float, float]],
    duck_level: float = 0.3,
    fade_s: float = 0.05,
) -> list[list[float]]:
    """Produce a [[t, v], ...] curve for a volume_curve column.

    Baseline 1.0; drops to ``duck_level`` over every ``vocal_regions`` range
    with ``fade_s`` fades on each edge.
    """
    points: list[list[float]] = []
    prev_end = 0.0
    points.append([0.0, 1.0])

    for start, end in vocal_regions:
        # Clamp to clip bounds
        start = max(0.0, start)
        end = min(clip_duration_s, end)
        if end <= start:
            continue

        fade_in_start = max(prev_end + 1e-4, start - fade_s)
        fade_out_end = min(clip_duration_s - 1e-4, end + fade_s)

        # Ensure strict monotonicity
        if fade_in_start > prev_end:
            points.append([fade_in_start, 1.0])
        points.append([start, duck_level])
        points.append([end, duck_level])
        points.append([fade_out_end, 1.0])
        prev_end = fade_out_end

    # Close out at clip end if we haven't already
    if prev_end < clip_duration_s:
        points.append([clip_duration_s, 1.0])

    return points


def test_autoduck_flow_end_to_end(autoduck_project):
    project_dir = autoduck_project["project_dir"]
    vocal_seg_id = autoduck_project["vocal_seg_id"]
    music_track_id = autoduck_project["music_track_id"]

    # --- Step 1: generate_dsp on the vocal segment ---
    result = _exec_generate_dsp(project_dir, {
        "source_segment_id": vocal_seg_id,
        "analyses": ["rms", "vocal_presence"],
    })
    assert "error" not in result, f"generate_dsp reported error: {result}"
    assert result["cached"] is False
    run_id = result["run_id"]
    assert result["datapoint_count"] > 0, "RMS envelope should produce datapoints"
    assert result["section_count"] >= 2, (
        f"expected ≥2 vocal_presence regions from 2 bursts, got {result['section_count']}"
    )

    # --- Step 2: query the dsp_sections for vocal_presence ---
    from scenecraft.db_analysis_cache import query_dsp_sections
    vp = query_dsp_sections(project_dir, run_id, section_type="vocal_presence")
    assert len(vp) >= 2

    # Sanity: regions should roughly cover our synthesised bursts (0.5-1.5s and 2.5-3.5s).
    # detect_presence is noisy at edges; assert that at least one region overlaps each burst.
    def _overlaps(r, burst_start, burst_end):
        return (r.start_s < burst_end) and (r.end_s > burst_start)

    assert any(_overlaps(r, 0.5, 1.5) for r in vp), (
        f"no vocal_presence region overlaps first burst (got: {[(r.start_s, r.end_s) for r in vp]})"
    )
    assert any(_overlaps(r, 2.5, 3.5) for r in vp), (
        f"no vocal_presence region overlaps second burst (got: {[(r.start_s, r.end_s) for r in vp]})"
    )

    # --- Step 3: compute duck curve from regions ---
    vocal_regions = [(r.start_s, r.end_s) for r in vp]
    duck_curve = _compute_duck_curve(4.0, vocal_regions, duck_level=0.3, fade_s=0.05)

    # Sanity the curve: strictly increasing times, starts at 0, contains duck_level values.
    times = [p[0] for p in duck_curve]
    assert times[0] == 0.0
    assert all(times[i] < times[i + 1] for i in range(len(times) - 1)), (
        f"duck curve times not strictly increasing: {times}"
    )
    values = [p[1] for p in duck_curve]
    assert any(abs(v - 0.3) < 1e-6 for v in values), "duck curve should drop to 0.3 somewhere"
    assert any(abs(v - 1.0) < 1e-6 for v in values), "duck curve should return to 1.0"

    # --- Step 4: update_volume_curve on the music track ---
    result = _exec_update_volume_curve(project_dir, {
        "target_type": "track",
        "target_id": music_track_id,
        "points": duck_curve,
    })
    assert "error" not in result, f"update_volume_curve reported error: {result}"
    assert result["ok"] is True
    assert result["points_written"] == len(duck_curve)

    # --- Step 5: verify the track's volume_curve was written ---
    tracks = get_audio_tracks(project_dir)
    music_track = next(t for t in tracks if t["id"] == music_track_id)
    stored_curve = music_track["volume_curve"]
    assert stored_curve == duck_curve, (
        f"stored volume_curve does not match computed duck curve.\n"
        f"expected: {duck_curve}\n"
        f"got:      {stored_curve}"
    )


def test_autoduck_flow_is_idempotent_on_rerun(autoduck_project):
    """Calling generate_dsp twice with the same args should be cache-hit the
    second time (confirming the auto-duck workflow only pays librosa cost once).
    """
    project_dir = autoduck_project["project_dir"]
    vocal_seg_id = autoduck_project["vocal_seg_id"]

    first = _exec_generate_dsp(project_dir, {
        "source_segment_id": vocal_seg_id,
        "analyses": ["rms", "vocal_presence"],
    })
    assert "error" not in first
    assert first["cached"] is False

    second = _exec_generate_dsp(project_dir, {
        "source_segment_id": vocal_seg_id,
        "analyses": ["rms", "vocal_presence"],
    })
    assert "error" not in second
    assert second["cached"] is True
    assert second["run_id"] == first["run_id"]
