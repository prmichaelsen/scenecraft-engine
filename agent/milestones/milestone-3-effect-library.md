# Milestone 3: Effect Library & Intelligence

**Goal**: Expand effect variety, add intensity mapping, section detection, and smooth keyframe curves
**Duration**: 1-2 weeks
**Dependencies**: M2 - Fusion Comp Generation
**Status**: Not Started

---

## Overview

This milestone adds the "intelligence" layer that differentiates beat-lab from a simple beat-to-keyframe converter. It includes: multiple effect presets, energy-based intensity mapping (stronger beats → bigger effects), musical section detection (verse vs chorus vs drop), and smooth spline-based keyframe curves.

---

## Deliverables

### 1. Effect Preset Library
- Zoom pulse, brightness flash, glow swell, color shift presets
- Configurable parameters per preset (magnitude, duration, curve)
- Preset selection via CLI flags

### 2. Intensity Mapping
- Map beat strength (0.0-1.0) to effect magnitude
- Configurable intensity curve (linear, exponential, logarithmic)
- Accented beats get proportionally stronger effects

### 3. Section Detection
- Spectral feature analysis to identify musical sections
- Rule-based heuristic classification (verse, chorus, bridge, drop, buildup)
- Different effect styles per section type
- Optional LLM classification (stretch goal)

### 4. Spline-Based Keyframes
- Ease-in, ease-out, ease-in-out curves via Fusion spline types
- Attack/decay profiles per effect type
- Natural-looking transitions instead of linear on/off

---

## Success Criteria

- [ ] At least 4 distinct effect presets available
- [ ] Intensity mapping produces visually different effects for weak vs strong beats
- [ ] Section detection identifies at least verse/chorus/drop sections
- [ ] Keyframe curves look smooth (not robotic/linear)
- [ ] CLI allows preset and section-mode selection

---

## Tasks

1. [Task 7: Effect Preset Library](../tasks/milestone-3-effect-library/task-7-effect-presets.md) - Multiple effect types with configurable parameters
2. [Task 8: Intensity Mapping & Section Detection](../tasks/milestone-3-effect-library/task-8-intensity-and-sections.md) - Energy-based scaling, spectral section classification
3. [Task 9: Spline Curves & Polish](../tasks/milestone-3-effect-library/task-9-spline-curves.md) - Smooth keyframe interpolation, attack/decay profiles

---

## Risks and Mitigation

| Risk | Impact | Probability | Mitigation Strategy |
|------|--------|-------------|---------------------|
| Section detection accuracy limited by heuristics | Medium | Medium | Start with simple energy-based detection; refine iteratively |
| Fusion spline type API unclear | Medium | Medium | Test each spline type via exported .setting files |

---

**Next Milestone**: [M4 - Resolve Integration & Distribution](milestone-4-resolve-integration.md)
**Blockers**: None
