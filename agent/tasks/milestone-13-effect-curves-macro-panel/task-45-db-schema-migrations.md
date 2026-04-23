# Task 45: DB schema + migration for effect curves tables

**Milestone**: [M13 - Effect Curves + Macro Panel + Touch-Record](../../milestones/milestone-13-effect-curves-macro-panel.md)
**Spec**: [local.effect-curves-macro-panel](../../specs/local.effect-curves-macro-panel.md) — R1-R6, R51, R52
**Estimated Time**: 2 hours
**Dependencies**: None
**Status**: Not Started
**Repository**: `scenecraft-engine`

---

## Objective

Add 5 new SQLite tables + one migration that persists effect chains, curves, send buses, per-track sends, and custom frequency labels.

---

## Steps

### 1. Write migration

Create `src/scenecraft/db/migrations/00NN_effect_curves.py` (next available migration number). Exactly the schema from spec §Implementation:

```sql
CREATE TABLE track_effects (
    id TEXT PRIMARY KEY,
    track_id TEXT NOT NULL REFERENCES audio_tracks(id) ON DELETE CASCADE,
    effect_type TEXT NOT NULL,
    order_index INTEGER NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    static_params TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX track_effects_track_order ON track_effects(track_id, order_index);

CREATE TABLE effect_curves (
    id TEXT PRIMARY KEY,
    effect_id TEXT NOT NULL REFERENCES track_effects(id) ON DELETE CASCADE,
    param_name TEXT NOT NULL,
    points TEXT NOT NULL,
    interpolation TEXT NOT NULL DEFAULT 'bezier',
    visible INTEGER NOT NULL DEFAULT 0,
    UNIQUE(effect_id, param_name)
);

CREATE TABLE project_send_buses (
    id TEXT PRIMARY KEY,
    bus_type TEXT NOT NULL,
    label TEXT NOT NULL,
    order_index INTEGER NOT NULL,
    static_params TEXT NOT NULL
);

CREATE TABLE track_sends (
    track_id TEXT NOT NULL REFERENCES audio_tracks(id) ON DELETE CASCADE,
    bus_id TEXT NOT NULL REFERENCES project_send_buses(id) ON DELETE CASCADE,
    level REAL NOT NULL DEFAULT 0.0,
    PRIMARY KEY (track_id, bus_id)
);

CREATE TABLE project_frequency_labels (
    id TEXT PRIMARY KEY,
    label TEXT NOT NULL,
    freq_min_hz REAL NOT NULL,
    freq_max_hz REAL NOT NULL
);
```

### 2. Default bus seeding

On migration apply for existing projects AND on new-project creation: seed `project_send_buses` with defaults:
- `Plate` (reverb) order 0 — uses `plate.wav` IR
- `Hall` (reverb) order 1 — uses `hall.wav` IR
- `Delay` order 2 — default 1/4-note time, 0.35 feedback
- `Echo` order 3 — default 120ms time, 0 feedback

### 3. Track sends seeding

On audio-track INSERT, automatically insert `track_sends` rows for every existing bus with `level=0`. Enforce via either an SQL trigger or a Python-side hook in the insert handler.

### 4. Python ORM types

Add dataclasses / TypedDicts in `src/scenecraft/db/models.py`:
- `TrackEffect`, `EffectCurve`, `SendBus`, `TrackSend`, `FrequencyLabel`

Mirror the spec's TypeScript types exactly — field names identical.

### 5. Query helpers

Thin functions in `src/scenecraft/db/effect_curves.py`:
- `list_track_effects(project_dir, track_id) -> list[TrackEffect]`
- `get_effect(project_dir, effect_id) -> TrackEffect | None`
- `upsert_effect_curve(project_dir, effect_id, param_name, points, interpolation, visible) -> EffectCurve`
- `list_curves_for_effect(project_dir, effect_id) -> list[EffectCurve]`
- `list_buses(project_dir) -> list[SendBus]`
- `upsert_track_send(project_dir, track_id, bus_id, level)`
- etc.

### 6. Tests

`tests/test_db_effect_curves.py`:
- Migration applies cleanly on a fresh DB
- Migration applies cleanly on a DB with existing audio_tracks (seeds track_sends for them)
- CASCADE DELETE on `audio_tracks` clears `track_effects` + `effect_curves`
- CASCADE DELETE on `track_effects` clears `effect_curves`
- Default bus seeding produces the 4 expected rows
- Unique constraint on `(effect_id, param_name)` enforced

---

## Verification

- [ ] Migration runs on a fresh project; all 5 tables exist
- [ ] Migration runs on an existing project with audio tracks; cascades populate correctly
- [ ] Python ORM types compile + match spec field names
- [ ] Query helpers return expected shapes
- [ ] Cascade deletes work in both directions tested
- [ ] Default buses seeded with expected IDs and `order_index`
- [ ] Tests pass: `pytest tests/test_db_effect_curves.py`

---

## Notes

- Seeded IR assets will land in T47; this task just sets `static_params.ir = 'plate.wav'` by reference, not by actually copying the asset (that happens in T47).
- Use the existing migration pattern from prior SQLite migrations in the repo.
- Keep `points` JSON-serialized text; validation happens in application code.
