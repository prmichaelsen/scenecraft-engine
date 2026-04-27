# Spec: engine-analysis-handlers

> **🤖 Agent Directive**: If you are reading this file, treat it as the authoritative description of end-system behavior for the engine-internal analysis handlers. Every row of the Behavior Table is the contract; every test under Tests is language-agnostic and must be translatable directly into pytest (or equivalent) without re-interpreting intent. Scenarios the source material did not resolve are marked `undefined` and linked to Open Questions — do NOT guess them into tests or implementations.

**Namespace**: local
**Version**: 1.0.0
**Created**: 2026-04-27
**Last Updated**: 2026-04-27
**Status**: Active

---

## Purpose

Define the black-box behavior of the five engine-internal analysis handlers — `bounce_audio`, `analyze_master_bus`, `generate_dsp`, `generate_descriptions`, and the waveform peaks HTTP endpoint — covering their WS / HTTP request-response contracts, cache-table interactions, analyzer invocations, and failure modes.

## Source

- **Mode**: Draft (chat-context, derived from engine source code + audit-2 §1E)
- **Primary sources**:
  - `src/scenecraft/chat.py:3036` — `_exec_analyze_master_bus`
  - `src/scenecraft/chat.py:3422` — `_exec_bounce_audio`
  - `src/scenecraft/chat.py:2632` — `_exec_generate_dsp`
  - `src/scenecraft/chat.py:3828` — `_exec_generate_descriptions`
  - `src/scenecraft/audio/peaks.py` — `compute_peaks`
  - `src/scenecraft/api_server.py:4989` — `/mix-render-upload`
  - `src/scenecraft/api_server.py:5180` — `/bounce-upload`
  - `src/scenecraft/api_server.py:495` — `/audio-clips/:id/peaks`
- **Complementary**: `../scenecraft/agent/specs/local.bounce-and-analysis.md` (frontend perspective — UX, elicitation, upload-side mechanics). This spec does NOT duplicate that spec; it covers engine-internal handler mechanics.

## Scope

### In Scope

- `_exec_bounce_audio`: input validation, end-time resolution, selection-id existence checks, `composite_hash` computation, `audio_bounces` cache row lifecycle, WS `bounce_audio_request` emission, per-request `asyncio.Event` handshake, 60s default timeout (`BOUNCE_RENDER_TIMEOUT_S`), duration/size readback, download-URL construction.
- `_exec_analyze_master_bus`: input validation, end-time resolution, `mix_graph_hash` computation, 5-tuple cache key `(mix_graph_hash, start_time_s, end_time_s, sample_rate, analyzer_version)`, WS `mix_render_request` emission, 60s default timeout (`MIX_RENDER_TIMEOUT_S`), the seven analyses (`peak`, `true_peak`, `rms`, `lufs`, `clipping_detect`, `spectral_centroid`, `dynamic_range`), bulk-insert persistence pattern.
- `_exec_generate_dsp`: 3-tuple cache key `(source_segment_id, analyzer_version, params_hash)`, synchronous librosa loads at `sr=22050`, `hop_length=512`, analyses (`onsets`, `rms`, `vocal_presence`, `tempo`, `spectral_centroid`).
- `_exec_generate_descriptions`: Gemini chunked invocation (default 30s chunks), structured JSON row conversion, per-chunk + global scalars, run-row-last creation pattern.
- `compute_peaks`: stat-based (`mtime_ns`+`size`+params) cache key, `float16` little-endian byte emission, ffmpeg streaming decode to mono 16 kHz s16le, `.peaks/` filesystem cache, `/audio-clips/:id/peaks` and `/pool/:seg_id/peaks` endpoints.

### Out of Scope

- Frontend UX, elicitation flow, OfflineAudioContext rendering — covered by `scenecraft/agent/specs/local.bounce-and-analysis.md`.
- Multipart upload validation details (WAV header cross-check, duration-drift tolerance, 64-hex validation) — covered by the file-serving / upload spec; this spec only cares about the release-side behavior (`set_*_render_event`).
- The underlying hashing algorithms (`compute_mix_graph_hash`, `compute_bounce_hash`, `_dsp_params_hash`) — those are specified elsewhere; this spec treats them as pure functions whose output is a stable hex digest.
- MCP tool registration / JSON-schema shapes — surface-level, not handler-internal.

---

## Requirements

### Bounce Audio (`_exec_bounce_audio`)

- **R-B1**: Reject non-numeric `start_time_s` / `end_time_s` with `{"error": ...}`; never raise.
- **R-B2**: Reject `start_time_s < 0`.
- **R-B3**: Reject `track_ids` and `clip_ids` both non-empty simultaneously (but both `[]` is allowed → `mode=full`).
- **R-B4**: Reject `track_ids` / `clip_ids` that aren't `list[str]`.
- **R-B5**: Reject `sample_rate` not in `{44100, 48000, 88200, 96000}`.
- **R-B6**: Reject `bit_depth` not in `{16, 24, 32}`.
- **R-B7**: Reject `channels` not in `{1, 2}`.
- **R-B8**: Derive `mode` from selection: `tracks` if `track_ids` non-empty, `clips` if `clip_ids` non-empty, else `full`.
- **R-B9**: Resolve `end_time_s=None` via `MAX(audio_clips.end_time WHERE deleted_at IS NULL)`; if result `<= start_time_s`, return `{"error": ...}` (and do NOT insert a bounce row).
- **R-B10**: Reject `track_ids` / `clip_ids` that don't exist (clips must also have `deleted_at IS NULL`).
- **R-B11**: Compute `composite_hash = compute_bounce_hash(project_dir, start, end, mode, track_ids, clip_ids, sample_rate, bit_depth, channels)` — same inputs → same hash.
- **R-B12**: On cache hit (row with `rendered_path IS NOT NULL`), return `{cached: True, ...}` immediately without WS round-trip.
- **R-B13**: If a stale cache row exists (`rendered_path IS NULL`, prior failed/timed-out render), delete it before inserting a new one.
- **R-B14**: On cache miss, insert an `audio_bounces` row (`rendered_path=NULL`), then if `ws is None` return `{"error": ...}` and delete the row; else emit `bounce_audio_request` over the WS with a fresh `request_id = uuid4().hex`, register a per-request `asyncio.Event` in `_BOUNCE_RENDER_EVENTS`, and `await event.wait()` bounded by `BOUNCE_RENDER_TIMEOUT_S` (default 60.0s; override via `timeout_s` kwarg).
- **R-B15**: If `ws.send` raises, delete the bounce row and return `{"error": "failed to send bounce_audio_request over ws: ..."}`.
- **R-B16**: If the wait times out, delete the bounce row and return `{"error": "bounce render timeout (Ns) — frontend did not upload", ...}`.
- **R-B17**: The `_BOUNCE_RENDER_EVENTS` entry is always popped in a `finally` block (timeout or success).
- **R-B18**: When the event is set but the file is still missing on disk, delete the row and return a diagnostic error.
- **R-B19**: On success, stat the WAV for `size_bytes`, read `duration_s` via `wave` (falling back to `soundfile` for 32-bit float WAVs), and call `update_bounce_rendered(bounce.id, rendered_path, size_bytes, duration_s)`.
- **R-B20**: Return a success payload with `{bounce_id, cached:False, rendered_path, download_url, duration_s, size_bytes, mode, tracks_requested, clips_requested, composite_hash, sample_rate, bit_depth, channels}`; `download_url` = `/api/projects/<project_name>/bounces/<bounce_id>.wav` (`<project>` placeholder if `project_name is None`).
- **R-B21**: `set_bounce_render_event(request_id)` returns `True` iff a matching pending event exists; `False` otherwise. Never raises.

### Analyze Master Bus (`_exec_analyze_master_bus`)

- **R-M1**: Reject non-numeric `start_time_s` / `end_time_s`, `sample_rate <= 0`, `start_time_s < 0` with `{"error": ...}`.
- **R-M2**: Reject `analyses` that isn't `None` or `list`; coerce list members to `str`.
- **R-M3**: Default `analyses` to the seven-element tuple `(peak, true_peak, rms, lufs, clipping_detect, spectral_centroid, dynamic_range)`.
- **R-M4**: Resolve `end_time_s=None` via `_resolve_mix_end_time`; if `<= start_time_s`, return `{"error": ...}`.
- **R-M5**: Compute `mix_graph_hash = compute_mix_graph_hash(project_dir)`; `analyzer_version = f"mix-librosa-{librosa.__version__}"`.
- **R-M6**: 5-tuple cache key: `(mix_graph_hash, start_time_s, end_time_s, sample_rate, analyzer_version)`.
- **R-M7**: On cache hit (via `get_mix_run`) AND `force_rerun=False`, return `{cached: True, run_id, mix_graph_hash, start_time_s, end_time_s, rendered_path, scalars, clipping_events, analyses_written}` without re-rendering or re-analyzing.
- **R-M8**: On `force_rerun=True`, delete the existing run before proceeding.
- **R-M9**: If `pool/mixes/<mix_graph_hash>.wav` exists, skip the WS round-trip; else emit `mix_render_request` over `ws`, `await` bounded by `MIX_RENDER_TIMEOUT_S` (default 60.0s; override via `timeout_s` kwarg).
- **R-M10**: If `ws is None` and WAV missing, return an error WITHOUT inserting a run row.
- **R-M11**: If `ws.send` raises, return an error (no row inserted).
- **R-M12**: On timeout, return an error; the run row is NOT inserted (insertion happens only after WAV is on disk).
- **R-M13**: `_MIX_RENDER_EVENTS` entry always popped in `finally`.
- **R-M14**: If event set but file still missing, return a diagnostic error (no run row inserted).
- **R-M15**: Load WAV via `soundfile`; if on-disk `sr != requested sample_rate`, return an error (no row inserted).
- **R-M16**: Insert the run row (with `rendered_path=None`) BEFORE running analyses.
- **R-M17**: For each requested analysis in `{peak, true_peak, rms, lufs, clipping_detect, spectral_centroid, dynamic_range}`, compute and accumulate datapoints / sections / scalars. Unknown analysis names are silently skipped.
- **R-M18**: Per-analysis exceptions inside `true_peak`, `lufs`, `spectral_centroid` are caught and logged; that analysis is NOT added to `analyses_written`, but other analyses continue.
- **R-M19**: A top-level exception during the analysis loop (outside the inner try/excepts) deletes the run row and returns `{"error": "analysis failed: ..."}`.
- **R-M20**: `dynamic_range` = `peak_db - lufs_integrated`; computes inputs on demand if not already in the requested set; skips the scalar when either input is `-inf` (silence / too-short buffer).
- **R-M21**: After the loop, bulk-insert datapoints (`rms`, `spectral_centroid`), sections (`clipping_event`), scalars (`peak_db`, `true_peak_db`, `lufs_integrated`, `clip_count`, `dynamic_range_db`); then `update_mix_run_rendered_path(run.id, "pool/mixes/<hash>.wav")`.
- **R-M22**: Return `{run_id, cached:False, mix_graph_hash, start_time_s, end_time_s, rendered_path, scalars, clipping_events, analyses_written}`.
- **R-M23**: `set_mix_render_event(request_id)` returns `True` iff a matching pending event exists; `False` otherwise. Never raises.

### Generate DSP (`_exec_generate_dsp`)

- **R-D1**: Reject missing / non-string `source_segment_id`.
- **R-D2**: Reject `analyses` that isn't `None` or `list`; default `["onsets", "rms", "vocal_presence", "tempo"]`.
- **R-D3**: Reject if `pool_segment` not found, has no `pool_path`, or the on-disk file is missing.
- **R-D4**: 3-tuple cache key: `(source_segment_id, analyzer_version, params_hash)` where `analyzer_version=f"librosa-{librosa.__version__}"` and `params_hash=_dsp_params_hash(analyses, 22050, 512)`.
- **R-D5**: On cache hit AND `force_rerun=False`, return `{cached:True, run_id, source_segment_id, analyses_written, datapoint_count, section_count, scalars}` without rerunning.
- **R-D6**: On `force_rerun=True`, delete the existing run before proceeding.
- **R-D7**: Load audio once via `load_audio(path, sr=22050)`; `FileNotFoundError` / `ValueError` → `{"error": "failed to load audio: ..."}`.
- **R-D8**: Known analyses: `onsets`, `rms`, `vocal_presence`, `tempo`, `spectral_centroid`. Unknown names silently skipped.
- **R-D9**: `rms` → datapoints `("rms", time_s, energy, None)` via `_compute_rms_envelope(y, sr, hop_length=512)`.
- **R-D10**: `onsets` → datapoints `("onset", time_s, strength, {"strength": strength})` via `_detect_onsets(y, sr, hop_length=512)`.
- **R-D11**: `vocal_presence` → sections `(start, end, "vocal_presence", None, None)` via `detect_presence(y, sr, hop_length=512)`.
- **R-D12**: `tempo` → scalar `tempo_bpm` via `librosa.beat.beat_track(y, sr, hop_length=512)`. Per-analysis exception is caught and logged; `tempo` is NOT added to `analyses_written` / `analyses_to_store`.
- **R-D13**: `spectral_centroid` → datapoints downsampled to ≤20 pts/sec via `librosa.feature.spectral_centroid`. Per-analysis exception is caught and logged.
- **R-D14**: Run row is created AFTER analyses succeed (not before), storing only the analyses that produced output (`analyses_to_store`).
- **R-D15**: Bulk inserts: `bulk_insert_dsp_datapoints`, `bulk_insert_dsp_sections`, `set_dsp_scalars`.
- **R-D16**: Never raises; all errors returned as `{"error": ...}`.
- **R-D17**: `_exec_generate_dsp` is synchronous (not async).

### Generate Descriptions (`_exec_generate_descriptions`)

- **R-G1**: Reject missing / non-string `source_segment_id`.
- **R-G2**: Defaults: `model="gemini-2.5-pro"`, `chunk_size_s=30.0`, `prompt_version="v1"`, `force_rerun=False`.
- **R-G3**: Reject if `pool_segment` not found, has no `pool_path`, or the on-disk file is missing.
- **R-G4**: 3-tuple cache key: `(source_segment_id, model, prompt_version)` via `get_audio_description_run`.
- **R-G5**: On cache hit AND `force_rerun=False`, return `{cached:True, run_id, source_segment_id, chunks_analyzed, chunks_failed:0, descriptions_written}` where `chunks_analyzed = len(distinct (start_s, end_s) pairs)` and `descriptions_written = len(stored rows)`.
- **R-G6**: On `force_rerun=True`, delete the existing run before proceeding.
- **R-G7**: Chunk via `_chunk_audio_for_gemini(path, chunk_duration=chunk_size_s)`; chunking exception → `{"error": "failed to chunk audio: ..."}`.
- **R-G8**: For each chunk, call `_gemini_describe_chunk_structured(chunk_path, start, end, model, prompt_version)`. If it returns `None`, increment `chunks_failed` and skip.
- **R-G9**: If the returned dict yields zero convertible rows via `_rows_from_description`, increment `chunks_failed` (not `chunks_analyzed`).
- **R-G10**: `_rows_from_description` emits at most one row per property: `section_type` (string), `mood` (string), `energy` (numeric, clamped to [0,1]), `vocal_style` (string; also emits a `NULL`-value row if key present with explicit `None`), `instrumentation` (list → comma-joined `value_text` + `{"instruments": [...]}` raw), `notes` (non-empty stripped string).
- **R-G11**: Run row created AFTER all chunks processed (not before); bulk-insert all rows via `bulk_insert_audio_descriptions`.
- **R-G12**: Return `{run_id, cached:False, source_segment_id, chunks_analyzed, chunks_failed, descriptions_written}`.
- **R-G13**: `_exec_generate_descriptions` is synchronous (not async).
- **R-G14**: Never raises; all errors returned as `{"error": ...}`.

### Waveform Peaks (`compute_peaks` + HTTP routes)

- **R-P1**: `duration <= 0` → returns empty bytes `b""` without invoking ffmpeg.
- **R-P2**: `resolution` clamped to `[50, 2000]`.
- **R-P3**: Cache key = `sha1(f"{resolved_path}|{mtime_ns}|{size}|{offset:.6f}|{duration:.6f}|{resolution}")[:16]`, stored at `<project_dir>/audio_staging/.peaks/<key>.f16`.
- **R-P4**: On cache hit, return the file's bytes unchanged (no ffmpeg).
- **R-P5**: On cache miss, spawn `ffmpeg -nostdin -loglevel error -ss <offset> -t <duration> -i <src> -vn -ac 1 -ar 16000 -f s16le -`; read stdout in `bucket_bytes = (total_samples // n_peaks) * 2` chunks; per bucket compute `max(abs(samples / 32768.0))` as `float32` then cast to `float16`.
- **R-P6**: Output length = `ceil(duration * resolution)` `float16` little-endian values (2 bytes each).
- **R-P7**: Ffmpeg not on `$PATH` → `FileNotFoundError` wrapped as `RuntimeError("ffmpeg not found: ...")`.
- **R-P8**: Ffmpeg exits non-zero AND no bucket was written → `RuntimeError(f"ffmpeg rc={rc}: <stderr snippet>")`.
- **R-P9**: Ffmpeg hangs > 60s → `proc.kill()` + `RuntimeError("ffmpeg timed out during peak decode")`.
- **R-P10**: Cache write failure (`OSError`) is logged but the function still returns the computed bytes.
- **R-P11**: `/audio-clips/:id/peaks` rejects missing clip (`404`), clip with no `source_path` (`400`), source path escaping the project dir (`400`), source missing on disk (`404`), `RuntimeError` from `compute_peaks` (`500 PEAKS_FAILED`).
- **R-P12**: Response headers: `Content-Type: application/octet-stream`, `X-Peak-Resolution: <n>`, `X-Peak-Duration: <seconds:.6f>`.
- **R-P13**: `/pool/:seg_id/peaks` uses the raw `pool_segments` row (full file, `source_offset=0`, `duration = full length`).

---

## Interfaces / Data Shapes

### WS Messages (Server → Client)

```json
// bounce_audio_request
{
  "type": "bounce_audio_request",
  "request_id": "<uuid4 hex>",
  "bounce_id": "<audio_bounces.id>",
  "composite_hash": "<64-char hex>",
  "start_time_s": 0.0,
  "end_time_s": 12.5,
  "mode": "full" | "tracks" | "clips",
  "track_ids": ["..."] | null,
  "clip_ids":  ["..."] | null,
  "sample_rate": 48000,
  "bit_depth": 24,
  "channels": 2
}

// mix_render_request
{
  "type": "mix_render_request",
  "request_id": "<uuid4 hex>",
  "mix_graph_hash": "<64-char hex>",
  "start_time_s": 0.0,
  "end_time_s": 12.5,
  "sample_rate": 48000
}
```

### Success Payloads

```python
# _exec_bounce_audio (success)
{
  "bounce_id": str, "cached": bool, "rendered_path": "pool/bounces/<hash>.wav",
  "download_url": "/api/projects/<name>/bounces/<id>.wav",
  "duration_s": float, "size_bytes": int, "mode": "full"|"tracks"|"clips",
  "tracks_requested": list[str], "clips_requested": list[str],
  "composite_hash": str, "sample_rate": int, "bit_depth": int, "channels": int,
}

# _exec_analyze_master_bus (success)
{
  "run_id": str, "cached": bool, "mix_graph_hash": str,
  "start_time_s": float, "end_time_s": float,
  "rendered_path": "pool/mixes/<hash>.wav",
  "scalars": dict[str, float], "clipping_events": int,
  "analyses_written": list[str],
}

# _exec_generate_dsp (success)
{
  "run_id": str, "cached": bool, "source_segment_id": str,
  "analyses_written": list[str],
  "datapoint_count": int, "section_count": int, "scalars": dict[str, float],
}

# _exec_generate_descriptions (success)
{
  "run_id": str, "cached": bool, "source_segment_id": str,
  "chunks_analyzed": int, "chunks_failed": int, "descriptions_written": int,
}
```

### Peaks HTTP Response

- Body: `float16` little-endian bytes, length `ceil(duration*resolution)*2`.
- Headers: `Content-Type: application/octet-stream`, `X-Peak-Resolution`, `X-Peak-Duration`.

---

## Behavior Table

| #  | Scenario | Expected Behavior | Tests |
|----|----------|-------------------|-------|
| 1  | bounce with valid inputs, no cache, WS uploads WAV | Row inserted, WS emitted, event awaited, row finalized, success payload returned | `bounce-happy-path-full-mode`, `bounce-emits-ws-request`, `bounce-finalizes-row` |
| 2  | bounce cache hit (row has rendered_path) | Return `{cached:True}` without WS round-trip | `bounce-cache-hit-short-circuits` |
| 3  | bounce stale cache row (rendered_path is NULL) | Old row deleted, new render initiated | `bounce-stale-row-deleted-before-retry` |
| 4  | bounce with invalid sample_rate / bit_depth / channels | Returns `{"error":...}`, no row inserted | `bounce-rejects-invalid-format-fields` |
| 5  | bounce with both track_ids and clip_ids non-empty | Returns `{"error": "pass either..."}` | `bounce-rejects-dual-selection` |
| 6  | bounce with missing track_id / clip_id | Returns `{"error": "...not found: [...]"}` | `bounce-rejects-missing-ids` |
| 7  | bounce end_time_s None and project has no clips | Returns `{"error":...}`, no row inserted | `bounce-empty-project-errors` |
| 8  | bounce ws is None AND WAV missing | Row inserted then deleted, returns error | `bounce-no-ws-cleans-up-row` |
| 9  | bounce ws.send raises | Row deleted, returns send-failure error, event popped | `bounce-ws-send-failure-cleans-up` |
| 10 | bounce 60s timeout, no upload arrives | Row deleted, returns timeout error, event popped | `bounce-timeout-cleans-up-row` |
| 11 | bounce event set but WAV still missing on disk | Row deleted, returns diagnostic error | `bounce-event-set-but-file-absent` |
| 12 | bounce 32-bit float WAV (stdlib `wave` fails) | Falls back to `soundfile` for duration | `bounce-reads-32bit-float-via-soundfile` |
| 13 | `set_bounce_render_event` with unknown request_id | Returns `False`, no exception | `bounce-set-event-unknown-id` |
| 14 | analyze_master_bus with valid inputs, WAV on disk | Run row inserted, analyses run, datapoints/sections/scalars persisted | `analyze-happy-path-default-analyses` |
| 15 | analyze_master_bus cache hit | Return `{cached:True}` without re-rendering or re-analyzing | `analyze-cache-hit-returns-cached-scalars` |
| 16 | analyze_master_bus force_rerun=True | Existing run deleted, re-run executed | `analyze-force-rerun-deletes-prior` |
| 17 | analyze_master_bus WAV missing AND ws is None | Returns error, NO run row inserted | `analyze-missing-wav-no-ws-no-row-inserted` |
| 18 | analyze_master_bus WS request triggers upload | `mix_render_request` emitted, event awaited, analysis resumes | `analyze-ws-roundtrip-unblocks-on-upload` |
| 19 | analyze_master_bus ws.send raises | Returns error, NO run row inserted | `analyze-ws-send-failure-no-row` |
| 20 | analyze_master_bus 60s timeout | Returns timeout error, NO run row inserted | `analyze-ws-timeout-no-row` |
| 21 | analyze_master_bus on-disk sample_rate != requested | Returns error, NO run row inserted | `analyze-sample-rate-mismatch-errors` |
| 22 | analyze_master_bus `true_peak` / `lufs` / `spectral_centroid` raises | Logged, skipped from `analyses_written`; other analyses still run | `analyze-per-analysis-exception-is-skipped` |
| 23 | analyze_master_bus unexpected top-level exception during analyses | Run row deleted, returns `{"error": "analysis failed: ..."}` | `analyze-toplevel-exception-rolls-back-row` |
| 24 | analyze_master_bus unknown analysis name | Silently skipped, not in `analyses_written` | `analyze-unknown-analysis-silently-skipped` |
| 25 | analyze_master_bus `dynamic_range` alone (peak/lufs not requested) | Computes peak + lufs on demand, emits scalar | `analyze-dynamic-range-computes-missing-inputs` |
| 26 | analyze_master_bus silence (peak=-inf, lufs=-inf) | `dynamic_range_db` scalar NOT persisted; others proceed | `analyze-silence-skips-dynamic-range-scalar` |
| 27 | generate_dsp with valid pool_segment | Run row created AFTER analyses; datapoints + scalars persisted | `dsp-happy-path-default-analyses` |
| 28 | generate_dsp cache hit | Return `{cached:True}` without re-analysis | `dsp-cache-hit-short-circuits` |
| 29 | generate_dsp force_rerun=True | Existing run deleted, re-run | `dsp-force-rerun-deletes-prior` |
| 30 | generate_dsp missing source_segment / missing file | Returns `{"error":...}`, never raises | `dsp-rejects-missing-segment-and-file` |
| 31 | generate_dsp unknown analysis name | Silently skipped | `dsp-unknown-analysis-silently-skipped` |
| 32 | generate_dsp tempo / spectral_centroid raises | Logged, skipped from `analyses_written`; others still run | `dsp-per-analysis-exception-is-skipped` |
| 33 | generate_dsp audio load raises | Returns `{"error": "failed to load audio: ..."}`, no run row inserted | `dsp-audio-load-failure-no-row` |
| 34 | generate_descriptions happy path, N chunks | Run row created AFTER chunks; all rows bulk-inserted | `descriptions-happy-path-multiple-chunks` |
| 35 | generate_descriptions cache hit | Return `{cached:True}` with chunk count derived from distinct (start,end) | `descriptions-cache-hit-counts-distinct-chunks` |
| 36 | generate_descriptions force_rerun=True | Existing run deleted, re-run | `descriptions-force-rerun-deletes-prior` |
| 37 | generate_descriptions chunk returns None | `chunks_failed += 1`, loop continues | `descriptions-chunk-none-is-failure` |
| 38 | generate_descriptions chunk returns dict with no convertible keys | `chunks_failed += 1` (not `chunks_analyzed`) | `descriptions-empty-dict-is-failure` |
| 39 | generate_descriptions chunking throws | Returns `{"error": "failed to chunk audio: ..."}`, no run row | `descriptions-chunking-failure-no-row` |
| 40 | generate_descriptions `_rows_from_description` field shapes | section_type/mood/vocal_style strings, energy clamped [0,1], instrumentation list joined + raw JSON, notes stripped, vocal_style explicit-null row emitted when key present | `descriptions-row-conversion-shapes` |
| 41 | peaks duration <= 0 | Returns `b""`, ffmpeg NOT invoked | `peaks-zero-duration-short-circuits` |
| 42 | peaks resolution below 50 / above 2000 | Clamped to [50, 2000] | `peaks-resolution-clamped` |
| 43 | peaks cache hit | Returns cached bytes, ffmpeg NOT invoked | `peaks-cache-hit-skips-ffmpeg` |
| 44 | peaks cache miss, ffmpeg succeeds | Returns float16 bytes length `ceil(duration*resolution)*2` | `peaks-cache-miss-decodes-and-writes` |
| 45 | peaks ffmpeg binary missing | Raises `RuntimeError("ffmpeg not found: ...")` | `peaks-ffmpeg-missing-raises` |
| 46 | peaks ffmpeg exits non-zero and no bucket written | Raises `RuntimeError(f"ffmpeg rc=...")` | `peaks-ffmpeg-nonzero-exit-raises` |
| 47 | peaks ffmpeg hangs > 60s | Killed, raises `RuntimeError("ffmpeg timed out during peak decode")` | `peaks-ffmpeg-timeout-kills-and-raises` |
| 48 | peaks cache-write OSError | Computed bytes still returned, error only logged | `peaks-cache-write-failure-still-returns` |
| 49 | peaks source file mtime changes | Cache key changes → new file written | `peaks-mtime-bump-busts-cache` |
| 50 | peaks HTTP route — missing clip / bad source_path / source missing / compute_peaks raises | 404 / 400 / 404 / 500 with structured error envelope | `peaks-route-error-responses` |
| 51 | peaks HTTP route success | 200 with `X-Peak-Resolution`, `X-Peak-Duration`, `Content-Type: application/octet-stream` | `peaks-route-success-headers` |
| 52 | concurrent peaks requests for the same clip | `undefined` | → [OQ-5](#open-questions) |
| 53 | bounce timeout fires at T+60s but upload lands at T+61s | `undefined` — row deleted at timeout; late upload writes WAV but the pending event is gone, so `set_bounce_render_event` returns False; the WAV sits on disk as an orphan (not linked to any bounces row) | → [OQ-1](#open-questions) |
| 54 | composite_hash cache hit but file on disk missing | `undefined` — bounce code checks `rendered_path IS NOT NULL` from DB but does NOT stat the file; analyze_master_bus re-renders via WS if file missing regardless of cache | → [OQ-2](#open-questions) |
| 55 | librosa raises mid-analysis in `analyze_master_bus` for `rms` / `peak` / `clipping_detect` (no inner try/except) | `undefined` — top-level `except` deletes the run row but other analyses already computed in the same loop iteration are lost; partial in-memory accumulators are discarded | → [OQ-3](#open-questions) |
| 56 | Gemini rate limit mid-chunk (raises inside `_gemini_describe_chunk_structured`) | `undefined` — current code expects `None` on failure; a raised exception propagates out of `_exec_generate_descriptions` and violates R-G14 | → [OQ-4](#open-questions) |
| 57 | Two concurrent `compute_peaks` calls for same (source, offset, duration, resolution) | `undefined` — both may launch ffmpeg; `write_bytes` of the same content is last-write-wins; unlikely to corrupt because bytes are identical, but not enforced | → [OQ-5](#open-questions) |
| 58 | bounce/analyze ws.send succeeds but WS closes before upload | `undefined` — the frontend never calls `/bounce-upload`, so timeout path fires; behavior identical to R-B16 / R-M12 | → [OQ-6](#open-questions) |
| 59 | `_exec_generate_descriptions` / `_exec_generate_dsp` called with a pool_segment whose file is currently being written | `undefined` — no file-lock; librosa may read truncated audio | → [OQ-7](#open-questions) |

---

## Behavior

### Bounce Audio — step by step

1. Validate `start_time_s`, `end_time_s`, selection (`track_ids` xor `clip_ids`), `sample_rate` ∈ valid-set, `bit_depth` ∈ {16,24,32}, `channels` ∈ {1,2}. Invalid → `{"error":...}`.
2. Determine `mode` from selection.
3. Resolve `end_time_s` via `_resolve_mix_end_time` if None; reject if `<= start_time_s`.
4. Verify `track_ids` / `clip_ids` exist in DB (clips with `deleted_at IS NULL`).
5. Compute `composite_hash`.
6. `get_bounce_by_hash`:
   - Row + `rendered_path IS NOT NULL` → return cached payload.
   - Row + `rendered_path IS NULL` → `delete_bounce`.
7. Build `selection` payload; `create_bounce(...)` → pending row.
8. If WAV at `pool/bounces/<hash>.wav` exists, skip to step 12. Else:
   - If `ws is None`: `delete_bounce`, return error.
   - `request_id = uuid4().hex`; register event in `_BOUNCE_RENDER_EVENTS`.
   - `ws.send(bounce_audio_request)` (on raise → `delete_bounce`, return error).
   - `asyncio.wait_for(event.wait(), timeout=BOUNCE_RENDER_TIMEOUT_S)`.
   - Timeout → `delete_bounce`, return timeout error.
   - `finally: _BOUNCE_RENDER_EVENTS.pop(request_id, None)`.
   - If WAV still missing → `delete_bounce`, return diagnostic error.
9. Stat WAV for `size_bytes`.
10. Read duration via `wave`; fall back to `soundfile` on failure.
11. `update_bounce_rendered(bounce.id, rel_path, size_bytes, duration_s)`.
12. Return success payload.

### Analyze Master Bus — step by step

1. Validate inputs.
2. Resolve `end_time_s`, validate window.
3. Compute `mix_graph_hash`, `analyzer_version`.
4. `get_mix_run` by 5-tuple key.
5. Cache hit + not forced → return cached payload (includes `clipping_events` count via `query_mix_sections`).
6. Cache hit + forced → `delete_mix_run`.
7. If `pool/mixes/<hash>.wav` exists, skip to step 11. Else WS round-trip (identical shape to bounce, different message type + timeout constant). Note: the run row is NOT inserted until the WAV exists on disk.
8. Load WAV via `soundfile`; compare on-disk `sr` to requested `sample_rate` → mismatch = error (no row inserted).
9. `create_mix_run(..., rendered_path=None)`.
10. Enter analysis loop (`try`):
    - `peak`: `peak_db` scalar.
    - `true_peak`: `true_peak_db` scalar (inner try/except).
    - `rms`: datapoints.
    - `lufs`: `lufs_integrated` scalar (inner try/except).
    - `clipping_detect`: `clipping_event` sections + `clip_count` scalar.
    - `spectral_centroid`: datapoints (inner try/except).
    - `dynamic_range`: computes peak/lufs on demand if missing; skips if either is `-inf`.
    - Unknown analysis names silently skipped.
    - Top-level exception → `delete_mix_run`, return `{"error": "analysis failed: ..."}`.
11. Bulk-insert datapoints / sections; `set_mix_scalars`; `update_mix_run_rendered_path`.
12. Return success payload.

### Generate DSP — step by step

1. Validate inputs, resolve pool_segment + path.
2. Build `analyzer_version`, `params_hash`.
3. `get_dsp_run` by 3-tuple; cache hit + not forced → return cached; cache hit + forced → `delete_dsp_run`.
4. Load audio via `load_audio(path, sr=22050)`.
5. Loop analyses. Per-analysis exceptions in `tempo` / `spectral_centroid` are caught; others accumulate directly (unhandled exception propagates? — see OQ-3 analogue below; current code does not wrap `rms`/`onsets`/`vocal_presence` in an inner try/except). Unknown names skipped.
6. `create_dsp_run(..., analyses=analyses_to_store)` AFTER loop.
7. Bulk-insert datapoints / sections; `set_dsp_scalars`.
8. Return `{run_id, cached:False, ..., datapoint_count, section_count, scalars}`.

### Generate Descriptions — step by step

1. Validate inputs, resolve pool_segment + path.
2. `get_audio_description_run` by `(source_segment_id, model, prompt_version)`; cache hit + not forced → return cached; cache hit + forced → `delete_audio_description_run`.
3. `_chunk_audio_for_gemini(path, chunk_duration=chunk_size_s)`; exception → error (no run row).
4. For each chunk: call `_gemini_describe_chunk_structured`. `None` → `chunks_failed += 1`. Dict → convert via `_rows_from_description`; if 0 rows, `chunks_failed += 1`; else `chunks_analyzed += 1`, `all_rows.extend(rows)`.
5. `create_audio_description_run(...)` AFTER the loop.
6. `bulk_insert_audio_descriptions(run.id, all_rows)`.
7. Return payload.

### Peaks — step by step

1. Validate `duration` and clamp `resolution`.
2. Stat source, compute cache key, check `.peaks/<key>.f16`.
3. Cache hit → return bytes.
4. Cache miss → spawn ffmpeg subprocess (`-ss`, `-t`, `-i`, mono 16 kHz s16le to stdout).
5. Stream stdout in `bucket_bytes` chunks; per bucket compute abs peak as `float32`.
6. `proc.wait(timeout=60)`; kill on timeout.
7. Cast to `float16`, `tobytes()`.
8. Attempt `cache_file.write_bytes(data)`; log OSError but still return bytes.

---

## Acceptance Criteria

- [ ] All invalid-input cases across the five handlers return `{"error": ...}` and never raise (except where OQ-3 / OQ-4 note undefined behavior).
- [ ] Cache keys are exactly as specified — no extra fields, no missing fields.
- [ ] Run-row creation ordering matches: `analyze_master_bus` inserts BEFORE analyses (so in-flight probes see the row); `generate_dsp` and `generate_descriptions` insert AFTER analyses (so failures don't leave empty rows).
- [ ] `bounce_audio` deletes its pending row on every failure branch (ws=None, ws.send raise, timeout, missing-file-after-set).
- [ ] `_BOUNCE_RENDER_EVENTS` / `_MIX_RENDER_EVENTS` entries are popped in all code paths (success, timeout, send-failure).
- [ ] `set_bounce_render_event` / `set_mix_render_event` never raise; return False for unknown IDs.
- [ ] `compute_peaks` never writes a partial file on ffmpeg failure.
- [ ] Peaks cache key includes `mtime_ns` so source edits bust the cache.
- [ ] Unknown analysis names are silently skipped in all three analyzer handlers.

---

## Tests

### Base Cases

The core behavior contract: happy path, common bad paths, primary positive and negative assertions. A reader should be able to understand the normal operation of the engine-internal analysis handlers from this subsection alone.

#### Test: bounce-happy-path-full-mode (covers R-B8, R-B9, R-B11, R-B19, R-B20)

**Given**:
- Project has audio_clips, `ws` is a mock.
- `pool/bounces/<hash>.wav` does not exist yet.
- A background task simulates the upload by writing the WAV and calling `set_bounce_render_event(request_id)`.

**When**: `_exec_bounce_audio(project_dir, {"sample_rate":48000,"bit_depth":24,"channels":2}, ws=ws, project_name="p")`.

**Then**:
- **mode-full**: returned `mode == "full"`.
- **composite-hash-hex**: `composite_hash` is 64 lowercase hex chars.
- **rendered-path**: `rendered_path == "pool/bounces/<composite_hash>.wav"`.
- **download-url-shape**: `download_url == f"/api/projects/p/bounces/{bounce_id}.wav"`.
- **duration-s-nonzero**: `duration_s > 0`.
- **size-bytes-matches-disk**: `size_bytes == os.stat(dest).st_size`.
- **row-finalized**: `audio_bounces` row has non-null `rendered_path`, `size_bytes`, `duration_s`.
- **cached-false**: `cached is False`.

#### Test: bounce-emits-ws-request (covers R-B14)

**Given**: Bounce call with no on-disk WAV, mocked ws.

**When**: The handler runs.

**Then**:
- **ws-send-called-once**: `ws.send` invoked exactly once.
- **message-type**: Parsed JSON has `type == "bounce_audio_request"`.
- **request-id-uuid4-hex**: `request_id` is 32-char lowercase hex.
- **payload-fields**: `bounce_id`, `composite_hash`, `start_time_s`, `end_time_s`, `mode`, `track_ids`, `clip_ids`, `sample_rate`, `bit_depth`, `channels` all present with correct types.

#### Test: bounce-finalizes-row (covers R-B19)

**Given**: Bounce handler completes successfully with a 32-bit float WAV that `wave` cannot open.

**When**: Readback runs.

**Then**:
- **falls-back-to-soundfile**: `duration_s` matches `soundfile.info` value.
- **update-called**: `update_bounce_rendered` invoked with `(bounce.id, rel_path, size_bytes, duration_s)`.

#### Test: bounce-cache-hit-short-circuits (covers R-B12)

**Given**: A bounce row already exists with `rendered_path="pool/bounces/<hash>.wav"`.

**When**: `_exec_bounce_audio` is called with the same parameters.

**Then**:
- **cached-true**: returned `cached is True`.
- **no-ws-send**: `ws.send` NOT invoked.
- **no-new-row**: no new `audio_bounces` row inserted.
- **returns-existing-id**: `bounce_id` equals the existing row's id.

#### Test: bounce-stale-row-deleted-before-retry (covers R-B13)

**Given**: Prior bounce row with `rendered_path IS NULL` (timed-out render).

**When**: A fresh bounce call arrives with the same hash.

**Then**:
- **old-row-deleted**: the NULL-path row is removed before inserting the new row.
- **unique-constraint-ok**: new row inserted without IntegrityError.

#### Test: bounce-rejects-invalid-format-fields (covers R-B5, R-B6, R-B7)

**Given**: Parameterized over `{sample_rate:22050}`, `{bit_depth:12}`, `{channels:3}`.

**When**: Handler invoked for each.

**Then**:
- **error-message-matches**: returned dict has an `error` key mentioning the invalid field.
- **no-row-inserted**: no `audio_bounces` row was created.
- **no-ws-send**: `ws.send` NOT invoked.

#### Test: bounce-rejects-dual-selection (covers R-B3)

**Given**: `track_ids=["a"]` AND `clip_ids=["b"]` both non-empty.

**When**: Handler invoked.

**Then**:
- **error-both-selected**: `error` mentions "either track_ids or clip_ids, not both".
- **no-row-inserted**: no row created.

#### Test: bounce-rejects-missing-ids (covers R-B10)

**Given**: `track_ids=["ghost-track"]`.

**When**: Handler invoked.

**Then**:
- **error-not-found**: `error` contains "track_ids not found: ['ghost-track']".

#### Test: bounce-empty-project-errors (covers R-B9)

**Given**: Project with zero `audio_clips`, `end_time_s=None`.

**When**: Handler invoked.

**Then**:
- **error-end-lte-start**: `error` describes `end_time_s <= start_time_s`.
- **no-row-inserted**: no row created.

#### Test: bounce-no-ws-cleans-up-row (covers R-B14)

**Given**: `ws=None`, no WAV on disk.

**When**: Handler invoked.

**Then**:
- **error-returned**: non-empty `error` string.
- **row-absent**: no `audio_bounces` row exists for `composite_hash` afterwards.

#### Test: bounce-ws-send-failure-cleans-up (covers R-B15, R-B17)

**Given**: `ws.send` raises `RuntimeError("conn closed")`.

**When**: Handler runs.

**Then**:
- **error-mentions-send**: `error` contains "failed to send bounce_audio_request over ws".
- **row-deleted**: pending bounce row removed.
- **event-popped**: `_BOUNCE_RENDER_EVENTS` does NOT contain the request_id after return.

#### Test: bounce-timeout-cleans-up-row (covers R-B16, R-B17)

**Given**: No upload ever arrives; `timeout_s=0.05`.

**When**: Handler runs.

**Then**:
- **error-timeout**: `error` contains "bounce render timeout".
- **row-deleted**: row removed.
- **event-popped**: event entry removed.

#### Test: bounce-event-set-but-file-absent (covers R-B18)

**Given**: Test calls `set_bounce_render_event(request_id)` WITHOUT writing the WAV.

**When**: Handler awakens.

**Then**:
- **error-file-still-missing**: `error` contains "WAV is still missing".
- **row-deleted**: row removed.

#### Test: bounce-set-event-unknown-id (covers R-B21)

**Given**: `_BOUNCE_RENDER_EVENTS` is empty.

**When**: `set_bounce_render_event("deadbeef")`.

**Then**:
- **returns-false**: return value is `False`.
- **no-raise**: no exception.

#### Test: analyze-happy-path-default-analyses (covers R-M3, R-M5, R-M16, R-M21, R-M22)

**Given**: WAV placed at `pool/mixes/<hash>.wav` with known peak/LUFS profile; `ws` can be None.

**When**: `_exec_analyze_master_bus(project_dir, {})`.

**Then**:
- **run-row-inserted**: exactly one `mix_runs` row with the 5-tuple key.
- **analyses-written-order**: returned list is a subset of the default seven.
- **scalars-keys**: includes `peak_db`, `lufs_integrated`, `clip_count`, `dynamic_range_db` (when not silence).
- **rendered-path-set**: DB row's `rendered_path == "pool/mixes/<hash>.wav"`.
- **cached-false**: `cached is False`.

#### Test: analyze-cache-hit-returns-cached-scalars (covers R-M7)

**Given**: A prior successful run exists with persisted scalars and clipping events.

**When**: Same 5-tuple call is made.

**Then**:
- **cached-true**: `cached is True`.
- **clipping-count-matches**: `clipping_events == len(query_mix_sections(...,"clipping_event"))`.
- **no-new-row**: no new run row inserted.
- **no-librosa-call**: `librosa.feature.rms` not invoked.

#### Test: analyze-force-rerun-deletes-prior (covers R-M8)

**Given**: Prior run exists.

**When**: Handler called with `force_rerun=True`.

**Then**:
- **prior-row-gone**: old `run_id` no longer present.
- **new-run-row**: a different `run_id` returned.

#### Test: analyze-missing-wav-no-ws-no-row-inserted (covers R-M10)

**Given**: No WAV on disk, `ws=None`.

**When**: Handler called.

**Then**:
- **error-returned**: non-empty error.
- **mix-runs-empty**: `mix_runs` table count unchanged.

#### Test: analyze-ws-roundtrip-unblocks-on-upload (covers R-M9, R-M13)

**Given**: No WAV on disk, mocked ws; background task writes WAV and calls `set_mix_render_event(request_id)`.

**When**: Handler runs.

**Then**:
- **ws-message-type**: `ws.send` called with `type == "mix_render_request"`.
- **analysis-completes**: return value has `cached:False` with scalars populated.
- **event-popped**: `_MIX_RENDER_EVENTS` empty after return.

#### Test: analyze-ws-send-failure-no-row (covers R-M11)

**Given**: `ws.send` raises; no WAV on disk.

**When**: Handler runs.

**Then**:
- **error-returned**: error mentions "failed to send mix_render_request over ws".
- **mix-runs-empty**: no run row inserted.

#### Test: analyze-ws-timeout-no-row (covers R-M12)

**Given**: `timeout_s=0.05`, no upload arrives.

**When**: Handler runs.

**Then**:
- **error-timeout**: error contains "mix render timeout".
- **mix-runs-empty**: no row inserted.

#### Test: analyze-sample-rate-mismatch-errors (covers R-M15)

**Given**: WAV on disk has `sr=44100`; handler called with `sample_rate=48000`.

**When**: Handler runs.

**Then**:
- **error-mismatch**: error mentions "does not match".
- **mix-runs-empty**: no row inserted.

#### Test: analyze-per-analysis-exception-is-skipped (covers R-M18)

**Given**: Patched `_mix_lufs` raises `ValueError`.

**When**: Handler runs with default analyses.

**Then**:
- **lufs-not-in-written**: `"lufs" not in analyses_written`.
- **peak-still-written**: `"peak" in analyses_written`.
- **no-rollback**: run row still exists with rendered_path set.

#### Test: analyze-toplevel-exception-rolls-back-row (covers R-M19)

**Given**: Patched `_mix_peak_db` raises a generic `Exception` outside any inner try/except.

**When**: Handler runs.

**Then**:
- **error-analysis-failed**: returned dict contains `"error": "analysis failed: ..."`.
- **row-deleted**: the `mix_runs` row has been deleted.

#### Test: analyze-unknown-analysis-silently-skipped (covers R-M17)

**Given**: `analyses=["peak", "does_not_exist"]`.

**When**: Handler runs.

**Then**:
- **unknown-not-in-written**: `"does_not_exist" not in analyses_written`.
- **no-error**: `error` key absent.

#### Test: analyze-dynamic-range-computes-missing-inputs (covers R-M20)

**Given**: `analyses=["dynamic_range"]` only.

**When**: Handler runs on a non-silent WAV.

**Then**:
- **dynamic-range-scalar-present**: `scalars["dynamic_range_db"]` is a finite float.
- **analyses-written-contains-dr**: `"dynamic_range" in analyses_written`.

#### Test: dsp-happy-path-default-analyses (covers R-D2, R-D4, R-D14, R-D15)

**Given**: Valid pool_segment with a short WAV on disk.

**When**: `_exec_generate_dsp(project_dir, {"source_segment_id": "seg-1"})`.

**Then**:
- **datapoint-count-positive**: `datapoint_count > 0`.
- **analyses-written-superset**: `analyses_written` is a subset of `["onsets","rms","vocal_presence","tempo"]`.
- **run-row-created-after**: patching `create_dsp_run` to raise produces NO orphaned row (because the run isn't created until after the loop).
- **cached-false**: `cached is False`.

#### Test: dsp-cache-hit-short-circuits (covers R-D5)

**Given**: Prior dsp_run exists for the same 3-tuple key.

**When**: Handler called again.

**Then**:
- **cached-true**: `cached is True`.
- **no-librosa-call**: `load_audio` not invoked.

#### Test: dsp-force-rerun-deletes-prior (covers R-D6)

**Given**: Prior run exists.

**When**: Handler called with `force_rerun=True`.

**Then**:
- **old-run-deleted**: prior `run_id` absent.
- **new-run-id**: returned `run_id` differs.

#### Test: dsp-rejects-missing-segment-and-file (covers R-D1, R-D3)

**Given**: Parameterized: missing `source_segment_id`, unknown id, segment with no `pool_path`, segment whose file doesn't exist on disk.

**When**: Handler called for each.

**Then**:
- **error-returned**: each returns an `error` string.
- **no-run-row**: no `dsp_runs` row created.

#### Test: dsp-unknown-analysis-silently-skipped (covers R-D8)

**Given**: `analyses=["rms","nonexistent"]`.

**When**: Handler runs.

**Then**:
- **unknown-not-in-written**: `"nonexistent" not in analyses_written`.

#### Test: descriptions-happy-path-multiple-chunks (covers R-G7, R-G11, R-G12)

**Given**: Mocked `_chunk_audio_for_gemini` returns 3 chunks; mocked `_gemini_describe_chunk_structured` returns a valid dict for each.

**When**: Handler runs.

**Then**:
- **chunks-analyzed-3**: `chunks_analyzed == 3`.
- **chunks-failed-0**: `chunks_failed == 0`.
- **descriptions-written-positive**: `descriptions_written > 0`.
- **run-row-after-chunks**: `create_audio_description_run` is invoked AFTER the per-chunk loop finishes.

#### Test: descriptions-cache-hit-counts-distinct-chunks (covers R-G5)

**Given**: Prior run with rows spanning 2 distinct (start_s, end_s) pairs.

**When**: Handler called with the same 3-tuple key.

**Then**:
- **cached-true**: `cached is True`.
- **chunks-analyzed-2**: `chunks_analyzed == 2`.
- **chunks-failed-0**: `chunks_failed == 0`.

#### Test: descriptions-force-rerun-deletes-prior (covers R-G6)

**Given**: Prior run exists.

**When**: `force_rerun=True`.

**Then**:
- **prior-deleted**: old run_id absent.

#### Test: descriptions-chunk-none-is-failure (covers R-G8)

**Given**: Mocked structured caller returns `None` for 1 of 3 chunks.

**When**: Handler runs.

**Then**:
- **chunks-analyzed-2**: `chunks_analyzed == 2`.
- **chunks-failed-1**: `chunks_failed == 1`.

#### Test: descriptions-empty-dict-is-failure (covers R-G9)

**Given**: Mocked structured caller returns `{}` for 1 chunk.

**When**: Handler runs.

**Then**:
- **chunks-failed-plus-one**: That chunk increments `chunks_failed`, not `chunks_analyzed`.

#### Test: descriptions-chunking-failure-no-row (covers R-G7)

**Given**: `_chunk_audio_for_gemini` raises.

**When**: Handler runs.

**Then**:
- **error-returned**: error mentions "failed to chunk audio".
- **no-run-row**: no `audio_description_runs` row created.

#### Test: descriptions-row-conversion-shapes (covers R-G10)

**Given**: A fixture dict covering every branch of `_rows_from_description`.

**When**: `_rows_from_description(dict, 0.0, 30.0)`.

**Then**:
- **section-type-row**: one row `("section_type", value_text=<str>, None, None, None)`.
- **mood-row**: one row for `mood`.
- **energy-clamped**: input `energy=1.5` produces `value_num=1.0`; `-0.5` produces `0.0`.
- **vocal-style-string**: `("vocal_style", value_text=<str>, None, None, None)`.
- **vocal-style-explicit-null**: dict with `"vocal_style": None` produces a row with `value_text=None, value_num=None`.
- **instrumentation-joined**: `value_text` is comma-joined; `raw_json == {"instruments": [...]}`.
- **notes-stripped**: leading/trailing whitespace removed; empty-after-strip → no row.

#### Test: peaks-happy-path (covers R-P3, R-P5, R-P6)

**Given**: A 1-second 440 Hz sine WAV; `resolution=400`.

**When**: `compute_peaks(wav, 0.0, 1.0, 400, project_dir=project_dir)`.

**Then**:
- **byte-length**: returned `len == ceil(1.0*400)*2 == 800`.
- **dtype**: `np.frombuffer(result, dtype=np.float16)` runs without error.
- **peak-magnitude**: all values in `[0.0, 1.0]`.
- **cache-file-written**: `<project_dir>/audio_staging/.peaks/<key>.f16` exists with identical bytes.

#### Test: peaks-zero-duration-short-circuits (covers R-P1)

**Given**: `duration=0`.

**When**: `compute_peaks(...)` called.

**Then**:
- **empty-bytes**: returns `b""`.
- **ffmpeg-not-called**: no subprocess spawned.

#### Test: peaks-resolution-clamped (covers R-P2)

**Given**: `resolution=5` (below min) and `resolution=5000` (above max).

**When**: Handler runs.

**Then**:
- **low-clamped**: output has `ceil(duration*50)*2` bytes.
- **high-clamped**: output has `ceil(duration*2000)*2` bytes.

#### Test: peaks-cache-hit-skips-ffmpeg (covers R-P4)

**Given**: Cache file already present.

**When**: Handler called with same args.

**Then**:
- **ffmpeg-not-called**: `subprocess.Popen` NOT invoked.
- **bytes-equal-cached**: returned bytes == cache file bytes.

#### Test: peaks-route-success-headers (covers R-P12)

**Given**: Valid audio_clip with source on disk.

**When**: `GET /api/projects/:name/audio-clips/:id/peaks?resolution=400`.

**Then**:
- **status-200**: HTTP status is 200.
- **content-type-octet-stream**: `Content-Type == "application/octet-stream"`.
- **x-peak-resolution**: `X-Peak-Resolution == "400"`.
- **x-peak-duration**: `X-Peak-Duration` matches clip duration to 6 decimal places.

#### Test: peaks-route-error-responses (covers R-P11)

**Given**: Parameterized: unknown clip_id; clip with empty `source_path`; `source_path` resolving outside project; source file missing; `compute_peaks` raises `RuntimeError`.

**When**: `GET /peaks` issued.

**Then**:
- **unknown-clip-404**: `{code:"NOT_FOUND"}` + 404.
- **bad-source-path-400**: 400.
- **outside-project-400**: 400.
- **missing-file-404**: 404.
- **compute-peaks-raise-500**: 500 + `code:"PEAKS_FAILED"`.

### Edge Cases

Boundaries, unusual inputs, concurrency, idempotency, ordering, time-dependent behavior, resource exhaustion.

#### Test: analyze-silence-skips-dynamic-range-scalar (covers R-M20)

**Given**: All-zero WAV on disk.

**When**: Handler called with default analyses.

**Then**:
- **peak-db-neg-inf**: `scalars["peak_db"] == float("-inf")`.
- **dynamic-range-absent**: `"dynamic_range_db" not in scalars` (because one operand is `-inf`).
- **dynamic-range-not-in-written**: `"dynamic_range" not in analyses_written`.

#### Test: dsp-per-analysis-exception-is-skipped (covers R-D12, R-D13)

**Given**: Patched `librosa.beat.beat_track` raises.

**When**: Handler runs with `analyses=["tempo","rms"]`.

**Then**:
- **tempo-not-written**: `"tempo" not in analyses_written`.
- **rms-still-written**: `"rms" in analyses_written`.
- **run-row-lists-stored-only**: DB row's `analyses` column == `["rms"]`.

#### Test: dsp-audio-load-failure-no-row (covers R-D7)

**Given**: Patched `load_audio` raises `ValueError`.

**When**: Handler runs.

**Then**:
- **error-returned**: `error` mentions "failed to load audio".
- **no-run-row**: no `dsp_runs` row inserted.

#### Test: peaks-ffmpeg-missing-raises (covers R-P7)

**Given**: `subprocess.Popen` raises `FileNotFoundError` (ffmpeg not on PATH).

**When**: `compute_peaks` runs with `duration > 0`.

**Then**:
- **runtime-error-raised**: `RuntimeError` with message starting "ffmpeg not found".
- **no-cache-file**: no `.f16` file written.

#### Test: peaks-ffmpeg-nonzero-exit-raises (covers R-P8)

**Given**: ffmpeg exits with non-zero rc and writes nothing to stdout (e.g. corrupt input).

**When**: `compute_peaks` runs.

**Then**:
- **runtime-error**: `RuntimeError("ffmpeg rc=<rc>: <stderr snippet>")` raised.

#### Test: peaks-ffmpeg-timeout-kills-and-raises (covers R-P9)

**Given**: Patched subprocess hangs past 60s (or patch `_TIMEOUT` shorter for tests).

**When**: `compute_peaks` runs.

**Then**:
- **proc-killed**: `.kill()` was invoked on the process.
- **runtime-error-timeout**: `RuntimeError("ffmpeg timed out during peak decode")` raised.

#### Test: peaks-cache-write-failure-still-returns (covers R-P10)

**Given**: Patched `Path.write_bytes` raises `OSError("disk full")`.

**When**: `compute_peaks` runs on a cache miss.

**Then**:
- **bytes-returned**: non-empty bytes still returned.
- **no-raise**: no exception propagates.

#### Test: peaks-mtime-bump-busts-cache (covers R-P3)

**Given**: A cached `.f16` file from an earlier decode.

**When**: The source file's mtime is changed and `compute_peaks` is called again.

**Then**:
- **new-cache-key**: a different `.f16` filename is written.
- **old-cache-untouched**: the prior cache file still exists on disk.

#### Test: bounce-reads-32bit-float-via-soundfile (covers R-B19)

**Given**: WAV on disk is 32-bit float; `wave.open` raises.

**When**: Handler reads duration.

**Then**:
- **duration-from-soundfile**: `duration_s` equals `soundfile.info(...).duration`.
- **update-called**: `update_bounce_rendered` still invoked with computed duration.

#### Test: bounce-uuid-collision-is-rejected (negative — concurrency)

**Given**: Two concurrent bounce calls with overlapping `composite_hash` but different `request_id`s.

**When**: Both run.

**Then**:
- **distinct-events-registered**: `_BOUNCE_RENDER_EVENTS` holds two distinct keys simultaneously.
- **no-cross-release**: `set_bounce_render_event(id_a)` does NOT release the waiter on `id_b`.

#### Test: analyze-rms-datapoints-non-negative (positive — analyzer correctness)

**Given**: A WAV with known RMS envelope.

**When**: `analyses=["rms"]`.

**Then**:
- **all-datapoints-nonneg**: every persisted `rms` datapoint has `value >= 0`.

#### Test: analyze-clipping-merge-threshold (covers R-M17 — clipping_detect edge)

**Given**: A WAV with two clipping runs separated by 5ms (< 10ms merge gap).

**When**: `analyses=["clipping_detect"]`.

**Then**:
- **merged-into-one**: exactly one `clipping_event` section persisted.
- **clip-count-scalar**: `scalars["clip_count"] == 1`.

#### Test: descriptions-energy-clamp-boundary (covers R-G10 edge)

**Given**: Dicts with `energy=-5`, `energy=5`, `energy=0`, `energy=1`.

**When**: `_rows_from_description` called.

**Then**:
- **clamped-low**: `-5 → 0.0`.
- **clamped-high**: `5 → 1.0`.
- **identity-zero**: `0 → 0.0`.
- **identity-one**: `1 → 1.0`.

#### Test: no-concurrency-in-handlers (negative — architectural)

**Given**: The four `_exec_*` handlers in `chat.py`.

**When**: Inspected.

**Then**:
- **no-threading-primitives**: no `threading.Thread`, no `multiprocessing`, no `concurrent.futures` usage inside handlers.
- **async-only-where-needed**: only `_exec_bounce_audio` and `_exec_analyze_master_bus` are `async def`; DSP and descriptions handlers are synchronous.
- **shared-event-dicts-guarded**: `_BOUNCE_RENDER_EVENTS` / `_MIX_RENDER_EVENTS` are accessed only from the single asyncio loop; cross-thread release uses `set_*_render_event` which simply calls `.set()`.

---

## Non-Goals

- Defining hashing algorithms (`compute_mix_graph_hash`, `compute_bounce_hash`, `_dsp_params_hash`). These are separate pure-function specs.
- Defining the DB table schemas (`audio_bounces`, `mix_runs`, `dsp_runs`, `audio_description_runs`, etc.). Specified in db-module specs.
- Defining the MCP tool JSON-schema / parameter validation layer around these handlers.
- Frontend-side OfflineAudioContext rendering and WAV-encoding correctness.
- Multipart upload internals (boundary parsing, WAV header cross-check, 64-hex validation) — covered by the file-serving/upload spec.
- Gemini API authentication, quota, retry policy — upstream of `_gemini_describe_chunk_structured`.
- `pool_segments` resolution details (source of truth for `pool_path`) — covered by the segment-management spec.

---

## Open Questions

### OQ-1: Late upload after timeout (orphaned WAV, leaked row?)

When `_exec_bounce_audio` times out at T+60s it deletes the bounce row AND removes the `_BOUNCE_RENDER_EVENTS` entry. If the frontend upload then lands at T+61s, the `/bounce-upload` handler writes `pool/bounces/<composite_hash>.wav` to disk but `set_bounce_render_event(request_id)` returns `False` (no matching event). The WAV is now an orphan: not tied to any `audio_bounces` row.

- Should the upload handler check `audio_bounces` by `composite_hash` and, if no row exists, reject the upload (delete the file)?
- Or should it re-insert a completed `audio_bounces` row so the next cache lookup returns it?
- Same question applies to `/mix-render-upload` — but the mix path doesn't insert a row until the WAV exists, so only the WAV is orphaned (no DB leak).

### OQ-2: composite_hash cache hit but file on disk missing

`_exec_bounce_audio` trusts the DB: if a row has `rendered_path IS NOT NULL`, it returns `{cached:True}` without statting the file. If an admin / cleanup process deletes `pool/bounces/<hash>.wav` but leaves the row, the cached payload's `download_url` serves a 404.

- Should cache-hit validation also stat the file?
- `_exec_analyze_master_bus` DOES stat the file (it re-renders via WS if missing) — should bounce mirror that?

### OQ-3: librosa raises mid-analysis in `analyze_master_bus` without an inner try/except (`rms`, `peak`, `clipping_detect`)

The inner try/except wraps only `true_peak`, `lufs`, `spectral_centroid`. If `rms`, `peak`, `clipping_detect`, or `dynamic_range`'s inner computations raise, the outer `except Exception` deletes the run row and returns `{"error": "analysis failed: ..."}`. But:

- Any datapoints / sections / scalars already accumulated in earlier loop iterations are lost (not persisted, row deleted).
- Should partial results be persisted when a later analysis fails, or is "all-or-nothing per run" the intended contract?
- If "all-or-nothing", should we move ALL analyses into their own inner try/except to match `true_peak`/`lufs`/`spectral_centroid`?

Analogous hole exists in `_exec_generate_dsp`: `rms`, `onsets`, `vocal_presence` have no inner try/except, so a librosa failure there propagates unhandled (violating R-D16 which says "never raises").

### OQ-4: Gemini rate limit mid-chunk

`_exec_generate_descriptions` treats `None` from `_gemini_describe_chunk_structured` as a per-chunk failure. But if Gemini raises a `google.api_core.exceptions.ResourceExhausted` (or equivalent) that propagates out of `_gemini_describe_chunk_structured`, the exception bubbles out of `_exec_generate_descriptions` — violating R-G14.

- Should `_exec_generate_descriptions` wrap each per-chunk call in `try/except`?
- Should rate-limit errors short-circuit the whole run (return partial error), or treat them as chunk failures and continue?

### OQ-5: Concurrent peaks request for same clip (file-cache write race)

`compute_peaks` is not locked. Two concurrent callers with a cache miss both spawn ffmpeg and both call `cache_file.write_bytes(data)`. Because the bytes are deterministic, the last writer wins with identical content — but:

- POSIX `write_bytes` opens with `O_WRONLY|O_CREAT|O_TRUNC`, so a reader could see a zero-byte or partial file during the window.
- Should we write to a temp file + `os.rename` (atomic) for safety?

### OQ-6: WS closes mid-wait

Symptom is identical to R-B16 / R-M12 (timeout). But the WS close is detectable — the engine could short-circuit the wait and return an error earlier.

- Should `_exec_*` observe the WS state and fail fast on close?

### OQ-7: Source file mutating during analysis

`_exec_generate_dsp` / `_exec_generate_descriptions` resolve and read `pool_path` without a file lock. If another process is writing the file (mid-extraction of a pool_segment), librosa may read a truncated buffer.

- Is this possible in practice (writes are atomic via os.rename)?
- If not atomic, should we hold a shared lock for the duration of analysis?

---

## Related Artifacts

- **Frontend spec**: `../scenecraft/agent/specs/local.bounce-and-analysis.md` (UX, elicitation, OfflineAudioContext rendering, upload client)
- **Engine audit-2 §1E**: audit reference on analysis handlers (caller-supplied context)
- **Source files**:
  - `src/scenecraft/chat.py` — `_exec_bounce_audio`, `_exec_analyze_master_bus`, `_exec_generate_dsp`, `_exec_generate_descriptions`
  - `src/scenecraft/audio/peaks.py` — `compute_peaks`
  - `src/scenecraft/api_server.py` — `/bounce-upload`, `/mix-render-upload`, `/audio-clips/:id/peaks`, `/pool/:seg_id/peaks`
  - `src/scenecraft/bounce_hash.py`, `src/scenecraft/mix_graph_hash.py` — hashing (out of scope)
  - `src/scenecraft/db_analysis_cache.py`, `src/scenecraft/db_mix_cache.py`, `src/scenecraft/db_bounces.py` — cache-table accessors

---

**Namespace**: local
**Spec**: engine-analysis-handlers
**Version**: 1.0.0
**Created**: 2026-04-27
**Last Updated**: 2026-04-27
**Status**: Active
