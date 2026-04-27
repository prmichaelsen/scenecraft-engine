# Task 90: Fix POST /bounce-upload orphan WAV — no audio_bounces DAL row

**Milestone**: None (unassigned; surfaced by M18 task-74 regression tests)
**Design Reference**: [engine-db-analysis-caches R25-R32](../../specs/local.engine-db-analysis-caches.md)
**Estimated Time**: 2h
**Dependencies**: None
**Status**: Not Started
**Repositories**: `scenecraft-engine`

---

## Objective

Make `POST /api/projects/:name/bounce-upload` populate the `audio_bounces` DAL row so uploads are retrievable via `GET /bounces/<id>.wav`, eliminating the "orphan WAV on disk with no DB record" production bug.

---

## Context

Surfaced by M18 task-74 (`engine-db-analysis-caches`) regression tests, commit `4b9c06b`. A test in `tests/specs/test_engine_db_analysis_caches.py` is xfailed as the witness; removing the xfail decorator flips it to a normal regression test once the fix lands.

### Bug Details

`POST /api/projects/:name/bounce-upload` writes the WAV to `pool/bounces/<composite_hash>.wav` but **never INSERTs an `audio_bounces` DAL row**. The row is only written by the WS-driven `_exec_bounce_audio` chat flow (`chat.py` around line 3422). Any direct HTTP upload that bypasses the chat path produces an orphan WAV on disk with no DB record. The corresponding `GET /bounces/<id>.wav` download endpoint then 404s (no row → no id to serve).

**Consequence**: Bounces uploaded via any path other than the WS chat flow are inaccessible — the file exists on disk but there's no id to serve it by. The analysis-cache contract (R25-R32) assumes every on-disk bounce has a DB row.

### Fix Approach

Two options considered:

- **(a)** Mandatory-couple: reject the upload unless there's a matching `_BOUNCE_RENDER_EVENTS` entry from a chat-initiated bounce.
- **(b)** Insert/upsert: the upload handler itself writes (or upserts) the `audio_bounces` row keyed on `composite_hash`, so the upload is always retrievable.

**Choice: (b).** Spec R25-R32 (task-74) implies the upload endpoint should populate the cache the same way the WS chat path does — the upload is the DB-of-record event, not a side channel. (a) is brittle (requires a chat event to exist for every upload) and breaks plausible future callers (CLI tools, re-uploads, tests).

---

## Steps

1. Open `src/scenecraft/api_server.py`; locate the `POST /bounce-upload` handler (grep for `bounce-upload`). Read the current flow: parse multipart, compute/verify composite_hash, write to `pool/bounces/<composite_hash>.wav`.
2. Open `src/scenecraft/chat.py` at `_exec_bounce_audio` (around line 3422) for the reference `audio_bounces` INSERT. Note the exact columns written (id, composite_hash, mix_graph_hash, selection, format, file_path, created_at, any other metadata).
3. Open `src/scenecraft/db_bounces.py` to identify the canonical DAL helper (e.g. `insert_bounce` or similar). If only the chat path currently uses it, make sure it's safe to call from the HTTP handler context (no WS-only side-effects).
4. In the `/bounce-upload` handler, after the WAV is written to disk, call the DAL helper to upsert an `audio_bounces` row keyed on `composite_hash`. Idempotent: if a row already exists for that composite_hash, update (or leave; defer to the DAL's existing semantics — whichever matches the chat-path behavior).
5. Return the created/existing bounce id in the JSON response so callers can GET it back.
6. Add a regression test in `tests/specs/test_engine_db_analysis_caches.py` (or wherever the M18 xfailed witness lives) that:
   - POSTs a fresh WAV to `/bounce-upload` without the chat/WS path
   - Verifies an `audio_bounces` row exists with the expected composite_hash
   - `GET /bounces/<id>.wav` returns 200 with the bytes written
7. Remove the `@pytest.mark.xfail(...)` decorator from the witness test so it becomes a regular regression test.
8. Run the full M18 analysis-caches suite to confirm nothing else broke:
   ```
   pytest tests/specs/test_engine_db_analysis_caches.py -v
   ```
9. Commit: `fix(bounce): populate audio_bounces row on POST /bounce-upload so uploads are retrievable`.

---

## Verification Checklist

- [ ] `POST /bounce-upload` inserts (or upserts) an `audio_bounces` row on success
- [ ] Response JSON includes the bounce id
- [ ] `GET /bounces/<id>.wav` returns 200 for a just-uploaded bounce that never touched the WS chat path
- [ ] Re-uploading the same composite_hash is idempotent (no duplicate row, or documented upsert behavior matches chat path)
- [ ] M18 witness test passes without xfail
- [ ] Full `test_engine_db_analysis_caches.py` suite green
- [ ] No regression in the WS chat-flow path (`_exec_bounce_audio` still works end-to-end)

---

## Key Design Decisions

- **Option (b) — upload-populates-row — over option (a) — reject orphan uploads.** Rationale: the upload endpoint is a first-class entry point for bounces, not merely a side-channel for chat. The DB row is the source of truth; the on-disk WAV is derived. Making upload the row-creation event is symmetrical with the chat path and future-proof against other callers (CLI, re-upload, tests).
- **Upsert semantics keyed on composite_hash.** composite_hash is already the content-address; two callers producing the same bytes should produce a single row.
- **The DAL helper is the single writer.** Handler calls into `db_bounces.py`; no raw SQL in the API layer (per R9a-adjacent plugin DB rule, and general hygiene).
