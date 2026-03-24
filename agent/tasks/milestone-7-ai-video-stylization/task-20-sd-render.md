# Task 20: SD Render Pipeline

**Milestone**: [M7 - AI Video Stylization](../../milestones/milestone-7-ai-video-stylization.md)
**Design Reference**: None
**Estimated Time**: 6 hours
**Dependencies**: Task 19
**Status**: Not Started

---

## Objective

Build the Stable Diffusion img2img render pipeline that processes frames with per-frame parameters (denoising strength, style prompt) using ComfyUI on a remote GPU, with ControlNet for temporal coherence.

---

## Steps

### 1. Create ComfyUI Workflow Template

Design a ComfyUI API workflow JSON that takes:
- Input image (the video frame)
- Positive prompt (style)
- Negative prompt (quality negatives)
- Denoising strength (0.0-1.0)
- ControlNet (canny or depth for composition preservation)
- Seed (consistent per-run for coherence)

### 2. Implement Remote Render Client

```python
class ComfyUIClient:
    """Connects to a ComfyUI instance and submits render jobs."""

    def __init__(self, host: str, port: int = 8188): ...
    def render_frame(self, image_path: str, prompt: str, denoise: float, seed: int) -> bytes: ...
    def render_batch(self, frame_params: list[dict], progress_callback=None) -> list[str]: ...
```

- Connect to ComfyUI's API (WebSocket + REST)
- Submit workflow per frame
- Download result image
- Progress reporting

### 3. Implement Temporal Coherence Strategy

- Use the same seed for all frames in a section
- ControlNet (canny edge detection) to preserve original composition
- Optional: use previous stylized frame as init image (frame-chaining) for smoother transitions
- Denoising strength is the key lever — lower = more original, higher = more stylized

### 4. Implement Batch Orchestrator

```python
def render_video_frames(
    frames_dir: str, output_dir: str,
    frame_params: list[dict],
    comfyui_host: str,
    model: str = "sd_xl_base_1.0.safetensors",
) -> None:
```

- Process frames sequentially (required for frame-chaining coherence)
- Save stylized frames to output_dir
- Resume support: skip already-rendered frames
- Error handling: retry failed frames

### 5. Add Tests

- Test ComfyUI workflow JSON generation
- Test frame param integration into workflow
- Test batch orchestrator with mock client

---

## Verification

- [ ] ComfyUI workflow template produces valid API JSON
- [ ] Client connects and submits render jobs
- [ ] ControlNet preserves composition
- [ ] Denoising strength varies per frame
- [ ] Resume works (skips existing frames)
- [ ] Progress callback fires

---

## Notes

- ComfyUI must be pre-installed on the GPU instance (handled by Task 21)
- SDXL recommended for quality; SD 1.5 as faster fallback
- ControlNet model: canny for line preservation, depth for 3D-aware scenes

---

**Next Task**: [Task 21: Cloud GPU Provisioning](task-21-cloud-gpu.md)
