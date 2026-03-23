# Milestone 2: Fusion Comp Generation

**Goal**: Generate importable DaVinci Resolve Fusion .setting files with keyframed effects from beat maps
**Duration**: 1-2 weeks
**Dependencies**: M1 - Audio Analysis Pipeline
**Status**: Not Started

---

## Overview

This milestone implements Phase 2 of the architecture — consuming JSON beat maps and generating Fusion .setting files that can be imported into DaVinci Resolve. This requires reverse-engineering the Fusion .setting format, building a Fusion comp generator in Python, and producing properly keyframed Transform and BrightnessContrast nodes.

Completing this milestone delivers a working MVP: audio file → beat map → Fusion comp → import into Resolve.

---

## Deliverables

### 1. Fusion .setting Format Research
- Document the Fusion .setting file structure from exported examples
- Identify node types needed: Transform, BrightnessContrast, Glow
- Understand keyframe format and spline types

### 2. Fusion Comp Generator
- Python module that produces valid .setting files
- Keyframe generation at beat timestamps
- Support for Transform (zoom), BrightnessContrast (flash), Glow nodes
- Linear and ease-in/ease-out keyframe interpolation

### 3. End-to-End Pipeline
- CLI: `python -m beatlab generate <beats.json> --output comp.setting`
- Combined CLI: `python -m beatlab run <audio_file> --fps 30 --output comp.setting`
- Generated .setting files import successfully into Resolve

---

## Success Criteria

- [ ] Generated .setting files are valid and importable into DaVinci Resolve 18+
- [ ] Keyframes land on correct frames matching beat timestamps
- [ ] At least 2 effect types work (zoom pulse + brightness flash)
- [ ] End-to-end pipeline: audio → .setting in one command
- [ ] Generated comp plays back with visible beat-synced effects

---

## Key Files to Create

```
src/beatlab/
├── fusion/
│   ├── __init__.py
│   ├── setting_writer.py
│   ├── nodes.py
│   └── keyframes.py
└── generator.py
```

---

## Tasks

1. [Task 4: Fusion .setting Format Research](../tasks/milestone-2-fusion-comp-generation/task-4-fusion-format-research.md) - Reverse-engineer .setting format from Resolve exports
2. [Task 5: Fusion Comp Generator](../tasks/milestone-2-fusion-comp-generation/task-5-fusion-comp-generator.md) - Python module to generate .setting files with keyframed nodes
3. [Task 6: End-to-End Pipeline & CLI](../tasks/milestone-2-fusion-comp-generation/task-6-end-to-end-pipeline.md) - Combined CLI, pipeline integration, Resolve import testing

---

## Testing Requirements

- [ ] Unit tests for .setting file structure validity
- [ ] Unit tests for keyframe timing accuracy
- [ ] Manual integration test: import generated .setting into Resolve
- [ ] End-to-end test: audio file → .setting file

---

## Risks and Mitigation

| Risk | Impact | Probability | Mitigation Strategy |
|------|--------|-------------|---------------------|
| Fusion .setting format undocumented | High | Medium | Export reference comps from Resolve, compare and reverse-engineer |
| Resolve version differences in .setting format | Medium | Low | Test with Resolve 18 and 19; keep generated comps minimal |
| Keyframe interpolation mismatch | Medium | Medium | Test all spline types; fallback to linear |

---

**Next Milestone**: [M3 - Effect Library & Intelligence](milestone-3-effect-library.md)
**Blockers**: None
**Notes**: This milestone delivers the MVP. After M2, users can go from audio file to beat-synced Fusion comp.
