# Task 8: Intensity Mapping & Section Detection

**Milestone**: [M3 - Effect Library & Intelligence](../../milestones/milestone-3-effect-library.md)
**Design Reference**: [Requirements](../../design/requirements.md)
**Estimated Time**: 4 hours
**Dependencies**: Task 2, Task 7
**Status**: Not Started

---

## Objective

Map beat intensity to effect magnitude so stronger beats produce bigger effects, and implement spectral-feature-based musical section detection to vary effects across verse/chorus/drop sections.

---

## Steps

### 1. Intensity-to-Magnitude Mapping

- Scale preset `peak_value` by beat intensity (0.0-1.0)
- Configurable mapping curves: linear, exponential, logarithmic
- `--intensity-curve` CLI flag
- Example: intensity=0.3 with zoom_pulse → Size peaks at 1.024 instead of 1.08

### 2. Section Detection via Spectral Features

In `analyzer.py`, add section analysis:
- Compute spectral features: spectral centroid, RMS energy, spectral rolloff
- Use `librosa.segment.recurrence_matrix` or simple energy thresholding
- Classify segments into: low_energy (verse/intro), mid_energy (bridge), high_energy (chorus/drop)
- Add `sections` array to beat map JSON:

```json
"sections": [
  {"start_time": 0.0, "end_time": 30.5, "start_frame": 0, "end_frame": 915, "type": "low_energy", "label": "verse"},
  {"start_time": 30.5, "end_time": 61.0, "start_frame": 915, "end_frame": 1830, "type": "high_energy", "label": "chorus"}
]
```

### 3. Section-Aware Effect Selection

- Map section types to preferred presets:
  - low_energy → subtle (zoom_pulse, glow_swell)
  - mid_energy → moderate (flash, zoom_bounce)
  - high_energy → intense (hard_cut, flash + zoom_bounce)
- `--section-mode` CLI flag to enable/disable
- Default preset mapping, overridable via config

### 4. Update Beat Map Schema

- Add `sections` array
- Add per-beat `section` field linking each beat to its section
- Bump beat map version to 1.1

---

## Verification

- [ ] Beat intensity scales effect magnitude proportionally
- [ ] Section detection identifies at least 2 distinct sections in a typical track
- [ ] Sections appear in beat map JSON
- [ ] Section-aware mode produces varied effects across sections
- [ ] `--section-mode` flag enables/disables feature
- [ ] `--intensity-curve` flag changes mapping behavior

---

**Next Task**: [Task 9: Spline Curves & Polish](task-9-spline-curves.md)
