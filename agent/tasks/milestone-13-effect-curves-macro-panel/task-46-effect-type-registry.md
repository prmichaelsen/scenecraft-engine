# Task 46: Effect type registry + param metadata + frequency labels

**Milestone**: [M13 - Effect Curves + Macro Panel + Touch-Record](../../milestones/milestone-13-effect-curves-macro-panel.md)
**Spec**: [local.effect-curves-macro-panel](../../specs/local.effect-curves-macro-panel.md) — R7-R10, R48-R50
**Estimated Time**: 3 hours
**Dependencies**: None (can run in parallel with T45)
**Status**: Not Started
**Repository**: `scenecraft` (frontend)

---

## Objective

Single source of truth for all 15 v1 effect types: their params, param metadata (animatable, range, scale, default, label presets), and the factory signature every effect implementation must follow. Plus built-in frequency label data.

---

## Steps

### 1. Types file

Create `src/lib/audio-effect-types.ts` with the spec's interfaces verbatim:

```ts
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
```

### 2. Registry

Module-level `EFFECT_REGISTRY: Record<string, EffectTypeSpec>` in the same file (or a sibling `audio-effect-registry.ts`). Prepopulate with placeholder specs for all 15 effect types — the `build()` factories can throw `"not implemented in T46"` stubs; actual implementations land in T49-T51.

Each entry lists its params with full metadata. Example:

```ts
compressor: {
  type: 'compressor',
  label: 'Compressor',
  category: 'dynamics',
  params: [
    { name: 'threshold', label: 'Threshold', animatable: true, range: { min: -60, max: 0 }, scale: 'db', default: -24 },
    { name: 'ratio', label: 'Ratio', animatable: true, range: { min: 1, max: 20 }, scale: 'linear', default: 4 },
    { name: 'attack', label: 'Attack', animatable: true, range: { min: 0, max: 1 }, scale: 'linear', default: 0.003 },
    { name: 'release', label: 'Release', animatable: true, range: { min: 0, max: 1 }, scale: 'linear', default: 0.25 },
    { name: 'knee', label: 'Knee', animatable: true, range: { min: 0, max: 40 }, scale: 'linear', default: 30 },
  ],
  build: () => { throw new Error('compressor.build() not implemented') },
}
```

Fill in the same shape for: `gate`, `limiter`, `eq_band`, `highpass`, `lowpass`, `pan`, `stereo_width`, `reverb_send`, `delay_send`, `echo_send`, `tremolo`, `auto_pan`, `chorus`, `flanger`, `phaser`, `drive`.

### 3. Animatable rule

Enforce the uniform rule from R9: every user-facing param is `animatable: true` EXCEPT:
- `drive.character` (discrete selector)
- `*_send.bus_id` (selector)
- `tremolo.rate`, `auto_pan.rate`, `chorus.rate`, `flanger.rate`, `phaser.rate` (LFO rates)
- Reverb bus `ir` / `impulse_response` (selector)

These 9 specific params are `animatable: false`.

### 4. Frequency labels

Create `src/lib/frequency-labels.ts`:

```ts
export const SPECTRUM_BANDS: FrequencyLabelPreset[] = [
  { label: 'sub bass', value: 35, hzRange: [20, 60] },
  { label: 'bass', value: 122, hzRange: [60, 250] },
  { label: 'low-mids / mud', value: 354, hzRange: [250, 500] },
  { label: 'mids', value: 1000, hzRange: [500, 2000] },
  { label: 'presence', value: 2828, hzRange: [2000, 4000] },
  { label: 'attack / upper-mids', value: 5657, hzRange: [4000, 8000] },
  { label: 'sibilance', value: 7348, hzRange: [6000, 9000] },
  { label: 'air', value: 14142, hzRange: [10000, 20000] },
]

export const INSTRUMENT_PRESETS: FrequencyLabelPreset[] = [
  { label: 'Kick body', value: 63, hzRange: [50, 80] },
  { label: 'Kick click', value: 3873, hzRange: [3000, 5000] },
  { label: 'Bass fundamental', value: 126, hzRange: [80, 200] },
  { label: 'Snare body', value: 195, hzRange: [150, 250] },
  { label: 'Snare crack', value: 3873, hzRange: [3000, 5000] },
  { label: 'Vocal warmth', value: 245, hzRange: [200, 300] },
  { label: 'Vocal presence', value: 3162, hzRange: [2000, 5000] },
  { label: 'Vocal sibilance', value: 7348, hzRange: [6000, 9000] },
  { label: 'Guitar body', value: 173, hzRange: [100, 300] },
  { label: 'Guitar bite', value: 1183, hzRange: [700, 2000] },
  { label: 'Hi-hat / cymbals', value: 9798, hzRange: [8000, 12000] },
]
```

`value` = geometric mean of the range (representative single Hz).

Wire these as the default `labelPresets` on the `eq_band.freq` param in the registry.

### 5. Tests

`src/lib/__tests__/audio-effect-types.test.ts`:
- `EFFECT_REGISTRY` contains all 15 expected types
- Every param in every effect has the required fields
- The exact 9 params listed above are `animatable: false`; all others `animatable: true`
- `SPECTRUM_BANDS` has 8 entries, `INSTRUMENT_PRESETS` has 11 entries
- Geometric-mean values are within 1 Hz of expected

---

## Verification

- [ ] All 15 effect types in registry
- [ ] Typecheck clean
- [ ] `build()` stubs throw clear "not implemented" errors (not silent crash)
- [ ] Frequency label data matches spec values
- [ ] Tests pass

---

## Notes

- This task defines the contract. Implementations of `build()` land in T49/T50/T51.
- Frontend file so the UI can import registry + param metadata directly without round-tripping to backend.
- Register with a freeze/lock mechanism later if hot-reload causes instability; for v1 a plain exported const is fine.
