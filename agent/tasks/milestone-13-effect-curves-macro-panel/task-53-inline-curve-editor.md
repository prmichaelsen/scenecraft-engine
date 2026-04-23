# Task 53: Extract shared `<InlineCurveEditor>` + audio inline curves

**Milestone**: [M13 - Effect Curves + Macro Panel + Touch-Record](../../milestones/milestone-13-effect-curves-macro-panel.md)
**Spec**: [local.effect-curves-macro-panel](../../specs/local.effect-curves-macro-panel.md) — R37-R42
**Estimated Time**: 8 hours
**Dependencies**: None strictly (can extract any time); T48 for curve evaluation integration
**Status**: Not Started
**Repository**: `scenecraft` (frontend)

---

## Objective

Refactor the existing time-remap curve-editor machinery in `TransitionPanel.tsx` into a reusable `<InlineCurveEditor>` component, then mount instances on audio tracks for each visible effect curve.

---

## Steps

### 1. Audit existing curve editor

Read `scenecraft/src/components/editor/TransitionPanel.tsx`:
- Identify the current curve-edit implementation (diamond keyframe rendering, drag handlers, double-click delete, right-click menu)
- Note coupled state (selectedTransition, zoom level, etc.)

### 2. Extract component

Create `src/components/editor/InlineCurveEditor.tsx`:

```tsx
type InlineCurveEditorProps = {
  // Curve data
  points: CurvePoint[]
  interpolation: 'bezier' | 'linear' | 'step'

  // Coordinate system
  xDomain: [number, number]       // time range rendered (e.g., [0, duration])
  yDomain: [number, number]       // value range (typically [0, 1])
  pxPerSec: number                // time scale
  height: number                  // pixel height of the editor strip
  scrollLeft: number              // synced with timeline scroll

  // Visual
  color: string                   // polyline + diamond color
  selectedKfIds?: Set<string>     // multi-select state from parent
  readOnly?: boolean              // disable all interaction

  // Callbacks (parent owns state)
  onPointsChange: (newPoints: CurvePoint[]) => void
  onInterpolationChange?: (interp: 'bezier' | 'linear' | 'step') => void
  onSelectionChange?: (kfIds: Set<string>) => void
}
```

Features:
- Render polyline using T48's `evaluateCurveAtTime` for bezier smoothness
- Render diamond keyframes at each point
- Drag a diamond to move (single or multi, if selected)
- Double-click diamond to delete
- Right-click diamond to cycle `interpolation` via `onInterpolationChange`
- Shift-click or box-select for multi-select

### 3. Migrate `TransitionPanel` to use it

Replace the inline curve-edit code in `TransitionPanel.tsx` with `<InlineCurveEditor>`. Preserve behavior exactly — any regression here is a break in video-transition editing.

### 4. Audio inline curves

In the audio timeline rendering code (existing `AudioTrack.tsx` or similar), mount `<InlineCurveEditor>` instances for each visible curve on the track:

```tsx
{visibleCurves.map((curve) => (
  <InlineCurveEditor
    key={curve.id}
    points={curve.points}
    interpolation={curve.interpolation}
    xDomain={[0, trackDuration]}
    yDomain={[0, 1]}
    pxPerSec={pxPerSec}
    scrollLeft={scrollLeft}
    height={trackHeight * 0.6}  // portion of the track
    color={colorForCurve(curve.effect_type, curve.param_name)}
    onPointsChange={(newPoints) => postUpdateEffectCurve(curve.id, { points: newPoints })}
    onInterpolationChange={(interp) => postUpdateEffectCurve(curve.id, { interpolation: interp })}
  />
))}
```

### 5. Color palette

Create `src/lib/curve-colors.ts`:

```ts
const NEON_PALETTE = [
  '#00ff88', '#ff00aa', '#ffaa00', '#00ccff',
  '#ff4488', '#88ff00', '#aa44ff', '#ff8844',
  // ... 8-12 perceptually-distinct colors
]

const COMMON_COMBOS: Record<string, string> = {
  'compressor:threshold': NEON_PALETTE[0],
  'eq_band:gain': NEON_PALETTE[1],
  'highpass:frequency': NEON_PALETTE[2],
  'lowpass:frequency': NEON_PALETTE[3],
  'reverb_send:wet': NEON_PALETTE[4],
  'pan:pan': NEON_PALETTE[5],
  // etc — the most-used curves
}

const LIGHT_BLUE = '#66ccff'

export function colorForCurve(effectType: string, paramName: string): string {
  const key = `${effectType}:${paramName}`
  if (COMMON_COMBOS[key]) return COMMON_COMBOS[key]
  // Hash fallback for less-common combos, but default to light blue for undifferentiated secondary
  return LIGHT_BLUE
}
```

Deterministic + stable across sessions per R42.

### 6. Multi-curve stacking

When a track has multiple visible curves, stack them with ~50% alpha so overlap is discernible (per R41). Simplest: render each at full height with `opacity: 0.5`; polylines and diamonds blend.

### 7. Tests

`src/components/editor/__tests__/InlineCurveEditor.test.tsx` (mount via testing-library; may need to install vitest per existing gap — see memory `No frontend tests yet`):
- Rendering 3 points renders 3 diamonds in correct pixel positions
- Dragging a diamond fires `onPointsChange` with updated coords
- Double-click fires `onPointsChange` with the clicked point removed
- Right-click cycles interpolation: bezier → linear → step → bezier
- Multi-select + drag moves all selected diamonds by the same delta
- `readOnly` prop disables all interactivity

Video transition parity: no regression in `TransitionPanel.tsx` curve editing.

---

## Verification

- [ ] `<InlineCurveEditor>` renders correctly on audio tracks and video transitions
- [ ] Video transition curve editing still works identically (no regression)
- [ ] Audio effect curves render inline when eye-toggle is on
- [ ] Diamond drag, double-click delete, right-click easing-cycle all work
- [ ] Multi-curve stacking is visually distinguishable
- [ ] Color assignment is deterministic across reloads
- [ ] Tests pass (install vitest if absent)

---

## Notes

- This is the riskiest refactor in M13 because it touches existing video transition UX. Do a visual smoke test of transition-panel curve editing before/after.
- Multi-select + box-select is complex; if time-constrained, ship single-select first and defer multi-select to T56.
- Color palette: consider using d3-scale-chromatic's `interpolateTurbo` for the hash-fallback colors. Adds a dep; skip for v1 if the explicit NEON_PALETTE is sufficient.
- Eye-toggle visibility state is read from `effect_curves.visible`; parent mounts/unmounts `<InlineCurveEditor>` based on it.
