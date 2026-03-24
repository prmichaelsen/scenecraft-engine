# Milestone 9: Wan2.1 Video-to-Video + FILM Transitions

**Goal**: Add Wan2.1 video-to-video engine with FILM frame interpolation for temporally coherent, section-aware video stylization
**Duration**: 2-3 weeks
**Dependencies**: M7 (AI Video Stylization), M8 (EbSynth Coherence)
**Status**: Not Started

---

## Overview

This milestone adds a second rendering engine (`--engine wan`) that uses Wan2.1 for section-level video-to-video stylization instead of SD keyframes + EbSynth. FILM handles smooth style transitions between sections. The existing pipeline remains available as `--engine ebsynth`.

---

## Deliverables

### 1. Wan2.1 Section Renderer
- Section chunking (4-8 second clips)
- ComfyUI workflow for Wan2.1 video-to-video
- Beat-aware denoising strength per section
- Per-clip caching with download-as-generated

### 2. FILM Transition System
- FILM model integration for frame interpolation
- Multi-frame window blending (3 frames each side)
- AI-controlled transition lengths per section boundary
- Always-transition behavior

### 3. Engine Selection & Preview
- `--engine wan|ebsynth` flag on `beatlab render`
- `--preview` flag for 512x512 fast renders
- Updated AI prompt with wan_denoise and transition_frames

### 4. Pipeline Integration
- Section clip → FILM transition → reassembly pipeline
- Work directory caching per clip
- Fusion .setting still generated alongside

---

## Success Criteria

- [ ] `beatlab render video.mp4 --engine wan --ai -o styled.mp4` produces section-varied stylized video
- [ ] FILM transitions create smooth style morphs between sections
- [ ] Drop transitions are short (2-4 frames), verse transitions are long (15-30 frames)
- [ ] `--preview` renders at 512x512 for fast iteration
- [ ] Per-clip caching enables resume on failure
- [ ] Clips download as generated for live preview
- [ ] Audio descriptions influence Wan2.1 style prompts
- [ ] Existing `--engine ebsynth` pipeline still works

---

## Tasks

1. [Task 26: Wan2.1 ComfyUI Workflow](../tasks/milestone-9-wan21-film-pipeline/task-26-wan21-workflow.md) - ComfyUI workflow for Wan2.1 v2v, section chunking, denoise control
2. [Task 27: FILM Integration](../tasks/milestone-9-wan21-film-pipeline/task-27-film-integration.md) - FILM model setup, multi-frame blending, transition generation
3. [Task 28: Engine Selection & AI Plan](../tasks/milestone-9-wan21-film-pipeline/task-28-engine-plan.md) - --engine flag, --preview, wan_denoise/transition_frames in AI plan
4. [Task 29: Wan Pipeline Assembly](../tasks/milestone-9-wan21-film-pipeline/task-29-pipeline-assembly.md) - End-to-end pipeline: chunks → Wan2.1 → FILM → reassemble, caching, live download

---

## Risks and Mitigation

| Risk | Impact | Probability | Mitigation |
|------|--------|-------------|------------|
| Wan2.1 not available in ComfyUI | High | Low | Use diffusers library directly as fallback |
| FILM quality on style transitions | Medium | Medium | Fall back to linear crossfade if FILM artifacts |
| 24GB VRAM not available on Vast.ai | Medium | Low | Filter instance search for A100/A6000 |
| Long render times | Low | High | Preview mode at 512x512, per-clip caching |

---

**Next Milestone**: TBD
**Blockers**: None
**Notes**: Wan2.1 requires 24GB+ VRAM. FILM is lightweight (CPU-capable).
