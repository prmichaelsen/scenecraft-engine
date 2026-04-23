# Task 48: Curve → AudioParam scheduling engine

**Milestone**: [M13 - Effect Curves + Macro Panel + Touch-Record](../../milestones/milestone-13-effect-curves-macro-panel.md)
**Spec**: [local.effect-curves-macro-panel](../../specs/local.effect-curves-macro-panel.md) — R6, R16-R19, R17
**Estimated Time**: 4 hours
**Dependencies**: T47 (audio graph)
**Status**: Not Started
**Repository**: `scenecraft` (frontend)

---

## Objective

Bridge between `effect_curves.points` (normalized 0..1 bezier/linear/step curves) and WebAudio's `AudioParam` scheduling. Handles initial schedule, seek-aware rescheduling, interpolation modes, and normalized-to-native unit mapping.

---

## Steps

### 1. Unit mappers

Create `src/lib/audio-param-scale.ts`:

```ts
export function normalizedToNative(
  value01: number,
  scale: ParamScale,
  range: { min: number; max: number },
): number {
  switch (scale) {
    case 'linear': return range.min + value01 * (range.max - range.min)
    case 'log':    return range.min * Math.pow(range.max / range.min, value01)  // assumes min > 0
    case 'db':     return range.min + value01 * (range.max - range.min)         // dB is already log-like; UI maps linearly in dB
    case 'hz':     return range.min * Math.pow(range.max / range.min, value01)  // Hz is perceived log
  }
}

export function nativeToNormalized(...): number { /* inverse */ }
```

### 2. Curve evaluator

`src/lib/audio-curve-eval.ts`:

```ts
export function evaluateCurveAtTime(
  points: CurvePoint[],
  interpolation: 'bezier' | 'linear' | 'step',
  t: number,
): number
```

Given a time-sorted `points` array and a query time `t`:
- `step`: return value of the nearest-left point; for `t` before the first point, return first point's value
- `linear`: linearly interpolate between adjacent points
- `bezier`: Catmull-Rom-ish natural cubic spline through the points — one smooth curve passing through each keyframe. Use existing curve evaluation logic in `audio-curves.ts` (`sampleClipDbAtPlayhead`) as a reference pattern.

### 3. Dense sampling for WebAudio

WebAudio's `setValueCurveAtTime` requires a `Float32Array` of evenly-spaced values. Create:

```ts
export function denseSampleCurve(
  points: CurvePoint[],
  interpolation: 'bezier' | 'linear' | 'step',
  startTime: number,
  duration: number,
  sampleRate: number = 100,  // 100 samples/sec plenty for automation
): Float32Array
```

Returns `duration * sampleRate` float32 values.

### 4. Schedule function

```ts
export function scheduleCurveOnParam(
  param: AudioParam,
  points: CurvePoint[],
  interpolation: 'bezier' | 'linear' | 'step',
  scale: ParamScale,
  range: { min: number; max: number },
  audioCtxStartTime: number,
  durationSeconds: number,
): void
```

Logic:
1. `param.cancelScheduledValues(audioCtxStartTime)`
2. Convert each point's value from normalized [0,1] to native via `normalizedToNative`
3. If `interpolation === 'step'`: one `setValueAtTime(nativeValue, audioCtxStartTime + point.time)` per point
4. If `interpolation === 'linear'`: `setValueAtTime(firstValue, startTime)` then `linearRampToValueAtTime(value, startTime + point.time)` per subsequent point
5. If `interpolation === 'bezier'`: `setValueCurveAtTime(Float32Array, startTime, duration)` with dense samples

### 5. Seek handling

Extend the audio-mixer's existing `seek(time: number)` method:
- For each track's active curves: re-run `scheduleCurveOnParam` starting from `time` onward
- Points with `time < seek_time` are dropped (not scheduled; their values are implicit in whatever the first scheduled point sets)
- At the exact seek position, set initial AudioParam value via `setValueAtTime` so there's no ramp-from-wrong-value glitch

### 6. Live-sample during recording

For touch-record (T55), we also need a synchronous sampler: given a curve and a time, return the current normalized value. Use `evaluateCurveAtTime` directly — this is what the Macro Panel knob uses to display its "current value" readout.

### 7. Tests

`src/lib/__tests__/audio-curve-eval.test.ts`:
- Step/linear/bezier all evaluate correctly at boundary and mid-range times
- Empty curve returns 0 / skipped appropriately
- Single-point curve behaves as constant
- `scheduleCurveOnParam` on a mock AudioParam calls the right methods in order
- Seek mid-playback drops earlier points and starts fresh from seek position
- Log/Hz/linear/dB scales round-trip via `normalizedToNative(nativeToNormalized(x)) === x`

Use the existing `sampleClipDbAtPlayhead` test patterns in `audio-curves.ts` as reference.

---

## Verification

- [ ] Scheduling a 5-point linear curve produces audible linear ramps on a test AudioParam
- [ ] Bezier curve evaluates smoothly (no discontinuities at keyframes)
- [ ] Step curve produces hard-step transitions
- [ ] Seek during playback cancels pre-seek schedules and resumes correctly from the new position
- [ ] Normalized-to-native unit conversion is correct for log, linear, dB, Hz scales
- [ ] Tests pass

---

## Notes

- WebAudio's `setValueCurveAtTime` is sample-rate-independent; the `sampleRate` param in `denseSampleCurve` is the *curve* sample rate, not audio sample rate. 100Hz is plenty for automation smoothness.
- Edge case: AudioParam values must be within the param's native range or WebAudio throws. The mappers must clamp before scheduling. Unit tests should verify clamping.
- Performance: for curves with >100 points OR >30s duration, consider lazy/windowed scheduling — only schedule the next 10s at a time, re-schedule as playhead advances. Out of scope for v1 (most curves will have <20 points).
