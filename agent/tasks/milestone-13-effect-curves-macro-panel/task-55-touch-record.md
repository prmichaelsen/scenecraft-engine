# Task 55: Touch-record state machine + sampling + bezier-fit

**Milestone**: [M13 - Effect Curves + Macro Panel + Touch-Record](../../milestones/milestone-13-effect-curves-macro-panel.md)
**Spec**: [local.effect-curves-macro-panel](../../specs/local.effect-curves-macro-panel.md) — R20-R27
**Estimated Time**: 6 hours
**Dependencies**: T48 (curve scheduling), T54 (MacroPanel knobs)
**Status**: Not Started
**Repository**: `scenecraft` (frontend)

---

## Objective

Implement the per-knob state machine that records a curve when the user gestures a knob during playback, simplifies the raw samples via bezier fitting at 2% tolerance, and commits as one undo unit.

---

## Steps

### 1. State machine per knob

Per-knob state: `idle → armed → recording → armed` (no auto-disarm after commit).

Managed locally in `useTouchRecord(effectId, paramName, param)` hook:

```ts
export function useTouchRecord(
  projectName: string,
  effectId: string,
  paramName: string,
  paramSpec: EffectParamSpec,
): {
  armState: 'idle' | 'armed' | 'recording'
  setArmState: (s: 'idle' | 'armed') => void
  onGestureStart: () => void            // called by Knob on mousedown
  onGestureChange: (normalizedValue: number) => void  // called during drag
  onGestureEnd: () => void              // called on mouseup
}
```

Transitions:
- `onGestureStart`:
  - If `playing && armState === 'armed'` → transition to `recording`, init sample buffer, start rAF sampling
  - If `!playing` → direct edit mode (commit on gestureEnd as a single curve edit at playhead)
- `onGestureChange(value)`:
  - In `recording`: live audible feedback via mixer `setTargetAtTime`; rAF sampler writes `[audioCtx.currentTime, value]` to the buffer
  - In direct edit mode: update current value only (no sampling)
- `onGestureEnd`:
  - If `recording`: commit buffer → simplify → POST update → undo-push → transition back to `armed`
  - If direct edit: commit single point at current playhead → POST update → undo-push
- Global playback stop: implicit `onGestureEnd` if currently recording

### 2. Sampler (rAF throttled ~30ms)

Inside the `recording` branch, a rAF loop pushes samples to the buffer. Throttle via a time-since-last-sample check:

```ts
let lastSampleTime = 0
const rAFLoop = () => {
  if (armState !== 'recording') return
  const now = performance.now()
  if (now - lastSampleTime >= 30) {
    buffer.push([audioCtx.currentTime - recordStartAudioCtxTime, currentKnobValue])
    lastSampleTime = now
  }
  requestAnimationFrame(rAFLoop)
}
```

Target ~33Hz (30ms interval) per R23.

### 3. Bezier-fit simplification on commit

`src/lib/curve-simplification.ts`:

```ts
export function bezierFitSimplify(
  rawSamples: CurvePoint[],
  tolerance: number,  // 0.02 = 2% of knob range
): CurvePoint[]
```

Algorithm (simpler variant of Schneider's bezier fitting):
1. Process the raw samples in windows of 6.
2. Fit a cubic bezier curve to each window (4 control points: 2 anchors + 2 handles).
3. Compute max error between fit and raw samples in the window.
4. If error < tolerance: keep only the 2 anchors (drop intermediate samples).
5. If error >= tolerance: split window in half, recurse.
6. Concatenate surviving anchors into the final point list.

Simpler alternative for v1: Douglas-Peucker on time-value pairs with linear interpolation error metric. Less optimal compression but easier to implement and tolerable for our use case.

### 4. Commit logic

On commit:
1. Call `bezierFitSimplify(buffer, 0.02)` → simplified points
2. Load existing curve for `(effect_id, param_name)` from DB (may be empty)
3. Strip existing points in `[recordStart_t, recordEnd_t]` (replace semantics per R24, Option A default)
4. Merge simplified points into the remaining existing points
5. POST `/effect-curves/:id` or `/effect-curves` with the merged points array
6. Push one undo entry: `{ type: 'curve-update', curve_id, before_points, after_points }`
7. Transition state back to `armed`
8. Fire `mixer.record-committed` event

### 5. Integration with MacroPanel

In the `<MacroKnobTile>` from T54, wire up:

```tsx
const {
  armState, setArmState,
  onGestureStart, onGestureChange, onGestureEnd,
} = useTouchRecord(projectName, effect.id, param.name, paramSpec)

<Knob
  value={currentValue}  // existing sampled-from-curve-at-playhead
  onGestureStart={onGestureStart}
  onChange={onGestureChange}
  onGestureEnd={onGestureEnd}
/>
<ArmCircle armed={armState !== 'idle'} onClick={() => setArmState(armState === 'idle' ? 'armed' : 'idle')} />
```

### 6. Disable / unmount handling

- When playback stops mid-recording: implicit gestureEnd (commit)
- When component unmounts mid-recording: implicit gestureEnd (commit)
- When effect is disabled mid-recording: implicit gestureEnd (commit) + bypass effect
- When project unloads mid-recording: implicit gestureEnd (commit) + standard cleanup

### 7. Direct-edit path (paused)

When `!playing` and user drags a knob:
- Don't enter recording state
- On gestureEnd, compute the existing curve value at current playhead, compare to gesture value — if different, update the curve with a single-point replacement at playhead OR add a new keyframe
- Push one undo entry

### 8. Undo integration

Use the existing scenecraft undo/redo machinery. Each commit (record pass OR direct edit OR static knob change) is ONE unit.

### 9. Tests

`src/hooks/__tests__/useTouchRecord.test.ts`:
- State transitions: idle → armed → recording → armed
- Gesture while paused edits directly, no recording
- Multi-arm parallel recording: two hooks recording simultaneously produce independent curves
- Bezier fit on a known ramp produces ≤4 points
- Playback stop during recording commits buffer
- Effect disable during recording commits buffer + enables bypass
- Undo reverts to exact pre-pass state

`src/lib/__tests__/curve-simplification.test.ts`:
- Known 100-sample straight ramp simplifies to 2 points (just endpoints)
- Random walk with 2% tolerance drops ~80% of input
- Single-point or empty input returns unchanged

---

## Undo during active recording (spec R29a — new per proofing pass)

If the user triggers Ctrl+Z while a knob is in the `recording` state, the implementation MUST:

1. **Commit the in-flight gesture first** — same path as mouseup / playback-stop: flush the sample buffer, bezier-fit, POST, push one undo unit with the committed curve state.
2. **Then execute the undo** — pops that just-committed state off the stack, restoring the pre-gesture curve state.
3. **Knob stays `armed`** — state transitions `recording → armed`, not `recording → idle`.
4. **Redo (Ctrl+Y) replays the gesture** — since the commit is a normal undo unit, redo restores the committed curve.

**Forbidden**: silently discarding gesture samples on undo-during-record. Users expect every gesture to be reversible; a silent discard would be invisible to redo and confusing. The commit-then-revert pattern preserves the reversibility contract (spec test `undo-during-recording-commits-then-reverts`).

**Sampling rate floor** (spec R23 clarified): target 33 Hz via rAF; the ≥30 Hz floor means no consecutive samples may be more than ~100ms apart even under frame jitter. Mock rAF in tests to assert sample-count falls within 80-110 for a 3-second gesture (spec test `recording-samples-at-33hz-target`).

---

## Verification

- [ ] Arming a knob + playing + dragging produces an audible swept curve
- [ ] Release mouse commits; curve persists in DB; undo reverts
- [ ] Multi-arm on 3 knobs during one playback produces 3 independent curves
- [ ] Drag while paused edits curve directly at playhead
- [ ] Stop during recording commits partial gesture
- [ ] Disable effect mid-record commits
- [ ] Bezier simplification reduces 30+ raw samples to ≤10 on smooth curves
- [ ] **Ctrl+Z mid-record commits gesture, then reverts** (spec R29a, test `undo-during-recording-commits-then-reverts`)
- [ ] **Sampling rate ≥30 Hz, target 33 Hz** (spec R23, test `recording-samples-at-33hz-target`)
- [ ] Tests pass

---

## Notes

- rAF sampling during recording touches the React render cycle minimally — the knob's displayed value updates via parent state, but the sample buffer writes are ref-based (not state) to avoid re-renders per frame.
- `audioCtx.currentTime` is the authoritative clock during recording; wall-clock `performance.now()` is only for rAF throttling.
- Consider a visual "recording…" pulse on the arm circle during active gesture (separate from armed vs recording state visual — maybe pulse opacity).
- Bezier fit algorithm: start with Douglas-Peucker for simplicity; upgrade to Schneider's bezier fit in a follow-up if curve shape quality is lacking.
- Undo entries need to carry enough state to round-trip: `{ curve_id, before_points, after_points, interpolation_before, interpolation_after }`.
