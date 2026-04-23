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
- [ ] Grid-size slider resizes tiles smoothly (spec test `macro-panel-size-slider-scales-tiles`)
- [ ] Grid ↔ list toggle preserves track selection (spec test `macro-panel-grid-list-toggle`)
- [ ] List mode displays all params as table rows
- [ ] Remove-effect button confirms then deletes
- [ ] **Bus sub-panel** (spec R36a): add / remove / rename / edit-static-params / reorder buses work; each action is one POST + one undo unit (spec test `bus-subpanel-crud`)
- [ ] Tests pass

---

## Bus sub-panel (spec R36a — new per proofing pass)

The Macro Panel exposes a dedicated **Buses** sub-panel reachable from the panel header button. Behavior:

- Lists rows from `project_send_buses`, ordered by `order_index`
- **Add bus**: picker for `bus_type` (reverb / delay / echo) + default `static_params` → POST `/send-buses`
- **Remove bus**: confirm dialog → DELETE `/send-buses/:id` (cascades to `track_sends` + clears `__send` curves)
- **Rename bus**: inline edit → POST `/send-buses/:id` with new `label`
- **Edit static params**: expand row for reverb IR dropdown (with custom-from-pool option per spec R54), delay time/feedback, echo time/tone
- **Reorder**: drag-and-drop or up/down arrows → POST `/send-buses/:id` with new `order_index` (server handles collision atomically per R_V1)

Each of these is exactly one POST + one undo unit (spec R25 + R36a). Reuse the sub-panel container pattern from any existing expandable sub-panel in the editor.

---

## UI-Structure Test Strategy (per spec)

Per spec §UI-Structure Test Strategy, this task ships two tiers of verification:

**Logic-level tests** (required): vitest + happy-dom covering:
- `macro-panel-grid-list-toggle` (R30)
- `macro-panel-size-slider-scales-tiles` (R31)
- `bus-subpanel-crud` (R36a)
- Panel state ephemerality on remount (R36 — spec test `panel-layout-state-not-persisted`)

**Visual-structure items** (manual + PR-review verification until visual-regression tooling lands):
- R32 knob tile contents (arm + enable + eye + knob layout)
- R33 knob widget 270-315° sweep angle
- R34 native-unit numeric readout formatting
- R41 stacked-curve ~50% alpha overlap
- R42 deterministic color palette by (effect_type, param_name)

Include a manual-verification checklist in the PR description for the visual items; don't block merge on them lacking tests.

---

## Notes

- Existing `AudioPropertiesPanel.tsx` is the closest template; copy its EditorStateContext pattern and workspace-layout registration.
- `<Knob>` component reuse: if any other UI in the codebase needs a knob, generalize this now; otherwise keep it local.
- Drag-reorder: use an existing lib if one's already in the codebase (check `package.json`), else keep it simple (up/down arrow buttons on each effect header).
- Precision drag: `shift+drag` should give finer control; test that this feels right in practice.
- Per project memory, scenecraft has no frontend tests yet; install vitest + happy-dom as part of this task for the logic-level tests above.
