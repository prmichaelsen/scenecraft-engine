# Task 7: Effect Preset Library

**Milestone**: [M3 - Effect Library & Intelligence](../../milestones/milestone-3-effect-library.md)
**Design Reference**: [Requirements](../../design/requirements.md)
**Estimated Time**: 3 hours
**Dependencies**: Task 5
**Status**: Not Started

---

## Objective

Create a library of effect presets (zoom pulse, brightness flash, glow swell, color shift) with configurable parameters, selectable via CLI.

---

## Steps

### 1. Define Effect Preset Interface

```python
@dataclass
class EffectPreset:
    name: str
    node_type: str          # Transform, BrightnessContrast, Glow, ColorCorrector
    parameter: str          # Which parameter to keyframe
    base_value: float       # Resting value
    peak_value: float       # Maximum effect value
    attack_frames: int      # Frames to reach peak
    release_frames: int     # Frames to return to base
    curve: str              # linear, ease_in, ease_out, smooth
```

### 2. Implement Presets

- **zoom_pulse**: Transform.Size 1.0 → 1.08, smooth curve, 2f attack / 4f release
- **zoom_bounce**: Transform.Size 1.0 → 1.15, ease_out, 1f attack / 6f release
- **flash**: BrightnessContrast.Gain 1.0 → 1.4, linear, 1f attack / 3f release
- **glow_swell**: Glow.Intensity 0.0 → 0.5, smooth, 3f attack / 6f release
- **color_shift**: ColorCorrector hue rotation, smooth, 4f attack / 8f release
- **hard_cut**: BrightnessContrast.Gain 1.0 → 2.0, linear, 0f attack / 1f release

### 3. Preset Registry

- Dict-based registry: `PRESETS["zoom_pulse"]`
- CLI `--preset` flag replaces `--effect`
- `beatlab presets` command lists available presets with descriptions

### 4. Multi-Preset Support

- Allow `--preset zoom_pulse,flash` to layer multiple effects
- Generator creates multiple nodes and keyframes per beat

---

## Verification

- [ ] At least 4 presets defined and registered
- [ ] `beatlab presets` lists all available presets
- [ ] `--preset` flag selects correct effect
- [ ] Multi-preset layering generates multiple nodes
- [ ] Each preset produces visually distinct effects in Resolve

---

**Next Task**: [Task 8: Intensity Mapping & Section Detection](task-8-intensity-and-sections.md)
