# Spec: Engine DB — Undo/Redo System

> **🤖 Agent Directive**: This spec defines the observable behavior of the
> trigger-populated undo/redo system implemented in `src/scenecraft/db.py`.
> Implementers MUST treat the Behavior Table and Tests section as the
> executable contract. Do not invent behavior for `undefined` rows — resolve
> them via Open Questions first.

**Namespace**: local
**Spec**: engine-db-undo-redo
**Version**: 1.0.0
**Created**: 2026-04-27
**Last Updated**: 2026-04-27
**Status**: Draft

---

## Purpose

Define the exact behavior of the SQLite-backed undo/redo subsystem: how
mutations are captured into `undo_log` by triggers, how undo groups bound
user-visible operations, how `undo_execute` / `redo_execute` replay inverse
SQL, and how the `active` capture flag and composite-PK deferred foreign keys
keep replay deterministic.

## Source

- **Mode**: `--from-draft` (ad-hoc chat directive; audit-2 §1C unit 8)
- **Primary source file**: `src/scenecraft/db.py`
  - Schema (undo_log / redo_log / undo_groups / undo_state): lines ~913–937
  - Undo triggers (row-sql tracked tables): lines ~1208–1229
  - Composite-PK triggers (`isolation_stems`, `track_sends`): lines ~1230–1298
  - Deferred FK on `isolation_stems`: lines ~380–389
  - `undo_begin` / `undo_execute` / `redo_execute` / `undo_history`: lines ~2848–2965
- **Context report**: `agent/reports/audit-2-architectural-deep-dive.md` §1C
  row 8 ("Undo/Redo System") and row 10 ("Deferred FK").

## Scope

### In scope

- Storage schema of `undo_log`, `redo_log`, `undo_groups`, `undo_state` and
  their required key/value seeds (`current_group=0`, `active=1`).
- Per-table AFTER INSERT/UPDATE/DELETE triggers that emit inverse SQL text
  into `undo_log`, gated by `undo_state.active = 1`.
- The explicit allow-list of **undo-tracked tables**: captures happen on the
  tables enumerated in `_undo_tracked_tables` plus the two composite-PK
  tables (`isolation_stems`, `track_sends`) — not on every mutable table.
- Group boundary semantics: `undo_begin(description)` allocates a new
  `current_group`, inserts a `undo_groups` row, and clears the redo stack.
- Replay semantics of `undo_execute` / `redo_execute`, including:
  - Inverse SQL is re-executed in DESC `seq` order.
  - Triggers remain ENABLED during undo replay to recapture forward SQL into
    `redo_log` via a temporary negative group id.
  - Triggers are DISABLED during redo replay by setting
    `undo_state.active = 0` for the duration of the loop.
- `DEFERRABLE INITIALLY DEFERRED` on `isolation_stems.isolation_id` so
  replay can insert children and parents in any row order within the
  implicit transaction.
- History pruning: on each `undo_begin`, trim `undo_groups` to the most
  recent 1000 entries and delete orphaned `undo_log` rows.
- Redo-stack invalidation: on each `undo_begin`, any `undo_groups` still
  marked `undone = 1` are deleted (branch discarded).

### Out of scope

- Frontend / UX: keyboard shortcuts, undo affordances, toast messages, WS
  broadcast of undo events. (A separate UI spec covers those.)
- Per-operation authoring of undo groups inside business logic (each mutator
  decides whether to wrap in a group; this spec does not enumerate call
  sites).
- Undo for tables not in `_undo_tracked_tables` (e.g. analysis caches,
  light_show plugin tables, transcriptions). Mutations there are silently
  not captured; that is by design.
- Multi-user concurrency. The system assumes a single writer per project DB.
- Cross-project undo.

---

## Requirements

All requirements are testable and traceable.

- **R1** — The schema initializer MUST create `undo_log(seq PK AUTOINCREMENT,
  undo_group INT NOT NULL, sql_text TEXT NOT NULL)`, `redo_log` with the
  identical shape, `undo_groups(id PK, description, timestamp, undone
  DEFAULT 0)`, and `undo_state(key PK, value INT)` on first open.
- **R2** — On first open, `undo_state` MUST contain the seed rows
  `('current_group', 0)` and `('active', 1)`.
- **R3** — For each table in `_undo_tracked_tables` (`keyframes`,
  `transitions`, `suppressions`, `effects`, `tracks`, `transition_effects`,
  `markers`, `audio_tracks`, `audio_clips`, `audio_isolations`,
  `track_effects`, `effect_curves`, `project_send_buses`,
  `project_frequency_labels`), three triggers MUST exist:
  `{table}_insert_undo`, `{table}_update_undo`, `{table}_delete_undo`.
- **R4** — Every row-sql undo trigger MUST be gated by the predicate
  `WHEN (SELECT value FROM undo_state WHERE key='active') = 1`. When
  `active = 0`, the trigger MUST NOT write to `undo_log`.
- **R5** — INSERT triggers MUST emit inverse SQL of form
  `DELETE FROM {table} WHERE id=<quoted NEW.id>`.
- **R6** — UPDATE triggers MUST emit inverse SQL of form
  `UPDATE {table} SET <col=OLD.col, …> WHERE id=<quoted OLD.id>` covering
  every column known to `PRAGMA table_info` at trigger-creation time.
- **R7** — DELETE triggers MUST emit inverse SQL of form
  `INSERT INTO {table} (<col_list>) VALUES (<OLD col expressions>)` for
  every column known at trigger-creation time.
- **R8** — Composite-PK tables `isolation_stems` and `track_sends` MUST have
  insert/update/delete undo triggers that encode the full composite key in
  the inverse SQL `WHERE` clause.
- **R9** — `isolation_stems.isolation_id` FK MUST be declared
  `DEFERRABLE INITIALLY DEFERRED` so undo replay may restore children and
  parents in arbitrary row order within the transaction.
- **R10** — `undo_begin(description)` MUST:
  - Increment `undo_state.current_group`.
  - If the new `current_group` collides with an existing `undo_groups.id`,
    bump past it to `MAX(id)+1` and write the bumped value back to
    `undo_state`.
  - Insert a `undo_groups(id, description, timestamp_iso_utc, undone=0)`
    row.
  - Delete all `undo_log` + `redo_log` + `undo_groups` rows where
    `undone = 1` (redo stack invalidated).
  - Prune `undo_groups` to the most recent 1000 by id; delete any
    `undo_log` rows orphaned by that prune.
  - Commit.
  - Return the new group id.
- **R11** — Mutations to tracked tables while `active = 1` and while a
  `current_group` is set MUST append one row to `undo_log` per mutation,
  with `undo_group = current_group` and a monotonically increasing `seq`.
- **R12** — `undo_execute()` MUST:
  - Select the most recent `undo_groups` row with `undone = 0`; if none,
    return `None`.
  - Set `undo_state.current_group` to `-group_id` (negative capture bucket).
  - Execute every `undo_log.sql_text` for `group_id` in **descending `seq`**
    order with triggers still ENABLED.
  - Move the trigger-captured rows from `undo_log` (under
    `undo_group = -group_id`) into `redo_log` under `undo_group = group_id`
    in ascending seq order, and delete the temporary negative-group rows.
  - Restore `undo_state.current_group = group_id`, mark
    `undo_groups.undone = 1` for `group_id`, commit, return
    `{id, description, timestamp}`.
- **R13** — `redo_execute()` MUST:
  - Select the earliest (`ORDER BY id ASC`) `undo_groups` row with
    `undone = 1`; if none, return `None`.
  - If `redo_log` has no rows for that group, return `None`.
  - Set `undo_state.active = 0`, execute every `redo_log.sql_text` in
    **ascending `seq`** order, restore `undo_state.active = 1`.
  - Set `undo_groups.undone = 0`, delete the consumed `redo_log` rows,
    commit, return `{id, description, timestamp}`.
- **R14** — After `undo_begin` runs, all `undo_groups` rows with
  `undone = 1` MUST NOT exist (redo branch is discarded — no branch point
  retained).
- **R15** — `undo_history(limit)` MUST return at most `limit` rows, most
  recent first, each with `{id, description, timestamp, undone:bool}`.
- **R16** — Mutations to tables not in the tracked list (e.g.
  `audio_bounces`, analysis caches, plugin sidecars) MUST NOT write to
  `undo_log`, regardless of `active` state.
- **R17** — Mutations performed while `undo_state.current_group = 0`
  (before any `begin_undo_group`) MUST skip undo capture entirely. The
  trigger body checks `current_group != 0`; when zero, no `undo_log` row
  is written. The mutation itself proceeds normally but is not undoable.
  (Resolves OQ-2.)
- **R18** — `begin_undo_group` / `end_undo_group` / `is_undo_capturing` are
  the target public API names. Legacy aliases `undo_begin` / (no legacy
  end) remain importable through one release cycle. `is_undo_capturing()`
  returns `True` iff `undo_state.active = 1` AND
  `undo_state.current_group != 0`. (Resolves OQ-1.)
- **R19** — On a new tracked mutation outside a `begin_undo_group`
  (i.e. `current_group = 0`), any existing `undone = 1` groups in
  `undo_groups` are discarded along with their `redo_log` rows before
  the mutation proceeds. This preserves the "new mutation discards redo
  stack" invariant even when the caller forgets to open a group.
  (Resolves OQ-3.)
- **R20** — Each `undo_groups` row MUST be capped at 10,000
  `undo_log` rows. When a tracked mutation would push the count above
  the cap, the oldest `undo_log` row for that group is deleted before
  the new row is appended. The group is documented as "partially
  un-undoable" and a subsequent `undo_execute` on that group produces
  a partial undo. (Resolves OQ-4.)
- **R21** — Every `undo_log` row MUST carry a `schema_version` column
  populated from the project's `schema_migrations` table
  (see `local.engine-migrations-framework`). On `undo_execute`, if the
  group's rows reference a `schema_version` lower than the DB's current
  `schema_version`, replay aborts with
  `UndoReplaySchemaVersionMismatch`. The caller is expected to discard
  the undo history rather than attempt replay. (Resolves OQ-5.)
- **R22** — `undo_groups` gains a `completed_at TEXT` column. A group is
  considered complete when (a) `undo_execute` runs on it, OR (b) the
  next `begin_undo_group` is called, OR (c) `end_undo_group` is
  called. On engine startup, a sweep MUST close every `undo_groups` row
  whose `completed_at IS NULL` AND whose `timestamp` is older than
  1 hour, setting `completed_at = now(UTC ISO)`. (Resolves OQ-6.)
- **R23** — The undo subsystem holds no internal lock spanning multiple
  API calls. Concurrent writes from the same user on the same project
  are undefined per INV-1. (Resolves OQ-7.)
- **R24** — `undo_execute` MUST wrap the replay loop in a SAVEPOINT.
  On any exception during replay, the SAVEPOINT is rolled back, the
  `undo_groups.replay_failed` column is set to 1, and the error is
  surfaced to the caller. The group remains in `undo_groups` but is
  no longer replayable (excluded from the `undone = 0` candidate list
  for future `undo_execute` calls). (Resolves OQ-8.)

---

## Interfaces / Data Shapes

### Public Python API (in `scenecraft.db`) — Target

```
begin_undo_group(project_dir: Path, description: str) -> int
end_undo_group(project_dir: Path) -> None
is_undo_capturing(project_dir: Path) -> bool
undo_execute(project_dir: Path) -> dict | None   # {id, description, timestamp}
redo_execute(project_dir: Path) -> dict | None   # {id, description, timestamp}
undo_history(project_dir: Path, limit: int = 50) -> list[dict]
```

**Transitional API aliases** (INV-8): through one release cycle, the legacy
names MUST remain importable and behave identically:

```
undo_begin    → alias for begin_undo_group
undo_execute  → kept as-is (new name not needed)
redo_execute  → kept as-is
```

There is no explicit `undo_end` in the current code; `end_undo_group` is a
new target API added as part of this renaming. Groups are still implicitly
closed by the next `begin_undo_group` or by `undo_execute` for
back-compat. `is_undo_capturing()` returns `undo_state.active == 1 AND
current_group != 0`.

### Schema

```sql
CREATE TABLE undo_log (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    undo_group INTEGER NOT NULL,
    sql_text TEXT NOT NULL
);
CREATE TABLE redo_log (   -- identical shape
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    undo_group INTEGER NOT NULL,
    sql_text TEXT NOT NULL
);
CREATE TABLE undo_groups (
    id INTEGER PRIMARY KEY,
    description TEXT NOT NULL,
    timestamp TEXT NOT NULL,       -- ISO-8601 UTC
    undone INTEGER DEFAULT 0       -- 0 = live, 1 = undone
);
CREATE TABLE undo_state (
    key TEXT PRIMARY KEY,           -- 'current_group' | 'active'
    value INTEGER
);
-- seeds:
INSERT OR IGNORE INTO undo_state VALUES ('current_group', 0);
INSERT OR IGNORE INTO undo_state VALUES ('active', 1);
```

### Trigger template (row-id tables)

```sql
CREATE TRIGGER {table}_insert_undo AFTER INSERT ON {table}
WHEN (SELECT value FROM undo_state WHERE key='active') = 1
BEGIN
  INSERT INTO undo_log (undo_group, sql_text)
  SELECT value, 'DELETE FROM {table} WHERE id=' || quote(NEW.id)
  FROM undo_state WHERE key='current_group';
END;
```
(UPDATE and DELETE follow the symmetric shape described in R6/R7.)

### Undo-tracked tables (authoritative list at spec write)

- Row-id pattern: `keyframes`, `transitions`, `suppressions`, `effects`,
  `tracks`, `transition_effects`, `markers`, `audio_tracks`, `audio_clips`,
  `audio_isolations`, `track_effects`, `effect_curves`,
  `project_send_buses`, `project_frequency_labels`.
- Composite-PK pattern: `isolation_stems`, `track_sends`.

---

## Behavior Table

| # | Scenario | Expected Behavior | Tests |
|---|----------|-------------------|-------|
| 1 | Fresh DB open | `undo_log`, `redo_log`, `undo_groups`, `undo_state` created; seeds `current_group=0`, `active=1` present | `schema-initialized`, `undo-state-seeds-present` |
| 2 | Insert into tracked table after `undo_begin` | One `undo_log` row with `DELETE FROM … WHERE id=<new-id>`, `undo_group = current_group` | `insert-capture-emits-delete` |
| 3 | Update tracked row after `undo_begin` | One `undo_log` row with `UPDATE … SET <OLD cols> WHERE id=<old-id>` | `update-capture-emits-old-values` |
| 4 | Delete tracked row after `undo_begin` | One `undo_log` row with `INSERT INTO … VALUES (<OLD values>)` | `delete-capture-emits-insert` |
| 5 | Mutation while `active = 0` | No row appended to `undo_log` | `capture-gated-off-writes-nothing` |
| 6 | Mutation on a non-tracked table | No row appended to `undo_log` | `non-tracked-table-not-captured` |
| 7 | `undo_begin` called | New `undo_groups` row with monotonically increasing id; returns that id | `undo-begin-allocates-group` |
| 8 | `undo_begin` after some undone groups exist | Undone groups deleted; their `undo_log` and `redo_log` rows deleted (redo stack cleared) | `undo-begin-clears-redo-stack`, `undo-begin-no-branch-point-retained` |
| 9 | `undo_execute` with live group | Inverse SQL re-executed in DESC seq; group marked `undone=1`; captured forward SQL lands in `redo_log` | `undo-replays-inverse-desc`, `undo-captures-redo-log` |
| 10 | `redo_execute` with undone group | Forward SQL re-executed in ASC seq with triggers disabled; group marked `undone=0`; `redo_log` entries cleaned | `redo-replays-forward-asc`, `redo-disables-capture-during-replay`, `redo-cleans-redo-log` |
| 11 | `undo_execute` with nothing to undo | Returns `None`, no state change | `undo-empty-returns-none` |
| 12 | `redo_execute` with nothing to redo | Returns `None`, no state change | `redo-empty-returns-none` |
| 13 | Composite-PK insert (`isolation_stems`) | Inverse SQL `DELETE` uses full composite key in `WHERE` | `composite-pk-insert-capture` |
| 14 | Replay that inserts parent+child in swapped order | Transaction commits thanks to DEFERRED FK | `deferred-fk-allows-replay-ordering` |
| 15 | Composite-PK insert (`track_sends`) | Inverse SQL `DELETE` uses full composite key | `track-sends-composite-capture` |
| 16 | `undo_groups` count exceeds 1000 | Oldest groups + their `undo_log` rows pruned on next `undo_begin` | `history-pruned-to-1000` |
| 17 | `undo_history(limit)` | Returns up to `limit` rows, newest first, with `undone` bool | `undo-history-shape-and-order` |
| 18 | Redo available, then a new mutation happens | Redo stack discarded on new mutation outside `begin_undo_group` (current behavior codified) | `redo-discarded-on-new-mutation` |
| 19 | `undo_log` growth within a single group | Cap at 10,000 rows per group; oldest log entry dropped on overflow (group becomes partially un-undoable) | `undo-log-capped-per-group` |
| 20 | Undo replay across a schema migration | `undo_log` rows tagged with `schema_version`; replay fails on mismatch with `UndoReplaySchemaVersionMismatch` error; user guided to discard undo history | `undo-replay-schema-mismatch-fails` |
| 21 | Process dies after `undo_begin` with mutations captured | Startup sweep closes `undo_groups` with `completed_at IS NULL` older than 1 hour | `startup-sweep-closes-orphan-groups` |
| 22 | Mutation with `current_group = 0` | Skip capture (treat as "not in a group"); mutation proceeds, not undoable | `current-group-zero-skips-capture` |
| 23 | Two concurrent writers to the same project DB | Undefined by INV-1 (single-writer per (user, project)); no internal lock held | `concurrent-writers-no-internal-lock` |
| 24 | `undo_execute` fails mid-replay | Replay wrapped in transaction; on failure, rollback + mark `undo_group.replay_failed=1` + surface error | `undo-replay-failure-rolls-back` |

---

## Behavior

### Capture path (forward mutation)

1. Caller invokes `undo_begin(project_dir, description)`; a new
   `current_group` id is allocated and an `undo_groups` row is written. Any
   previously-undone groups (and their `undo_log` / `redo_log` rows) are
   discarded.
2. Caller performs one or more INSERT/UPDATE/DELETE statements against
   tracked tables.
3. For each mutation, the per-table AFTER trigger fires. If
   `undo_state.active = 1`, it appends a row to `undo_log` carrying the
   inverse SQL text and the current `undo_group`.
4. Caller commits. No explicit `undo_end` call is required; the group is
   closed implicitly on the next `undo_begin`.

### Undo path

1. `undo_execute` picks the newest `undone=0` group.
2. It sets `current_group = -group_id` (a negative bucket) so that the
   triggers which fire during replay write their forward-SQL into a
   disposable slot.
3. It replays every `undo_log.sql_text` for `group_id` in **descending
   seq** order. This is essential because later mutations may depend on
   earlier ones.
4. It then copies the trigger-captured rows from `undo_log`
   (`undo_group = -group_id`) into `redo_log` (`undo_group = group_id`) in
   ascending seq order, and deletes the scratch rows.
5. `current_group` is restored to the original `group_id`; the
   `undo_groups` row is marked `undone = 1`.

### Redo path

1. `redo_execute` picks the oldest `undone=1` group. (Note: oldest, not
   newest — this implements a FIFO redo stack.)
2. If `redo_log` has no rows for that group, it returns `None`.
3. Sets `undo_state.active = 0` to suppress trigger capture while replaying
   forward SQL.
4. Replays `redo_log.sql_text` for the group in **ascending seq** order.
5. Restores `active = 1`, clears the `undone` flag, deletes the consumed
   `redo_log` rows.

### Deferred FK

`isolation_stems.isolation_id` is declared `DEFERRABLE INITIALLY DEFERRED`.
During undo/redo replay, rows may be inserted in an order that
transiently violates the FK (e.g. child before parent). FK checks are
deferred until transaction commit, making replay independent of row order.

---

## Acceptance Criteria

- [ ] Schema creation and seeds match R1/R2 and are idempotent across
  repeated opens.
- [ ] All 14 row-id tables have all three triggers present and gated on
  `undo_state.active = 1`.
- [ ] `isolation_stems` and `track_sends` triggers encode full composite
  keys.
- [ ] `isolation_stems.isolation_id` is DEFERRABLE INITIALLY DEFERRED.
- [ ] `undo_begin` returns a fresh monotonic id, writes `undo_groups`,
  clears redo stack, and prunes history to ≤ 1000 groups.
- [ ] Mutations while `active = 0` do not write to `undo_log`.
- [ ] Mutations to non-tracked tables do not write to `undo_log`.
- [ ] `undo_execute` replays inverse SQL DESC and populates `redo_log`.
- [ ] `redo_execute` replays forward SQL ASC with triggers disabled.
- [ ] `undo_execute` / `redo_execute` return `None` when nothing to do.
- [ ] `undo_history(limit)` returns rows shaped `{id, description,
  timestamp, undone:bool}` newest-first.
- [ ] All `undefined` rows in the Behavior Table remain unresolved until the
  corresponding Open Question is closed.

---

## Tests

### Base Cases

The core behavior contract.

#### Test: schema-initialized (covers R1)

**Given**: A fresh project directory with no existing `project.db`.
**When**: `get_db(project_dir)` is called for the first time.
**Then** (assertions):
- **tables-exist**: `sqlite_master` lists `undo_log`, `redo_log`,
  `undo_groups`, `undo_state`.
- **undo-log-columns**: `undo_log` has columns `seq` (INTEGER PK
  AUTOINCREMENT), `undo_group` (INTEGER NOT NULL), `sql_text` (TEXT NOT
  NULL).
- **undo-groups-columns**: `undo_groups` has `id` PK, `description` TEXT,
  `timestamp` TEXT, `undone` INTEGER DEFAULT 0.

#### Test: undo-state-seeds-present (covers R2)

**Given**: Freshly initialized DB.
**When**: Select from `undo_state`.
**Then** (assertions):
- **current-group-seeded**: Row `('current_group', 0)` exists.
- **active-seeded**: Row `('active', 1)` exists.
- **idempotent-on-reopen**: Re-running initialization does not duplicate
  seeds (unique key constraint preserved).

#### Test: insert-capture-emits-delete (covers R3, R5, R11)

**Given**: Fresh DB; `undo_begin(project_dir, "add keyframe")` called and
  returned `g`.
**When**: `INSERT INTO keyframes(id, …) VALUES ('k1', …)` runs.
**Then** (assertions):
- **one-undo-row**: Exactly one new row in `undo_log` for `undo_group = g`.
- **inverse-sql-is-delete**: Its `sql_text` equals
  `DELETE FROM keyframes WHERE id='k1'` (with SQLite `quote()`
  formatting).

#### Test: update-capture-emits-old-values (covers R6, R11)

**Given**: Existing keyframe `k1` with value `v_old`; `undo_begin` called.
**When**: `UPDATE keyframes SET value='v_new' WHERE id='k1'` runs.
**Then** (assertions):
- **inverse-sql-is-update**: `undo_log.sql_text` starts with
  `UPDATE keyframes SET ` and contains every column assigned to its OLD
  value, ending with `WHERE id='k1'`.

#### Test: delete-capture-emits-insert (covers R7, R11)

**Given**: Existing keyframe `k1`; `undo_begin` called.
**When**: `DELETE FROM keyframes WHERE id='k1'` runs.
**Then** (assertions):
- **inverse-sql-is-insert**: `undo_log.sql_text` is
  `INSERT INTO keyframes (<col_list>) VALUES (<OLD values>)` with
  every column present.

#### Test: capture-gated-off-writes-nothing (covers R4)

**Given**: `undo_begin` called; `UPDATE undo_state SET value=0 WHERE
  key='active'` has been executed.
**When**: `INSERT INTO keyframes(...)` runs.
**Then** (assertions):
- **no-undo-row**: `SELECT COUNT(*) FROM undo_log WHERE undo_group=g` is
  unchanged from before the insert.

#### Test: non-tracked-table-not-captured (covers R16)

**Given**: `undo_begin` called; `active = 1`.
**When**: An insert runs against `audio_bounces` (not in
  `_undo_tracked_tables`).
**Then** (assertions):
- **no-undo-row**: `undo_log` has no new rows for the current group.

#### Test: undo-begin-allocates-group (covers R10)

**Given**: Fresh DB, no existing groups.
**When**: `undo_begin(project_dir, "op A")` is called; then again with `"op
  B"`.
**Then** (assertions):
- **first-id-returned**: First call returns `1` and a row exists in
  `undo_groups` with `id=1`, `description='op A'`, `undone=0`,
  ISO-8601 UTC `timestamp`.
- **second-id-monotonic**: Second call returns `2`.

#### Test: undo-begin-clears-redo-stack (covers R10, R14)

**Given**: Group `g1` exists with `undone=1` and has rows in
  `undo_log`/`redo_log`.
**When**: `undo_begin("g2")` is called.
**Then** (assertions):
- **undone-group-deleted**: `undo_groups` has no row with id `g1`.
- **undone-undo-log-deleted**: `undo_log` has no rows with `undo_group=g1`.
- **undone-redo-log-deleted**: `redo_log` has no rows with `undo_group=g1`.
- **new-group-inserted**: A new `undo_groups` row exists for `g2`.

#### Test: undo-begin-no-branch-point-retained (covers R14)

**Given**: Two undone groups `g1 < g2`.
**When**: `undo_begin("g3")` runs.
**Then** (assertions):
- **both-undone-groups-gone**: Neither `g1` nor `g2` exists in
  `undo_groups`.

#### Test: undo-replays-inverse-desc (covers R12)

**Given**: `undo_begin` returned `g`; in order: insert `k1`, update
  `k1.value='v'`, insert `k2`. `undo_log` has three rows seq 1..3.
**When**: `undo_execute(project_dir)` runs.
**Then** (assertions):
- **replay-order**: The three `sql_text` statements are executed seq 3, 2,
  1.
- **final-state**: `keyframes` contains neither `k1` nor `k2`.
- **group-marked-undone**: `undo_groups.undone=1` for `g`.
- **return-shape**: Returns `{id: g, description, timestamp}` dict.

#### Test: undo-captures-redo-log (covers R12)

**Given**: Same setup as `undo-replays-inverse-desc`.
**When**: `undo_execute` completes.
**Then** (assertions):
- **redo-log-has-three-rows**: `redo_log` has exactly 3 rows with
  `undo_group=g`.
- **negative-scratch-empty**: `undo_log` has no rows with
  `undo_group=-g`.
- **current-group-restored**: `undo_state.current_group = g`.

#### Test: redo-replays-forward-asc (covers R13)

**Given**: After `undo-replays-inverse-desc`, `redo_log` has rows seq
  n..n+2.
**When**: `redo_execute(project_dir)` runs.
**Then** (assertions):
- **replay-order**: Statements execute in ascending seq.
- **final-state**: `k1` (with `value='v'`) and `k2` exist in `keyframes`.
- **group-flag**: `undo_groups.undone=0` for `g`.
- **redo-log-cleaned**: `redo_log` has no rows with `undo_group=g`.

#### Test: redo-disables-capture-during-replay (covers R13)

**Given**: As above.
**When**: `redo_execute` runs.
**Then** (assertions):
- **active-flag-toggled**: During replay, `undo_state.active` is `0`; after
  replay, `active = 1`.
- **no-new-undo-rows**: No rows are added to `undo_log` as a side effect of
  redo.

#### Test: redo-cleans-redo-log (covers R13)

**Given**: After a successful `redo_execute` for group `g`.
**When**: Inspect `redo_log`.
**Then** (assertions):
- **empty-for-group**: `SELECT COUNT(*) FROM redo_log WHERE undo_group=g`
  returns 0.

#### Test: undo-empty-returns-none

**Given**: Fresh DB; no `undo_begin` has been called.
**When**: `undo_execute(project_dir)` is called.
**Then** (assertions):
- **returns-none**: Returns `None`.
- **no-state-change**: `undo_state`, `undo_log`, `undo_groups` unchanged.

#### Test: redo-empty-returns-none

**Given**: No undone groups exist.
**When**: `redo_execute(project_dir)` is called.
**Then** (assertions):
- **returns-none**: Returns `None`.

#### Test: undo-history-shape-and-order (covers R15)

**Given**: Three groups created at distinct timestamps; `g2` undone.
**When**: `undo_history(project_dir, limit=10)`.
**Then** (assertions):
- **newest-first**: Returned list is ordered by id DESC.
- **row-shape**: Each dict has keys `{id, description, timestamp, undone}`.
- **undone-is-bool**: `undone` is a Python bool; `g2` maps to `True`,
  others to `False`.
- **limit-respected**: Calling with `limit=1` returns one row.

### Edge Cases

Boundaries, composite keys, pruning, replay ordering, and undecided
scenarios.

#### Test: composite-pk-insert-capture (covers R8)

**Given**: `undo_begin` called.
**When**: `INSERT INTO isolation_stems(isolation_id, stem_name, …)
  VALUES('iso1','vocals',…)`.
**Then** (assertions):
- **inverse-uses-composite-key**: `undo_log.sql_text` is
  `DELETE FROM isolation_stems WHERE isolation_id='iso1' AND
  stem_name='vocals'` (or equivalent quoting).

#### Test: track-sends-composite-capture (covers R8)

**Given**: `undo_begin` called.
**When**: An insert into `track_sends` with composite key `(track_id,
  bus_id)`.
**Then** (assertions):
- **inverse-uses-composite-key**: `undo_log.sql_text` DELETE includes both
  `track_id` and `bus_id` in `WHERE`.

#### Test: deferred-fk-allows-replay-ordering (covers R9)

**Given**: A parent `isolations` row and two `isolation_stems` children
  existed, then were deleted. `undo_log` sequence forces replay to insert
  a child before its parent.
**When**: `undo_execute` runs.
**Then** (assertions):
- **no-fk-violation**: Replay completes without error.
- **rows-restored**: Parent and children both present after commit.

#### Test: history-pruned-to-1000 (covers R10)

**Given**: `undo_groups` has 1005 rows.
**When**: `undo_begin(...)` is called.
**Then** (assertions):
- **groups-trimmed**: `undo_groups` contains the 1000 most-recent ids plus
  the newly inserted group (= 1001 max, or 1000 depending on whether the
  new one is counted; assert `COUNT(*) <= 1001`).
- **orphan-undo-log-gone**: No `undo_log` rows remain whose `undo_group`
  is not in `undo_groups`.

#### Test: undo-begin-bumps-past-collision (covers R10)

**Given**: `undo_state.current_group = 5`, but `undo_groups` already has
  id `9` (stale counter).
**When**: `undo_begin("x")` is called.
**Then** (assertions):
- **returned-id-greater**: Return value is `10`.
- **state-updated**: `undo_state.current_group = 10`.

#### Test: single-writer-assumption

**Given**: A project DB opened by the engine.
**When**: Any undo operation runs.
**Then** (assertions):
- **no-locking-protocol**: The code does NOT acquire any named mutex or
  advisory lock — it relies on SQLite's single-writer serialization. This
  test is a negative assertion so future changes that add concurrent
  writers must update the spec.

#### Test: current-group-zero-skips-capture (covers R17, resolves OQ-2)

**Given**: Fresh DB; `undo_state.current_group = 0`; `active = 1`; no
  `begin_undo_group` ever called.
**When**: `INSERT INTO keyframes(id, ...) VALUES ('k1', ...)` runs.
**Then** (assertions):
- **no-undo-row**: `undo_log` has zero rows.
- **row-inserted**: The keyframe exists; mutation proceeded.

#### Test: redo-discarded-on-new-mutation (covers R19, resolves OQ-3)

**Given**: Group `g1` undone with populated `redo_log`; caller issues a
  tracked mutation WITHOUT first calling `begin_undo_group`
  (`current_group = 0`).
**When**: The mutation fires.
**Then** (assertions):
- **redo-stack-cleared**: `redo_log` has no rows for any group.
- **undone-groups-gone**: `undo_groups` has no rows with `undone = 1`.
- **mutation-applied**: The new row is present in the target table.

#### Test: undo-log-capped-per-group (covers R20, resolves OQ-4)

**Given**: Group `g` has 10,000 rows in `undo_log`.
**When**: A 10,001st tracked mutation occurs under `g`.
**Then** (assertions):
- **cap-enforced**: `SELECT COUNT(*) FROM undo_log WHERE undo_group=g`
  equals 10,000.
- **oldest-dropped**: The row with the lowest `seq` for `g` before the
  mutation is gone.
- **newest-present**: The latest mutation's row is present.

#### Test: undo-replay-schema-mismatch-fails (covers R21, resolves OQ-5)

**Given**: An `undo_log` row with `schema_version = 3`; the DB's
  `schema_migrations` current version is `4`.
**When**: `undo_execute` picks up that row's group.
**Then** (assertions):
- **error-raised**: `UndoReplaySchemaVersionMismatch` raised.
- **no-partial-replay**: No mutation applied; SAVEPOINT rolled back.
- **group-unchanged**: `undo_groups.undone` for that group remains `0`.

#### Test: startup-sweep-closes-orphan-groups (covers R22, resolves OQ-6)

**Given**: Two `undo_groups` rows with `completed_at IS NULL` — one with
  `timestamp` 2 hours ago, one with `timestamp` 10 minutes ago.
**When**: The engine startup sweep runs.
**Then** (assertions):
- **old-closed**: The 2-hour-old group's `completed_at` is a UTC ISO
  timestamp.
- **recent-kept-open**: The 10-minute-old group's `completed_at` is
  still NULL.

#### Test: concurrent-writers-no-internal-lock (covers R23, resolves OQ-7, INV-1 negative-assertion)

**Given**: The undo API is invoked with a mock that asserts no named
  mutex or module-level lock is acquired across the call boundary.
**When**: `begin_undo_group` / `undo_execute` / `redo_execute` each run.
**Then** (assertions):
- **no-internal-lock-held**: No `threading.Lock` or `asyncio.Lock` is
  acquired or released during these calls.
- **concurrency-undefined**: Spec asserts concurrency is undefined per
  INV-1; SQLite's own writer serialization is the only contention
  surface.

#### Test: undo-replay-failure-rolls-back (covers R24, resolves OQ-8)

**Given**: `undo_log` contains a row whose replay will raise (e.g. a
  UNIQUE conflict).
**When**: `undo_execute` hits that row mid-replay.
**Then** (assertions):
- **savepoint-rolled-back**: No partial mutation visible after the call.
- **group-flagged**: `undo_groups.replay_failed = 1` for that group.
- **excluded-from-future**: A subsequent `undo_execute` does NOT pick up
  this group.
- **error-surfaced**: The original exception is raised to the caller.

#### Test: undo-across-schema-migration *(undefined — see OQ-5)*

**Given**: `undo_log.sql_text` authored against schema v1; a migration
  adds a NOT NULL column to the tracked table.
**When**: `undo_execute` runs the stored inverse SQL.
**Then** (assertions):
- **expected-behavior**: `undefined`.

#### Test: orphan-undo-group-after-process-death *(undefined — see OQ-6)*

**Given**: `undo_begin("x")` committed; mutations captured; process
  crashes before any other operation.
**When**: A new process reopens the DB and calls `undo_execute`.
**Then** (assertions):
- **expected-behavior**: `undefined`.

#### Test: redo-after-new-non-undo-mutation *(undefined — see OQ-3)*

**Given**: Group `g1` undone (with populated `redo_log`); user issues a
  new tracked mutation WITHOUT first calling `undo_begin` (so `active=1`
  and `current_group` still equals `g1`).
**When**: `redo_execute` is called.
**Then** (assertions):
- **expected-behavior**: `undefined` — does the new mutation corrupt the
  redo entry, does it land in a ghost group id, or does redo silently
  succeed on a now-inconsistent row?

#### Test: mutation-with-current-group-zero *(undefined — see OQ-2)*

**Given**: Fresh DB; `current_group = 0`; `active = 1`; no `undo_begin`
  ever called.
**When**: A tracked INSERT runs.
**Then** (assertions):
- **expected-behavior**: `undefined` — the trigger still fires and
  `undo_log` gets a row with `undo_group = 0`, which has no matching
  `undo_groups` row. Subsequent `undo_execute` ignores it.

#### Test: undo-replay-failure *(undefined — see OQ-8)*

**Given**: `undo_log` contains SQL that will fail at replay (e.g. a UNIQUE
  conflict because an unrelated row now occupies the id).
**When**: `undo_execute` runs and the replay loop raises.
**Then** (assertions):
- **expected-behavior**: `undefined` — current code has no try/rollback
  block; partial replay state may persist.

#### Test: unbounded-undo-log-growth *(undefined — see OQ-4)*

**Given**: A long-running session performs thousands of tracked mutations
  inside a single `undo_begin` group without ever calling `undo_execute`
  or starting a new group.
**When**: Session continues indefinitely.
**Then** (assertions):
- **expected-behavior**: `undefined` — pruning only fires inside
  `undo_begin`; a single mega-group can grow without bound.

---

## Non-Goals

- Frontend undo UX (keyboard, history panel, toasts) — covered elsewhere.
- Undo for plugin-owned sidecar tables. Plugins that want undo must either
  install their own triggers or add their tables to `_undo_tracked_tables`
  via a future extension point.
- Undo across project-DB boundaries.
- Undo for schema DDL (migrations are one-way).
- Selective / branching undo (classic linear undo/redo only; the redo
  stack is unconditionally discarded on next `undo_begin`).
- Concurrent writer safety / row-level locks.

---

## Transitional Behavior (INV-8)

Target-ideal API is `begin_undo_group` / `end_undo_group` /
`is_undo_capturing` (see R18). Current code ships only `undo_begin`
(no explicit end; capture state accessed via raw `undo_state.active`
reads). The following divergences are documented, not codified as the
eventual contract:

- **API aliases**: `undo_begin` is kept importable and dispatches to
  `begin_undo_group` through one release cycle. Callers migrating to
  the target API should switch names and adopt explicit
  `end_undo_group` calls where the intent is to close a group without
  undoing it.
- **`schema_version` column on `undo_log`**: target requires tagging
  rows with the project's current `schema_migrations` version; current
  code has no `schema_migrations` table (see
  `local.engine-migrations-framework` OQ-1). Until the migrations
  framework lands, `schema_version` column may be NULL; mismatch check
  in R21 is a no-op pending implementation.
- **`completed_at` column on `undo_groups`**: target adds this column;
  legacy DBs require an additive ALTER (safe). Startup sweep
  (R22) requires the column to exist.
- **`replay_failed` column on `undo_groups`**: target adds this
  column; additive ALTER.

## Open Questions

### Resolved

- **OQ-1** (API naming): **fix** — rename to `begin_undo_group` / `end_undo_group` / `is_undo_capturing`; keep `undo_begin` / `undo_execute` / `redo_execute` as back-compat aliases through one release cycle. R18, Transitional Behavior.
- **OQ-2** (mutation with `current_group=0`): **codify** — skip capture entirely; mutation proceeds but not undoable. R17, test `current-group-zero-skips-capture`.
- **OQ-3** (redo after new non-undo mutation): **codify** — discard redo stack on new mutation outside `begin_undo_group`. R19, test `redo-discarded-on-new-mutation`.
- **OQ-4** (`undo_log` growth within single group): **fix** — cap 10,000 rows per group; drop oldest on overflow; group partially un-undoable. R20, test `undo-log-capped-per-group`.
- **OQ-5** (replay across schema migrations): **fix** — `undo_log` rows tagged with `schema_version` from `schema_migrations`; replay fails with `UndoReplaySchemaVersionMismatch` on mismatch. R21, test `undo-replay-schema-mismatch-fails`.
- **OQ-6** (orphan group after process death): **fix** — startup sweep closes `undo_groups.completed_at IS NULL` older than 1 hour. R22, test `startup-sweep-closes-orphan-groups`.
- **OQ-7** (multi-writer): closed per INV-1. R23 + negative-assertion test `concurrent-writers-no-internal-lock`.
- **OQ-8** (replay failure recovery): **fix** — replay wrapped in SAVEPOINT; rollback + mark `undo_groups.replay_failed=1` + surface error; group no longer replayable. R24, test `undo-replay-failure-rolls-back`.

### Deferred

(None — all 8 OQs resolved.)

### Historical

### OQ-1 — API naming mismatch

Prompt references `begin_undo_group`, `end_undo_group`, and
`is_undo_capturing`. Actual code exposes `undo_begin` only (no explicit
end; capture state lives in `undo_state.active`). Should the public API
be renamed/expanded to match the prompt's shape, or should the spec stay
with the current surface? Current answer: stay with current surface,
documented in the Interfaces section.

### OQ-2 — Mutation with `current_group = 0`

On a fresh DB, `current_group` is seeded to `0`. Any tracked mutation
before the first `undo_begin` produces an `undo_log` row with
`undo_group=0` and no corresponding `undo_groups` row. Should the
trigger also require a positive `current_group`? Or should startup
auto-create a "bootstrap" group? (Behavior row 22.)

### OQ-3 — Redo after a new non-undo mutation

The current code only discards the redo stack inside `undo_begin`. If a
caller mutates tracked tables without calling `undo_begin` first,
`current_group` still equals the just-undone group, so new mutations may
intermix with its `undo_log`. Does redo still mean the same thing? Should
any tracked mutation while there are `undone=1` groups implicitly clear
the redo stack? (Behavior row 18.)

### OQ-4 — `undo_log` growth unbounded within a single group

History pruning trims `undo_groups` to the last 1000, but never caps the
number of rows per group. A single `undo_begin` followed by heavy
mutation traffic can grow `undo_log` without bound until the group is
rolled or undone. Do we need a per-group row cap, or a size-based cap, or
is caller discipline (regular `undo_begin` calls) sufficient?

### OQ-5 — Replay across schema migrations

Stored `sql_text` is authored against the table shape at mutation time.
If a migration adds a NOT NULL column with no default, replaying an old
DELETE's inverse `INSERT` will fail. Strategies: (a) invalidate/clear
`undo_log` on migration; (b) rewrite stored SQL during migration; (c)
accept breakage and document.

### OQ-6 — Orphan group after process death

There is no explicit `undo_end`. If a process crashes between
`undo_begin` and either `undo_execute` or the next `undo_begin`, the
group stays `undone=0` with associated `undo_log` rows. A new process
sees it as the most recent undoable group. Is this intended? Should
startup run a consistency sweep that discards groups with no rows, or
rolls groups created within the last N seconds of crash?

### OQ-7 — Multi-writer

System assumes single writer per project DB. Should concurrent writers
be detected and rejected, or is relying on SQLite's implicit
serialization sufficient?

### OQ-8 — Replay failure recovery

Neither `undo_execute` nor `redo_execute` wraps the replay loop in a
savepoint or try/rollback. A mid-replay failure leaves the DB in a
partially-undone state. Should replay be wrapped in a SAVEPOINT that
rolls back on exception? Should the group be marked "corrupt" and
skipped?

---

## Related Artifacts

- Source: `src/scenecraft/db.py` (schema + triggers + undo/redo functions).
- Audit: `agent/reports/audit-2-architectural-deep-dive.md` §1C row 8
  ("Undo/Redo System") and row 10 ("Deferred FK").
- Related specs (future): `local.engine-db-schema`, plugin sidecar-tables
  spec (for undo extensibility).

---

**Namespace**: local
**Spec**: engine-db-undo-redo
**Version**: 1.0.0
**Status**: Draft
