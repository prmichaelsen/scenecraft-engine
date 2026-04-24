"""Transcribe plugin — unit tests.

Covers:
 - WhisperClient per-model output normalisation (all 4 Replicate models)
 - transcribe_clip cache hit / miss
 - Plugin registers the namespaced tool and operation at activate() time
 - Plugin settings round-trip through meta + merge with defaults
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Any

import pytest


# ── helpers ─────────────────────────────────────────────────────────────


def _make_project(tmp_path: Path) -> Path:
    """Initialise a project.db in a fresh dir. Adds one audio_track + one
    audio_clip pointing at a dummy file so transcribe_clip can resolve
    the source path."""
    from scenecraft.db import get_db, add_audio_clip

    project_dir = tmp_path / "p"
    project_dir.mkdir()
    audio_path = project_dir / "fake.m4a"
    audio_path.write_bytes(b"\x00" * 64)  # enough for os.path.exists + open()

    get_db(project_dir)  # ensures schema + triggers
    conn = get_db(project_dir)
    conn.execute(
        "INSERT INTO audio_tracks (id, name, display_order, hidden, muted, solo, volume_curve) "
        "VALUES ('t1', 'T1', 0, 0, 0, 0, '[[0,0],[1,0]]')",
    )
    conn.commit()
    add_audio_clip(project_dir, {
        "id": "c1",
        "track_id": "t1",
        "source_path": "fake.m4a",
        "start_time": 0.0,
        "end_time": 5.0,
        "source_offset": 0.0,
        "muted": False,
    })
    return project_dir


# ── WhisperClient normalisation ─────────────────────────────────────────


def test_parse_fast_whisper_output():
    from scenecraft.ai.whisper_client import _parse_output_fast

    raw = {
        "text": "hello world",
        "chunks": [
            {"timestamp": [0.0, 1.5], "text": "hello"},
            {"timestamp": [1.5, 3.0], "text": "world"},
        ],
    }
    n = _parse_output_fast(raw)
    assert n.model == "fast"
    assert n.text == "hello world"
    assert len(n.segments) == 2
    assert n.segments[0].start == 0.0 and n.segments[0].end == 1.5
    assert n.segments[1].text == "world"
    assert n.duration_seconds == 3.0


def test_parse_whisperx_output_with_words():
    from scenecraft.ai.whisper_client import _parse_output_whisperx

    raw = {
        "detected_language": "en",
        "segments": [
            {
                "start": 0.0, "end": 2.0, "text": "hi there",
                "words": [
                    {"word": "hi", "start": 0.0, "end": 0.3, "score": 0.98},
                    {"word": "there", "start": 0.4, "end": 1.9, "score": 0.95},
                ],
            },
        ],
    }
    n = _parse_output_whisperx(raw)
    assert n.model == "whisperx"
    assert n.language == "en"
    assert n.text == "hi there"
    assert len(n.segments) == 1
    seg = n.segments[0]
    assert len(seg.words) == 2
    assert seg.words[0].text == "hi"
    assert seg.words[0].score == pytest.approx(0.98)


def test_parse_whisper_classic_output():
    from scenecraft.ai.whisper_client import _parse_output_whisper

    raw = {
        "detected_language": "es",
        "transcription": "hola mundo",
        "segments": [
            {"start": 0.0, "end": 1.2, "text": "hola"},
            {"start": 1.2, "end": 2.5, "text": "mundo"},
        ],
    }
    n = _parse_output_whisper(raw)
    assert n.model == "whisper"
    assert n.language == "es"
    assert n.text == "hola mundo"
    assert n.duration_seconds == 2.5


def test_parse_whisper_timestamped_output():
    from scenecraft.ai.whisper_client import _parse_output_whisper_timestamped

    raw = {
        "text": "well",
        "segments": [
            {
                "start": 0.0, "end": 0.5, "text": "well",
                "words": [{"text": "well", "start": 0.0, "end": 0.5, "confidence": 0.9}],
            },
        ],
    }
    n = _parse_output_whisper_timestamped(raw)
    assert n.model == "whisper-timestamped"
    assert n.segments[0].words[0].score == pytest.approx(0.9)


def test_resolve_model_unknown():
    from scenecraft.ai.whisper_client import resolve_model

    with pytest.raises(ValueError, match="unknown whisper model"):
        resolve_model("bogus")


def test_model_choices_includes_all_four():
    from scenecraft.ai.whisper_client import model_choices

    choices = model_choices()
    assert set(choices) == {"fast", "whisperx", "whisper", "whisper-timestamped"}


# ── transcribe_clip + cache ─────────────────────────────────────────────


class _StubClient:
    """Stand-in for WhisperClient — counts calls + returns a preset result."""

    def __init__(self, result):
        self.result = result
        self.calls = 0

    def transcribe(self, *args, **kwargs):
        self.calls += 1
        # Model in result comes from the first positional config lookup in
        # the real client; stub just returns whatever was passed in the
        # fixture.
        return self.result


def _stub_transcript(model: str):
    from scenecraft.ai.whisper_client import (
        MODELS,
        NormalizedTranscript,
        TranscriptSegment,
    )
    return NormalizedTranscript(
        text="stub transcript",
        segments=[
            TranscriptSegment(start=0.0, end=1.0, text="stub"),
            TranscriptSegment(start=1.0, end=2.0, text="transcript"),
        ],
        language="en",
        model=model,
        model_slug=MODELS[model]["slug"],
        duration_seconds=2.0,
        raw_output={"text": "stub transcript"},
    )


def test_transcribe_clip_cache_miss_then_hit(tmp_path, monkeypatch):
    project_dir = _make_project(tmp_path)
    stub = _StubClient(_stub_transcript("fast"))

    from scenecraft.ai import transcriber

    monkeypatch.setattr(transcriber, "WhisperClient", lambda: stub)

    # First call: miss → one client call, persists a run.
    r1 = transcriber.transcribe_clip(project_dir, "c1", model="fast")
    assert r1.cached is False
    assert stub.calls == 1
    assert r1.text == "stub transcript"
    assert len(r1.segments) == 2

    # Second call: identical args → cache hit → NO additional client call.
    r2 = transcriber.transcribe_clip(project_dir, "c1", model="fast")
    assert r2.cached is True
    assert stub.calls == 1
    assert r2.run_id == r1.run_id
    assert r2.text == r1.text

    # Force rerun bypasses the cache.
    r3 = transcriber.transcribe_clip(project_dir, "c1", model="fast", force_rerun=True)
    assert r3.cached is False
    assert stub.calls == 2


def test_transcribe_clip_model_variant_separates_cache(tmp_path, monkeypatch):
    project_dir = _make_project(tmp_path)
    from scenecraft.ai import transcriber

    # Return a transcript whose .model matches whatever the caller asked
    # for by looking at the stub state. We swap in a fresh stub per call.
    call_log: list[str] = []

    class ModelAwareStub:
        def transcribe(self, audio_path, *, model, language=None, word_timestamps=False, **_):
            call_log.append(model)
            return _stub_transcript(model)

    monkeypatch.setattr(transcriber, "WhisperClient", ModelAwareStub)

    r_fast = transcriber.transcribe_clip(project_dir, "c1", model="fast")
    r_wx = transcriber.transcribe_clip(project_dir, "c1", model="whisperx")

    assert r_fast.model == "fast"
    assert r_wx.model == "whisperx"
    assert r_fast.run_id != r_wx.run_id
    assert call_log == ["fast", "whisperx"]

    # Re-asking for 'fast' hits the fast-model cache specifically.
    r_fast_again = transcriber.transcribe_clip(project_dir, "c1", model="fast")
    assert r_fast_again.cached is True
    assert r_fast_again.run_id == r_fast.run_id
    assert call_log == ["fast", "whisperx"]  # no extra calls


def test_word_timestamps_flag_separates_cache(tmp_path, monkeypatch):
    project_dir = _make_project(tmp_path)
    from scenecraft.ai import transcriber

    class Stub:
        def transcribe(self, audio_path, *, model, language=None, word_timestamps=False, **_):
            return _stub_transcript("fast")

    monkeypatch.setattr(transcriber, "WhisperClient", Stub)

    r_no = transcriber.transcribe_clip(project_dir, "c1", model="fast", word_timestamps=False)
    r_yes = transcriber.transcribe_clip(project_dir, "c1", model="fast", word_timestamps=True)
    assert r_no.run_id != r_yes.run_id


def test_transcribe_clip_unknown_clip_raises(tmp_path, monkeypatch):
    project_dir = _make_project(tmp_path)
    from scenecraft.ai import transcriber

    monkeypatch.setattr(transcriber, "WhisperClient", lambda: _StubClient(_stub_transcript("fast")))
    with pytest.raises(ValueError, match="id not found in audio_clips or pool_segments"):
        transcriber.transcribe_clip(project_dir, "missing_clip")


def test_transcribe_falls_back_to_pool_segment(tmp_path, monkeypatch):
    """A pool_segment id without a matching audio_clip should still resolve."""
    from scenecraft.ai import transcriber
    from scenecraft.db import get_db

    project_dir = _make_project(tmp_path)
    # Plant a pool_segments row pointing at a disk file
    pool_audio = project_dir / "pool" / "segments" / "seg1.wav"
    pool_audio.parent.mkdir(parents=True)
    pool_audio.write_bytes(b"\x00" * 64)
    conn = get_db(project_dir)
    conn.execute(
        "INSERT INTO pool_segments (id, pool_path, kind, created_at) "
        "VALUES (?, ?, 'imported', '2026-04-24T00:00:00Z')",
        ("seg_xyz", "pool/segments/seg1.wav"),
    )
    conn.commit()

    monkeypatch.setattr(transcriber, "WhisperClient", lambda: _StubClient(_stub_transcript("fast")))
    r = transcriber.transcribe_clip(project_dir, "seg_xyz", model="fast")
    assert r.cached is False
    assert r.text == "stub transcript"


def test_unknown_model_override_raises(tmp_path, monkeypatch):
    project_dir = _make_project(tmp_path)
    from scenecraft.ai import transcriber

    monkeypatch.setattr(transcriber, "WhisperClient", lambda: _StubClient(_stub_transcript("fast")))
    with pytest.raises(ValueError, match="unknown whisper model"):
        transcriber.transcribe_clip(project_dir, "c1", model="bogus")


# ── Plugin-settings round-trip ──────────────────────────────────────────


def test_plugin_settings_round_trip(tmp_path):
    project_dir = _make_project(tmp_path)
    from scenecraft.ai import transcriber

    # Defaults merge cleanly when nothing is stored.
    s = transcriber.get_plugin_settings(project_dir)
    assert s["default_model"] == "fast"
    assert s["default_language"] == ""
    assert s["default_word_timestamps"] is False

    # Persist + read back.
    transcriber.set_plugin_setting(project_dir, "default_model", "whisperx")
    transcriber.set_plugin_setting(project_dir, "default_word_timestamps", True)
    s2 = transcriber.get_plugin_settings(project_dir)
    assert s2["default_model"] == "whisperx"
    assert s2["default_word_timestamps"] is True

    # Unknown setting rejected.
    with pytest.raises(ValueError):
        transcriber.set_plugin_setting(project_dir, "not_a_real_setting", "x")


def test_plugin_settings_resolve_into_transcribe_clip(tmp_path, monkeypatch):
    project_dir = _make_project(tmp_path)
    from scenecraft.ai import transcriber

    # Configure default_model = whisperx at plugin-setting layer.
    transcriber.set_plugin_setting(project_dir, "default_model", "whisperx")

    captured: list[str] = []

    class Stub:
        def transcribe(self, audio_path, *, model, language=None, word_timestamps=False, **_):
            captured.append(model)
            return _stub_transcript(model)

    monkeypatch.setattr(transcriber, "WhisperClient", Stub)

    # No explicit model override → plugin default kicks in.
    r = transcriber.transcribe_clip(project_dir, "c1")
    assert captured == ["whisperx"]
    assert r.model == "whisperx"


# ── Plugin host registration ────────────────────────────────────────────


def test_plugin_registers_namespaced_tools():
    from scenecraft.plugin_host import PluginHost
    from scenecraft.plugins import transcribe as transcribe_plugin

    # Deactivate first so test is idempotent across runs in the same proc.
    PluginHost.deactivate(transcribe_plugin.__name__)
    PluginHost.register(transcribe_plugin)

    run_tool = PluginHost.get_mcp_tool("transcribe__transcribe_clip")
    assert run_tool is not None
    assert run_tool.plugin == "transcribe"
    assert run_tool.destructive is False
    enum = run_tool.input_schema["properties"]["model"].get("enum")
    assert set(enum) == {"fast", "whisperx", "whisper", "whisper-timestamped"}
    assert run_tool.input_schema["required"] == ["clip_id"]

    list_tool = PluginHost.get_mcp_tool("transcribe__list_transcriptions")
    assert list_tool is not None
    assert list_tool.destructive is False

    get_tool = PluginHost.get_mcp_tool("transcribe__get_transcription")
    assert get_tool is not None
    assert get_tool.input_schema["required"] == ["run_id"]

    op = PluginHost.get_operation("transcribe.run")
    assert op is not None
    assert "audio_clip" in op.entity_types


def test_list_and_get_runs(tmp_path, monkeypatch):
    """Full chat-tool surface: transcribe_clip creates a run, list
    returns its summary, get returns the full segments."""
    from scenecraft.ai import transcriber
    from scenecraft.plugins.transcribe.handlers import (
        handle_get_transcription,
        handle_list_transcriptions,
        handle_transcribe_clip,
    )

    project_dir = _make_project(tmp_path)
    monkeypatch.setattr(transcriber, "WhisperClient", lambda: _StubClient(_stub_transcript("fast")))

    # Create a run through the chat tool.
    ctx = {"project_dir": project_dir, "project_name": "p", "ws": None, "tool_use_id": None}
    run_result = handle_transcribe_clip({"clip_id": "c1"}, ctx)
    run_id = run_result["run_id"]
    assert "error" not in run_result
    assert run_result["cached"] is False

    # List: should include the run.
    listed = handle_list_transcriptions({}, ctx)
    assert "error" not in listed
    assert listed["count"] >= 1
    matching = [r for r in listed["runs"] if r["run_id"] == run_id]
    assert len(matching) == 1
    assert matching[0]["model"] == "fast"
    assert matching[0]["segment_count"] == 2  # _stub_transcript has 2 segments

    # List with clip_id filter.
    filtered = handle_list_transcriptions({"clip_id": "c1"}, ctx)
    assert filtered["count"] >= 1
    assert all(r["clip_id"] == "c1" for r in filtered["runs"])

    # List with a clip id that doesn't exist.
    none_match = handle_list_transcriptions({"clip_id": "nope"}, ctx)
    assert none_match["count"] == 0

    # Get the full run back.
    fetched = handle_get_transcription({"run_id": run_id}, ctx)
    assert "error" not in fetched
    assert fetched["run_id"] == run_id
    assert fetched["text"] == "stub transcript"
    assert len(fetched["segments"]) == 2
    assert fetched["segments"][0]["start"] == 0.0
    assert fetched["segments"][0]["end"] == 1.0

    # Bad run id.
    missing = handle_get_transcription({"run_id": "tr_does_not_exist"}, ctx)
    assert missing.get("error", "").startswith("transcribe run not found")

    # Missing run_id field.
    no_id = handle_get_transcription({}, ctx)
    assert no_id.get("error") == "run_id is required and must be a string"
