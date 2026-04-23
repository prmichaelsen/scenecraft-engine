# Task 49: Dynamics + EQ effect implementations

**Milestone**: [M13 - Effect Curves + Macro Panel + Touch-Record](../../milestones/milestone-13-effect-curves-macro-panel.md)
**Spec**: [local.effect-curves-macro-panel](../../specs/local.effect-curves-macro-panel.md) — R7-R9
**Estimated Time**: 4 hours
**Dependencies**: T46 (registry), T47 (graph)
**Status**: Not Started
**Repository**: `scenecraft` (frontend)

---

## Objective

Implement the 6 effect-type `build()` factories for dynamics + EQ: `compressor`, `gate`, `limiter`, `eq_band`, `highpass`, `lowpass`.

---

## Steps

### 1. File layout

Create `src/lib/audio-effects/` dir. One file per effect:
- `compressor.ts`
- `gate.ts`
- `limiter.ts`
- `eq-band.ts`
- `highpass.ts`
- `lowpass.ts`

Each exports a `build(ctx, staticParams): EffectNode` that conforms to the `EffectNode` interface from T46.

Index file `src/lib/audio-effects/index.ts` wires these `build()` functions into the registry (replacing T46's stubs).

### 2. Compressor

`DynamicsCompressorNode` directly:
- `input` = `output` = the `DynamicsCompressorNode` (single-node effect)
- `setParam`:
  - `threshold` → `compressor.threshold`
  - `ratio` → `compressor.ratio`
  - `attack` → `compressor.attack`
  - `release` → `compressor.release`
  - `knee` → `compressor.knee`
- `scheduleCurve`: dense-schedule via T48's `scheduleCurveOnParam`

### 3. Gate

WebAudio has no native gate. Implement as:
- A `GainNode` whose `gain` is modulated by a "sidechain" detector
- For v1 simplicity: no sidechain; approximate gate as a threshold-triggered gain. Compute effective gain per-sample in an `AudioWorkletNode`, OR use a simpler approximation: `WaveShaperNode` with a curve that maps `|signal| < threshold → 0, else → signal`.
- Animatable params: `threshold`, `attack`, `release`, `ratio` (how hard the gate slams shut — 1.0 = full gate)

**Keep it simple for v1**: single `GainNode` driven by a threshold-controlled envelope follower in JS. Use `ScriptProcessorNode` as a stopgap if `AudioWorkletNode` is too heavy for this task (document as a known perf concern).

### 4. Limiter

`DynamicsCompressorNode` with extreme ratio (20:1+), fast attack (0 ms), fast release (50 ms). The "threshold" animatable param is really the ceiling.

### 5. EQ Band

`BiquadFilterNode` with `type: 'peaking'`:
- `input` = `output` = the biquad
- Animatable: `gain`, `frequency`, `Q`
- Static: none for v1

### 6. High-pass filter

`BiquadFilterNode` with `type: 'highpass'`:
- `input` = `output` = the biquad
- Animatable: `frequency`, `Q` (aka resonance)
- Static: none

### 7. Low-pass filter

Same as highpass but `type: 'lowpass'`.

### 8. Universal setParam pattern

Each `build()` returns an `EffectNode` whose `setParam(name, value, when?)`:
- For animatable params: call `setValueAtTime` OR `setTargetAtTime` on the corresponding `AudioParam`
- For static params (if any): error — the caller should rebuild the chain to change static params
- `scheduleCurve(name, points, startTime, duration)`: calls into T48's `scheduleCurveOnParam` with the right `scale` and `range` from the effect's param spec

### 9. dispose()

Each effect's `dispose()`:
- `node.disconnect()` on every internal node
- No `close()` on AudioContext (shared)

### 10. Tests

`src/lib/audio-effects/__tests__/*.test.ts`, one per effect:
- `build()` returns a valid `EffectNode` with `input`/`output`/`setParam`/`dispose`
- `setParam` on an animatable param updates the corresponding AudioParam
- `setParam` on a static param throws
- `dispose` disconnects all internal nodes
- Pass-through behavior verified by mocking a source, running a known sample through, checking output shape

Mock WebAudio via existing test fixtures in the repo.

---

## Verification

- [ ] Adding a compressor to a track produces audible compression on loud signals
- [ ] Threshold curve sweep audibly changes compression engagement point
- [ ] EQ band with +6 dB at 1 kHz audibly boosts 1 kHz content
- [ ] High-pass filter with 200 Hz cutoff removes bass content
- [ ] Low-pass filter with 4 kHz cutoff removes high content
- [ ] Gate with high threshold mutes quiet passages
- [ ] Limiter prevents clipping at 0 dBFS
- [ ] All 6 implementations pass their unit tests

---

## Notes

- Gate implementation is the trickiest. If AudioWorklet adds too much overhead, accept the ScriptProcessorNode hack for v1 with a TODO to migrate later.
- All these effects are thin wrappers over WebAudio native nodes. Each effect file should be <100 lines.
- EQ band gain range default: ±12 dB. Can extend later if users want wider range.
- Compressor-as-limiter pattern: same node, different defaults, different `params` spec in the registry.
