# Milestone 13: Effect Curves + Macro Panel + Touch-Record

**Goal**: Ship a full audio mixing surface: per-track effect chains across 6 P0 categories, project-scoped send buses, a Macro Panel of arm-able knobs, and DAW-style touch-record so the user rides curves live during playback.
**Duration**: ~2 weeks (12 tasks, ~70 dev hours)
**Dependencies**: None (builds on existing `scenecraft/src/lib/audio-mixer.ts` and the `TransitionPanel` curve-editor machinery that gets extracted for reuse)
**Status**: Not Started

---

## Overview

Today scenecraft's audio pipeline has per-track + per-clip volume curves — and nothing else. This milestone extends that into a full mixing surface: each track hosts an ordered chain of effects (compressor, EQ, filter, delay, reverb send, chorus, flanger, phaser, tremolo, pan, drive, …); every animatable parameter can be automated with a curve; a new Macro Panel surfaces those parameters as knobs; users arm knobs and record curves live while the project plays.

Everything is pinned by [spec local.effect-curves-macro-panel](../specs/local.effect-curves-macro-panel.md) (**58 requirements, 46 named tests**, proofed 2026-04-23 — Ready for Implementation). Source design: [local.effect-curves-macro-panel](../design/local.effect-curves-macro-panel.md). Decision record: clarification-7 (gitignored).

**Cross-repo scope split** (from spec §Scope > Cross-Repo Split):
- **scenecraft-engine** (this repo) owns: SQLite schema + migrations (R1-R5, R52), HTTP endpoints + POST validation (R_V1), cascade delete semantics. Tasks 45, 46 (registry data model), 52.
- **scenecraft** (frontend) owns: WebAudio graph (R7, R11-R19), Macro Panel UI + Bus sub-panel (R28-R36a), InlineCurveEditor (R37-R42), touch-record state machine (R20-R29a), copy-paste automation (R43-R47), frequency-label preset constants (R48-R49 client data). Tasks 47, 48, 49, 50, 51, 53, 54, 55, 56.

Both repos must satisfy the same spec contract; tasks in this milestone enumerate each side's share of the work.

---

## Deliverables

### 1. Data model
- New SQLite tables: `track_effects`, `effect_curves`, `project_send_buses`, `track_sends`, `project_frequency_labels`
- Migration file in `src/scenecraft/db/migrations/`
- ORM types mirroring the spec's TypeScript interfaces

### 2. Effect engine
- Effect type registry listing all 15 v1 types with params metadata + factory (`EffectTypeSpec`, `EffectNode`)
- Per-track audio graph builder with enable-bypass, chain reorder
- Project-scoped send bus builder (default 2 reverb + 1 delay + 1 echo)
- ~6 bundled IR files (~200KB gzipped)

### 3. Curve scheduling
- Curve → `AudioParam` scheduling with bezier/linear/step modes
- Seek-aware rescheduling
- Normalized `[0, 1]` ↔ native-unit converters (log-dB, log-Hz, linear)

### 4. 15 effect types across 6 categories
- Dynamics: compressor, gate, limiter
- EQ: eq_band, highpass, lowpass
- Spatial: pan, stereo_width
- Sends: reverb_send, delay_send, echo_send
- Modulation: tremolo, auto_pan, chorus, flanger, phaser
- Distortion: drive

### 5. HTTP endpoints
- 11 endpoints across track-effects, effect-curves, send-buses, track-sends, frequency-labels
- Integration with `cache_invalidation` (range-based, matching task-38 pattern)

### 6. Macro Panel UI
- New `MacroPanel.tsx` registered in existing `PanelLayout` system
- Grid ↔ list view modes
- Per-knob: label, value readout, arm circle (red/grey), enable power-button (blue), visibility eye
- 270-315° knob sweep; grid-size slider (48-200px tiles)

### 7. Inline timeline curves
- Shared `<InlineCurveEditor>` extracted from `TransitionPanel.tsx`
- Diamond keyframes, multi-select + drag, double-click delete, right-click easing-cycle
- Deterministic neon-palette color assignment per `(effect_type, param_name)`

### 8. Touch-record
- Per-knob state machine (idle → armed → recording → armed)
- 33 Hz sampling during gesture
- Bezier-fit simplification with 2% tolerance
- Replace-semantics on commit; one undo unit per pass

### 9. Frequency labels
- 8 built-in spectrum bands + 11 instrument presets
- Per-project custom labels via `project_frequency_labels`

### 10. P2: Copy-paste automation
- Multi-select keyframes across curves + tracks
- `trackDelta`-aware paste (reuses M10 pattern)
- Cross-track validity filter (same effect_type + param_name only)

---

## Success Criteria

- [ ] All 15 effect types work end-to-end: add via HTTP, configure static params, animate dynamic params, enable/disable, reorder, remove
- [ ] All 29 named tests in the spec pass (13 Base + 16 Edge)
- [ ] User can record a filter-cutoff sweep with touch-record and hear it back identically on replay
- [ ] Multi-knob simultaneous recording produces independent curves per knob
- [ ] Disabled effect is bypassed in audio; curves preserved
- [ ] Reverb bus with bundled IR produces audible character; send level animates smoothly
- [ ] Inline timeline curves render / hide via eye-toggle; full keyframe editing works
- [ ] Multi-select + Ctrl+C/V copies automation across tracks with correct `trackDelta`
- [ ] Effect chain + curves persist across restart (SQLite migration round-trip)
- [ ] Macro Panel grid ↔ list toggle preserves track selection
- [ ] EQ band knob dropdown lists 8 spectrum bands + 11 instrument presets + any custom labels

---

## Non-goals

- Full pitch-shift / time-stretch on tracks (deferred; detune-only for clips via external lib in a future milestone)
- LFO / modulation routing on top of curves
- VST / external plugin support (permanent non-goal)
- MIDI controller binding (P5, future)
- Algorithmic reverb (rejected; ConvolverNode + multi-bus covers it)
- Character-crossfade distortion (users layer instead)
- Global cross-project frequency labels (P3, future)
- User-configurable curve colors in preferences (P4, future)
- Interactive Tutorial Panel (separate spike)
- Per-clip effect chains (per-track only)
- Unifying volume curves with the new system (volume stays separate)

---

## Task Ordering

```
T45 schema + migrations ─────┬───→ T47 audio graph + buses + IR ──┬───→ T48 curve scheduling ──┬──→ T55 touch-record ──┐
T46 effect registry + labels ┘   ├───→ T49 dynamics + EQ            │                              ├──→ T54 MacroPanel ──┬──→ T56 copy-paste (P2)
                                  ├───→ T50 spatial + send            │                              │                     │
                                  └───→ T51 modulation + drive         │                              │                     │
                                                                       │                              │                     │
                                  T52 HTTP endpoints ──────────────────┘                              │                     │
                                                                                                      │                     │
                                  T53 InlineCurveEditor extraction + audio inline ─────────────────────┘                     │
                                                                                                                              ▼
                                                                                                                          M13 done
```

- T45 + T46 = foundations (parallel)
- T47 needs T45+T46; T48 needs T47
- T49/T50/T51 (effect implementations) parallel after T46+T47
- T52 (backend endpoints) parallel after T45
- T53 (InlineCurveEditor) parallel, no deps
- T54 (MacroPanel) needs T46+T53
- T55 (touch-record) needs T48+T54
- T56 (copy-paste) needs T53+T54 — marked P2

---

## Related

- [local.effect-curves-macro-panel (spec)](../specs/local.effect-curves-macro-panel.md)
- [local.effect-curves-macro-panel (design)](../design/local.effect-curves-macro-panel.md)
- clarification-7 (gitignored decision record)
- Existing: `scenecraft/src/lib/audio-mixer.ts`, `scenecraft/src/components/editor/AudioPropertiesPanel.tsx`, `scenecraft/src/components/editor/TransitionPanel.tsx`
- M10 `trackDelta` copy-paste machinery (reused in T56)
