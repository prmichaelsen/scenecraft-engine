# Spec: Effect Curves + Macro Panel + Touch-Record

**Namespace**: local
**Version**: 1.0.1
**Created**: 2026-04-23
**Last Updated**: 2026-04-23
**Status**: Ready for Implementation

---

**Purpose**: Implementation-ready contract for M13's audio mixing pipeline: per-track effect chains, project-scoped send buses, automatable parameters, a Macro Panel of arm-able knobs, touch-record over live playback, and inline timeline curve editing.
**Source**: `--from-design agent/design/local.effect-curves-macro-panel.md`

---

## Scope

### Cross-Repo Split

This spec is the **cross-repo contract**. Implementation crosses two repos:

- **scenecraft-engine** (this repo) owns: SQLite schema + migrations (R1-R5, R52), HTTP endpoints (R51, Interfaces), POST validation (R_V1 below), cascade delete semantics.
- **scenecraft** (frontend repo) owns: WebAudio graph construction (R7, R11-R19), Macro Panel UI (R28-R34), InlineCurveEditor (R37-R42), touch-record state machine (R20-R27), copy-paste automation (R43-R47), frequency-label preset data file (R48-R49 client-side constants).

Both sides must adhere to the same Requirements + Tests contract. UI-structure requirements (R28-R34, R41, R42) are primarily verified on the frontend side; persistence + HTTP requirements are primarily verified on the engine side.

### In-Scope

- Audio effect chains per track (ordered, enable/disable, add/remove/reorder)
- 17 effect types across 6 categories (Dynamics, EQ, Spatial, Time-based, Modulation, Distortion)
- Project-scoped send buses (Reverb, Delay, Echo), user-configurable count
- Per-`(effect, param)` curve automation using existing `CurvePoint` format
- Macro Panel UI with grid + list layouts, per-knob arm/enable/visible controls
- Touch-record: gesture-only during playback, direct edit when paused
- Inline timeline curve editing (shared `<InlineCurveEditor>`)
- Multi-select + `trackDelta` copy/paste of automation keyframes (P2 inside M13)
- Frequency label presets (spectrum + instrument sets) + project-scoped custom labels
- New SQLite tables: `track_effects`, `effect_curves`, `project_send_buses`, `track_sends`, `project_frequency_labels`
- Undo integration: one undo unit per record pass or static knob change
- Ship ~6 IR files for ConvolverNode reverb

### Out-of-Scope (Non-Goals)

- Pitch-shift / time-stretch on tracks or clips beyond simple detune (±100 cents) via external lib on short clips
- LFO / modulation routing on top of curves
- VST / external plugin support (permanent non-goal)
- MIDI controller binding (P5, future milestone)
- Character crossfade on Distortion (users layer instead)
- Algorithmic reverb (rejected in favor of ConvolverNode)
- Global cross-project frequency labels (P3, future)
- User-configurable curve colors in preferences (P4, future)
- Automating static params (IR choice, character selector, bus_id, LFO rate) — static per instance
- Interactive Tutorial Panel (separate spike)
- Per-clip effect chains (per-track only)
- Unifying volume curves with the new system
- Choice of pitch-shift library (`soundtouch-js` / `phaze` / custom) — decided when the feature ships, not in M13
- Tutorial auto-advance detection (lives with the separate Tutorial Panel spike)

---

## Requirements

### Data Model

- **R1**: SQLite `track_effects` table stores ordered effect chain per audio track with columns `id`, `track_id`, `effect_type`, `order_index`, `enabled`, `static_params`, `created_at`.
- **R2**: SQLite `effect_curves` table stores one row per `(effect_id, param_name)`, with JSON `points` array of `[time_seconds, value_normalized_0_to_1]`, `interpolation` enum, `visible` flag. A `UNIQUE(effect_id, param_name)` constraint prevents duplicate curves for the same param; attempts to insert a second curve on the same pair MUST fail at the SQL layer before reaching application code.
- **R3**: SQLite `project_send_buses` table stores the per-project bus list with `id`, `bus_type`, `label`, `order_index`, `static_params`.
- **R4**: SQLite `track_sends` table stores per-track send levels with `(track_id, bus_id)` primary key.
- **R5**: SQLite `project_frequency_labels` table stores per-project custom EQ labels: `id`, `label`, `freq_min_hz`, `freq_max_hz`. The table lives in the per-project `project.db` (NOT `server.db`); project-scope is enforced by DB location, not an explicit FK. Deleting or relocating the project's `.scenecraft/` directory discards its custom labels by construction.
- **R6**: Curve point values are stored normalized to `[0, 1]`; conversion to native units (Hz, dB, ratio, etc.) is computed at runtime via per-param mappers (linear/log/db/hz scales).

### Effect Registry

- **R7**: A single source-of-truth `EffectTypeSpec` registry lists all 15 v1 effect types, each with: `type`, `label`, `category`, `params[]` (with `animatable: bool`, `range`, `scale`, `default`, optional `labelPresets`), and a `build(ctx, staticParams) -> EffectNode` factory returning `{input, output, setParam, dispose}`.
- **R8**: Supported effect types in v1: `compressor`, `gate`, `limiter`, `eq_band`, `highpass`, `lowpass`, `pan`, `stereo_width`, `reverb_send`, `delay_send`, `echo_send`, `tremolo`, `auto_pan`, `chorus`, `flanger`, `phaser`, `drive`.
- **R9**: Every effect's animatable params are ALL of its user-facing params except: `character` (on `drive`), `bus_id` (on sends), `rate` (on `tremolo`/`auto_pan`/`chorus`/`flanger`/`phaser`), and IR-choice (on reverb buses). Attempts to create an `effect_curves` row for a non-animatable param MUST fail with HTTP 400 and a clear error naming the param. Animatability is a property of `EffectParamSpec.animatable`; the server consults the registry to validate POSTs.
- **R10**: `eq_band` provides a `labelPresets` list drawn from spectrum-band set (8 entries) + instrument-preset set (11 entries), each with `{label, value_hz, hzRange?}`.

- **R8a** (synthetic effect_type for send animation): `__send` is a reserved internal `effect_type` string used ONLY to animate per-bus send levels via the `effect_curves` table. It is NOT registered in the `EffectTypeSpec` registry (R7), has NO `build()` factory, and MUST be rejected by the POST `/track-effects` endpoint (only real effect types are instantiable). Send-level curves reference `__send` as their `effect_type` with `param_name = bus_id`; the mixer has a special path for these curves that binds them to the `track → bus` GainNode instead of an EffectNode. Any other use of `__send` is a defect.

- **R_V1** (POST validation — engine-side): Endpoints MUST validate inputs before writing:
  - `POST /track-effects` rejects with HTTP 400 if `effect_type` is not in R8's enumerated list (R8a's `__send` is rejected here explicitly).
  - `POST /effect-curves` rejects with HTTP 404 if the referenced `effect_id` does not exist.
  - `POST /effect-curves/:id` rejects with HTTP 404 if `:id` does not exist.
  - `POST /track-sends` rejects with HTTP 404 if `track_id` or `bus_id` does not exist.
  - `POST /track-effects/:id` with an `order_index` colliding with an existing sibling effect MUST atomically swap (server assigns a conflict-free layout in one transaction); it MUST NOT leave two effects sharing an order_index.
  - DELETE of a non-existent `track_effects` row is an idempotent no-op (HTTP 200 with empty body), not a 404.

### Audio Graph

- **R11**: Each audio track's runtime graph is `clip_source → volume_gain (existing) → effect_1 → effect_2 → … → pan → track_gain → send_taps → destination`, where `send_taps` are parallel `GainNode`s routing to each project send bus.
- **R12**: Project send buses are built once per project load; each bus owns a single effect instance (Reverb=ConvolverNode + IR, Delay=DelayNode+feedback, Echo=DelayNode+single-tap+tone) whose static params come from `project_send_buses.static_params`.
- **R13**: Send levels animate a `GainNode` on each `track → bus` path; they are exposed as curves via a synthetic effect_type `__send` with `param_name` = `bus_id`.
- **R14**: Effect chain is rebuilt when `track_effects` rows change (add/remove/reorder/enable-toggle). Param-only changes do NOT rebuild the chain; they re-schedule the affected `AudioParam` only.
- **R15**: Disabled effects (`enabled=0`) are bypassed in the audio graph: `effect.input` connects directly to `effect.output` at the same position. Re-enabling reconnects the inner chain without rebuilding neighbors.

### Curve → AudioParam Scheduling

- **R16**: On graph build or curve change, each curve schedules its points via `param.cancelScheduledValues(0)` followed by `param.setValueCurveAtTime(Float32Array, startTime, duration)` (or per-point `setValueAtTime` + `setTargetAtTime` for bezier).
- **R17**: Curve values are converted from normalized `[0, 1]` to the AudioParam's native unit using the param's `scale` mapper before scheduling.
- **R18**: When the playhead seeks, active curves reschedule from the seek position forward. Points before the seek position are not scheduled.
- **R19**: Interpolation `bezier` produces smooth curves via `setValueCurveAtTime` with densely-sampled values; `linear` uses `linearRampToValueAtTime`; `step` uses `setValueAtTime` only.

### Touch-Record State Machine

- **R20**: Per-knob state: `idle → armed → recording → armed` (no auto-disarm after commit); only `armed` knobs can enter `recording`.
- **R21**: Transition to `recording` requires: knob is `armed` AND global playback state is `playing` AND user has mousedown'd the knob.
- **R22**: Transition back to `armed` (commit): user mouseup on the knob OR global playback stops OR the component unmounts.
- **R23**: During `recording`, knob value is sampled at ≥30 Hz (target 33 Hz, once per `requestAnimationFrame` frame throttled to ~30ms).
- **R24**: On commit: the raw sample buffer is simplified by bezier-fitting successive 6-point windows, dropping control points whose removal changes fit error by less than 2% of knob range. Simplified points replace any pre-existing curve points inside `[gesture_start_t, gesture_end_t]`.
- **R25**: Each commit pushes exactly ONE undo unit to the scenecraft undo stack. Each static (non-recorded) knob change also pushes one undo unit.
- **R26**: Knob adjustment while playback is NOT playing directly edits the curve at the current playhead time (equivalent to dragging a keyframe in the inline editor). No recording state machine transition.
- **R27**: Touch mode is the ONLY record mode in v1. There is no Latch or Write mode toggle.

### Macro Panel UI

- **R28**: A new `MacroPanel.tsx` component is registered in `EditorPanelLayout.tsx` using the existing `PanelLayout` system.
- **R29**: The panel reads `selectedAudioTrackId` from `EditorStateContext`; when a track is selected, it renders one `EffectGroup` per row of the track's `track_effects`, ordered by `order_index`.
- **R30**: The panel has a view-mode toggle (grid ↔ list). Grid mode renders knob tiles in a responsive flex-grid; list mode renders rows with columns (label, enable, arm, slider, visible).
- **R31**: Grid mode has a size slider in the panel header that scales knob-tile dimensions between a minimum of ~48px and maximum of ~200px per tile.
- **R32**: Each knob tile contains: label, arm circle (red outline+filled when armed, grey outline+filled when idle), power button (blue high-contrast when enabled), eye icon (open when visible, closed when hidden), and the knob widget.
- **R33**: The knob widget uses a ~270-315° sweep; bottom-left indicates normalized 0, bottom-right indicates normalized 1 (or param range min/max).
- **R34**: Knob displays its current value in native units (e.g., "+3 dB", "8000 Hz", "0.7") above or below the widget depending on available space.
- **R35**: When a param's curve has `visible=1` (eye on), that curve renders inline on the track's timeline as a polyline with diamond keyframes at each curve point.
- **R36**: Panel state (view-mode, grid size slider, scroll position) is NOT persisted between sessions; it resets on mount.

- **R36a** (Bus sub-panel): The Macro Panel exposes a dedicated "Buses" sub-panel reachable from the panel header. The sub-panel lists the project's `project_send_buses` rows and supports: add bus (picks `bus_type` + default `static_params`), remove bus (except protected defaults if any), rename bus, edit static params (IR choice for reverb, time/feedback for delay, time/tone for echo), reorder buses (drag). Each CRUD action is one POST + one undo unit.

- **R29a** (Undo during active recording): If the user triggers undo (Ctrl+Z) while a knob is in `recording` state, the in-flight gesture MUST first commit (same behavior as releasing the mouse — spec R22), and THEN the undo executes against the just-committed state. Net effect: Ctrl+Z mid-record reverts the gesture the user was in the middle of making. The knob stays `armed`. The alternative (discard the in-flight gesture silently) is explicitly forbidden because users expect Ctrl+Z to be reversible — discarded gestures would be invisible to redo.

### Inline Timeline Curves

- **R37**: A shared `<InlineCurveEditor>` component is extracted from `TransitionPanel.tsx`'s existing curve-editor machinery and reused for audio effect curves and video transitions alike.
- **R38**: Users can drag individual keyframe diamonds to move them in time + value.
- **R39**: Users can multi-select diamonds (shift-click OR box-select). Dragging any selected diamond moves all selected diamonds by the same `(Δtime, Δvalue)`.
- **R40**: Double-click on a diamond deletes it. Right-click on a diamond cycles its `interpolation` through `bezier → linear → step → bezier`.
- **R41**: When multiple curves are visible on the same track, they render stacked vertically with ~50% alpha overlap so all are discernible.
- **R42**: Curve colors are assigned deterministically from a neon palette keyed on `(effect_type, param_name)` so the same param always renders in the same color across sessions. Less-common curves default to light blue.

### Copy-Paste Automation (P2 inside M13)

- **R43**: Users can multi-select keyframes across any number of visible curves on one or more tracks.
- **R44**: Ctrl+C serializes the selection into a clipboard blob containing `{source_track_ids, relative_t_offsets, kf_values_per_curve, effect_type_per_curve, param_name_per_curve}`.
- **R45**: Ctrl+V at the current playhead pastes the clipboard, computing `trackDelta = selected_track_id - primary_source_track_id` and offsetting track assignments accordingly.
- **R46**: Paste targets are filtered at paste-time: only target curves with matching `(effect_type, param_name)` receive pasted keyframes. Mismatched sources produce no paste for that source.
- **R47**: Pasted keyframes push exactly ONE undo unit to the scenecraft undo stack.

### Frequency Labels

- **R48**: 8 spectrum bands ship built-in: `sub bass (20-60)`, `bass (60-250)`, `low-mids / mud (250-500)`, `mids (500-2000)`, `presence (2000-4000)`, `attack / upper-mids (4000-8000)`, `sibilance (6000-9000)`, `air (10000-20000)`. Each presents a representative single Hz value equal to the geometric mean of its range.
- **R49**: 11 instrument-preset labels ship built-in with the exact Hz ranges enumerated in the design doc.
- **R50**: Users can define custom per-project labels via the EQ band knob's label dropdown. Custom labels persist in `project_frequency_labels`.

### Persistence

- **R51**: All effect chain + curve + bus state is persisted to the project's SQLite DB via new POST endpoints that mirror the existing `postUpdateAudioTrack` / `postUpdateAudioClip` patterns.
- **R52**: Schema changes are applied via a new migration file in `src/scenecraft/db/migrations/`.

### Reverb Assets

- **R53**: Six built-in impulse response files ship with the app at `src/scenecraft/assets/impulse_responses/`: `room-small.wav`, `room-large.wav`, `hall.wav`, `plate.wav`, `spring.wav`, `chamber.wav`. Combined gzipped bundle size ≤ 200 KB.
- **R54**: Users can also use an arbitrary audio file from the project pool as a custom IR.

---

## Interfaces / Data Shapes

### TypeScript

```ts
// scenecraft/src/lib/audio-effect-types.ts

export type EffectCategory = 'dynamics' | 'eq' | 'spatial' | 'time' | 'modulation' | 'distortion' | 'send'

export type ParamScale = 'linear' | 'log' | 'db' | 'hz'

export interface EffectParamSpec {
  name: string
  label: string
  animatable: boolean
  range: { min: number; max: number }
  scale: ParamScale
  default: number
  labelPresets?: Array<{ label: string; value: number; hzRange?: [number, number] }>
}

export interface EffectTypeSpec {
  type: string
  label: string
  category: EffectCategory
  params: EffectParamSpec[]
  build: (ctx: AudioContext, staticParams: Record<string, unknown>) => EffectNode
}

export interface EffectNode {
  input: AudioNode
  output: AudioNode
  setParam: (name: string, value: number, when?: number) => void
  scheduleCurve: (name: string, points: CurvePoint[], startTime: number, duration: number) => void
  dispose: () => void
}

// Existing type reused:
// export type CurvePoint = [time: number, value: number]

export interface TrackEffect {
  id: string
  track_id: string
  effect_type: string
  order_index: number
  enabled: boolean
  static_params: Record<string, unknown>
}

export interface EffectCurve {
  id: string
  effect_id: string
  param_name: string
  points: CurvePoint[]                    // stored normalized 0..1
  interpolation: 'bezier' | 'linear' | 'step'
  visible: boolean
}

export interface SendBus {
  id: string
  bus_type: 'reverb' | 'delay' | 'echo'
  label: string
  order_index: number
  static_params: Record<string, unknown>
}

export interface TrackSend {
  track_id: string
  bus_id: string
  level: number                           // 0..1, animatable via a curve
}
```

### HTTP endpoints

```
POST /api/projects/:name/track-effects             { track_id, effect_type, static_params }
POST /api/projects/:name/track-effects/:id         { order_index?, enabled?, static_params? }
DELETE /api/projects/:name/track-effects/:id
POST /api/projects/:name/effect-curves             { effect_id, param_name, points, interpolation, visible }
POST /api/projects/:name/effect-curves/:id         { points?, interpolation?, visible? }
DELETE /api/projects/:name/effect-curves/:id
POST /api/projects/:name/send-buses                { bus_type, label, static_params }
POST /api/projects/:name/send-buses/:id            { label?, order_index?, static_params? }
DELETE /api/projects/:name/send-buses/:id
POST /api/projects/:name/track-sends               { track_id, bus_id, level }
POST /api/projects/:name/frequency-labels          { label, freq_min_hz, freq_max_hz }
DELETE /api/projects/:name/frequency-labels/:id
```

### Event names emitted by the audio mixer

- `mixer.chain-rebuilt`  `{track_id}`
- `mixer.curve-scheduled`  `{effect_id, param_name, points_count}`
- `mixer.record-started`  `{effect_id, param_name, t_start}`
- `mixer.record-committed`  `{effect_id, param_name, t_start, t_end, points_added, points_removed}`

---

## Behavior

### Adding an Effect

1. User selects an audio track (panel updates via `selectedAudioTrackId` change).
2. User clicks "Add Effect" in the Macro Panel; dropdown lists all 17 effect types grouped by category.
3. User selects a type; a POST to `/track-effects` inserts the row with `enabled=1`, `order_index` = max(existing) + 1, `static_params` defaulted from `EffectTypeSpec.params` defaults.
4. Mixer receives change event; rebuilds the track's chain; the new effect's node is inserted before the `pan → track_gain` tail.
5. Macro Panel re-renders with a new `EffectGroup` row containing one knob per animatable param.

### Recording a Curve

1. User arms a knob (click arm circle: grey → red).
2. User presses play; playback begins. Armed knob state persists.
3. User clicks and holds the knob and drags; a `mousedown` handler enters `recording` state and starts a `requestAnimationFrame` loop throttled to ~30ms.
4. Each frame tick samples knob value and appends `[audioCtx.currentTime, value_normalized]` to an in-memory buffer. Mixer calls `param.setTargetAtTime(native_value, audioCtx.currentTime, 0.01)` for live audible feedback.
5. User releases mouse (`mouseup`). Recording commits:
   - Simplify the sample buffer by bezier-fit with 2% tolerance.
   - POST to `/effect-curves/:id` with the new full `points` array (or create the curve row if first record).
   - Points in `[gesture_start_t, gesture_end_t]` are replaced; points outside are preserved.
   - One undo unit pushed.
   - Knob remains armed; ready for another pass.
6. On pause / stop during recording: treat as implicit mouseup and commit.

### Editing a Curve Inline

1. User toggles eye-icon on a knob; `effect_curves.visible=1` is persisted.
2. The curve renders as a polyline on the selected track's timeline lane, with diamond keyframes at each point.
3. User drags a diamond; its `[time, value]` updates; the curve re-renders in real time.
4. On mouseup, one POST updates the curve row; one undo unit pushed.
5. User multi-selects diamonds via shift-click or box-select; any drag applies uniform `(Δtime, Δvalue)` to all selected diamonds.
6. Double-click on a diamond deletes it; one undo unit pushed.
7. Right-click on a diamond cycles `interpolation` to the next value; one undo unit pushed.

### Bypassing a Disabled Effect

1. User clicks a knob's power-button; `track_effects.enabled` toggles.
2. Mixer updates the chain so that the disabled effect's `input` is directly wired to the same downstream node as its `output` would be.
3. The effect's internal state (scheduled curves, LFO) is NOT destroyed; re-enabling restores the same scheduled curves.

### Configuring Send Buses

1. User opens a "Buses" sub-panel in the Macro Panel.
2. Defaults on new project: 2 reverb buses (`Plate`, `Hall`, both with distinct IR assets), 1 delay bus, 1 echo bus.
3. User can add more, remove non-default ones, change static params (IR, delay time, echo tone), rename.
4. Each track automatically gets a `track_sends` row (level=0) for each bus. The Macro Panel surfaces one "Send to {bus.label}" knob per bus on every track.

### Seeking During Playback

1. User drags the timeline playhead (seek event).
2. Mixer cancels all `AudioParam` scheduled values.
3. For each active curve: reschedule points whose `time >= seek_time`.
4. Playback resumes from the new position if it was playing.

---

## Acceptance Criteria

- [ ] All 17 effect types can be added to a track, configured, enabled/disabled, reordered, and removed via HTTP endpoints.
- [ ] Every animatable param of every effect can be automated with a bezier-by-default curve.
- [ ] A user can record a filter-cutoff sweep by arming the knob, pressing play, and dragging the knob; the recorded curve plays back identically on replay.
- [ ] Multi-arm multi-knob recording works: user can arm 3 knobs, press play, drag any of them at any time during playback; each produces its own curve independently.
- [ ] Disabling an effect removes its audible contribution while preserving its configured curves; re-enabling restores them.
- [ ] A reverb bus with a `plate.wav` IR produces a plate-reverb character on tracks that send to it.
- [ ] Send-level curves animate the `track → bus` gain smoothly.
- [ ] Inline timeline curves render when eye-toggle is on, hide when off, and allow full keyframe editing (drag, multi-select, double-click delete, right-click easing cycle).
- [ ] Multi-select + Ctrl+C/V copies automation keyframes from one track's verse-1 to another track's verse-2 with correct `trackDelta` + time offset.
- [ ] Undo reverts the most recent record pass (or static knob change) to the curve state immediately before that action.
- [ ] 6 IR files ship with the app; a filesystem check confirms `src/scenecraft/assets/impulse_responses/{room-small,room-large,hall,plate,spring,chamber}.wav` all exist and combined gzipped bundle ≤ 200 KB (R53); ConvolverNode reverb works with any of them.
- [ ] Static params (R9 exception list: `character`, `bus_id`, `rate`, IR-choice) cannot be animated — POST to `/effect-curves` for any of them returns HTTP 400.
- [ ] Effect chain + curves persist across scenecraft restarts.
- [ ] Macro Panel grid ↔ list view toggles without losing track selection.
- [ ] Grid-size slider continuously scales tile dimensions between 48px and 200px.
- [ ] EQ band knob label dropdown lists all 8 spectrum bands + 11 instrument presets + any project custom labels.

---

## Tests

### Base Cases

The core behavior contract: happy path, common bad paths, primary positive and negative assertions.

#### Test: add-effect-persists-and-rebuilds-chain (covers R1, R7, R8, R14)

**Given**:
- A project with audio track `T1` and no existing effects
- An empty `track_effects` table

**When**: POST `/track-effects` with `{track_id: T1, effect_type: compressor, static_params: {ratio: 4, attack: 10}}`

**Then** (assertions):
- **http-200**: response status is 200 with JSON containing the new effect `id` and `order_index: 0`
- **db-row-present**: a row exists in `track_effects` with `track_id=T1, effect_type='compressor', enabled=1, order_index=0, static_params` equal to the posted value
- **chain-rebuilt-event**: the mixer emits `mixer.chain-rebuilt {track_id: T1}` within 100 ms
- **graph-contains-compressor**: the track's WebAudio chain contains a `DynamicsCompressorNode` downstream of the track's volume gain and upstream of its pan node

#### Test: animatable-param-records-curve (covers R9, R20-R25)

**Given**:
- Track `T1` has a `highpass` effect with `cutoff` animatable
- No curve exists for `(effect_id, 'cutoff')`
- Playback is playing at t=0
- The `cutoff` knob is armed

**When**: user mousedown on the knob at audioCtx.currentTime=1.0, drags from value 0.2 to 0.8 over 2 seconds, mouseup at audioCtx.currentTime=3.0

**Then** (assertions):
- **curve-created**: a new row in `effect_curves` exists for `(effect_id, 'cutoff')`
- **curve-spans-gesture**: the curve's min point time ≥ 1.0 and max point time ≤ 3.0
- **curve-values-monotonic**: sampling the curve at 1.0, 2.0, 3.0 returns values approximately 0.2, 0.5, 0.8 (within 5% tolerance)
- **interpolation-bezier**: `effect_curves.interpolation = 'bezier'`
- **undo-one-unit**: exactly one entry was pushed to the scenecraft undo stack
- **knob-stays-armed**: the knob's arm state after mouseup is still "armed"
- **record-committed-event**: the mixer emits `mixer.record-committed {effect_id, param_name: 'cutoff', points_added: N, points_removed: 0}` once

#### Test: disabled-effect-is-bypassed (covers R15)

**Given**: Track `T1` has a `drive` effect with `enabled=1`, producing audible saturation at the current playhead

**When**: POST `/track-effects/:id` with `{enabled: 0}`

**Then** (assertions):
- **db-enabled-zero**: `track_effects.enabled = 0` for that effect
- **chain-bypassed**: the drive WaveShaperNode is no longer between volume_gain and pan in the graph
- **curves-preserved**: the effect's `effect_curves` rows are unchanged
- **audio-clean**: the rendered audio at the same playhead is identical to the track with no drive effect (within float-precision tolerance)

#### Test: send-bus-defaults-on-new-project (covers R3, R12)

**Given**: A freshly created project with no audio tracks or buses

**When**: The first audio track is added

**Then** (assertions):
- **buses-seeded**: `project_send_buses` contains exactly 4 rows: `Plate` (reverb), `Hall` (reverb), `Delay` (delay), `Echo` (echo), in that `order_index`
- **track-sends-seeded**: `track_sends` contains 4 rows for the new track, one per bus, all with `level=0`
- **graph-has-buses**: the WebAudio graph contains 4 parallel bus branches, each terminating at `audioCtx.destination`

#### Test: send-level-curve-animates-gain (covers R13, R16)

**Given**: Track `T1` with a `track_sends` entry to bus `B_plate` and a curve on `(synthetic effect_type '__send', param_name = B_plate.id)` rising 0→1 linearly over 5 seconds

**When**: Playback begins at t=0

**Then** (assertions):
- **gain-node-scheduled**: the `GainNode` on the `T1 → B_plate` send path has `setValueCurveAtTime` scheduled
- **gain-at-t-0**: sampling the audio at t=0 shows zero contribution from bus `B_plate`
- **gain-at-t-5**: sampling the audio at t=5 shows full send level (level=1) contribution from bus `B_plate`
- **reverb-output-present**: the bus output is non-zero at t=5 given a non-silent input

#### Test: inline-curve-eye-toggle-shows-keyframes (covers R35, R37, R38)

**Given**: A curve on `(E1, threshold)` with 5 keyframes; `visible=0`

**When**: User clicks the eye icon on the threshold knob

**Then** (assertions):
- **db-visible-one**: `effect_curves.visible = 1`
- **inline-rendered**: the `<InlineCurveEditor>` component mounts on the selected track's timeline lane
- **diamonds-count-5**: exactly 5 diamond keyframes render at the correct `[time, value]` positions
- **polyline-interpolates**: a polyline connects the diamonds using the curve's interpolation scheme

#### Test: inline-keyframe-multi-select-moves-together (covers R39)

**Given**: Three visible keyframes at times `{1, 2, 3}` and values `{0.3, 0.5, 0.7}`; all are selected via box-select

**When**: User drags the middle diamond by `(Δtime=+0.5, Δvalue=+0.1)`

**Then** (assertions):
- **all-three-moved**: the three keyframes now sit at times `{1.5, 2.5, 3.5}` and values `{0.4, 0.6, 0.8}`
- **db-points-updated**: one POST to `/effect-curves/:id` with the new points array
- **undo-one-unit**: exactly one undo unit pushed

#### Test: double-click-diamond-deletes (covers R40)

**Given**: An inline curve with 4 keyframes

**When**: User double-clicks the second keyframe

**Then** (assertions):
- **db-points-3**: the curve now has 3 points; the one at the clicked position is gone
- **undo-one-unit**: exactly one undo unit pushed
- **polyline-updates**: the rendered polyline reconnects using the remaining 3 points

#### Test: right-click-diamond-cycles-interpolation (covers R40)

**Given**: A curve with `interpolation='bezier'`

**When**: User right-clicks any of its diamonds three times in sequence

**Then** (assertions):
- **after-1**: `interpolation='linear'`
- **after-2**: `interpolation='step'`
- **after-3**: `interpolation='bezier'`
- **undo-three-units**: three undo units pushed in order

#### Test: copy-paste-across-tracks-uses-trackdelta (covers R43-R47)

**Given**:
- Track `T1` has a visible `cutoff` curve with keyframes at `[(10.0, 0.2), (12.0, 0.8)]`
- Track `T2` has a `cutoff` curve (same effect type + param) with no keyframes in `[30.0, 35.0]`
- Playback is paused with playhead at t=30.0
- User selects both keyframes on T1 via box-select

**When**: User issues Ctrl+C, selects T2, Ctrl+V

**Then** (assertions):
- **t2-has-two-new-kfs**: T2's `cutoff` curve now contains points at approximately `[(30.0, 0.2), (32.0, 0.8)]` (offsets preserved from the source)
- **t1-unchanged**: T1's curve is unchanged
- **undo-one-unit**: one undo unit pushed for the paste

#### Test: volume-curves-remain-separate (covers Non-Goal + existing behavior)

**Given**: A track with an existing volume curve (pre-existing system) and a new `drive` effect with its own `amount` curve

**When**: Playback runs

**Then** (assertions):
- **volume-applied**: the existing volume curve still modulates the track's volume_gain node
- **drive-applied**: the new `amount` curve modulates the drive effect's wet control
- **independent-state**: modifying the volume curve does NOT modify the drive curve and vice versa
- **volume-not-in-track-effects**: the volume curve is NOT stored in `track_effects` / `effect_curves`; it continues to live in its existing table

#### Test: undo-reverts-record-pass (covers R25)

**Given**: A user records a touch-record pass that writes 20 bezier keyframes to an effect curve

**When**: User presses Ctrl+Z

**Then** (assertions):
- **curve-restored**: the curve's `points` array matches exactly what it contained before the record pass began
- **events-emitted**: a `mixer.curve-scheduled` event fires with the pre-pass points

#### Test: effect-curves-unique-constraint (covers R2)

**Given**: Effect E1 has an existing curve for `param_name='cutoff'`

**When**: An out-of-band direct SQL INSERT attempts a second row with `(effect_id=E1, param_name='cutoff', points=[...])`

**Then** (assertions):
- **sql-error**: SQLite raises a UNIQUE constraint violation (integrity error)
- **db-row-count**: `effect_curves` still contains exactly one row for `(E1, 'cutoff')`
- **app-upsert-unaffected**: the normal POST path (which should UPSERT via `INSERT OR REPLACE` or equivalent) continues to work correctly for app-layer updates — raw duplicate INSERTs are blocked at the SQL layer

#### Test: recording-samples-at-33hz-target (covers R23)

**Given**: User is recording on an armed knob with `audioCtx.currentTime` advancing at realtime rate; the record loop runs for 3.00 seconds of playback

**When**: Recording commits

**Then** (assertions):
- **sample-count-in-range**: the pre-simplification raw buffer contains between 80 and 110 samples (3s × 33Hz = 99 samples; ±30% tolerance for rAF jitter and frame-rate variance)
- **min-rate-enforced**: no 100ms+ gap between consecutive samples (asserts the ≥30Hz floor even under frame jitter)
- **rAF-driven**: the loop's tick source is `requestAnimationFrame`, not `setInterval` — verifiable by mocking rAF and counting invocations

#### Test: track-sends-row-per-track-per-bus (covers R4)

**Given**: A project with 4 default buses (Plate, Hall, Delay, Echo)

**When**: A second audio track is added to the project

**Then** (assertions):
- **db-rows-present**: `track_sends` contains exactly 4 new rows for the new track, one per bus, each with `level=0`
- **pk-composite**: the `(track_id, bus_id)` primary key is enforced — a second INSERT with the same pair fails at the SQL layer
- **cascade-on-track-delete**: deleting the audio track cascades to remove its 4 `track_sends` rows

#### Test: interpolation-mode-schedules-correct-audioparam-call (covers R19)

**Given**: Three identical curves on three different effect params, with `interpolation='bezier'`, `'linear'`, and `'step'` respectively; each curve has points `[(0, 0), (1, 1)]`

**When**: The mixer schedules the curves at project load

**Then** (assertions):
- **bezier-uses-setValueCurveAtTime**: the bezier-interpolated param received a `setValueCurveAtTime(Float32Array, startTime, duration)` call with a densely-sampled Float32Array
- **linear-uses-linearRampToValueAtTime**: the linear-interpolated param received a `setValueAtTime(0, t0)` + `linearRampToValueAtTime(1, t1)` sequence (no Float32Array)
- **step-uses-setValueAtTime-only**: the step-interpolated param received only `setValueAtTime(0, t0)` + `setValueAtTime(1, t1)` calls (no ramps, no curves)

#### Test: custom-ir-from-pool-file (covers R54)

**Given**: A pool segment containing a WAV file that the user wants as a custom IR; a reverb bus exists with the default `plate.wav` IR

**When**: User updates the bus's `static_params.ir_path` to point at the pool segment's file path

**Then** (assertions):
- **db-static-params-updated**: `project_send_buses.static_params.ir_path` now references the pool segment's path
- **convolver-reloaded**: the bus's ConvolverNode has the new buffer loaded (verified via a `mixer.ir-changed {bus_id}` event OR by sampling the reverb tail and checking it differs from plate.wav)
- **plate-default-not-deleted**: the shipped `plate.wav` IR asset on disk is untouched

#### Test: animating-static-param-rejected (covers R9, R8a, R_V1, negative)

**Given**: A `drive` effect exists on track T1 (its `character` param is static)

**When**:
- (a) User POSTs to `/effect-curves` with `{effect_id: drive.id, param_name: 'character', points: [...]}`
- (b) User POSTs to `/effect-curves` with `{effect_id: __send_synthetic, param_name: 'wet', ...}` referencing a non-existent effect_id
- (c) User POSTs to `/track-effects` with `{effect_type: '__send', ...}`

**Then** (assertions):
- **http-400-a**: (a) returns HTTP 400 with an error naming the static param `character`
- **http-404-b**: (b) returns HTTP 404 because the referenced `effect_id` doesn't exist
- **http-400-c**: (c) returns HTTP 400 because `__send` is synthetic and not in the R8 registry
- **no-db-row-created**: `effect_curves` and `track_effects` gain zero new rows across all three attempts

#### Test: unknown-effect-type-rejected (covers R_V1)

**Given**: A valid track T1

**When**: POST `/track-effects` with `{track_id: T1, effect_type: 'timewarp', ...}` (not in R8)

**Then** (assertions):
- **http-400**: 400 with error message naming the unknown type
- **no-db-row**: `track_effects` unchanged
- **no-chain-rebuild**: no `mixer.chain-rebuilt` event fires

#### Test: delete-nonexistent-effect-idempotent (covers R_V1)

**Given**: No `track_effects` row with id `'nope-123'` exists

**When**: DELETE `/track-effects/nope-123`

**Then** (assertions):
- **http-200**: response is HTTP 200 with empty body (not 404 — delete is idempotent)
- **no-error-logged**: no error-level log entry fires

#### Test: order-index-collision-resolved-atomically (covers R_V1, R14)

**Given**: Track T1 with effects E1 (order=0), E2 (order=1), E3 (order=2)

**When**: Client POSTs `/track-effects/E3` with `{order_index: 0}` (wants E3 at position 0)

**Then** (assertions):
- **final-orders-unique**: after the request, all three effects have distinct `order_index` values (no duplicates)
- **e3-first**: E3.order_index = 0; E1 and E2 shifted to 1 and 2
- **single-transaction**: the reorder happens in one SQL transaction (intermediate-state reads never see two effects at order 0)
- **chain-rebuild-once**: exactly ONE `mixer.chain-rebuilt` event fires

#### Test: undo-during-recording-commits-then-reverts (covers R29a)

**Given**: User is mid-record on knob K1 of effect E1's param P1 with 40 samples buffered; E1 had pre-existing curve state C0 before the gesture started

**When**: User presses Ctrl+Z while the mousedown is still held

**Then** (assertions):
- **gesture-commits-first**: the in-flight gesture's samples flush to the DB (bezier-fit + POST), producing curve state C1
- **then-undo-reverts**: the undo pops C1 off the stack and restores C0
- **two-undo-entries-stack-to-one-visible**: post-undo stack shows only C0 as latest; redo (Ctrl+Y) replays the gesture to C1
- **knob-stays-armed**: arm state transitions from recording → armed (not idle)
- **silent-discard-forbidden**: no code path discards gesture samples silently

#### Test: bus-subpanel-crud (covers R36a)

**Given**: The Macro Panel is open on a track; the Buses sub-panel is closed

**When**: User clicks a "Buses" button in the panel header

**Then** (assertions):
- **subpanel-opens**: the Buses sub-panel renders
- **lists-current-buses**: the sub-panel lists all rows from `project_send_buses`, ordered by `order_index`
- **add-bus-posts**: clicking "Add Reverb Bus" POSTs to `/send-buses` and the new row appears
- **rename-persists**: editing a label POSTs to `/send-buses/:id` and survives a reload
- **remove-bus-cascades**: removing a bus cascades to remove `track_sends` rows for that bus and removes its associated `__send` curves
- **each-action-one-undo**: each CRUD action produces exactly one undo unit

#### Test: macro-panel-grid-list-toggle (covers R30)

**Given**: The panel is mounted in grid mode with the `cutoff` knob visible

**When**: User clicks the grid↔list toggle

**Then** (assertions):
- **layout-switches**: the DOM transitions from a grid container to a list (`<table>` or similar) layout
- **same-knobs-listed**: all knobs previously visible in grid mode are present as table rows in list mode
- **selection-preserved**: `selectedAudioTrackId` in EditorStateContext is unchanged across the toggle

#### Test: macro-panel-size-slider-scales-tiles (covers R31)

**Given**: The panel is in grid mode; slider at default ~50% (tile ~120px)

**When**: User drags the slider to maximum

**Then** (assertions):
- **tiles-scale**: all rendered tiles have a computed width between 180px and 200px (per R31's 48–200px range)
- **reflows-correctly**: the grid column count adjusts; no tile clips
- **min-end**: dragging to minimum produces tiles between 48px and 60px

#### Test: persists-across-restart (covers R51, R52)

**Given**: A project with 3 tracks, 12 total effects, 8 curves, 4 buses, all configured and populated

**When**: The scenecraft process stops and is restarted; the project is reopened

**Then** (assertions):
- **db-rows-present**: all 12 effects, 8 curves, 4 buses, and corresponding track_sends rows are present in the SQLite DB
- **graph-rebuilt**: the WebAudio graph matches pre-restart topology for every track
- **playback-identical**: playing from t=0 produces the same audio output as before restart (within float-precision tolerance)

### Edge Cases

Boundaries, unusual inputs, concurrency, idempotency, ordering, time-dependent behavior, resource exhaustion.

#### Test: curve-point-values-out-of-range-clamped (covers R6, R17)

**Given**: A POST to `/effect-curves/:id` with a `points` array containing `[(1.0, -0.5), (2.0, 1.8)]`

**When**: The server processes the request

**Then** (assertions):
- **db-values-clamped**: stored points are `[(1.0, 0.0), (2.0, 1.0)]`
- **http-200**: server returns 200 (clamping is not an error)
- **warning-logged**: a warning-level log entry mentions clamping with the effect_id + param_name

#### Test: record-without-arm-is-noop (covers R21)

**Given**: A knob with arm state "idle" and playback playing

**When**: User mousedown + drag + mouseup on the knob

**Then** (assertions):
- **no-curve-created**: `effect_curves` unchanged
- **no-undo-pushed**: undo stack unchanged
- **live-audible-feedback**: the mixer still calls setTargetAtTime for immediate sound during the drag (so user hears what they're doing)
- **value-reverts-after-release**: on mouseup, knob returns to the value it had before mousedown

#### Test: gesture-while-paused-edits-directly (covers R26)

**Given**: Playback paused with playhead at t=5.0; knob is armed; no curve exists for this param

**When**: User drags the knob to value 0.75

**Then** (assertions):
- **curve-created-one-point**: a new curve row with exactly one point at `(5.0, 0.75)`
- **interpolation-bezier**: the single-point curve's interpolation is `bezier`
- **no-record-started-event**: `mixer.record-started` is NOT emitted
- **undo-one-unit**: one undo unit pushed

#### Test: record-commit-at-playback-stop (covers R22)

**Given**: User is mid-record on a knob; mixer is streaming audio

**When**: User presses the stop button while still holding the knob mousedown

**Then** (assertions):
- **commit-fires**: the in-progress gesture commits with its accumulated samples up to the stop moment
- **db-row-updated**: `effect_curves.points` is updated with the simplified buffer
- **undo-one-unit**: one undo unit pushed
- **knob-returns-to-armed**: arm state transitions back to "armed" (not "recording")

#### Test: bezier-fit-simplification-drops-redundant-points (covers R24)

**Given**: A 66-sample raw buffer describing a monotonic 0→1 ramp with 33Hz sampling over 2 seconds, no deviation > 2% from the straight line

**When**: Commit runs

**Then** (assertions):
- **simplified-points-count**: final `points` array has ≤ 4 points (endpoints + minimal control points)
- **shape-preserved**: sampling the simplified bezier at any t ∈ [0, 2] stays within 2% of the raw buffer value at that t

#### Test: two-overlapping-recordings-on-different-knobs (covers R20, multi-arm)

**Given**: Two armed knobs `K1` (effect E1, param P1) and `K2` (effect E2, param P2); playback is playing

**When**: User mousedown-drags `K1` during [1.0, 3.0], and during [2.0, 2.5] the user's OTHER hand also mousedown-drags `K2` (two pointers / two mouse-chords — real setup: a controller + mouse)

**Then** (assertions):
- **both-curves-created**: `effect_curves` has rows for `(E1, P1)` and `(E2, P2)`
- **e1-spans-correct**: E1's curve has points in `[1.0, 3.0]`
- **e2-spans-correct**: E2's curve has points in `[2.0, 2.5]`
- **two-undo-units**: exactly two undo units are pushed (one per gesture, in commit order)
- **no-cross-contamination**: E1's curve has no points in E2's gesture range and vice versa

#### Test: seek-during-curve-playback-reschedules (covers R18)

**Given**: A curve rising 0→1 over [0, 10]; playback is at t=3.0 with the AudioParam currently at value ~0.3

**When**: User seeks to t=8.0

**Then** (assertions):
- **cancel-values-called**: the AudioParam's scheduled values were canceled (via `cancelScheduledValues(0)` or equivalent)
- **new-schedule-starts-at-t8**: the new schedule's first point is at `audioCtx.currentTime` (corresponding to project-time 8.0) with value ~0.8
- **points-before-8-not-scheduled**: no remaining schedule entries reference project times < 8.0

#### Test: concurrent-effect-add-and-record (covers R14, concurrency)

**Given**: Track T1 is playing with 3 effects. User is mid-record on effect E1's param P1 (recording state)

**When**: User adds a 4th effect via the "+" button (POST /track-effects)

**Then** (assertions):
- **record-commits-cleanly**: the in-progress recording commits with its current sample buffer, as if the user had released the mouse
- **chain-rebuilds**: the chain is rebuilt with the new 4th effect appended
- **e1-curve-intact**: E1's newly-committed curve is present and contains no corruption
- **no-orphan-schedule**: no AudioParam scheduling from the pre-rebuild chain remains (all canceled as part of rebuild)

#### Test: disable-mid-record-pauses-recording (covers R15, R20, concurrency)

**Given**: A knob is recording; user drags it steadily

**When**: User clicks the power-button on the SAME effect (disables it) while the drag continues

**Then** (assertions):
- **record-commits**: the recording commits at the moment of disable
- **chain-bypassed**: the effect is bypassed in the graph
- **curve-saved**: the `effect_curves.points` reflects the samples up to disable time
- **post-disable-drag-is-noop**: further drag movement after disable produces no further samples
- **undo-one-unit-record**: exactly one undo unit for the record commit
- **undo-one-unit-enable-toggle**: a separate undo unit for the enable-toggle

#### Test: simultaneous-copy-paste-across-10-tracks (covers R43-R46)

**Given**: 10 tracks each with a visible `cutoff` curve containing 4 keyframes in [10, 14]

**When**: User selects all 40 keyframes via box-select, Ctrl+C, seeks to t=20.0, Ctrl+V

**Then** (assertions):
- **all-10-tracks-updated**: each track's `cutoff` curve gains 4 new keyframes in [20, 24]
- **source-kfs-intact**: each track's original [10, 14] keyframes are unchanged
- **one-undo-unit**: exactly one undo unit pushed (not 10 or 40)

#### Test: missing-ir-file-falls-back (covers R12, R53)

**Given**: A reverb bus configured to use `plate.wav`, but the IR file is missing from disk

**When**: The project loads

**Then** (assertions):
- **bus-preserved**: the bus row exists in the DB; it is not auto-deleted
- **graph-has-bypass**: the bus's ConvolverNode is bypassed (send level routes directly through without convolution)
- **warning-logged**: a warning mentions the missing IR and bus id
- **user-notification**: the UI surfaces a banner or indicator on the bus

#### Test: eq-band-freq-sweep-hits-instrument-preset-ranges (covers R10, R48, R49)

**Given**: An `eq_band` knob with `freq` animatable; user selects preset "Vocal presence (2-5 kHz)" from the dropdown

**When**: The selection commits

**Then** (assertions):
- **freq-default-set**: the `freq` knob's current static value is set to the geometric mean of 2000-5000 Hz (~3162 Hz) OR the preset's `value_hz` field if present
- **label-displayed**: the knob's label text reads "Vocal presence"
- **hz-displayed**: the numeric readout reads "~3.2 kHz" (unit formatting)

#### Test: user-defined-label-persists (covers R50)

**Given**: No custom labels in the project

**When**: User adds a label "My Hat Freq (11-13 kHz)" via POST `/frequency-labels`

**Then** (assertions):
- **db-row-present**: `project_frequency_labels` has the new row
- **dropdown-contains**: the EQ band knob's dropdown now lists the custom label after the built-in sets
- **persists-across-restart**: reopening the project retains the custom label

#### Test: reorder-effects-via-order-index-update (covers R1, R14)

**Given**: Track T1 with effects E1 (order 0), E2 (order 1), E3 (order 2)

**When**: User drags E3 before E1 → POST updates `order_index` values to E3=0, E1=1, E2=2

**Then** (assertions):
- **db-orders-updated**: stored order_indices match the new values
- **chain-rebuilt**: the mixer rebuilds the chain in new order; audio reflects E3 processing first now
- **curves-unchanged**: no `effect_curves.points` content is modified

#### Test: orphan-curve-cleaned-on-effect-delete (covers R14, persistence hygiene)

**Given**: Effect E1 has 2 curves in `effect_curves`

**When**: DELETE `/track-effects/:id` for E1

**Then** (assertions):
- **effect-gone**: `track_effects` row is gone
- **curves-gone**: both `effect_curves` rows are cascade-deleted (ON DELETE CASCADE)
- **inline-render-cleared**: if either curve was visible inline, the rendered polylines disappear

#### Test: touch-mode-is-single-record-mode (covers R27, negative assertion)

**Given**: Any point in the Macro Panel UI

**When**: User searches for a "Latch" or "Write" mode toggle

**Then** (assertions):
- **no-latch-toggle**: no UI element reading "Latch" exists
- **no-write-toggle**: no UI element reading "Write" mode exists
- **only-touch**: Touch is the only record mode in behavior

#### Test: no-recording-while-paused (covers R21, negative assertion)

**Given**: Playback paused; armed knob; user drags the knob

**When**: The drag completes

**Then** (assertions):
- **no-multi-point-curve**: the commit produces either zero new points or exactly one new point at the current playhead (NOT a sequence of samples over gesture duration)
- **no-mixer-record-started-event**: `mixer.record-started` is never emitted during the gesture

#### Test: panel-layout-state-not-persisted (covers R36)

**Given**: User toggles to list view, adjusts grid size to maximum

**When**: User closes the editor, reopens it

**Then** (assertions):
- **view-mode-default**: panel reopens in grid mode
- **size-default**: grid size is back at default
- **Panel placement from PanelLayout system IS persisted** (that's separate machinery) — only the in-panel view-mode/size are ephemeral

#### Test: graph-rebuild-during-recording-commits-and-reschedules (covers R14, R16, concurrency)

**Given**: User is recording curve on E1.P1 of track T1

**When**: User adds a NEW effect E2 to the same track T1 (triggers chain rebuild)

**Then** (assertions):
- **e1-recording-commits**: the in-progress recording flushes its buffer and writes to DB
- **e1-curve-rescheduled-to-new-node**: after rebuild, E1's new node has its complete curve scheduled, including the freshly-committed points
- **e2-chain-contains**: the new E2 node is present in the rebuilt chain
- **no-audio-dropout-beyond-rebuild-window**: audio glitch is bounded to the rebuild transition (acceptable ≤ 50ms)

#### Test: bus-count-limits-enforced (covers R3, hygiene)

**Given**: A project with 4 reverb buses already configured

**When**: User attempts to POST `/send-buses` to add a 5th reverb bus

**Then** (assertions):
- **http-200-or-limited**: EITHER the create succeeds (no artificial cap) OR the server returns a clear error stating the limit. The answer is decided by Open Question below.

---

## UI-Structure Test Strategy

Scenecraft has no frontend test harness today (per project memory). UI-structure requirements (R28-R36a, R37-R42) are verified with a layered approach:

- **Logic-level requirements** (R30 grid↔list toggle, R31 size-slider scaling, R36 ephemerality, R36a bus sub-panel CRUD, R37-R40 inline editor drag / multi-select / delete / interpolation cycle, R43-R47 copy-paste, R29a undo-during-record): covered by the Tests section above. These are observable from DOM inspection + state-change assertions; vitest + happy-dom (or similar) is sufficient.
- **Visual-structure requirements** (R32 tile contents, R33 270-315° knob sweep, R34 numeric-unit formatting, R41 stacked-curve rendering with ~50% alpha, R42 deterministic color palette): deferred to **manual + PR-review verification** until the project adopts visual-regression tooling (Storybook snapshots, Percy, or similar). Acknowledged gap; low regression risk since these are render-once-correctly-and-leave-alone concerns.
- **Real-audio requirements** (send-level audibility, reverb character, send animation producing audible fade): partially covered via event assertions (mixer.* events); full audio correctness verified during implementation by ear.

Implementers installing vitest per this project's "No frontend tests yet" memory note MUST ship the logic-level tests listed above; visual-structure items may ship with a manual-verification checklist instead.

---

## Open Questions

1. **Maximum bus count per project**: is there a cap on how many send buses a project can have? (e.g., 8 reverb + 4 delay + 4 echo = 16 total). Current design says "user-configurable"; implementation may need a hard ceiling to keep the audio graph bounded. **Captured in edge test `bus-count-limits-enforced`.**
2. **Effect reordering via drag-and-drop**: required in v1 or deferred? Current design implies yes via `order_index` POST, but UI drag-reorder is unspecified.
3. **CPU budget / benchmark**: what's the acceptable max CPU on the reference machine (16-core) for 20 tracks × 4 effects × active automation? Design says "4-8% estimated, benchmark during implementation."
4. **Effect visibility across user tabs**: if the same project is open in two browser tabs, do curve edits sync live (via existing WS) or are conflicts accepted? Likely inherits existing behavior; confirm during implementation.

### Resolved / reclassified

- ~~Pitch-shift library choice~~ → moved to Non-Goals (decide when the feature ships, not M13).
- ~~Tutorial auto-advance~~ → moved to Non-Goals (lives with the separate Tutorial Panel spike).
- ~~Single-thread mixer assumption~~ → demoted to an implementation review item (not a design decision). Confirm during code review that no code path touches the WebAudio graph from an AudioWorklet thread. The `graph-rebuild-during-recording-commits-and-reschedules` test covers the bounded-glitch acceptance already.

---

## Key Design Decisions

See `agent/design/local.effect-curves-macro-panel.md` §Key Design Decisions for the full table. Summary:

- Per-track effect chains (not per-clip)
- ConvolverNode reverb with static IR (multi-bus for character variety)
- Echo as distinct effect type from Delay
- All params animatable uniformly (no per-effect exception list)
- Touch-only record mode; bezier default; 33Hz sample; 2% tolerance
- Grid OR list Macro Panel layout
- Inline timeline curves via eye-toggle (shared `<InlineCurveEditor>`)
- Volume curves stay separate (no unification)
- P2 inside M13: multi-select + `trackDelta` copy/paste automation
- Deferred: LFO routing, MIDI, VST, full pitch/time-stretch, Tutorial Panel

---

## Related Artifacts

- `agent/design/local.effect-curves-macro-panel.md` — design (this spec's source)
- `agent/clarifications/clarification-7-effect-curves-macro-panel.md` — decision record (gitignored, local)
- `scenecraft/src/lib/audio-mixer.ts` — existing mixer this extends
- `scenecraft/src/components/editor/AudioPropertiesPanel.tsx` — panel pattern
- `scenecraft/src/components/editor/TransitionPanel.tsx` — `<InlineCurveEditor>` extraction source
- M10 `trackDelta` copy-paste machinery — reused for automation paste
