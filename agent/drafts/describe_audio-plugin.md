# Draft: `describe_audio` Plugin

**Status**: Draft / early concept — not yet a design
**Created**: 2026-04-23
**Related**: [`local.stem-aware-audio-descriptions.md`](../design/local.stem-aware-audio-descriptions.md), [`local.multi-model-stem-pipeline.md`](../design/local.multi-model-stem-pipeline.md), `plugins/isolate_vocals/` (reference implementation of the plugin shape)

---

## Concept

Package the existing Gemini-based audio-description pipeline (`GeminiAudioDescriber` + `describe_sections` in `scenecraft.ai.audio_describer`) as a first-party scenecraft plugin — `describe_audio` — so users can right-click an `audio_clip` in the timeline and produce a structured, per-section musical description as a first-class project artifact, with progress streamed over WS and the result browsable in a dedicated panel.

The motivating output shape exists today (46 sections × 7 sub-sections: event log / rhythm / energy / sustained / key moments / instruments / mood). It's currently only accessible via a Python script against an audio file. This plugin turns it into a real editor-level operation.

---

## Why a Plugin

Follows the pattern already set by `isolate_vocals`: heavy, model-backed audio operations live behind a narrow plugin seam, not in the editor core. Benefits:

- Same UX shape as other audio operations (context-menu → panel → run → progress → result).
- Reuses `plugin_api` (db helpers, `job_manager`, `extract_audio_as_wav`, `register_rest_endpoint`, `make_disposable`).
- Same chat-tool discoverability path (`describe_audio__run` via `chat.py`).
- Can be a sibling to a future `isolate_stems` plugin and compose with it (stems → per-stem descriptions, per the stem-aware-descriptions design).

---

## Plugin Shape (Mirroring `isolate_vocals`)

### Backend (`scenecraft-engine/src/scenecraft/plugins/describe_audio/`)

- `plugin.yaml` — manifest (name, version, activation events, contributed operation + context menu).
- `__init__.py` — `activate(plugin_api, context)` calls `PluginHost.register_operation(...)` + `plugin_api.register_rest_endpoint(...)`.
- `describe_audio.py` — `run(entity_type, entity_id, context) -> {"description_id": str, "job_id": str}`. Threaded worker: resolves source audio, detects sections, calls Gemini per section group (with chunking — see stem-aware design), writes markdown artifact, registers a DB row, updates `job_manager` progress.
- `README.md`, `tests/`.

### Frontend (`scenecraft/src/plugins/describe_audio/`)

- `plugin.yaml` — manifest mirror.
- `index.ts` — `activate(host, context)`: registers panel + operation + context menu on `audio_clip`.
- `AudioDescriptionsPanel.tsx` — list of prior description runs for the selected `audio_clip`, with expand-to-view of the structured output (section navigator + per-section accordion of the 7 sub-sections).
- `DescribeAudioRunForm.tsx` — minimal confirmation form (model, section-count estimate, ETA) inline in the panel.
- `describe-audio-client.ts` — REST call + WS subscription for the job.

### Operation Surface

```yaml
contributes:
  operations:
    - id: describe_audio.run
      label: "Describe audio"
      entityTypes: [audio_clip]
      handler: "backend:describe_audio.run"
      panel: "frontend:describe_audio.AudioDescriptionsPanel"
      outputs:
        - kind: audio_description   # see "Open Questions — Output Entity"
  contextMenus:
    - entityType: audio_clip
      items:
        - operation: describe_audio.run
          label: "Describe audio…"
          icon: text
          reveals: panel:audio_descriptions
```

---

## Minimum Viable Flow

1. User right-clicks an `audio_clip` in the timeline → "Describe audio…".
2. Panel reveals, kickoff form shows estimated section count + ETA. User hits Run.
3. REST call to `/api/projects/:name/plugins/describe_audio/run` creates a job, returns `job_id`.
4. Worker thread (in the plugin): load audio → detect sections → loop Gemini calls with 30s chunking → incrementally write output artifact → update progress.
5. On completion, a new `audio_description` row is linked to the `audio_clip`, and the panel renders it section by section.
6. All writes are inside one `undo_begin` group so the whole run is undoable.

---

## Open Questions (Need Resolution Before Promoting Draft → Design)

1. **Output entity shape.** Three options:
   - **(a) Inline columns on `audio_clips`** (a `description_markdown` + `described_at` column). Simple, but no history.
   - **(b) Separate `audio_descriptions` table** keyed on `audio_clip_id`, with one row per run (so re-runs produce new rows and we keep history). Mirrors `audio_isolations` exactly.
   - **(c) Store markdown as a file in `pool/descriptions/<uuid>.md`** and reference it from a lightweight `audio_descriptions` table. Keeps large text out of SQLite.
   Leaning toward **(b) + (c) combined**: lightweight junction row in DB + markdown file on disk. That also lets the panel stream partial results as the worker writes the file incrementally.

2. **Source audio resolution.** `isolate_vocals` resolves `audio_clip.effective_path` via `get_audio_clip_effective_path`. Same path works here — but we may also want a `subset` (trim_in/trim_out) mode for describing just a selected range of a long clip.

3. **Section detection.** Currently `detect_sections` lives in `scenecraft.analyzer` (a library function, not a plugin API). Should the plugin call into it directly (cross-module reach, acceptable for now), or should it become a `plugin_api` re-export? For MVP, direct call is fine; flag as off-surface to clean up later.

4. **Relationship to stems.** The companion stem-aware design proposes per-stem descriptions (drums-only, vocals-only, etc.) for higher-precision output. This plugin should treat that as a future option (`--include-stems` in the run form) that depends on stems already being present on the `audio_clip`. For MVP: full-mix only.

5. **Chat tool surface.** Add `describe_audio__run(entity_type, entity_id)` to `chat.py` and to `_DESTRUCTIVE_TOOL_PATTERNS` if we consider description runs destructive (they aren't destructive but they cost API credits — worth gating via elicitation, even briefly).

6. **Timestamp format bug (existing pipeline).** Current `_offset_timestamps` regex (`\[(\d+):(\d{2})\]`) only matches bracket-format timestamps, but Gemini produces `0:00.000s` (no brackets). Result: all timestamps in the generated markdown are section-relative, not track-absolute. Fix before wrapping as a plugin or the user-visible output will be confusing.

---

## Scope Guardrails

For the initial draft-promotion, explicitly **out of scope**:

- Streaming partial Gemini responses (wait for full response per chunk).
- Multiple model backends (Gemini only; abstract describer lives behind the existing ABC if anyone wants to add Qwen2 later).
- Editing / annotating descriptions after generation.
- Cross-clip descriptions (one run = one clip).
- Integration with `isolate_stems` for per-stem descriptions — defer to the stem-aware design's Phase 2.

---

## Next Steps

1. Answer the output-entity-shape question (option a/b/c above).
2. Fix the section-relative-timestamp bug in the existing `audio_describer.py` so the plugin isn't built on top of an off-by-section output.
3. Promote this draft to `agent/design/local.describe-audio-plugin.md` with a real spec: DB schema (if b/c), REST contract, panel wireframe, progress reporting semantics.
4. Create a milestone + tasks in scenecraft's `agent/` following the M11 pattern (schema → scaffolding → backend → frontend → panel → chat tool).
