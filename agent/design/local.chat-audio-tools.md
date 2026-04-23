# Chat-Agent Audio Authoring Tools

**Concept**: Expose write-path tools to the embedded chat agent so it can create audio tracks, audio clips, effects, automation curves, and run cached librosa + LLM analysis to drive auto-duck / auto-mix / auto-master flows.
**Created**: 2026-04-23
**Last Updated**: 2026-04-23
**Status**: Proposal (Phase 1 shipped; Phase 2 unblocked after M13 completion; Phase 3 net-new)

---

## Overview

The embedded SceneCraft chat agent can currently **read** audio state (pool segments, existing tracks, existing clips) via its tool surface, but it cannot **write** any audio state onto the timeline. When a user asks the agent to "lay down these four vocal takes as a new track" or "add a compressor to the lead vocal," the agent can describe what needs to happen but cannot do it.

This design specifies three phases of chat tools that close that gap. Phase 1 ships authoring primitives (tracks + clips). Phase 2 ships effect/curve mutations. Phase 3 ships a cached-analysis pipeline (librosa + LLM) that powers auto-duck, auto-mix, and auto-master workflows. All Phase 1–2 tools reuse existing DB functions and the existing chat tool pattern. Phase 3 adds new DB tables for caching analysis output.

---

## Problem Statement

- The chat agent has zero write-path for audio. Anything beyond read-only summaries requires the user to drop into the GUI.
- The agent is already the natural interface for bulk/tedious operations (lay down 10 takes on 10 tracks, copy volume automation between tracks, normalize effect params across a bus).
- No existing write tool covers this surface; the one `add_keyframe` chat tool pattern (chat.py:393–411, 1171–1204) is per-entity and doesn't generalize.
- The request has already surfaced organically from the agent itself: *"I have no way to actually create the audio_clip records or audio_track records that would place them on the timeline."*

---

## Solution

**Three phases**:

### Phase 1 — SHIPPED 2026-04-23
1. `add_audio_track` — create an empty track
2. `add_audio_clip` — place a pool_segment on a track with timing

### Phase 2 — ALL UNBLOCKED (M13 complete 2026-04-23)
3. `update_volume_curve` — replace the inline volume curve on a track or clip (no M13 dep; inline column)
4. `update_effect_param_curve` — upsert an automation curve on an effect parameter (uses M13 `effect_curves` table)
5. `add_audio_effect` — append an effect to a track's chain (uses M13 `track_effects` + effect registry)

### Phase 3 — Cached analysis pipeline (new DB tables)
6. `generate_dsp` — run librosa analysis on a pool_segment, persist results in `dsp_*` tables
7. `generate_descriptions` — run LLM analysis (Gemini) on a pool_segment, persist structured properties in `audio_description*` tables
8. `analyze_audio_track` / `analyze_audio_clip` — high-level composite: resolves to pool_segments, calls generate_dsp + generate_descriptions, returns summary
9. `apply_mix_plan` — batch-apply effect + curve + volume updates in one undo group (for agent-generated mix plans)

**Core design choices**:
- **Primitives, not compound operations** (Phase 1–2). Each tool maps to exactly one DB mutation and one undo group.
- **Thin wrappers over existing DB functions** where possible. Phase 1 & 2 tools reuse `db.add_audio_track`, `db.add_audio_clip`, `db.upsert_effect_curve`, `db.add_track_effect`, etc.
- **Auto-compute `end_time`** from `pool_segments.duration_seconds` when the agent omits it.
- **Two separate curve tools, not one overloaded `update_curves`**, because volume curves live inline on `audio_tracks.volume_curve` / `audio_clips.volume_curve` (single column) while effect param curves live in the M13 `effect_curves` table.
- **Cached analysis, not on-demand recompute** (Phase 3). `librosa.analyze_audio` on a 3-minute vocal takes ~2–5s. Cache results keyed by `(source_segment_id, analyzer_version, params_hash)` so the agent pays that cost once per source, reuses across chat turns, and can run `sql_query` over the structured datapoints.
- **DSP (quantitative) ≠ descriptions (qualitative)** — two separate table families:
  - `dsp_*`: numerical librosa output (onsets, RMS envelope, spectral features, BPM, sections-by-transient-detection). Trust it. Agent queries it for exact time/value facts.
  - `audio_description*`: LLM-emitted structured properties (mood, genre, vocal style, section-by-semantic-meaning, energy). The agent's "vibes layer."
  - Do not ask the LLM for things librosa does accurately (tempo, onsets). Do not ask librosa for things it can't do (mood, genre).
- **`apply_mix_plan` is the only non-primitive**, by design. Sub-LLM auto-mix flows produce a multi-step plan (add 3 effects, set 5 curves, adjust 2 volumes). Composing this as 10 separate undo groups is hostile; one batched group per plan is the right shape.

---

## Implementation

### Phase 1 tools

Each tool is registered via the chat.py pattern:
1. Tool schema dict in the tool-schemas section
2. Added to `TOOLS` list (chat.py:658)
3. `_exec_<tool_name>(project_dir, input_data)` handler
4. Dispatch case in `_execute_tool()` (chat.py:~1750–1850)

**Tool 1: `add_audio_track`**

```python
ADD_AUDIO_TRACK_TOOL = {
    "name": "add_audio_track",
    "description": "Create a new, empty audio track on the timeline. "
                   "Returns the new track_id. Muted/volume are initial static values.",
    "input_schema": {
        "type": "object",
        "properties": {
            "name":   {"type": "string", "description": "Display name. If omitted, auto-generated as 'Track N'."},
            "muted":  {"type": "boolean", "default": False},
            "volume": {"type": "number",  "default": 1.0, "minimum": 0.0, "maximum": 2.0},
        },
        "required": [],
    },
}

def _exec_add_audio_track(project_dir, input_data):
    track_id = next_audio_track_id(project_dir)
    name = input_data.get("name") or f"Track {track_id[-4:]}"
    muted = bool(input_data.get("muted", False))
    volume = float(input_data.get("volume", 1.0))
    volume_curve = json.dumps([[0, volume], [1, volume]])  # constant curve

    undo_begin(project_dir, f"Chat: add audio track {track_id}")
    add_audio_track(project_dir, {
        "id": track_id, "name": name,
        "muted": muted, "solo": False, "hidden": False,
        "volume_curve": volume_curve,
        "display_order": next_audio_track_display_order(project_dir),
    })
    return {"track_id": track_id}
```

**Tool 2: `add_audio_clip`**

```python
ADD_AUDIO_CLIP_TOOL = {
    "name": "add_audio_clip",
    "description": "Place an audio source on a track at a timeline position. "
                   "If end_time is omitted, it is computed as "
                   "start_time + (pool_segment.duration_seconds - source_offset).",
    "input_schema": {
        "type": "object",
        "properties": {
            "track_id":      {"type": "string"},
            "source_path":   {"type": "string", "description": "pool_segments.pool_path"},
            "start_time":    {"type": "number", "default": 0.0},
            "source_offset": {"type": "number", "default": 0.0},
            "end_time":      {"type": "number", "description": "Optional; auto-computed if omitted"},
            "volume_curve":  {"type": "string", "default": "[[0,1],[1,1]]"},
            "label":         {"type": "string"},
        },
        "required": ["track_id", "source_path"],
    },
}

def _exec_add_audio_clip(project_dir, input_data):
    track_id = input_data["track_id"]
    source_path = input_data["source_path"]
    start_time = float(input_data.get("start_time", 0.0))
    source_offset = float(input_data.get("source_offset", 0.0))
    end_time = input_data.get("end_time")

    if end_time is None:
        seg = get_pool_segment_by_path(project_dir, source_path)
        if not seg or seg.get("duration_seconds") is None:
            raise ValueError(
                f"Cannot auto-compute end_time: pool segment {source_path!r} "
                f"has no duration_seconds; pass end_time explicitly."
            )
        end_time = start_time + (seg["duration_seconds"] - source_offset)

    clip_id = next_audio_clip_id(project_dir)
    undo_begin(project_dir, f"Chat: add audio clip {clip_id} to track {track_id}")
    add_audio_clip(project_dir, {
        "id": clip_id,
        "track_id": track_id,
        "source_path": source_path,
        "start_time": start_time,
        "end_time": end_time,
        "source_offset": source_offset,
        "volume_curve": input_data.get("volume_curve", "[[0,1],[1,1]]"),
        "muted": False,
        "label": input_data.get("label"),
    })
    return {"audio_clip_id": clip_id}
```

Both tools require one helper lookup: `get_pool_segment_by_path(project_dir, pool_path)`. If that helper doesn't already exist, add it to db.py as a thin SELECT.

### Phase 2 tools (all unblocked 2026-04-23 — M13 complete)

Final signatures; all three can ship in one PR:

```python
# Tool 3: add_audio_effect  (sequenced after M13 task 46)
add_audio_effect(
    track_id: str,
    effect_type: str,               # enum from effect registry
    order_index: int | None = None, # None = append
    params_json: str | None = None, # initial static values
    enabled: bool = True,
) -> {"effect_id": str}

# Tool 4: update_volume_curve  (inline column, post-M13)
update_volume_curve(
    target_type: Literal["track", "clip"],
    target_id: str,
    points_json: str,
    interpolation: Literal["bezier", "linear", "step"] = "bezier",
) -> {"ok": True}
# → UPDATE audio_tracks.volume_curve OR audio_clips.volume_curve

# Tool 5: update_effect_param_curve  (M13 effect_curves table)
update_effect_param_curve(
    effect_id: str,
    param_name: str,
    points_json: str,
    interpolation: Literal["bezier", "linear", "step"] = "bezier",
) -> {"curve_id": str}
# → UPSERT into effect_curves WHERE (effect_id, param_name)
```

### Phase 3 — Cached analysis pipeline (auto-duck / auto-mix / auto-master)

New SQLite tables in per-project `project.db`, mirroring the M13 pattern (no global state, scoped by DB location).

**Schema (7 new tables, 2 table families)**:

```sql
-- Family 1: DSP (librosa, quantitative)

CREATE TABLE dsp_analysis_runs (
  id                TEXT PRIMARY KEY,
  source_segment_id TEXT NOT NULL REFERENCES pool_segments(id) ON DELETE CASCADE,
  analyzer_version  TEXT NOT NULL,        -- e.g. "librosa-0.10.2"
  params_hash       TEXT NOT NULL,        -- hash of analysis params (sr, hop_length, etc.)
  analyses_json     TEXT NOT NULL,        -- which analyses were run: ["onsets","rms","spectral_centroid",...]
  created_at        TEXT NOT NULL,
  UNIQUE(source_segment_id, analyzer_version, params_hash)  -- cache key
);

-- EAV-ish for time-series datapoints that all fit (time, value)
CREATE TABLE dsp_datapoints (
  run_id      TEXT NOT NULL REFERENCES dsp_analysis_runs(id) ON DELETE CASCADE,
  data_type   TEXT NOT NULL,      -- 'onset' | 'rms' | 'spectral_centroid' | 'zcr' | ...
  time_s      REAL NOT NULL,
  value       REAL NOT NULL,
  extra_json  TEXT,               -- only when a single REAL isn't enough (rare)
  PRIMARY KEY (run_id, data_type, time_s)
);
CREATE INDEX dsp_datapoints_type_time ON dsp_datapoints(run_id, data_type, time_s);

-- Time-ranged regions (sections-by-transient, vocal-presence ranges, etc.)
CREATE TABLE dsp_sections (
  run_id       TEXT NOT NULL REFERENCES dsp_analysis_runs(id) ON DELETE CASCADE,
  start_s      REAL NOT NULL,
  end_s        REAL NOT NULL,
  section_type TEXT NOT NULL,     -- 'drop' | 'vocal_presence' | 'silence' | ...
  label        TEXT,
  confidence   REAL,
  PRIMARY KEY (run_id, start_s, section_type)
);

-- Scalars (tempo_bpm, peak_db, global_rms, etc.)
CREATE TABLE dsp_scalars (
  run_id   TEXT NOT NULL REFERENCES dsp_analysis_runs(id) ON DELETE CASCADE,
  metric   TEXT NOT NULL,         -- 'tempo_bpm' | 'global_rms' | 'peak_db' | ...
  value    REAL NOT NULL,
  PRIMARY KEY (run_id, metric)
);

-- Family 2: LLM descriptions (qualitative, semantic)

CREATE TABLE audio_description_runs (
  id                TEXT PRIMARY KEY,
  source_segment_id TEXT NOT NULL REFERENCES pool_segments(id) ON DELETE CASCADE,
  model             TEXT NOT NULL,        -- 'gemini-2.5-pro', 'claude-opus-4-7', ...
  prompt_version    TEXT NOT NULL,        -- so prompt iterations produce new runs
  chunk_size_s      REAL NOT NULL,
  created_at        TEXT NOT NULL,
  UNIQUE(source_segment_id, model, prompt_version)
);

CREATE TABLE audio_descriptions (
  run_id      TEXT NOT NULL REFERENCES audio_description_runs(id) ON DELETE CASCADE,
  start_s     REAL NOT NULL,
  end_s       REAL NOT NULL,
  property    TEXT NOT NULL,      -- 'section_type' | 'mood' | 'energy' | 'vocal_style' | 'genre' | ...
  value_text  TEXT,
  value_num   REAL,
  confidence  REAL,
  raw_json    TEXT,
  PRIMARY KEY (run_id, start_s, property)
);
CREATE INDEX audio_descriptions_property_time ON audio_descriptions(run_id, property, start_s);

CREATE TABLE audio_description_scalars (
  run_id     TEXT NOT NULL REFERENCES audio_description_runs(id) ON DELETE CASCADE,
  property   TEXT NOT NULL,       -- 'key' | 'global_genre' | 'vocal_gender' | ...
  value_text TEXT,
  value_num  REAL,
  confidence REAL,
  PRIMARY KEY (run_id, property)
);
```

**Cache semantics**:
- Source segments are immutable in this codebase (new audio → new pool_segment). Cache is stable across timeline edits.
- Cache key for DSP: `(source_segment_id, analyzer_version, params_hash)`. Upgrading librosa changes `analyzer_version` → new run, old run persists until source_segment is deleted.
- Cache key for descriptions: `(source_segment_id, model, prompt_version)`. Prompt iteration produces new runs — A/B-able.
- Clip trim changes don't invalidate; the agent filters datapoints by `time_s BETWEEN trim_in AND trim_out`.

**Sizing**: RMS envelope @50ms window × 3-min vocal = 3600 rows. ~10 analyses per segment × 100 segments = ~3.6M rows. SQLite fine with the proposed indexes.

**Chat tools (Phase 3)**:

```python
# Tool 6: generate_dsp
generate_dsp(
    source_segment_id: str,
    analyses: list[str] = ["onsets", "rms", "spectral_centroid", "sections", "tempo"],
    force_rerun: bool = False,   # default: return existing run_id if cache hit
) -> {"run_id": str, "cached": bool, "summary": {...}}

# Tool 7: generate_descriptions
generate_descriptions(
    source_segment_id: str,
    model: str = "gemini-2.5-pro",
    properties: list[str] = ["section_type", "mood", "energy", "vocal_style", "instrumentation"],
    force_rerun: bool = False,
) -> {"run_id": str, "cached": bool, "properties_written": int}

# Tool 8: analyze_audio_track / analyze_audio_clip (convenience composites)
analyze_audio_track(
    track_id: str,
    dsp: bool = True,
    descriptions: bool = False,   # off by default — descriptions cost money
) -> {"clips": [{"clip_id": ..., "dsp_run_id": ..., "description_run_id": ...}]}

analyze_audio_clip(
    audio_clip_id: str,
    dsp: bool = True,
    descriptions: bool = False,
) -> {"clip_id": ..., "dsp_run_id": ..., "description_run_id": ...}

# Tool 9: apply_mix_plan (batch-apply agent-generated mix decisions)
apply_mix_plan(
    description: str,              # "auto-duck pass on music when vocal present"
    operations: list[dict],        # ordered list of {"op": "add_effect"|"set_volume_curve"|..., ...}
) -> {"applied": int, "skipped": int, "undo_group_id": str}
# All operations in one undo group. Partial failure aborts all.
```

**Auto-duck demo flow** (the minimum viable end-to-end):
1. Agent calls `generate_dsp(source_segment_id=<vocal_seg>, analyses=["rms","sections"])` → cached on re-run.
2. Agent queries via `sql_query`: `SELECT start_s, end_s FROM dsp_sections WHERE run_id=? AND section_type='vocal_presence'`.
3. Agent generates a duck curve: points that drop to ~0.3 during vocal ranges, ramp back to 1.0 with short transitions.
4. Agent calls `update_volume_curve(target_type='track', target_id=<music_track>, points_json=<duck curve>)`.
5. Done. User plays back → vocal ducks music automatically.

Auto-mix and auto-master are the same scaffolding with more steps: `generate_dsp` + `generate_descriptions` → sub-LLM produces a mix plan → `apply_mix_plan` applies it atomically.

- **Unblocks the agent's authoring surface.** Once Phase 1 lands, the chat agent can assemble tracks from pool segments end-to-end in one turn.
- **Undo parity with GUI edits.** Trigger-based undo-log (db.py:728–747) auto-captures the mutations; the only work in the tool is `undo_begin()` wrapping.
- **No schema drift.** Both phases reuse existing tables; zero new columns added by this design.
- **Composable by the agent.** An agent that wants "new track + first clip + compressor + volume curve" composes four calls, each its own undo unit — user can peel back any step individually.
- **Pattern-consistent.** Matches `add_keyframe` exactly; new chat tools in the future follow the same template.

---

## Trade-offs

- **More round-trips for common workflows.** "Create track and drop a clip on it" is two tool calls, not one. The agent will make those two calls in the same model turn anyway, so latency cost is near-zero; the only real cost is prompt-token overhead for the second tool description. Acceptable.
- **Agent must know pool_segment pool_paths.** The agent already has a read tool for pool_segments, so this is a solved problem — but if a future agent prompt-engineers around `source_path` vs `source_id`, we'll need to decide whether `add_audio_clip` accepts either. Keep it `source_path` (pool_path string) for now; add an id-accepting overload later if needed.
- **Phase 2 blocks on M13.** If M13 slips, the chat agent cannot author effects or curves. This is the right call — shipping Phase 2 tools against a hardcoded effect list would require a rewrite once M13 lands, and the agent can already describe effect chains to the user in words as a fallback.
- **`end_time` auto-compute is a data-contract dependency.** If any pool_segment lacks `duration_seconds`, the tool errors. That's the correct behavior, but it makes the tool's success rate coupled to the ingest pipeline's completeness. Audit: confirm all ingest paths populate `duration_seconds` before Phase 1 ships.

---

## Dependencies

**Phase 1**:
- `db.add_audio_track()`, `db.add_audio_clip()` — exist (db.py:2547, 2726)
- `undo_begin()` — exists (db.py:2365–2398)
- Trigger-based undo capture — exists (db.py:728–747)
- `get_pool_segment_by_path()` — **may need to be added** (thin SELECT helper)
- `next_audio_track_id`, `next_audio_clip_id`, `next_audio_track_display_order` — may need to be added if not already present
- Chat tool dispatcher — exists (chat.py `_execute_tool()`)

**Phase 2** (all gated):
- M13 task 45 — `effect_curves` table + `audio_track_effects` table
- M13 task 46 — effect registry (enumerates valid `effect_type` values and their param schemas)

---

## Testing Strategy

- **Unit**: for each tool, one happy-path test (valid inputs → row inserted → undo reverts), one negative test (missing required field → clear error), one defaults test (omitted optional fields → correct auto-computed values).
- **Integration**: a single test that drives the chat dispatcher end-to-end: agent issues `add_audio_track` → `add_audio_clip` → verify both rows exist, timeline renders audio at the right spot, one undo step removes the clip (track stays), second undo step removes the track.
- **Undo parity**: add an assertion that the resulting `undo_log` rows are semantically identical to GUI-driven adds.
- **Regression**: one test for the `end_time` auto-compute failure path (pool_segment with null `duration_seconds` → tool raises, no partial row inserted).

---

## Migration Path

No migration needed — purely additive. No table changes, no data backfill. Existing chat tools continue to work unchanged.

---

## Key Design Decisions

### Granularity

| Decision | Choice | Rationale |
|---|---|---|
| Tool shape | Primitives only (no compound `add_audio_clip_to_new_track`) | One tool call = one DB mutation = one undo group; compositions are free for the agent |
| Separate volume-curve and effect-param-curve tools | Yes | Different storage (inline column vs separate table); overloading one tool adds dispatch complexity with no gain |

### Sequencing

| Decision | Choice | Rationale |
|---|---|---|
| Phase 1 vs Phase 2 split | Ship `add_audio_track` + `add_audio_clip` now; defer effect/curve tools | Current agent request only needs Phase 1; Phase 2 needs M13 registry to ship safely |
| Ship before M13 effect registry? | Moot — M13 shipped 2026-04-23 | Phase 2 is fully unblocked |
| Block M14 (rotoscope) on this? | No | Independent surface |
| Phase 3 analysis pipeline | Cached DB tables, not on-demand | Librosa costs ~2-5s per vocal; caching avoids repaying per chat turn. Also enables sub-LLM mix flows via sql_query over structured data. |
| DSP vs description split | Two separate table families | Librosa = facts; LLM = vibes. Don't mix storage; don't ask LLM for BPM. |

### Data model

| Decision | Choice | Rationale |
|---|---|---|
| `end_time` default | Auto-compute from `pool_segments.duration_seconds - source_offset` | Matches GUI behavior; explicit error if duration null |
| `source_path` param shape | Pool_path string (not pool_segment id) | Consistent with `audio_clips.source_path` column; agent already has read access to pool_paths |
| Volume curve default | `[[0,1],[1,1]]` (constant at 1.0) | Matches existing DB helper default |
| Track auto-naming | `Track <last 4 of id>` when name omitted | Cheap, stable, human-readable fallback |

### Undo

| Decision | Choice | Rationale |
|---|---|---|
| Undo wrapping | `undo_begin()` in each executor before DB call | Matches `add_keyframe` pattern; triggers handle inverse SQL automatically |
| Batch undo (multi-op combined into one group) | Not in v1 | Agent composes; user can undo each step individually |

---

## Future Considerations

- **`remove_audio_clip` / `remove_audio_track`** — deletion counterparts once add tools are validated in production. Soft-delete via `audio_clips.deleted_at` is already in the schema.
- **`duplicate_audio_clip`** — convenience for "copy this clip to another track" workflows. Could be a Phase 3 addition.
- **`batch_add_audio_clips`** — if agents routinely want to lay down 10+ clips at once and single-undo-per-clip feels noisy, add a batch variant that wraps all inserts in one `undo_begin` group. Only build once we have evidence of friction.
- **`add_send_bus`** / **`add_macro_knob`** — much further out, gated on M13 full delivery.
- **Id-accepting overload** of `add_audio_clip` (accept either `source_path` or `source_segment_id`) — only if agents trip over the string-path form in practice.

---

**Status**: Proposal
**Recommendation**: Implement Phase 1 (`add_audio_track`, `add_audio_clip`) in a single PR against `chat.py`. Defer Phase 2 until M13 tasks 45–46 land.
**Related Documents**:
- [local.effect-curves-macro-panel.md](local.effect-curves-macro-panel.md) — M13 (blocks Phase 2)
- [local.undo-system.md](local.undo-system.md) — undo semantics relied on here
