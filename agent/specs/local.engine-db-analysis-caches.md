# Spec: Engine DB — Analysis Cache Tables

**Namespace**: local
**Version**: 1.0.0
**Created**: 2026-04-27
**Last Updated**: 2026-04-27
**Status**: Ready for Proofing

---

**Purpose**: Black-box specification for the five analysis cache systems that persist expensive audio-analysis, rendered-audio, and waveform-peak artifacts inside a scenecraft-engine project. Covers the four DB-backed caches (DSP, mix, audio-description, bounce) and the one filesystem-backed cache (waveform peaks). This spec fixes the cache-key shapes, uniqueness guarantees, cache-hit / cache-miss / force-rerun semantics, analyzer_version pattern, and transaction discipline around bulk-insert. Implementation details of the *producers* (librosa, pyloudnorm, Gemini, OfflineAudioContext round-trip) are intentionally black-boxed — they belong in the `engine-analysis-handlers` spec.

**Source**: `--from-draft` — user-authored prompt plus references to:
- `src/scenecraft/db.py:745–910` (DDL for cache tables)
- `src/scenecraft/db_analysis_cache.py` (DSP + audio-description DAL)
- `src/scenecraft/db_mix_cache.py` (mix DAL)
- `src/scenecraft/db_bounces.py` (bounce DAL)
- `src/scenecraft/audio/peaks.py` (waveform-peaks filesystem cache)
- `src/scenecraft/chat.py:3036` (`_exec_analyze_master_bus`) and `:3422` (`_exec_bounce_audio`) — the two on-demand producers
- `agent/reports/audit-2-architectural-deep-dive.md §1C` (DB Schema + DAL + Migrations), `§1E` (Bounce + Analysis)

---

## Scope

### In-Scope

- The **five cache systems** and their cache-key shapes, uniqueness rules, and lookup semantics:
  1. **DSP analysis cache** — `dsp_analysis_runs` + `dsp_datapoints` + `dsp_sections` + `dsp_scalars`. Key: `(source_segment_id, analyzer_version, params_hash)` (3-tuple).
  2. **Mix analysis cache** — `mix_analysis_runs` + `mix_datapoints` + `mix_sections` + `mix_scalars`. Key: `(mix_graph_hash, start_time_s, end_time_s, sample_rate, analyzer_version)` (5-tuple).
  3. **Audio-description cache** — `audio_description_runs` + `audio_descriptions` + `audio_description_scalars`. Key: `(source_segment_id, model, prompt_version)` (3-tuple).
  4. **Bounce cache** — `audio_bounces`. Key: `composite_hash` (single SHA-256 hex derived from mix_graph_hash + selection + format).
  5. **Waveform-peaks cache** — filesystem-only, at `<project_dir>/audio_staging/.peaks/<key>.f16`. Key: SHA-1 of `(resolved_source_path, stat.st_mtime_ns, stat.st_size, source_offset, duration, resolution)` truncated to 16 hex chars.
- UNIQUE constraints at the table level, parent→child FK cascade deletes, and (run_id, …) primary keys on datapoints / sections / scalars.
- The `analyzer_version` pattern — how it namespaces cached rows so a library upgrade reads as a cache miss rather than stale data.
- The `force_rerun` bypass — how it turns a cache hit into a cache-miss-plus-delete-existing-row.
- Persistence transaction discipline for producers: the run row is created *before* analyses run, child rows (datapoints / sections / scalars) are inserted *after* analyses complete, and the partial run row MUST be deleted on mid-flight failure.
- Deletion semantics: parent row delete cascades to children; orphan children are impossible by FK.
- Cache durability: rows persist forever by default; there is no TTL, no LRU, and no bounded size. (See OQ-4.)
- Hash collision posture: cache keys treat hash equality as identity; no content verification. (See OQ-2.)

### Out-of-Scope (Non-Goals)

- Handler behavior (input validation, WS round-trip for `mix_render_request` / `bounce_audio_request`, error codes, timeouts, concurrency around `_MIX_RENDER_EVENTS` / `_BOUNCE_RENDER_EVENTS`) — covered by `local.engine-analysis-handlers.md` (companion spec).
- Frontend bounce UX (download URL format, "downloads" panel, toast messaging) — already specced.
- DDL migrations to *add* caches; this spec describes the steady-state shape. Migration sequencing lives in `local.engine-db-migrations.md` (TBD).
- `mix_graph_hash`, `params_hash`, and `composite_hash` input composition — those are specced alongside their respective producers (`mix_graph_hash.py`, `bounce_hash.py`). This spec treats them as opaque strings.
- DSP / mix / description / bounce *semantics* (what "rms", "lufs_integrated", "clipping_event" actually mean). This spec treats analysis names and metric names as opaque identifiers.
- Concurrent writes across processes. The engine assumes a single process per project; cache correctness under multi-process writes is undefined (see OQ-5).

---

## Requirements

### Table Shapes (Identity + Uniqueness)

- **R1** — `dsp_analysis_runs` has `PRIMARY KEY (id)` and `UNIQUE (source_segment_id, analyzer_version, params_hash)`. Two rows with the same 3-tuple MUST NOT coexist.
- **R2** — `mix_analysis_runs` has `PRIMARY KEY (id)` and `UNIQUE (mix_graph_hash, start_time_s, end_time_s, sample_rate, analyzer_version)`. Two rows with the same 5-tuple MUST NOT coexist.
- **R3** — `audio_description_runs` has `PRIMARY KEY (id)` and `UNIQUE (source_segment_id, model, prompt_version)`. Two rows with the same 3-tuple MUST NOT coexist.
- **R4** — `audio_bounces` has `PRIMARY KEY (id)` and `UNIQUE (composite_hash)`. Two rows with the same composite_hash MUST NOT coexist.
- **R5** — Child tables (`dsp_datapoints`, `dsp_sections`, `dsp_scalars`, `mix_datapoints`, `mix_sections`, `mix_scalars`, `audio_descriptions`, `audio_description_scalars`) have `run_id REFERENCES <parent>(id) ON DELETE CASCADE`. Deleting the parent MUST remove every child row.
- **R6** — Every child table's primary key includes `run_id` plus the discriminating columns (`data_type + time_s`, `start_s + section_type`, `metric`, `start_s + property`, `property`). The same `(run_id, discriminator…)` tuple MUST NOT coexist on two rows.

### Cache Lookup Semantics

- **R7** — DSP lookup: `get_dsp_run(source_segment_id, analyzer_version, params_hash)` returns either the unique matching row or `None`. Never a list.
- **R8** — Mix lookup: `get_mix_run(mix_graph_hash, start, end, sample_rate, analyzer_version)` returns either the unique matching row or `None`.
- **R9** — Audio-description lookup: `get_audio_description_run(source_segment_id, model, prompt_version)` returns either the unique matching row or `None`.
- **R10** — Bounce lookup: `get_bounce_by_hash(composite_hash)` returns either the unique matching row or `None`.
- **R11** — Cache hits return the previously persisted row verbatim. The caller MUST NOT need to re-run any analysis to materialize the response from a cached row plus its children.

### `analyzer_version` Pattern

- **R12** — `analyzer_version` is an opaque identifier chosen by the producer that captures the exact analysis implementation. The canonical shape is `<analyzer-name>-librosa-<librosa.__version__>` (e.g. `mix-librosa-0.10.2`), but the spec treats it as an opaque string.
- **R13** — A change in `analyzer_version` MUST NOT collide with any existing cached row. It MUST produce a cache miss on lookup, and the new analysis run MUST be persisted as a new row alongside (not replacing) the older row.
- **R14** — Old rows with a prior `analyzer_version` MUST remain readable; nothing in the cache system automatically evicts them when `analyzer_version` changes.

### `force_rerun` Bypass

- **R15** — The DSP and mix producers accept a `force_rerun` boolean input (default `False`). The audio-description and bounce producers have functionally equivalent behavior (audio-description: new `prompt_version` creates a new row; bounce: pending rows without `rendered_path` are deleted-then-recreated).
- **R16** — When `force_rerun=True` and a cache row exists, the producer MUST delete the existing row (cascading its children) BEFORE creating a new row. This guarantees the UNIQUE constraint does not reject the new row.
- **R17** — When `force_rerun=False` and a cache row exists, the producer MUST return the cached row without running analysis.
- **R18** — `force_rerun=True` on a cache *miss* MUST behave identically to `force_rerun=False` on a cache miss (no row to delete; run normally).

### Transaction Discipline (Producer Contract)

- **R19** — The producer MUST create the run row *before* executing analyses, with `rendered_path=None` or equivalent "in-flight" marker. This lets concurrent lookups observe that a run is in progress (though this spec does not guarantee ordering between concurrent producers — see OQ-5).
- **R20** — On successful analysis completion, the producer MUST bulk-insert datapoints, sections, and scalars, then update `rendered_path` (mix + bounce) or `analyses_json` (DSP) as the final step.
- **R21** — On exception during analysis (mid-flight), the producer MUST delete the run row it created (cascading any partially inserted children) and return an error. A partial run row MUST NOT persist in the cache after a failed analysis.
- **R22** — The producer MUST NOT leave a row whose run_id has zero children AND whose `rendered_path` is null AND whose creator is no longer running. (Restated: partial rows from crashed producers are out of scope for the steady-state spec — see OQ-3.)
- **R23** — `INSERT OR REPLACE` is used for bulk child-row inserts, so repeating a child-row insert with the same discriminator is idempotent and MUST NOT produce a UNIQUE constraint error.

### Waveform-Peaks Filesystem Cache

- **R24** — The peaks cache lives at `<project_dir>/audio_staging/.peaks/<key>.f16`. The directory is auto-created on first write.
- **R25** — Cache key: `sha1(f"{source_path.resolve()}|{stat.st_mtime_ns}|{stat.st_size}|{source_offset:.6f}|{duration:.6f}|{resolution}").hexdigest()[:16]`. If the source file is edited, its `st_mtime_ns` or `st_size` changes and the key changes, producing a cache miss.
- **R26** — Cache hit: `compute_peaks(...)` MUST return the cached bytes verbatim without re-decoding audio.
- **R27** — Cache miss: `compute_peaks(...)` decodes the audio slice via ffmpeg, computes the peaks, writes the file, and returns the bytes.
- **R28** — If the cache write fails (disk full, permissions), `compute_peaks` MUST still return the computed bytes to the caller and log a warning. The failed write MUST NOT propagate as an exception.
- **R29** — The peaks cache has no DB row and is not cascaded by any DB delete. It is only invalidated by source-file mtime/size change. There is no explicit purge API (see OQ-6).

### Rendered-File Sidecar (Mix + Bounce)

- **R30** — The mix cache stores its rendered WAV at `<project_dir>/pool/mixes/<mix_graph_hash>.wav`. The DB row's `rendered_path` is the relative path `pool/mixes/<mix_graph_hash>.wav` or `NULL` if not yet uploaded.
- **R31** — The bounce cache stores its rendered WAV at `<project_dir>/pool/bounces/<composite_hash>.wav`. The DB row's `rendered_path` is the relative path `pool/bounces/<composite_hash>.wav` or `NULL` if not yet uploaded.
- **R32** — A cache *row* whose `rendered_path IS NULL` is treated by the bounce producer as "pending / failed" and is deleted before retry. A cache *row* whose `rendered_path` is set but whose WAV file is missing on disk is **undefined** — see OQ-7.

---

## Interfaces / Data Shapes

### DDL (authoritative)

```sql
CREATE TABLE dsp_analysis_runs (
    id TEXT PRIMARY KEY,
    source_segment_id TEXT NOT NULL REFERENCES pool_segments(id) ON DELETE CASCADE,
    analyzer_version TEXT NOT NULL,
    params_hash TEXT NOT NULL,
    analyses_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(source_segment_id, analyzer_version, params_hash)
);
-- Children (cascade on parent delete):
CREATE TABLE dsp_datapoints (run_id, data_type, time_s, value, extra_json,
    PRIMARY KEY (run_id, data_type, time_s));
CREATE TABLE dsp_sections (run_id, start_s, end_s, section_type, label, confidence,
    PRIMARY KEY (run_id, start_s, section_type));
CREATE TABLE dsp_scalars (run_id, metric, value,
    PRIMARY KEY (run_id, metric));

CREATE TABLE mix_analysis_runs (
    id TEXT PRIMARY KEY,
    mix_graph_hash TEXT NOT NULL,
    start_time_s REAL NOT NULL,
    end_time_s REAL NOT NULL,
    sample_rate INTEGER NOT NULL,
    analyzer_version TEXT NOT NULL,
    analyses_json TEXT NOT NULL,
    rendered_path TEXT,  -- nullable; path to WAV under pool/mixes/
    created_at TEXT NOT NULL,
    UNIQUE(mix_graph_hash, start_time_s, end_time_s, sample_rate, analyzer_version)
);
-- Children: mix_datapoints, mix_sections, mix_scalars — same shape as DSP.

CREATE TABLE audio_description_runs (
    id TEXT PRIMARY KEY,
    source_segment_id TEXT NOT NULL REFERENCES pool_segments(id) ON DELETE CASCADE,
    model TEXT NOT NULL,
    prompt_version TEXT NOT NULL,
    chunk_size_s REAL NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(source_segment_id, model, prompt_version)
);
-- Children: audio_descriptions (time-ranged properties), audio_description_scalars
--   (segment-global properties). Both cascade.

CREATE TABLE audio_bounces (
    id TEXT PRIMARY KEY,
    composite_hash TEXT NOT NULL UNIQUE,
    start_time_s REAL NOT NULL,
    end_time_s REAL NOT NULL,
    mode TEXT NOT NULL,            -- "full" | "tracks" | "clips"
    selection_json TEXT NOT NULL,  -- {} | {track_ids:[]} | {clip_ids:[]}
    sample_rate INTEGER NOT NULL,
    bit_depth INTEGER NOT NULL,
    channels INTEGER NOT NULL DEFAULT 2,
    rendered_path TEXT,            -- nullable; pool/bounces/<hash>.wav
    size_bytes INTEGER,
    duration_s REAL,
    created_at TEXT NOT NULL
);
-- No child tables. Bounce is a leaf artifact row.
```

### DAL Surface (authoritative; Python — `scenecraft.db_analysis_cache`, `scenecraft.db_mix_cache`, `scenecraft.db_bounces`)

```python
# DSP
get_dsp_run(project_dir, source_segment_id, analyzer_version, params_hash)
    -> DspAnalysisRun | None
create_dsp_run(project_dir, source_segment_id, analyzer_version, params_hash,
               analyses, created_at) -> DspAnalysisRun
list_dsp_runs(project_dir, source_segment_id) -> list[DspAnalysisRun]
delete_dsp_run(project_dir, run_id) -> None   # cascades children
bulk_insert_dsp_datapoints(project_dir, run_id, datapoints) -> int
bulk_insert_dsp_sections(project_dir, run_id, sections) -> int
set_dsp_scalars(project_dir, run_id, scalars: dict[str, float]) -> None
query_dsp_datapoints(project_dir, run_id, data_type=None) -> list[DspDatapoint]
query_dsp_sections(project_dir, run_id, section_type=None) -> list[DspSection]
get_dsp_scalars(project_dir, run_id) -> dict[str, float]

# Audio description (same shape + model/prompt_version key)
get_audio_description_run(project_dir, source_segment_id, model, prompt_version)
    -> AudioDescriptionRun | None
create_audio_description_run(...), list_audio_description_runs(...),
delete_audio_description_run(...), bulk_insert_audio_descriptions(...),
query_audio_descriptions(...), set_audio_description_scalars(...),
get_audio_description_scalars(...)

# Mix (5-tuple key + rendered_path)
get_mix_run(project_dir, mix_graph_hash, start, end, sample_rate, analyzer_version)
    -> MixAnalysisRun | None
create_mix_run(project_dir, *, mix_graph_hash, start_s, end_s, sample_rate,
               analyzer_version, analyses, rendered_path, created_at)
    -> MixAnalysisRun
update_mix_run_rendered_path(project_dir, run_id, rendered_path) -> None
list_mix_runs_for_hash(project_dir, mix_graph_hash) -> list[MixAnalysisRun]
delete_mix_run(project_dir, run_id) -> None   # cascades children
bulk_insert_mix_datapoints(...), bulk_insert_mix_sections(...),
set_mix_scalars(...), query_mix_datapoints(...), query_mix_sections(...),
get_mix_scalars(...)

# Bounce (no children; single row per composite_hash)
get_bounce_by_hash(project_dir, composite_hash) -> AudioBounce | None
get_bounce_by_id(project_dir, bounce_id) -> AudioBounce | None
create_bounce(project_dir, *, composite_hash, start_time_s, end_time_s, mode,
              selection, sample_rate, bit_depth, channels) -> AudioBounce
update_bounce_rendered(project_dir, bounce_id, rendered_path, size_bytes,
                       duration_s) -> None
delete_bounce(project_dir, bounce_id) -> None
list_bounces(project_dir) -> list[AudioBounce]
```

### Filesystem-Peaks Surface (`scenecraft.audio.peaks`)

```python
compute_peaks(source_path: Path, source_offset: float, duration: float,
              resolution: int = 400, project_dir: Path | None = None) -> bytes
# Cache location: <project_dir>/audio_staging/.peaks/<key>.f16
# Cache key:      sha1("{abs_path}|{mtime_ns}|{size}|{off:.6f}|{dur:.6f}|{res}")[:16]
# Returns float16 LE bytes, one absolute peak per bucket.
```

---

## Behavior Table

| # | Scenario | Expected Behavior | Tests |
|---|----------|-------------------|-------|
| 1 | DSP lookup, no matching row | Returns `None` | `dsp-lookup-miss-returns-none` |
| 2 | DSP lookup, matching row | Returns the unique row | `dsp-lookup-hit-returns-row` |
| 3 | DSP insert duplicate 3-tuple | Raises UNIQUE constraint error | `dsp-duplicate-key-rejected` |
| 4 | DSP delete run | Cascades; datapoints, sections, scalars are gone | `dsp-delete-cascades-children` |
| 5 | Mix lookup, matching 5-tuple | Returns the unique row | `mix-lookup-hit-returns-row` |
| 6 | Mix lookup, different `start_time_s` | Returns `None` (cache miss) | `mix-lookup-miss-on-window-change` |
| 7 | Mix lookup, different `sample_rate` | Returns `None` (cache miss) | `mix-lookup-miss-on-sample-rate-change` |
| 8 | Mix lookup, different `analyzer_version` | Returns `None`; old row still exists | `mix-analyzer-version-miss-keeps-old-row` |
| 9 | Mix insert duplicate 5-tuple | Raises UNIQUE constraint error | `mix-duplicate-key-rejected` |
| 10 | Mix delete run | Cascades children | `mix-delete-cascades-children` |
| 11 | Audio-description lookup, matching 3-tuple | Returns the unique row | `desc-lookup-hit-returns-row` |
| 12 | Audio-description new `prompt_version` | Returns `None`; old row still exists (A/B) | `desc-prompt-version-miss-keeps-old-row` |
| 13 | Audio-description duplicate 3-tuple | Raises UNIQUE constraint error | `desc-duplicate-key-rejected` |
| 14 | Audio-description delete run | Cascades time-ranged + scalar children | `desc-delete-cascades-children` |
| 15 | Bounce lookup by composite_hash, hit | Returns the unique row | `bounce-lookup-hit-returns-row` |
| 16 | Bounce insert duplicate composite_hash | Raises UNIQUE constraint error | `bounce-duplicate-hash-rejected` |
| 17 | Bounce delete row | Row is gone; WAV file is NOT removed | `bounce-delete-does-not-remove-wav` |
| 18 | Bounce row exists with `rendered_path IS NULL` | Treated as pending/failed; producer deletes + retries | `bounce-null-rendered-path-triggers-retry` |
| 19 | `force_rerun=True` on cache hit (DSP / mix) | Old row + children deleted; new row created | `force-rerun-deletes-then-creates` |
| 20 | `force_rerun=True` on cache miss | Behaves identically to `force_rerun=False` miss | `force-rerun-on-miss-is-noop-plus-run` |
| 21 | `force_rerun=False` on cache hit | Returns cached row; no new row; no analysis run | `default-rerun-returns-cached` |
| 22 | Producer exception mid-analysis | In-flight run row deleted; no partial children persist | `producer-exception-deletes-partial-row` |
| 23 | Bulk-insert datapoints with duplicate `(run_id, data_type, time_s)` | `INSERT OR REPLACE` overwrites; no UNIQUE error | `bulk-insert-is-idempotent` |
| 24 | Empty datapoints list to `bulk_insert_*` | Returns 0; no SQL executed | `bulk-insert-empty-is-noop` |
| 25 | Parent `pool_segments` row deleted | DSP + description runs cascade away via FK | `pool-segment-delete-cascades-to-runs` |
| 26 | Peaks cache hit | Returns bytes from file; no ffmpeg invocation | `peaks-cache-hit-skips-decode` |
| 27 | Peaks cache miss | Decodes via ffmpeg; writes file; returns bytes | `peaks-cache-miss-decodes-and-writes` |
| 28 | Peaks: source file edited (mtime changes) | New key → cache miss; old `.f16` lingers | `peaks-source-edit-invalidates-key` |
| 29 | Peaks: cache write fails | Bytes still returned; warning logged; no exception | `peaks-cache-write-failure-is-non-fatal` |
| 30 | `list_bounces` / `list_*_runs` on empty DB | Returns `[]`, not `None` | `list-on-empty-returns-empty-list` |
| 31 | Concurrent `create_dsp_run` with same key, two callers | **undefined** | → [OQ-5](#open-questions) |
| 32 | Librosa version downgrade — old analyzer_version now unreachable | **undefined** (cost of immutability) | → [OQ-1](#open-questions) |
| 33 | SHA-256 collision on `composite_hash` (or SHA-1 on peaks key) | **undefined** (no policy) | → [OQ-2](#open-questions) |
| 34 | Process crash between `create_mix_run` and child inserts | **undefined** (partial row lingers) | → [OQ-3](#open-questions) |
| 35 | Cache grows unbounded over project lifetime | **undefined** (no TTL / LRU) | → [OQ-4](#open-questions) |
| 36 | Mix row has `rendered_path` set but WAV is missing on disk | **undefined** (no sweep) | → [OQ-7](#open-questions) |
| 37 | Peaks cache orphan files after source deletion | **undefined** (no purge) | → [OQ-6](#open-questions) |

---

## Behavior

### Cache Lookup (all DB caches)

1. Caller calls `get_*_run(...)` (or `get_bounce_by_hash(...)`) with the full cache key.
2. DAL executes a single `SELECT * FROM <table> WHERE <key columns> = ?` against the project's SQLite connection.
3. If one row matches: map via `_row_to_*` and return the model. Children are not pre-loaded; caller fetches them via separate `query_*` / `get_*_scalars` calls when needed.
4. If zero rows match: return `None`.
5. Two rows matching the same key is impossible by R1–R4 (UNIQUE) and is not handled defensively.

### Cache Miss with Force-Rerun

1. Producer performs cache lookup.
2. If existing row AND `force_rerun=True`: `delete_*_run(existing.id)` (cascades children); proceed as miss.
3. Producer creates new run row via `create_*_run(...)` with `rendered_path=None` / `created_at=<now>`.
4. Producer runs the analysis pipeline (librosa / pyloudnorm / Gemini / OfflineAudioContext). This spec treats that as opaque.
5. On success: `bulk_insert_*_datapoints(...)`, `bulk_insert_*_sections(...)`, `set_*_scalars(...)`, then (mix / bounce only) `update_*_rendered_path(...)` / `update_bounce_rendered(...)`.
6. On exception inside step 4 or 5: `delete_*_run(run.id)`. Return error.

### Peaks Cache

1. Caller calls `compute_peaks(source_path, offset, duration, resolution, project_dir)`.
2. If `project_dir` is provided: compute the SHA-1 cache key from the tuple in R25; check `<project_dir>/audio_staging/.peaks/<key>.f16`.
3. If the file exists: return its bytes.
4. Else: ffmpeg-decode the source slice, compute bucketed peaks as float16 LE, attempt to write the file.
5. If the write throws: log warning, continue. Return the computed bytes regardless.

---

## Acceptance Criteria

- [ ] Every DDL constraint in the "DDL (authoritative)" block is present in `db.py::_ensure_schema()` (verified against the current `db.py:749–910` block).
- [ ] Every DAL function in the "DAL Surface" block is exported from its module and has a docstring referencing its table set.
- [ ] Every test in the Tests section below passes against a fresh ephemeral SQLite project DB.
- [ ] `DspAnalysisRun`, `MixAnalysisRun`, `AudioDescriptionRun`, `AudioBounce` model classes (in `db_models.py`) mirror the DDL columns 1:1.
- [ ] The two producers `_exec_analyze_master_bus` and `_exec_bounce_audio` call `delete_*` on mid-flight exception paths (code-review acceptance; see R21).
- [ ] `compute_peaks` still returns bytes when the cache-file write raises (negative test — see `peaks-cache-write-failure-is-non-fatal`).
- [ ] Every row in the Behavior Table maps to at least one named test below (or to an Open Question for `undefined` rows).

---

## Tests

### Base Cases

The core behavior contract: cache-hit and cache-miss semantics for each of the five systems, UNIQUE enforcement, cascade on delete, `force_rerun` bypass, and transaction discipline around producer failures.

#### Test: dsp-lookup-miss-returns-none (covers R7)

**Given**: A project DB with zero rows in `dsp_analysis_runs`.
**When**: `get_dsp_run(project_dir, "seg-1", "mix-librosa-0.10.2", "phash-A")` is called.
**Then**:
- **returns-none**: The return value is `None`.
- **no-children-queried**: No row is written to any child table (`dsp_datapoints`, `dsp_sections`, `dsp_scalars`).

#### Test: dsp-lookup-hit-returns-row (covers R7, R11)

**Given**: One row in `dsp_analysis_runs` with `(source_segment_id="seg-1", analyzer_version="v1", params_hash="p1")`, and some associated datapoints.
**When**: `get_dsp_run(project_dir, "seg-1", "v1", "p1")` is called.
**Then**:
- **returns-row**: The return value is a `DspAnalysisRun` whose fields match the stored row verbatim.
- **no-analysis-run**: No librosa call occurs as a side effect of the lookup.

#### Test: dsp-duplicate-key-rejected (covers R1)

**Given**: A row already exists in `dsp_analysis_runs` with `(source_segment_id="seg-1", analyzer_version="v1", params_hash="p1")`.
**When**: A second `create_dsp_run(project_dir, "seg-1", "v1", "p1", analyses=[...], created_at=...)` is attempted.
**Then**:
- **unique-error-raised**: A SQLite `IntegrityError` (or the DAL's normalized equivalent) is raised and references the UNIQUE constraint.
- **no-new-row**: `list_dsp_runs(project_dir, "seg-1")` still returns exactly one row.

#### Test: dsp-delete-cascades-children (covers R5)

**Given**: A DSP run with 10 datapoints, 2 sections, and 3 scalars.
**When**: `delete_dsp_run(project_dir, run_id)` is called.
**Then**:
- **parent-gone**: `get_dsp_run(...)` for the key returns `None`.
- **datapoints-gone**: `query_dsp_datapoints(project_dir, run_id)` returns `[]`.
- **sections-gone**: `query_dsp_sections(project_dir, run_id)` returns `[]`.
- **scalars-gone**: `get_dsp_scalars(project_dir, run_id)` returns `{}`.

#### Test: mix-lookup-hit-returns-row (covers R8, R11)

**Given**: One row in `mix_analysis_runs` with the 5-tuple `(hash="h1", 0.0, 30.0, 48000, "v1")`.
**When**: `get_mix_run(project_dir, "h1", 0.0, 30.0, 48000, "v1")` is called.
**Then**:
- **returns-row**: The return value is the stored `MixAnalysisRun` with all columns matching verbatim.

#### Test: mix-lookup-miss-on-window-change (covers R2, R8)

**Given**: One row with `(mix_graph_hash="h1", start=0.0, end=30.0, sample_rate=48000, analyzer_version="v1")`.
**When**: `get_mix_run(project_dir, "h1", 0.0, 45.0, 48000, "v1")` is called (different `end`).
**Then**:
- **returns-none**: The return value is `None`.
- **original-row-untouched**: `get_mix_run(project_dir, "h1", 0.0, 30.0, 48000, "v1")` still returns the original row.

#### Test: mix-lookup-miss-on-sample-rate-change (covers R2, R8)

**Given**: One row with `sample_rate=48000`.
**When**: `get_mix_run(...)` is called with `sample_rate=44100`, all other key columns identical.
**Then**:
- **returns-none**: The return value is `None`.

#### Test: mix-analyzer-version-miss-keeps-old-row (covers R13, R14)

**Given**: One row with `analyzer_version="mix-librosa-0.10.2"`.
**When**: `get_mix_run(...)` is called with `analyzer_version="mix-librosa-0.10.3"`, all other key columns identical.
**Then**:
- **returns-none-on-new-version**: The new-version lookup returns `None`.
- **old-row-still-readable**: The old-version lookup still returns the original row.
- **list-contains-both**: After the producer persists the new-version run, `list_mix_runs_for_hash(project_dir, "h1")` returns both rows.

#### Test: mix-duplicate-key-rejected (covers R2)

**Given**: A row already exists with 5-tuple `("h1", 0.0, 30.0, 48000, "v1")`.
**When**: `create_mix_run(...)` is called with an identical 5-tuple.
**Then**:
- **unique-error-raised**: An `IntegrityError` is raised.

#### Test: mix-delete-cascades-children (covers R5)

**Given**: A mix run with datapoints, sections, and scalars.
**When**: `delete_mix_run(project_dir, run_id)` is called.
**Then**:
- **parent-gone**: `get_mix_run(...)` returns `None` for the key.
- **datapoints-gone**, **sections-gone**, **scalars-gone**: All three child queries return empty.
- **wav-file-not-touched**: The file at `pool/mixes/<mix_graph_hash>.wav` (if any) is NOT removed by this call. (Negative.)

#### Test: desc-lookup-hit-returns-row (covers R9, R11)

**Given**: One row in `audio_description_runs` with `("seg-1", "gemini-1.5", "v2")`.
**When**: `get_audio_description_run(project_dir, "seg-1", "gemini-1.5", "v2")` is called.
**Then**:
- **returns-row**: Returns the stored `AudioDescriptionRun`.

#### Test: desc-prompt-version-miss-keeps-old-row (covers R3, R9, R14)

**Given**: One row with `prompt_version="v2"`.
**When**: `get_audio_description_run(...)` is called with `prompt_version="v3"`.
**Then**:
- **returns-none**: The new-version lookup returns `None`.
- **old-still-readable**: The v2 lookup still returns its row.
- **ab-preserved**: Once the v3 run is persisted, both coexist (A/B comparison).

#### Test: desc-duplicate-key-rejected (covers R3)

**Given**: A row with `("seg-1", "gemini-1.5", "v2")` already exists.
**When**: `create_audio_description_run(...)` is called with the same 3-tuple.
**Then**:
- **unique-error-raised**: An `IntegrityError` is raised.

#### Test: desc-delete-cascades-children (covers R5)

**Given**: A description run with N time-ranged descriptions and M scalar descriptions.
**When**: `delete_audio_description_run(project_dir, run_id)` is called.
**Then**:
- **parent-gone**, **time-ranged-gone**, **scalars-gone**: All three queries return empty.

#### Test: bounce-lookup-hit-returns-row (covers R10)

**Given**: One row in `audio_bounces` with `composite_hash="abc…64hex"` and `rendered_path="pool/bounces/abc….wav"`.
**When**: `get_bounce_by_hash(project_dir, "abc…64hex")` is called.
**Then**:
- **returns-row**: Returns the stored `AudioBounce`.
- **rendered-path-present**: The returned row's `rendered_path` is `"pool/bounces/abc….wav"`.

#### Test: bounce-duplicate-hash-rejected (covers R4)

**Given**: A row with `composite_hash="abc…"` exists.
**When**: `create_bounce(...)` is called with the same `composite_hash`.
**Then**:
- **unique-error-raised**: An `IntegrityError` is raised against the UNIQUE index on `composite_hash`.

#### Test: bounce-delete-does-not-remove-wav (covers R31)

**Given**: A bounce row with `rendered_path="pool/bounces/abc….wav"` and the WAV present on disk.
**When**: `delete_bounce(project_dir, bounce_id)` is called.
**Then**:
- **row-gone**: `get_bounce_by_id(...)` returns `None`.
- **wav-file-untouched**: The WAV file at `pool/bounces/abc….wav` still exists. (Negative — DB delete is decoupled from filesystem cleanup.)

#### Test: bounce-null-rendered-path-triggers-retry (covers R32, R15–R17)

**Given**: A bounce row exists with `rendered_path IS NULL` (prior producer run timed out).
**When**: `_exec_bounce_audio` is invoked with the same inputs that produced this hash.
**Then**:
- **pending-row-deleted**: The existing row is removed before the retry proceeds (so the UNIQUE constraint does not reject the new `create_bounce`).
- **new-row-created**: After a successful render, a new row with the same `composite_hash` is present and its `rendered_path` is populated.

#### Test: force-rerun-deletes-then-creates (covers R16, R19–R20)

**Given**: A cache hit exists for DSP key `(seg-1, v1, p1)` with datapoints and scalars.
**When**: `_exec_analyze_*` (DSP producer) is invoked with `force_rerun=True`.
**Then**:
- **old-row-deleted**: The previously cached run row's `id` no longer exists in `dsp_analysis_runs`.
- **old-children-gone**: Datapoints belonging to the old run_id are gone (cascade).
- **new-row-created**: A new row with the same 3-tuple exists, with a *different* `id`.
- **new-children-present**: The new run's datapoints / scalars are present.

#### Test: force-rerun-on-miss-is-noop-plus-run (covers R18)

**Given**: No cache row exists for the DSP key.
**When**: Producer is invoked with `force_rerun=True`.
**Then**:
- **no-delete-attempted**: No `DELETE FROM dsp_analysis_runs` statement is issued prior to creation. (Observable via DAL call instrumentation or just absence of error.)
- **new-row-created**: Producer completes and a run row exists after.

#### Test: default-rerun-returns-cached (covers R17, R21)

**Given**: A cache hit exists for DSP key `(seg-1, v1, p1)`.
**When**: Producer is invoked with `force_rerun=False` (default).
**Then**:
- **cached-returned**: Response is the cached row's data.
- **no-new-row**: `list_dsp_runs(project_dir, "seg-1")` count is unchanged.
- **no-analysis-invoked**: No librosa / pyloudnorm / Gemini call occurs.

#### Test: producer-exception-deletes-partial-row (covers R21)

**Given**: The mix producer is set up so that during analysis, `_mix_lufs(...)` raises a contrived exception.
**When**: `_exec_analyze_master_bus` runs and hits the exception.
**Then**:
- **partial-row-deleted**: No row remains in `mix_analysis_runs` for the target 5-tuple.
- **no-orphan-children**: `mix_datapoints`, `mix_sections`, `mix_scalars` have zero rows referencing the transient run_id.
- **error-returned**: The producer returns `{"error": "analysis failed: …"}` rather than raising.

#### Test: bulk-insert-is-idempotent (covers R6, R23)

**Given**: A run row `run-A` and a pre-existing datapoint `(run-A, "rms", 1.25, 0.9)`.
**When**: `bulk_insert_dsp_datapoints(project_dir, "run-A", [("rms", 1.25, 0.42, None)])` is called.
**Then**:
- **no-unique-error**: No exception is raised.
- **value-overwritten**: The stored datapoint's `value` is now `0.42`; exactly one row with `(run_id="run-A", data_type="rms", time_s=1.25)` exists.

#### Test: bulk-insert-empty-is-noop (covers R23)

**Given**: A run row `run-A` with zero datapoints.
**When**: `bulk_insert_dsp_datapoints(project_dir, "run-A", [])` is called.
**Then**:
- **returns-zero**: Return value is `0`.
- **no-rows-inserted**: `query_dsp_datapoints(project_dir, "run-A")` returns `[]`.

#### Test: pool-segment-delete-cascades-to-runs (covers R1, R3, R5)

**Given**: `pool_segments` has row `seg-1`; DSP and audio-description runs both exist referencing `seg-1`.
**When**: `DELETE FROM pool_segments WHERE id = 'seg-1'` is executed.
**Then**:
- **dsp-run-gone**: No rows remain in `dsp_analysis_runs` with `source_segment_id="seg-1"`.
- **desc-run-gone**: No rows remain in `audio_description_runs` with `source_segment_id="seg-1"`.
- **dsp-children-gone**: No rows in `dsp_datapoints`/`sections`/`scalars` for those run_ids.

#### Test: list-on-empty-returns-empty-list

**Given**: A fresh project DB with no rows in any cache table.
**When**: `list_dsp_runs(project_dir, "seg-anything")`, `list_mix_runs_for_hash(project_dir, "h-any")`, `list_audio_description_runs(project_dir, "seg-any")`, `list_bounces(project_dir)` are each called.
**Then**:
- **each-returns-empty-list**: All four return `[]`. None returns `None`. (Negative on null return.)

#### Test: peaks-cache-hit-skips-decode (covers R26)

**Given**: A peaks cache file already exists at `<project_dir>/audio_staging/.peaks/<key>.f16` for a specific `(source_path, offset, duration, resolution)` tuple.
**When**: `compute_peaks(...)` is called with the same tuple.
**Then**:
- **bytes-equal-file**: The return value equals the file's bytes verbatim.
- **no-ffmpeg-invocation**: No `subprocess.run` / `Popen` call targeting `ffmpeg` occurs. (Negative.)

#### Test: peaks-cache-miss-decodes-and-writes (covers R27)

**Given**: No cache file exists at the expected path.
**When**: `compute_peaks(...)` is called.
**Then**:
- **ffmpeg-invoked**: ffmpeg is invoked exactly once.
- **bytes-returned**: Return value is a non-empty `bytes` object.
- **cache-file-written**: The expected `.f16` file now exists and its contents equal the returned bytes.

#### Test: peaks-source-edit-invalidates-key (covers R25)

**Given**: A peaks cache file exists for source `X`, and `X` is then edited (its `st_mtime_ns` changes).
**When**: `compute_peaks(...)` is called with the same `(offset, duration, resolution)`.
**Then**:
- **new-key-computed**: The cache key derived from the new `(mtime_ns, size)` is different from the old key.
- **cache-miss-triggers-decode**: ffmpeg is invoked.
- **old-file-lingers**: The old cache file at the *old* key still exists on disk (not cleaned up). (See OQ-6.)

### Edge Cases

Boundaries, unusual inputs, failure-injection, and the `undefined` set.

#### Test: peaks-cache-write-failure-is-non-fatal (covers R28)

**Given**: `compute_peaks` is invoked in a project where writes to the `.peaks` directory will fail (e.g. directory made read-only, or monkeypatched `write_bytes` raises).
**When**: `compute_peaks(...)` is called on a cache miss.
**Then**:
- **bytes-still-returned**: The function returns the computed bytes.
- **no-exception-raised**: The call does not raise. (Negative.)
- **warning-logged**: A warning is emitted via the module's `_log` sink.

#### Test: list-dsp-runs-ordering

**Given**: Three DSP runs for `seg-1` inserted with `created_at` values in order `T1 < T2 < T3`.
**When**: `list_dsp_runs(project_dir, "seg-1")` is called.
**Then**:
- **ordered-desc-by-created-at**: Returned list is `[T3, T2, T1]`. (From `db_analysis_cache.py:89` — `ORDER BY created_at DESC`.)

#### Test: peaks-key-format-precision

**Given**: Two `compute_peaks` invocations with `source_offset=0.1` and `source_offset=0.100001`, all other args identical.
**When**: Both are invoked.
**Then**:
- **different-keys**: The two cache keys differ (because the format string uses `{:.6f}` precision, which distinguishes these two values).

#### Test: bounce-selection-json-empty-for-full-mode (covers R4)

**Given**: `create_bounce(..., mode="full", selection={})` is called.
**When**: The row is subsequently fetched via `get_bounce_by_hash(...)`.
**Then**:
- **mode-full**: `mode == "full"`.
- **selection-empty**: `selection_json` round-trips as `{}` (empty object).

#### Test: dsp-datapoint-extra-json-null-vs-object

**Given**: Two datapoints inserted under the same run, one with `extra=None` and one with `extra={"bin": 42}`.
**When**: `query_dsp_datapoints(...)` is called.
**Then**:
- **null-extra-roundtrips**: The first datapoint's `extra_json` column is NULL (not the string `"null"`).
- **object-extra-roundtrips**: The second datapoint's `extra` parses back to `{"bin": 42}`.

#### Test: librosa-downgrade-old-cache-unreachable (references OQ-1)

**Note**: This is an **undefined** scenario and cannot be fully specified. Current behavior: if the librosa package is downgraded and `analyzer_version` uses `librosa.__version__`, the downgrade produces a cache miss against any row that was written by a *newer* librosa version (since the version string reverts). Whether those newer rows become "unreachable" (they stay in the DB but are never queried), whether they should be garbage-collected, or whether a downgrade should trigger a warning is not decided. See OQ-1.

**Given**: A DSP run exists with `analyzer_version="dsp-librosa-0.10.3"`.
**When**: The engine is restarted with librosa 0.10.2 and the producer is invoked for the same `(source_segment_id, params_hash)`.
**Then**:
- **behavior-undefined**: Spec does not pin an observable outcome. Do not assert; defer to OQ-1 for resolution.

#### Test: concurrent-create-same-key (references OQ-5)

**Note**: Undefined. Two producers invoking `create_dsp_run` with the same 3-tuple in rapid succession is not specified. One caller will see an `IntegrityError`; whether either caller retries, or whether the second caller sees the first caller's children, is implementation-defined. See OQ-5.

#### Test: process-crash-mid-bulk-insert (references OQ-3)

**Note**: Undefined. If the producer crashes after `create_mix_run(...)` but before child bulk-inserts, the partial row persists without children. The spec does not require cleanup on next startup. See OQ-3.

#### Test: cache-unbounded-growth (references OQ-4)

**Note**: Undefined. `audio_bounces` grows monotonically as the user explores different mixes / selections. Similarly, `mix_analysis_runs` grows with every unique `analyzer_version` + time window. No spec'd limit exists. See OQ-4.

---

## Non-Goals

- Defining what analyses exist (`"rms"`, `"lufs"`, `"clipping_detect"`, etc.). Opaque strings in this spec.
- Defining how `mix_graph_hash`, `params_hash`, or `composite_hash` are computed. Opaque in this spec.
- Defining the WS round-trip (`mix_render_request`, `bounce_audio_request`) — covered in the handlers spec.
- Defining the upload endpoints (`/api/projects/:name/mix-render-upload`, `/bounce-upload`) — covered in the handlers spec.
- Defining frontend download URL shapes for bounces.
- Defining background / maintenance jobs (LRU eviction, vacuum, orphan sweep). If any such job is added later, it gets its own spec and resolves OQ-3, OQ-4, OQ-6, OQ-7.
- Defining cross-project cache sharing (de-dup across projects). Caches are strictly per-project by path.

---

## Open Questions

- **OQ-1** — **Librosa version downgrade → old cache unreachable.** `analyzer_version` uses `librosa.__version__`. A library downgrade produces a cache miss against rows written by a newer version, but those newer rows remain in the DB forever (storage cost) and never match a future lookup unless the library is upgraded again. Options: (a) accept the cost, (b) emit a one-time warning on startup listing orphaned `analyzer_version` values, (c) add a manual CLI to purge orphan versions, (d) GC rows whose `analyzer_version` doesn't match the currently installed library on next analysis run. Recommendation: (c) — explicit, cheap, never wrong. Deferred.
- **OQ-2** — **Hash collision policy.** DSP (`params_hash` — hex digest), mix (`mix_graph_hash`), bounce (`composite_hash` — SHA-256), peaks (SHA-1 truncated to 16 hex = 64 bits) all treat hash equality as row identity with no content verification. Collision odds are astronomically low for SHA-256 (~2⁻²⁵⁶) and negligible for SHA-1 at 64 bits (~2⁻⁶⁴ per pair, higher than SHA-256 but still safe for practical project sizes). Policy on intentional collision / adversarial input is not defined. Is this acceptable, or should peaks move to SHA-256 for consistency?
- **OQ-3** — **Partial run rows lingering after producer crash.** R21 requires the producer to clean up on *caught* exceptions. A hard crash (segfault, OOM kill, power loss) between `create_*_run` and child inserts leaves a parent row with zero children and `rendered_path IS NULL`. For bounce, the retry path (R32) handles this. For DSP / mix / description, there is no retry detection — the next lookup will cache-hit the empty parent row and return no data. Options: (a) add a startup sweep that deletes runs with zero children *and* `rendered_path IS NULL` older than N minutes, (b) make every producer check for partial rows at lookup time, (c) ignore (low-probability in practice).
- **OQ-4** — **Cache growth unbounded.** No TTL, no LRU, no max row count. Over the lifetime of a project, `audio_bounces` and `mix_analysis_runs` grow monotonically. Do we need: (a) a size cap (e.g. last N bounces; evict oldest `rendered_path`), (b) a manual `cache prune` CLI, (c) a background sweep based on `created_at`, or (d) nothing (user's problem)? For the peaks filesystem cache, the same question applies at the filesystem level.
- **OQ-5** — **Concurrent writes with the same cache key.** Two producer invocations with the same 3-tuple / 5-tuple / composite_hash in the same process or across processes: the UNIQUE constraint will reject the second `INSERT`. Whether the DAL retries, returns the first caller's row, or raises to the caller is unspecified. Practically, the engine assumes single-process per project; this resolves naturally. Specify explicitly?
- **OQ-6** — **Peaks cache orphan files.** If a source file is edited (cache miss produces a new key), or deleted entirely, the old `.f16` files linger in `audio_staging/.peaks/`. No purge API exists. Add one?
- **OQ-7** — **Cache row present, rendered WAV missing on disk.** If the user (or an external process) deletes `pool/mixes/<hash>.wav` or `pool/bounces/<hash>.wav` while the DB row persists with `rendered_path` set, the next lookup returns a row pointing at a missing file. For bounce, the download endpoint will 404. For mix analysis, the cache hit path returns a valid summary *without* re-reading the WAV (since the analyses_json is cached), so it works fine — but serving `rendered_path` to a downstream tool that tries to read it will fail. Options: (a) stat-check the file on cache hit and treat missing-file as cache miss, (b) accept the 404 behavior and rely on user re-running with `force_rerun=True`, (c) add a `cache verify` CLI.

---

## Related Artifacts

- **Source references**:
  - `src/scenecraft/db.py:745–910` — DDL for all five cache table groups.
  - `src/scenecraft/db_analysis_cache.py` — DSP + audio-description DAL.
  - `src/scenecraft/db_mix_cache.py` — Mix DAL.
  - `src/scenecraft/db_bounces.py` — Bounce DAL.
  - `src/scenecraft/audio/peaks.py` — Filesystem peaks cache.
  - `src/scenecraft/chat.py:3036` — `_exec_analyze_master_bus` (mix producer).
  - `src/scenecraft/chat.py:3422` — `_exec_bounce_audio` (bounce producer).
  - `src/scenecraft/mix_graph_hash.py`, `src/scenecraft/bounce_hash.py` — hash composition (out of scope for this spec).
- **Source audit**: `agent/reports/audit-2-architectural-deep-dive.md` §1C (DB Schema + DAL + Migrations — 12 units + 50+ tables), §1E (Bounce + Analysis — 6 units).
- **Companion spec (planned)**: `local.engine-analysis-handlers.md` — handler behavior, WS round-trip, timeouts, and upload endpoints.
- **Related spec**: `local.fastapi-migration.md` — the upload endpoints live under the FastAPI-migrated API.
- **Related spec**: `local.openapi-tool-codegen.md` — the chat-tool schemas for `analyze_master_bus` and `bounce_audio` are generated from FastAPI route definitions.

---

**Namespace**: local
**Spec**: engine-db-analysis-caches
**Version**: 1.0.0
**Created**: 2026-04-27
**Last Updated**: 2026-04-27
**Status**: Ready for Proofing
