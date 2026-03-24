# Task 22: Render CLI & AI Director

**Milestone**: [M7 - AI Video Stylization](../../milestones/milestone-7-ai-video-stylization.md)
**Design Reference**: [AI Effect Director](../../design/local.ai-effect-director.md)
**Estimated Time**: 3 hours
**Dependencies**: Task 21
**Status**: Not Started

---

## Objective

Add the `beatlab render` CLI command and extend the AI director to output per-section SD style prompts alongside Fusion effect choices.

---

## Steps

### 1. Add render Command to CLI

```
beatlab render VIDEO_FILE [OPTIONS]

Options:
  --beats PATH          Beat map JSON (if not provided, analyze audio from video)
  --fps FLOAT           Override frame rate
  --style TEXT          Default SD style prompt
  --ai / --no-ai       Use AI to pick styles per section
  --prompt TEXT         Creative direction for AI
  --output PATH         Output video file
  --base-denoise FLOAT  Base denoising strength (default: 0.3)
  --beat-denoise FLOAT  Beat denoising strength (default: 0.5)
  --model TEXT          SD model name (default: sdxl)
  --local / --cloud     Render locally or on cloud GPU (default: cloud)
  --dry-run             Show cost estimate without rendering
```

### 2. Extend Effect Plan Schema

Add optional `style_prompt` field to SectionPlan:

```python
@dataclass
class SectionPlan:
    ...
    style_prompt: str | None = None  # SD style for this section
```

### 3. Update AI Director Prompt

Add to system prompt:

```
## Video Stylization (SD Render)

When the user requests video rendering (--render mode), also include a style_prompt
per section describing the visual style for Stable Diffusion img2img:

- style_prompt: Short SD prompt describing the look (e.g. "psychedelic melting colors,
  vivid neon, dreamlike", "dark moody noir, high contrast, film grain")
- Keep prompts concise (under 50 words)
- Match style to the music's mood and energy
```

### 4. Wire Up the Pipeline

```python
@main.command()
def render(video_file, beats, style, ai, prompt, output, ...):
    # 1. Analyze audio (extract from video if no --beats)
    # 2. Get AI plan with style_prompts (if --ai)
    # 3. Extract frames
    # 4. Generate per-frame params from beat map + plan
    # 5. Render on cloud GPU (or local)
    # 6. Reassemble video with original audio
```

### 5. Add Audio Extraction from Video

If no `--beats` provided, extract audio from video first:
```
ffmpeg -i video.mp4 -vn -acodec pcm_s16le -ar 22050 /tmp/audio.wav
```
Then run `analyze_audio()` on it.

### 6. Add Tests

- Test render command argument parsing
- Test AI plan includes style_prompt when render mode
- Test audio extraction from video

---

## Verification

- [ ] `beatlab render` command exists with all options
- [ ] Audio extracted from video when no --beats provided
- [ ] AI director includes style_prompt per section
- [ ] Per-frame params generated correctly from beat map
- [ ] Dry-run shows cost estimate without rendering
- [ ] Full pipeline: video → extract → render → reassemble → output.mp4

---

**Related Design Docs**: [AI Effect Director](../../design/local.ai-effect-director.md)
