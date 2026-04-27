# Generation Provider Landscape

**Last updated**: 2026-04-27
**Status**: Research reference (not a spec)

Evaluation of image, video, and audio generation APIs for potential integration into SceneCraft. Prioritized by user desirability and API readiness.

---

## Current Stack

| Category | Provider | Integration |
|----------|----------|-------------|
| Image | Google Imagen (Vertex AI) | `render/google_video.py` |
| Image | Nano Banana 2 (Replicate) | `render/google_video.py` |
| Image | SDXL (Replicate) | `render/google_video.py` |
| Video | Google Veo 3.0/3.1 (Vertex AI) | `render/google_video.py` |
| Video | Runway Gen-4.5 | `render/google_video.py` |
| Video | Kling 3.0 (Replicate) | `render/kling_video.py` |
| Music | Musicful | External API |
| Audio | isolate_vocals (2-stem) | In-process librosa |

---

## Tier 1 — High impact, API-ready, fills clear gaps

### ElevenLabs (Voice + SFX)

- **Category**: Voice synthesis, sound effects generation
- **Gap filled**: No voice or SFX generation in current stack
- **API**: Mature REST API, Python/JS SDKs, well-documented
- **Capabilities**: Text-to-speech, voice cloning, text-to-SFX, voice-to-voice, dubbing
- **Pricing**: Free tier (10k chars/mo), Pro $22/mo (100k chars), Scale tiers available
- **Integration path**: REST client, similar pattern to existing providers
- **Priority**: Highest. Covers two missing categories (voice + SFX) with a single integration.

### FLUX 1.1 Pro / Pro Ultra (Black Forest Labs)

- **Category**: Image generation
- **Gap filled**: Quality ceiling above SDXL/Nano Banana; excellent text rendering in images
- **API**: Official at `api.bfl.ml`, also on Replicate and Fal.ai
- **Capabilities**: Text-to-image, photorealistic + artistic, native high-res (up to 2K with Ultra)
- **Pricing**: ~$0.04-0.06/image (BFL API), similar on Replicate
- **Open weights**: FLUX.1 Dev and Schnell variants are open; Pro/Ultra are API-only
- **Integration path**: Replicate (existing pattern) or BFL native API
- **Priority**: High. Dramatic keyframe quality upgrade with minimal integration effort.

### Luma Ray2 (Video gen)

- **Category**: Video generation
- **Gap filled**: Strong camera motion control and 3D consistency; different aesthetic from Veo/Runway/Kling
- **API**: Official Luma developer API, also on Replicate
- **Capabilities**: Text-to-video, image-to-video, camera motion controls, ~5-9s clips
- **Pricing**: Credit-based
- **Integration path**: Replicate or native API; same async job pattern as existing providers
- **Priority**: High. Gives users meaningful stylistic choice alongside existing providers.

### Demucs 4-stem separation (Meta)

- **Category**: Audio processing (stem separation)
- **Gap filled**: Current isolate_vocals is 2-stem only; Demucs gives vocals/drums/bass/other
- **API**: Open source (MIT), available on Replicate
- **Capabilities**: 4-stem separation (htdemucs), industry-standard quality
- **Pricing**: Free to self-host; ~$0.02-0.05/track on Replicate
- **Integration path**: Replicate call, replaces/upgrades isolate_vocals
- **Priority**: High. Directly enables remix workflows. Trivial integration via Replicate.
- **Note**: Memory item `project_future_stem_splitter_plugin` already tracks this as the long-term plan.

---

## Tier 2 — Strong but overlaps existing capabilities or has friction

### MiniMax / Hailuo (Video gen)

- **Category**: Video generation
- **Gap filled**: Best naturalistic human motion; strongest for people-heavy content
- **API**: MiniMax API (api.minimaxi.com) + Replicate
- **Capabilities**: Text-to-video, image-to-video, ~6s clips, very fluid human motion
- **Pricing**: Competitive credit-based
- **Integration path**: Replicate or native API
- **Priority**: Medium-high. Niche but valuable — none of the current providers match its human motion quality.

### Recraft V3 (Image gen)

- **Category**: Image generation
- **Gap filled**: Unique SVG/vector output; topped quality benchmarks
- **API**: Native API (OpenAI-compatible format), also on Replicate
- **Capabilities**: Text-to-image, vectorization, background removal, style control
- **Pricing**: ~$0.04-0.08/image
- **Integration path**: OpenAI-compatible endpoint or Replicate
- **Priority**: Medium. Vector output is genuinely unique. Good if users need design assets.

### Suno v4 API (Music gen)

- **Category**: Music generation (vocals)
- **Gap filled**: Full vocal song generation with lyrics (if Musicful is instrumental-only)
- **API**: Official API launched late 2024 (developer access program)
- **Capabilities**: Text-to-song with vocals, lyrics, multiple genres, extend/continue
- **Pricing**: Subscription + credit-based
- **Integration path**: REST client; new provider pattern
- **Priority**: Medium. Depends on whether Musicful covers vocal generation. API maturity TBD.

### Stable Audio 2.0 (Stability AI)

- **Category**: Audio generation (music + SFX)
- **Gap filled**: Audio-to-audio style transfer (unique capability)
- **API**: Stability API, also on Replicate; open-weight variant available
- **Capabilities**: Text-to-music, text-to-SFX, audio-to-audio, up to 3min @ 44.1kHz stereo
- **Pricing**: ~$0.01-0.05/generation
- **Integration path**: Replicate or Stability API
- **Priority**: Medium. Audio-to-audio is unique and useful for iterative editing. Stability AI business stability is a risk.

---

## Tier 3 — Good to have, lower urgency

### Wan 2.1 (Alibaba) — Video gen

- Open weights (Apache 2.0), self-hostable, zero per-call cost
- Text-to-video, image-to-video, up to 720p-1080p
- Available on Replicate
- Best for: high-volume generation or custom fine-tuning
- Note: `local.wan21-film-pipeline.md` already exists as a design doc

### Ideogram 3.0 — Image gen

- Best text-in-image rendering (logos, signage, typography)
- Official API + Replicate
- ~$0.03-0.08/image
- Best for: title cards, design-heavy keyframes

### MusicGen / AudioGen (Meta) — Audio gen

- Open source (MIT), Replicate-hosted
- MusicGen: instrumental music up to 30s; AudioGen: environmental SFX
- Free to self-host, ~$0.02-0.08/run on Replicate
- Best for: vendor-independent fallback, no lock-in

### Midjourney — Image gen

- Highest aesthetic quality for artistic styles
- Official API launched late 2024/early 2025
- Access may still be restricted/invite-based
- Best for: artistic/painterly keyframes

### CogVideoX — Video gen

- Open source (THUDM/Zhipu AI), Replicate-hosted
- Text-to-video, image-to-video, ~6s @ 720p
- Quality tier below commercial leaders
- Best for: self-hosting, research, custom pipelines

### HunyuanVideo (Tencent) — Video gen

- Open weights (13B params), Replicate-hosted
- Strong motion coherence
- Resource-intensive to self-host
- Best for: large open model if self-hosting GPU infra

---

## Not viable for integration (as of 2026-04-27)

| Model | Reason |
|-------|--------|
| Sora | No API (web-only via ChatGPT); also excluded per user preference (no OpenAI) |
| Udio | No public API (web-only) |
| Google MusicFX / MusicLM | No API (Google Labs only) |
| SeedDance 2.0 (ByteDance) | No weights, no API, research paper only |
| Vidu (Shengshu) | Limited API, not on Replicate, inconsistent quality |

---

## Recommended implementation order

1. **ElevenLabs** — voice + SFX (two gaps, one integration)
2. **FLUX Pro** — keyframe quality upgrade (Replicate pattern already exists)
3. **Demucs** — 4-stem separation (upgrades existing isolate_vocals)
4. **Luma Ray2** — video gen variety (Replicate or native API)
5. **MiniMax/Hailuo** — human motion video gen
6. **Recraft V3** — vector/design image gen
7. Remaining Tier 2-3 as needed

---

## Notes

- All research based on training knowledge through May 2025 + limited web verification April 2026. Field moves fast — re-verify API status and pricing before implementing.
- No formal provider abstraction exists yet (duck-typed classes). Consider defining a protocol/ABC if adding 2+ more providers.
- Replicate is the fastest integration path for most models (existing `KlingClient` pattern).
- Fal.ai is worth evaluating as a Replicate alternative — often faster cold-start times.
