# Task 9: Spline Curves & Polish

**Milestone**: [M3 - Effect Library & Intelligence](../../milestones/milestone-3-effect-library.md)
**Design Reference**: [Requirements](../../design/requirements.md)
**Estimated Time**: 2 hours
**Dependencies**: Task 5, Task 7
**Status**: Not Started

---

## Objective

Implement proper Fusion spline interpolation types for smooth, natural-looking keyframe transitions. Replace linear on/off keyframes with shaped attack/decay curves.

---

## Steps

### 1. Research Fusion Spline Types

From Task 4 research, identify available interpolation modes:
- Linear
- Cubic (Bezier handles)
- Flat (hold)
- Smooth (auto-tangent)

### 2. Implement Spline Serialization

In `keyframes.py`:
- Extend `Keyframe` class with tangent/handle data
- Serialize spline control points in .setting format
- Support at minimum: linear, smooth, flat

### 3. Update Effect Presets with Curves

- Each preset specifies its curve type
- zoom_pulse: smooth (natural bounce)
- flash: linear attack, smooth release
- glow_swell: smooth both ways
- hard_cut: flat (instant snap)

### 4. Add Overshoot/Bounce Option

- Optional overshoot: peak slightly past target, then settle
- Adds organic feel to zoom effects
- `--overshoot` flag (default: off)

### 5. Quality Comparison

- Generate two .setting files: one with linear keyframes, one with spline curves
- Visually compare in Resolve to confirm improvement

---

## Verification

- [ ] Spline data serializes correctly in .setting format
- [ ] Smooth curves visible in Resolve's spline editor
- [ ] Different presets use different interpolation types
- [ ] Overshoot option produces visible bounce effect
- [ ] Generated effects look noticeably better than linear

---

**Next Task**: [Task 10: Resolve Script Packaging](../../tasks/milestone-4-resolve-integration/task-10-resolve-script.md)
