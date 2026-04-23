# Task 51: Modulation + distortion effect implementations

**Milestone**: [M13 - Effect Curves + Macro Panel + Touch-Record](../../milestones/milestone-13-effect-curves-macro-panel.md)
**Spec**: [local.effect-curves-macro-panel](../../specs/local.effect-curves-macro-panel.md) — R7-R9
**Estimated Time**: 6 hours
**Dependencies**: T46 (registry), T47 (graph)
**Status**: Not Started
**Repository**: `scenecraft` (frontend)

---

## Objective

Implement the 6 effect-type `build()` factories: `tremolo`, `auto_pan`, `chorus`, `flanger`, `phaser`, `drive`.

---

## Steps

### 1. LFO helper

Create `src/lib/audio-effects/lfo.ts`:

```ts
export function createLFO(ctx: AudioContext, rate: number, depth: number): {
  output: AudioNode,
  setRate: (hz: number, when?: number) => void,
  setDepth: (amount: number, when?: number) => void,
  dispose: () => void,
}
```

Implementation: `OscillatorNode` (sine, default) → `GainNode` (depth). Output is the gain node — sums with a DC bias elsewhere to modulate a target param.

### 2. Tremolo

`src/lib/audio-effects/tremolo.ts`:
- `GainNode` as volume modulator
- LFO modulates `gain.value` via `offset + depth * sin(2π·rate·t)`
- Technique: `lfo.output → gainNode.gain` connection (WebAudio allows AudioNode → AudioParam); set gainNode's base value via an `OfflineAudioContext` DC buffer OR via setting `gain.value = 1` and summing an LFO with output range `[-depth, +depth]` that centers around 1
- Animatable: `depth` (range 0-1)
- Static: `rate` (Hz, 0.1-20)

### 3. Auto-pan

Same pattern as tremolo but modulates a `StereoPannerNode.pan`:
- `StereoPannerNode` with pan driven by LFO
- LFO output range: `[-depth, +depth]` mapped to pan
- Animatable: `depth`
- Static: `rate`

### 4. Chorus

`src/lib/audio-effects/chorus.ts`:
- `DelayNode` with a small delay time (~20ms) modulated by an LFO (~0.5-2 Hz)
- Mixed with dry signal via a wet/dry gain pair
- Animatable: `depth` (LFO amplitude on delay time), `feedback` (0-0.95), `wet` (0-1)
- Static: `rate`

Routing:
```
input → dry_gain → output
input → delay → feedback_gain → delay (loop)
        delay → wet_gain → output
LFO → delay.delayTime  (modulates around base ~20ms)
```

### 5. Flanger

Same as chorus but with shorter delay (~1-10ms) and higher feedback (0-0.95):
- Animatable: `depth`, `feedback`, `wet`
- Static: `rate`

### 6. Phaser

Different topology — cascade of all-pass filters with cutoff modulated by LFO:
- 4 or 6 `BiquadFilterNode`s with `type: 'allpass'`, in series
- LFO modulates each allpass `frequency` (staggered for depth)
- Mixed with dry via wet/dry
- Animatable: `depth`, `feedback`, `wet`
- Static: `rate`

### 7. Drive / saturation

`src/lib/audio-effects/drive.ts`:
- `WaveShaperNode` with a distortion curve
- Animatable: `amount` (0-1, controls curve steepness), `wet` (0-1)
- Static: `character` — selector among:
  - `'tape'` — gentle cubic (`y = 1.5x - 0.5x³`)
  - `'tube'` — asymmetric (stronger on positive half)
  - `'transistor'` — hard-clipped tanh
  - `'fuzz'` — heavy clipping with high-order terms
  - `'bit-crush'` — quantize to N bits (animated via `amount`? probably not — stick to static bit depth for v1)

Each character is a different `Float32Array` curve passed to `WaveShaperNode.curve`.

Routing:
```
input → drive_amount_gain (pre-gain based on amount) → waveShaper → wet_gain → output
input → dry_gain → output  (parallel passthrough at 1-wet)
```

### 8. Static-params change handling

`character` on drive and `rate` on LFO-based effects are static per `EffectTypeSpec.params` (animatable:false). Changing them requires the mixer to dispose and rebuild that effect. The chain builder handles this automatically when it rebuilds on `static_params` change.

### 9. Tests

One test file per effect under `src/lib/audio-effects/__tests__/`:
- LFO produces expected output range at known time points
- Tremolo depth curve audibly sweeps amplitude modulation
- Auto-pan depth curve audibly sweeps L-R modulation
- Chorus, flanger, phaser: feedback + wet curves behave
- Drive with different character presets produces audibly different distortion
- All `dispose()` calls disconnect all internal nodes

---

## Verification

- [ ] Tremolo at depth=0 is passthrough; at depth=1 produces full-amplitude tremolo
- [ ] Auto-pan at depth=1 sweeps fully L-R
- [ ] Chorus produces thickening without audible echo (short delay + small feedback)
- [ ] Flanger produces "whoosh" sweep
- [ ] Phaser produces swirling harmonic notches
- [ ] All 4 drive characters sound distinct
- [ ] Tests pass

---

## Notes

- LFO-based effects: the trick is routing `LFO_GainNode.output → targetAudioParam`. WebAudio allows `AudioNode → AudioParam` connection; the AudioParam's value becomes `nominal + sum(connected_inputs)`.
- Phaser is the most complex — 4-6 allpass stages with staggered LFO modulation. Consider shipping with fewer stages (2-4) for v1 if implementation gets hairy.
- Drive curves: precompute the 4 `Float32Array` curves at module load; share across all drive instances.
- Static `rate` on LFOs: OscillatorNode.frequency IS animatable in WebAudio, but we expose it as static per the spec's uniform rule (rate sweeps are niche). If a future user requests it, flip the `animatable` flag in the registry and the scheduling just works.
