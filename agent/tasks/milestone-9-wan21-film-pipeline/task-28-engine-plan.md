# Task 28: Engine Selection & AI Plan Extension

**Milestone**: [M9 - Wan2.1 + FILM Pipeline](../../milestones/milestone-9-wan21-film-pipeline.md)
**Design Reference**: [Wan2.1 + FILM Pipeline](../../design/local.wan21-film-pipeline.md)
**Estimated Time**: 2 hours
**Dependencies**: Task 22 (Render CLI)
**Status**: Not Started

---

## Objective

Add `--engine wan|ebsynth` and `--preview` flags to `beatlab render`. Extend the AI effect plan schema with `wan_denoise` and `transition_frames`. Update the AI director prompt to generate these fields.

---

## Steps

### 1. CLI Flags
- Add `--engine` option to `beatlab render`: choices `wan`, `ebsynth` (default: `ebsynth`)
- Add `--preview` flag: when set, Wan2.1 renders at 512x512
- Route to appropriate pipeline based on engine choice

### 2. Effect Plan Schema Extension
- Add `wan_denoise` (float, 0.0-1.0) to `SectionPlan` dataclass
- Add `transition_frames` (int, 2-30) to `SectionPlan` dataclass
- Update `parse_effect_plan()` to read new fields
- Update `validate_effect_plan()` to check ranges

### 3. AI Director Prompt Update
- Add Wan2.1 denoising guidance to system prompt:
  - Explain what wan_denoise controls
  - Tie to energy levels and audio descriptions
  - Examples: breakdown=0.3, verse=0.4, chorus=0.55, drop=0.65
- Add transition_frames guidance:
  - Explain FILM transitions between sections
  - Short (2-4f) for hard cuts/drops, long (15-30f) for smooth mood shifts
  - Use audio descriptions to inform: "building tension" → medium transition into short drop transition
- Audio descriptions should influence both style_prompt and wan_denoise

### 4. Backward Compatibility
- Existing `--engine ebsynth` ignores wan_denoise and transition_frames
- Plan schema accepts but doesn't require new fields

---

## Verification

- [ ] `--engine wan` routes to Wan2.1 pipeline
- [ ] `--engine ebsynth` still works as before
- [ ] `--preview` sets 512x512 resolution
- [ ] AI plan includes wan_denoise and transition_frames per section
- [ ] New fields are optional (ebsynth pipeline unaffected)
