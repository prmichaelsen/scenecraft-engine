# Task 54: MacroPanel component — grid + list, knobs, arm/enable/visible

**Milestone**: [M13 - Effect Curves + Macro Panel + Touch-Record](../../milestones/milestone-13-effect-curves-macro-panel.md)
**Spec**: [local.effect-curves-macro-panel](../../specs/local.effect-curves-macro-panel.md) — R28-R36
**Estimated Time**: 10 hours
**Dependencies**: T46 (registry), T53 (InlineCurveEditor for visibility toggle rendering)
**Status**: Not Started
**Repository**: `scenecraft` (frontend)

---

## Objective

Build the new MacroPanel UI: grid or list view of per-knob controls (label, arm circle, power button, eye icon, knob widget) for every animatable param on every effect on the selected track.

---

## Steps

### 1. Component tree

`src/components/editor/MacroPanel.tsx`:

```tsx
export function MacroPanel() {
  const { selectedAudioTrackId } = useEditorState()
  const [viewMode, setViewMode] = useState<'grid' | 'list'>('grid')
  const [tileSize, setTileSize] = useState(96)  // px

  const track = ...  // fetch from editor data
  const effects = ...  // fetch from backend via audio-client

  if (!selectedAudioTrackId) return <EmptyState message="Select an audio track to see its effects" />

  return (
    <div className="h-full flex flex-col">
      <MacroPanelHeader viewMode={viewMode} onViewModeChange={setViewMode}
                       tileSize={tileSize} onTileSizeChange={setTileSize} />
      <MacroPanelBody trackId={selectedAudioTrackId} effects={effects}
                      viewMode={viewMode} tileSize={tileSize} />
    </div>
  )
}
```

### 2. Registration

Register `MacroPanel` in `EditorPanelLayout.tsx` following the `AudioPropertiesPanel` pattern. Default workspace layout: MacroPanel docked alongside AudioPropertiesPanel as a sibling tab in the right sidebar.

### 3. MacroPanelHeader

Controls:
- View-mode toggle button (grid ↔ list icon; clicking swaps)
- Grid-size slider (visible only in grid mode): 48px min, 200px max, step 8px
- "+ Add Effect" dropdown listing all 15 effect types grouped by category. On selection: POST `/track-effects` with the default static params.

### 4. MacroPanelBody — grid mode

For each effect in the track's chain (in `order_index` order):
- Section header: effect label + enable toggle (affects ALL knobs of that effect) + remove button + drag-reorder handle
- Grid of `<MacroKnobTile>` — one tile per animatable param in the effect's spec

`<MacroKnobTile>`:
```tsx
<div className="flex flex-col items-center p-1" style={{ width: tileSize, height: tileSize * 1.5 }}>
  <div className="text-[10px] text-gray-300 text-center mb-1">{paramLabel}</div>
  <Knob
    value={currentValue}
    range={range}
    scale={scale}
    onChange={handleKnobGesture}  // live + record path (T55)
    size={tileSize * 0.6}
  />
  <div className="flex gap-1 mt-1">
    <ArmCircle armed={armState} onClick={toggleArm} />
    <PowerButton enabled={effectEnabled} onClick={toggleEnable} />
    <EyeIcon visible={curveVisible} onClick={toggleVisible} />
  </div>
  <div className="text-[9px] text-gray-400">{nativeValueDisplay}</div>
</div>
```

### 5. MacroPanelBody — list mode

Table rows, one per animatable param across all effects on the track:

| Effect | Param | Enable | Arm | Slider | Visible |
|---|---|---|---|---|---|
| compressor | threshold | [o] | [○] | ─────●─ | [👁] |
| compressor | ratio | [o] | [○] | ───●─── | [👁] |
| eq_band | gain | [o] | [●] | ─────●─ | [👁] |

Horizontal slider instead of knob; click/drag controls value. Same state underneath.

### 6. `<Knob>` component

`src/components/ui/Knob.tsx`:

```tsx
type KnobProps = {
  value: number  // 0..1 normalized
  range: { min: number; max: number }
  scale: ParamScale
  size: number   // pixel diameter
  onChange: (normalized: number) => void
  onGestureStart?: () => void
  onGestureEnd?: () => void
  color?: string  // arm state color
}
```

Rendering:
- SVG circle with a sweep indicator
- 270-315° sweep: bottom-left = 0, bottom-right = 1
- Mouse drag vertically to adjust (up = increase, down = decrease)
- Scale-to-pixel: 1.0 per 200px of vertical drag (shift-drag for precision: 1.0 per 1000px)
- Fires `onGestureStart` on mousedown, `onGestureEnd` on mouseup — T55 hooks into these

Display the current native value (formatted per scale: "+3.2 dB" for dB, "8.0 kHz" for Hz) above or below the knob.

### 7. Arm / enable / visibility buttons

`<ArmCircle>`: red ring + filled red dot when armed, grey ring + filled grey dot when idle. ~16px.
`<PowerButton>`: power-icon SVG; high-contrast blue (`#4d9eff`) when enabled, grey when disabled. ~16px.
`<EyeIcon>`: open-eye or closed-eye icon based on `visible`. ~16px.

### 8. Value sampling

Each knob's displayed `currentValue` is sampled from the param's curve at the current playhead time:

```tsx
const currentValue = useMemo(() => {
  const curve = curves.find(c => c.param_name === param.name)
  if (!curve || curve.points.length === 0) {
    // No curve: use effect's static "current set value" (from a parallel DB field OR cached on the effect node)
    return effect.current_values[param.name] ?? param.default
  }
  return evaluateCurveAtTime(curve.points, curve.interpolation, currentTime)
}, [curves, currentTime, ...])
```

During `playing`, this re-samples on each `currentTime` change (driven by `useCurrentTime()`). During pause, it reflects the playhead-at-pause value.

### 9. Add / remove / reorder effects

- "+ Add Effect" dropdown in header → POST `/track-effects`
- Remove button on each effect section → DELETE `/track-effects/:id` (confirm via dialog)
- Drag handle → reorder via PATCH `/track-effects/:id` with new `order_index`

### 10. Empty state

When no audio track selected: centered "Select an audio track to see its effects." message.

When selected track has no effects: show "+ Add Effect" dropdown with a large call-to-action.

### 11. Styling

- Dark theme consistent with existing editor panels
- Knob tiles: dark background, subtle border, rounded corners
- Arm circle glow effect when armed (Tailwind `ring-2 ring-red-500 ring-opacity-50`)

### 12. Tests

`src/components/editor/__tests__/MacroPanel.test.tsx` + unit tests for `<Knob>`:
- Knob drag updates value continuously
- Arm circle toggles state on click
- Power button toggles effect enable
- Eye icon toggles curve visibility
- Grid ↔ list view toggle preserves selection
- Tile size slider scales knob dimensions

---

## Verification

- [ ] MacroPanel appears in panel list when selecting an audio track
- [ ] Adding an effect via dropdown updates the panel with a new effect group
- [ ] Knob drag produces audible parameter change
- [ ] Arm circle cycles idle ↔ armed on click
- [ ] Power button enables/disables effect (audibly)
- [ ] Eye icon shows/hides inline timeline curve
- [ ] Value readout updates during playback
- [ ] Grid-size slider resizes tiles smoothly
- [ ] List mode displays all params as table rows
- [ ] Remove-effect button confirms then deletes
- [ ] Tests pass

---

## Notes

- Existing `AudioPropertiesPanel.tsx` is the closest template; copy its EditorStateContext pattern and workspace-layout registration.
- `<Knob>` component reuse: if any other UI in the codebase needs a knob, generalize this now; otherwise keep it local.
- Drag-reorder: use an existing lib if one's already in the codebase (check `package.json`), else keep it simple (up/down arrow buttons on each effect header).
- Precision drag: `shift+drag` should give finer control; test that this feels right in practice.
