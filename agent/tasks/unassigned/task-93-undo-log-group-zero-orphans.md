# Task 93: Purge undo_log orphan rows under undo_group=0 at get_db bootstrap

**Milestone**: None (unassigned; surfaced by M18 task-73 regression tests)
**Design Reference**: [engine-db-undo-redo R17 transitional + R22 startup-sweep target](../../specs/local.engine-db-undo-redo.md)
**Estimated Time**: 1h
**Dependencies**: None
**Status**: Not Started
**Repositories**: `scenecraft-engine`

---

## Objective

Stop unbounded `undo_log` accumulation under `undo_group=0` by adding a startup sweep in `get_db` that purges pre-session seed-insert rows (group 0), keeping the existing `undo_begin` orphan sweep focused on pruned-groups cleanup only.

---

## Context

Surfaced by M18 task-73 (`engine-db-undo-redo`) regression tests, commit `46ac600`. A witness test in `tests/specs/test_engine_db_undo_redo.py` documents the leak.

### Bug Details

R10's orphan sweep in `undo_begin` only deletes `undo_log` rows whose `undo_group` is in the set of groups being pruned. Rows written under `undo_group=0` — produced by DB seed inserts (default `audio_track`, buses, etc.) that happen in `get_db` **before any explicit `undo_begin`** — never satisfy that condition. They accumulate for the lifetime of `project.db`, one batch per engine boot.

**Consequence**: Every engine startup adds another set of group-0 rows to `undo_log`. Over many boots this table grows without bound, and the rows have no recovery value (they correspond to schema-seeded defaults that will be re-seeded on the next bootstrap if missing).

### Fix Approach

Two options considered:

- **(a)** Extend the `undo_begin` orphan sweep to also delete `undo_group=0` rows older than the oldest live `undo_groups` entry.
- **(b)** Add a dedicated startup sweep in `get_db` bootstrap that purges `undo_group=0` rows unconditionally.

**Choice: (b).** Cleaner separation of concerns: `undo_begin` stays focused on the hot path (pruned-groups cleanup for the new group being started); bootstrap sweep happens once per boot, outside the undo-session lifecycle. Avoids mutating `undo_begin`'s per-transaction behavior and the associated testing surface.

---

## Steps

1. Open `src/scenecraft/db.py`; locate `get_db` and `undo_begin`.
2. In `get_db` bootstrap, after the schema is ensured and any seed inserts have run, add a sweep:
   ```sql
   DELETE FROM undo_log WHERE undo_group = 0;
   ```
   Place it after seed inserts (so the just-seeded rows are also purged — their only role was the insert itself; there's nothing to undo) and before any user-driven `undo_begin` can run.
3. Confirm the seed path actually uses `undo_group=0` (not some other marker like NULL). If it uses something else, purge that value instead; update the design reference accordingly.
4. Add a regression test in `tests/specs/test_engine_db_undo_redo.py`:
   - Fresh `project.db` bootstrap: assert `SELECT COUNT(*) FROM undo_log WHERE undo_group=0` is zero after `get_db` returns.
   - Second call to `get_db` (simulated reboot): same assertion — sweep is idempotent.
   - Normal `undo_begin` / `undo_end` flow still works (group-N rows are not affected).
5. Run the full M18 undo-redo suite to confirm nothing else broke:
   ```
   pytest tests/specs/test_engine_db_undo_redo.py -v
   ```
6. Commit: `fix(db): purge undo_group=0 seed-insert rows on get_db bootstrap`.

---

## Verification Checklist

- [ ] `undo_log` has zero `undo_group=0` rows after `get_db` bootstrap
- [ ] Sweep is idempotent across multiple `get_db` calls / reboots
- [ ] Normal grouped undo/redo (group N>0) still works unchanged
- [ ] `undo_begin` behavior unchanged (orphan sweep logic untouched)
- [ ] Regression test added
- [ ] Full `test_engine_db_undo_redo.py` suite green

---

## Key Design Decisions

- **Option (b) — bootstrap sweep — over option (a) — extend undo_begin.** Separation of concerns: `undo_begin` hot path stays focused; startup sweep is a single idempotent DELETE, trivially tested and reasoned about.
- **Unconditional purge of group 0.** Group-0 rows correspond to schema-seeded defaults that are re-seeded on next bootstrap if missing; there is no recovery value in keeping them. They're noise, not history.
- **Sweep after seed inserts, not before.** Ensures newly-seeded rows from this bootstrap are also purged in the same pass — otherwise every boot would leave exactly one cohort of group-0 rows behind.
