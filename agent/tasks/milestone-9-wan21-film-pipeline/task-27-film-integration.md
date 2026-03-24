# Task 27: FILM Integration

**Milestone**: [M9 - Wan2.1 + FILM Pipeline](../../milestones/milestone-9-wan21-film-pipeline.md)
**Design Reference**: [Wan2.1 + FILM Pipeline](../../design/local.wan21-film-pipeline.md)
**Estimated Time**: 3 hours
**Dependencies**: None (independent of Wan2.1)
**Status**: Not Started

---

## Objective

Integrate Google FILM (Frame Interpolation for Large Motion) for smooth style transitions between stylized video sections. Uses a multi-frame window (3 frames each side) and AI-controlled transition lengths.

---

## Steps

### 1. FILM Model Setup
- Add `google-research/frame-interpolation` as dependency (or PyPI package if available)
- Create `src/beatlab/render/film.py` with `FILMInterpolator` class
- Handle model download/caching on first use
- Support CPU execution (FILM is lightweight)

### 2. Multi-Frame Window Blending
- `generate_transition(frames_a: list[3], frames_b: list[3], num_frames: int) -> list[Frame]`
- Take last 3 frames of section A, first 3 frames of section B
- FILM interpolates between them to produce `num_frames` transition frames
- Recursively subdivide for high frame counts (FILM generates midpoints)

### 3. Transition Length Control
- **Intra-section** (between clips within a section): fixed ~4-8 frames, just smoothing discontinuities
- **Inter-section** (between sections): read `transition_frames` from AI effect plan, range 2-30 frames
  - Short (2-4): hard impact at drops
  - Long (15-30): smooth morph between verses
- Always transition at every clip boundary

### 4. Transition Assembly
- `assemble_with_transitions(section_clips, plan) -> final_frames`
- For each section: stitch clips with intra-section FILM transitions
- Between sections: stitch with inter-section FILM transitions (AI-controlled length)
- Handle first/last sections (no transition before first, none after last)

---

## Verification

- [ ] FILM model loads and generates interpolated frames
- [ ] Multi-frame window produces smoother results than single-frame
- [ ] Transition lengths match AI plan values
- [ ] Assembly produces continuous frame sequence
- [ ] Works on CPU without GPU
