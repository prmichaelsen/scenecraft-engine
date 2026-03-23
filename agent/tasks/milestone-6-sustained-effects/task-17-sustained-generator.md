# Task 17: Sustained Effects in Generator & Plan

**Milestone**: [M6 - Sustained Effects & Color Grading](../../milestones/milestone-6-sustained-effects.md)
**Design Reference**: None
**Estimated Time**: 3 hours
**Dependencies**: Task 16
**Status**: Not Started

---

## Objective

Add `sustained_effects` to the SectionPlan schema and update the generator to create section-level hold keyframes from the AI effect plan. Sustained effects hold for the entire section duration, separate from beat-pulse effects.

---

## Steps

### 1. Extend SectionPlan Schema

In `plan.py`, add to SectionPlan:

```python
@dataclass
class SectionPlan:
    section_index: int
    presets: list[str] = field(default_factory=list)           # beat pulses
    custom_effects: list[dict] = field(default_factory=list)   # beat pulses
    sustained_effects: list[dict] = field(default_factory=list) # section holds — NEW
    intensity_curve: str = "linear"
    attack_frames: int | None = None
    release_frames: int | None = None
```

Each sustained effect dict:
```json
{
  "node_type": "ColorCorrector",
  "parameters": {
    "MasterSaturation": 1.3,
    "GainR": 1.15,
    "MasterContrast": 0.15,
    "MasterLift": -0.03
  },
  "transition_frames": 15
}
```

### 2. Update parse_effect_plan()

Parse `sustained_effects` from the LLM JSON.

### 3. Update Generator — _generate_from_plan()

For each section with sustained_effects:
- Group sustained effects by node_type
- Create one node per unique node_type (e.g. one ColorCorrector for all color params)
- For each parameter, create a KeyframeTrack with `add_hold()` using section start/end frames
- If consecutive sections have the same sustained node, reuse the node and transition between values

### 4. Handle Section Transitions

When section N has `MasterSaturation: 1.3` and section N+1 has `MasterSaturation: 0.9`:
- The hold keyframes naturally overlap at section boundaries
- The transition_frames create smooth crossfades between values

### 5. Add Tests

- Test sustained_effects parsed from JSON
- Test generator creates hold keyframes for sustained effects
- Test ColorCorrector with multiple sustained params
- Test section transitions produce smooth crossfades

---

## Verification

- [ ] SectionPlan includes sustained_effects field
- [ ] parse_effect_plan() handles sustained_effects
- [ ] Generator creates hold keyframes for sustained effects
- [ ] ColorCorrector node has multiple animated parameters
- [ ] Section transitions are smooth (no hard cuts between color grades)
- [ ] Sustained and pulse effects coexist on different nodes
- [ ] Existing tests pass

---

**Next Task**: [Task 18: AI Color Grading Prompt](task-18-ai-color-prompt.md)
