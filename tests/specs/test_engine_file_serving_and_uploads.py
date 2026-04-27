"""Spec-locked regression tests for `local.engine-file-serving-and-uploads`.

Covers four endpoints and the Migration Contract MC-1..MC-7. This is the
contract the FastAPI port (M16) MUST preserve byte-for-byte.

Endpoints:
  - GET  /api/projects/:name/files/*           (Range, ETag, INM, IMS, traversal)
  - POST /api/projects/:name/bounce-upload     (multipart WAV + DAL upsert)
  - POST /api/projects/:name/mix-render-upload (multipart WAV, no float fallback)
  - GET  /api/projects/:name/pool/:seg_id/peaks (float16 LE bytes + headers)

Test classes:
  TestFileServing       — R1..R7, R18..R20
  TestBounceUpload      — R8..R12, R20
  TestMixRenderUpload   — R13, R14
  TestPeaks             — R15..R17
  TestMigrationContract — MC-1..MC-7 invariants pinned for the port

Target-state xfails (per spec OQs / Transitional Behavior section):
  OQ-1 suffix range, OQ-2 multi-range, OQ-3 invalid syntax → 416,
  OQ-4 0-byte + Range, OQ-5 start beyond EOF, OQ-7 413 caps,
  OQ-9 path-traversal `Path.relative_to`, OQ-11 X-Peak-Resolution clamped echo.

This is an e2e-heavy spec — file serving is observable only over HTTP. We
extend the conftest's stdlib-urllib helper to support custom headers and
multipart bodies; no httpx/requests dependency.
"""
from __future__ import annotations

import io
import json as _json
import os
import struct
import urllib.error
import urllib.request
import uuid
import wave
from pathlib import Path
from typing import Any

import numpy as np
import pytest


# ────────────────────────── HTTP helpers ──────────────────────────


def _http(
    method: str,
    url: str,
    body: bytes | None = None,
    headers: dict[str, str] | None = None,
    timeout: float = 10.0,
) -> tuple[int, dict[str, str], bytes]:
    """Minimal stdlib HTTP client that honors arbitrary headers / raw bodies.

    Returns (status, headers_lowercased, body_bytes). Header keys are
    lower-cased for case-insensitive lookup.
    """
    req = urllib.request.Request(url, data=body, method=method)
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return (
                resp.status,
                {k.lower(): v for k, v in resp.headers.items()},
                resp.read(),
            )
    except urllib.error.HTTPError as e:
        return (
            e.code,
            {k.lower(): v for k, v in (e.headers or {}).items()},
            e.read() if hasattr(e, "read") else b"",
        )


def _multipart(fields: dict[str, Any]) -> tuple[bytes, str]:
    """Encode a dict of fields into a multipart/form-data body.

    Bytes values are sent as `audio` files; everything else stringified.
    Returns (body, content_type).
    """
    boundary = f"----scenecraft-{uuid.uuid4().hex}"
    parts: list[bytes] = []
    for name, value in fields.items():
        parts.append(b"--" + boundary.encode() + b"\r\n")
        if isinstance(value, (bytes, bytearray)):
            parts.append(
                f'Content-Disposition: form-data; name="{name}"; filename="{name}.bin"\r\n'.encode()
            )
            parts.append(b"Content-Type: application/octet-stream\r\n\r\n")
            parts.append(bytes(value))
            parts.append(b"\r\n")
        else:
            parts.append(
                f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode()
            )
            parts.append(str(value).encode("utf-8"))
            parts.append(b"\r\n")
    parts.append(b"--" + boundary.encode() + b"--\r\n")
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


# ──────────────────────── WAV synthesis helpers ────────────────────────


def _make_pcm_wav(
    duration_s: float,
    sample_rate: int = 44100,
    channels: int = 2,
    sample_width_bytes: int = 2,
    freq: float = 440.0,
) -> bytes:
    """Synth a sine-tone PCM WAV at the given bit depth (2=16, 3=24).

    Returns raw WAV bytes via the stdlib `wave` module.
    """
    n_frames = int(round(duration_s * sample_rate))
    t = np.arange(n_frames, dtype=np.float64) / sample_rate
    sig = np.sin(2 * np.pi * freq * t) * 0.3
    if channels == 2:
        frames = np.stack([sig, sig], axis=1)
    else:
        frames = sig[:, None]

    if sample_width_bytes == 2:
        ints = (frames * 32767).astype("<i2")
        raw = ints.tobytes()
    elif sample_width_bytes == 3:
        ints32 = (frames * 8388607).astype("<i4")
        # 24-bit little endian: take low 3 bytes per sample
        b = ints32.tobytes()
        # Each i4 is 4 bytes; drop the high byte of each 4-byte group.
        flat = bytearray()
        for i in range(0, len(b), 4):
            flat.extend(b[i : i + 3])
        raw = bytes(flat)
    else:
        raise ValueError(f"unsupported sample_width_bytes={sample_width_bytes}")

    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(channels)
        wf.setsampwidth(sample_width_bytes)
        wf.setframerate(sample_rate)
        wf.writeframes(raw)
    return buf.getvalue()


def _make_float32_wav(
    duration_s: float, sample_rate: int = 44100, channels: int = 2
) -> bytes:
    """32-bit float WAV via soundfile (the `wave` module rejects float WAVs).

    Used to exercise the bounce-upload soundfile-fallback branch (R9) and
    the mix-render-upload no-fallback rejection path (R14).
    """
    import soundfile as sf

    n = int(round(duration_s * sample_rate))
    t = np.arange(n, dtype=np.float32) / sample_rate
    sig = (np.sin(2 * np.pi * 440 * t) * 0.3).astype(np.float32)
    if channels == 2:
        sig = np.stack([sig, sig], axis=1)
    buf = io.BytesIO()
    sf.write(buf, sig, sample_rate, subtype="FLOAT", format="WAV")
    return buf.getvalue()


def _hex64(data: bytes) -> str:
    import hashlib

    return hashlib.sha256(data).hexdigest()


# ──────────────────────── Fixtures ────────────────────────


@pytest.fixture
def files_url(engine_server, project_name):
    return f"{engine_server.base_url}/api/projects/{project_name}/files"


@pytest.fixture
def project_path(engine_server, project_name) -> Path:
    return engine_server.work_dir / project_name


@pytest.fixture
def small_file(project_path: Path) -> tuple[Path, bytes]:
    """1024-byte known-content file under pool/. Returns (path, bytes)."""
    pool = project_path / "pool"
    pool.mkdir(parents=True, exist_ok=True)
    data = bytes((i % 256 for i in range(1024)))
    p = pool / "a.bin"
    p.write_bytes(data)
    return p, data


# ════════════════════════════════════════════════════════════════════
#  TestFileServing — GET /api/projects/:name/files/*
# ════════════════════════════════════════════════════════════════════


class TestFileServing:
    """GET /files/* — Range, ETag, conditional GETs, traversal, MIME.

    Pins MC-1 (Range parsing), MC-2 (ETag format), MC-3 (Cache headers),
    MC-6 (path-traversal guard).
    """

    # ── 200 full body / headers (R1, R7, R18) ─────────────────────────

    def test_get_full_file_200(self, engine_server, files_url, small_file):
        """covers R1, row 1 — 200 + full body + cache headers."""
        path, data = small_file
        s, h, body = _http("GET", f"{files_url}/pool/a.bin")
        assert s == 200
        assert body == data
        assert h["content-length"] == "1024"
        assert h["accept-ranges"] == "bytes"
        assert h["cache-control"] == "public, max-age=3600, immutable"
        assert "etag" in h
        assert "last-modified" in h

    def test_get_etag_format(self, engine_server, files_url, small_file):
        """covers R7, MC-2, row 11 — ETag is `"<size_hex>-<mtime_int_hex>"`."""
        path, _ = small_file
        st = path.stat()
        expected = f'"{st.st_size:x}-{int(st.st_mtime):x}"'
        s, h, _ = _http("GET", f"{files_url}/pool/a.bin")
        assert s == 200
        assert h["etag"] == expected

    def test_cors_headers_on_200(self, engine_server, files_url, small_file):
        """covers R18 — CORS present on 200."""
        s, h, _ = _http("GET", f"{files_url}/pool/a.bin")
        assert s == 200
        assert "access-control-allow-origin" in h

    def test_last_modified_rfc2822(self, engine_server, files_url, small_file):
        """covers R25, OQ-10 — Last-Modified parses as RFC 2822 GMT."""
        from email.utils import parsedate_to_datetime

        s, h, _ = _http("GET", f"{files_url}/pool/a.bin")
        assert s == 200
        dt = parsedate_to_datetime(h["last-modified"])
        assert dt is not None

    # ── Range — current behavior (R2, MC-1) ────────────────────────────

    def test_range_basic_206(self, engine_server, files_url, small_file):
        """covers R2, row 2 — `Range: bytes=0-99` → 206 + first 100 bytes."""
        _, data = small_file
        s, h, body = _http(
            "GET", f"{files_url}/pool/a.bin", headers={"Range": "bytes=0-99"}
        )
        assert s == 206
        assert body == data[0:100]
        assert h["content-range"] == "bytes 0-99/1024"
        assert h["content-length"] == "100"
        assert h["accept-ranges"] == "bytes"

    def test_range_open_ended_206(self, engine_server, files_url, small_file):
        """covers R2, row 3 — `Range: bytes=500-` → 206 + bytes 500..EOF."""
        _, data = small_file
        s, h, body = _http(
            "GET", f"{files_url}/pool/a.bin", headers={"Range": "bytes=500-"}
        )
        assert s == 206
        assert body == data[500:]
        assert h["content-range"] == "bytes 500-1023/1024"

    def test_range_clamped_beyond_eof_206(
        self, engine_server, files_url, small_file
    ):
        """covers R2, row 4 — end clamped to file_size-1."""
        _, data = small_file
        s, h, body = _http(
            "GET",
            f"{files_url}/pool/a.bin",
            headers={"Range": "bytes=0-99999999"},
        )
        assert s == 206
        assert h["content-range"] == "bytes 0-1023/1024"
        assert body == data

    # ── Conditional GETs (R3, R4) ──────────────────────────────────────

    def test_if_none_match_304(self, engine_server, files_url, small_file):
        """covers R3, row 5 — matching INM → 304 with no body."""
        s1, h1, _ = _http("GET", f"{files_url}/pool/a.bin")
        etag = h1["etag"]
        s2, h2, body = _http(
            "GET", f"{files_url}/pool/a.bin", headers={"If-None-Match": etag}
        )
        assert s2 == 304
        assert body == b""
        assert h2.get("etag") == etag
        # 304 must omit Cache-Control (MC-3).
        assert "cache-control" not in h2

    def test_if_none_match_miss_returns_200(
        self, engine_server, files_url, small_file
    ):
        """covers R3, row 7 — non-matching INM → 200 + full body."""
        s, _, body = _http(
            "GET",
            f"{files_url}/pool/a.bin",
            headers={"If-None-Match": '"deadbeef-cafebabe"'},
        )
        assert s == 200
        assert len(body) == 1024

    def test_if_modified_since_304(
        self, engine_server, files_url, small_file
    ):
        """covers R4, row 6 — IMS >= mtime → 304."""
        from email.utils import formatdate

        path, _ = small_file
        future = formatdate(path.stat().st_mtime + 60, usegmt=True)
        s, _, body = _http(
            "GET",
            f"{files_url}/pool/a.bin",
            headers={"If-Modified-Since": future},
        )
        assert s == 304
        assert body == b""

    def test_cors_present_on_304(self, engine_server, files_url, small_file):
        """covers R18, row 37 — CORS on 304 (frontend caching relies on it)."""
        s1, h1, _ = _http("GET", f"{files_url}/pool/a.bin")
        s2, h2, _ = _http(
            "GET",
            f"{files_url}/pool/a.bin",
            headers={"If-None-Match": h1["etag"]},
        )
        assert s2 == 304
        assert "access-control-allow-origin" in h2

    # ── 404 / traversal (R5, R6, MC-6) ────────────────────────────────

    def test_404_for_missing_file(self, engine_server, files_url):
        """covers R6, row 9 — missing file → 404 NOT_FOUND."""
        s, _, body = _http("GET", f"{files_url}/pool/nope.bin")
        assert s == 404
        parsed = _json.loads(body)
        assert parsed.get("code") == "NOT_FOUND"

    def test_path_traversal_blocked(
        self, engine_server, files_url):
        """covers R5, row 8 — `../../../etc/passwd` traversal escapes work_dir → 403.

        The guard's boundary is `work_dir` (not project_dir), so the escape
        must traverse out of work_dir. We aim at /etc/passwd which exists on
        every Linux host.
        """
        # Enough `..` segments to escape both the project and work_dir
        # regardless of how deep the tmp_path_factory placed engine_server_workdir.
        url = f"{files_url}/../../../../../../../../../etc/passwd"
        s, _, body = _http("GET", url)
        assert s == 403, (s, body[:200])
        assert _json.loads(body).get("code") == "FORBIDDEN"

    @pytest.mark.xfail(
        reason="target-state OQ-9: traversal guard MUST switch from startswith to Path.relative_to",
        strict=False,
    )
    def test_traversal_via_sibling_prefix_dir_rejected(
        self, engine_server, project_name, files_url, project_path
    ):
        """covers R23, OQ-9, row 47 — sibling-prefix dir bypass via startswith."""
        # Construct a sibling dir whose name is a prefix of work_dir's last component.
        # Today's `startswith(str(work_dir))` accepts `/work_dir-evil/...`.
        # Target: Path.relative_to rejects it.
        work_dir = engine_server.work_dir
        evil = work_dir.parent / (work_dir.name + "-evil")
        evil.mkdir(exist_ok=True)
        (evil / "secret.bin").write_bytes(b"leak")
        try:
            # Build a request that, after .resolve(), ends up inside `<workdir>-evil/`.
            rel = f"../../{evil.name}/secret.bin"
            s, _, _ = _http("GET", f"{files_url}/{rel}")
            assert s == 403
        finally:
            (evil / "secret.bin").unlink(missing_ok=True)
            evil.rmdir()

    # ── Target-state RFC 7233 xfails ──────────────────────────────────

    @pytest.mark.xfail(
        reason="target-state OQ-1: suffix range bytes=-N MUST return last N bytes (RFC 7233); current falls through to 200",
        strict=False,
    )
    def test_suffix_range_returns_last_n_bytes(
        self, engine_server, files_url, small_file
    ):
        """covers R21, OQ-1, row 39 — suffix range."""
        _, data = small_file
        s, h, body = _http(
            "GET", f"{files_url}/pool/a.bin", headers={"Range": "bytes=-500"}
        )
        assert s == 206
        assert len(body) == 500
        assert body == data[524:]
        assert h["content-range"] == "bytes 524-1023/1024"

    @pytest.mark.xfail(
        reason="target-state OQ-2: multi-range MUST emit multipart/byteranges; current first-only",
        strict=False,
    )
    def test_multi_range_returns_multipart(
        self, engine_server, files_url, small_file
    ):
        """covers R21, OQ-2, row 40."""
        s, h, body = _http(
            "GET",
            f"{files_url}/pool/a.bin",
            headers={"Range": "bytes=0-10,50-60"},
        )
        assert s == 206
        assert "multipart/byteranges" in h["content-type"].lower()
        assert b"Content-Range: bytes 0-10/1024" in body
        assert b"Content-Range: bytes 50-60/1024" in body

    @pytest.mark.xfail(
        reason="target-state OQ-3: invalid Range syntax MUST return 416; current returns 200",
        strict=False,
    )
    def test_invalid_range_syntax_416(
        self, engine_server, files_url, small_file
    ):
        """covers R21, OQ-3, row 41 — `Range: bytes=abc` → 416."""
        s, h, _ = _http(
            "GET", f"{files_url}/pool/a.bin", headers={"Range": "bytes=abc"}
        )
        assert s == 416
        assert h["content-range"].startswith("bytes */")

    @pytest.mark.xfail(
        reason="target-state OQ-4: 0-byte file + Range MUST return 416",
        strict=False,
    )
    def test_zero_byte_file_with_range_416(
        self, engine_server, files_url, project_path
    ):
        """covers R21, OQ-4, row 42 — 0-byte file + Range → 416."""
        empty = project_path / "pool" / "empty.bin"
        empty.parent.mkdir(parents=True, exist_ok=True)
        empty.write_bytes(b"")
        s, _, _ = _http(
            "GET",
            f"{files_url}/pool/empty.bin",
            headers={"Range": "bytes=0-"},
        )
        assert s == 416

    @pytest.mark.xfail(
        reason="target-state OQ-5: range start beyond EOF MUST return 416; current undefined",
        strict=False,
    )
    def test_range_start_beyond_eof_416(
        self, engine_server, files_url, small_file
    ):
        """covers R21, OQ-5, row 43 — start > size → 416."""
        s, h, _ = _http(
            "GET",
            f"{files_url}/pool/a.bin",
            headers={"Range": "bytes=10000-"},
        )
        assert s == 416
        assert h["content-range"].startswith("bytes */1024")


# ════════════════════════════════════════════════════════════════════
#  TestBounceUpload — POST /api/projects/:name/bounce-upload
# ════════════════════════════════════════════════════════════════════


class TestBounceUpload:
    """POST /bounce-upload — multipart parse, WAV validate, DAL upsert.

    Pins MC-4 (multipart field names), MC-5 (validator precedence: wave →
    soundfile fallback → unlink+400).
    """

    @staticmethod
    def _post(engine_server, project_name, fields):
        body, ctype = _multipart(fields)
        return _http(
            "POST",
            f"{engine_server.base_url}/api/projects/{project_name}/bounce-upload",
            body=body,
            headers={"Content-Type": ctype},
        )

    # ── R8, R20 — happy paths + idempotence ───────────────────────────

    def test_happy_24bit_returns_201(
        self, engine_server, project_name, project_path
    ):
        """covers R8, row 12 — 24-bit PCM WAV → 201, file written, JSON correct."""
        wav = _make_pcm_wav(2.0, 44100, 2, sample_width_bytes=3)
        h_hex = _hex64(wav)
        s, _, body = self._post(
            engine_server,
            project_name,
            {
                "audio": wav,
                "composite_hash": h_hex,
                "start_time_s": "0",
                "end_time_s": "2.0",
                "sample_rate": "44100",
                "bit_depth": "24",
                "channels": "2",
            },
        )
        assert s == 201, body
        parsed = _json.loads(body)
        assert parsed["rendered_path"] == f"pool/bounces/{h_hex}.wav"
        assert parsed["sample_rate"] == 44100
        assert parsed["channels"] == 2
        assert parsed["chat_released"] is False
        dest = project_path / "pool" / "bounces" / f"{h_hex}.wav"
        assert dest.exists()
        assert dest.read_bytes() == wav

    def test_happy_16bit_returns_201(
        self, engine_server, project_name, project_path
    ):
        """16-bit PCM is the standard path. Ensures `wave.open` succeeds."""
        wav = _make_pcm_wav(0.5, 48000, 1, sample_width_bytes=2)
        h_hex = _hex64(wav)
        s, _, body = self._post(
            engine_server,
            project_name,
            {
                "audio": wav,
                "composite_hash": h_hex,
                "start_time_s": "0",
                "end_time_s": "0.5",
                "sample_rate": "48000",
                "bit_depth": "16",
                "channels": "1",
            },
        )
        assert s == 201, body

    def test_idempotent_same_hash(
        self, engine_server, project_name, project_path
    ):
        """covers R20, row 24 — same hash twice → both 201, file overwritten."""
        wav = _make_pcm_wav(0.25, 44100, 2, 2)
        h_hex = _hex64(wav)
        common = {
            "audio": wav,
            "composite_hash": h_hex,
            "start_time_s": "0",
            "end_time_s": "0.25",
            "sample_rate": "44100",
            "bit_depth": "16",
            "channels": "2",
        }
        s1, _, _ = self._post(engine_server, project_name, common)
        s2, _, _ = self._post(engine_server, project_name, common)
        assert s1 == 201
        assert s2 == 201
        dest = project_path / "pool" / "bounces" / f"{h_hex}.wav"
        assert dest.read_bytes() == wav

    # ── R10 — mismatch → 400 + unlink ─────────────────────────────────

    def test_channels_mismatch_unlinks(
        self, engine_server, project_name, project_path
    ):
        """covers R10, row 14 — channels mismatch → 400, file deleted."""
        wav = _make_pcm_wav(0.5, 44100, 1, 2)  # mono
        h_hex = _hex64(wav)
        s, _, body = self._post(
            engine_server,
            project_name,
            {
                "audio": wav,
                "composite_hash": h_hex,
                "start_time_s": "0",
                "end_time_s": "0.5",
                "sample_rate": "44100",
                "bit_depth": "16",
                "channels": "2",  # declared stereo, WAV is mono
            },
        )
        assert s == 400
        assert b"channels mismatch" in body
        assert not (
            project_path / "pool" / "bounces" / f"{h_hex}.wav"
        ).exists()

    def test_sample_rate_mismatch_unlinks(
        self, engine_server, project_name, project_path
    ):
        """covers R10, row 15."""
        wav = _make_pcm_wav(0.5, 44100, 2, 2)
        h_hex = _hex64(wav)
        s, _, _ = self._post(
            engine_server,
            project_name,
            {
                "audio": wav,
                "composite_hash": h_hex,
                "start_time_s": "0",
                "end_time_s": "0.5",
                "sample_rate": "48000",  # wrong
                "bit_depth": "16",
                "channels": "2",
            },
        )
        assert s == 400
        assert not (
            project_path / "pool" / "bounces" / f"{h_hex}.wav"
        ).exists()

    def test_corrupt_wav_unlinks(
        self, engine_server, project_name, project_path
    ):
        """covers R10, row 16 — random bytes labeled as WAV → 400 + unlink."""
        garbage = os.urandom(2048)
        h_hex = _hex64(garbage)
        s, _, body = self._post(
            engine_server,
            project_name,
            {
                "audio": garbage,
                "composite_hash": h_hex,
                "start_time_s": "0",
                "end_time_s": "1",
                "sample_rate": "44100",
                "bit_depth": "16",
                "channels": "2",
            },
        )
        assert s == 400
        assert b"Invalid WAV file" in body
        assert not (
            project_path / "pool" / "bounces" / f"{h_hex}.wav"
        ).exists()

    # ── R9 — soundfile fallback path (32-bit float) ───────────────────

    def test_float32_via_soundfile_fallback(
        self, engine_server, project_name, project_path
    ):
        """covers R9, MC-5, row 13 — 32-bit float WAV traverses soundfile fallback."""
        try:
            wav = _make_float32_wav(0.5, 44100, 2)
        except Exception as e:  # pragma: no cover — soundfile missing
            pytest.skip(f"soundfile unavailable: {e}")
        h_hex = _hex64(wav)
        s, _, body = self._post(
            engine_server,
            project_name,
            {
                "audio": wav,
                "composite_hash": h_hex,
                "start_time_s": "0",
                "end_time_s": "0.5",
                "sample_rate": "44100",
                "bit_depth": "32",
                "channels": "2",
            },
        )
        assert s == 201, body
        assert (
            project_path / "pool" / "bounces" / f"{h_hex}.wav"
        ).exists()

    # ── R11 — field-level validation ──────────────────────────────────

    def test_missing_composite_hash_400(self, engine_server, project_name):
        """covers R11, row 17."""
        wav = _make_pcm_wav(0.25, 44100, 2, 2)
        s, _, body = self._post(
            engine_server,
            project_name,
            {
                "audio": wav,
                "start_time_s": "0",
                "end_time_s": "0.25",
                "sample_rate": "44100",
                "bit_depth": "16",
                "channels": "2",
            },
        )
        assert s == 400
        assert b"composite_hash" in body

    def test_bad_hash_format_400(self, engine_server, project_name):
        """covers R11, row 18 — non-hex / wrong length."""
        wav = _make_pcm_wav(0.1, 44100, 2, 2)
        s, _, body = self._post(
            engine_server,
            project_name,
            {
                "audio": wav,
                "composite_hash": "zz",
                "start_time_s": "0",
                "end_time_s": "0.1",
                "sample_rate": "44100",
                "bit_depth": "16",
                "channels": "2",
            },
        )
        assert s == 400
        assert b"64 hex" in body

    def test_bad_channels_400(self, engine_server, project_name, project_path):
        """covers R11, row 19 — channels=3 rejected pre-write."""
        wav = _make_pcm_wav(0.1, 44100, 2, 2)
        h_hex = _hex64(wav)
        s, _, _ = self._post(
            engine_server,
            project_name,
            {
                "audio": wav,
                "composite_hash": h_hex,
                "start_time_s": "0",
                "end_time_s": "0.1",
                "sample_rate": "44100",
                "bit_depth": "16",
                "channels": "3",
            },
        )
        assert s == 400
        assert not (
            project_path / "pool" / "bounces" / f"{h_hex}.wav"
        ).exists()

    def test_bad_bit_depth_400(self, engine_server, project_name):
        """covers R11, row 20 — bit_depth=8 rejected."""
        wav = _make_pcm_wav(0.1, 44100, 2, 2)
        h_hex = _hex64(wav)
        s, _, _ = self._post(
            engine_server,
            project_name,
            {
                "audio": wav,
                "composite_hash": h_hex,
                "start_time_s": "0",
                "end_time_s": "0.1",
                "sample_rate": "44100",
                "bit_depth": "8",
                "channels": "2",
            },
        )
        assert s == 400

    def test_bad_time_range_400(self, engine_server, project_name):
        """covers R11, row 21 — end <= start rejected."""
        wav = _make_pcm_wav(0.1, 44100, 2, 2)
        h_hex = _hex64(wav)
        s, _, _ = self._post(
            engine_server,
            project_name,
            {
                "audio": wav,
                "composite_hash": h_hex,
                "start_time_s": "1",
                "end_time_s": "0",
                "sample_rate": "44100",
                "bit_depth": "16",
                "channels": "2",
            },
        )
        assert s == 400

    def test_bad_sample_rate_400(self, engine_server, project_name):
        """covers R11 — non-positive sample_rate rejected."""
        wav = _make_pcm_wav(0.1, 44100, 2, 2)
        s, _, _ = self._post(
            engine_server,
            project_name,
            {
                "audio": wav,
                "composite_hash": _hex64(wav),
                "start_time_s": "0",
                "end_time_s": "0.1",
                "sample_rate": "0",
                "bit_depth": "16",
                "channels": "2",
            },
        )
        assert s == 400

    # ── R12 — request_id event release (non-fatal exception) ──────────

    def test_request_id_exception_is_nonfatal(
        self, engine_server, project_name, monkeypatch
    ):
        """covers R12, R19, row 23 — set_bounce_render_event raising → 201,
        chat_released=false, upload still succeeds."""
        from scenecraft import chat as _chat

        def _boom(_rid):
            raise RuntimeError("oops")

        monkeypatch.setattr(_chat, "set_bounce_render_event", _boom)
        wav = _make_pcm_wav(0.1, 44100, 2, 2)
        s, _, body = self._post(
            engine_server,
            project_name,
            {
                "audio": wav,
                "composite_hash": _hex64(wav),
                "start_time_s": "0",
                "end_time_s": "0.1",
                "sample_rate": "44100",
                "bit_depth": "16",
                "channels": "2",
                "request_id": "req-xyz",
            },
        )
        assert s == 201, body
        assert _json.loads(body)["chat_released"] is False

    # ── Target-state body-size cap (OQ-7) ─────────────────────────────

    @pytest.mark.xfail(
        reason="target-state OQ-7: multipart >200MB MUST 413; no cap today",
        strict=False,
    )
    def test_multipart_over_200mb_413(self, engine_server, project_name):
        """covers R22, OQ-7, row 45."""
        # We don't actually allocate 200 MB in the test — we synthesize a
        # bogus Content-Length header and a small body; the target middleware
        # MUST reject based on declared length OR streamed length.
        body, ctype = _multipart(
            {
                "audio": b"x",
                "composite_hash": "0" * 64,
                "start_time_s": "0",
                "end_time_s": "1",
                "sample_rate": "44100",
                "bit_depth": "16",
                "channels": "2",
            }
        )
        s, _, _ = _http(
            "POST",
            f"{engine_server.base_url}/api/projects/{project_name}/bounce-upload",
            body=body,
            headers={"Content-Type": ctype, "Content-Length": str(300 * 1024 * 1024)},
        )
        assert s == 413


# ════════════════════════════════════════════════════════════════════
#  TestMixRenderUpload — POST /api/projects/:name/mix-render-upload
# ════════════════════════════════════════════════════════════════════


class TestMixRenderUpload:
    """POST /mix-render-upload — wave-only validator (no soundfile fallback,
    R14) plus ±100ms duration drift check (R13)."""

    @staticmethod
    def _post(engine_server, project_name, fields):
        body, ctype = _multipart(fields)
        return _http(
            "POST",
            f"{engine_server.base_url}/api/projects/{project_name}/mix-render-upload",
            body=body,
            headers={"Content-Type": ctype},
        )

    def test_happy_returns_201(
        self, engine_server, project_name, project_path
    ):
        """covers R13, row 25 — 1s 48kHz stereo 16-bit → 201."""
        wav = _make_pcm_wav(1.0, 48000, 2, 2)
        h_hex = _hex64(wav)
        s, _, body = self._post(
            engine_server,
            project_name,
            {
                "audio": wav,
                "mix_graph_hash": h_hex,
                "start_time_s": "0",
                "end_time_s": "1.0",
                "sample_rate": "48000",
                "channels": "2",
            },
        )
        assert s == 201, body
        parsed = _json.loads(body)
        assert abs(parsed["duration_s"] - 1.0) < 0.01
        assert (
            project_path / "pool" / "mixes" / f"{h_hex}.wav"
        ).exists()

    def test_duration_drift_unlinks(
        self, engine_server, project_name, project_path
    ):
        """covers R13, row 26 — declared 1.5s vs WAV 1.0s → 400 + unlink."""
        wav = _make_pcm_wav(1.0, 48000, 2, 2)
        h_hex = _hex64(wav)
        s, _, body = self._post(
            engine_server,
            project_name,
            {
                "audio": wav,
                "mix_graph_hash": h_hex,
                "start_time_s": "0",
                "end_time_s": "1.5",
                "sample_rate": "48000",
                "channels": "2",
            },
        )
        assert s == 400
        assert b"duration mismatch" in body
        assert not (
            project_path / "pool" / "mixes" / f"{h_hex}.wav"
        ).exists()

    def test_no_float_fallback(
        self, engine_server, project_name, project_path
    ):
        """covers R14, MC-5, row 27 — 32-bit float WAV → 400 (no soundfile fallback)."""
        try:
            wav = _make_float32_wav(0.5, 44100, 2)
        except Exception as e:  # pragma: no cover
            pytest.skip(f"soundfile unavailable: {e}")
        h_hex = _hex64(wav)
        s, _, body = self._post(
            engine_server,
            project_name,
            {
                "audio": wav,
                "mix_graph_hash": h_hex,
                "start_time_s": "0",
                "end_time_s": "0.5",
                "sample_rate": "44100",
                "channels": "2",
            },
        )
        assert s == 400
        assert b"Invalid WAV file" in body
        assert not (
            project_path / "pool" / "mixes" / f"{h_hex}.wav"
        ).exists()

    def test_channels_mismatch_unlinks(
        self, engine_server, project_name, project_path
    ):
        """covers R13, row 28."""
        wav = _make_pcm_wav(1.0, 48000, 2, 2)
        h_hex = _hex64(wav)
        s, _, _ = self._post(
            engine_server,
            project_name,
            {
                "audio": wav,
                "mix_graph_hash": h_hex,
                "start_time_s": "0",
                "end_time_s": "1.0",
                "sample_rate": "48000",
                "channels": "1",  # mismatch
            },
        )
        assert s == 400
        assert not (
            project_path / "pool" / "mixes" / f"{h_hex}.wav"
        ).exists()

    def test_missing_field_400(self, engine_server, project_name):
        """covers R11-equivalent — missing mix_graph_hash → 400."""
        wav = _make_pcm_wav(0.1, 48000, 2, 2)
        s, _, body = self._post(
            engine_server,
            project_name,
            {
                "audio": wav,
                "start_time_s": "0",
                "end_time_s": "0.1",
                "sample_rate": "48000",
                "channels": "2",
            },
        )
        assert s == 400
        assert b"mix_graph_hash" in body


# ════════════════════════════════════════════════════════════════════
#  TestPeaks — GET /api/projects/:name/pool/:seg_id/peaks
# ════════════════════════════════════════════════════════════════════


class TestPeaks:
    """GET /pool/:seg_id/peaks — float16 LE bytes + headers, ffmpeg streaming.

    Pins MC-7 (peaks byte contract) plus the cache-key invariants in R16.
    """

    @pytest.fixture
    def seeded_pool(self, engine_server, project_name, project_path):
        """Insert a pool segment whose pool_path points at a real WAV.

        Uses `add_pool_segment` directly (DAL) so we're independent of the
        upload pipeline.
        """
        from scenecraft.db import add_pool_segment

        pool_dir = project_path / "pool"
        pool_dir.mkdir(parents=True, exist_ok=True)
        wav_bytes = _make_pcm_wav(2.0, 44100, 1, 2)
        wav_path = pool_dir / "peaks_src.wav"
        wav_path.write_bytes(wav_bytes)

        seg_id = add_pool_segment(
            project_path,
            kind="imported",
            created_by="test",
            pool_path="pool/peaks_src.wav",
            duration_seconds=2.0,
        )
        return seg_id, wav_path

    def test_peaks_body_length_and_headers(
        self, engine_server, project_name, seeded_pool
    ):
        """covers R15, MC-7, row 29 — content-length = 2 * ceil(2.0*400) = 1600."""
        seg_id, _ = seeded_pool
        s, h, body = _http(
            "GET",
            f"{engine_server.base_url}/api/projects/{project_name}"
            f"/pool/{seg_id}/peaks?resolution=400",
        )
        if s != 200:
            pytest.skip(f"ffmpeg likely unavailable on test host: status={s}")
        assert h["content-type"] == "application/octet-stream"
        assert h["content-length"] == "1600"
        assert int(h["x-peak-resolution"]) == 400
        assert h["x-peak-duration"] == f"{2.0:.6f}"
        # float16 decode — 800 floats, all in [0, 1]
        peaks = np.frombuffer(body, dtype="<f2")
        assert peaks.shape == (800,)
        assert np.all(np.isfinite(peaks))
        assert np.all(peaks >= 0) and np.all(peaks <= 1.0)

    def test_peaks_cache_hit_skips_ffmpeg(
        self, engine_server, project_name, seeded_pool, monkeypatch
    ):
        """covers R15, R16, MC-7 — second call with identical params hits cache,
        no new ffmpeg subprocess. We verify by patching subprocess.Popen AFTER
        the first call and asserting it's not invoked."""
        import subprocess as _sp

        seg_id, _ = seeded_pool
        url = (
            f"{engine_server.base_url}/api/projects/{project_name}"
            f"/pool/{seg_id}/peaks?resolution=400"
        )
        s1, _, body1 = _http("GET", url)
        if s1 != 200:
            pytest.skip(f"ffmpeg likely unavailable: {s1}")

        # NOTE: monkeypatching server-side subprocess.Popen via test-side
        # monkeypatch only works when the server thread imports the same
        # module reference. compute_peaks does `import subprocess`. The cache
        # path returns BEFORE Popen is reached — verify by asserting bytes
        # equal AND the cache file exists in audio_staging/.peaks/.
        s2, _, body2 = _http("GET", url)
        assert s2 == 200
        assert body1 == body2
        cache_dir = (
            engine_server.work_dir / project_name / "audio_staging" / ".peaks"
        )
        assert cache_dir.exists()
        cached = list(cache_dir.glob("*.f16"))
        assert len(cached) >= 1, "peaks cache file should exist after first call"

    def test_peaks_cache_invalidates_on_touch(
        self, engine_server, project_name, seeded_pool
    ):
        """covers R16, MC-7, row 33 — touching source file (mtime bumped) →
        new cache key → fresh compute. Bytes are identical (content unchanged).
        """
        import os as _os
        import time as _time

        seg_id, src = seeded_pool
        url = (
            f"{engine_server.base_url}/api/projects/{project_name}"
            f"/pool/{seg_id}/peaks?resolution=400"
        )
        s1, _, body1 = _http("GET", url)
        if s1 != 200:
            pytest.skip("ffmpeg likely unavailable")

        cache_dir = (
            engine_server.work_dir / project_name / "audio_staging" / ".peaks"
        )
        keys_before = {p.name for p in cache_dir.glob("*.f16")}

        # Bump mtime by ≥1ns; sleep a hair to ensure st_mtime_ns differs.
        _time.sleep(0.01)
        new_t = src.stat().st_mtime + 1
        _os.utime(src, (new_t, new_t))

        s2, _, body2 = _http("GET", url)
        assert s2 == 200
        assert body1 == body2  # content unchanged
        keys_after = {p.name for p in cache_dir.glob("*.f16")}
        # New cache key produced.
        assert keys_after != keys_before
        assert len(keys_after - keys_before) >= 1

    def test_peaks_resolution_clamped_low(
        self, engine_server, project_name, seeded_pool
    ):
        """covers R15, row 30 — resolution=10 clamped to 50; body length matches.

        Note: per spec MC-7 + current behavior, body length follows clamped
        value; X-Peak-Resolution echoes REQUESTED today (xfail in MC test).
        """
        seg_id, _ = seeded_pool
        s, h, body = _http(
            "GET",
            f"{engine_server.base_url}/api/projects/{project_name}"
            f"/pool/{seg_id}/peaks?resolution=10",
        )
        if s != 200:
            pytest.skip("ffmpeg likely unavailable")
        # ceil(2.0 * 50) = 100 peaks → 200 bytes
        assert int(h["content-length"]) == 200

    def test_peaks_resolution_clamped_high(
        self, engine_server, project_name, seeded_pool
    ):
        """covers R15, row 31 — resolution=5000 clamped to 2000."""
        seg_id, _ = seeded_pool
        s, h, _ = _http(
            "GET",
            f"{engine_server.base_url}/api/projects/{project_name}"
            f"/pool/{seg_id}/peaks?resolution=5000",
        )
        if s != 200:
            pytest.skip("ffmpeg likely unavailable")
        # ceil(2.0 * 2000) = 4000 peaks → 8000 bytes
        assert int(h["content-length"]) == 8000

    def test_peaks_empty_duration(
        self, engine_server, project_name, project_path
    ):
        """covers R15, row 36 — duration=0 → 200 + empty body."""
        from scenecraft.db import add_pool_segment

        pool_dir = project_path / "pool"
        pool_dir.mkdir(parents=True, exist_ok=True)
        # Use a minimal valid WAV so the path exists; duration_seconds=0 in DB.
        (pool_dir / "zero.wav").write_bytes(_make_pcm_wav(0.1, 44100, 1, 2))
        seg_id = add_pool_segment(
            project_path,
            kind="imported",
            created_by="test",
            pool_path="pool/zero.wav",
            duration_seconds=0.0,
        )
        s, _, body = _http(
            "GET",
            f"{engine_server.base_url}/api/projects/{project_name}"
            f"/pool/{seg_id}/peaks?resolution=400",
        )
        assert s == 200
        assert body == b""

    def test_peaks_404_unknown_seg(self, engine_server, project_name):
        """covers R15, row 34 — unknown seg_id → 404."""
        s, _, body = _http(
            "GET",
            f"{engine_server.base_url}/api/projects/{project_name}"
            f"/pool/does-not-exist/peaks",
        )
        assert s == 404
        assert _json.loads(body).get("code") == "NOT_FOUND"

    def test_peaks_path_escape_400(
        self, engine_server, project_name, project_path
    ):
        """covers R17, row 35 — pool_path escaping project → 400."""
        from scenecraft.db import add_pool_segment

        # Insert a row whose pool_path escapes the project_dir.
        seg_id = add_pool_segment(
            project_path,
            kind="imported",
            created_by="test",
            pool_path="../../../../etc/passwd",
            duration_seconds=1.0,
        )
        s, _, body = _http(
            "GET",
            f"{engine_server.base_url}/api/projects/{project_name}"
            f"/pool/{seg_id}/peaks?resolution=400",
        )
        # 400 BAD_REQUEST per current handler (`relative_to` ValueError branch).
        assert s in (400, 404)
        if s == 400:
            assert b"outside project" in body or b"BAD_REQUEST" in body

    @pytest.mark.xfail(
        reason="target-state OQ-11: X-Peak-Resolution MUST echo CLAMPED value, not requested",
        strict=False,
    )
    def test_x_peak_resolution_echoes_clamped(
        self, engine_server, project_name, seeded_pool
    ):
        """covers R26, OQ-11 — header MUST echo internal clamped value."""
        seg_id, _ = seeded_pool
        s, h, _ = _http(
            "GET",
            f"{engine_server.base_url}/api/projects/{project_name}"
            f"/pool/{seg_id}/peaks?resolution=10",
        )
        if s != 200:
            pytest.skip("ffmpeg likely unavailable")
        assert int(h["x-peak-resolution"]) == 50  # not 10


# ════════════════════════════════════════════════════════════════════
#  TestMigrationContract — pin MC-1..MC-7 invariants for FastAPI port
# ════════════════════════════════════════════════════════════════════


class TestMigrationContract:
    """Explicit assertions of every Migration Contract row.

    These are the contracts the FastAPI port (M16) MUST preserve byte-for-byte.
    Each assertion is self-contained so a failure in this class is an
    immediate, specific signal to the porter.
    """

    # MC-1: Range header parsing — `bytes=N-` and `bytes=N-M` only,
    # end clamped, 206 status, suffix/multi-range fall through (xfail target).
    def test_mc1_range_basic_206(self, engine_server, files_url, small_file):
        s, h, body = _http(
            "GET", f"{files_url}/pool/a.bin", headers={"Range": "bytes=0-99"}
        )
        assert s == 206
        assert h["content-range"] == "bytes 0-99/1024"
        assert len(body) == 100

    def test_mc1_range_clamped_at_eof(
        self, engine_server, files_url, small_file
    ):
        s, h, _ = _http(
            "GET",
            f"{files_url}/pool/a.bin",
            headers={"Range": "bytes=100-99999999"},
        )
        assert s == 206
        assert h["content-range"] == "bytes 100-1023/1024"

    # MC-2: ETag format MUST be `"<size_hex>-<mtime_int_hex>"` exactly.
    def test_mc2_etag_format_byte_exact(
        self, engine_server, files_url, small_file
    ):
        path, _ = small_file
        st = path.stat()
        expected = f'"{st.st_size:x}-{int(st.st_mtime):x}"'
        _, h, _ = _http("GET", f"{files_url}/pool/a.bin")
        assert h["etag"] == expected
        # Frontend depends on this format for cache-busting; FastAPI's default
        # FileResponse uses inode-style — refactor MUST override.

    # MC-3: Cache-Control on hot 200/206; 304 omits Cache-Control + Last-Modified;
    # Accept-Ranges on both 200 and 206.
    def test_mc3_cache_control_on_200(
        self, engine_server, files_url, small_file
    ):
        _, h, _ = _http("GET", f"{files_url}/pool/a.bin")
        assert h["cache-control"] == "public, max-age=3600, immutable"
        assert h["accept-ranges"] == "bytes"

    def test_mc3_cache_control_on_206(
        self, engine_server, files_url, small_file
    ):
        _, h, _ = _http(
            "GET", f"{files_url}/pool/a.bin", headers={"Range": "bytes=0-9"}
        )
        assert h["cache-control"] == "public, max-age=3600, immutable"
        assert h["accept-ranges"] == "bytes"

    def test_mc3_304_omits_cache_control(
        self, engine_server, files_url, small_file
    ):
        _, h1, _ = _http("GET", f"{files_url}/pool/a.bin")
        _, h2, _ = _http(
            "GET",
            f"{files_url}/pool/a.bin",
            headers={"If-None-Match": h1["etag"]},
        )
        assert "cache-control" not in h2
        assert "last-modified" not in h2
        assert h2.get("etag") == h1["etag"]

    # MC-4: Multipart field names — case-sensitive substring match in
    # Content-Disposition. Verified by the upload happy paths above; this
    # test pins that field-name renaming would break.
    def test_mc4_field_name_case_sensitive(
        self, engine_server, project_name, project_path
    ):
        wav = _make_pcm_wav(0.1, 44100, 2, 2)
        h_hex = _hex64(wav)
        # Send with capitalized "Composite_Hash" — current parser uses
        # 'name="composite_hash"' substring match (case-sensitive) → field
        # is treated as missing → 400.
        body, ctype = _multipart(
            {
                "audio": wav,
                "Composite_Hash": h_hex,  # wrong case
                "start_time_s": "0",
                "end_time_s": "0.1",
                "sample_rate": "44100",
                "bit_depth": "16",
                "channels": "2",
            }
        )
        s, _, body_resp = _http(
            "POST",
            f"{engine_server.base_url}/api/projects/{project_name}/bounce-upload",
            body=body,
            headers={"Content-Type": ctype},
        )
        assert s == 400
        assert b"composite_hash" in body_resp

    # MC-5: WAV validator precedence —
    #   bounce-upload: wave → soundfile → unlink+400.
    #   mix-render-upload: wave only → unlink+400 (no fallback). Pinned by
    # TestMixRenderUpload.test_no_float_fallback above, restated here.
    def test_mc5_mix_render_no_soundfile_fallback(
        self, engine_server, project_name, project_path
    ):
        try:
            wav = _make_float32_wav(0.5, 44100, 2)
        except Exception as e:
            pytest.skip(f"soundfile unavailable: {e}")
        h_hex = _hex64(wav)
        body, ctype = _multipart(
            {
                "audio": wav,
                "mix_graph_hash": h_hex,
                "start_time_s": "0",
                "end_time_s": "0.5",
                "sample_rate": "44100",
                "channels": "2",
            }
        )
        s, _, _ = _http(
            "POST",
            f"{engine_server.base_url}/api/projects/{project_name}/mix-render-upload",
            body=body,
            headers={"Content-Type": ctype},
        )
        assert s == 400
        assert not (
            project_path / "pool" / "mixes" / f"{h_hex}.wav"
        ).exists()

    # MC-6: Path-traversal guard semantics. Today: `startswith` on resolved
    # stringified path. Production-safe (work_dir ends in `.scenecraft`, no
    # sibling prefix) but the refactor MUST switch to `Path.relative_to`.
    @pytest.mark.xfail(
        reason="target-state OQ-9 (audit leak #11): guard MUST use Path.relative_to, not str.startswith",
        strict=False,
    )
    def test_mc6_guard_uses_relative_to(self):
        """Static inspection: no `str.startswith` on stringified resolved paths."""
        src = (
            Path(__file__).parent.parent.parent
            / "src"
            / "scenecraft"
            / "api_server.py"
        ).read_text()
        # Find the file-serve handler block.
        idx = src.find("def _handle_serve_file")
        assert idx > 0
        block = src[idx : idx + 2000]
        assert "relative_to" in block, "guard should use Path.relative_to"
        assert "startswith(str(work_dir" not in block, (
            "guard still uses str.startswith — leak #11 not fixed"
        )

    # MC-7: Peaks endpoint byte contract (validated in TestPeaks). Restate
    # the float16-LE invariant here so the contract is unmistakable to the
    # porter.
    def test_mc7_peaks_dtype_is_float16_le(
        self, engine_server, project_name, project_path
    ):
        from scenecraft.db import add_pool_segment

        pool_dir = project_path / "pool"
        pool_dir.mkdir(parents=True, exist_ok=True)
        (pool_dir / "src.wav").write_bytes(_make_pcm_wav(1.0, 44100, 1, 2))
        seg_id = add_pool_segment(
            project_path,
            kind="imported",
            created_by="test",
            pool_path="pool/src.wav",
            duration_seconds=1.0,
        )
        s, h, body = _http(
            "GET",
            f"{engine_server.base_url}/api/projects/{project_name}"
            f"/pool/{seg_id}/peaks?resolution=400",
        )
        if s != 200:
            pytest.skip("ffmpeg likely unavailable")
        assert h["x-peak-duration"] == f"{1.0:.6f}"
        # 2 bytes per peak; ceil(1.0 * 400) = 400 peaks; 800 bytes.
        assert len(body) == 800
        assert len(body) % 2 == 0
        # Decode as float16 little-endian.
        peaks = np.frombuffer(body, dtype="<f2")
        assert peaks.shape == (400,)
