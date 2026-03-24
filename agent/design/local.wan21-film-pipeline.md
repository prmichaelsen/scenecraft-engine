# Wan2.1 Video-to-Video + FILM Transitions

**Concept**: Section-level video-to-video stylization using Wan2.1 with FILM frame interpolation for smooth style transitions between sections
**Created**: 2026-03-24
**Status**: Design Specification

---

## Overview

Adds `--engine wan` to the existing `beatlab render` command. Instead of SD img2img keyframes + EbSynth propagation, Wan2.1 processes video clips per section (chunked into 4-8 second segments) with style prompts from the AI effect plan. FILM interpolation handles transitions between sections, with transition lengths controlled by the AI director.

The existing SD + EbSynth pipeline remains as `--engine ebsynth` (default). Fusion .setting effects are still generated alongside for layering in Resolve.

---

## Architecture

### Pipeline Flow

```
Audio → Beat Analysis → Sections → AI Effect Plan (with style_prompts + transition_frames)
                                         ↓
Video → Extract Section Clips → Wan2.1 v2v per clip → FILM transitions → Reassemble
                                         ↓
                              + Fusion .setting (zoom/shake/color)
```

### Section Chunking

Sections are chunked into 4-8 second clips for Wan2.1 processing:
- Each section is split into clips of max 8 seconds
- Clips within a section share the same style_prompt
- Wan2.1 processes each clip independently
- FILM runs between all clip boundaries — both within sections and between sections

### FILM Everywhere

FILM transitions happen at two levels:
- **Intra-section** (between clips within the same section): short transitions (~4-8 frames) to smooth out Wan2.1 clip-to-clip discontinuities. Same style on both sides, so the blend is subtle.
- **Inter-section** (between sections): AI-controlled transition lengths (2-30 frames) for creative style morphing between different looks.

### FILM Transitions

At every clip boundary (both within and between sections):
1. Take last 3 frames of clip A's output
2. Take first 3 frames of clip B's output
3. FILM generates intermediate frames between them
4. **Intra-section**: fixed ~4-8 frame transition (just smoothing)
5. **Inter-section**: AI-controlled length (2-30 frames, creative morphing)
6. Always transition, even between similar-style sections

### Denoising Strength

Beat-aware denoising per section:
- Low energy sections: lower denoising (0.3-0.4) — subtle style, preserves detail
- High energy / drops: higher denoising (0.5-0.7) — more dramatic transformation
- Values from AI effect plan or section energy level

---

## CLI Integration

```bash
# Wan2.1 engine (full resolution)
beatlab render video.mp4 --engine wan --ai --describe -o styled.mp4

# Preview mode (512x512, fast)
beatlab render video.mp4 --engine wan --preview --ai -o preview.mp4

# Default: existing SD + EbSynth pipeline
beatlab render video.mp4 --ai -o styled.mp4
beatlab render video.mp4 --engine ebsynth --ai -o styled.mp4
```

New options:
- `--engine wan|ebsynth` — select rendering engine (default: ebsynth)
- `--preview` — render at 512x512 for fast previews

---

## AI Effect Plan Extension

The plan schema gets a new `transition_frames` field per section for controlling FILM transitions:

```json
{
  "sections": [
    {
      "section_index": 0,
      "presets": ["zoom_pulse"],
      "style_prompt": "soft watercolor, muted tones",
      "intensity_curve": "linear",
      "wan_denoise": 0.35,
      "transition_frames": 15
    },
    {
      "section_index": 1,
      "presets": ["flash", "shake_x", "shake_y"],
      "style_prompt": "vivid neon, psychedelic melting",
      "intensity_curve": "exponential",
      "wan_denoise": 0.6,
      "transition_frames": 3
    }
  ]
}
```

The AI director prompt is updated to:
- Set `wan_denoise` per section based on energy and audio description
- Set `transition_frames` per section boundary (short for drops, long for smooth transitions)
- Audio descriptions from Gemini influence both style_prompt and wan_denoise

---

## Work Directory & Caching

```
.beatlab_work/
├── audio.wav
├── beats.json
├── plan.json
├── frames/              # Original extracted frames
├── wan_clips/           # Wan2.1 output per section chunk
│   ├── section_000_chunk_000.mp4
│   ├── section_000_chunk_001.mp4
│   ├── section_001_chunk_000.mp4
│   └── ...
├── transitions/         # FILM transition clips
│   ├── transition_000_001.mp4
│   └── ...
├── styled/              # Final assembled frames
└── status.json
```

- Each Wan2.1 clip is cached individually — resume on failure
- Clips are downloaded as they complete for live preview
- `--fresh` clears the work dir to start over

---

## Error Handling

- Wan2.1 failure: fatal error, no fallback to EbSynth
- FILM failure: fatal error
- Clear error messages with section index and clip info

---

## GPU Requirements

- Wan2.1: 24GB+ VRAM (A100, A6000, or similar)
- FILM: lightweight, can run on CPU or small GPU
- Cloud GPU via existing Vast.ai integration

---

## Key Design Decisions

| Decision | Choice | Rationale |
|---|---|---|
| Engine selection | `--engine wan\|ebsynth` coexist | Users can compare results, different use cases |
| Section chunking | 4-8 second clips | Wan2.1 memory limits, enables per-clip caching |
| FILM window | 3 frames each side | Smoother morphing than single-frame blending |
| Transition always | Yes, even similar sections | Consistency, subtle transitions still add polish |
| Failure mode | Fatal, no fallback | User explicitly chose Wan2.1, silent fallback would be surprising |
| Preview mode | 512x512 | ~4x faster, good enough to evaluate style choices |
| Audio descriptions | Influence Wan2.1 prompts | Richer context for style decisions |
| Caching | Per-clip download as generated | Resume support + live preview during long renders |

---

**Status**: Design Specification
**Related Documents**: [Requirements](requirements.md), [AI Effect Director](local.ai-effect-director.md), [Clarification 2](../clarifications/clarification-2-wan21-video-stylization.md)
