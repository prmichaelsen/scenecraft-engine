# Milestone 6: Sustained Effects & Color Grading

**Goal**: Add section-level sustained effects (hold for duration) and full ColorCorrector support for color grading per section
**Duration**: 1 week
**Dependencies**: M5 - AI Effect Director
**Status**: Not Started

---

## Overview

Current effects are beat-pulse only — they spike on each beat and return to base. This milestone adds sustained effects that hold for an entire section (e.g. "verse is blue-tinted and dark", "chorus is warm and saturated"). It also adds the Fusion ColorCorrector node with full color control: hue shift, saturation, contrast, per-channel gain/lift, and black/white point manipulation.

The combination of beat pulses + sustained section grading enables cinematic looks like "dark moody verse with subtle glow pulses → bright warm chorus with hard flash + zoom."

---

## Deliverables

### 1. Hold Keyframe Mode
- `KeyframeTrack.add_hold(start_frame, end_frame, value, transition_frames)` method
- Smooth transition in at section start, hold value, smooth transition out at section end
- Works alongside existing pulse keyframes on different parameters

### 2. ColorCorrector Node
- Full Fusion ColorCorrector with parameters:
  - MasterGain, MasterLift, MasterGamma, MasterContrast, MasterSaturation
  - MasterHueAngle (hue rotation)
  - GainR/GainG/GainB (per-channel brightness)
  - LiftR/LiftG/LiftB (per-channel black point)

### 3. Sustained Effects in Effect Plan
- `sustained_effects` field on SectionPlan: dict of parameter→value pairs that hold for the section
- Generator creates one ColorCorrector (or other node) per unique sustained param set
- Hold keyframes transition smoothly between sections with different values

### 4. AI Color Grading
- System prompt includes color grading parameters and guidance
- LLM can specify sustained color grades per section alongside beat pulses
- Example: `{"sustained_effects": {"MasterSaturation": 1.3, "GainR": 1.15, "MasterContrast": 0.15, "MasterLift": -0.03}}`

---

## Success Criteria

- [ ] `add_hold()` produces correct keyframes: transition in → hold → transition out
- [ ] ColorCorrector node generates valid .setting output
- [ ] Sustained effects hold for section duration (not beat pulses)
- [ ] Smooth transitions between sections with different sustained values
- [ ] AI mode can combine beat pulses + sustained color grading
- [ ] `--ai --prompt "dark moody verse, bright warm chorus"` produces visible color differences
- [ ] All existing tests pass (no regressions)

---

## Tasks

1. [Task 16: Hold Keyframes & ColorCorrector Node](../tasks/milestone-6-sustained-effects/task-16-hold-keyframes-colorcorrector.md) - Hold mode, ColorCorrector node type
2. [Task 17: Sustained Effects in Generator & Plan](../tasks/milestone-6-sustained-effects/task-17-sustained-generator.md) - SectionPlan schema, generator integration
3. [Task 18: AI Color Grading Prompt](../tasks/milestone-6-sustained-effects/task-18-ai-color-prompt.md) - Update prompt, add color presets

---

## Risks and Mitigation

| Risk | Impact | Probability | Mitigation Strategy |
|------|--------|-------------|---------------------|
| ColorCorrector param names wrong in Fusion | Medium | Medium | Export a CC node from Resolve to verify param names |
| Sustained + pulse keyframes on same node conflict | Medium | Low | Use separate nodes for sustained vs pulse effects |

---

**Blockers**: None
