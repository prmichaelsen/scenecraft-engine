# Milestone 7: AI Video Stylization

**Goal**: Add a `beatlab render` command that takes video + beat map and produces AI-stylized video using Stable Diffusion, with beat-synced denoising strength and per-section style prompts, rendered on cloud GPU
**Duration**: 2-3 weeks
**Dependencies**: M5 - AI Effect Director
**Status**: Not Started

---

## Overview

This milestone adds a completely new output path: instead of generating Fusion .setting files for compositing, `beatlab render` takes an actual video file, extracts frames, runs Stable Diffusion img2img on each frame with parameters driven by the beat map, and reassembles the stylized frames into a new video.

The beat map drives the render:
- **Per-section style prompts**: "psychedelic melting" for the chorus, "dark ethereal" for the verse
- **Beat-synced denoising strength**: pulses higher on beats (more transformation) and lower between beats (more original)
- **Section-level style changes**: different SD prompts per section, smooth interpolation between styles
- **Intensity mapping**: stronger beats = more radical transformation

Rendering happens on a cloud GPU (Vast.ai) for cost efficiency (~$2-3 for a 30-minute video vs $500+ on API services).

---

## Deliverables

### 1. Frame Extraction & Reassembly
- ffmpeg-based frame extraction (video → PNG frames)
- Frame reassembly with original audio (stylized frames → video)
- Resolution handling (render at lower res for speed, upscale if needed)
- Temp directory management

### 2. SD Render Pipeline
- ComfyUI workflow that takes: frame image, style prompt, denoising strength, ControlNet settings
- Beat map consumer that generates per-frame parameters:
  - Denoising strength: base 0.3, pulse to 0.5 on beats
  - Style prompt: from section's SD style
  - ControlNet: preserve composition from original frame
- Temporal consistency: frame-to-frame coherence via ControlNet + low denoising
- Batch processing: render frames sequentially with consistent seed

### 3. Cloud GPU Management
- Vast.ai API integration: search instances, create, SSH deploy, monitor, destroy
- Automated setup: install ComfyUI + models + deps on fresh instance
- File transfer: upload frames + beat map, download results
- Cost estimation: estimate render time and cost before starting
- Graceful cleanup: destroy instance when done

### 4. CLI & AI Director Integration
- `beatlab render video.mp4 --beats beats.json --style "psychedelic" -o output.mp4`
- `beatlab render video.mp4 --ai --prompt "dreamy verse, intense drop" -o output.mp4`
- AI director outputs SD style prompts per section in the effect plan
- Progress reporting during long renders

---

## Success Criteria

- [ ] `beatlab render` extracts frames, processes, reassembles video
- [ ] Denoising strength pulses on beats (visible style transformation on hits)
- [ ] Different sections have different visual styles
- [ ] Temporal coherence — no wild flickering between frames
- [ ] Cloud GPU provisioning is automated (user just needs Vast.ai API key)
- [ ] 30-minute video renders in ~2-4 hours for ~$3
- [ ] Original audio preserved in output

---

## Tasks

1. [Task 19: Frame Extraction & Reassembly](../tasks/milestone-7-ai-video-stylization/task-19-frame-pipeline.md) - ffmpeg frame extract/reassemble pipeline
2. [Task 20: SD Render Pipeline](../tasks/milestone-7-ai-video-stylization/task-20-sd-render.md) - ComfyUI workflow, beat map → per-frame SD params
3. [Task 21: Cloud GPU Provisioning](../tasks/milestone-7-ai-video-stylization/task-21-cloud-gpu.md) - Vast.ai instance management
4. [Task 22: Render CLI & AI Director](../tasks/milestone-7-ai-video-stylization/task-22-render-cli.md) - beatlab render command, AI style prompts

---

## Risks and Mitigation

| Risk | Impact | Probability | Mitigation Strategy |
|------|--------|-------------|---------------------|
| Temporal flickering between frames | High | Medium | ControlNet + consistent seed + low denoising; frame blending as fallback |
| Vast.ai API changes or availability | Medium | Low | Support manual SSH as fallback; document instance setup for other providers |
| SD model quality varies | Medium | Medium | Default to SDXL; let user specify model; test with multiple models |
| Long render times | Medium | High | Show progress, support resume after interruption, allow resolution tradeoff |
| Large file transfers to/from cloud | Medium | Medium | Compress frames, parallel transfer, or render directly to cloud storage |

---

## Architecture Notes

```
beatlab render video.mp4 --beats beats.json --style "psychedelic"
  │
  ├── 1. Extract frames (ffmpeg, local)
  │     video.mp4 → /tmp/frames/frame_00001.png ...
  │
  ├── 2. Generate per-frame params from beat map
  │     frame_00001: denoise=0.3, prompt="psychedelic melting"
  │     frame_00015: denoise=0.5, prompt="psychedelic melting"  ← beat hit
  │     frame_00900: denoise=0.3, prompt="dark ethereal"        ← new section
  │
  ├── 3. Provision cloud GPU (Vast.ai)
  │     Create instance → install ComfyUI → upload frames + params
  │
  ├── 4. Render (on cloud GPU)
  │     ComfyUI processes each frame with its params
  │
  ├── 5. Download results
  │     /tmp/styled_frames/frame_00001.png ...
  │
  └── 6. Reassemble (ffmpeg, local)
        styled frames + original audio → output.mp4
```

---

**Blockers**: None
**Notes**:
- Vast.ai API key required (VASTAI_API_KEY env var)
- SD model weights downloaded on first use (~5GB)
- ControlNet model weights also needed (~1.5GB)
- User needs ffmpeg installed locally
