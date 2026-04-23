# Effect Curves + Macro Panel + Touch-Record

**Concept**: Add per-track audio effects with curve automation (P0 tiers: Dynamics, EQ, Spatial, Time-based, Modulation, Distortion), surface all animatable parameters as knobs in a Macro Panel, and let the user record curves live via DAW-style touch-record while playback runs.
**Created**: 2026-04-23
**Status**: Design Specification
**Source**: [clarification-7-effect-curves-macro-panel](../clarifications/clarification-7-effect-curves-macro-panel.md) (Completed)

---

## Overview

Scenecraft's audio pipeline currently supports per-track volume curves via `scenecraft/src/lib/audio-mixer.ts`. This design extends that into a full mixing surface: each track hosts an ordered chain of effects (compressor, EQ, filter, delay, reverb send, chorus, flanger, phaser, tremolo, pan, drive, etc.), and every animatable parameter of every effect can be automated with a curve. A new Macro Panel surfaces those parameters as knobs; users arm knobs and record curves live while the project plays — mirroring Logic's Touch mode, Ableton's Session Automation Record, and Pro Tools' Latch mode.

The visible outcome: a scenecraft user can start playback, twist a "filter cutoff" knob with the mouse, and the sweep shows up as a bezier curve on that track's timeline. Re-play, arm a different knob, add a parallel automation pass. Build a full mix by riding one knob at a time through the music. It's the most-used interaction pattern in modern production.

---

## Problem Statement

- **Audio mixing is flat today**: volume is the only automatable audio parameter. No EQ, no dynamics, no spatial shaping over time. Projects sound static.
- **Effects-as-keyframes doesn't scale**: the existing video effect-curve pattern (per-transition property curves in `TransitionPanel.tsx`) lets users edit curve points one at a time. For audio, users need to *ride* parameters in real time, not click dozens of points — the muscle-memory of gesturing to the music is the feature.
- **Parameter discoverability is poor**: if we add 10 effects with 4 knobs each, without a unified panel users can't find the knobs they want to animate.
- **Frequency-band EQ work needs engineer vocabulary**: raw Hz numbers aren't how mixing is done. Engineers think "add 3dB at 200Hz for kick body" not "set band 2 gain to +3 at frequency 200".

---

## Solution

Three coordinated subsystems:

**1. Effect graph + curve storage** — `track_effects` SQLite table holding per-track effect chains; `effect_curves` table holding CurvePoint[] per `(effect_id, param_name)`. Project-scoped `project_send_buses` table holding the user-configurable list of reverb / delay / echo buses with their static parameters. Schedule + compositor stay out — this is 100% runtime audio-graph work, not render-path work.

**2. WebAudio engine** — `scenecraft/src/lib/audio-mixer.ts` extended. Each track builds an effect chain from its `track_effects` entries, chained through WebAudio built-in nodes (`BiquadFilter`, `DynamicsCompressor`, `ConvolverNode`, `DelayNode`, `StereoPannerNode`, `WaveShaper`, plus small custom nodes for Echo/Tremolo/Chorus/Flanger/Phaser built from `DelayNode` + `GainNode` + `OscillatorNode` LFOs). Curves drive `AudioParam` values via `setValueAtTime` / `setTargetAtTime` scheduling.

**3. UI** — New `MacroPanel.tsx` showing armed/idle/enabled/visible knobs for the selected track, grid or list layout. Touch-record writes keyframes to curves at 33 Hz during gesture, with bezier fit + 2% tolerance simplification. Visible curves render inline on the Timeline as diamond keyframes (shared `<InlineCurveEditor>` extracted from the existing time-remap widget).

**Alternatives rejected:**
- **Per-clip effects** (vs per-track): adds complexity to graph construction, matches few DAWs. Rejected.
- **Insert reverb per track** (vs sends): CPU-wasteful pattern; amateur mixes do this. Rejected in favor of multi-bus sends.
- **Algorithmic reverb** (Freeverb): would make decay/size animatable but loses ConvolverNode's sound quality. Rejected — user prefers ConvolverNode with static IR + multiple buses for character variety.
- **AudioWorklet pitch-shift / time-stretch**: cost too high for v1 (~1 week specialist work or 200-500KB library). Deferred to a future milestone; detune via external lib only for short clips.
- **Unifying volume curve into the new effect-curve system**: volume is heavily used and pre-dates this system; unifying risks regressions for zero user-facing benefit. Rejected.

---

## Implementation

### Data model

```sql
-- Effect instance on a track (ordered chain)
CREATE TABLE track_effects (
    id TEXT PRIMARY KEY,
    track_id TEXT NOT NULL REFERENCES audio_tracks(id) ON DELETE CASCADE,
    effect_type TEXT NOT NULL,           -- 'compressor' | 'gate' | 'limiter' | 'eq_band' | 'highpass' | 'lowpass' | 'pan' | 'stereo_width' | 'reverb_send' | 'delay_send' | 'echo_send' | 'tremolo' | 'auto_pan' | 'chorus' | 'flanger' | 'phaser' | 'drive'
    order_index INTEGER NOT NULL,        -- chain position, 0 = first
    enabled INTEGER NOT NULL DEFAULT 1,  -- power-button state
    static_params TEXT NOT NULL,         -- JSON of set-and-forget knobs (IR choice, character selector, LFO rate, etc.)
    created_at TEXT NOT NULL
);

-- Curves per (effect, animatable param)
CREATE TABLE effect_curves (
    id TEXT PRIMARY KEY,
    effect_id TEXT NOT NULL REFERENCES track_effects(id) ON DELETE CASCADE,
    param_name TEXT NOT NULL,            -- 'threshold' | 'gain' | 'freq' | 'Q' | 'wet' | 'depth' | etc.
    points TEXT NOT NULL,                -- JSON array of [time_seconds, value_0_to_1] pairs
    interpolation TEXT NOT NULL DEFAULT 'bezier',  -- 'bezier' | 'linear' | 'step'
    visible INTEGER NOT NULL DEFAULT 0,  -- eye-toggle state (inline timeline render)
    UNIQUE(effect_id, param_name)
);

-- Project-scoped send buses (default 2 reverb + 1 delay + 1 echo)
CREATE TABLE project_send_buses (
    id TEXT PRIMARY KEY,
    bus_type TEXT NOT NULL,              -- 'reverb' | 'delay' | 'echo'
    label TEXT NOT NULL,                 -- user-editable 'Plate', 'Hall', 'Tape Echo', etc.
    order_index INTEGER NOT NULL,
    static_params TEXT NOT NULL          -- JSON of bus-level parameters (IR for reverb, time+feedback for delay, time+tone for echo)
);

-- Per-track send levels to buses (one row per track × bus)
CREATE TABLE track_sends (
    track_id TEXT NOT NULL REFERENCES audio_tracks(id) ON DELETE CASCADE,
    bus_id TEXT NOT NULL REFERENCES project_send_buses(id) ON DELETE CASCADE,
    level REAL NOT NULL DEFAULT 0.0,     -- 0..1; animatable via a curve with effect_type='__send' + param_name=bus_id
    PRIMARY KEY (track_id, bus_id)
);
```

### Effect type registry

Single source of truth at `scenecraft/src/lib/audio-effect-types.ts`:

```ts
export interface EffectTypeSpec {
  type: string                            // key used in track_effects.effect_type
  label: string                           // human-friendly label
  category: 'dynamics' | 'eq' | 'spatial' | 'time' | 'modulation' | 'distortion' | 'send'
  params: EffectParamSpec[]               // every knob (animatable or static)
  build: (ctx: AudioContext, staticParams: Record<string, unknown>) => EffectNode
}

export interface EffectParamSpec {
  name: string
  label: string                           // 'Threshold', 'Cutoff', 'Wet', etc.
  animatable: boolean
  range: { min: number; max: number }     // in native units (dB, Hz, 0..1, etc.)
  scale: 'linear' | 'log' | 'db' | 'hz'
  default: number
  // For EQ bands that accept a frequency-label preset:
  labelPresets?: Array<{ label: string; value: number; hzRange?: [number, number] }>
}

export interface EffectNode {
  input: AudioNode                        // what the previous effect / source connects TO
  output: AudioNode                       // what the next effect connects FROM
  setParam: (name: string, value: number, when?: number) => void
  dispose: () => void
}
```

**v1 effect types** (category → types):

| Category | Types | Animatable params | Static params |
|---|---|---|---|
| dynamics | `compressor`, `gate`, `limiter` | all (threshold, ratio, attack, release, knee) | — |
| eq | `eq_band` | gain, freq, Q | — |
| eq | `highpass`, `lowpass` | cutoff, Q | — |
| spatial | `pan` | pan | — |
| spatial | `stereo_width` | width | — |
| time | `reverb_send`, `delay_send`, `echo_send` | wet (send level to chosen bus) | bus_id |
| modulation | `tremolo`, `auto_pan` | depth | rate |
| modulation | `chorus`, `flanger`, `phaser` | depth, feedback, wet | rate |
| distortion | `drive` | amount, wet | character (tape/tube/transistor/fuzz) |

"All params animatable" per clarification-7, except genuinely discrete selectors (character, bus_id, IR choice) which are locked static per-effect-instance.

### WebAudio graph construction

For each audio track, the mixer builds the chain once on load and updates it on track_effects change. Chain topology:

```
AudioSource (clip) → volume-curve gain (existing) → [effect_1 → effect_2 → ...] → pan → track-gain → trackSendTap → AudioContext.destination
                                                                                               ↓
                                                                     trackSendTap splits to each (track_sends.level × bus_id)
                                                                                               ↓
                                                          [reverb_bus_1, reverb_bus_2, delay_bus, echo_bus] → destination
```

Send levels animate a GainNode on each `track → bus` path. Bus effect parameters (reverb IR, delay time, echo tone) are STATIC per-bus-instance; users change character by adding more buses with different settings.

### Curve → AudioParam binding

For each `effect_curves` row:
- On graph build: schedule the entire curve to the AudioParam via `param.cancelScheduledValues(0)` + `param.setValueCurveAtTime(float32Array, startTime, duration)`
- On curve edit (user drags a point): re-schedule the affected segment
- On touch-record gesture tick (during playback): call `param.setTargetAtTime(newValue, audioCtx.currentTime, 0.01)` for immediate response while also writing the keyframe to the curve for persistence

### Touch-record state machine

```
States: idle → armed → recording → committed
Transitions:
  click record-circle on knob → armed
  armed + playing + mousedown on knob → recording
  recording + rAF tick → sample knob value at audioCtx.currentTime, append to in-memory sample buffer
  recording + mouseup → committed (flush to DB + undo stack)
  committed → armed (stays armed unless user explicitly disarms)
Constraint: only gestures during playback write to the curve. Gestures while paused edit the curve directly at playhead.
```

Sample buffer flush algorithm (on mouseup):
1. Raw buffer has ~N samples at ~30ms intervals (33 Hz during gesture).
2. Fit cubic bezier segments to successive 6-point windows.
3. Drop intermediate control points whose removal increases fit error by < 2% of knob range.
4. Replace any pre-existing curve points in `[gesture_start_t, gesture_end_t]` with the simplified set.
5. Commit to `effect_curves.points` as a single `postUpdateEffectCurve({effect_id, param_name, points})` call.
6. Push one undo unit.

### Macro Panel component tree

```
<MacroPanel>                              # selection-aware via EditorStateContext
  <MacroPanelHeader>
    <ViewModeToggle />                    # grid ↔ list
    <SizeSlider />                        # grid tile scale (grid mode only)
  </MacroPanelHeader>
  <MacroPanelBody>
    For each effect on the selected track, ordered by track_effects.order_index:
      <EffectGroup effect={...}>
        For each animatable param in the effect:
          <MacroKnob
            label={param.label}
            effect_id={effect.id}
            param_name={param.name}
            value={currentValue}          # sampled from curve at playhead OR last-set value
            range={param.range}
            scale={param.scale}
            armed={bool}                  # red circle when armed/recording
            enabled={effect.enabled}      # blue power-button
            visible={curve.visible}       # eye toggle (renders on timeline)
            onGesture={(v) => ...}        # writes to curve during recording OR sets current value when paused
            onArmToggle={() => ...}
            onEnableToggle={() => ...}
            onVisibleToggle={() => ...}
          />
      </EffectGroup>
  </MacroPanelBody>
</MacroPanel>
```

Layout: grid (default) renders `MacroKnob` tiles in a responsive flex-grid; list renders the same data as table rows (label, enable/disable, arm, slider, visibility).

### Inline timeline curves

Eye-toggle on a knob pushes the curve onto the audio track's Timeline lane. Rendering logic:
- Shared `<InlineCurveEditor>` component extracted from `TransitionPanel.tsx`'s existing curve editor
- Renders curve polyline + diamond keyframes at CurvePoint[] positions
- Handles: drag-single, drag-multi-selected, double-click-delete, right-click cycle easing
- Color assignment: perceptually-uniform distinct palette for common effect/param combos; light blue default for less-common
- Stacking: multiple visible curves on the same track layer vertically with ~50% alpha

### Frequency labels

Static data file at `scenecraft/src/lib/frequency-labels.ts`:

```ts
export const SPECTRUM_BANDS = [
  { label: 'sub bass', range: [20, 60] },
  { label: 'bass', range: [60, 250] },
  { label: 'low-mids / mud', range: [250, 500] },
  { label: 'mids', range: [500, 2000] },
  { label: 'presence', range: [2000, 4000] },
  { label: 'attack / upper-mids', range: [4000, 8000] },
  { label: 'sibilance', range: [6000, 9000] },
  { label: 'air', range: [10000, 20000] },
]

export const INSTRUMENT_PRESETS = [
  { label: 'Kick body', range: [50, 80] },
  { label: 'Kick click', range: [3000, 5000] },
  { label: 'Bass fundamental', range: [80, 200] },
  { label: 'Snare body', range: [150, 250] },
  { label: 'Snare crack', range: [3000, 5000] },
  { label: 'Vocal warmth', range: [200, 300] },
  { label: 'Vocal presence', range: [2000, 5000] },
  { label: 'Vocal sibilance', range: [6000, 9000] },
  { label: 'Guitar body', range: [100, 300] },
  { label: 'Guitar bite', range: [700, 2000] },
  { label: 'Hi-hat / cymbals', range: [8000, 12000] },
]
```

EQ band knob offers a dropdown preset picker alongside the raw Hz input. Custom labels stored at project-scope via new SQLite table `project_frequency_labels`.

### Copy-paste automation (P2 inside M13)

Multi-select keyframes on inline curves → Ctrl+C → Ctrl+V at new playhead position. Cross-track paste uses existing `trackDelta` pattern from M10:
- Clipboard format: `{ sourceTrackIds, relativeOffsets, kfValuesPerCurve, targetEffectType, targetParamName }`
- Paste target: current selected track + playhead time
- Auto-filter clipboard → destination validity (only paste onto same effect_type + param_name curves)
- Musical-reprise workflow: copy verse-1 automation, paste at verse-2 offset, `trackDelta = 0`, `timeDelta = verse2_start - verse1_start`

Build into the same `<InlineCurveEditor>` selection + clipboard machinery. Can ship as last task of M13 since it depends on everything else.

### Stretch / future items (NOT in M13)

- **Interactive Tutorial Panel** with chat-agent context loading — separate spike/milestone. 4 starter tutorials to ship alongside:
  1. Live-ride a filter sweep with touch-record
  2. Layer two drive effects for character crossfade
  3. Automate a reverb send for a pre-chorus swell
  4. Copy-paste automation across musical reprises
- LFO / modulation routing (layer an LFO on top of a curve)
- MIDI-controller binding to knobs (P5)
- VST / plugin support (permanently deferred — out of scope forever)
- Pitch-shift / time-stretch of full tracks
- Character-crossfade distortion (Option B from clarification) — learn the layering idiom first
- Global cross-project frequency labels (P3)
- User-configurable curve colors in prefs (P4)

---

## Key Design Decisions

### Architecture

| Decision | Choice | Rationale |
|---|---|---|
| Effect scope | Per-track chain, not per-clip | Matches pro DAWs; simpler graph; user didn't request per-clip |
| Reverb engine | ConvolverNode (IR-based), decay/size static | User preference; ship bundled IR library; multiple buses for character variety |
| Bus count | User-configurable, default 2 reverb + 1 delay + 1 echo | Resolves "different reverbs on different tracks" without insert waste |
| Echo vs Delay | Separate effect types | Single-tap tape character ≠ rhythmic multi-tap; distinct UX + defaults |
| Volume curves | Stay separate from new system | Pre-existing, heavily used, unification = regression risk, zero user benefit |
| Curve format | Extend existing `CurvePoint = [time, value]` | Reuses all tooling (schema, serialize, render) |
| DB schema | New tables (`track_effects`, `effect_curves`, `project_send_buses`, `track_sends`) | Queryable; embedding JSON on tracks loses this |
| WebAudio backend | Built-ins only for v1 | No AudioWorklet complexity; sufficient for P0 effects |
| Pitch-shift | Deferred; detune-only for clips via external lib | Full pitch-shift = week of specialist work |

### UX

| Decision | Choice | Rationale |
|---|---|---|
| Panel UX | Grid of knob-tiles OR list table, user-toggleable | User-specified layout in clarification-7 Item 2.1 |
| Panel placement | New `MacroPanel` via existing PanelLayout, docked right sidebar default | Follows `AudioPropertiesPanel` pattern; no hard-coded constraint |
| Record mode | Touch only (gesture-triggered); stays armed | Safest + most fluid; user explicitly chose over Latch |
| Pause + adjust | Direct curve edit at playhead (no recording) | Consistent with clicking a keyframe on the timeline |
| Sample rate | 33 Hz during gesture, bezier-fit simplified at 2% tolerance | Imperceptible to human gesture; storage light |
| Default interpolation | Bezier | User-specified; gives natural curves from recorded gestures |
| Visual state | Armed = red circle outline + filled red dot; idle = grey; enabled = blue power button; visibility = eye | User-specified in clarification Item 2.1 |
| Keyframe editing | Matches time-remap: drag, multi-select, double-click delete, right-click easing-cycle | Reuse existing pattern; extract `<InlineCurveEditor>` |
| Curve colors | Neon distinct palette for common effects, light blue default | User pref; P4 user override in preferences |

### Scope

| Decision | Choice | Rationale |
|---|---|---|
| Chorus/Flanger/Phaser | In v1 | Cheap to implement (DelayNode + LFO) |
| All effect params animatable | Uniform rule | User said "all" applies across effects, not just compressor |
| Frequency labels | Ship spectrum-band (8) + instrument-specific (11) sets | Standard mix vocabulary |
| Custom labels | Project-scoped in v1; global P3 | Simpler; shareable via project export |
| Inline automation lanes | In v1 via eye-toggle | Table-stakes for automation UX |
| Copy-paste automation | P2 inside M13 | Powerful for musical-reprise workflows |
| LFO modulation routing | Deferred | Post-v1 enhancement |
| VST plugins, MIDI binding | Deferred | Separate effort; VST likely permanent |

---

## Trade-offs

**Multi-bus vs per-track insert reverb**:
- Multi-bus (chosen): bounded CPU, industry-standard, covers 95% of "different reverbs" needs
- Per-track insert: max flexibility but CPU-wasteful and encourages bad mixing habits

**ConvolverNode static decay vs algorithmic animatable**:
- ConvolverNode (chosen): better sonic quality, standard, static decay is a minor workflow limitation
- Algorithmic (Freeverb): decay animatable, but lower quality; tradeoff not worth it when you can add another bus

**"All params animatable" rule vs narrow animatable set**:
- All animatable (chosen): uniform mental model, no "why can I automate threshold but not attack" confusion
- Narrow: less UI complexity, smaller recording overhead, but adds friction when user wants a rare automation

**Bezier default vs linear default**:
- Bezier (chosen): natural-looking curves from gestures, matches time-remap convention
- Linear: faster to compute, simpler storage — but recorded gestures look mechanical

**Per-track effect chain vs per-clip**:
- Per-track (chosen): simpler graph, matches every pro DAW, matches existing audio-mixer.ts shape
- Per-clip: more flexibility but rarely needed; explosive graph complexity with hundreds of clips

---

## Open Questions (post-M13)

1. How should automation respond to source clip trim? If a curve keyframe sits at project-time 30s and the user trims the clip that contains that time, does the keyframe move, stay, or get orphaned? (Proposed: stay — curves are track-level, not clip-level.)
2. Should effect enable/disable itself be automatable (binary curve)? (Proposed: defer; not commonly needed.)
3. What's the CPU budget for 40 tracks × 5 effects each × realtime automation? (Estimate 4-8% on modern hardware; benchmark during M13 implementation.)
4. Interactive Tutorial Panel state-machine: how does the agent detect the user completing a step to auto-advance? (Defer — full design in separate clarification.)

---

## Related

- [clarification-7-effect-curves-macro-panel](../clarifications/clarification-7-effect-curves-macro-panel.md) — full decision record
- `scenecraft/src/lib/audio-mixer.ts` — existing mixer this work extends
- `scenecraft/src/components/editor/AudioPropertiesPanel.tsx` — pattern for new `MacroPanel`
- `scenecraft/src/components/editor/TransitionPanel.tsx` — existing curve-editor machinery to extract
- M10 `trackDelta` copy-paste machinery — reused for automation keyframe paste
