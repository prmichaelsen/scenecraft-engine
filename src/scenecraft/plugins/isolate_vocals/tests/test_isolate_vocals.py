"""Tests for the M11 task-102 isolate-vocals plugin.

Uses a mock DFN3 in place of the real model — ``model.denoise_wav`` is
monkeypatched to a function that writes a silent WAV of the same duration.
The residual subtraction then yields ``source - silence == source``, which
keeps the schema wiring realistic without requiring the DFN3 binary in CI.
"""

from __future__ import annotations

import time
import wave
from pathlib import Path

import pytest


# ── Fixtures ─────────────────────────────────────────────────────────────


@pytest.fixture
def project(tmp_path):
    from scenecraft.db import get_db

    project_dir = tmp_path / "iso_project"
    project_dir.mkdir()
    get_db(project_dir)  # forces schema bootstrap
    (project_dir / "pool" / "segments").mkdir(parents=True, exist_ok=True)
    return project_dir


@pytest.fixture
def source_wav(project):
    """A 1-second mono 48kHz PCM WAV that looks like an actual audio file."""
    pool = project / "pool" / "segments"
    src_id = "src_audio_clip_1"
    src_path = pool / f"{src_id}.wav"
    sr = 48000
    n = sr  # 1 second
    # Generate pseudo-random but deterministic 16-bit PCM samples.
    import numpy as np

    rng = np.random.default_rng(42)
    pcm = rng.integers(-10000, 10000, size=n, dtype=np.int16).tobytes()
    with wave.open(str(src_path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(pcm)
    return src_path


@pytest.fixture
def audio_clip(project, source_wav):
    """Insert a row in audio_clips pointing at the fixture WAV."""
    from scenecraft.db import add_audio_clip, add_audio_track

    track_id = "audio_track_test_1"
    add_audio_track(project, {"id": track_id, "name": "test", "display_order": 0})
    clip_id = "audio_clip_test_1"
    rel = source_wav.relative_to(project).as_posix()
    add_audio_clip(
        project,
        {
            "id": clip_id,
            "track_id": track_id,
            "source_path": rel,
            "start_time": 0.0,
            "end_time": 1.0,
        },
    )
    return clip_id


@pytest.fixture
def mock_dfn3(monkeypatch):
    """Patch denoise_wav with a fake that writes a silent WAV of matching duration.

    This avoids needing the DeepFilterNet3 binary in CI while still exercising
    the file-io + residual-subtraction pipeline.
    """
    def fake_denoise(in_path: Path, out_path: Path) -> None:
        with wave.open(str(in_path), "rb") as r:
            sr = r.getframerate()
            n = r.getnframes()
        # Silent output preserves duration; residual = source - silence = source.
        silence = b"\x00\x00" * n
        with wave.open(str(out_path), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(sr)
            w.writeframes(silence)

    from scenecraft.plugins.isolate_vocals import model as model_mod

    monkeypatch.setattr(model_mod, "denoise_wav", fake_denoise)
    return fake_denoise


def _wait_for_job(job_id: str, timeout: float = 10.0):
    """Spin on the job_manager until the job reaches a terminal state.

    Returns the Job dataclass instance (not a dict). Attribute access:
    ``job.status``, ``job.result``, ``job.error``.
    """
    from scenecraft.ws_server import job_manager

    deadline = time.time() + timeout
    while time.time() < deadline:
        job = job_manager.get_job(job_id)
        if job is not None and job.status in ("completed", "failed"):
            return job
        time.sleep(0.05)
    raise AssertionError(f"job {job_id} did not terminate within {timeout}s")


# ── Happy path ────────────────────────────────────────────────────────────


def test_run_audio_clip_happy_path(project, audio_clip, mock_dfn3):
    from scenecraft.plugins.isolate_vocals import run

    result = run(
        "audio_clip",
        audio_clip,
        {"project_dir": project, "project_name": "iso_project", "range_mode": "full"},
    )
    assert "error" not in result, result
    assert "isolation_id" in result
    assert "job_id" in result
    isolation_id = result["isolation_id"]
    job_id = result["job_id"]

    job = _wait_for_job(job_id)
    assert job.status == "completed", f"error={job.error!r}"

    # audio_isolations row + status + 2 stems
    from scenecraft.db import get_isolations_for_entity, get_isolation_stems

    runs = get_isolations_for_entity(project, "audio_clip", audio_clip)
    assert len(runs) == 1
    r = runs[0]
    assert r["id"] == isolation_id
    assert r["status"] == "completed"
    assert r["model"] == "deepfilternet3"
    assert len(r["stems"]) == 2

    stem_types = {s["stem_type"] for s in r["stems"]}
    assert stem_types == {"vocal", "background"}

    stems = get_isolation_stems(project, isolation_id)
    for s in stems:
        pool_rel = s["pool_path"]
        assert pool_rel.startswith("pool/segments/")
        assert (project / pool_rel).exists()

    # pool_segments rows
    from scenecraft.db import get_pool_segment

    for s in stems:
        seg = get_pool_segment(project, s["pool_segment_id"])
        assert seg is not None
        assert seg["kind"] == "generated"
        assert seg["createdBy"] == "isolate-vocals"

    # job result payload
    assert job.result["isolation_id"] == isolation_id
    assert len(job.result["stems"]) == 2


# ── Residual arithmetic ────────────────────────────────────────────────────


def test_subtract_audio_wav_matches_source_minus_vocal(tmp_path):
    """If vocal=silence, residual should ~equal source."""
    import numpy as np

    from scenecraft.plugins.isolate_vocals.isolate_vocals import (
        _read_wav_s16_mono,
        _subtract_audio_wav,
    )

    sr = 48000
    n = 4800  # 0.1s
    rng = np.random.default_rng(1)
    src_pcm = rng.integers(-5000, 5000, size=n, dtype=np.int16).tobytes()
    voc_pcm = b"\x00\x00" * n  # silence
    src_path = tmp_path / "src.wav"
    voc_path = tmp_path / "voc.wav"
    out_path = tmp_path / "bg.wav"
    for p, pcm in ((src_path, src_pcm), (voc_path, voc_pcm)):
        with wave.open(str(p), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(sr)
            w.writeframes(pcm)

    _subtract_audio_wav(src_path, voc_path, out_path)

    out_pcm, out_sr, _ = _read_wav_s16_mono(out_path)
    assert out_sr == sr
    src_arr = np.frombuffer(src_pcm, dtype=np.int16)
    out_arr = np.frombuffer(out_pcm, dtype=np.int16)
    # source - silence ≈ source (small rounding at clip boundaries is fine; none here)
    assert (src_arr == out_arr).all()


# ── Error surfaces ─────────────────────────────────────────────────────────


def test_run_unknown_entity_type(project):
    from scenecraft.plugins.isolate_vocals import run

    r = run("keyframe", "kf_1", {"project_dir": project, "project_name": "x"})
    assert "error" in r


def test_run_transition_not_implemented(project):
    from scenecraft.plugins.isolate_vocals import run

    r = run(
        "transition",
        "tr_abc",
        {"project_dir": project, "project_name": "x", "range_mode": "full"},
    )
    assert "error" in r
    assert "not implemented" in r["error"].lower()


def test_run_missing_clip_returns_error(project):
    from scenecraft.plugins.isolate_vocals import run

    r = run(
        "audio_clip",
        "audio_clip_does_not_exist",
        {"project_dir": project, "project_name": "x", "range_mode": "full"},
    )
    assert "error" in r


def test_run_missing_source_file_fails_job(project, audio_clip, mock_dfn3):
    """If the source file vanishes between clip resolution and ffmpeg, run
    returns synchronously with an error (checked before kickoff)."""
    from scenecraft.db import get_audio_clips
    from scenecraft.plugins.isolate_vocals import run

    # Sanity-check the fixture works, then delete the WAV before run.
    clip = next(c for c in get_audio_clips(project) if c["id"] == audio_clip)
    src = project / clip["source_path"]
    src.unlink()

    r = run(
        "audio_clip",
        audio_clip,
        {"project_dir": project, "project_name": "x", "range_mode": "full"},
    )
    assert "error" in r


# ── REST wrapper ──────────────────────────────────────────────────────────


def test_handle_rest_missing_entity_id(project):
    from scenecraft.plugins.isolate_vocals import handle_rest

    r = handle_rest("/x", project, "x", {})
    assert "error" in r
    assert "entity_id" in r["error"]


def test_handle_rest_dispatches_to_run(project, audio_clip, mock_dfn3):
    from scenecraft.plugins.isolate_vocals import handle_rest

    r = handle_rest(
        "/api/projects/iso_project/plugins/isolate-vocals/run",
        project,
        "iso_project",
        {"entity_type": "audio_clip", "entity_id": audio_clip, "range_mode": "full"},
    )
    assert "error" not in r, r
    assert "isolation_id" in r and "job_id" in r
    _wait_for_job(r["job_id"])
