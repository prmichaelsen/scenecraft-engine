# Task 18: AI Color Grading Prompt

**Milestone**: [M6 - Sustained Effects & Color Grading](../../milestones/milestone-6-sustained-effects.md)
**Design Reference**: [AI Effect Director](../../design/local.ai-effect-director.md)
**Estimated Time**: 2 hours
**Dependencies**: Task 17
**Status**: Not Started

---

## Objective

Update the AI system prompt with color grading options and sustained effect instructions. The LLM should be able to specify both beat-pulse effects AND section-level color grades in a single plan.

---

## Steps

### 1. Add Color Parameters to System Prompt

Add a new section describing available ColorCorrector parameters:

```
## Color Grading (Sustained Effects)

Sustained effects hold for an entire section — they set the mood/look, not the rhythm.

Available ColorCorrector parameters:
- MasterGain (float, default 1.0): Overall brightness. >1 brighter, <1 darker.
- MasterLift (float, default 0.0): Black point / shadows. Negative = crushed blacks.
- MasterGamma (float, default 1.0): Midtones. >1 lifts mids, <1 darkens mids.
- MasterContrast (float, default 0.0): Contrast. 0.2 = punchy, 0.4 = dramatic.
- MasterSaturation (float, default 1.0): Color intensity. >1 vivid, <1 desaturated.
- MasterHueAngle (float, default 0.0): Hue rotation in degrees. 15 = warm shift, -15 = cool shift.
- GainR/GainG/GainB (float, default 1.0): Per-channel brightness. GainR=1.2 = warmer.
- LiftR/LiftG/LiftB (float, default 0.0): Per-channel shadows. LiftB=0.02 = blue shadows.

Use sustained_effects to set these per section. Keep values subtle — small changes (0.05-0.2) create visible looks.
```

### 2. Add Creative Guidelines for Color

```
## Color Grading Guidelines

- Dark/moody: MasterGain=0.85, MasterContrast=0.2, MasterLift=-0.02, MasterSaturation=0.8
- Warm/energetic: GainR=1.1, MasterSaturation=1.2, MasterGamma=1.05
- Cool/ethereal: GainB=1.1, LiftB=0.01, MasterSaturation=0.9
- High-energy drop: MasterContrast=0.3, MasterSaturation=1.3, MasterGain=1.1
- Transition from dark verse → bright chorus: natural if sustained values differ per section
```

### 3. Update JSON Schema Example

Add sustained_effects to the example:

```json
{
  "section_index": 0,
  "presets": ["zoom_pulse"],
  "sustained_effects": [
    {
      "node_type": "ColorCorrector",
      "parameters": {
        "MasterSaturation": 0.8,
        "MasterLift": -0.02,
        "MasterContrast": 0.2
      },
      "transition_frames": 15
    }
  ]
}
```

### 4. Add Tests

- Test system prompt includes ColorCorrector parameters
- Test system prompt includes sustained_effects in schema example

---

## Verification

- [ ] System prompt includes color grading parameters with descriptions
- [ ] System prompt includes creative guidelines for common looks
- [ ] JSON schema example shows sustained_effects
- [ ] `--ai --prompt "dark verse, bright chorus"` produces color differences
- [ ] Color values are subtle (LLM guided by parameter descriptions)
- [ ] Tests pass

---

**Related Design Docs**: [AI Effect Director](../../design/local.ai-effect-director.md)
