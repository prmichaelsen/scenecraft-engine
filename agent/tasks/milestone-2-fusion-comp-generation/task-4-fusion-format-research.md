# Task 4: Fusion .setting Format Research

**Milestone**: [M2 - Fusion Comp Generation](../../milestones/milestone-2-fusion-comp-generation.md)
**Design Reference**: [Requirements](../../design/requirements.md)
**Estimated Time**: 3 hours
**Dependencies**: None (can run in parallel with M1)
**Status**: Not Started

---

## Objective

Reverse-engineer the DaVinci Resolve Fusion .setting file format by exporting reference compositions and documenting the structure for Transform, BrightnessContrast, and Glow nodes with keyframes.

---

## Context

Fusion .setting files are the comp interchange format — they contain serialized Fusion node graphs. The format is undocumented by Blackmagic, so we must reverse-engineer it from exported examples. This research directly informs the generator implementation in Task 5.

---

## Steps

### 1. Export Reference Compositions from Resolve

Create simple Fusion comps in Resolve and export as .setting:
- A single Transform node with 2-3 zoom keyframes
- A single BrightnessContrast node with brightness keyframes
- A Glow node with intensity keyframes
- A comp with multiple connected nodes
- A comp with different spline/interpolation types

### 2. Analyze .setting File Structure

Document:
- File header/envelope format
- Node definition syntax
- Input/output connection format
- Keyframe representation
- Spline type identifiers (linear, ease-in, ease-out, cubic)
- Time/frame coordinate system

### 3. Document Node Schemas

For each node type (Transform, BrightnessContrast, Glow):
- Required fields
- Keyframeable parameters
- Default values
- Connection points (inputs/outputs)

### 4. Create Reference Document

Write findings to `agent/design/fusion-setting-format.md`:
- File structure overview
- Node type schemas
- Keyframe format
- Spline types
- Example snippets

### 5. Create Minimal Test Fixture

Write a minimal valid .setting file by hand and verify it imports into Resolve.

---

## Verification

- [ ] At least 3 reference .setting files exported from Resolve
- [ ] File format documented with node structure
- [ ] Keyframe format understood and documented
- [ ] Spline/interpolation types identified
- [ ] Hand-written minimal .setting file imports into Resolve
- [ ] Design document created at agent/design/fusion-setting-format.md

---

## Notes

- If Resolve is not available for export, search for .setting file format documentation online or in Fusion scripting references
- The Fusion scripting guide and VFXPedia may have partial format documentation
- .setting files appear to use a Lua-table-like serialization format

---

**Next Task**: [Task 5: Fusion Comp Generator](task-5-fusion-comp-generator.md)
