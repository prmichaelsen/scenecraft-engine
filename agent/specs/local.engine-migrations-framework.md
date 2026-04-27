# Spec: Engine DB Migrations Framework

**Namespace**: local
**Version**: 1.0.0
**Created**: 2026-04-27
**Last Updated**: 2026-04-27
**Status**: Ready for Proofing

---

**Purpose**: Implementation-ready contract for the *current* scenecraft-engine database migration framework — the set of behaviors encoded in `db.py:_ensure_schema()` that evolve a per-project SQLite DB as the code ships new columns and tables. This spec captures what the system actually does today (column-existence-guarded `ALTER TABLE ADD COLUMN`, one-shot in-place transforms, hardcoded plugin sidecar creation), names the gaps (no `schema_migrations` version table, no `register_migration` API, no rollback, no constraint migration, no data-migration framework), and flags the scenarios the design does not resolve as `undefined` so they can be closed before any implementation work extends the framework.

**Source**: `--from-draft` — task prompt referencing `agent/commands/acp.spec.md`, `agent/reports/audit-2-architectural-deep-dive.md` §1C units 2–3 and §3 leaks #16–18, and the current `src/scenecraft/db.py` implementation (specifically `_ensure_schema()`, lines 136–1049, and the connection bootstrap at lines 44–70).

---

## Scope

### In-Scope

- The schema-bootstrap entry point `_ensure_schema(conn)` and its invocation contract (once per DB path per process, gated by `_migrated_dbs: set[str]`).
- The `CREATE TABLE IF NOT EXISTS` / `CREATE INDEX IF NOT EXISTS` idempotence pattern used for fresh-DB schema materialization.
- The additive migration pattern: `PRAGMA table_info(<table>)` → set of column names → `ALTER TABLE <table> ADD COLUMN <col> <type> [NOT NULL DEFAULT ...]` guarded by `if <col> not in <cols>`.
- The one-shot data-transform pattern (e.g. legacy `transform_x` / `transform_y` static values → `transform_x_curve` / `transform_y_curve` flat JSON curves; `transform_z_curve` → `transform_scale_x_curve` + `transform_scale_y_curve`).
- The legacy-column DROP pattern (greenfield rescue: `volume` → `volume_curve` — DROP the whole table so `CREATE TABLE IF NOT EXISTS` rebuilds it).
- The `ALTER TABLE ... DROP COLUMN` fallback-on-failure pattern (SQLite 3.35+; wrap in `try/except sqlite3.OperationalError: pass`).
- The seed-defaults pattern: after schema materialization, optionally INSERT fixture rows (today: 4 send buses) when the target table is empty.
- The plugin-sidecar auto-creation pattern: plugin-owned tables prefixed `<plugin_id>__<table>` are defined inline inside core `_ensure_schema()`, not contributed by plugins (today: `generate_music__*`, `transcribe__*`, `light_show__*`).
- The process-local migration memo `_migrated_dbs: set[str]` that skips `_ensure_schema` re-entry after first call per DB path, and its interaction with `_conn_lock` (module-global `threading.Lock`).
- The **gaps** the system does not implement today, flagged explicitly:
  - No `schema_migrations` / version-tracking table.
  - No `register_migration(plugin_id, version, up, down, context)` API on `PluginHost` / `plugin_api`.
  - No plugin-contributed migrations (plugin sidecar DDL lives in core `db.py`, violating the spirit of R9a for plugin table ownership).
  - No rollback / `down` migration path — the framework is append-only and forward-only.
  - No constraint migration (can't change a column's `NOT NULL`, `UNIQUE`, `CHECK`, or FK target without a full table rebuild, which the framework does not do).
  - No first-class data migration (data transforms happen inline in `_ensure_schema`, mixed with DDL).
- The contradiction between this implementation and the scenecraft plugin-host design in `agent/milestones/milestone-17-track-contribution-point-and-light-show-plugin.md` + `task-135-migration-contribution-point.md`, which describes `register_migration` with up/down roundtrip and a per-project `schema_migrations` meta table. That design is **not implemented**; this spec records that fact.

### Out-of-Scope (Non-Goals)

- The schema contents themselves (what columns, tables, indexes, triggers exist). Entity-level schemas are covered by other engine-* specs (keyframes, transitions, audio, light_show, transcribe, generate_music, undo/redo, analysis caches).
- `schema_migrations` meta table design and the `register_migration(up, down, ...)` plugin primitive — these are future work, covered (if/when adopted) by a follow-up spec tied to M17 task-135. This spec's job is to capture the *current* framework and mark those as `undefined`.
- Cross-process / cross-host locking (scenecraft-engine is single-process per host; SQLite's own `busy_timeout=60000` + WAL handles concurrent *DML*, not concurrent schema init).
- The global `server.db` (auth/spend/users) — this spec covers per-project `project.db` migrations only. `server.db` schema management is a separate concern.
- Connection-pool lifecycle, transaction semantics, undo/redo replay — those are separate DAL concerns covered by their own specs.
- Any tooling around migration authoring, inspection, or CLI (`scenecraft migrate status`, `scenecraft migrate up`, etc. — none of these exist today and this spec does not add them).

---

## Requirements

### Bootstrap and Invocation

- **R1**: `_ensure_schema(conn)` MUST be callable on any SQLite `Connection` pointed at an empty DB, a partially-initialized DB (older-revision schema), or a fully-current DB, and leave the DB at the current schema revision in all three cases without raising.
- **R2**: `_ensure_schema` MUST be invoked exactly once per `(db_path, process)` pair. The process-local `_migrated_dbs: set[str]` guards re-entry; after a successful call, subsequent `get_db(...)` calls for the same `db_path` in the same process MUST NOT re-run `_ensure_schema`.
- **R3**: The guard set `_migrated_dbs` MUST be consulted while holding `_conn_lock`. Concurrent `get_db` calls for the same `db_path` from different threads in the same process MUST NOT run `_ensure_schema` more than once between them.
- **R4**: `_ensure_schema` MUST establish `PRAGMA foreign_keys=ON`, `PRAGMA journal_mode=WAL`, `PRAGMA synchronous=NORMAL`, and `PRAGMA busy_timeout=60000` before running DDL. (Today these PRAGMAs are set on the connection by `get_db` *before* `_ensure_schema` is called; the spec preserves that ordering.)

### Fresh-DB Materialization

- **R5**: For every core table and index the engine needs, `_ensure_schema` MUST issue `CREATE TABLE IF NOT EXISTS <name> (...)` / `CREATE INDEX IF NOT EXISTS <name> ON <table>(<cols>)` such that running `_ensure_schema` on an empty DB yields the current schema in one pass.
- **R6**: On a fresh DB, after `_ensure_schema` returns, the DB MUST be immediately usable by the DAL — no second pass, no deferred work.

### Additive Column Migration

- **R7**: To add a new column to an existing table, the framework MUST:
  1. Query `PRAGMA table_info(<table>)` and collect column names into a set.
  2. If the new column name is absent, issue `ALTER TABLE <table> ADD COLUMN <col> <type> [NOT NULL DEFAULT <literal>]`.
  3. If the new column name is present, do nothing for that column.
- **R8**: The additive-column pattern MUST be idempotent under arbitrary re-invocation (running `_ensure_schema` N times yields the same schema as running it once).
- **R9**: When an added column is declared `NOT NULL`, it MUST have a `DEFAULT <literal>` so existing rows get a valid value at `ALTER` time. Columns without `NOT NULL` MAY omit `DEFAULT` and default to `NULL`.
- **R10**: The framework MUST re-read `PRAGMA table_info(<table>)` between logically independent migration blocks on the same table when a subsequent check depends on the earlier `ALTER` having run. (The current code re-queries `table_info` several times against the same table for this reason — e.g. `tr_cols`, `tr_cols2`, `tr_cols3`, `tr_cols4`.)

### One-Shot Data Transform

- **R11**: When a new column's values must be derived from a pre-existing column (e.g. flat curve from static float), the transform MUST:
  1. First ensure the new column exists (R7).
  2. Run an `UPDATE` that populates the new column *only where the new column is still NULL* (so user edits after the first migration are not clobbered on re-run).
  3. Be safe to re-run: once the `UPDATE ... WHERE <new> IS NULL` completes, subsequent invocations match zero rows.
- **R12**: When an old column is superseded by a new column, the framework MAY attempt `ALTER TABLE <table> DROP COLUMN <old>`. If `DROP COLUMN` fails (SQLite < 3.35, or a trigger references the old column), the framework MUST catch `sqlite3.OperationalError`, leave the old column in place, and continue. The old column is then considered inert (no code path reads it); removal is deferred to a later schema-cleanup pass.

### Legacy-Column DROP TABLE Rescue

- **R13**: For tables whose rows have never been user-populated in production (today: `audio_tracks`, `audio_clips`), the framework MAY detect a legacy column (`volume`) + missing replacement column (`volume_curve`) and `DROP TABLE IF EXISTS <table>` before the `CREATE TABLE IF NOT EXISTS` block. This path MUST run before any `CREATE TABLE IF NOT EXISTS` statements so the table is recreated with the current definition.
- **R14**: The DROP-TABLE rescue MUST NOT run if either (a) the legacy column is absent, or (b) the replacement column is already present — i.e. it is guarded by `"<legacy>" in cols AND "<replacement>" not in cols`.

### Seed Defaults

- **R15**: After schema materialization, the framework MAY INSERT fixture rows into a target table only if that table is empty (`SELECT COUNT(*) FROM <table>` returns 0). Today this pattern seeds 4 default send buses (`_seed_default_send_buses`).
- **R16**: Seed-defaults MUST NOT overwrite or upsert existing rows. On a non-empty table, the seed step is a no-op.

### Plugin Sidecar Tables (current implementation)

- **R17**: Plugin-owned tables whose names are prefixed `<plugin_id>__<table>` MUST be defined in the core `_ensure_schema` DDL block today (hardcoded), not contributed at plugin activation. The prefix convention is the only boundary between core and plugin-owned tables; there is no runtime-enforced isolation.
- **R18**: Plugin sidecar tables MUST follow the same `CREATE TABLE IF NOT EXISTS` and additive-column patterns as core tables (R5, R7–R10).

### Target-State Requirements (per INV-8)

- **R19 (target)**: Every per-project DB MUST contain a `schema_migrations(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL, applied_by TEXT NOT NULL)` table. Migrations are applied in ascending `version` order; the DAL consults this table to determine which migrations have run. (Transitional: today the framework infers migration state from `PRAGMA table_info` presence; see [Transitional Behavior](#transitional-behavior).)
- **R20 (target)**: `plugin_api.register_migration(version: int, up_fn: Callable[[sqlite3.Connection], None], down_fn: Callable[[sqlite3.Connection], None] | None = None) -> Disposable` MUST be callable from within a plugin's `activate()`. Migrations from all plugins + core are collected and applied in global `version` order on project open. Cycles or duplicate versions MUST raise at registration time.
- **R21 (target — rollback)**: `down_fn` is optional. When provided, `scenecraft migrate down --to <version>` walks migrations in reverse, invoking each `down_fn`. When omitted, the migration is forward-only and MUST be documented as such in the plugin's migration file header. The framework MUST NOT fabricate rollback behavior.
- **R22 (target — constraint migration via rebuild)**: The framework MUST expose `plugin_api.migrate.rebuild_table(name: str, new_schema: str, row_transform: Callable[[sqlite3.Row], dict] | None = None) -> None`, which (1) creates `<name>_new` with `new_schema`, (2) copies rows through the optional transform, (3) `DROP TABLE <name>`, (4) `ALTER TABLE <name>_new RENAME TO <name>`, inside a single transaction. CHECK / UNIQUE / NOT-NULL / FK-target changes MUST go through this helper. Additive `ALTER TABLE ADD COLUMN` is still permitted for the no-constraint-change case.
- **R23 (target — data migrations)**: `up_fn(conn)` receives the live `sqlite3.Connection` and MAY execute arbitrary Python (multi-statement SQL, parse-per-row backfills, external-service lookups). There is no staged-migration framework; migration authors compose complex backfills inside a single `up_fn`.
- **R24 (target — cross-process init lock)**: Schema init + migration-apply MUST be serialized across OS processes on the same `project.db` via an advisory `flock` on `.scenecraft/schema.lock`. The lock is acquired before `_ensure_schema` begins and released after the migration batch commits. A process that fails to acquire within a bounded timeout MUST fall through to reading the (now-migrated) schema state and verify before returning.

---

## Interfaces / Data Shapes

### Entry point

```python
# src/scenecraft/db.py

def _ensure_schema(conn: sqlite3.Connection) -> None:
    """
    Idempotent schema bootstrap.

    Preconditions:
      - conn has foreign_keys=ON, journal_mode=WAL, busy_timeout=60000
        already applied by get_db().
      - conn points at either an empty DB, a partial-schema DB from an
        earlier engine version, or a fully-current DB.

    Postconditions:
      - All CREATE TABLE / CREATE INDEX statements have run.
      - All additive ALTER TABLE ADD COLUMN migrations for the current
        revision are applied.
      - All one-shot data transforms for the current revision have run.
      - Seed defaults (e.g. 4 send buses) have been inserted if applicable.

    Raises:
      - sqlite3.OperationalError if a required ALTER/CREATE fails outside
        the guarded try/except blocks. DROP COLUMN failures are swallowed
        (R12).
    """
```

### Module-level guard

```python
_migrated_dbs: set[str] = set()    # keyed by str(db_path); process-local
_conn_lock: threading.Lock         # guards _connections and _migrated_dbs
```

### Additive migration pattern (canonical form)

```python
cols = {row[1] for row in conn.execute("PRAGMA table_info(<table>)").fetchall()}
if "<new_col>" not in cols:
    conn.execute("ALTER TABLE <table> ADD COLUMN <new_col> <TYPE> [NOT NULL DEFAULT <literal>]")
```

### One-shot transform pattern (canonical form)

```python
cols = {row[1] for row in conn.execute("PRAGMA table_info(<table>)").fetchall()}
if "<new_col>" not in cols:
    conn.execute("ALTER TABLE <table> ADD COLUMN <new_col> <TYPE>")
    # First-time only: derive from legacy column
    rows = conn.execute("SELECT id, <legacy_col> FROM <table> WHERE <legacy_col> IS NOT NULL").fetchall()
    for r in rows:
        conn.execute("UPDATE <table> SET <new_col> = ? WHERE id = ? AND <new_col> IS NULL", (derive(r[1]), r[0]))
```

### DROP COLUMN fallback pattern

```python
try:
    conn.execute("ALTER TABLE <table> DROP COLUMN <old_col>")
except sqlite3.OperationalError:
    pass  # SQLite < 3.35 or trigger reference; column stays, is inert
```

### Seed-defaults pattern

```python
if conn.execute("SELECT COUNT(*) FROM <table>").fetchone()[0] == 0:
    for fixture in _DEFAULTS:
        conn.execute("INSERT INTO <table> (...) VALUES (...)", (...))
```

### NOT in scope — interfaces NOT provided

```python
# These do NOT exist in the engine today. Any spec/task that assumes them
# is describing M17 task-135 future work, not the current framework.

# NOT IMPLEMENTED:
#   PluginHost.register_migration(plugin_id, version, up, down, context) -> Disposable
#   plugin_api.register_migration(...)
#   CREATE TABLE schema_migrations (plugin_id TEXT, version INTEGER, applied_at TEXT, ...)
#   _apply_pending_migrations(plugin_id, cursor)
#   any `down` migration path
#   any CHECK / UNIQUE / NOT-NULL constraint migration path
```

---

## Behavior Table

| # | Scenario | Expected Behavior | Tests |
|---|----------|-------------------|-------|
| 1 | Fresh empty DB → `_ensure_schema` | All CREATE TABLE / CREATE INDEX run; all additive migrations are no-ops; seeds run if their target is empty | `fresh-db-reaches-current-schema-in-one-pass` |
| 2 | Current-revision DB → `_ensure_schema` | All CREATE ... IF NOT EXISTS match existing; all additive guards match existing; seeds see non-empty tables and no-op | `idempotent-on-current-schema` |
| 3 | `_ensure_schema` called N times in same process | Only first call runs DDL; subsequent `get_db` invocations bypass via `_migrated_dbs` | `process-local-memo-skips-reentry` |
| 4 | Two threads call `get_db` concurrently on same new DB | `_conn_lock` serializes; `_ensure_schema` runs exactly once | `conn-lock-serializes-first-init` |
| 5 | Pre-revision DB missing a column | `PRAGMA table_info` returns set without column; `ALTER TABLE ADD COLUMN` runs; second pass is a no-op | `alter-table-add-column-runs-once` |
| 6 | Pre-revision DB missing a `NOT NULL` column | `ALTER TABLE ADD COLUMN ... NOT NULL DEFAULT <literal>` runs; existing rows get the literal | `not-null-add-uses-default-literal` |
| 7 | Pre-revision DB where a superseded column still exists | New column added; one-shot UPDATE populates from legacy values where new column is NULL; subsequent runs match zero rows | `one-shot-transform-populates-only-nulls` |
| 8 | DROP COLUMN on old SQLite or trigger reference | `sqlite3.OperationalError` caught; column remains; migration succeeds | `drop-column-failure-is-swallowed` |
| 9 | Legacy `volume` column + missing `volume_curve` on `audio_tracks` / `audio_clips` | Table DROPPED before CREATE; table recreated with current definition | `legacy-volume-triggers-drop-table-rescue` |
| 10 | Legacy column absent on `audio_tracks` / `audio_clips` | DROP-TABLE rescue does NOT run; CREATE IF NOT EXISTS is a no-op | `drop-table-rescue-skipped-when-not-applicable` |
| 11 | Seed-target table is empty | Fixture rows INSERTED (e.g. 4 default send buses in order) | `empty-seed-target-gets-defaults` |
| 12 | Seed-target table already populated | No INSERT; existing rows untouched | `non-empty-seed-target-is-noop` |
| 13 | Plugin sidecar table (e.g. `light_show__fixtures`) needed | Created by core `_ensure_schema` DDL (hardcoded), not by plugin | `plugin-sidecar-tables-created-by-core` |
| 14 | Code path queries for `schema_migrations` table (target) | Table exists; row per applied migration version | `schema-migrations-table-present-after-init` |
| 15 | Plugin calls `plugin_api.register_migration(version, up_fn, down_fn)` (target) | Registered against host; applied in version order on next project open | `register-migration-applies-in-version-order` |
| 16 | `scenecraft migrate down --to <v>` (target) | Walks registered migrations in reverse invoking each `down_fn`; refuses when any required `down_fn` is `None` | `migrate-down-invokes-down-fns-in-reverse` |
| 17 | Legacy DB with `NOT NULL` on `keyframes.track_id` that current spec wants nullable | `rebuild_table` helper recreates table with relaxed schema, copies rows verbatim | `rebuild-table-relaxes-not-null-constraint` |
| 18 | Plugin needs to add a `CHECK` constraint to `light_show__fixtures.intensity` | `rebuild_table` creates new schema with CHECK; rows copied through transform | `rebuild-table-adds-check-constraint` |
| 19 | Migration that must re-seed or transform data beyond a single-pass UPDATE | `up_fn(conn)` runs arbitrary Python; multi-statement SQL supported | `up-fn-runs-arbitrary-python` |
| 20 | Concurrent schema init across OS processes on the same `project.db` | Advisory `flock` on `.scenecraft/schema.lock` serializes; loser waits then verifies | `schema-lock-serializes-cross-process-init` |
| 21 | `_ensure_schema` raises mid-way (e.g. ALTER TABLE fails on row with pre-existing NULL) | Exception propagates; partial DDL is durable (SQLite auto-commits DDL); `_migrated_dbs` is NOT updated so next call re-runs | `exception-leaves-dbs-unmarked-for-retry` |
| 22 | Read `PRAGMA table_info` between two migration blocks on same table | Snapshot reflects earlier `ALTER TABLE ADD COLUMN` within the same `_ensure_schema` run | `table-info-reflects-in-run-alters` |

---

## Behavior

### Step 1 — Bootstrap gate

`get_db(project_dir, db_path=None)` is the only public entry point. On first call for a given `db_path` in a process:

1. Acquire `_conn_lock`.
2. If `thread_key` not in `_connections`, open a new `sqlite3.Connection` with `check_same_thread=False`, `timeout=60`, `row_factory=sqlite3.Row`.
3. Apply PRAGMAs: `journal_mode=WAL`, `synchronous=NORMAL`, `foreign_keys=ON`, `busy_timeout=60000`.
4. If `db_path not in _migrated_dbs`, call `_ensure_schema(conn)`. On success, add `db_path` to `_migrated_dbs`. On exception, **do not** add to the set (R21 / Test-21) and propagate.
5. Store the connection in `_connections[thread_key]`.
6. Release `_conn_lock`.

Subsequent calls for the same `db_path` skip steps 4 (`_ensure_schema`) entirely.

### Step 2 — Legacy-column DROP TABLE rescue (audio_tracks / audio_clips only)

Before any `CREATE TABLE IF NOT EXISTS`, for each of `audio_clips`, `audio_tracks`:

1. Read `PRAGMA table_info(<table>)` into a column-name set.
2. If `"volume" in cols and "volume_curve" not in cols`: `DROP TABLE IF EXISTS <table>`.
3. Otherwise, no-op.

This is the only form of non-additive DDL the framework performs, and it is restricted to tables that were never user-populated in production.

### Step 3 — Bulk schema materialization

One large `conn.executescript(...)` issues every `CREATE TABLE IF NOT EXISTS`, `CREATE INDEX IF NOT EXISTS`, and trigger-setup block the engine needs for the current revision, including all plugin sidecar tables (`generate_music__*`, `transcribe__*`, `light_show__*`).

### Step 4 — Additive ALTER TABLE migrations

For each evolving table, a block of the form:

```python
cols = {row[1] for row in conn.execute("PRAGMA table_info(<table>)").fetchall()}
if "<col>" not in cols:
    conn.execute("ALTER TABLE <table> ADD COLUMN <col> <TYPE> [NOT NULL DEFAULT <literal>]")
```

runs for every column that has been added since the table's original definition. When a subsequent block depends on the earlier `ALTER` having taken effect, the block re-reads `PRAGMA table_info(<table>)` first.

### Step 5 — One-shot data transforms

Where a new column derives from a superseded column, the transform runs inside the same guarded block that added the column. It populates the new column only where still NULL, making the transform safe on re-run (the second pass finds zero rows to update).

### Step 6 — Optional DROP COLUMN

When a legacy column has been fully superseded and the team has decided it is safe to remove, a `try/except sqlite3.OperationalError: pass` wraps the DROP. On SQLite ≥ 3.35 without trigger references, this cleans up. On older SQLite or when triggers reference the column, the column remains inert.

### Step 7 — Seed defaults

`_seed_default_send_buses(conn)` is called (and by convention, any future seed) gated on the target table being empty:

```python
existing = conn.execute("SELECT COUNT(*) FROM project_send_buses").fetchone()[0]
if existing == 0:
    _seed_default_send_buses(conn)
```

### Step 8 — Commit

`_ensure_schema` does not explicitly commit; DDL in SQLite auto-commits. Data-transform `UPDATE` / `INSERT` statements are committed when the calling transaction closes. `get_db` opens the connection in SQLite's default auto-commit outside explicit transactions.

---

## Acceptance Criteria

- [ ] `_ensure_schema` runs cleanly on a fresh empty DB and yields the current schema in one pass (R1, R5, R6).
- [ ] `_ensure_schema` is idempotent on an already-current DB (R8, Test-2).
- [ ] `_ensure_schema` runs exactly once per `(db_path, process)` pair, guarded by `_migrated_dbs` under `_conn_lock` (R2, R3, Tests 3–4).
- [ ] Every additive migration uses the `PRAGMA table_info` + `ALTER TABLE ADD COLUMN` pattern (R7–R10).
- [ ] Every `NOT NULL` column added via migration has a `DEFAULT <literal>` (R9).
- [ ] One-shot transforms populate only NULL target cells so user edits survive re-runs (R11).
- [ ] `ALTER TABLE DROP COLUMN` failures are swallowed and do not abort migration (R12).
- [ ] `audio_tracks` / `audio_clips` DROP-TABLE rescue runs iff `"volume" in cols AND "volume_curve" not in cols` (R13, R14).
- [ ] `_seed_default_send_buses` runs iff `project_send_buses` is empty (R15, R16).
- [ ] Plugin sidecar tables are defined in core `db.py` today; no runtime plugin-contribution path exists (R17, R18, R20).
- [ ] No `schema_migrations` table is created or queried (R19).
- [ ] Every `undefined` row in the Behavior Table maps to an Open Question with no implementation work assumed.

---

## Tests

### Base Cases

The framework's core contract: fresh-DB bootstrap, idempotent re-invocation, additive migration, one-shot transform, seed defaults, process-local memo.

#### Test: fresh-db-reaches-current-schema-in-one-pass (covers R1, R5, R6)

**Given**: A brand-new empty SQLite file at `/tmp/test.db` and a `sqlite3.Connection` opened against it with `foreign_keys=ON` and WAL enabled.

**When**: `_ensure_schema(conn)` is called.

**Then** (assertions):
- **schema-complete**: every core table the engine DAL reads from exists (`keyframes`, `transitions`, `audio_tracks`, `audio_clips`, `suppressions`, `project_send_buses`, `meta`, `undo_log`, `redo_log`, `undo_state`, etc.).
- **sidecars-complete**: every plugin sidecar table exists (`generate_music__generations`, `generate_music__tracks`, `transcribe__runs`, `transcribe__segments`, `light_show__fixtures`, `light_show__overrides`, and any others hardcoded in `db.py`).
- **indexes-present**: every `CREATE INDEX IF NOT EXISTS` in `db.py` has produced an entry in `sqlite_master`.
- **send-buses-seeded**: `SELECT COUNT(*) FROM project_send_buses` returns exactly 4, with the rows `Plate`, `Hall`, `Delay`, `Echo` in `order_index` 0–3.
- **no-error-raised**: the call returns without exception.

#### Test: idempotent-on-current-schema (covers R2, R8)

**Given**: A DB that has already had `_ensure_schema` run against it once to completion.

**When**: `_ensure_schema(conn)` is called a second time on the same connection (`_migrated_dbs` cleared for the test).

**Then** (assertions):
- **schema-unchanged**: `sqlite_master` row count and contents are identical before and after the second call.
- **no-duplicate-seed**: `project_send_buses` still has exactly 4 rows; no new inserts.
- **no-error-raised**: the call returns without exception.

#### Test: process-local-memo-skips-reentry (covers R2)

**Given**: A DB path that has just been initialized by the current process (`db_path in _migrated_dbs`).

**When**: `get_db(project_dir)` is called again from the same thread or a different thread in the same process.

**Then** (assertions):
- **no-reentry**: `_ensure_schema` is NOT invoked a second time (observable via a mock/spy or by confirming no DDL activity in a `sqlite_master` mtime check).
- **connection-returned**: the call returns a usable `sqlite3.Connection`.

#### Test: conn-lock-serializes-first-init (covers R3)

**Given**: A DB path for which `_migrated_dbs` is empty.

**When**: Two threads call `get_db(project_dir)` simultaneously against the same `db_path`.

**Then** (assertions):
- **single-init**: `_ensure_schema` runs exactly once across both threads.
- **both-return-conn**: both threads receive a usable `sqlite3.Connection`.
- **no-deadlock**: both calls return within a bounded time.

#### Test: alter-table-add-column-runs-once (covers R7, R8)

**Given**: A DB where `transitions` has an older schema missing the `label_color` column.

**When**: `_ensure_schema(conn)` runs.

**Then** (assertions):
- **column-added**: `PRAGMA table_info(transitions)` now lists `label_color`.
- **existing-rows-defaulted**: every pre-existing row has `label_color = ''` (the declared DEFAULT).
- **second-call-noop**: a second `_ensure_schema` call does not re-ALTER; `sqlite_master` schema-text for `transitions` is unchanged.

#### Test: not-null-add-uses-default-literal (covers R9)

**Given**: A DB where `keyframes` is missing the `track_id` column and has one pre-existing row.

**When**: `_ensure_schema(conn)` runs.

**Then** (assertions):
- **column-added-not-null**: `PRAGMA table_info(keyframes)` reports `track_id` with `notnull=1`.
- **default-applied**: the pre-existing row now has `track_id = 'track_1'`.
- **no-null-violation**: no `IntegrityError` was raised.

#### Test: one-shot-transform-populates-only-nulls (covers R11)

**Given**:
- A DB where `transitions` has `transform_x` and `transform_y` legacy static columns populated (e.g. `transform_x = 10, transform_y = 20` for row `id = 't1'`), and no `transform_x_curve` / `transform_y_curve` columns yet.

**When**: `_ensure_schema(conn)` runs.

**Then** (assertions):
- **curve-columns-added**: `transform_x_curve` and `transform_y_curve` now exist as TEXT columns.
- **curve-populated-from-static**: row `t1` has `transform_x_curve = '[[0,10],[1,10]]'` and `transform_y_curve = '[[0,20],[1,20]]'` (flat curves at the legacy values).
- **re-run-no-clobber**: if the user then hand-edits `transform_x_curve = '[[0,5],[1,5]]'` on row `t1` and `_ensure_schema` runs again (memo cleared), `transform_x_curve` remains `'[[0,5],[1,5]]'` — the transform's `WHERE new_col IS NULL` guard skips the already-populated row.

#### Test: drop-column-failure-is-swallowed (covers R12)

**Given**: A DB whose SQLite build does not support `ALTER TABLE DROP COLUMN` (simulate by attaching a trigger on `transitions` that references `OLD.transform_z_curve`), and where the one-shot split-Z transform has already copied `transform_z_curve` into `transform_scale_x_curve` and `transform_scale_y_curve`.

**When**: `_ensure_schema(conn)` runs.

**Then** (assertions):
- **drop-attempted-and-caught**: the attempted `ALTER TABLE transitions DROP COLUMN transform_z_curve` raised `sqlite3.OperationalError` internally, which was caught.
- **column-remains**: `PRAGMA table_info(transitions)` still lists `transform_z_curve`.
- **migration-succeeded**: `_ensure_schema` returned without raising, and the new columns are populated.

#### Test: legacy-volume-triggers-drop-table-rescue (covers R13)

**Given**: A DB where `audio_tracks` has the legacy `volume REAL` column but not `volume_curve`, and contains zero rows (the precondition the rescue relies on: table was never user-populated).

**When**: `_ensure_schema(conn)` runs.

**Then** (assertions):
- **table-dropped-and-recreated**: `PRAGMA table_info(audio_tracks)` now lists `volume_curve` (TEXT) and does NOT list `volume`.
- **no-row-loss-of-real-data**: the table was empty beforehand, so no user data was lost.
- **same-path-for-audio-clips**: the same rescue runs independently for `audio_clips`.

#### Test: drop-table-rescue-skipped-when-not-applicable (covers R14)

**Given**: A DB where `audio_tracks` already has `volume_curve` (no legacy column) OR does not have `volume`.

**When**: `_ensure_schema(conn)` runs.

**Then** (assertions):
- **no-drop**: `audio_tracks` row count and schema are identical before and after.
- **no-warning**: no error or warning is raised.

#### Test: empty-seed-target-gets-defaults (covers R15)

**Given**: A DB where `project_send_buses` exists and is empty.

**When**: `_ensure_schema(conn)` runs.

**Then** (assertions):
- **four-rows-inserted**: `SELECT COUNT(*) FROM project_send_buses` returns 4.
- **correct-order**: rows in `order_index` order are `Plate` (reverb), `Hall` (reverb), `Delay` (delay), `Echo` (echo).
- **static-params-json**: each row's `static_params` is a valid JSON string matching the `_DEFAULT_SEND_BUSES` fixture.

#### Test: non-empty-seed-target-is-noop (covers R16)

**Given**: A DB where `project_send_buses` already has 1 user-customized row.

**When**: `_ensure_schema(conn)` runs.

**Then** (assertions):
- **row-count-unchanged**: `SELECT COUNT(*) FROM project_send_buses` still returns 1.
- **user-row-intact**: the user's row is not modified (labels, static_params, order_index unchanged).
- **no-seed-applied**: the 4 default-bus fixtures are NOT inserted.

#### Test: plugin-sidecar-tables-created-by-core (covers R17, R18, R20)

**Given**: A fresh empty DB; no plugins loaded.

**When**: `_ensure_schema(conn)` runs (with no plugin host involvement).

**Then** (assertions):
- **sidecar-tables-exist**: `generate_music__generations`, `generate_music__tracks`, `transcribe__runs`, `transcribe__segments`, `light_show__fixtures`, `light_show__overrides` all exist in `sqlite_master`.
- **no-plugin-api-called**: there is no `register_migration` or `register_table` call on `plugin_api` — the tables were created by core `_ensure_schema` DDL.
- **no-register-migration-symbol**: `plugin_api` module has no `register_migration` attribute (`hasattr(plugin_api, "register_migration") is False`).

### Edge Cases

Boundaries and hazard classes: concurrency, partial failures, stale memo state, and the explicitly-undefined scenarios.

#### Test: exception-leaves-dbs-unmarked-for-retry (covers R2 negative path)

**Given**: A DB where a synthetic corruption (e.g. a table named the same as a reserved word, or a `CHECK` constraint failure on an existing row) will cause one `ALTER TABLE` inside `_ensure_schema` to raise.

**When**: `_ensure_schema(conn)` is called and the ALTER raises.

**Then** (assertions):
- **exception-propagates**: the `sqlite3.OperationalError` is raised to the caller (not swallowed).
- **memo-not-set**: `db_path not in _migrated_dbs` after the failure.
- **next-call-retries**: the next `get_db(project_dir)` call for the same `db_path` re-invokes `_ensure_schema`.
- **partial-ddl-durable**: any DDL that ran before the failure is durable in the DB (SQLite DDL auto-commits), so the retry picks up where the previous attempt failed.

#### Test: table-info-reflects-in-run-alters (covers R10)

**Given**: A pre-revision DB where `transitions` is missing `label`, `label_color`, and `tags`.

**When**: `_ensure_schema(conn)` runs, which issues three sequential `ALTER TABLE transitions ADD COLUMN` statements with a re-read of `PRAGMA table_info(transitions)` between blocks that reuse `tr_cols`.

**Then** (assertions):
- **all-three-columns-present**: after the function returns, `PRAGMA table_info(transitions)` lists `label`, `label_color`, and `tags`.
- **no-duplicate-alter**: none of the three `ALTER`s ran twice (verified by confirming no `OperationalError: duplicate column name` was raised).

#### Test: negative-no-schema-migrations-table (covers R19)

**Given**: A fresh DB after `_ensure_schema`.

**When**: The DAL queries `SELECT name FROM sqlite_master WHERE type='table' AND name='schema_migrations'`.

**Then** (assertions):
- **table-absent**: the query returns zero rows.
- **no-api-exposed**: `plugin_api` has no `register_migration` attribute; `scenecraft.plugin_host.PluginHost` has no `register_migration` method.

#### Test: negative-no-rollback-api (covers R21)

**Given**: A DB at the current schema revision.

**When**: A caller asks the framework to revert to a prior revision.

**Then** (assertions):
- **no-rollback-function-exists**: `scenecraft.db` exports no `rollback_migration`, `migrate_down`, or equivalent.
- **manual-only**: the only documented way to reduce schema is to edit `db.py` and redeploy.

#### Test: negative-no-constraint-migration (covers R22)

**Given**: A DB where `transitions.label` is declared `NOT NULL DEFAULT ''` (as added by the additive migration).

**When**: A hypothetical need arises to relax that to `NULL`-allowed, or to add a `CHECK(length(label) <= 64)`.

**Then** (assertions):
- **no-framework-path**: the additive-migration framework has no codepath that modifies column constraints in place.
- **requires-manual-table-rebuild**: the only way to achieve the change is a manual CREATE-new / INSERT-select / DROP-old / RENAME sequence authored by hand in `_ensure_schema`, which is out of the framework's contract.

#### Test: schema-migrations-table-present-after-init (covers R19 target)

**Given**: A fresh DB after `_ensure_schema`.

**When**: Query `SELECT name FROM sqlite_master WHERE type='table' AND name='schema_migrations'`.

**Then** (assertions):
- **table-exists**: query returns one row.
- **columns-present**: `PRAGMA table_info(schema_migrations)` lists `version`, `applied_at`, `applied_by`.
- **seeded-with-core**: every core migration version has a row with non-null `applied_at`.

#### Test: register-migration-applies-in-version-order (covers R20 target)

**Given**: Plugin A registers `register_migration(version=1001, up_fn=fn_a1)`; Plugin B registers `register_migration(version=1000, up_fn=fn_b)`; Plugin A registers `register_migration(version=1002, up_fn=fn_a2)`.

**When**: Project DB is opened and the framework applies pending migrations.

**Then** (assertions):
- **apply-order**: `fn_b` runs first, then `fn_a1`, then `fn_a2` — strict ascending version order regardless of registration order.
- **ledger-updated**: `schema_migrations` gains three rows with versions 1000, 1001, 1002.
- **idempotent-reopen**: reopening the project does not re-run any of the three.

#### Test: migrate-down-invokes-down-fns-in-reverse (covers R21 target)

**Given**: Two migrations applied — v1010 with `down_fn=d1` and v1011 with `down_fn=d2`.

**When**: `scenecraft migrate down --to 1009` is invoked.

**Then** (assertions):
- **reverse-order**: `d2` runs before `d1`.
- **ledger-pruned**: `schema_migrations` rows for 1010 and 1011 are deleted.
- **refuses-on-none-down**: if v1010 had `down_fn=None`, the command aborts before any down runs with `MigrationNotReversibleError`.

#### Test: rebuild-table-relaxes-not-null-constraint (covers R22 target)

**Given**: A legacy DB where `keyframes.track_id` has `NOT NULL` and 10 populated rows.

**When**: A migration calls `plugin_api.migrate.rebuild_table("keyframes", new_schema=<same schema but track_id TEXT nullable>)`.

**Then** (assertions):
- **new-schema-nullable**: post-rebuild, `PRAGMA table_info(keyframes)` reports `track_id` with `notnull=0`.
- **rows-preserved**: all 10 rows present with identical `id`, `track_id`, and other values.
- **atomic**: the rebuild executed inside a single transaction; partial failure would leave the original table in place.

#### Test: rebuild-table-adds-check-constraint (covers R22 target)

**Given**: `light_show__fixtures` exists without a CHECK on `intensity`.

**When**: A migration calls `rebuild_table("light_show__fixtures", new_schema=<includes CHECK(intensity BETWEEN 0 AND 1)>, row_transform=lambda r: {**dict(r), "intensity": max(0, min(1, r["intensity"]))})`.

**Then** (assertions):
- **check-enforced**: subsequent inserts with `intensity=1.5` raise `sqlite3.IntegrityError`.
- **transform-applied**: pre-existing rows with out-of-range values were clamped via the transform.

#### Test: up-fn-runs-arbitrary-python (covers R23 target)

**Given**: A migration whose `up_fn(conn)` (1) adds a column, (2) SELECTs every row, (3) parses a JSON blob per row, (4) writes derived values back, (5) creates an index.

**When**: The framework applies the migration.

**Then** (assertions):
- **all-steps-applied**: column present, derived values correct, index listed in `sqlite_master`.
- **single-version-row**: one `schema_migrations` row added, not five.

#### Test: schema-lock-serializes-cross-process-init (covers R24 target)

**Given**: Two OS processes both open the same `project.db` simultaneously with no prior init.

**When**: Both call `_ensure_schema`.

**Then** (assertions):
- **flock-acquired-once**: advisory lock on `.scenecraft/schema.lock` is held by exactly one process at a time; the other blocks until release.
- **no-double-apply**: each migration's `up_fn` runs exactly once (verified via spy or `schema_migrations` row count).
- **both-return-healthy**: after release, the waiting process observes the completed schema and returns cleanly.

#### Test: negative-no-internal-lock-across-api (covers INV-1)

**Given**: A migration in progress for project A; a separate project B's DAL call arrives concurrently.

**When**: The B call executes.

**Then** (assertions):
- **no-shared-lock**: B's call is not blocked by A's schema_lock (lock is per-project, path-specific).
- **independent-connections**: INV-1 holds — single-writer applies per (user, project), not across projects.

#### Test: undefined-legacy-not-null-track-id (covers OQ-4)

**Given**: A DB produced by an older engine where `keyframes.track_id` was created with `NOT NULL` but no DEFAULT, and contains rows with `track_id = 'track_1'` (populated by an earlier ALTER) — AND a current code path that assumes `track_id` is nullable.

**When**: The framework runs and a DAL insert writes `track_id = NULL`.

**Then** (assertions):
- **behavior-undefined**: this spec does NOT guarantee the insert succeeds or fails; the framework does not inspect `notnull` state and makes no decision. Resolution pending [OQ-4](#open-questions).

#### Test: undefined-plugin-check-constraint (covers OQ-5)

**Given**: A plugin needs to add `CHECK(value BETWEEN 0 AND 1)` to `light_show__fixtures.intensity`.

**When**: The plugin attempts this.

**Then** (assertions):
- **no-supported-path**: there is no framework API today; a plugin must either edit core `db.py` or wait for the M17 `register_migration` primitive. Resolution pending [OQ-5](#open-questions).

#### Test: undefined-data-migration-multi-stage (covers OQ-6)

**Given**: A migration that must (a) add a column, (b) backfill it from an external source (e.g. parse JSON blobs across all rows), and (c) enforce a constraint.

**When**: This is attempted inside `_ensure_schema`.

**Then** (assertions):
- **no-staged-framework**: `_ensure_schema` supports only in-pass inline transforms; there is no `post-ensure` hook, no staged progress tracking, no resume-from-partial. Resolution pending [OQ-6](#open-questions).

#### Test: undefined-concurrent-schema-init-across-processes (covers OQ-7)

**Given**: Two OS processes (e.g. `scenecraft server` + a CLI command) both call `get_db(project_dir)` against the same `project.db` at the same moment, both with empty `_migrated_dbs` sets.

**When**: Both call `_ensure_schema` concurrently.

**Then** (assertions):
- **behavior-undefined**: `_migrated_dbs` is process-local, so both processes will attempt bootstrap. SQLite's file lock + `busy_timeout=60000` arbitrates individual statements, but double-execution of the full `CREATE TABLE IF NOT EXISTS` / `ALTER TABLE ADD COLUMN IF NOT EXISTS`-style block is not specified end-to-end. Resolution pending [OQ-7](#open-questions).

**Note on coverage completeness**: happy path (Tests 1–2, 5–7, 9, 11, 13), bad path (Test 21, plus undefined Tests 17–20), positive assertions (schema presence, row counts, specific rows), negative assertions (no duplicate seeds, no clobbered user edits, no `register_migration` symbol, no `schema_migrations` table, no rollback API, no constraint-migration path) are all represented across Base + Edge.

---

## Non-Goals

- Implementing a `schema_migrations` meta table (deferred to M17 task-135 as a separate spec).
- Implementing `register_migration` on `PluginHost` / `plugin_api` (deferred to M17 task-135).
- Implementing any `down` / rollback path (explicitly out of scope; see R21).
- Rebuilding tables to change constraints (`NOT NULL`, `UNIQUE`, `CHECK`, FK target) — requires a separate table-rebuild framework, not in scope.
- Staged data-migration with resume / progress tracking — not in scope.
- A CLI surface for migration inspection or invocation (`scenecraft migrate status`, etc.) — not in scope.
- Cross-process coordination of schema init beyond what SQLite's own file lock + `busy_timeout` provides — not in scope.
- Migrating `server.db` (auth/spend/users) — covered by a separate spec if/when needed.
- Moving plugin sidecar table DDL out of core `db.py` into plugin-owned migrations — future work under M17 task-135.

---

## Transitional Behavior

Per INV-8, Requirements R19–R24 describe the **target-ideal** migration framework. Until the FastAPI refactor milestone (task-135) lands, the following **transitional** behavior ships today in `src/scenecraft/db.py`:

- **No `schema_migrations` table**: migration state is inferred purely from `PRAGMA table_info(<table>)` + `sqlite_master` queries. Code paths that depend on "did migration vN run?" MUST inspect schema shape, not a version ledger.
- **No `register_migration` API**: `plugin_api` has no `register_migration` attribute; `PluginHost` has no `register_migration` method. Plugin sidecar DDL lives hardcoded in core `db.py` (`generate_music__*`, `transcribe__*`, `light_show__*`).
- **Additive-only ALTER**: all column migrations use the `PRAGMA table_info` + `ALTER TABLE ADD COLUMN` pattern. Constraint changes (NOT NULL, CHECK, UNIQUE, FK target) are NOT supported; pre-existing legacy constraints (e.g. `audio_clips.track_id NOT NULL` on older DBs) remain in place.
- **No rollback**: there is no `down_fn`, no `migrate down` CLI, no way to return a DB to a prior schema revision without editing `db.py` and redeploying.
- **One-shot data transforms inline**: data-derivation migrations happen inside `_ensure_schema` using the `UPDATE ... WHERE new_col IS NULL` pattern. Multi-stage backfills are not supported.
- **No cross-process lock**: `_migrated_dbs` is process-local. Two OS processes hitting an un-bootstrapped `project.db` both enter `_ensure_schema` concurrently; SQLite's per-statement file lock + `busy_timeout=60000` is the only coordination. Double-bootstrap is tolerated only because every DDL is guarded by `IF NOT EXISTS` or a column-existence check — it is NOT transactionally correct.

Regression-lock tests (Tests 1–13 in Base Cases, Tests 21–22 in Edge Cases) cover this transitional behavior and stay in place until the target framework lands. The `### Base Cases` target-behavior tests added above (Tests 14–20) are authored against the target contract and will begin passing only after the migration primitive is implemented.

**Migration sequence** to the target:
1. Add `schema_migrations` table (idempotent `CREATE TABLE IF NOT EXISTS`) — cheap, non-breaking.
2. Add `plugin_api.register_migration` + `plugin_api.migrate.rebuild_table` primitives.
3. Move hardcoded plugin sidecar DDL out of `db.py` into each plugin's `activate()` via `register_migration`.
4. Wire advisory `flock` on `.scenecraft/schema.lock`.
5. Expose `scenecraft migrate down --to <version>` CLI + enforce `down_fn` presence check.

---

## Open Questions

### Resolved

- **OQ-1 (`schema_migrations` version table)** — **Resolved** (fix): target state includes per-project `schema_migrations(version, applied_at, applied_by)` table; current column-existence check is transitional. See R19 and [Transitional Behavior](#transitional-behavior).
- **OQ-2 (`register_migration` plugin primitive)** — **Resolved** (codify): yes, M17 design is the target. `plugin_api.register_migration(version, up_fn, down_fn=None)` called during plugin activation; migrations applied in global version order on project open. See R20.
- **OQ-3 (rollback semantics)** — **Resolved** (codify): `down_fn` is optional; when omitted, migration is forward-only and docs must note it. `scenecraft migrate down --to <v>` walks reverse. See R21.
- **OQ-4 (legacy DB pre-existing `NOT NULL`)** — **Resolved** (fix): target includes `migrate.rebuild_table(name, new_schema, row_transform=None)` helper. Current additive-ALTER approach is transitional and leaves legacy NOT NULL in place on pre-M13 DBs. See R22.
- **OQ-5 (plugin CHECK constraints)** — **Resolved** (codify): supported via `rebuild_table`; CHECK / UNIQUE / NOT-NULL changes require a full table rebuild. See R22.
- **OQ-6 (data migrations beyond single-pass UPDATE)** — **Resolved** (codify): `up_fn(conn)` accepts arbitrary Python including multi-statement SQL. See R23.
- **OQ-7 (concurrent schema init across OS processes)** — **Resolved** (fix): advisory `flock` on `.scenecraft/schema.lock` during `_ensure_schema` + migration apply. See R24.

### Deferred

_(none — all OQs resolved in the 2026-04-27 pass)_

### Historical (retained for audit trail)

#### OQ-1: `schema_migrations` version table — is inference-from-schema sufficient indefinitely?

Today, "which migration has run" is inferred purely from `PRAGMA table_info(<table>)` and table presence. This works as long as every migration is column-additive or table-creative. Constraint changes, data-only migrations, and rollback all require an explicit version ledger. Should the engine add a `schema_migrations` table unconditionally (cheap insurance), defer until a plugin needs it (YAGNI), or punt to M17 task-135 which already proposes one?

#### OQ-2: `register_migration` plugin primitive — is the M17 design still the target?

`agent/milestones/milestone-17-track-contribution-point-and-light-show-plugin.md` and `task-135-migration-contribution-point.md` describe a `register_migration(plugin_id, version, up, down, context)` API with per-plugin versioning, up/down roundtrip, SQL-string / list-of-SQL / callable content types, and transactional execution. That design is unimplemented. Is it still the intended direction, or has the team's thinking evolved (e.g. declarative `plugin.yaml` migration lists rather than imperative `register_migration` calls)?

#### OQ-3: Rollback semantics — what's the contract for reverting a shipped migration?

Today: the only rollback is edit `db.py` + redeploy, and any rows written under the new schema may be unreconcilable. Is that acceptable as the permanent position, or does the team want first-class rollback? If yes, does it need to preserve the rows written under the new schema (transform back), or is it acceptable to fail on "cannot safely revert"?

#### OQ-4: Legacy DB with pre-existing `NOT NULL` constraint on a now-nullable column

The audit calls out `keyframes.track_id` specifically: older DBs may still have `NOT NULL` on `track_id` from before the framework added it nullably. `PRAGMA table_info` reports `notnull=1` but the framework never reads that field and never rebuilds the table. If a current DAL call writes `track_id = NULL`, the insert fails on the legacy DB but succeeds on a fresh DB. Is this acceptable as "known broken on pre-M13 DBs" (rare — scenecraft is greenfield), or does the framework need a one-time table-rebuild to normalize constraints? If yes, what's the rebuild heuristic (trigger from which marker)?

#### OQ-5: Plugin CHECK constraints — supported or explicitly forbidden?

Plugins today cannot add `CHECK` constraints in their sidecar tables because those tables' DDL lives in core `db.py` where only the core team writes SQL. Once `register_migration` exists, plugins *could* declare CHECK via `CREATE TABLE` but *adding* a CHECK to an existing table requires a rebuild. Should the `register_migration` contract explicitly forbid constraint migration and require plugins to plan constraints at initial table creation?

#### OQ-6: Data migrations beyond single-pass `UPDATE ... WHERE new_col IS NULL`

The current framework's one-shot transform works for simple derivations. Multi-stage backfills (parse-per-row + normalize + validate + enforce-constraint) have no framework support. Should these be authored as Python callables inside `_ensure_schema` (expands the function indefinitely), as separate `post_schema` hooks (adds a phase the framework doesn't have), or punted to M17 task-135's callable migration content type?

#### OQ-7: Concurrent schema init across OS processes

`_migrated_dbs` is process-local. Two processes both hitting an un-bootstrapped `project.db` (e.g. engine server + a CLI admin command at the same moment) will both enter `_ensure_schema`. SQLite's file lock + `busy_timeout=60000` will serialize individual DDL statements, but the full sequence (DROP TABLE rescue + CREATE block + ALTER blocks + seeds) is not an atomic transaction. Possible outcomes: both complete cleanly (DDL is `IF NOT EXISTS` + guarded); one fails mid-way and retries on next invocation; one clobbers a partially-populated seed of the other. Is "both complete cleanly because every DDL is idempotent" the position the spec should cement, or does the framework need a lock file / meta-table advisory lock?

---

## Related Artifacts

- **Source audit**: `agent/reports/audit-2-architectural-deep-dive.md` §1C units 2–3, §2 invariant "Migration additive-only", §3 leaks #16 / #17 / #18.
- **Source code**: `src/scenecraft/db.py` — `_ensure_schema()` (lines 136–1049), `_seed_default_send_buses()` (lines 116–133), `_migrated_dbs` guard (line 44), `_conn_lock` + `get_db` (lines 40–70).
- **Contradicted plan**: `agent/milestones/milestone-17-track-contribution-point-and-light-show-plugin.md` (describes `register_migration`, `schema_migrations` meta table, up/down roundtrip — all unimplemented today).
- **Future-work spec** (when M17 task-135 lands): a new `agent/specs/local.plugin-migration-contribution-point.md` should define `register_migration`, the `schema_migrations` table, transactional `up`/`down` execution, and the plugin-host integration. This current spec stays as the record of the M16-era framework.
- **Related engine specs**: `local.fastapi-migration.md`, `local.openapi-tool-codegen.md` (neither touches DB migrations).
- **Related memory**: `project_plugins_own_sidecar_tables.md` (plugins own their sidecar tables — aspirational; today's implementation violates this by keeping sidecar DDL in core `db.py`).

---

## Notes

- This spec describes the framework *as it is*, not *as it ought to be*. The framework has evolved organically over M9–M16 and is serviceable for the single-engine, single-project, additive-only reality the codebase lives in. It is not forward-compatible with M17's plugin-migration ambition without real work.
- The framework's greatest strength is its simplicity: column-existence-checks are trivially idempotent, and re-running `_ensure_schema` on any DB is safe. Its greatest weakness is the same: because the "migration" is just the current state of `_ensure_schema`, there is no audit trail of which migrations ran when, no way for plugins to contribute, and no way to reverse anything.
- When M17 task-135 is picked up, the right move is to (a) add `schema_migrations` alongside the current framework (don't replace it — core migrations can stay in `_ensure_schema`), (b) expose `register_migration` for plugins only, and (c) migrate the hardcoded plugin sidecar DDL (`generate_music__*`, `transcribe__*`, `light_show__*`) out of `db.py` into each plugin's `activate()` — deleting those blocks from core is the payoff.
- Five of the seven Open Questions (OQ-1, OQ-2, OQ-3, OQ-5, OQ-6) converge on "what does M17 actually deliver?"; answering that one design question closes most of this spec's `undefined` rows.

---

**Namespace**: local
**Spec**: engine-migrations-framework
**Version**: 1.0.0
**Created**: 2026-04-27
**Last Updated**: 2026-04-27
**Status**: Ready for Proofing
