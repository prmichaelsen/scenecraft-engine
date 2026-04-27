# Spec: Engine DB Schema — Core Timeline Entities

**Namespace**: local
**Version**: 1.0.0
**Created**: 2026-04-27
**Last Updated**: 2026-04-27
**Status**: Draft

---

## Purpose

Codify the per-project SQLite schema + DAL (Data Access Layer) contract for the engine's **core timeline entities**: keyframes, transitions, transition_effects, audio_tracks, audio_clips, audio_candidates, tr_candidates, audio_clip_links, and sections. Fix the observable contract so the upcoming FastAPI refactor of `api_server.py` can swap the transport layer without regressing semantics, and so retroactive unit-test coverage can be written against a stable spec.

---

## Source

- Mode: `--from-draft` (synthesized from audit-2 report + direct read of `src/scenecraft/db.py`)
- Primary code: `/home/prmichaelsen/.acp/projects/scenecraft-engine/src/scenecraft/db.py`
- Audit: `/home/prmichaelsen/.acp/projects/scenecraft-engine/agent/reports/audit-2-architectural-deep-dive.md` §1C units 1–7, §3 leaks #8, #9, #17

---

## Scope

**In**:
- Full per-table column enumeration (name, type, NOT NULL, DEFAULT, FK, CHECK, index) for the 9 tables above
- Soft-delete vs hard-delete semantics per table
- Cascade behavior on delete (including DAL-implemented cascades that are NOT FK-enforced)
- Ordering semantics (keyframes by timestamp, tr_candidates by added_at ASC, audio_candidates by added_at DESC, audio_tracks by display_order, sections by sort_order)
- JSON column value shape contracts: `volume_curve`, `remap`, `candidates`, `selected`, `context`, `tags`, `instruments`/`motifs`/`events`
- Known gaps codified as `undefined` behaviors with matching Open Questions: FK gap on transitions.from_kf/to_kf (audit leak #8), display_order uniqueness gap (#9), post-M13 nullable track_id migration gap (#17)
- DAL function contract (signature, return shape, pre/post conditions) for the CRUD surface over these tables

**Out**:
- `track_effects`, `effect_curves`, `project_send_buses`, `track_sends`, `project_frequency_labels` — covered by spec `local.engine-db-effects-and-curves` (see audit §5 #3)
- `pool_segments`, `pool_segment_tags` — already specced in scenecraft project (`local.pool-segments-and-variant-kind.md`)
- `dsp_*`, `mix_*`, `audio_description_*`, `audio_bounces` — covered by `local.engine-db-analysis-caches`
- `undo_log`, `redo_log`, `undo_groups`, `undo_state`, trigger-based capture — covered by `local.engine-db-undo-redo`
- Connection pool, WAL, retry-on-locked, transaction context manager — covered by `local.engine-connection-and-transactions`
- Migration framework — covered by `local.engine-migrations-framework`
- Keyframe / transition generation pipelines, render, compositor — separate specs
- Opacity keyframes, tracks (video), markers, bench, prompt_roster, effects (simple deprecated), suppressions — adjacent; out of scope here
- Plugin-owned sidecar tables (`generate_music__*`, `generate_foley__*`, `transcribe__*`, `light_show__*`, `audio_isolations`, `isolation_stems`) — each covered under its respective plugin spec

---

## Requirements

### Schema — keyframes

- **R1.** `keyframes` table has PK `id` TEXT and columns: `timestamp TEXT NOT NULL`, `section TEXT NOT NULL DEFAULT ''`, `source TEXT NOT NULL DEFAULT ''`, `prompt TEXT NOT NULL DEFAULT ''`, `selected INTEGER` (nullable; index into `candidates` list, or NULL), `candidates TEXT NOT NULL DEFAULT '[]'` (JSON array), `context TEXT` (nullable JSON object), `deleted_at TEXT` (nullable soft-delete stamp).
- **R2.** Migration-added columns (present on new DBs and back-filled on legacy): `track_id TEXT NOT NULL DEFAULT 'track_1'`, `label TEXT NOT NULL DEFAULT ''`, `label_color TEXT NOT NULL DEFAULT ''`, `blend_mode TEXT NOT NULL DEFAULT ''`, `opacity REAL` (nullable), `refinement_prompt TEXT NOT NULL DEFAULT ''`.
- **R3.** Indexes: `idx_keyframes_timestamp` on `timestamp`, `idx_keyframes_deleted` on `deleted_at`.
- **R4.** Soft-delete: `delete_keyframe(kf_id, deleted_at)` sets `deleted_at` to the supplied ISO string; row is preserved. `restore_keyframe(kf_id)` clears `deleted_at`. `get_keyframes(include_deleted=False)` filters `WHERE deleted_at IS NULL ORDER BY timestamp`; `get_binned_keyframes` returns only `deleted_at IS NOT NULL` rows.
- **R5.** `update_keyframe(kf_id, timestamp=<new>)` computes `delta = _parse_kf_timestamp(new) - _parse_kf_timestamp(old)` and, if non-zero, shifts `start_time` and `end_time` of every non-deleted `audio_clip` linked (via `audio_clip_links`) to any transition where `from_kf = kf_id AND deleted_at IS NULL`. Zero delta is a no-op. `_parse_kf_timestamp` accepts `'m:ss(.fff)'`, `'H:MM:SS(.fff)'`, or numeric; returns 0.0 on unparseable.

### Schema — transitions

- **R6.** `transitions` table has PK `id` TEXT and columns: `from_kf TEXT NOT NULL`, `to_kf TEXT NOT NULL`, `duration_seconds REAL NOT NULL DEFAULT 0`, `slots INTEGER NOT NULL DEFAULT 1`, `action TEXT NOT NULL DEFAULT ''`, `use_global_prompt INTEGER NOT NULL DEFAULT 0`, `selected TEXT NOT NULL DEFAULT '[]'` (JSON; single-slot flattens to scalar on read), `remap TEXT NOT NULL DEFAULT '{"method":"linear","target_duration":0}'`, `deleted_at TEXT`, `include_section_desc INTEGER NOT NULL DEFAULT 1`.
- **R7.** **There are NO foreign-key constraints** on `from_kf` / `to_kf`. A transition may reference a nonexistent or soft-deleted keyframe; SQLite will not reject such an insert or update. (Audit leak #8.)
- **R8.** Migration-added columns include (non-exhaustive): `track_id`, `label`, `label_color`, `tags`, `blend_mode`, `opacity`, 11 `*_curve` JSON columns (opacity/red/green/blue/black/hue_shift/saturation/invert/brightness/contrast/exposure), `is_adjustment`, `chroma_key` (JSON), mask fields (`mask_center_x/y`, `mask_radius`, `mask_feather`), transform fields (`transform_x/y`, `transform_x/y_curve`, `transform_scale_x/y_curve`, `anchor_x/y`), `hidden`, `ingredients` (JSON array), `negative_prompt`, `seed`, `trim_in REAL NOT NULL DEFAULT 0`, `trim_out REAL`, `source_video_duration REAL`.
- **R9.** Indexes: `idx_transitions_from` on `from_kf`, `idx_transitions_to` on `to_kf`, `idx_transitions_deleted` on `deleted_at`.
- **R10.** Soft-delete: `delete_transition(tr_id, deleted_at)` sets `deleted_at` on the row AND (a) soft-deletes every audio_clip whose `id` appears in `audio_clip_links WHERE transition_id = tr_id AND audio_clips.deleted_at IS NULL`, (b) **hard-deletes** every `audio_clip_links` row for that transition. `restore_transition(tr_id)` clears `deleted_at` on the transition only; linked clip rows and link rows are NOT automatically restored.
- **R11.** `add_transition` derives `track_id` from the `from_kf`'s `keyframes.track_id` when not supplied; falls back to `'track_1'` if the from_kf row is missing. `selected` is normalized: scalar/None becomes `[value]`, list is passed through.
- **R12.** `_row_to_transition` serializes `selected`: if stored value is a single-element list, the element is returned (flattened for frontend). Otherwise returned as-is. The stored column is always a JSON list.
- **R13.** `get_transitions(include_deleted=False)` returns `WHERE deleted_at IS NULL` (no ORDER BY — order undefined). `get_transitions_involving(kf_id)` filters `deleted_at IS NULL AND (from_kf = ? OR to_kf = ?)`.

### Schema — transition_effects

- **R14.** `transition_effects` table: PK `id` TEXT, `transition_id TEXT NOT NULL` (no FK constraint), `type TEXT NOT NULL`, `params TEXT NOT NULL DEFAULT '{}'`, `enabled INTEGER NOT NULL DEFAULT 1`, `z_order INTEGER NOT NULL DEFAULT 0`. Index `idx_tr_effects` on `transition_id`.
- **R15.** `add_transition_effect` computes the new row's `z_order` as `COALESCE(MAX(z_order), -1) + 1` scoped to the same `transition_id`. Returned ID is generated via `generate_id("tfx")`.
- **R16.** `delete_transition_effect` is a **hard delete** (`DELETE FROM transition_effects WHERE id = ?`). There is no soft-delete column and no cascade from transition soft-deletion (effects persist when their transition is soft-deleted).

### Schema — audio_tracks

- **R17.** `audio_tracks` table: PK `id` TEXT, `name TEXT NOT NULL DEFAULT 'Audio Track 1'`, `display_order INTEGER NOT NULL DEFAULT 0`, `hidden INTEGER NOT NULL DEFAULT 0`, `muted INTEGER NOT NULL DEFAULT 0`, `solo INTEGER NOT NULL DEFAULT 0` (migration-added; guarded in `_row_to_*` mapper), `volume_curve TEXT NOT NULL DEFAULT '[[0,0],[1,0]]'`.
- **R18.** **There is NO UNIQUE constraint on `display_order`.** Two rows may hold the same value (audit leak #9). The DAL `reorder_audio_tracks(track_ids)` writes `display_order = i` for each index; concurrent invocations have undefined ordering outcome.
- **R19.** `delete_audio_track(track_id)` **hard-deletes** the track row AND soft-deletes every `audio_clip WHERE track_id = ? AND deleted_at IS NULL` with `deleted_at = now(UTC ISO)`.
- **R20.** `get_audio_tracks()` returns rows `ORDER BY display_order`. Ties are returned in SQLite's implementation-defined order.

### Schema — audio_clips

- **R21.** `audio_clips` table: PK `id` TEXT, `track_id TEXT NOT NULL` (see R22), `source_path TEXT NOT NULL DEFAULT ''`, `start_time REAL NOT NULL DEFAULT 0`, `end_time REAL NOT NULL DEFAULT 0`, `source_offset REAL NOT NULL DEFAULT 0`, `volume_curve TEXT NOT NULL DEFAULT '[[0,0],[1,0]]'`, `muted INTEGER NOT NULL DEFAULT 0`, `remap TEXT NOT NULL DEFAULT '{"method":"linear","target_duration":0}'`, `label TEXT` (nullable), `deleted_at TEXT` (nullable). Migration-added: `selected TEXT` (nullable; references `pool_segments.id` but no FK).
- **R22.** **Migration gap**: freshly created DBs declare `track_id TEXT NOT NULL`. Legacy DBs created before the post-M13 master-bus effect migration may still have `track_id NOT NULL`; the schema-bootstrap does not rewrite this table to make `track_id` nullable. `PRAGMA table_info` does not report the notnull bit transition, so inspecting "is this DB migrated?" via column listing alone is unreliable (audit leak #17).
- **R23.** Indexes: `idx_audio_clips_track` on `track_id`, `idx_audio_clips_deleted` on `deleted_at`.
- **R24.** Soft-delete: `delete_audio_clip(clip_id)` sets `deleted_at = now(UTC ISO)`. No DAL restore helper is exposed (restore happens via undo log replay).
- **R25.** `get_audio_clips(track_id=None)` returns only non-deleted rows, enriched with derived fields:
  - `playback_rate`: 1.0 by default, or `source_span / kf_span` when the clip is linked to a transition (source_span = `(trim_out or source_video_duration) - trim_in`, kf_span = `to_kf.ts - from_kf.ts`; returns 1.0 when either span ≤ 0).
  - `effective_source_offset`: stored `source_offset` + linked transition's `trim_in` (0 if not linked).
  - `linked_transition_id`: the transition_id from `audio_clip_links` if linked, else None.
  - `variant_kind`: resolved from `pool_segments.variant_kind` via `selected` FK (None if no selection).
- **R26.** `update_audio_clip(clip_id, **fields)` allows arbitrary field updates; `muted` is int-coerced, `remap` and `volume_curve` are JSON-encoded when non-string.

### Schema — audio_candidates

- **R27.** `audio_candidates` table: `audio_clip_id TEXT NOT NULL REFERENCES audio_clips(id)`, `pool_segment_id TEXT NOT NULL REFERENCES pool_segments(id)`, `added_at TEXT NOT NULL`, `source TEXT NOT NULL`. PK `(audio_clip_id, pool_segment_id)`. Indexes: `idx_audio_cand_clip`, `idx_audio_cand_seg`.
- **R28.** FK constraints are **declared** in DDL. Per `engine-connection-and-transactions.md` R4+R26, the engine applies `PRAGMA foreign_keys=ON` as the final step of new-connection creation (post-schema-init). **Target state**: FK violations on `audio_candidates.audio_clip_id` / `audio_candidates.pool_segment_id` reject orphan inserts with `sqlite3.IntegrityError` at runtime. (Earlier drafts of this spec said FK enforcement was off — that was pre-OQ-8-resolution text and is no longer the contract.)
- **R29.** `add_audio_candidate(audio_clip_id, pool_segment_id, source, added_at=None)` uses `INSERT OR IGNORE` (idempotent on PK). `source` MUST be in `('generated', 'imported', 'chat_generation', 'plugin')` — assertion raises otherwise.
- **R30.** `get_audio_candidates(audio_clip_id)` joins `pool_segments`, returns ordered `ORDER BY added_at DESC` (newest first) with per-row `addedAt` and `junctionSource` fields appended.
- **R31.** `assign_audio_candidate(audio_clip_id, pool_segment_id_or_None)` sets `audio_clips.selected`; None reverts to the original `source_path`.
- **R32.** `remove_audio_candidate(audio_clip_id, pool_segment_id)` deletes the junction row AND clears `audio_clips.selected` if it matched the removed segment.

### Schema — tr_candidates

- **R33.** `tr_candidates` table: `transition_id TEXT NOT NULL`, `slot INTEGER NOT NULL DEFAULT 0`, `pool_segment_id TEXT NOT NULL REFERENCES pool_segments(id)`, `added_at TEXT NOT NULL`, `source TEXT NOT NULL`. PK `(transition_id, slot, pool_segment_id)`. Indexes: `idx_tr_candidates_tr`, `idx_tr_candidates_segment`, `idx_tr_candidates_order` on `(transition_id, slot, added_at)`.
- **R34.** **No FK** is declared on `transition_id`. Candidates can reference a nonexistent or deleted transition. (Same class of leak as R7.)
- **R35.** `add_tr_candidate(transition_id, slot, pool_segment_id, source, added_at=None)` uses `INSERT OR IGNORE`; `source` MUST be in `('generated', 'imported', 'split-inherit', 'cross-tr-copy')`.
- **R36.** `get_tr_candidates(transition_id, slot=0)` returns rows joined with `pool_segments`, ordered **`ORDER BY added_at ASC`** (oldest first). Rank is derived by enumeration order on read — there is no stored rank column.
- **R37.** `clone_tr_candidates(source_tr, target_tr, new_source='split-inherit')` copies every row from source to target preserving `slot` and `added_at`, uses `INSERT OR IGNORE`, returns count copied. Used for split/duplicate transition operations.

### Schema — audio_clip_links

- **R38.** `audio_clip_links` table: `audio_clip_id TEXT NOT NULL`, `transition_id TEXT NOT NULL`, `offset REAL NOT NULL DEFAULT 0`. PK `(audio_clip_id, transition_id)`. Indexes: `idx_acl_transition`, `idx_acl_audio_clip`. No FKs declared on either column.
- **R39.** `add_audio_clip_link` upserts via `INSERT … ON CONFLICT(audio_clip_id, transition_id) DO UPDATE SET offset = excluded.offset`.
- **R40.** `remove_audio_clip_links_for_transition(transition_id)` hard-deletes all link rows for a transition and returns the list of `audio_clip_id` values that were unlinked.

### Schema — sections

- **R41.** `sections` table: PK `id` TEXT, `label TEXT NOT NULL DEFAULT ''`, `start TEXT NOT NULL DEFAULT '0:00'`, `"end" TEXT` (nullable; double-quoted identifier because `end` is a SQLite keyword), `mood TEXT NOT NULL DEFAULT ''`, `energy TEXT NOT NULL DEFAULT ''`, `instruments TEXT NOT NULL DEFAULT '[]'`, `motifs TEXT NOT NULL DEFAULT '[]'`, `events TEXT NOT NULL DEFAULT '[]'`, `visual_direction TEXT NOT NULL DEFAULT ''`, `notes TEXT NOT NULL DEFAULT ''`, `sort_order INTEGER NOT NULL DEFAULT 0`.
- **R42.** `get_sections()` returns rows `ORDER BY sort_order`.
- **R43.** `set_sections(sections)` is a **full replace**: it `DELETE FROM sections` then inserts every supplied row with `sort_order = i` (index in supplied list). This is the only write path exposed; there is no granular `add_section`/`update_section`/`delete_section`.

### DAL rejection errors (target)

- **R51.** `delete_keyframe_hard(kf_id)` — if any row exists in `transitions` where `from_kf = kf_id OR to_kf = kf_id` (regardless of transition `deleted_at`), DAL raises `KeyframeInUseError` before attempting the DELETE. Transition hard-deletion MUST precede keyframe hard-deletion. (Resolves OQ-1.)
- **R52.** `add_audio_candidate` — if the referenced `audio_clip_id` row has `deleted_at IS NOT NULL`, DAL raises `AudioClipDeletedError`. No insert. (Resolves OQ-3.)
- **R53.** `add_tr_candidate` — if the referenced `transition_id` row has `deleted_at IS NOT NULL`, DAL raises `TransitionDeletedError`. No insert. (Resolves OQ-4.)
- **R54.** DAL validates JSON curve columns (all `*_curve` columns on `transitions`, and `volume_curve` on audio_tracks/audio_clips) for monotonic non-decreasing `x` on every insert/update; non-monotonic input raises `ValueError`. No sort, no tolerance. (Resolves OQ-6.)
- **R55.** Schema CHECK constraint on `transitions.remap` and `audio_clips.remap` rejects rows where the parsed `target_duration` is negative. Implemented via generated column or trigger (CHECK on JSON) at table creation; legacy rows validated on migration. (Resolves OQ-7.)
- **R56.** `reorder_audio_tracks` holds no internal lock spanning multiple API calls. Concurrent invocations from the same user/project are undefined per INV-1 (single-writer per (user, project)). (Resolves OQ-5.)

### General — JSON columns

- **R44.** `keyframes.candidates` is a JSON array of candidate IDs/metadata; `_row_to_keyframe` runs `json.loads`. Default `'[]'` parses as `[]`.
- **R45.** `keyframes.context` is a JSON object or NULL; `_row_to_keyframe` returns None when NULL, dict otherwise.
- **R46.** `audio_tracks.volume_curve` and `audio_clips.volume_curve` are JSON arrays of `[x, dB]` tuples. Default shape is `[[0, 0], [1, 0]]`. There is no runtime validation of monotonicity of x values.
- **R47.** `transitions.remap` and `audio_clips.remap` are JSON objects shaped `{"method": str, "target_duration": number}`. Default `{"method": "linear", "target_duration": 0}`. No runtime validation of `target_duration >= 0`.
- **R48.** `transitions.selected` is stored as a JSON list (one element per slot, `null` allowed). Single-element lists are flattened to scalars on read (R12).
- **R49.** `transitions.tags`, `transitions.ingredients` are JSON arrays; empty list is the default.
- **R50.** Curve columns on `transitions` (`opacity_curve`, `red_curve`, etc.) are nullable JSON arrays of `[x, y]` points when non-null. No monotonicity or range validation at the DAL layer.

---

## Interfaces / Data Shapes

### DAL Public Contract (selected)

```
# keyframes
get_keyframes(project_dir, include_deleted=False) -> list[dict]
get_keyframe(project_dir, kf_id) -> dict | None
get_binned_keyframes(project_dir) -> list[dict]
add_keyframe(project_dir, kf: dict) -> None   # INSERT OR REPLACE
update_keyframe(project_dir, kf_id, **fields) -> None   # propagates to linked audio on timestamp change
delete_keyframe(project_dir, kf_id, deleted_at: str) -> None   # soft
restore_keyframe(project_dir, kf_id) -> None

# transitions
get_transitions(project_dir, include_deleted=False) -> list[dict]
get_transition(project_dir, tr_id) -> dict | None
get_transitions_involving(project_dir, kf_id) -> list[dict]
add_transition(project_dir, tr: dict) -> None   # INSERT OR REPLACE, derives track_id
update_transition(project_dir, tr_id, **fields) -> None
delete_transition(project_dir, tr_id, deleted_at: str) -> None   # soft + cascade link cleanup
restore_transition(project_dir, tr_id) -> None

# transition_effects
get_transition_effects(project_dir, transition_id) -> list[dict]
get_all_transition_effects(project_dir) -> dict[str, list[dict]]
add_transition_effect(project_dir, transition_id, effect_type, params=None) -> str
update_transition_effect(project_dir, effect_id, **fields) -> None
delete_transition_effect(project_dir, effect_id) -> None   # hard

# audio_tracks
get_audio_tracks(project_dir) -> list[dict]
add_audio_track(project_dir, track: dict) -> None
update_audio_track(project_dir, track_id, **fields) -> None
delete_audio_track(project_dir, track_id) -> None   # hard track + cascade soft-delete clips
reorder_audio_tracks(project_dir, track_ids: list[str]) -> None

# audio_clips
get_audio_clips(project_dir, track_id=None) -> list[dict]   # enriched with derived fields
add_audio_clip(project_dir, clip: dict) -> None
update_audio_clip(project_dir, clip_id, **fields) -> None
delete_audio_clip(project_dir, clip_id) -> None   # soft

# audio_clip_links
add_audio_clip_link(project_dir, audio_clip_id, transition_id, offset=0.0) -> None   # upsert
get_audio_clip_links_for_transition(project_dir, transition_id) -> list[dict]
get_audio_clip_links_for_clip(project_dir, audio_clip_id) -> list[dict]
remove_audio_clip_link(project_dir, audio_clip_id, transition_id) -> None   # hard
remove_audio_clip_links_for_transition(project_dir, transition_id) -> list[str]
update_audio_clip_link_offset(project_dir, audio_clip_id, transition_id, offset) -> None

# tr_candidates
add_tr_candidate(project_dir, *, transition_id, slot, pool_segment_id, source, added_at=None) -> None
remove_tr_candidate(project_dir, transition_id, slot, pool_segment_id) -> None
get_tr_candidates(project_dir, transition_id, slot=0) -> list[dict]   # ORDER BY added_at ASC
clone_tr_candidates(project_dir, *, source_transition_id, target_transition_id, new_source='split-inherit') -> int
count_tr_candidate_refs(project_dir, pool_segment_id) -> int

# audio_candidates
add_audio_candidate(project_dir, *, audio_clip_id, pool_segment_id, source, added_at=None) -> None
get_audio_candidates(project_dir, audio_clip_id) -> list[dict]   # ORDER BY added_at DESC
assign_audio_candidate(project_dir, audio_clip_id, pool_segment_id_or_None) -> None
remove_audio_candidate(project_dir, audio_clip_id, pool_segment_id) -> None
get_audio_clip_effective_path(project_dir, audio_clip: dict) -> str

# sections
get_sections(project_dir) -> list[dict]
set_sections(project_dir, sections: list[dict]) -> None   # full replace
```

### `remap` JSON shape

```json
{ "method": "linear", "target_duration": 0.0 }
```

`method` is a string enum (observed: `"linear"`; schema does not CHECK). `target_duration` is seconds.

### `volume_curve` JSON shape

```
[[x0, dB0], [x1, dB1], ...]
```

`x` is a normalized 0..1 timeline position; `dB` is gain offset. Default `[[0,0],[1,0]]` = flat 0 dB.

### `candidates` / `selected` shapes

- `keyframes.candidates`: JSON array (implementation-free; callers decide element shape)
- `keyframes.selected`: INTEGER index into `candidates`, or NULL
- `transitions.selected`: JSON list with one element per slot (IDs or NULL); flattened to scalar on read for single-slot transitions
- `audio_clips.selected`: TEXT pool_segment_id or NULL

---

## Behavior Table

| # | Scenario | Expected Behavior | Tests |
|---|----------|-------------------|-------|
| 1 | add_keyframe then get_keyframe | Round-trips all scalar + JSON columns | `add-then-get-keyframe` |
| 2 | delete_keyframe sets deleted_at | Row preserved, get_keyframes (default) hides it | `delete-keyframe-soft` |
| 3 | restore_keyframe clears deleted_at | Row reappears in get_keyframes | `restore-keyframe` |
| 4 | get_keyframes(include_deleted=True) | Returns both live and binned rows, ordered by timestamp | `get-keyframes-include-deleted` |
| 5 | update_keyframe shifts timestamp | Linked audio clip start/end shifted by same delta | `keyframe-timestamp-propagates-to-audio` |
| 6 | update_keyframe with zero delta | No audio shift occurs | `keyframe-timestamp-zero-delta-noop` |
| 7 | add_transition deriving track_id | track_id inherited from from_kf | `transition-derives-track-from-kf` |
| 8 | delete_transition cascade | transition soft-deleted; linked audio_clips soft-deleted; link rows hard-deleted | `delete-transition-cascades-to-audio` |
| 9 | restore_transition after cascade | Transition restored; linked clips and links NOT restored | `restore-transition-partial` |
| 10 | get_transitions default filter | Excludes deleted_at IS NOT NULL | `get-transitions-excludes-deleted` |
| 11 | transitions.selected single-slot flatten | Single-element list returned as scalar | `transition-selected-flatten` |
| 12 | add_transition_effect z_order computed | New row has max(z_order)+1 scoped by transition | `transition-effect-z-order-autoincrement` |
| 13 | delete_transition_effect hard-deletes | Row is physically removed | `delete-transition-effect-hard` |
| 14 | Effects persist when parent transition soft-deleted | transition_effects rows untouched | `effects-persist-on-transition-soft-delete` |
| 15 | reorder_audio_tracks writes sequential display_order | Row i has display_order = i | `reorder-audio-tracks-sequential` |
| 16 | get_audio_tracks orders by display_order | Ascending order preserved | `audio-tracks-ordered` |
| 17 | delete_audio_track cascades to clips | Track hard-deleted; its non-deleted clips soft-deleted with UTC ISO stamp | `delete-audio-track-cascades` |
| 18 | add_audio_clip then get_audio_clips | Returns row with derived playback_rate=1.0, effective_source_offset=source_offset | `audio-clip-unlinked-derivations` |
| 19 | Linked audio clip derived fields | playback_rate = source_span/kf_span, eff_offset = offset + trim_in | `audio-clip-linked-derivations` |
| 20 | audio_clips.selected resolves variant_kind | Derived field matches pool_segments.variant_kind | `audio-clip-variant-kind-resolution` |
| 21 | delete_audio_clip sets deleted_at | Row hidden from default get_audio_clips | `delete-audio-clip-soft` |
| 22 | add_tr_candidate is idempotent on PK | Second insert is silently ignored | `tr-candidate-idempotent` |
| 23 | get_tr_candidates orders by added_at ASC | Oldest first | `tr-candidates-order-ascending` |
| 24 | add_audio_candidate idempotent | INSERT OR IGNORE on (clip, segment) PK | `audio-candidate-idempotent` |
| 25 | get_audio_candidates orders DESC | Newest first | `audio-candidates-order-descending` |
| 26 | assign_audio_candidate(None) | selected cleared; effective path reverts to source_path | `assign-audio-candidate-none-reverts` |
| 27 | remove_audio_candidate clears selection when matched | selected set to NULL only if it matched removed segment | `remove-audio-candidate-clears-selection` |
| 28 | clone_tr_candidates preserves slot + added_at | Target tr has identical slot/added_at; source tag stored via new_source | `clone-tr-candidates-preserves-ordering` |
| 29 | audio_clip_link upsert | Second add_audio_clip_link updates offset, no duplicate row | `audio-clip-link-upsert` |
| 30 | remove_audio_clip_links_for_transition returns ids | Returns list of clip ids that were unlinked | `unlink-transition-returns-ids` |
| 31 | set_sections full replace | Deletes all existing rows, inserts new with sort_order = index | `set-sections-replaces` |
| 32 | get_sections orders by sort_order | Ascending | `get-sections-ordered` |
| 33 | add_tr_candidate bad source raises | AssertionError for source not in allowlist | `tr-candidate-bad-source-assertion` |
| 34 | add_audio_candidate bad source raises | AssertionError for source not in allowlist | `audio-candidate-bad-source-assertion` |
| 35 | Hard-delete a keyframe with live transitions | DAL raises `KeyframeInUseError` if any transition (soft-deleted or not) references kf.id via from_kf/to_kf | `keyframe-in-use-blocks-hard-delete` |
| 36 | audio_clip whose track_id references a deleted track | Existing cascade (delete_audio_track soft-deletes clips) handles this; no new semantic | `audio-clip-track-cascade-preexisting` |
| 37 | add_audio_candidate for a soft-deleted audio_clip | DAL raises `AudioClipDeletedError` | `add-audio-candidate-on-deleted-clip-rejected` |
| 38 | add_tr_candidate for a soft-deleted transition | DAL raises `TransitionDeletedError` | `add-tr-candidate-on-deleted-transition-rejected` |
| 39 | Concurrent reorder_audio_tracks producing display_order collisions | Undefined by INV-1 (single-writer per (user, project)); no internal lock held | `reorder-audio-tracks-no-internal-lock` |
| 40 | JSON curve with non-monotonic x values | DAL raises `ValueError` on insert/update; no tolerance-and-sort | `curve-non-monotonic-x-rejected` |
| 41 | remap.target_duration < 0 | CHECK constraint rejects at DB layer | `remap-negative-target-duration-rejected` |
| 42 | transitions.from_kf references a nonexistent keyframe | Insert/update succeeds (no FK); get_transition returns the row; _row_to_transition emits it with the dangling id | `transition-dangling-from-kf-allowed` |
| 43 | Legacy DB with `audio_clips.track_id NOT NULL` after master-bus migration | Target: table-rebuild migration via `register_migration` + `rebuild_table` helper re-creates audio_clips with nullable track_id. Transitional: column-existence check leaves NOT NULL in place | `audio-clips-legacy-nullable-track-id-rebuild` |
| 44 | FK declared on audio_candidates.audio_clip_id with PRAGMA foreign_keys=ON | Orphan insert raises `sqlite3.IntegrityError` at runtime | `audio-candidate-orphan-insert-rejects` |
| 45 | delete_keyframe with already-deleted_at | deleted_at overwritten with new value | `delete-keyframe-already-deleted-overwrites` |
| 46 | update_transition with no fields | No-op; returns without executing UPDATE | `update-transition-noop-empty` |
| 47 | _parse_kf_timestamp on unparseable input | Returns 0.0; propagation is a no-op | `parse-kf-timestamp-fallback` |
| 48 | update_keyframe audio-propagation raises DatabaseError | Main update succeeds; propagation error is logged, not raised | `keyframe-update-propagation-error-swallowed` |

---

## Behavior

1. **Connection**: every DAL function resolves the per-project SQLite connection via `get_db(project_dir)` (thread-local; WAL; 60s busy timeout). Writes call `conn.commit()` explicitly.
2. **Row mapping**: each entity has a `_row_to_<entity>(row)` helper that parses JSON columns, coerces booleans from INTEGER, and guards post-migration columns with `"col" in row.keys()` to tolerate un-migrated DBs.
3. **Soft-delete semantics**:
   - Soft-delete tables: `keyframes`, `transitions`, `audio_clips`.
   - Hard-delete tables (in scope): `transition_effects`, `audio_tracks`, `audio_clip_links`, `audio_candidates`, `tr_candidates`, `sections`.
   - `audio_clips` soft-delete is driven by DAL (no FK cascade): both `delete_audio_track` and `delete_transition` manually soft-delete linked clips.
4. **FK gaps** (codified, not fixed here):
   - `transitions.from_kf` / `to_kf`: no FK. Dangling references allowed (R7 / test #42).
   - `transition_effects.transition_id`: no FK.
   - `audio_clips.track_id`: no FK.
   - `audio_clip_links` columns: no FKs.
   - `tr_candidates.transition_id`: no FK (`pool_segment_id` declares one).
   - `audio_candidates` declares FKs and `PRAGMA foreign_keys=ON` is applied post-schema-init per R28 and the connection spec; orphan inserts are rejected at runtime.
5. **Ordering contracts**:
   - `tr_candidates`: ASCENDING by `added_at` (oldest first — rank v1 = oldest).
   - `audio_candidates`: DESCENDING by `added_at` (newest first).
   - `audio_tracks`: ASCENDING by `display_order` (no UNIQUE; ties implementation-defined).
   - `keyframes`: ASCENDING by `timestamp` (lexicographic on TEXT; numeric parsing happens downstream).
   - `sections`: ASCENDING by `sort_order`.
   - `transitions`: no stable order from `get_transitions`.
6. **Undo coupling**: all DAL writes are captured by undo triggers defined in `undo_log` (specced separately). This spec does NOT describe the trigger behavior but it is an out-of-band observer of every write covered here.

---

## Acceptance Criteria

- [ ] Unit tests exist for every row in the Behavior Table except rows 35–41 and 43, which remain as `undefined` until resolved in Open Questions.
- [ ] Every DAL function listed in "DAL Public Contract" has a signature-level test confirming shape of return value.
- [ ] Every `_row_to_*` helper has a test round-tripping each JSON column through `add_*` → DB → `get_*` verifying `json.loads` parity.
- [ ] Delete cascades (R10, R19) have tests asserting both the primary row state and the derived row states.
- [ ] FK gap tests (#42, #44) succeed by demonstrating orphan inserts are NOT rejected — documenting the gap.
- [ ] Migration gap (R22) has an Open Question ticket referenced from any test that would depend on nullable `track_id`.

---

## Tests

### Base Cases

#### Test: add-then-get-keyframe (covers R1, R2)
**Given**: A fresh project DB and a keyframe dict with all documented fields populated (including `candidates=[{"foo": 1}]` and `context={"k": "v"}`).
**When**: `add_keyframe(project_dir, kf)` then `get_keyframe(project_dir, kf['id'])`.
**Then** (assertions):
- **id-roundtrip**: returned dict's `id` == supplied id
- **json-candidates-parsed**: returned `candidates` is a list equal to supplied list (not a JSON string)
- **json-context-parsed**: returned `context` is a dict equal to supplied dict
- **defaults-applied**: omitted `section`/`source`/`prompt` default to empty string
- **deleted-at-null**: `deleted_at` is None on a freshly-added row

#### Test: delete-keyframe-soft (covers R4)
**Given**: An added keyframe.
**When**: `delete_keyframe(project_dir, kf_id, "2026-04-27T00:00:00Z")`.
**Then**:
- **row-preserved**: `get_keyframe(project_dir, kf_id)` still returns the row
- **deleted-at-set**: `deleted_at` equals supplied ISO string
- **excluded-from-default-list**: `get_keyframes(project_dir)` does NOT contain this id
- **present-in-binned**: `get_binned_keyframes(project_dir)` contains this id

#### Test: restore-keyframe (covers R4)
**Given**: A soft-deleted keyframe.
**When**: `restore_keyframe(project_dir, kf_id)`.
**Then**:
- **deleted-at-null**: `deleted_at` is None
- **included-in-default-list**: `get_keyframes(project_dir)` contains this id

#### Test: get-keyframes-include-deleted (covers R4)
**Given**: Two live keyframes and one soft-deleted keyframe.
**When**: `get_keyframes(project_dir, include_deleted=True)`.
**Then**:
- **all-three-returned**: Result length is 3
- **ordered-by-timestamp**: Result ordered by `timestamp` lexicographic ascending

#### Test: keyframe-timestamp-propagates-to-audio (covers R5)
**Given**: Keyframe K at `0:10.000`, transition T `from_kf=K`, audio clip C linked to T via `audio_clip_links`, clip at `start_time=10, end_time=14`.
**When**: `update_keyframe(project_dir, K.id, timestamp='0:12.000')`.
**Then**:
- **clip-start-shifted**: clip's `start_time` == 12
- **clip-end-shifted**: clip's `end_time` == 16
- **clip-not-soft-deleted**: clip's `deleted_at` is None

#### Test: transition-derives-track-from-kf (covers R11)
**Given**: A keyframe K with `track_id='track_7'` and a transition dict with `from=K.id` and no explicit `track_id`.
**When**: `add_transition(project_dir, tr)`.
**Then**:
- **track-id-inherited**: stored transition's `track_id` == 'track_7'

#### Test: delete-transition-cascades-to-audio (covers R10)
**Given**: Transition T with two linked audio clips A and B (via `audio_clip_links`), both non-deleted.
**When**: `delete_transition(project_dir, T.id, "2026-04-27T00:00:00Z")`.
**Then**:
- **transition-soft-deleted**: T.deleted_at is set
- **clip-a-soft-deleted**: A.deleted_at == supplied stamp
- **clip-b-soft-deleted**: B.deleted_at == supplied stamp
- **links-hard-deleted**: `get_audio_clip_links_for_transition(T.id)` returns empty list

#### Test: restore-transition-partial (covers R10)
**Given**: Just-cascaded transition from previous test.
**When**: `restore_transition(project_dir, T.id)`.
**Then**:
- **transition-restored**: T.deleted_at is None
- **clips-still-deleted**: A and B still have `deleted_at` set (no auto-restore)
- **links-not-recreated**: `get_audio_clip_links_for_transition(T.id)` is still empty

#### Test: transition-selected-flatten (covers R12)
**Given**: Transition row with `selected='[null]'` (single-element list of None).
**When**: `get_transition(project_dir, tr_id)`.
**Then**:
- **selected-scalar-none**: returned `selected` is None (not `[None]`)

#### Test: transition-effect-z-order-autoincrement (covers R15)
**Given**: Transition T with existing effects at z_order 0 and 1.
**When**: `add_transition_effect(project_dir, T.id, 'blur')`.
**Then**:
- **new-z-order-2**: new row's z_order == 2
- **scoped-to-transition**: effects on other transitions are not considered in the max computation

#### Test: delete-transition-effect-hard (covers R16)
**Given**: One effect exists.
**When**: `delete_transition_effect(project_dir, effect_id)`.
**Then**:
- **row-gone**: row no longer present in `transition_effects`
- **no-deleted-at-column**: schema has no `deleted_at` on `transition_effects`

#### Test: reorder-audio-tracks-sequential (covers R18, R20)
**Given**: Three tracks `A`, `B`, `C`.
**When**: `reorder_audio_tracks(project_dir, ['C', 'A', 'B'])` then `get_audio_tracks(project_dir)`.
**Then**:
- **c-first**: First returned row id is `C` with `display_order=0`
- **a-second**: Second is `A` with `display_order=1`
- **b-third**: Third is `B` with `display_order=2`

#### Test: delete-audio-track-cascades (covers R19)
**Given**: Track T with clip C1 non-deleted and clip C2 already soft-deleted.
**When**: `delete_audio_track(project_dir, T.id)`.
**Then**:
- **track-hard-deleted**: T row gone from `audio_tracks`
- **c1-soft-deleted**: C1.deleted_at is a UTC ISO string (newly set)
- **c2-unchanged**: C2.deleted_at retains its prior value (not overwritten)

#### Test: audio-clip-unlinked-derivations (covers R25)
**Given**: Clip C on track T with `source_offset=3.0`, not linked to any transition.
**When**: `get_audio_clips(project_dir, track_id=T.id)`.
**Then**:
- **playback-rate-one**: `playback_rate` == 1.0
- **effective-offset-equals-source-offset**: `effective_source_offset` == 3.0
- **linked-transition-none**: `linked_transition_id` is None

#### Test: audio-clip-linked-derivations (covers R25)
**Given**: Clip C linked to transition T with `trim_in=1.0`, `trim_out=5.0`, keyframes spanning `0:00 → 0:02` (kf_span=2.0), clip `source_offset=0.5`.
**When**: `get_audio_clips(project_dir)`.
**Then**:
- **playback-rate-two**: `playback_rate` == 2.0 (source_span=4 / kf_span=2)
- **effective-offset-is-offset-plus-trim-in**: `effective_source_offset` == 1.5

#### Test: tr-candidates-order-ascending (covers R36)
**Given**: Three candidates for (T, slot=0) with `added_at` '2026-01-01', '2026-02-01', '2026-03-01'.
**When**: `get_tr_candidates(project_dir, T.id, slot=0)`.
**Then**:
- **jan-first**: First element has addedAt == '2026-01-01'
- **mar-last**: Last element has addedAt == '2026-03-01'

#### Test: audio-candidates-order-descending (covers R30)
**Given**: Three audio candidates with added_at '2026-01-01', '2026-02-01', '2026-03-01'.
**When**: `get_audio_candidates(project_dir, clip_id)`.
**Then**:
- **mar-first**: First element has addedAt == '2026-03-01'
- **jan-last**: Last element has addedAt == '2026-01-01'

#### Test: assign-audio-candidate-none-reverts (covers R31)
**Given**: Clip C with `selected=seg_42` and `source_path='orig.wav'`, and a `pool_segments` row with `poolPath='variant.wav'`.
**When**: `assign_audio_candidate(project_dir, C.id, None)` then `get_audio_clip_effective_path`.
**Then**:
- **selected-cleared**: `audio_clips.selected` IS NULL
- **effective-path-reverts**: effective path == 'orig.wav'

#### Test: remove-audio-candidate-clears-selection (covers R32)
**Given**: Clip C with `selected=seg_42` and audio_candidates row linking seg_42.
**When**: `remove_audio_candidate(project_dir, C.id, 'seg_42')`.
**Then**:
- **junction-gone**: junction row removed
- **selected-null**: `audio_clips.selected` IS NULL

#### Test: clone-tr-candidates-preserves-ordering (covers R37)
**Given**: Transition S has 3 candidates with specific (slot, added_at) pairs.
**When**: `clone_tr_candidates(project_dir, source_transition_id=S.id, target_transition_id=T.id)`.
**Then**:
- **count-returned**: return value == 3
- **slot-preserved**: target rows have same slot values as source
- **added-at-preserved**: target rows have identical added_at
- **source-rewritten**: target rows have `source='split-inherit'`

#### Test: audio-clip-link-upsert (covers R39)
**Given**: Existing link (clip=C, tr=T, offset=0.0).
**When**: `add_audio_clip_link(project_dir, C.id, T.id, offset=2.5)`.
**Then**:
- **no-duplicate**: exactly one row in `audio_clip_links` for (C, T)
- **offset-updated**: offset == 2.5

#### Test: set-sections-replaces (covers R43)
**Given**: Existing 3 sections in DB.
**When**: `set_sections(project_dir, [sec_x, sec_y])`.
**Then**:
- **count-two**: `get_sections` returns exactly 2 rows
- **sort-order-sequential**: sec_x.sort_order=0, sec_y.sort_order=1
- **old-sections-gone**: none of the pre-existing ids are present

#### Test: get-sections-ordered (covers R42)
**Given**: DB populated via `set_sections` with 4 dicts in specific order.
**When**: `get_sections(project_dir)`.
**Then**:
- **ascending-sort-order**: rows returned in the order supplied (sort_order 0,1,2,3)

### Edge Cases

#### Test: keyframe-timestamp-zero-delta-noop (covers R5)
**Given**: Keyframe K at `'0:10'` with a linked audio clip at start_time=10.
**When**: `update_keyframe(project_dir, K.id, timestamp='0:10')`.
**Then**:
- **no-audio-shift**: clip's start_time remains 10
- **no-propagation-query**: propagation helper short-circuits (observable via no mutation)

#### Test: parse-kf-timestamp-fallback (covers R5)
**Given**: An unparseable timestamp `'not-a-time'`.
**When**: `_parse_kf_timestamp('not-a-time')` is called and `update_keyframe` uses it.
**Then**:
- **zero-returned**: helper returns 0.0
- **no-propagation**: downstream audio clip shift is 0 (no mutation)

#### Test: keyframe-update-propagation-error-swallowed (covers R5)
**Given**: `_propagate_linked_audio_on_from_kf_shift` is patched to raise `sqlite3.DatabaseError`.
**When**: `update_keyframe(project_dir, kf_id, timestamp=new)` with non-zero delta.
**Then**:
- **main-update-applied**: keyframes.timestamp == new
- **error-not-raised**: call returns normally
- **error-logged-to-stderr**: one log line containing the kf_id and delta appears on stderr

#### Test: audio-clip-variant-kind-resolution (covers R25)
**Given**: Clip C.selected = 'seg_9'; pool_segments['seg_9'].variant_kind = 'isolate-vocal'.
**When**: `get_audio_clips(project_dir)`.
**Then**:
- **variant-kind-resolved**: returned dict has `variant_kind == 'isolate-vocal'`
- **no-n-plus-one**: Implementation-agnostic check — not asserted directly, but a perf test may bound the query count (see Non-Goals)

#### Test: tr-candidate-idempotent (covers R35)
**Given**: Row (T.id, 0, seg_1, 'generated', 'ts1') already present.
**When**: `add_tr_candidate(project_dir, transition_id=T.id, slot=0, pool_segment_id='seg_1', source='generated')`.
**Then**:
- **no-new-row**: `get_tr_candidates(T.id, 0)` still has 1 element
- **original-added-at-preserved**: addedAt still 'ts1'

#### Test: audio-candidate-idempotent (covers R29)
**Given**: Row (C.id, seg_1) present.
**When**: `add_audio_candidate(project_dir, audio_clip_id=C.id, pool_segment_id='seg_1', source='generated')`.
**Then**:
- **no-new-row**: `get_audio_candidates(C.id)` has 1 element
- **original-added-at-preserved**: unchanged

#### Test: tr-candidate-bad-source-assertion (covers R35)
**Given**: N/A.
**When**: `add_tr_candidate(project_dir, transition_id=T.id, slot=0, pool_segment_id='x', source='bogus')`.
**Then**:
- **assertion-raised**: AssertionError with message mentioning 'bad source'

#### Test: audio-candidate-bad-source-assertion (covers R29)
**Given**: N/A.
**When**: `add_audio_candidate(project_dir, audio_clip_id=C.id, pool_segment_id='x', source='bogus')`.
**Then**:
- **assertion-raised**: AssertionError with message mentioning 'bad source'

#### Test: delete-keyframe-already-deleted-overwrites (covers R4)
**Given**: Keyframe with `deleted_at='T1'`.
**When**: `delete_keyframe(project_dir, kf_id, 'T2')`.
**Then**:
- **deleted-at-overwritten**: `deleted_at == 'T2'`

#### Test: update-transition-noop-empty (covers R12)
**Given**: Existing transition.
**When**: `update_transition(project_dir, tr_id)` with no keyword args.
**Then**:
- **no-update-executed**: function returns without writing
- **row-unchanged**: all columns identical to pre-call state

#### Test: effects-persist-on-transition-soft-delete (covers R16)
**Given**: Transition T with two `transition_effects` rows.
**When**: `delete_transition(project_dir, T.id, 'ts')`.
**Then**:
- **effects-row-count-unchanged**: `get_transition_effects(T.id)` still returns both rows
- **no-cascade-column**: schema has no foreign key or trigger removing effects

#### Test: transition-dangling-from-kf-allowed (covers R7)
**Given**: No keyframe with id 'ghost-kf'.
**When**: `add_transition(project_dir, {'id': 'T', 'from': 'ghost-kf', 'to': 'ghost-kf', ...})`.
**Then**:
- **insert-succeeds**: No exception
- **row-readable**: `get_transition('T')` returns a dict with `from == 'ghost-kf'`
- **track-id-fallback**: derived `track_id` == `'track_1'` (from_kf not found)

#### Test: audio-candidate-orphan-insert-rejects (covers R28)
**Given**: No `audio_clips` row with id `'missing-clip'`. Connection created via `get_db` which applies `PRAGMA foreign_keys=ON` post-schema-init.
**When**: `add_audio_candidate(project_dir, audio_clip_id='missing-clip', pool_segment_id=<real>, source='imported')`.
**Then**:
- **insert-rejects**: `sqlite3.IntegrityError` raised (FK violation; PRAGMA foreign_keys=ON)
- **row-absent**: No row in `audio_candidates` for the attempted insert
- **matches-connection-spec**: Behavior aligns with `engine-connection-and-transactions.md` R4+R26

#### Test: unlink-transition-returns-ids (covers R40)
**Given**: 3 link rows for T.id.
**When**: `remove_audio_clip_links_for_transition(project_dir, T.id)`.
**Then**:
- **returns-three-ids**: return value is a list of length 3
- **rows-gone**: `get_audio_clip_links_for_transition(T.id)` returns empty

#### Test: keyframe-in-use-blocks-hard-delete (covers R51, resolves OQ-1)
**Given**: Keyframe K referenced by transition T.from_kf (T not soft-deleted).
**When**: `delete_keyframe_hard(project_dir, K.id)`.
**Then**:
- **raises-keyframe-in-use**: `KeyframeInUseError` raised
- **row-preserved**: K still present in `keyframes`
- **transition-unchanged**: T row unchanged

#### Test: audio-clip-track-cascade-preexisting (covers OQ-2 close)
**Given**: Track T with clip C1 (non-deleted).
**When**: `delete_audio_track(project_dir, T.id)` then a new `add_audio_clip` referencing `T.id` as `track_id`.
**Then**:
- **c1-soft-deleted**: C1.deleted_at set by existing cascade (R19)
- **no-new-error-path**: spec adds no new semantic; existing cascade is authoritative

#### Test: add-audio-candidate-on-deleted-clip-rejected (covers R52, resolves OQ-3)
**Given**: Clip C with `deleted_at='T1'`.
**When**: `add_audio_candidate(project_dir, audio_clip_id=C.id, pool_segment_id='seg_1', source='generated')`.
**Then**:
- **raises-audio-clip-deleted**: `AudioClipDeletedError` raised
- **no-row-inserted**: `audio_candidates` count unchanged

#### Test: add-tr-candidate-on-deleted-transition-rejected (covers R53, resolves OQ-4)
**Given**: Transition T with `deleted_at='T1'`.
**When**: `add_tr_candidate(project_dir, transition_id=T.id, slot=0, pool_segment_id='seg_1', source='generated')`.
**Then**:
- **raises-transition-deleted**: `TransitionDeletedError` raised
- **no-row-inserted**: `tr_candidates` count unchanged

#### Test: reorder-audio-tracks-no-internal-lock (covers R56, resolves OQ-5, INV-1 negative-assertion)
**Given**: Three tracks A, B, C.
**When**: `reorder_audio_tracks(project_dir, ['C','A','B'])` is invoked with a mock asserting no acquisition of a project-scoped mutex / module-level lock during the call.
**Then**:
- **no-internal-lock-held**: no `threading.Lock` or `asyncio.Lock` is acquired across the API call boundary
- **concurrency-undefined**: spec asserts concurrency is undefined per INV-1; this test is a negative assertion, not a race test

#### Test: curve-non-monotonic-x-rejected (covers R54, resolves OQ-6)
**Given**: Transition T exists.
**When**: `update_transition(project_dir, T.id, opacity_curve=[[0,0],[0.5,1],[0.3,0.5]])`.
**Then**:
- **raises-value-error**: `ValueError` with message mentioning non-monotonic x
- **row-unchanged**: T.opacity_curve still the pre-call value

#### Test: remap-negative-target-duration-rejected (covers R55, resolves OQ-7)
**Given**: Transition T exists.
**When**: `update_transition(project_dir, T.id, remap={'method':'linear','target_duration':-1})`.
**Then**:
- **check-constraint-violation**: `sqlite3.IntegrityError` (CHECK) raised at DB layer
- **row-unchanged**: T.remap still the pre-call value

#### Test: audio-clips-legacy-nullable-track-id-rebuild (covers R_transitional + OQ-8, target)
**Given**: Legacy DB where `audio_clips.track_id` was created as NOT NULL.
**When**: Target migration runs via `register_migration` applying `rebuild_table('audio_clips', new_schema_with_nullable_track_id)`.
**Then**:
- **column-nullable-post-migration**: `PRAGMA table_info(audio_clips)` reports track_id notnull=0
- **rows-preserved**: row count and content identical across the rebuild
- **transitional-note**: until `register_migration`/`rebuild_table` lands, legacy DBs keep NOT NULL — master-bus effect migration must not depend on nullable track_id

---

## Transitional Behavior (INV-8)

Target-ideal behavior is captured in Requirements R51–R56. The following current code divergences are documented, not codified as the eventual contract:

- **Legacy `audio_clips.track_id NOT NULL`**: schema bootstrap uses column-existence check (additive ALTER only). Making `track_id` nullable on legacy DBs requires the future `plugin_api.register_migration` + `migrate.rebuild_table` helpers (see `local.engine-migrations-framework` OQ-4). Until those land, legacy DBs retain the NOT NULL constraint; master-bus-effect paths that rely on nullable track_id MUST be guarded or deferred.
- **No CHECK on `remap.target_duration`**: current DDL stores the JSON blob without validation; R55 requires a schema rebuild which, like above, depends on the future migration helpers.
- **No DAL rejection errors for in-use/deleted entities today**: R51–R53 describe target-ideal errors not yet raised. Current code silently accepts these operations; tests for the errors will fail until the DAL is patched.

---

## Non-Goals

- Enforcing FK constraints at the schema level for `transitions.from_kf` / `to_kf` / `transition_id` junctions (see audit leak #8 — a separate migration is required; not this spec's remit).
- Adding UNIQUE(display_order) on `audio_tracks` (audit leak #9).
- Making `audio_clips.track_id` nullable on legacy DBs (audit leak #17).
- Schema-level JSON validation (monotonic curves, target_duration >= 0). These remain DAL-layer or API-layer responsibilities specced elsewhere.
- Performance targets for `get_audio_clips` N+1 elimination (already implemented via bulk preloads, but not asserted as a contract here).
- Plugin-owned sidecar tables, pool_segments variant model, analysis caches, effects + curves, undo/redo triggers (each has its own spec).
- Query-count / planner behavior assertions (would couple tests to implementation).
- Behavior of the FastAPI router on top of these DAL calls — covered by `local.engine-rest-api-dispatcher`.

---

## Open Questions

### Resolved

- **OQ-1** (hard-delete keyframe with live transitions): **fix** — DAL raises `KeyframeInUseError` if any `transitions.from_kf = kf.id OR to_kf = kf.id` (soft-deleted or not). Encoded in R51 and test `keyframe-in-use-blocks-hard-delete`.
- **OQ-2** (audio_clip.track_id → deleted track): **close** — existing cascade in R19 already handles this; no new semantic. Witnessed by test `audio-clip-track-cascade-preexisting`.
- **OQ-3** (add_audio_candidate for soft-deleted clip): **fix** — DAL raises `AudioClipDeletedError`. R52, test `add-audio-candidate-on-deleted-clip-rejected`.
- **OQ-4** (add_tr_candidate for soft-deleted transition): **fix** — DAL raises `TransitionDeletedError`. R53, test `add-tr-candidate-on-deleted-transition-rejected`.
- **OQ-5** (concurrent reorder): closed per INV-1 (single-writer per (user, project)). R56 + negative-assertion test `reorder-audio-tracks-no-internal-lock`.
- **OQ-6** (non-monotonic curve x): **fix** — DAL validates monotonic x; `ValueError` on violation. R54, test `curve-non-monotonic-x-rejected`.
- **OQ-7** (remap.target_duration < 0): **fix** — CHECK constraint rejects at DB layer. R55, test `remap-negative-target-duration-rejected`.
- **OQ-8** (legacy NOT NULL track_id): **fix (target)** — table-rebuild migration via `register_migration` + `rebuild_table`. Transitional (current): column-existence check leaves NOT NULL. See Transitional Behavior section and test `audio-clips-legacy-nullable-track-id-rebuild`.

### Deferred

(None — all 8 OQs resolved.)

### Historical

**OQ-1. Hard-delete of a keyframe with live transitions referencing it.**
There is no DAL helper that hard-deletes a keyframe; soft-delete is the only exposed path. However, admin tooling, raw SQL, or an undo replay COULD issue `DELETE FROM keyframes WHERE id = ?`. Because there is no FK, SQLite will accept it, leaving transitions with dangling `from_kf`/`to_kf`. What is the intended behavior? Options: (a) add FK with `ON DELETE RESTRICT`, (b) add FK with `ON DELETE SET NULL` and treat transitions with NULL anchors as orphaned (new state), (c) leave dangling references permitted and document. Related: audit leak #8.

**OQ-2. `audio_clip.track_id` references a track that has been hard-deleted.**
`delete_audio_track` soft-deletes clips on the track at the time of deletion, but a race (new clip insert using the just-deleted track_id) or a raw insert can produce an `audio_clip` whose track is gone. Desired behavior on read? `get_audio_clips` currently returns such rows unchanged; `get_audio_clips(track_id=…)` may return empty for the missing track.

**OQ-3. `add_audio_candidate` for a soft-deleted audio clip.**
No DAL check currently blocks this. Should the insert be rejected? Ignored? Queued for restore? Current behavior: row is inserted successfully.

**OQ-4. `add_tr_candidate` for a soft-deleted transition.**
Mirror of OQ-3. Current behavior: row is inserted successfully; `get_tr_candidates` continues to return it even though the transition is in the bin.

**OQ-5. Concurrent `reorder_audio_tracks` producing display_order collisions.**
No UNIQUE, no transaction boundary in the DAL fn (loop of UPDATEs with a single commit at the end). Two concurrent reorders can interleave and produce duplicate `display_order` values. Should `reorder_audio_tracks` take a project-scoped structural lock? Should schema add UNIQUE? Related: audit leak #9.

**OQ-6. JSON curve with non-monotonic x values** (e.g., `[[0,0],[0.5,1],[0.3,0.5]]`).
Stored as-is. Consumers that integrate or interpolate will emit undefined output. Should the DAL reject / sort / warn?

**OQ-7. `remap.target_duration < 0`.**
Stored as-is; downstream use during render produces undefined output (likely divide-by-zero or negative rate). Should the DAL clamp to 0, reject, or remain permissive?

**OQ-8. Legacy DB with `audio_clips.track_id NOT NULL` post-master-bus migration.**
`PRAGMA table_info` reports the column but does not cleanly surface the `NOT NULL` bit change required for post-M13 master-bus effects. How do we detect / migrate these DBs? Related: audit leak #17. Possible resolutions: (a) add a `schema_migrations` version table (audit recommendation #4), (b) `CREATE TABLE audio_clips__new … INSERT SELECT … DROP old; RENAME` rewrite, (c) leave as-is and document that master-bus effects require a fresh DB.

---

## Related Artifacts

- **Audit**: `agent/reports/audit-2-architectural-deep-dive.md` §1C (DB + DAL + migrations), §3 leaks #8, #9, #17
- **Source code**: `src/scenecraft/db.py`
- **Adjacent specs (out of scope here)**:
  - `agent/specs/local.engine-db-effects-and-curves.md` (pending)
  - `agent/specs/local.engine-db-analysis-caches.md` (pending)
  - `agent/specs/local.engine-db-undo-redo.md` (pending)
  - `agent/specs/local.engine-connection-and-transactions.md` (pending)
  - `agent/specs/local.engine-migrations-framework.md` (pending)
  - `agent/specs/local.engine-rest-api-dispatcher.md` (pending) — consumer of this DAL
  - `agent/specs/local.fastapi-migration.md` — the refactor this spec codifies the DAL contract for
- **Scenecraft-side specs referenced for context**:
  - `local.pool-segments-and-variant-kind.md` (pool_segments model)
  - `local.plugin-api-surface-and-r9a.md` (R9a invariant that plugins must not import db.py)

---

**Status**: Draft — Open Questions OQ-1..OQ-8 must be resolved (or explicitly accepted as deferred) before implementation of the FastAPI refactor depends on this contract.
