# Task 26: Wan2.1 ComfyUI Workflow

**Milestone**: [M9 - Wan2.1 + FILM Pipeline](../../milestones/milestone-9-wan21-film-pipeline.md)
**Design Reference**: [Wan2.1 + FILM Pipeline](../../design/local.wan21-film-pipeline.md)
**Estimated Time**: 4 hours
**Dependencies**: Task 20 (SD Render Pipeline)
**Status**: Not Started

---

## Objective

Create a ComfyUI workflow for Wan2.1 video-to-video processing. Implement section chunking (4-8 second clips) and beat-aware denoising strength control.

---

## Steps

### 1. Wan2.1 ComfyUI Workflow
- Create a ComfyUI workflow JSON/API format for Wan2.1 video-to-video
- Input: source video clip + style prompt + denoising strength
- Output: stylized video clip
- Test with a sample 4-second clip

### 2. Section Chunking
- Add `chunk_section()` function that splits a section's frames into 4-8 second clips
- Respect frame boundaries (don't split mid-frame)
- Return list of clip paths for each section

### 3. Wan2.1 Client
- Create `src/beatlab/render/wan.py` with `Wan21Client` class
- `render_clip(input_clip, style_prompt, denoise_strength, resolution) -> output_clip`
- Support both local ComfyUI and remote (Vast.ai) execution
- Handle resolution switching for `--preview` (512x512) vs full (1280x720)

### 4. Beat-Aware Denoising
- Map section energy level to denoising strength:
  - low_energy: 0.3-0.4
  - mid_energy: 0.4-0.5
  - high_energy: 0.5-0.7
- Override with `wan_denoise` from AI effect plan when available

---

## Verification

- [ ] ComfyUI workflow renders a Wan2.1 v2v clip successfully
- [ ] Section chunking produces correct 4-8 second clips
- [ ] Wan21Client works with local and remote ComfyUI
- [ ] Denoising varies by section energy
- [ ] Preview mode renders at 512x512
