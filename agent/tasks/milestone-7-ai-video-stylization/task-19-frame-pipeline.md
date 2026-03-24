# Task 19: Frame Extraction & Reassembly

**Milestone**: [M7 - AI Video Stylization](../../milestones/milestone-7-ai-video-stylization.md)
**Design Reference**: None
**Estimated Time**: 2 hours
**Dependencies**: None
**Status**: Not Started

---

## Objective

Build the ffmpeg-based pipeline that extracts video frames to PNGs and reassembles stylized frames back into a video with the original audio track.

---

## Steps

### 1. Create render/ Module

```
src/beatlab/render/
├── __init__.py
├── frames.py      # Frame extraction and reassembly
```

### 2. Implement Frame Extraction

```python
def extract_frames(video_path: str, output_dir: str, fps: float | None = None) -> tuple[int, float]:
    """Extract frames from video using ffmpeg.

    Returns (frame_count, detected_fps).
    """
```

- Use `ffmpeg -i video.mp4 -qscale:v 2 output_dir/frame_%06d.png`
- If fps specified, use `-vf fps={fps}` to downsample
- Detect source fps from ffprobe
- Return frame count and fps for beat map alignment

### 3. Implement Frame Reassembly

```python
def reassemble_video(
    frames_dir: str, output_path: str, fps: float,
    audio_source: str | None = None,
) -> None:
    """Reassemble frames into video, optionally with original audio."""
```

- Use `ffmpeg -framerate {fps} -i frames_dir/frame_%06d.png -i audio_source -c:v libx264 -pix_fmt yuv420p -c:a aac output.mp4`
- Handle case where audio is longer/shorter than video

### 4. Implement Per-Frame Parameter Generation

```python
def generate_frame_params(
    beat_map: dict, total_frames: int, fps: float,
    base_denoise: float = 0.3, beat_denoise: float = 0.5,
    section_styles: dict[int, str] | None = None,
    default_style: str = "psychedelic",
) -> list[dict]:
    """Generate per-frame SD parameters from beat map.

    Returns list of {frame, denoise, prompt, seed} dicts.
    """
```

- Base denoising strength between beats
- Pulse denoising on beat frames (scaled by intensity)
- Style prompt changes at section boundaries
- Consistent seed for temporal coherence

### 5. Add Tests

- Test frame count detection from mock ffprobe output
- Test per-frame param generation with sample beat map
- Test denoising pulses on beat frames
- Test style changes at section boundaries

---

## Verification

- [ ] `extract_frames()` calls ffmpeg correctly
- [ ] `reassemble_video()` produces valid video with audio
- [ ] Per-frame params have correct denoising on beats
- [ ] Style prompts change at section boundaries
- [ ] Tests pass

---

**Next Task**: [Task 20: SD Render Pipeline](task-20-sd-render.md)
