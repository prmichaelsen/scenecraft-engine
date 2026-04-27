# Spec: Engine File Serving + Uploads (Refactor Regression Test)

> **🤖 Agent Directive**: This spec codifies the observable black-box behavior of the engine's file-serving and WAV-upload HTTP endpoints as they exist today (BaseHTTPRequestHandler). It exists to prevent regression during the FastAPI migration. Every `####` test below MUST continue to pass against the refactored FastAPI implementation, executed by the same HTTP client against the same URL shapes. Any divergence is a refactor bug, not an expected improvement.

**Namespace**: local
**Version**: 1.0.0
**Created**: 2026-04-27
**Last Updated**: 2026-04-27
**Status**: Active
**Compatibility**: scenecraft-engine FastAPI migration (see `local.fastapi-migration.md`)

---

## Purpose

Freeze the observable HTTP contract of four engine endpoints so the api_server.py → FastAPI rewrite cannot silently regress Range-request semantics, ETag/If-Modified-Since cache behavior, multipart WAV validation precedence, or waveform cache keying.

## Source

- Mode: `--from-draft` (spec task description, refactor-regression-test flavor)
- Origin code:
  - `src/scenecraft/api_server.py` L8990–9093 — `_handle_serve_file` (GET file serving + Range)
  - `src/scenecraft/api_server.py` L4989–5178 — `_handle_mix_render_upload`
  - `src/scenecraft/api_server.py` L5180–5370 — `_handle_bounce_upload`
  - `src/scenecraft/api_server.py` L9385–9438 — `_handle_pool_peaks`
  - `src/scenecraft/audio/peaks.py` — `compute_peaks` (float16 bytes, stat-based cache key, ffmpeg streaming decode)
  - `src/scenecraft/chat.py` L3410+ — `_exec_bounce_audio` (paired WS→upload round-trip, out of scope here)
- Related: engine audit-2 §1A unit 9, §1E unit 6, §3 leak #11 (`startswith` path-traversal guard)

## Scope

**In scope — refactor must preserve**:
1. `GET  /api/projects/:name/files/*` — Range, ETag, If-Modified-Since, 304, 65 KiB chunked I/O, path-traversal guard.
2. `POST /api/projects/:name/bounce-upload` — multipart parse, WAV header validate (`wave` → `soundfile` fallback for 32-bit float), channels/sample_rate cross-check, delete-on-fail, optional `request_id` → chat event release.
3. `POST /api/projects/:name/mix-render-upload` — multipart parse, WAV header validate (`wave` only, no float fallback), channels/sample_rate cross-check, ±100ms duration drift check, delete-on-fail.
4. `GET  /api/projects/:name/pool/:seg_id/peaks` — returns raw float16 LE bytes, `X-Peak-Resolution` + `X-Peak-Duration` headers, stat-based cache via `compute_peaks`.

**Out of scope**:
- Chat-tool round-trip internals (`_exec_bounce_audio`, `_exec_analyze_master_bus`). Covered elsewhere.
- Internals of `compute_peaks` (ffmpeg subprocess, bucketing math). This spec only pins the endpoint's observable bytes + headers.
- Upload progress / streaming upload bodies — current impl `rfile.read(content_length)` is a single buffered read; refactor MAY switch to streaming, but MUST preserve all assertions below.
- Authentication / authorization (none today).
- TLS (terminated by cloudflared upstream).

---

## Migration Contract

The FastAPI rewrite MUST hold these byte-exact contracts:

### MC-1: Range header parsing — RFC 7233 subset
- Only `bytes=N-` and `bytes=N-M` forms are guaranteed supported today (regex `^bytes=(\d+)-(\d*)$`).
- Suffix ranges (`bytes=-500`, last 500 bytes) are **currently undefined** — regex does not match → falls through to full-file 200. FastAPI's default `FileResponse` DOES handle suffix ranges. See OQ-1.
- Multi-range (`bytes=0-100,200-300`) is **currently undefined** — regex matches only the first range. See OQ-2.
- End index is clamped: `end = min(parsed_end, file_size - 1)`.
- Response code for a valid range is `206`. Invalid-range syntax today returns `200 full file` (no 416). See OQ-3.

### MC-2: ETag format
- Exact format: `"<size_hex>-<mtime_int_hex>"` — both unquoted hex, hyphen-separated, wrapped in double quotes.
- Example: `"1a4f-68c3d2a1"` for a 6735-byte file with mtime 1757690017.
- Comparison against `If-None-Match` strips surrounding quotes/spaces on both sides before equality test.
- FastAPI's default `FileResponse` uses a different ETag format (`<mtime>-<size>` inode-style). The refactor MUST override to this exact format, otherwise all cached frontend assets re-download on first request post-migration.

### MC-3: Cache-Control directives
- Hot 200 / 206 response: `Cache-Control: public, max-age=3600, immutable`.
- `Accept-Ranges: bytes` on both 200 and 206.
- `Last-Modified`: RFC 2822 GMT via `email.utils.formatdate(..., usegmt=True)`.
- 304 response omits Cache-Control and Last-Modified; echoes `ETag` only.
- Thumbnail / poster path (separate handler, L8980) uses `max-age=86400, immutable` — not in scope here.

### MC-4: Multipart field names (upload endpoints)
- `bounce-upload` required fields: `audio`, `composite_hash`, `start_time_s`, `end_time_s`, `sample_rate`, `bit_depth`, `channels`. Optional: `request_id`.
- `mix-render-upload` required fields: `audio`, `mix_graph_hash`, `start_time_s`, `end_time_s`, `sample_rate`, `channels`. Optional: `request_id`.
- Field names are exact; parse is case-sensitive (`'name="audio"'` substring match on Content-Disposition header).
- Hash fields: exactly 64 hex chars, case-insensitive (`0-9a-fA-F`).
- Starlette's multipart parser (inherited by FastAPI `UploadFile`) MUST accept the same field names; the refactor MUST NOT rename any field.

### MC-5: WAV validator precedence
- `bounce-upload`: try `wave.open()` first → on **any** exception, fall back to `soundfile.info()` → on fallback failure, unlink + 400.
- `mix-render-upload`: try `wave.open()` only → on any exception, unlink + 400. No soundfile fallback (32-bit float mix renders are not expected).
- Validation order after successful open: channels → sample_rate → (mix-render only) duration drift ±100 ms.
- On ANY mismatch after bytes were written, the destination file is unlinked BEFORE returning 400. This invariant matters because the cache is content-addressable — a surviving corrupt file would be served forever.

### MC-6: Path-traversal guard semantics (audit leak #11)
- Current guard: `str((work_dir / project_name / file_path).resolve()).startswith(str(work_dir.resolve()))`.
- This is `str.startswith` on the stringified resolved path — not `Path.relative_to`. That means a sibling directory whose name is a prefix of `work_dir` (`/mnt/storage/prmichaelsen/.scenecraft` vs `/mnt/storage/prmichaelsen/.scenecraftx`) could bypass. In production today `work_dir` ends in `.scenecraft`, which has no sibling prefix → not exploitable in practice, but the refactor MUST switch to `Path.relative_to(work_dir)` (throws → 403) to close the leak.
- Symlinks inside `work_dir` pointing outside: `.resolve()` follows them, so the guard correctly rejects them today. Refactor MUST preserve this.
- Symlinks inside `pool/` (peaks handler): `_handle_pool_peaks` uses `relative_to(project_dir.resolve())` — correct. The peaks endpoint is NOT leak-#11.

### MC-7: Peaks endpoint byte contract
- Response body: raw `float16` little-endian bytes, length `2 * ceil(duration * resolution)` except clamped: `resolution` ∈ `[50, 2000]`.
- Headers: `Content-Type: application/octet-stream`, `Content-Length: <exact>`, `X-Peak-Resolution: <int>`, `X-Peak-Duration: <float, 6dp>`.
- Cache key: `sha1("{resolved_path}|{st_mtime_ns}|{st_size}|{offset:.6f}|{duration:.6f}|{resolution}")[:16]`. Touching the source file (even with the same content) invalidates the cache.
- `duration <= 0` → empty body (`b""`), 200.

---

## Known Divergence From FastAPI Defaults

1. **`FileResponse` Range handling**: FastAPI/Starlette's `FileResponse` supports Range natively but emits a different ETag format and handles suffix/multi-range. Refactor MUST either (a) override ETag + disable suffix/multi-range, or (b) accept the new behavior and update frontend cache-busting assumptions. Either choice MUST be explicit — no accidental change.
2. **Multipart parsing**: Starlette uses `python-multipart`. Field order, boundary handling, and CRLF stripping differ from the hand-rolled `body.split(b'--' + boundary)` parser. The refactor MUST still accept the frontend's existing requests byte-for-byte; a manual regression test MUST post a captured request from the running frontend and confirm 201.
3. **`rfile.read(content_length)`** is synchronous + buffered. FastAPI `UploadFile.read()` is async + spooled-to-tempfile above ~1 MiB. Disk-space implications on the engine host are new — note for ops.
4. **Error body shape**: `_error(status, code, message)` today emits `{"error": {"code", "message"}}`. FastAPI's default `HTTPException` emits `{"detail": "..."}`. The refactor MUST use a custom exception handler to preserve the `{"error": {...}}` shape; the frontend parses `err.error.code`.
5. **CORS**: handled by `_cors_headers()` inline today. Refactor MUST mount `CORSMiddleware` with the same allowed origins and preserve the exact header set on 304 responses (which are cache-sensitive).

---

## Interfaces

### GET `/api/projects/:name/files/*`

| Input | Source |
|---|---|
| `:name` | path param |
| `*` (arbitrary subpath) | path param |
| `Range` | request header (optional) |
| `If-None-Match` | request header (optional) |
| `If-Modified-Since` | request header (optional) |

Responses:
- `200 OK` — full body, `Content-Length`, `Content-Type`, `Accept-Ranges: bytes`, `Cache-Control`, `ETag`, `Last-Modified`.
- `206 Partial Content` — body bytes `[start..end]`, `Content-Range: bytes <s>-<e>/<size>`, other headers as above.
- `304 Not Modified` — no body, `ETag` only.
- `403 FORBIDDEN` — path-traversal attempt. Body: `{"error":{"code":"FORBIDDEN","message":"Path traversal denied"}}`.
- `404 NOT_FOUND` — file missing. Body: `{"error":{"code":"NOT_FOUND","message":"File not found: <rel>"}}`.

### POST `/api/projects/:name/bounce-upload`

Multipart fields: `audio` (bytes), `composite_hash` (hex64), `start_time_s` (float), `end_time_s` (float), `sample_rate` (int), `bit_depth` (int ∈ {16,24,32}), `channels` (int ∈ {1,2}), optional `request_id` (str).

- `201 Created` — body `{"rendered_path": "pool/bounces/<hash>.wav", "bytes": <int>, "channels": <int>, "sample_rate": <int>, "duration_s": <float>, "chat_released": <bool>}`.
- `400 BAD_REQUEST` — missing/invalid field, hash malformed, WAV corrupt, channels or sample_rate mismatch.
- `500 INTERNAL_ERROR` — unexpected server exception.

### POST `/api/projects/:name/mix-render-upload`

Same shape minus `bit_depth`; plus `mix_graph_hash` in place of `composite_hash`. Additionally validates `|wav_duration - (end-start)| <= 0.100`.

### GET `/api/projects/:name/pool/:seg_id/peaks?resolution=N`

- `200 OK` — body = raw float16 LE bytes; headers `X-Peak-Resolution`, `X-Peak-Duration`.
- `400 BAD_REQUEST` — pool segment has no `pool_path`, or `pool_path` resolves outside project.
- `404 NOT_FOUND` — unknown `seg_id`, or file missing on disk.
- `500 PEAKS_FAILED` — ffmpeg decode failure.

---

## Requirements

1. **R1** — GET file serving returns full file body with 200 and correct `Content-Length`, `Content-Type`, `Accept-Ranges`, `Cache-Control`, `ETag`, `Last-Modified`.
2. **R2** — GET with valid `Range: bytes=N-M` returns 206 with exact byte slice, correct `Content-Range`, and 65 KiB-chunked I/O (does not load entire range into memory).
3. **R3** — GET with matching `If-None-Match` returns 304 with only `ETag` header echoed.
4. **R4** — GET with `If-Modified-Since >= file mtime` returns 304.
5. **R5** — GET with path-traversal attempt (`../`, absolute path, symlink out) returns 403.
6. **R6** — GET for nonexistent file returns 404.
7. **R7** — ETag format is exactly `"<size_hex>-<mtime_int_hex>"`.
8. **R8** — `bounce-upload` accepts a valid 16/24-bit PCM WAV matching declared `sample_rate` + `channels`, writes to `pool/bounces/<composite_hash>.wav`, returns 201.
9. **R9** — `bounce-upload` accepts 32-bit float WAV via `soundfile` fallback when `wave.open` raises.
10. **R10** — `bounce-upload` rejects channels mismatch, sample_rate mismatch, or corrupt WAV with 400 AND unlinks the written file.
11. **R11** — `bounce-upload` rejects missing required fields, non-hex hash, wrong-length hash, `bit_depth` ∉ {16,24,32}, `channels` ∉ {1,2}, non-positive `sample_rate`, `end_time_s <= start_time_s` with 400 (no file written / written-then-unlinked).
12. **R12** — `bounce-upload` with `request_id` calls `set_bounce_render_event(request_id)`; exceptions from that call do NOT fail the upload (response still 201, `chat_released: false`).
13. **R13** — `mix-render-upload` performs all of R8/R10/R11 equivalents PLUS a `|wav_duration - (end-start)| <= 0.100` check; failure unlinks + 400.
14. **R14** — `mix-render-upload` does NOT have a soundfile fallback; WAV parse failure → unlink + 400.
15. **R15** — `pool/:seg_id/peaks` returns `2 * ceil(duration * clamped_resolution)` bytes of float16 LE with `X-Peak-Resolution` + `X-Peak-Duration` headers; `duration <= 0` → empty body + 200.
16. **R16** — peaks cache key incorporates `st_mtime_ns` and `st_size` of the pool file; changing the file (touch / rewrite) MUST produce a fresh computation.
17. **R17** — peaks endpoint rejects pool-path escape (symlink or crafted pool_path) with 400.
18. **R18** — CORS headers are present on every response including 304 and error bodies.
19. **R19** — All endpoints tolerate `BrokenPipeError` / `ConnectionResetError` mid-response without raising to the server loop.
20. **R20** — Upload endpoints are idempotent on repeated identical uploads (content-addressable by hash) — same hash → overwrite-safe → second response identical to first.

---

## Behavior Table

| # | Scenario | Expected Behavior | Tests |
|---|----------|-------------------|-------|
| 1 | GET existing file, no headers | 200 + full body + cache headers | `get-full-file-200`, `get-headers-present` |
| 2 | GET with `Range: bytes=0-99` on 1 KiB file | 206, body = first 100 bytes, `Content-Range: bytes 0-99/1024` | `get-range-basic` |
| 3 | GET with `Range: bytes=500-` (open-ended) | 206, body = bytes 500..EOF, `Content-Range: bytes 500-<size-1>/<size>` | `get-range-open-ended` |
| 4 | GET with `Range: bytes=0-99999999` (beyond EOF) | 206, end clamped to `file_size-1`, body = full file | `get-range-clamped-beyond-eof` |
| 5 | GET with matching `If-None-Match` | 304, only `ETag` header | `get-if-none-match-304` |
| 6 | GET with `If-Modified-Since` >= mtime | 304 | `get-if-modified-since-304` |
| 7 | GET with non-matching `If-None-Match` | 200 + full body | `get-if-none-match-miss` |
| 8 | GET traversal attempt `../../etc/passwd` | 403 FORBIDDEN | `get-traversal-blocked` |
| 9 | GET nonexistent file | 404 NOT_FOUND | `get-404` |
| 10 | GET large file with `Range: bytes=0-` | 206, body = full file, I/O is 65 KiB-chunked (not buffered in one read) | `get-large-file-chunked` |
| 11 | ETag format check | ETag matches `"^\"[0-9a-f]+-[0-9a-f]+\"$"` | `etag-format` |
| 12 | bounce-upload valid 24-bit WAV | 201, file written at `pool/bounces/<hash>.wav`, JSON body correct | `bounce-upload-happy-24bit` |
| 13 | bounce-upload valid 32-bit float WAV | 201 via soundfile fallback | `bounce-upload-happy-float32` |
| 14 | bounce-upload channels mismatch | 400, file unlinked | `bounce-upload-channels-mismatch-unlinks` |
| 15 | bounce-upload sample_rate mismatch | 400, file unlinked | `bounce-upload-sr-mismatch-unlinks` |
| 16 | bounce-upload corrupt bytes | 400, file unlinked | `bounce-upload-corrupt-unlinks` |
| 17 | bounce-upload missing `composite_hash` | 400 | `bounce-upload-missing-field` |
| 18 | bounce-upload non-hex hash | 400 | `bounce-upload-bad-hash` |
| 19 | bounce-upload `channels=3` | 400 | `bounce-upload-bad-channels` |
| 20 | bounce-upload `bit_depth=8` | 400 | `bounce-upload-bad-bit-depth` |
| 21 | bounce-upload `end_time_s <= start_time_s` | 400 | `bounce-upload-bad-time-range` |
| 22 | bounce-upload with `request_id`, event setter succeeds | 201, `chat_released: true` | `bounce-upload-releases-chat` |
| 23 | bounce-upload with `request_id`, event setter raises | 201, `chat_released: false` (upload still succeeds) | `bounce-upload-chat-setter-raises-nonfatal` |
| 24 | bounce-upload same hash twice | both respond 201 identically; file overwritten not appended | `bounce-upload-idempotent` |
| 25 | mix-render-upload valid WAV | 201 | `mix-render-happy` |
| 26 | mix-render-upload duration drifts by 200ms | 400, file unlinked | `mix-render-duration-drift-unlinks` |
| 27 | mix-render-upload 32-bit float WAV | 400 (no fallback), file unlinked | `mix-render-no-float-fallback` |
| 28 | mix-render-upload channels mismatch | 400, file unlinked | `mix-render-channels-mismatch-unlinks` |
| 29 | peaks for valid seg, resolution=400, duration=2.0 | 200, body length = 2 * ceil(2.0*400) = 1600 bytes float16 | `peaks-body-length` |
| 30 | peaks with `resolution=10` | clamped to 50 internally; length matches clamped value | `peaks-resolution-clamped-low` |
| 31 | peaks with `resolution=5000` | clamped to 2000 | `peaks-resolution-clamped-high` |
| 32 | peaks cache hit | second call returns identical bytes, no ffmpeg spawn | `peaks-cache-hit` |
| 33 | peaks cache invalidation on file touch | `st_mtime_ns` change → new cache key → fresh compute | `peaks-cache-invalidates-on-touch` |
| 34 | peaks unknown seg_id | 404 | `peaks-404-seg` |
| 35 | peaks with pool_path escaping project | 400 | `peaks-path-escape` |
| 36 | peaks duration=0 | 200, empty body | `peaks-empty-duration` |
| 37 | CORS headers on 304 | `Access-Control-Allow-Origin` present | `cors-on-304` |
| 38 | client disconnect mid-response | no exception surfaces to server loop | `disconnect-during-body-swallowed` |
| 39 | GET with suffix-range `Range: bytes=-500` | `undefined` | → [OQ-1](#open-questions) |
| 40 | GET with multi-range `Range: bytes=0-10,50-60` | `undefined` | → [OQ-2](#open-questions) |
| 41 | GET with invalid range syntax `Range: bytes=abc` | `undefined` (today: falls through to 200; RFC says 416) | → [OQ-3](#open-questions) |
| 42 | GET 0-byte file | `undefined` | → [OQ-4](#open-questions) |
| 43 | GET with `Range: bytes=1000-` beyond EOF start | `undefined` (RFC says 416) | → [OQ-5](#open-questions) |
| 44 | Simultaneous uploads, same `composite_hash`, different bytes | `undefined` — race on `write_bytes` then validate | → [OQ-6](#open-questions) |
| 45 | Upload >2 GiB body | `undefined` — BaseHTTPRequestHandler read limits, disk-full behavior | → [OQ-7](#open-questions) |
| 46 | Symlink inside `pool/` pointing outside project | `undefined` — `.resolve()` + `relative_to` should reject, not tested today | → [OQ-8](#open-questions) |
| 47 | Path-traversal via sibling-prefix dir (`/work-dir-evil/` when work_dir is `/work-dir`) | `undefined` — `startswith` leak (audit #11); not reachable in prod but refactor MUST use `relative_to` | → [OQ-9](#open-questions) |

---

## Tests

### Base Cases

#### Test: get-full-file-200 (covers R1)
**Given**: A project `demo` with `pool/a.bin` = 1024 bytes of known content.
**When**: Client issues `GET /api/projects/demo/files/pool/a.bin` with no Range/conditional headers.
**Then** (assertions):
- **status-200**: response status is 200.
- **body-exact**: response body equals the 1024 source bytes byte-for-byte.
- **content-length**: `Content-Length: 1024`.
- **content-type**: `Content-Type` matches `mimetypes.guess_type("a.bin")` or `application/octet-stream`.
- **accept-ranges**: `Accept-Ranges: bytes`.
- **cache-control**: `Cache-Control: public, max-age=3600, immutable`.
- **etag-present**: `ETag` header present and matches format regex (see `etag-format`).
- **last-modified-present**: `Last-Modified` header parses as RFC 2822 GMT.

#### Test: get-headers-present (covers R1)
**Given**: Same file as above.
**When**: `GET` issued.
**Then**:
- **cors-origin**: `Access-Control-Allow-Origin` header present.

#### Test: get-range-basic (covers R2)
**Given**: `pool/a.bin` of 1024 bytes.
**When**: `GET` with `Range: bytes=0-99`.
**Then**:
- **status-206**: status is 206.
- **body-length-100**: response body is exactly 100 bytes.
- **body-matches-slice**: body equals source bytes `[0:100]`.
- **content-range**: `Content-Range: bytes 0-99/1024`.
- **content-length-100**: `Content-Length: 100`.

#### Test: get-range-open-ended (covers R2)
**Given**: 1024-byte file.
**When**: `GET` with `Range: bytes=500-`.
**Then**:
- **status-206**: 206.
- **body-length-524**: body is 524 bytes.
- **content-range**: `Content-Range: bytes 500-1023/1024`.

#### Test: get-range-clamped-beyond-eof (covers R2)
**Given**: 1024-byte file.
**When**: `GET` with `Range: bytes=0-99999999`.
**Then**:
- **status-206**: 206.
- **content-range**: `Content-Range: bytes 0-1023/1024`.
- **body-length**: 1024 bytes.

#### Test: get-if-none-match-304 (covers R3)
**Given**: File served once; client captures `ETag`.
**When**: Second `GET` with `If-None-Match: <captured>`.
**Then**:
- **status-304**: status is 304.
- **empty-body**: response body is empty.
- **etag-echoed**: `ETag` header matches captured value.
- **no-cache-control**: `Cache-Control` header absent on 304.

#### Test: get-if-modified-since-304 (covers R4)
**Given**: File with known mtime `T`.
**When**: `GET` with `If-Modified-Since: <RFC2822 of T>`.
**Then**:
- **status-304**: 304.

#### Test: get-if-none-match-miss (covers R3)
**Given**: File modified since last fetch.
**When**: `GET` with stale `If-None-Match`.
**Then**:
- **status-200**: 200 + full body.

#### Test: get-traversal-blocked (covers R5)
**Given**: Project `demo`.
**When**: `GET /api/projects/demo/files/../../../etc/passwd`.
**Then**:
- **status-403**: status is 403.
- **error-code**: body `error.code == "FORBIDDEN"`.

#### Test: get-404 (covers R6)
**When**: `GET /api/projects/demo/files/nonexistent.bin`.
**Then**:
- **status-404**: 404.
- **error-code**: body `error.code == "NOT_FOUND"`.

#### Test: etag-format (covers R7)
**Given**: File of 6735 bytes, mtime 1757690017.
**Then**:
- **regex**: `ETag` matches literal `"1a4f-68c3d2a1"` (or equivalent hex of actual size/mtime).

#### Test: bounce-upload-happy-24bit (covers R8, R20)
**Given**: Project `demo`. Client generates a 2-second 44100 Hz stereo 24-bit PCM WAV whose SHA-256 is `H`.
**When**: `POST /api/projects/demo/bounce-upload` multipart with `audio=<bytes>, composite_hash=H, start_time_s=0, end_time_s=2.0, sample_rate=44100, bit_depth=24, channels=2`.
**Then**:
- **status-201**: 201.
- **file-written**: `<project>/pool/bounces/<H>.wav` exists and byte-equals uploaded bytes.
- **json-rendered-path**: body `rendered_path == "pool/bounces/<H>.wav"`.
- **json-sample-rate**: body `sample_rate == 44100`.
- **json-channels**: body `channels == 2`.
- **json-chat-released-false**: body `chat_released == false` (no request_id provided).

#### Test: bounce-upload-happy-float32 (covers R9)
**Given**: 32-bit float WAV generated via soundfile.
**When**: Valid `POST` with `bit_depth=32`.
**Then**:
- **status-201**: 201.
- **file-written**: exists.
- **validator-path**: a log line or test hook confirms the `soundfile` fallback branch was taken (i.e. `wave.open` raised).

#### Test: bounce-upload-channels-mismatch-unlinks (covers R10)
**Given**: A mono WAV.
**When**: `POST` with `channels=2` declared.
**Then**:
- **status-400**: 400.
- **file-unlinked**: `pool/bounces/<H>.wav` does NOT exist after the response.
- **error-code**: `error.code == "BAD_REQUEST"`.

#### Test: bounce-upload-missing-field (covers R11)
**When**: POST omits `composite_hash`.
**Then**:
- **status-400**: 400.
- **error-msg-mentions-field**: error message contains `composite_hash`.

#### Test: mix-render-happy (covers R13)
**Given**: 1-second 48000 Hz stereo 16-bit PCM WAV, hash `H`.
**When**: `POST /api/projects/demo/mix-render-upload` with matching metadata.
**Then**:
- **status-201**: 201.
- **file-written**: `pool/mixes/<H>.wav` exists.
- **duration-in-body**: response `duration_s` within 0.01 of 1.0.

#### Test: mix-render-duration-drift-unlinks (covers R13)
**Given**: 1-second WAV.
**When**: POST declares `start_time_s=0, end_time_s=1.5` (500ms drift).
**Then**:
- **status-400**: 400.
- **file-unlinked**: destination does not exist.
- **error-msg**: mentions "duration mismatch".

#### Test: mix-render-no-float-fallback (covers R14)
**Given**: 32-bit float WAV.
**When**: POST to `/mix-render-upload`.
**Then**:
- **status-400**: 400.
- **file-unlinked**: no file left behind.
- **error-msg**: mentions "Invalid WAV file".

#### Test: peaks-body-length (covers R15)
**Given**: Pool segment `seg_id=s1` with `duration_seconds=2.0`, valid `pool_path` pointing at a real audio file.
**When**: `GET /api/projects/demo/pool/s1/peaks?resolution=400`.
**Then**:
- **status-200**: 200.
- **content-type**: `application/octet-stream`.
- **content-length-1600**: `Content-Length: 1600` (2 * ceil(2.0 * 400)).
- **x-peak-resolution**: header `X-Peak-Resolution: 400`.
- **x-peak-duration**: header `X-Peak-Duration: 2.000000`.
- **body-dtype**: decoding body as `np.float16` little-endian yields 800 finite floats each in `[0, 1]`.

### Edge Cases

#### Test: get-large-file-chunked (covers R2)
**Given**: A 500 MiB file in `pool/`.
**When**: `GET` with `Range: bytes=0-` (full range via Range header — the real browser pattern).
**Then**:
- **status-206**: 206.
- **peak-memory-bounded**: server RSS increase during transfer stays under ~2 MiB (confirming 65 KiB chunked reads, not whole-range buffering).
- **body-sha256**: streamed body SHA-256 matches source SHA-256.

#### Test: peaks-cache-hit (covers R15, R16)
**Given**: First peaks call warmed the cache for `(path, offset, duration, resolution)`.
**When**: Second identical call.
**Then**:
- **same-bytes**: response body byte-equals first call.
- **no-ffmpeg**: no new ffmpeg subprocess is spawned (process count / monkeypatch observes zero new invocations).

#### Test: peaks-cache-invalidates-on-touch (covers R16)
**Given**: Cache warm.
**When**: Pool file is `touch`ed (mtime bumped, content identical), then peaks re-requested.
**Then**:
- **fresh-compute**: ffmpeg IS spawned this time.
- **same-bytes**: result bytes equal prior (content unchanged).

#### Test: peaks-resolution-clamped-low (covers R15)
**When**: `GET ...?resolution=10`.
**Then**:
- **clamped-to-50**: response length matches `2 * ceil(duration * 50)`.
- **x-peak-resolution-header**: header echoes the requested `10` (current behavior — the header does NOT reflect internal clamp). **Note**: if this drifts in refactor, update the frontend too.

#### Test: peaks-resolution-clamped-high (covers R15)
**When**: `resolution=5000`.
**Then**:
- **clamped-to-2000**: body length matches 2000-peak-per-second clamped compute.

#### Test: peaks-empty-duration (covers R15)
**Given**: Pool segment with `duration_seconds=0`.
**Then**:
- **status-200**: 200.
- **empty-body**: body is `b""`.

#### Test: peaks-path-escape (covers R17)
**Given**: A pool_segments row whose `pool_path` has been tampered to `"../../etc/passwd"`.
**When**: peaks GET.
**Then**:
- **status-400**: 400.
- **error-msg**: contains "outside project".

#### Test: bounce-upload-sr-mismatch-unlinks (covers R10)
**Given**: 44100 Hz WAV, declared `sample_rate=48000`.
**Then**:
- **status-400**: 400.
- **file-unlinked**: destination absent.

#### Test: bounce-upload-corrupt-unlinks (covers R10)
**Given**: 1 KiB of `/dev/urandom` labeled as `audio`.
**Then**:
- **status-400**: 400.
- **file-unlinked**: destination absent.
- **error-msg**: mentions "Invalid WAV file".

#### Test: bounce-upload-bad-hash (covers R11)
**When**: POST with `composite_hash="zz"` (non-hex, wrong length).
**Then**:
- **status-400**: 400.
- **error-msg**: contains "64 hex chars".

#### Test: bounce-upload-bad-channels (covers R11)
**When**: `channels=3`.
**Then**:
- **status-400**: 400.
- **no-file-written**: destination does not appear (validation is pre-write for numeric-range checks).

#### Test: bounce-upload-bad-bit-depth (covers R11)
**When**: `bit_depth=8`.
**Then**:
- **status-400**: 400.
- **no-file-written**: destination does not appear.

#### Test: bounce-upload-bad-time-range (covers R11)
**When**: `end_time_s=0, start_time_s=1`.
**Then**:
- **status-400**: 400.

#### Test: bounce-upload-releases-chat (covers R12)
**Given**: A registered `request_id` `req-123` in `chat.set_bounce_render_event`.
**When**: POST includes `request_id=req-123`.
**Then**:
- **status-201**: 201.
- **chat-released-true**: `chat_released: true`.
- **event-fired**: the corresponding asyncio.Event is set (observable via the waiting coroutine unblocking).

#### Test: bounce-upload-chat-setter-raises-nonfatal (covers R12, R19)
**Given**: `set_bounce_render_event` is monkeypatched to raise.
**When**: POST with `request_id=whatever`.
**Then**:
- **status-201**: upload still 201 (the setter exception MUST be caught).
- **chat-released-false**: `chat_released: false`.
- **file-written**: destination exists.

#### Test: bounce-upload-idempotent (covers R20)
**Given**: First POST succeeded.
**When**: Identical POST issued again.
**Then**:
- **status-201**: 201.
- **file-bytes-unchanged**: destination bytes identical.
- **mtime-updated**: file was rewritten (or content-equal no-op — implementation-defined but MUST not raise).

#### Test: mix-render-channels-mismatch-unlinks (covers R13)
**Given**: Stereo WAV.
**When**: POST declares `channels=1`.
**Then**:
- **status-400**: 400.
- **file-unlinked**: destination absent.

#### Test: cors-on-304 (covers R18)
**Given**: A file with a cached ETag.
**When**: `GET` with matching `If-None-Match`.
**Then**:
- **status-304**: 304.
- **cors-origin-present**: `Access-Control-Allow-Origin` header IS present on the 304 response (current behavior — frontend caching relies on it).

#### Test: disconnect-during-body-swallowed (covers R19)
**Given**: Client initiates a large-file GET and closes the connection mid-body.
**When**: Server continues writing until `BrokenPipeError`.
**Then**:
- **no-exception-to-loop**: server stays up; no traceback reaches the HTTPServer main loop.
- **subsequent-request-succeeds**: immediately following request on a fresh connection returns 200.

#### Test: peaks-404-seg (covers R15)
**When**: `GET /api/projects/demo/pool/nope/peaks`.
**Then**:
- **status-404**: 404.
- **error-code**: `NOT_FOUND`.

---

## Acceptance Criteria

- [ ] All Base Case tests pass against the current BaseHTTPRequestHandler impl (baseline — captures existing behavior).
- [ ] All Base Case tests pass against the FastAPI rewrite with no code changes to the test suite.
- [ ] All Edge Case tests pass against both impls OR the `undefined` rows have been resolved and converted to concrete tests via a follow-up clarification.
- [ ] Manual captured-request regression: a real browser PUT/POST captured via devtools → replayed against rewrite → 201 with identical response body.
- [ ] ETag format byte-exact match verified on at least 3 different file sizes/mtimes.
- [ ] CORS headers present on 200, 206, 304, 400, 403, 404 responses (verified via test or curl matrix).

---

## Non-Goals

- Authentication, authorization, rate limiting.
- Streaming upload parsing (single-buffered `rfile.read` is OK today; refactor MAY stream but MUST keep all assertions).
- Gzip / brotli compression of file bodies.
- HTTP/2, HTTP/3.
- Observability metrics (Prometheus histograms, etc.).
- Refactoring `_handle_pool_peaks` to async — it's sync and currently blocks the request thread during ffmpeg decode; acceptable for now.

---

## Open Questions

- **OQ-1 (referenced by Behavior Table row 39)** — Suffix Range `bytes=-500`: does the refactor preserve today's "fall-through to 200 full body" behavior, or adopt RFC 7233's "last 500 bytes, 206"? Frontend does not send suffix ranges today (video/audio players use `bytes=N-`), so either is safe — but a decision is required.
- **OQ-2 (row 40)** — Multi-range `bytes=0-10,50-60`: today only the first subrange is served (still 206, Content-Type not `multipart/byteranges`). FastAPI's `FileResponse` rejects or implements multipart/byteranges. Decide.
- **OQ-3 (row 41)** — Invalid Range syntax `bytes=abc`: RFC says 416 Range Not Satisfiable. Today: 200 full body. Preserve or fix?
- **OQ-4 (row 42)** — 0-byte file: what does `ETag` format produce (`"0-<mtime>"`), what does `Range: bytes=0-` return? Today: likely 206 with empty body and `Content-Range: bytes 0--1/0` (broken). Define behavior.
- **OQ-5 (row 43)** — Range start beyond EOF (`bytes=1000-` on 100-byte file): RFC says 416. Today falls through with clamped end < start → undefined. Decide.
- **OQ-6 (row 44)** — Concurrent uploads with same `composite_hash` but different bytes (impossible if clients are honest, but): race on `dest.write_bytes` → whichever finishes last wins → may pass validation while the other reader sees torn bytes. Acceptable today because hashes are content-derived; lock-free OK. Confirm.
- **OQ-7 (row 45)** — Upload body >2 GiB: `int(Content-Length)` → `rfile.read(len)` on a single `bytes` object. Python's `bytes` can hold it but memory pressure is real; disk-full during `dest.write_bytes` raises → caught by outer `except` → 500. Define max and enforce via request-size middleware in FastAPI.
- **OQ-8 (row 46)** — Symlink inside `pool/` pointing outside project: `_handle_pool_peaks` uses `(project_dir / pool_rel).resolve()` then `relative_to(project_dir.resolve())` → should correctly reject. Needs an explicit test to confirm across impls.
- **OQ-9 (row 47)** — Audit leak #11 (`startswith` path-traversal guard): refactor MUST switch to `Path.relative_to`. Flag as a required behavior change (not a preserved quirk); add explicit test.
- **OQ-10** — Does the frontend rely on `Last-Modified` being present (or only `ETag`)? If `ETag`-only is fine, refactor can simplify.
- **OQ-11** — `X-Peak-Resolution` header: does it echo the requested value or the clamped internal value? Current code echoes requested. Confirm frontend tolerates this drift.

---

## Related Artifacts

- Spec: `agent/specs/local.fastapi-migration.md` — umbrella migration plan.
- Audit: engine audit-2 — §1A unit 9 (file serving), §1E unit 6 (uploads), §3 leak #11 (traversal).
- Code: `src/scenecraft/api_server.py`, `src/scenecraft/audio/peaks.py`, `src/scenecraft/chat.py`.
- Future spec: chat WS round-trip (`_exec_bounce_audio`, `_exec_analyze_master_bus`) — paired with the upload endpoints but intentionally out of scope here.

---

**Namespace**: local
**Spec**: engine-file-serving-and-uploads
**Version**: 1.0.0
**Status**: Active
