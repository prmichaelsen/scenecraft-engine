# Task 5: Fusion Comp Generator

**Milestone**: [M2 - Fusion Comp Generation](../../milestones/milestone-2-fusion-comp-generation.md)
**Design Reference**: [Requirements](../../design/requirements.md)
**Estimated Time**: 4 hours
**Dependencies**: Task 4 (format research)
**Status**: Not Started

---

## Objective

Build a Python module that generates valid Fusion .setting files with keyframed Transform and BrightnessContrast nodes, consuming a beat map JSON to place keyframes at beat timestamps.

---

## Context

This is the core generation engine. Using the format knowledge from Task 4, we build a Python writer that constructs Fusion node graphs programmatically and serializes them to .setting files. The writer must handle node definitions, connections, keyframe placement, and spline interpolation.

---

## Steps

### 1. Create Fusion Module Structure

```
src/beatlab/fusion/
├── __init__.py
├── setting_writer.py   # Serializes comp to .setting format
├── nodes.py            # Node type definitions (Transform, BC, Glow)
└── keyframes.py        # Keyframe and spline utilities
```

### 2. Implement Setting Writer

`setting_writer.py`:
- `FusionComp` class that holds nodes and connections
- `serialize()` method that produces .setting file content
- Handle the file envelope/header format
- Node positioning in the Fusion flow view

### 3. Implement Node Types

`nodes.py`:
- `TransformNode` — Size (zoom), Center, Angle parameters
- `BrightnessContrastNode` — Gain, Brightness, Contrast parameters
- `GlowNode` — Glow intensity, size parameters
- Each node has `add_keyframe(frame, value)` method

### 4. Implement Keyframe System

`keyframes.py`:
- `Keyframe(frame: int, value: float, interpolation: str)`
- Support interpolation types: linear, ease_in, ease_out, smooth
- `KeyframeTrack` class to manage a sequence of keyframes
- Attack/release helper: given a beat frame, create a keyframe pair (peak at beat, decay after)

### 5. Implement Beat-to-Keyframe Mapper

In `generator.py`:
- Read beat map JSON
- For each beat, create keyframe pairs on appropriate nodes:
  - Transform.Size: 1.0 → 1.1 → 1.0 (zoom pulse)
  - BrightnessContrast.Gain: 1.0 → 1.3 → 1.0 (flash)
- Place attack keyframe at beat frame, release keyframe N frames later

### 6. Test Output

- Generate a .setting file from a sample beat map
- Verify file structure matches reference exports from Task 4
- Import into Resolve (manual test)

---

## Verification

- [ ] FusionComp serializes to valid .setting format
- [ ] Transform node with keyframes produces correct output
- [ ] BrightnessContrast node with keyframes produces correct output
- [ ] Keyframe timing matches beat map frame numbers
- [ ] Generated .setting file imports into Resolve without error
- [ ] Effects are visible on playback at beat positions

---

**Next Task**: [Task 6: End-to-End Pipeline & CLI](task-6-end-to-end-pipeline.md)
