"""Integration tests for generate-music plugin backed by a mock Musicful server.

Covers the spec's Base Cases for the run handler + polling worker. Auth-gate
tests (R54a-f) are deferred per the 2026-04-23 dev directive (skip-auth).
"""

from __future__ import annotations

import http.server
import io
import json
import os
import socket
import tempfile
import threading
import time
import urllib.parse
from pathlib import Path

import pytest


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class MockMusicfulState:
    """Shared state for the mock server across a test."""

    def __init__(self):
        self.tasks: dict[str, dict] = {}
        self.generate_responses: list[list[str]] = []   # queue of task-id lists to return
        self.generate_call_count = 0
        self.tasks_call_count = 0
        self.tasks_429_remaining = 0                     # force N consecutive 429s on /tasks
        self.key_info = {"key_music_counts": 237, "email": "test@example.com"}
        self.audio_blob = b"FAKE_MP3_PAYLOAD"


def _make_handler(state: MockMusicfulState):
    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *args, **kwargs):  # silence test output
            pass

        def _send(self, status: int, body):
            self.send_response(status)
            if isinstance(body, (dict, list)):
                raw = json.dumps(body).encode("utf-8")
                self.send_header("content-type", "application/json")
            elif isinstance(body, bytes):
                raw = body
                self.send_header("content-type", "audio/mpeg")
            else:
                raw = str(body).encode("utf-8")
                self.send_header("content-type", "text/plain")
            self.send_header("content-length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_GET(self):
            parsed = urllib.parse.urlparse(self.path)
            if parsed.path == "/v1/music/tasks":
                state.tasks_call_count += 1
                if state.tasks_429_remaining > 0:
                    state.tasks_429_remaining -= 1
                    return self._send(429, {"error": "rate limited"})
                qs = urllib.parse.parse_qs(parsed.query)
                ids = qs.get("ids", [""])[0].split(",") if qs.get("ids") else []
                out = [state.tasks[i] for i in ids if i in state.tasks]
                return self._send(200, out)
            if parsed.path == "/v1/get_api_key_info":
                return self._send(200, state.key_info)
            if parsed.path == "/fake_audio.mp3":
                return self._send(200, state.audio_blob)
            return self._send(404, {"error": f"unknown path {parsed.path}"})

        def do_POST(self):
            parsed = urllib.parse.urlparse(self.path)
            length = int(self.headers.get("content-length", 0))
            raw = self.rfile.read(length) if length else b""
            try:
                body = json.loads(raw) if raw else {}
            except Exception:
                body = {}
            if parsed.path == "/v1/music/generate":
                state.generate_call_count += 1
                task_ids = state.generate_responses.pop(0) if state.generate_responses else ["t1", "t2"]
                return self._send(200, {"task_ids": task_ids, "received": body})
            return self._send(404, {"error": f"unknown path {parsed.path}"})

    return Handler


@pytest.fixture
def mock_musicful(monkeypatch):
    """Spin up a mock Musicful server and point the service registry at it."""
    state = MockMusicfulState()
    port = _free_port()
    handler = _make_handler(state)
    server = http.server.ThreadingHTTPServer(("127.0.0.1", port), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    # Redirect the plugin_api service registry to the mock.
    import scenecraft.plugin_api as pa
    monkeypatch.setitem(
        pa.SERVICE_REGISTRY, "musicful",
        (f"http://127.0.0.1:{port}", "MUSICFUL_API_KEY", "x-api-key"),
    )
    monkeypatch.setenv("MUSICFUL_API_KEY", "test-key-1234")

    # Speed up polling for tests.
    from scenecraft.plugins.generate_music import generate_music as gm
    monkeypatch.setattr(gm, "POLL_INTERVAL_SECONDS", 0.05)

    yield state, port

    server.shutdown()
    thread.join(timeout=2)


@pytest.fixture
def project_dir(tmp_path):
    """Provide a project_dir with the schema initialized."""
    from scenecraft.db import get_db
    pd = tmp_path / "project"
    pd.mkdir()
    # Touch the DB to trigger schema init.
    get_db(pd)
    return pd


@pytest.fixture
def server_root(tmp_path, monkeypatch):
    """Set SCENECRAFT_ROOT so plugin_api.find_root() resolves to a test dir."""
    root = tmp_path / ".scenecraft"
    root.mkdir()
    monkeypatch.setenv("SCENECRAFT_ROOT", str(root))
    # Touch server.db to ensure schema is created.
    from scenecraft.vcs.bootstrap import get_server_db
    get_server_db(root)
    return root


def _audio_url(port: int) -> str:
    return f"http://127.0.0.1:{port}/fake_audio.mp3"


def _wait_terminal(project_dir: Path, generation_id: str, timeout: float = 5.0) -> dict:
    """Block until the generation reaches a terminal status."""
    from scenecraft import plugin_api
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        last = plugin_api.get_music_generation(project_dir, generation_id)
        if last and last["status"] in ("completed", "failed"):
            return last
        time.sleep(0.05)
    raise TimeoutError(f"generation {generation_id} did not terminate; last={last}")


def test_generates_music_auto_no_context_happy_path(mock_musicful, project_dir, server_root):
    state, port = mock_musicful
    state.generate_responses.append(["t1", "t2"])
    state.tasks["t1"] = {
        "id": "t1", "title": "Neon Midnight", "style": "dark",
        "duration": 167, "audio_url": _audio_url(port),
        "cover_url": "http://x/c1.jpg", "status": 3,
    }
    state.tasks["t2"] = {
        "id": "t2", "title": "Neon Midnight v2", "style": "dark",
        "duration": 172, "audio_url": _audio_url(port),
        "cover_url": "http://x/c2.jpg", "status": 3,
    }

    from scenecraft.plugins.generate_music.generate_music import run
    result = run(
        project_dir, "p1",
        action="auto", style="dark cinematic synth",
        instrumental=1, model="MFV2.0",
    )
    assert "error" not in result, result
    assert result["task_ids"] == ["t1", "t2"]
    gen_id = result["generation_id"]

    # Musicful payload filtered per R13
    assert state.generate_call_count == 1

    terminal = _wait_terminal(project_dir, gen_id)
    assert terminal["status"] == "completed"
    assert terminal["error"] is None or terminal["error"] == ""

    # Two pool_segments + two tracks
    from scenecraft import plugin_api
    tracks = plugin_api.get_music_generation_tracks(project_dir, gen_id)
    assert len(tracks) == 2

    # spend_ledger: exactly one row, unit='credit', amount=2
    from scenecraft.vcs.bootstrap import list_spend
    rows = list_spend(server_root, plugin_id="generate-music")
    assert len(rows) == 1
    assert rows[0]["amount"] == 2
    assert rows[0]["unit"] == "credit"
    assert rows[0]["job_ref"] == gen_id


def test_rejects_unsupported_action(mock_musicful, project_dir, server_root):
    state, port = mock_musicful
    from scenecraft.plugins.generate_music.generate_music import run
    result = run(project_dir, "p1", action="extend", style="x")
    assert "error" in result
    assert "not supported" in result["error"]
    # No Musicful call, no ledger row
    assert state.generate_call_count == 0
    from scenecraft.vcs.bootstrap import list_spend
    assert list_spend(server_root) == []


def test_rate_limit_retry_succeeds(mock_musicful, project_dir, server_root):
    state, port = mock_musicful
    state.generate_responses.append(["t1"])
    state.tasks["t1"] = {
        "id": "t1", "title": "R", "style": "x", "duration": 30,
        "audio_url": _audio_url(port), "status": 3,
    }
    state.tasks_429_remaining = 2  # first two poll calls are 429

    from scenecraft.plugins.generate_music.generate_music import run
    result = run(project_dir, "p1", action="auto", style="ambient", instrumental=1)
    gen_id = result["generation_id"]
    terminal = _wait_terminal(project_dir, gen_id, timeout=10.0)
    assert terminal["status"] == "completed", terminal


def test_rate_limit_exhausts_retries_fails(mock_musicful, project_dir, server_root):
    state, port = mock_musicful
    state.generate_responses.append(["t1"])
    state.tasks["t1"] = {"id": "t1", "status": 0, "duration": 0, "audio_url": None}
    state.tasks_429_remaining = 999  # always 429

    from scenecraft.plugins.generate_music.generate_music import run
    result = run(project_dir, "p1", action="auto", style="noise", instrumental=1)
    gen_id = result["generation_id"]
    terminal = _wait_terminal(project_dir, gen_id, timeout=15.0)
    assert terminal["status"] == "failed"
    assert terminal["error"] == "rate_limit_exceeded"
    # No credit ledger row for failures
    from scenecraft.vcs.bootstrap import list_spend
    assert list_spend(server_root) == []


def test_partial_success_one_of_two(mock_musicful, project_dir, server_root):
    state, port = mock_musicful
    state.generate_responses.append(["ok", "fail"])
    state.tasks["ok"] = {
        "id": "ok", "title": "OK", "style": "x", "duration": 60,
        "audio_url": _audio_url(port), "status": 3,
    }
    state.tasks["fail"] = {
        "id": "fail", "status": 4, "duration": 0, "audio_url": None,
        "fail_code": 500, "fail_reason": "model_overloaded",
    }

    from scenecraft.plugins.generate_music.generate_music import run
    result = run(project_dir, "p1", action="auto", style="lofi", instrumental=1)
    gen_id = result["generation_id"]
    terminal = _wait_terminal(project_dir, gen_id)
    assert terminal["status"] == "completed"           # per R20
    assert terminal["error"] and "model_overloaded" in terminal["error"]

    from scenecraft import plugin_api
    tracks = plugin_api.get_music_generation_tracks(project_dir, gen_id)
    assert len(tracks) == 1                             # only the successful one

    from scenecraft.vcs.bootstrap import list_spend
    rows = list_spend(server_root)
    assert len(rows) == 1
    assert rows[0]["amount"] == 1                       # credits only for the success


def test_generates_with_audio_clip_context_writes_candidate(mock_musicful, project_dir, server_root):
    # Create an audio_clip row to anchor the candidate junction.
    import sqlite3
    from scenecraft.db import get_db, _now_iso
    conn = get_db(project_dir)
    # Minimal audio_track + audio_clip — columns vary by M9 schema version;
    # only insert id/name which are stable.
    conn.execute(
        "INSERT INTO audio_tracks (id, name) VALUES (?, ?)",
        ("at-1", "Music"),
    )
    conn.execute(
        """INSERT INTO audio_clips (id, track_id, source_path, start_time, end_time)
           VALUES (?, ?, ?, ?, ?)""",
        ("ac-7", "at-1", "segments/source.mp3", 0.0, 10.0),
    )
    conn.commit()

    state, port = mock_musicful
    state.generate_responses.append(["t1"])
    state.tasks["t1"] = {
        "id": "t1", "title": "C", "style": "x", "duration": 30,
        "audio_url": _audio_url(port), "status": 3,
    }

    from scenecraft.plugins.generate_music.generate_music import run
    result = run(
        project_dir, "p1",
        action="auto", style="cinematic", instrumental=1,
        entity_type="audio_clip", entity_id="ac-7",
    )
    gen_id = result["generation_id"]
    terminal = _wait_terminal(project_dir, gen_id)
    assert terminal["status"] == "completed"

    # audio_candidates junction written
    ac_rows = conn.execute(
        "SELECT * FROM audio_candidates WHERE audio_clip_id = ?",
        ("ac-7",),
    ).fetchall()
    assert len(ac_rows) == 1
    # No tr_candidates written
    tr_rows = conn.execute("SELECT * FROM tr_candidates").fetchall()
    assert len(tr_rows) == 0


def test_filter_fields_by_action_auto_excludes_lyrics(mock_musicful, project_dir, server_root):
    state, port = mock_musicful
    state.generate_responses.append(["t1"])
    state.tasks["t1"] = {
        "id": "t1", "title": "A", "style": "x", "duration": 5,
        "audio_url": _audio_url(port), "status": 3,
    }

    from scenecraft.plugins.generate_music.generate_music import run, _build_payload
    payload = _build_payload(
        action="auto", style="style",
        model="MFV2.0", instrumental=1,
        lyrics="should be ignored",
        title="should be ignored",
        gender="male",
    )
    assert "lyrics" not in payload
    assert "title" not in payload
    assert payload["gender"] == "male"
    assert payload["action"] == "auto"


def test_filter_custom_with_instrumental_drops_lyrics(mock_musicful, project_dir, server_root):
    from scenecraft.plugins.generate_music.generate_music import _build_payload
    payload = _build_payload(
        action="custom", style="x", model="MFV2.0", instrumental=1,
        lyrics="fa la la",
    )
    assert "lyrics" not in payload


def test_filter_custom_includes_lyrics_when_not_instrumental(mock_musicful, project_dir, server_root):
    from scenecraft.plugins.generate_music.generate_music import _build_payload
    payload = _build_payload(
        action="custom", style="x", model="MFV2.0", instrumental=0,
        lyrics="fa la la", title="Song",
    )
    assert payload["lyrics"] == "fa la la"
    assert payload["title"] == "Song"


def test_missing_api_key_returns_admin_error(monkeypatch, project_dir, server_root):
    monkeypatch.delenv("MUSICFUL_API_KEY", raising=False)
    from scenecraft.plugins.generate_music.generate_music import run
    result = run(project_dir, "p1", action="auto", style="x", instrumental=1)
    assert "error" in result
    assert "administrator" in result["error"]


def test_empty_style_rejected(mock_musicful, project_dir, server_root):
    from scenecraft.plugins.generate_music.generate_music import run
    result = run(project_dir, "p1", action="auto", style="", instrumental=1)
    assert "error" in result
    assert "style is required" in result["error"]


def test_style_over_limit_rejected(mock_musicful, project_dir, server_root):
    from scenecraft.plugins.generate_music.generate_music import run
    huge = "x" * 5001
    result = run(project_dir, "p1", action="auto", style=huge, instrumental=1)
    assert "error" in result
    assert "5000" in result["error"]
