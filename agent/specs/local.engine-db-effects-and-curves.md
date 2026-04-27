# Spec: engine-db-effects-and-curves

**Namespace**: local
**Version**: 1.0.0
**Created**: 2026-04-27
**Last Updated**: 2026-04-27
**Status**: Draft

---

## Purpose

Define the engine-side persistence contract for per-project audio effects, their
automation curves, project-level send buses, and the track-to-bus send junction.
This spec covers the on-disk schema (SQLite DDL in `db.py`), the DAL surface
(public `add_*` / `get_*` / `list_*` / `update_*` / `delete_*` / `upsert_*`
helpers) and the cross-table invariants the rest of the engine relies on.

## Source

- `--from-draft` — chat-authored context, grounded in:
  - `/home/prmichaelsen/.acp/projects/scenecraft-engine/src/scenecraft/db.py`
    (DDL, seed, migration, and DAL functions for `track_effects`,
    `effect_curves`, `project_send_buses`, `track_sends`).
  - `/home/prmichaelsen/.acp/projects/scenecraft-engine/agent/reports/audit-2-architectural-deep-dive.md`
    §1C (DB schema + DAL + migrations inventory).
  - `/home/prmichaelsen/.acp/projects/scenecraft/agent/specs/local.audio-effects-and-curve-scheduling.md`
    (frontend-side contract; informational cross-reference).

## Scope

**In scope**

- DDL for four tables:
  - `track_effects` (per-track OR master-bus effect row; `track_id` nullable).
  - `effect_curves` (per-parameter JSON point arrays keyed by `(effect_id, param_name)`).
  - `project_send_buses` (project-scoped bus catalog; seeded with four defaults).
  - `track_sends` (composite-PK junction of `audio_tracks` × `project_send_buses` with `level`).
- Public DAL surface in `scenecraft.db`:
  - Track-effect helpers: `add_track_effect`, `get_track_effect`,
    `list_track_effects`, `update_track_effect`, `delete_track_effect`.
  - Master-bus helpers: `add_master_bus_effect`, `list_master_bus_effects`,
    `get_master_bus_effect` (NULL-`track_id` scoping).
  - Curve helpers: `add_effect_curve`, `get_effect_curve`,
    `list_curves_for_effect`, `upsert_effect_curve`, `update_effect_curve`,
    `delete_effect_curve`.
  - Bus helpers: `add_send_bus`, `get_send_bus`, `list_send_buses`,
    `update_send_bus`, `delete_send_bus`.
  - Send helpers: `list_track_sends`, `get_track_send`, `upsert_track_send`,
    `delete_track_send`.
- Semantics:
  - Master-bus vs per-track effect distinction via `track_id IS NULL`.
  - `enabled` flag (integer 0/1, exposed as `bool`) for bypass.
  - `order_index` determining serial chain order within a scope
    (per-track OR the master bus).
  - `effect_curves` UNIQUE constraint on `(effect_id, param_name)` and
    UPSERT policy.
  - `ON DELETE CASCADE` chains:
    - `audio_tracks` → `track_effects` (track-scoped rows).
    - `audio_tracks` → `track_sends`.
    - `track_effects` → `effect_curves`.
    - `project_send_buses` → `track_sends`.
  - Seeding: `project_send_buses` is seeded with four default buses (Plate
    reverb, Hall reverb, Delay, Echo) if empty at schema bootstrap AND
    `track_sends` is backfilled at level `0.0` for every existing track.
  - Auto-insert trigger: new `audio_tracks` INSERT backfills zero-level
    `track_sends` rows for every existing bus.
  - Sparse JSON merge-patch pattern in the DAL (RFC 7396): the DAL's
    `update_*` helpers accept whole-value replacement of the `static_params`
    or `points` JSON blob; partial-merge is NOT performed in these helpers
    (those helpers whole-replace). The merge-patch pattern IS implemented in
    the light-show scenes DAL (`db.py:4690–4703`) and is referenced here to
    make its absence on `static_params` an intentional, tested design choice.
  - Raw-UPDATE bypass: direct SQL `UPDATE` against these tables bypasses all
    DAL validation and JSON-coercion logic; the spec flags this explicitly
    so tests can assert the DAL is the only safe writer.

**Out of scope**

- Frontend curve scheduling and WebAudio node graph — already specced in
  `scenecraft/agent/specs/local.audio-effects-and-curve-scheduling.md`.
- Effect-type registry (set of valid `effect_type` strings, per-type param
  list, animatable flags) — already specced in the same cross-ref spec.
- HTTP endpoints that call this DAL (REST routing, auth, validation) —
  covered by `engine-rest-api-dispatcher`.
- Undo/redo triggers — covered by `engine-db-undo-redo`. This spec only
  asserts that effect/curve/bus/send tables are present in the
  `_undo_tracked_tables` list (cross-reference only).
- Migrations framework — covered by `engine-migrations-framework`. This
  spec asserts the *observable outcome* of the one migration relevant
  here (legacy NOT-NULL `track_id` → nullable) but does not spec the
  migration mechanism.
- Plugin-owned sidecar tables — these four tables are CORE (no `__`
  prefix); plugins do not extend them.

## Requirements

1. **R1 — `track_effects` table shape**: Columns `id TEXT PK`,
   `track_id TEXT NULL REFERENCES audio_tracks(id) ON DELETE CASCADE`,
   `effect_type TEXT NOT NULL`, `order_index INTEGER NOT NULL`,
   `enabled INTEGER NOT NULL DEFAULT 1`, `static_params TEXT NOT NULL`,
   `created_at TEXT NOT NULL`. `static_params` is always valid JSON
   (empty object `"{}"` when unspecified).
2. **R2 — master-bus via NULL `track_id`**: A `track_effects` row with
   `track_id IS NULL` denotes a master-bus effect. Track-scoped
   helpers (`list_track_effects`, `get_track_effect`) MUST NOT return
   master-bus rows, and master-bus helpers MUST NOT return track-scoped
   rows.
3. **R3 — legacy NOT-NULL migration**: On schema bootstrap against a
   legacy DB where `track_effects.track_id` is NOT NULL, the schema is
   rewritten in place to make it nullable while preserving all existing
   rows and their non-null `track_id` values. Fresh DBs start nullable.
4. **R4 — `effect_curves` table shape**: Columns `id TEXT PK`,
   `effect_id TEXT NOT NULL REFERENCES track_effects(id) ON DELETE CASCADE`,
   `param_name TEXT NOT NULL`, `points TEXT NOT NULL`,
   `interpolation TEXT NOT NULL DEFAULT 'bezier'`,
   `visible INTEGER NOT NULL DEFAULT 0`, with
   `UNIQUE(effect_id, param_name)`.
5. **R5 — `project_send_buses` table shape**: Columns `id TEXT PK`,
   `bus_type TEXT NOT NULL`, `label TEXT NOT NULL`,
   `order_index INTEGER NOT NULL`, `static_params TEXT NOT NULL`.
6. **R6 — `track_sends` table shape**: Columns
   `track_id TEXT NOT NULL REFERENCES audio_tracks(id) ON DELETE CASCADE`,
   `bus_id TEXT NOT NULL REFERENCES project_send_buses(id) ON DELETE CASCADE`,
   `level REAL NOT NULL DEFAULT 0.0`, `PRIMARY KEY (track_id, bus_id)`.
7. **R7 — default bus seeding**: When `project_send_buses` is empty at
   schema bootstrap, exactly four rows are inserted with these
   `(bus_type, label, order_index, static_params)` tuples, in this order:
   - `("reverb", "Plate", 0, {"ir": "plate.wav"})`
   - `("reverb", "Hall",  1, {"ir": "hall.wav"})`
   - `("delay",  "Delay", 2, {"time_division": "1/4", "feedback": 0.35})`
   - `("echo",   "Echo",  3, {"time_ms": 120.0, "feedback": 0.0, "tone": 0.5})`
8. **R8 — track-send backfill on seed**: After default-bus seeding,
   `track_sends` is populated with a `(track_id, bus_id, 0.0)` row for
   every `(audio_track, bus)` pair via `INSERT OR IGNORE`.
9. **R9 — new-track send backfill trigger**: An `AFTER INSERT` trigger
   on `audio_tracks` inserts `(NEW.id, bus.id, 0.0)` for every row in
   `project_send_buses`, using `INSERT OR IGNORE`.
10. **R10 — `add_track_effect` default `order_index`**: When
    `order_index` is omitted, it is assigned `COALESCE(MAX(order_index), -1) + 1`
    **scoped to the same `track_id`** (not global).
11. **R11 — `add_master_bus_effect` default `order_index`**: Same rule as
    R10 but scoped to `track_id IS NULL`.
12. **R12 — `delete_track_effect` cascades to curves**: Deleting a
    `track_effects` row hard-deletes all `effect_curves` rows with matching
    `effect_id` via FK `ON DELETE CASCADE`. Deleting a non-existent id is
    a no-op (no error raised).
13. **R13 — `upsert_effect_curve` semantics**: `INSERT ... ON CONFLICT
    (effect_id, param_name) DO UPDATE SET points/interpolation/visible`.
    The row's `id` is stable across updates (only the payload fields
    change). `add_effect_curve` (non-upsert) raises
    `sqlite3.IntegrityError` on duplicate `(effect_id, param_name)`.
14. **R14 — `upsert_track_send` semantics**: `INSERT ... ON CONFLICT
    (track_id, bus_id) DO UPDATE SET level = excluded.level`. Returns the
    hydrated `_TrackSend` dataclass.
15. **R15 — `delete_send_bus` cascades to sends**: Deleting a bus
    hard-deletes every `track_sends` row with matching `bus_id` via FK
    `ON DELETE CASCADE`.
16. **R16 — JSON coercion in DAL update helpers**: `update_track_effect`,
    `update_effect_curve`, and `update_send_bus` detect non-string values
    for `static_params` / `points` and `json.dumps` them before UPDATE.
    Boolean `enabled` / `visible` are coerced to integer 0/1. Other
    fields are passed through.
17. **R17 — enabled flag materializes as bool on read**: Row-mapper
    functions (`_row_to_track_effect`, `_row_to_effect_curve`) return
    `enabled`/`visible` as Python `bool`, and JSON-decode `static_params`
    /`points` into native Python objects.
18. **R18 — ordering contracts**:
    - `list_track_effects` returns rows ordered by `order_index` ASC.
    - `list_master_bus_effects` returns rows ordered by `order_index` ASC.
    - `list_curves_for_effect` returns rows ordered by `param_name` ASC.
    - `list_send_buses` returns rows ordered by `order_index` ASC.
    - `list_track_sends` returns rows ordered by `track_id, bus_id` ASC.
19. **R19 — whole-value replacement for JSON blobs in DAL updates**:
    The DAL's `update_*` helpers REPLACE `static_params` / `points`
    wholesale when those fields are present; they do NOT merge-patch.
    (The RFC 7396 sparse merge-patch pattern is used elsewhere — e.g.
    light-show scenes — but is intentionally NOT applied here.)
20. **R20 — raw-UPDATE bypass is observable**: A direct
    `conn.execute("UPDATE track_effects SET static_params = ?", ...)`
    with a non-JSON string succeeds at the SQL layer and is readable
    back via the DAL, but the DAL's `_row_to_track_effect` mapper will
    raise when it attempts `json.loads`. This is the known leak.
21. **R21 — CASCADE on track delete**: Hard-deleting an `audio_tracks`
    row cascades to:
    - every `track_effects` row with matching `track_id`,
    - every `effect_curves` row attached to those effects (transitively),
    - every `track_sends` row with matching `track_id`.
22. **R22 — master-bus effects survive track delete**: Hard-deleting any
    `audio_tracks` row does NOT delete any `track_effects` row with
    `track_id IS NULL`, nor any `effect_curves` attached to master-bus
    effects.

## Interfaces / Data Shapes

### DDL (authoritative SQL)

```sql
CREATE TABLE IF NOT EXISTS track_effects (
    id TEXT PRIMARY KEY,
    track_id TEXT REFERENCES audio_tracks(id) ON DELETE CASCADE, -- nullable = master-bus
    effect_type TEXT NOT NULL,
    order_index INTEGER NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    static_params TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS track_effects_track_order
    ON track_effects(track_id, order_index);

CREATE TABLE IF NOT EXISTS effect_curves (
    id TEXT PRIMARY KEY,
    effect_id TEXT NOT NULL REFERENCES track_effects(id) ON DELETE CASCADE,
    param_name TEXT NOT NULL,
    points TEXT NOT NULL,
    interpolation TEXT NOT NULL DEFAULT 'bezier',
    visible INTEGER NOT NULL DEFAULT 0,
    UNIQUE(effect_id, param_name)
);
CREATE INDEX IF NOT EXISTS idx_effect_curves_effect
    ON effect_curves(effect_id);

CREATE TABLE IF NOT EXISTS project_send_buses (
    id TEXT PRIMARY KEY,
    bus_type TEXT NOT NULL,
    label TEXT NOT NULL,
    order_index INTEGER NOT NULL,
    static_params TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_send_buses_order
    ON project_send_buses(order_index);

CREATE TABLE IF NOT EXISTS track_sends (
    track_id TEXT NOT NULL REFERENCES audio_tracks(id) ON DELETE CASCADE,
    bus_id TEXT NOT NULL REFERENCES project_send_buses(id) ON DELETE CASCADE,
    level REAL NOT NULL DEFAULT 0.0,
    PRIMARY KEY (track_id, bus_id)
);
CREATE INDEX IF NOT EXISTS idx_track_sends_bus
    ON track_sends(bus_id);
```

### DAL dataclasses (from `db_models.py`)

```python
@dataclass
class TrackEffect:
    id: str
    track_id: str | None           # None ⇒ master-bus effect
    effect_type: str
    order_index: int
    enabled: bool
    static_params: dict            # json-decoded
    created_at: str                # ISO-8601

@dataclass
class EffectCurve:
    id: str
    effect_id: str
    param_name: str
    points: list                   # json-decoded list of point dicts
    interpolation: str             # "bezier" | "linear" | ...
    visible: bool

@dataclass
class SendBus:
    id: str
    bus_type: str
    label: str
    order_index: int
    static_params: dict

@dataclass
class TrackSend:
    track_id: str
    bus_id: str
    level: float
```

### DAL function signatures (authoritative)

```python
# track_effects (track-scoped)
add_track_effect(project_dir, *, track_id: str, effect_type: str,
                 static_params: dict|None = None,
                 order_index: int|None = None,
                 enabled: bool = True) -> TrackEffect
get_track_effect(project_dir, effect_id: str) -> TrackEffect|None
list_track_effects(project_dir, track_id: str) -> list[TrackEffect]
update_track_effect(project_dir, effect_id: str, **fields) -> None
delete_track_effect(project_dir, effect_id: str) -> None

# master-bus (track_id IS NULL)
add_master_bus_effect(project_dir, *, effect_type: str,
                      static_params: dict|None = None,
                      order_index: int|None = None,
                      enabled: bool = True) -> TrackEffect
list_master_bus_effects(project_dir) -> list[TrackEffect]
get_master_bus_effect(project_dir, effect_id: str) -> TrackEffect|None

# effect_curves
add_effect_curve(project_dir, *, effect_id: str, param_name: str,
                 points: list|None = None,
                 interpolation: str = "bezier",
                 visible: bool = False) -> EffectCurve
get_effect_curve(project_dir, curve_id: str) -> EffectCurve|None
list_curves_for_effect(project_dir, effect_id: str) -> list[EffectCurve]
upsert_effect_curve(project_dir, *, effect_id: str, param_name: str,
                    points: list|None = None,
                    interpolation: str = "bezier",
                    visible: bool = False) -> EffectCurve
update_effect_curve(project_dir, curve_id: str, **fields) -> None
delete_effect_curve(project_dir, curve_id: str) -> None

# project_send_buses
add_send_bus(project_dir, *, bus_type: str, label: str,
             static_params: dict|None = None,
             order_index: int|None = None) -> SendBus
get_send_bus(project_dir, bus_id: str) -> SendBus|None
list_send_buses(project_dir) -> list[SendBus]
update_send_bus(project_dir, bus_id: str, **fields) -> None
delete_send_bus(project_dir, bus_id: str) -> None

# track_sends
list_track_sends(project_dir, track_id: str|None = None,
                 bus_id: str|None = None) -> list[TrackSend]
get_track_send(project_dir, track_id: str, bus_id: str) -> TrackSend|None
upsert_track_send(project_dir, *, track_id: str, bus_id: str,
                  level: float) -> TrackSend
delete_track_send(project_dir, track_id: str, bus_id: str) -> None
```

## Behavior Table

| # | Scenario | Expected Behavior | Tests |
|---|----------|-------------------|-------|
| 1 | Fresh DB bootstrap | Four default buses seeded in fixed order; zero `track_sends` rows (no tracks yet) | `default-buses-seeded-on-fresh-db` |
| 2 | Bootstrap with existing tracks, empty buses | Four buses seeded AND `track_sends` backfilled at level 0.0 for every (track, bus) pair | `bus-seed-backfills-existing-tracks` |
| 3 | Bootstrap with non-empty buses | No seeding, no backfill | `bus-seed-skipped-when-non-empty` |
| 4 | `add_track_effect` with omitted `order_index` | Assigns `max(order_index)+1` within the same `track_id` | `add-effect-defaults-order-index-per-track` |
| 5 | `add_master_bus_effect` with omitted `order_index` | Assigns `max(order_index)+1` scoped to `track_id IS NULL` | `add-master-effect-defaults-order-index-master-scope` |
| 6 | `list_track_effects(track_id)` with master-bus rows present | Returns only rows for `track_id`, excluding master-bus (`track_id IS NULL`) | `list-track-effects-excludes-master-bus` |
| 7 | `list_master_bus_effects` with track-scoped rows present | Returns only `track_id IS NULL` rows | `list-master-bus-effects-excludes-track-scoped` |
| 8 | `get_master_bus_effect` called with a track-scoped id | Returns `None` | `get-master-effect-rejects-track-scoped-id` |
| 9 | INSERT new `audio_tracks` row | Trigger inserts `(new_id, bus_id, 0.0)` for every existing bus | `new-track-autocreates-track-sends` |
| 10 | `upsert_track_send` with existing PK | Updates `level` in place; no duplicate rows | `upsert-track-send-updates-level` |
| 11 | `upsert_effect_curve` with existing `(effect_id, param_name)` | Updates `points`/`interpolation`/`visible`; row `id` unchanged | `upsert-curve-preserves-id` |
| 12 | `add_effect_curve` duplicate `(effect_id, param_name)` | Raises `sqlite3.IntegrityError` | `add-curve-raises-on-duplicate` |
| 13 | `delete_track_effect` | Row removed; attached `effect_curves` cascade-deleted; idempotent on unknown id | `delete-effect-cascades-curves`, `delete-effect-unknown-id-noop` |
| 14 | `delete_send_bus` | Bus removed; attached `track_sends` cascade-deleted | `delete-bus-cascades-sends` |
| 15 | Hard-delete `audio_tracks` row | Cascades to `track_effects`, `effect_curves` (via effects), `track_sends`; master-bus rows untouched | `delete-track-cascades-to-effects-and-sends`, `delete-track-preserves-master-bus-effects` |
| 16 | `update_track_effect` with dict for `static_params` | DAL `json.dumps`es the dict before UPDATE; read-back returns the same dict | `update-effect-coerces-dict-to-json` |
| 17 | `update_effect_curve` with list for `points` | DAL `json.dumps`es the list; read-back returns the same list | `update-curve-coerces-list-to-json` |
| 18 | `update_track_effect(static_params={"gain": 2.0})` on an effect whose existing params are `{"ratio": 4.0, "thresh": -12}` | Whole-replaces to `{"gain": 2.0}` (NOT merge-patched) | `update-effect-static-params-is-whole-replace` |
| 19 | `list_send_buses` after manual reorder via `update_send_bus(order_index=...)` | Returns rows in new ascending `order_index` order | `list-buses-respects-order-index` |
| 20 | `list_curves_for_effect` | Returns rows sorted by `param_name` ASC | `list-curves-ordered-by-param-name` |
| 21 | Legacy DB with NOT-NULL `track_id` on bootstrap | Schema rewritten to nullable; all existing rows preserved | `migration-relaxes-track-id-to-nullable` |
| 22 | `_row_to_track_effect` on well-formed row | `enabled` returned as `bool`, `static_params` as decoded `dict` | `row-mapper-hydrates-types` |
| 23 | Raw `UPDATE track_effects SET static_params = 'not json'` then DAL read | DAL mapper raises on `json.loads` | `raw-update-non-json-breaks-mapper` |
| 24 | `delete_track_effect` when curves exist for that effect | FK CASCADE removes curves; no dangling curve rows remain | `delete-effect-with-curves-cascades` |
| 25 | `effect_type` not in frontend registry | `undefined` | → [OQ-1](#open-questions) |
| 26 | `upsert_track_send` to a deleted bus id | `undefined` | → [OQ-2](#open-questions) |
| 27 | Non-JSON string written to `static_params` via raw UPDATE | DAL read raises; but behavior at HTTP/tool callers is `undefined` | → [OQ-3](#open-questions) |
| 28 | Gaps in `order_index` (e.g. 0, 2, 5 after deletes) | `undefined` (ordering still well-defined but no compaction is spec'd) | → [OQ-4](#open-questions) |
| 29 | `delete_effect_curve` on a curve whose effect was already deleted | `undefined` (curve was already cascade-deleted) | → [OQ-5](#open-questions) |

## Behavior

1. **Schema bootstrap** (`_ensure_schema` in `db.py:136`):
   1. Execute DDL block creating all four tables with indexes if not present.
   2. Run migration pass: inspect `PRAGMA table_info(track_effects)`; if
      `track_id` has `notnull=1`, perform `CREATE TABLE track_effects__new ... ;
      INSERT ... SELECT ... ; DROP TABLE track_effects; ALTER TABLE
      track_effects__new RENAME TO track_effects;` atomically.
   3. Install the `track_sends_insert_undo` / `track_sends_update_undo` /
      `track_sends_delete_undo` triggers and the
      `auto_backfill_track_sends` AFTER-INSERT trigger on `audio_tracks`.
   4. If `SELECT COUNT(*) FROM project_send_buses = 0`: call
      `_seed_default_send_buses(conn)` (inserts the four defaults) and
      backfill `track_sends` via `INSERT OR IGNORE ... CROSS JOIN`.
2. **Add track effect** (`add_track_effect`): generate id with `eff_` prefix;
   compute `order_index` if omitted; `json.dumps` `static_params or {}`;
   INSERT; commit; re-select and return the hydrated dataclass.
3. **Add master-bus effect** (`add_master_bus_effect`): identical to (2)
   but with `track_id = NULL` and the `MAX(order_index)+1` computed over
   `WHERE track_id IS NULL`.
4. **Update track effect** (`update_track_effect`): iterate `**fields`,
   coerce booleans to 0/1, coerce dict `static_params` via `json.dumps`,
   build dynamic `SET a=?, b=?`, execute UPDATE, commit. Whole-replace
   semantics for JSON columns.
5. **Delete track effect** (`delete_track_effect`): `DELETE FROM
   track_effects WHERE id = ?`; FK CASCADE drops matching `effect_curves`
   rows. Commits. Idempotent on unknown id.
6. **Upsert effect curve** (`upsert_effect_curve`): generate new id;
   `INSERT ... ON CONFLICT (effect_id, param_name) DO UPDATE SET ...`;
   commit; re-select by `(effect_id, param_name)` (NOT by generated id —
   the row's existing id wins on conflict) and return.
7. **Upsert track send** (`upsert_track_send`): `INSERT ... ON CONFLICT
   (track_id, bus_id) DO UPDATE SET level`; return hydrated dataclass.
8. **Delete send bus** (`delete_send_bus`): `DELETE FROM project_send_buses
   WHERE id = ?`; FK CASCADE drops matching `track_sends` rows.
9. **Row mapping** (`_row_to_track_effect`, `_row_to_effect_curve`,
   `_row_to_send_bus`, `_row_to_track_send`): `json.loads` the blob
   columns (empty → `{}`/`[]`), cast integer booleans to Python `bool`,
   cast `level` to `float`.

## Acceptance Criteria

- [ ] All four CREATE TABLE statements present with exactly the columns,
      types, nullability, defaults, and FK actions specified in R1/R4/R5/R6.
- [ ] Indexes `track_effects_track_order`, `idx_effect_curves_effect`,
      `idx_send_buses_order`, `idx_track_sends_bus` present.
- [ ] Four default buses seeded in order on fresh DB (R7).
- [ ] `track_sends` backfill on seed populates every `(track, bus)` pair at
      level 0.0 via `INSERT OR IGNORE` (R8).
- [ ] AFTER-INSERT trigger on `audio_tracks` creates zero-level sends for
      every existing bus (R9).
- [ ] `add_track_effect` default `order_index` is per-`track_id` max+1 (R10).
- [ ] `add_master_bus_effect` default `order_index` is per-master-scope
      max+1, disjoint from track-scoped numbering (R11).
- [ ] Legacy NOT-NULL `track_id` migrated to nullable on bootstrap; rows
      preserved (R3).
- [ ] `list_track_effects(t)` excludes master-bus rows; `list_master_bus_effects()`
      excludes track-scoped rows; `get_master_bus_effect(id)` returns `None`
      when the row is track-scoped (R2).
- [ ] `upsert_effect_curve` preserves the row id on update (R13).
- [ ] `add_effect_curve` duplicate raises `IntegrityError` (R13).
- [ ] `delete_track_effect` cascades to `effect_curves` (R12).
- [ ] `delete_send_bus` cascades to `track_sends` (R15).
- [ ] Hard-delete of `audio_tracks` cascades to `track_effects`,
      `effect_curves`, and `track_sends`; master-bus rows survive (R21/R22).
- [ ] DAL `update_*` helpers coerce bool → 0/1 and dict/list → JSON
      string (R16).
- [ ] DAL `update_*` helpers whole-replace JSON blobs (R19) — no merge
      semantics on `static_params`/`points`.
- [ ] Row mappers hydrate `enabled`/`visible` to `bool` and JSON blobs to
      Python objects (R17).
- [ ] `list_*` orderings as specified in R18.
- [ ] All four tables present in `_undo_tracked_tables`.

## Tests

### Base Cases

#### Test: default-buses-seeded-on-fresh-db (covers R7)

**Given**: a fresh project directory with no `project.db`.
**When**: `_ensure_schema` runs (first DAL call against the project).
**Then** (assertions):
- **bus-count-4**: `list_send_buses(project_dir)` returns exactly 4 rows.
- **bus-order**: Their `(bus_type, label)` tuples in `order_index` order are
  `[("reverb","Plate"), ("reverb","Hall"), ("delay","Delay"), ("echo","Echo")]`.
- **bus-static-params**: Each bus's `static_params` dict equals the R7
  payload for its index.

#### Test: bus-seed-backfills-existing-tracks (covers R8)

**Given**:
- Fresh DB.
- Two `audio_tracks` rows inserted BEFORE the bus-seeding path runs (simulating
  a migrated pre-M13 project).

**When**: schema bootstrap runs (triggering the seed + backfill path).

**Then** (assertions):
- **sends-count-8**: `list_track_sends()` returns `2 × 4 = 8` rows.
- **sends-level-zero**: Every returned row's `level` equals `0.0`.
- **every-pair-present**: For every `(track_id, bus_id)` in the cartesian
  product, exactly one row exists.

#### Test: bus-seed-skipped-when-non-empty (covers R7)

**Given**: a DB where `project_send_buses` already has 1 custom row.
**When**: schema bootstrap runs again.
**Then** (assertions):
- **no-new-buses**: `list_send_buses()` still returns exactly 1 row.
- **no-track-sends-created**: `list_track_sends()` is unchanged.

#### Test: add-effect-defaults-order-index-per-track (covers R10)

**Given**: track `tA` has one existing `track_effects` row with
`order_index = 0`; track `tB` has no effects.
**When**:
- `add_track_effect(track_id="tA", effect_type="gain")` is called.
- `add_track_effect(track_id="tB", effect_type="gain")` is called.
**Then** (assertions):
- **tA-order-1**: The new row on `tA` has `order_index = 1`.
- **tB-order-0**: The new row on `tB` has `order_index = 0`.

#### Test: add-master-effect-defaults-order-index-master-scope (covers R11)

**Given**:
- Track `tA` has 3 effects at `order_index = 0,1,2`.
- Master bus has 1 effect at `order_index = 0`.

**When**: `add_master_bus_effect(effect_type="limiter")` is called.

**Then** (assertions):
- **master-order-1**: The new master-bus row has `order_index = 1`
  (not `3` — it does NOT pool with track-scoped numbering).

#### Test: list-track-effects-excludes-master-bus (covers R2)

**Given**: Track `tA` has 2 track-scoped effects; master bus has 1 effect.
**When**: `list_track_effects(tA)` is called.
**Then** (assertions):
- **count-2**: Returns exactly 2 rows.
- **all-track-scoped**: Every row's `track_id == "tA"`.
- **no-master-leak**: No row has `track_id is None`.

#### Test: list-master-bus-effects-excludes-track-scoped (covers R2)

**Given**: same setup as above.
**When**: `list_master_bus_effects()` is called.
**Then** (assertions):
- **count-1**: Returns exactly 1 row.
- **master-only**: That row's `track_id is None`.

#### Test: get-master-effect-rejects-track-scoped-id (covers R2)

**Given**: A track-scoped effect with id `eff_abc`.
**When**: `get_master_bus_effect(project_dir, "eff_abc")` is called.
**Then** (assertions):
- **returns-none**: Returns `None`.
- **no-exception**: Does not raise.

#### Test: new-track-autocreates-track-sends (covers R9)

**Given**: 4 default buses present; zero tracks.
**When**: A new `audio_tracks` row `tX` is inserted via the raw SQL used
by track DAL.
**Then** (assertions):
- **sends-count-4**: `list_track_sends(track_id="tX")` returns 4 rows.
- **one-per-bus**: Each row matches a distinct `bus_id`.
- **level-zero**: Every row's `level` is `0.0`.

#### Test: upsert-track-send-updates-level (covers R14)

**Given**: `(tX, busA, 0.3)` exists.
**When**: `upsert_track_send(track_id="tX", bus_id="busA", level=0.7)` is called.
**Then** (assertions):
- **single-row**: Only one row for `(tX, busA)` exists.
- **level-0.7**: Its `level` is `0.7`.
- **return-hydrated**: The returned `TrackSend` dataclass has the new level.

#### Test: upsert-curve-preserves-id (covers R13)

**Given**: `upsert_effect_curve(effect_id="e1", param_name="gain", points=[{...}])`
has created a row with id `curve_initial`.
**When**: `upsert_effect_curve(effect_id="e1", param_name="gain",
points=[{new}], interpolation="linear", visible=True)` is called again.
**Then** (assertions):
- **row-count-1**: Only one row exists for `(e1, "gain")`.
- **id-unchanged**: Its id is still `curve_initial`.
- **points-updated**: `points` reflects the new list.
- **interp-linear**: `interpolation == "linear"`.
- **visible-true**: `visible is True`.

#### Test: add-curve-raises-on-duplicate (covers R13)

**Given**: A row `(e1, "gain")` already exists in `effect_curves`.
**When**: `add_effect_curve(effect_id="e1", param_name="gain")` is called.
**Then** (assertions):
- **integrity-error**: Raises `sqlite3.IntegrityError`.
- **row-count-unchanged**: The original row still exists and is unmodified.

#### Test: delete-effect-cascades-curves (covers R12)

**Given**: Effect `e1` with two curves `(e1, "gain")` and `(e1, "ratio")`.
**When**: `delete_track_effect(project_dir, "e1")`.
**Then** (assertions):
- **effect-gone**: `get_track_effect("e1") is None`.
- **curves-gone**: `list_curves_for_effect("e1")` returns `[]`.

#### Test: delete-effect-unknown-id-noop (covers R12)

**Given**: `track_effects` does not contain any row with id `eff_does_not_exist`.
**When**: `delete_track_effect(project_dir, "eff_does_not_exist")`.
**Then** (assertions):
- **no-exception**: Does not raise.
- **no-row-count-change**: The count of `track_effects` rows is unchanged.

#### Test: delete-bus-cascades-sends (covers R15)

**Given**: Bus `busA` with 3 `track_sends` rows attached.
**When**: `delete_send_bus(project_dir, "busA")`.
**Then** (assertions):
- **bus-gone**: `get_send_bus("busA") is None`.
- **sends-gone**: `list_track_sends(bus_id="busA") == []`.

#### Test: delete-track-cascades-to-effects-and-sends (covers R21)

**Given**:
- Track `tA` with 2 track-effects (each with 1 curve) and 4 track-sends
  (one per default bus).

**When**: Raw `DELETE FROM audio_tracks WHERE id = 'tA'` runs with FK
pragma enabled.

**Then** (assertions):
- **effects-gone**: `list_track_effects("tA") == []`.
- **curves-gone**: `list_curves_for_effect(<either effect id>) == []`.
- **sends-gone**: `list_track_sends(track_id="tA") == []`.

#### Test: delete-track-preserves-master-bus-effects (covers R22)

**Given**:
- Track `tA` with 1 effect (and 1 curve).
- Master bus with 1 effect (and 1 curve).

**When**: Track `tA` is hard-deleted (as above).

**Then** (assertions):
- **master-effect-survives**: `list_master_bus_effects()` still returns
  the master-bus effect.
- **master-curve-survives**: `list_curves_for_effect(<master effect id>)`
  still returns its curve.

#### Test: update-effect-coerces-dict-to-json (covers R16)

**Given**: Existing effect `e1`.
**When**: `update_track_effect("e1", static_params={"gain": 2.0})`.
**Then** (assertions):
- **round-trip-dict**: `get_track_effect("e1").static_params == {"gain": 2.0}`.
- **raw-row-is-json-string**: A direct `SELECT static_params FROM track_effects
  WHERE id='e1'` returns the string `'{"gain": 2.0}'`.

#### Test: update-curve-coerces-list-to-json (covers R16)

**Given**: Existing curve `c1`.
**When**: `update_effect_curve("c1", points=[{"t": 0.0, "v": 0.0}])`.
**Then** (assertions):
- **round-trip-list**: `get_effect_curve("c1").points ==
  [{"t": 0.0, "v": 0.0}]`.
- **raw-row-is-json-string**: Direct SELECT returns the JSON-string form.

#### Test: update-effect-static-params-is-whole-replace (covers R19)

**Given**: Effect `e1` with `static_params = {"ratio": 4.0, "thresh": -12}`.
**When**: `update_track_effect("e1", static_params={"gain": 2.0})`.
**Then** (assertions):
- **ratio-gone**: `get_track_effect("e1").static_params` does NOT contain `"ratio"`.
- **thresh-gone**: Same dict does NOT contain `"thresh"`.
- **only-gain**: `get_track_effect("e1").static_params == {"gain": 2.0}`.

#### Test: list-buses-respects-order-index (covers R18)

**Given**: Four default buses exist (order 0..3).
**When**: `update_send_bus(bus3.id, order_index=0)` then `list_send_buses()`.
**Then** (assertions):
- **reordered**: The first returned bus's id is `bus3.id`.
- **stable-asc**: Remaining rows are in ascending `order_index` order.

#### Test: list-curves-ordered-by-param-name (covers R18)

**Given**: Effect `e1` with curves for `param_name` in `["zeta","alpha","mu"]`.
**When**: `list_curves_for_effect("e1")`.
**Then** (assertions):
- **order-alpha-mu-zeta**: Returned `param_name` sequence is `["alpha","mu","zeta"]`.

#### Test: migration-relaxes-track-id-to-nullable (covers R3)

**Given**: A DB created with the legacy DDL where `track_effects.track_id`
is `NOT NULL` and contains two rows with non-null `track_id`.
**When**: `_ensure_schema` is invoked.
**Then** (assertions):
- **track-id-nullable**: `PRAGMA table_info(track_effects)` reports
  `notnull = 0` for the `track_id` column.
- **rows-preserved**: Both original rows still exist with identical
  `id`, `track_id`, `effect_type`, `order_index`, `enabled`,
  `static_params`, `created_at`.

#### Test: row-mapper-hydrates-types (covers R17)

**Given**: A raw `track_effects` row with `enabled = 1` and
`static_params = '{"a": 1}'`.
**When**: `get_track_effect(id)` is called.
**Then** (assertions):
- **enabled-is-bool**: Returned `.enabled` is `True` (Python `bool`, not `int`).
- **static-params-is-dict**: Returned `.static_params == {"a": 1}` as `dict`.

### Edge Cases

#### Test: raw-update-non-json-breaks-mapper (covers R20)

**Given**: Effect `e1` exists with valid JSON params.
**When**:
- A raw `conn.execute("UPDATE track_effects SET static_params = 'not-json' WHERE id='e1'")` is run.
- `get_track_effect("e1")` is invoked.
**Then** (assertions):
- **mapper-raises**: `json.decoder.JSONDecodeError` propagates from the
  mapper.
- **dal-bypassed-successfully**: The SQL UPDATE itself succeeded
  (demonstrating the known DAL-bypass leak).

#### Test: delete-effect-with-curves-cascades (covers R12, R21)

**Given**: Effect `e1` has 3 curves; effect `e2` on the same track has 1 curve.
**When**: `delete_track_effect("e1")`.
**Then** (assertions):
- **e1-curves-gone**: `list_curves_for_effect("e1") == []`.
- **e2-curves-intact**: `list_curves_for_effect("e2")` still has 1 row.

#### Test: upsert-track-send-fresh-insert (covers R14)

**Given**: No existing row for `(tX, busA)`.
**When**: `upsert_track_send(track_id="tX", bus_id="busA", level=0.5)`.
**Then** (assertions):
- **row-created**: `get_track_send("tX","busA").level == 0.5`.
- **return-matches**: The return value equals the freshly-read row.

#### Test: list-track-sends-filter-combinations (covers R18)

**Given**: 2 tracks × 4 buses, all sends present.
**When**:
- `list_track_sends()` — no filter.
- `list_track_sends(track_id="tA")`.
- `list_track_sends(bus_id="busA")`.
- `list_track_sends(track_id="tA", bus_id="busA")`.
**Then** (assertions):
- **no-filter-count-8**: Unfiltered returns 8 rows.
- **track-filter-count-4**: Track-filtered returns 4.
- **bus-filter-count-2**: Bus-filtered returns 2.
- **both-filter-count-1**: Both-filtered returns 1.
- **order-stable**: Results are ordered by `(track_id, bus_id)` ASC.

#### Test: add-effect-empty-static-params (covers R1)

**Given**: No effect exists on track `tA`.
**When**: `add_track_effect(track_id="tA", effect_type="gain")` with no
`static_params`.
**Then** (assertions):
- **stored-empty-obj**: Raw row's `static_params` column is the string `'{}'`.
- **hydrated-empty-dict**: `get_track_effect(id).static_params == {}`.

#### Test: update-noop-when-no-fields (covers R16)

**Given**: Effect `e1` exists.
**When**: `update_track_effect("e1")` with no kwargs.
**Then** (assertions):
- **no-sql-executed**: Row's `created_at` is unchanged (no UPDATE touched it).
- **no-exception**: Returns `None` cleanly.

#### Test: single-threaded-dal-no-implicit-concurrency (covers R10, R11, R14)

**Given**: The DAL is called from a single thread per project/thread-id key.
**When**: Two sequential `add_track_effect` calls race only if tests
introduce threads (they do not).
**Then** (assertions):
- **order-indexes-monotonic**: Sequential calls produce `order_index`
  values `n`, `n+1` with no gaps or duplicates.
- **note**: This negative assertion is deliberate — the DAL does NOT
  provide locking for concurrent `add_*` calls on the same `track_id`.
  Concurrent writers can produce duplicate `order_index` values; that
  class of bug is out of scope here and belongs to
  `engine-rest-api-dispatcher` (structural lock coverage).

#### Test: delete-send-bus-unknown-id-noop

**Given**: No bus with id `bus_missing`.
**When**: `delete_send_bus(project_dir, "bus_missing")`.
**Then** (assertions):
- **no-exception**: Returns cleanly.
- **bus-count-unchanged**: `list_send_buses()` count is unchanged.

#### Test: delete-track-send-unknown-pk-noop

**Given**: No row for `(tMissing, bMissing)`.
**When**: `delete_track_send(project_dir, "tMissing", "bMissing")`.
**Then** (assertions):
- **no-exception**: Returns cleanly.
- **count-unchanged**: `list_track_sends()` count is unchanged.

#### Test: update-bus-static-params-whole-replace (covers R19)

**Given**: Bus with `static_params = {"ir": "plate.wav"}`.
**When**: `update_send_bus(bus.id, static_params={"ir": "hall.wav", "wet": 0.5})`.
**Then** (assertions):
- **whole-replace**: `get_send_bus(bus.id).static_params ==
  {"ir": "hall.wav", "wet": 0.5}`.

#### Test: upsert-curve-conflict-ignores-new-generated-id (covers R13)

**Given**: `(e1, "gain")` exists with id `curve_A`.
**When**: `upsert_effect_curve(effect_id="e1", param_name="gain", ...)`
is called (which generates a new candidate id `curve_B` internally).
**Then** (assertions):
- **id-stays-A**: Post-upsert, the row's id is still `curve_A`.
- **no-curve-B**: No row with id `curve_B` exists.

#### Test: fk-cascade-requires-foreign-keys-pragma (covers R21)

**Given**: A connection where `PRAGMA foreign_keys` is OFF.
**When**: A track is deleted.
**Then** (assertions):
- **no-cascade**: Child rows (effects/sends) remain — documenting that
  FK enforcement depends on the pragma being ON in `get_db()`.
- **note**: Asserts the production default (`get_db` sets `PRAGMA
  foreign_keys = ON`) in a separate positive test.

## Non-Goals

- Partial / merge-patch updates of `static_params` or `points`. If needed
  later, add a new helper (`patch_effect_static_params`) rather than
  overloading `update_track_effect`; this matches the
  `local.light_show__scenes` pattern but is intentionally absent here.
- Automatic `order_index` compaction after deletes. Gaps are allowed and
  do not affect correctness of `ORDER BY order_index`.
- Validation of `effect_type` against a registry. That lives in the HTTP
  layer and in the frontend effect-registry spec.
- Validation of `bus_type` against an allowlist. Any string is accepted.
- Validation of `level` range (`0.0..1.0+` is the convention but not enforced).
- Validation of `points` JSON structure; schema is enforced on read in
  the frontend scheduler.
- A `schema_migrations` version table. This is a global migrations gap
  (audit-2 §3 #16) and belongs to `engine-migrations-framework`.
- Spending / usage metrics; none of these tables participate in spend.
- Multi-project semantics. Every function takes a `project_dir` and
  scopes to that DB exclusively.

## Open Questions

- **OQ-1 — effect_type not in frontend registry**: What is the engine's
  responsibility when a row is written with `effect_type = "does_not_exist"`?
  Today the DAL silently accepts it (no registry is reachable from the
  engine). Options:
    a) Accept silently; frontend skips unknown types at build time (current behavior).
    b) Validate at HTTP boundary against an effect-type catalog served from the frontend spec (would require importing the registry into Python or serving it).
    c) Add a `known_effect_types` column-check trigger sourced from a manifest.
  No decision on file; marked `undefined` in Behavior Table #25.

- **OQ-2 — `upsert_track_send` to a deleted bus id**: FK enforcement
  will raise `IntegrityError` IF `PRAGMA foreign_keys = ON`. If OFF
  (legacy connections), a dangling send row is created. Should the
  DAL perform an explicit existence check, or rely on the pragma? Also:
  what if a bus is deleted concurrently with an upsert? Marked `undefined`
  in Behavior Table #26.

- **OQ-3 — non-JSON in `static_params`**: The DAL coerces dicts via
  `json.dumps`, but raw callers (tests, chat tools, SQL console) can
  write arbitrary strings. The mapper raises on read. Should the DAL
  add a `CHECK (json_valid(static_params))` constraint, or is the raw
  leak acceptable (paired with a negative test asserting the failure
  mode)? Marked `undefined` in Behavior Table #27.

- **OQ-4 — `order_index` gaps**: After delete, `order_index` values may
  be non-contiguous (e.g., `0, 2, 3`). Frontend ordering is still
  well-defined (sort-by-asc). Should the DAL auto-compact on delete?
  Should a `reorder_track_effects([ids...])` helper exist? Marked
  `undefined` in Behavior Table #28.

- **OQ-5 — `delete_effect_curve` after effect-delete**: If the parent
  effect was already deleted, the curve was already cascade-deleted.
  `delete_effect_curve(stale_id)` is currently a silent no-op. Is that
  desirable, or should it raise to help callers detect stale references?
  Marked `undefined` in Behavior Table #29.

## Related Artifacts

- **Source code**: `scenecraft-engine/src/scenecraft/db.py`
  (DDL `:695–736`, seed `:104–133`, migration `:1164–1194`, trigger
  `:1300–1325`, DAL `:3656–4091`).
- **Dataclasses**: `scenecraft-engine/src/scenecraft/db_models.py`
  (`TrackEffect`, `EffectCurve`, `SendBus`, `TrackSend`).
- **Audit**: `scenecraft-engine/agent/reports/audit-2-architectural-deep-dive.md`
  §1C (DB schema + DAL + migrations), §3 boundary leaks #15 (raw-UPDATE
  bypass), #16 (no schema_migrations), #17 (nullable migration not
  validated).
- **Cross-reference (frontend)**:
  `scenecraft/agent/specs/local.audio-effects-and-curve-scheduling.md`
  (effect-type registry, WebAudio node graph, curve scheduling).
- **Related engine specs (planned)**:
  - `engine-db-schema-core-entities` — defines `audio_tracks` whose FK
    this spec depends on.
  - `engine-db-undo-redo` — owns the `undo_log`/`redo_log` triggers
    referenced here.
  - `engine-migrations-framework` — owns the column-existence migration
    pattern used by the NOT-NULL→nullable rewrite in R3.
  - `engine-rest-api-dispatcher` — owns the HTTP surface that calls this
    DAL and is the correct home for effect_type registry validation,
    concurrency locking, and raw-SQL gating.

---

**Spec name**: local.engine-db-effects-and-curves
**Namespace**: local
**Version**: 1.0.0
**Created**: 2026-04-27
**Last Updated**: 2026-04-27
**Status**: Draft
