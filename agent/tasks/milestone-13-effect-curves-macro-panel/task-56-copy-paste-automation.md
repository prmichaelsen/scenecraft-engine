# Task 56: Multi-select + trackDelta copy-paste of automation keyframes (P2)

**Milestone**: [M13 - Effect Curves + Macro Panel + Touch-Record](../../milestones/milestone-13-effect-curves-macro-panel.md)
**Spec**: [local.effect-curves-macro-panel](../../specs/local.effect-curves-macro-panel.md) — R43-R47
**Estimated Time**: 4 hours
**Dependencies**: T53 (InlineCurveEditor with multi-select), T54 (MacroPanel)
**Status**: Not Started
**Priority**: P2 inside M13 — ship last
**Repository**: `scenecraft` (frontend)

---

## Objective

Multi-select automation keyframes across multiple visible curves and tracks, then Ctrl+C / Ctrl+V to copy/paste them to new timeline positions or other tracks using the M10 `trackDelta` pattern.

---

## Steps

### 1. Selection model

Extend the existing keyframe-selection state in `EditorStateContext` (or add a new dedicated context) to support cross-curve/cross-track selection:

```ts
type AutomationKfSelection = {
  curve_id: string
  kf_index: number         // index into curve.points
  track_id: string         // for trackDelta computation on paste
}

// In EditorStateContext:
selectedAutomationKfs: Set<string>  // serialized "curve_id:kf_index" keys
```

### 2. Multi-select UX

In `<InlineCurveEditor>` (T53):
- Shift-click a diamond: toggles its membership in `selectedAutomationKfs`
- Click-drag on empty area starts a box-select; on mouseup, add all diamonds inside the box
- Click a diamond without shift: clears selection, selects only that one
- Selected diamonds visually differ (larger, brighter outline)

### 3. Clipboard format

On Ctrl+C (keyboard handler scoped to the editor):

```ts
type AutomationClipboard = {
  version: 1
  primary_track_id: string  // track of the first selected keyframe
  primary_start_time: number  // min time across selection
  items: Array<{
    effect_type: string
    param_name: string
    track_delta: number       // source track - primary track (0 if same)
    time_offset: number        // kf.time - primary_start_time
    value: number              // normalized 0..1
    interpolation: 'bezier' | 'linear' | 'step'
  }>
}
```

Serialize as JSON. Store in browser clipboard via `navigator.clipboard.writeText` so Ctrl+V works across tabs too.

### 4. Paste at playhead

On Ctrl+V:
- Parse clipboard JSON; validate version
- `paste_track_id = selectedAudioTrackId` (or primary focused track)
- `paste_start_time = playheadTime`
- For each item:
  - Compute `target_track_id = track_with_index(paste_track_id.index + item.track_delta)` — bounds-check; skip if out of range
  - Find the curve on `target_track_id` matching `(effect_type, param_name)`; skip if no such curve exists
  - Append `[paste_start_time + item.time_offset, item.value]` to the curve's points
- Dedupe any collisions (same time keep latest)
- POST `/effect-curves/:id` per curve with the new points arrays
- Push ONE undo unit for the whole paste

### 5. Target validity filter

Only paste onto curves with matching `(effect_type, param_name)`. Mismatches produce no write for that source. Show a user toast: "3 of 5 items pasted; 2 skipped (destination curve not found)" when partial.

### 6. Keyboard shortcuts

Hook into the existing scenecraft keyboard-handler pattern. Scope the Ctrl+C / Ctrl+V to when the MacroPanel or Timeline editor has focus (not when typing in a text input).

Support also Cmd+C / Cmd+V on macOS, and maybe a middle-button-drag gesture later.

### 7. Tests

`src/components/editor/__tests__/automation-copy-paste.test.tsx`:
- Select 2 keyframes on curve A (track T1), Ctrl+C, switch to track T2, Ctrl+V at t=30: T2's curve A gains 2 new points with correct offsets
- Paste onto a track without the source curve type: toast shown, no DB writes for that item
- Multi-track source selection (kfs on T1 and T2 simultaneously): paste on T3 computes trackDelta per item, producing kfs on T3 and T4
- Undo reverts all pasted kfs as one unit

---

## Verification

- [ ] Multi-select via shift-click and box-select works
- [ ] Copy + paste at playhead across tracks produces correctly offset kfs
- [ ] Cross-curve-type paste is filtered with user notification
- [ ] trackDelta math works for paste across multiple tracks simultaneously
- [ ] Undo reverts paste as single unit
- [ ] Tests pass

---

## Notes

- This is the last task in M13 — ship after everything else works.
- Cross-tab clipboard (via `navigator.clipboard`) is nice-to-have; if flaky, fall back to in-memory clipboard that only works within one tab.
- Reuses the M10 `trackDelta` conceptual pattern (which was for transition clips); this applies it to automation keyframes.
- Consider adding a "paste special" dialog later that lets user choose time-offset vs no-offset, target selection filters, etc. Out of scope for v1.
