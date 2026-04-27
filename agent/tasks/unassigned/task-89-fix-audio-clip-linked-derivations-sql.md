# Task 88: Fix audio_clip linked-derivations SQL — bogus double-quoted from/to

**Milestone**: None (unassigned; surfaced by M18 task-71 regression tests)
**Design Reference**: [engine-db-schema-core-entities R25](../../specs/local.engine-db-schema-core-entities.md)
**Estimated Time**: 0.5h
**Dependencies**: None
**Status**: Not Started
**Repositories**: `scenecraft-engine`

---

## Objective

Fix the bogus `"from"` / `"to"` double-quoted identifiers in the `get_audio_clips` bulk-preload SELECT in `src/scenecraft/db.py` so that R25's `playback_rate` and `effective_source_offset` derivations work correctly for linked audio clips.

---

## Context

Surfaced by M18 task-71 regression tests (commit `6f4e960`). The test `test_audio_clip_linked_derivations` in `tests/specs/test_engine_db_schema_core_entities.py` is currently `@pytest.mark.xfail(...)`'d with a detailed bug-witness reason. Once the SQL is fixed, it will flip to XPASS; the xfail decorator must be removed in the same commit so it becomes a normal regression test.

### Bug Details

`get_audio_clips` in `db.py` (around line 3128) executes a SELECT with bogus double-quoted identifiers:

```sql
SELECT id, "from" AS from_kf, "to" AS to_kf, trim_in, trim_out, source_video_duration
FROM transitions
WHERE ...
```

The `transitions` table DDL columns are named `from_kf` and `to_kf` — there are no `from` or `to` columns. SQLite's parser silently treats double-quoted identifiers as **string literals** when the identifier doesn't match an existing column. So every row returns `from_kf='from'` and `to_kf='to'` (literal strings), which never match any real keyframe id.

**Consequence**: R25-contracted `playback_rate` and `effective_source_offset` derivations for linked audio clips silently fall through to `(1.0, stored_offset)` on every call. Link-based derived timing has been non-functional in production.

### Fix

Drop the bogus aliases. Corrected query:

```sql
SELECT id, from_kf, to_kf, trim_in, trim_out, source_video_duration
FROM transitions
WHERE ...
```

---

## Steps

1. Open `src/scenecraft/db.py`; locate the `get_audio_clips` function (around line 3100–3160).
2. Find the SELECT statement using `"from" AS from_kf, "to" AS to_kf` (around line 3128).
3. Replace with `SELECT id, from_kf, to_kf, trim_in, trim_out, source_video_duration FROM transitions WHERE ...` — drop the double-quoted aliases.
4. Run the M18 task-71 regression test that witnessed the bug:
   ```
   pytest tests/specs/test_engine_db_schema_core_entities.py::test_audio_clip_linked_derivations -v
   ```
   Confirm it XPASSes.
5. Remove the `@pytest.mark.xfail(...)` decorator from `test_audio_clip_linked_derivations` in `tests/specs/test_engine_db_schema_core_entities.py`. Update its docstring to drop the bug-witness language; it is now a regular regression test.
6. Run the full M18 test module to confirm nothing else broke:
   ```
   pytest tests/specs/test_engine_db_schema_core_entities.py -v
   ```
7. Commit: `fix(db): correct bogus "from"/"to" double-quoted identifiers in get_audio_clips SELECT`.

---

## Verification Checklist

- [ ] SQL statement uses bare column names `from_kf`, `to_kf` (no double quotes, no aliases)
- [ ] `test_audio_clip_linked_derivations` passes without xfail
- [ ] Full `test_engine_db_schema_core_entities.py` suite green (37+8 xfail — one fewer xfail than before, no new FAIL)
- [ ] No other call sites rely on the bogus alias behavior (grep for `from_kf='from'` patterns)

---

## Why This Wasn't Caught Sooner

SQLite's silent-string-literal-fallback on unknown double-quoted identifiers is a documented-but-surprising quirk. Migrate to single quotes for string literals project-wide as a follow-up (separate task; not this one's scope).
