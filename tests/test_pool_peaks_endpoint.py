"""Integration test for GET /api/projects/:name/pool/:seg_id/peaks.

Exercises the real compute_peaks codepath (via ffmpeg subprocess) on a short
synthetic WAV so the endpoint wiring — route match, db lookup, file resolution,
peaks shape, response headers, error paths — is covered end-to-end.
"""

from __future__ import annotations

import io
import json
import wave
from http.server import BaseHTTPRequestHandler
from pathlib import Path

import pytest


@pytest.fixture
def project(tmp_path):
    from scenecraft.db import get_db

    d = tmp_path / "peaks_project"
    d.mkdir()
    get_db(d)
    (d / "pool" / "segments").mkdir(parents=True, exist_ok=True)
    return d


@pytest.fixture
def pool_seg_with_wav(project):
    """Insert a pool_segments row and write a tiny WAV at its pool_path."""
    from scenecraft.db import add_pool_segment

    # Write a short, silent-ish 0.25s 48k mono WAV.
    sr = 48000
    n = sr // 4
    pcm = (b"\x00\x01" * n)
    pool_dir = project / "pool" / "segments"
    # Pre-generate id so file name matches what the endpoint resolves.
    seg_id = add_pool_segment(
        project,
        kind="generated",
        created_by="test",
        pool_path="",  # patched below
        duration_seconds=n / sr,
        byte_size=len(pcm),
    )
    # Rewrite pool_path to the real file + write the file at that path.
    pool_rel = f"pool/segments/{seg_id}.wav"
    from scenecraft.db import get_db
    conn = get_db(project)
    conn.execute("UPDATE pool_segments SET pool_path = ? WHERE id = ?", (pool_rel, seg_id))
    conn.commit()
    wav_path = project / pool_rel
    with wave.open(str(wav_path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(pcm)
    return seg_id, wav_path, n / sr


# ── _handle_pool_peaks via a minimal fake handler harness ────────────────


class _FakeHeaders:
    def __init__(self, data=None):
        self._data = data or {}

    def get(self, k, default=None):
        return self._data.get(k, default)


class _FakeHandler:
    """Minimal shim implementing just what _handle_pool_peaks needs."""

    def __init__(self, project_dir, work_dir):
        self._project_dir = project_dir
        self._work_dir = work_dir
        self.status = None
        self.headers_out = {}
        self.body = b""
        self._response_sent = False
        self._refreshed_cookie = None
        self.headers = _FakeHeaders({"Origin": None})
        self.wfile = io.BytesIO()

    # ----- response helpers the endpoint calls -----
    def send_response(self, status):
        self.status = status
        self._response_sent = True

    def send_header(self, k, v):
        self.headers_out[k] = v

    def end_headers(self):
        pass

    # ----- private helpers the endpoint depends on -----
    def _require_project_dir(self, name):
        return self._project_dir

    def _cors_headers(self):
        pass

    def _error(self, status, code, message):
        self.status = status
        self.body = json.dumps({"error": message, "code": code}).encode()


def _invoke_endpoint(project, seg_id, query="resolution=200"):
    """Execute the real _handle_pool_peaks through a FakeHandler."""
    import scenecraft.api_server as api_mod

    # Pull the method off the class via make_handler without starting a server.
    handler_cls = api_mod.make_handler(project.parent, no_auth=True)
    # _handle_pool_peaks is a method, so we need `self` — reuse a FakeHandler.
    method = handler_cls._handle_pool_peaks

    fh = _FakeHandler(project_dir=project, work_dir=project.parent)
    method(fh, project.name, seg_id, query)
    return fh


def test_pool_peaks_happy_path(project, pool_seg_with_wav):
    seg_id, _, duration = pool_seg_with_wav
    fh = _invoke_endpoint(project, seg_id, query="resolution=200")
    assert fh.status == 200
    assert fh.headers_out["Content-Type"] == "application/octet-stream"
    assert fh.headers_out["X-Peak-Resolution"] == "200"
    assert abs(float(fh.headers_out["X-Peak-Duration"]) - duration) < 1e-3


def test_pool_peaks_404_unknown_segment(project):
    fh = _invoke_endpoint(project, "does_not_exist", query="resolution=200")
    assert fh.status == 404


def test_pool_peaks_404_missing_file(project, pool_seg_with_wav):
    seg_id, wav_path, _ = pool_seg_with_wav
    wav_path.unlink()
    fh = _invoke_endpoint(project, seg_id, query="resolution=200")
    assert fh.status == 404


def test_pool_peaks_defaults_to_resolution_400(project, pool_seg_with_wav):
    seg_id, _, _ = pool_seg_with_wav
    fh = _invoke_endpoint(project, seg_id, query="")
    assert fh.status == 200
    assert fh.headers_out["X-Peak-Resolution"] == "400"
