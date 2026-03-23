# Task 16: Hold Keyframes & ColorCorrector Node

**Milestone**: [M6 - Sustained Effects & Color Grading](../../milestones/milestone-6-sustained-effects.md)
**Design Reference**: None
**Estimated Time**: 2 hours
**Dependencies**: Task 15
**Status**: Not Started

---

## Objective

Add a `hold` keyframe mode to KeyframeTrack that transitions to a value, holds it for a duration, then transitions out. Add a ColorCorrector Fusion node with full color parameters.

---

## Steps

### 1. Add hold() to KeyframeTrack

```python
def add_hold(
    self,
    start_frame: int,
    end_frame: int,
    value: float,
    base_value: float = 0.0,
    transition_frames: int = 15,
    interpolation: str = "smooth",
) -> None:
    """Hold a value for a section duration with smooth transitions."""
    # Transition in
    self.add(start_frame, base_value, interpolation)
    self.add(start_frame + transition_frames, value, interpolation)
    # Transition out
    self.add(end_frame - transition_frames, value, interpolation)
    self.add(end_frame, base_value, interpolation)
```

### 2. Add ColorCorrector Node

In `nodes.py`:

```python
def make_color_corrector(
    name: str = "ColorCorrector1",
    source_op: str | None = None,
    pos_x: float = 440,
) -> FusionNode:
```

Supported keyframeable parameters:
- MasterGain, MasterLift, MasterGamma, MasterContrast, MasterSaturation
- MasterHueAngle
- GainR, GainG, GainB
- LiftR, LiftG, LiftB

### 3. Register in NODE_MAKERS

Add `"ColorCorrector": make_color_corrector` to the generator's NODE_MAKERS dict.

### 4. Add Tests

- Test hold keyframes produce 4 keyframes (in-start, in-end, out-start, out-end)
- Test hold values are correct
- Test ColorCorrector node serializes correctly
- Test ColorCorrector with animated params produces BezierSpline

---

## Verification

- [ ] `add_hold()` produces correct transition-in, hold, transition-out keyframes
- [ ] ColorCorrector node serializes to valid .setting format
- [ ] Multiple animated params on one ColorCorrector work
- [ ] NODE_MAKERS includes ColorCorrector
- [ ] Existing tests pass

---

**Next Task**: [Task 17: Sustained Effects in Generator & Plan](task-17-sustained-generator.md)
