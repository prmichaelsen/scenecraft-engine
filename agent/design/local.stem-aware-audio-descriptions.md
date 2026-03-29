# Stem-Aware Audio Descriptions

**Concept**: Run Gemini audio analysis on isolated stems (not just the full mix) for precise per-instrument descriptions, with chunked audio to avoid API size limits
**Created**: 2026-03-29
**Status**: Proposal

---

## Overview

The current audio description pipeline sends full-mix audio segments to Gemini, which tries to identify all instruments from the combined signal. With multi-model stem separation now available (MDX23C-InstVoc + DrumSep + Demucs 6s), we can send isolated stems to Gemini instead — it hears only the kick, only the vocals, only the piano — producing dramatically more precise descriptions.

Additionally, the current `audio_describer.py` sends raw audio segments without chunking, causing API failures on long segments. This design adds chunking (max 30s per API call) to both the full-mix and per-stem description paths.

---

## Problem Statement

- **Full-mix descriptions miss detail**: Gemini hearing the full mix describes "drums and synths" generically. Hearing the isolated kick track, it can describe the exact kick pattern, ghost notes, fills.
- **Long segments fail**: `audio_describer.py` sends entire section groups (sometimes minutes long) as raw WAV to Gemini, exceeding input limits and causing timeouts.
- **Descriptions cap at 30**: The old `max_sections=30` sampled every Nth group, leaving 160/190 sections with "Continuation of previous section." (Now removed, but chunking still needed.)
- **Two separate code paths**: `audio_describer.py` (descriptions.md) and `audio_intelligence.py` (_gemini_describe_chunk) have different prompts and chunking behavior.

---

## Solution

### Approach: Hybrid 4-Stem Descriptions

Run Gemini on 4 stems per section, then combine into one rich description:

| Stem | Source | What Gemini Hears | Description Focus |
|---|---|---|---|
| Full mix | Original audio | Everything | Overall vibe, mood, energy arc, production quality |
| Drums | DrumSep instrumental | Isolated percussion | Kick/snare/hh patterns, fills, rhythm precision |
| Vocals | MDX23C-InstVoc | Isolated vocals/synth voices | Vocal timing, character, lyrics, phrasing |
| Other/synths | Demucs 6s | Synths, pads, FX | Synth stabs, pad swells, melodic content, sustained sounds |

**Why not all 11 stems?** Cost. 11 stems × 100 groups = 1100 API calls. 4 stems × 100 = 400 calls — still comprehensive but 3x cheaper.

**Optional per-stem deep dives**: For sections flagged as high-energy or complex, also send bass, guitar, piano individually.

### Chunking

All audio sent to Gemini is chunked to max 30 seconds per API call:
- Section group spans 0:00-0:45 → two chunks (0:00-0:30, 0:30-0:45)
- Each chunk gets its own Gemini call
- Descriptions from multiple chunks are concatenated

### Combined Description Format

Per section, the output is:

```markdown
## Section 42 (chorus, high_energy)
**Time**: 567.1s - 575.1s

### Full Mix
Overall high-energy EDM with four-on-the-floor kick...

### Drums
Kick: four-on-the-floor at [0:00], [0:00.46], [0:00.92]...
Snare: hits on beats 2 and 4...
Hi-hat: sixteenth note pattern throughout...

### Vocals
Ethereal processed female vocal enters at [0:02], sustained...

### Synths/Other
Massive supersaw chord stab at [0:00], sustained 2.3s...
Rising filter sweep from [0:05] to [0:07]...
```

---

## Implementation

### Step 1: Add chunking to `audio_describer.py`

Update `GeminiAudioDescriber.describe()` to chunk audio > 30s:

```python
def describe(self, audio, sr, max_chunk_seconds=30):
    if len(audio) / sr > max_chunk_seconds:
        # Split into chunks, describe each, concatenate
        chunks = split_audio(audio, sr, max_chunk_seconds)
        descriptions = [self._describe_chunk(chunk, sr) for chunk in chunks]
        return "\n\n".join(descriptions)
    else:
        return self._describe_chunk(audio, sr)
```

### Step 2: Add `describe_with_stems()` to `audio_describer.py`

New function that takes stem paths + section time range, runs Gemini on each stem:

```python
def describe_with_stems(
    describer, stem_paths, sr, start_time, end_time,
    stems_to_describe=("full_mix", "drums", "vocals", "other"),
):
    descriptions = {}
    for stem_name in stems_to_describe:
        audio = load_stem_segment(stem_paths[stem_name], sr, start_time, end_time)
        descriptions[stem_name] = describer.describe(audio, sr)
    return combine_descriptions(descriptions)
```

### Step 3: Update `describe_sections()` to support stems

Add optional `stem_paths` parameter. When provided, uses per-stem descriptions. When not, falls back to full-mix only.

### Step 4: Wire into CLI

The `--describe` flag on the `render` command and the `audio-intelligence` pipeline should both support `--stem-descriptions` to use the multi-stem approach when stems are available.

---

## Benefits

- **Precision**: Gemini hearing isolated kick vs full mix = night and day for rhythm description
- **No size limit failures**: 30s chunks always fit within Gemini's input limits
- **Full coverage**: No section cap, all 190 sections described
- **Reusable**: Per-stem descriptions feed into both descriptions.md and the audio intelligence pipeline
- **Graceful degradation**: Falls back to full-mix if stems aren't available

---

## Trade-offs

- **Cost**: 4x more Gemini API calls than full-mix only. ~400 calls for a 35-min track vs ~100.
- **Time**: ~4x longer to generate descriptions. Mitigated by parallel API calls per stem.
- **Stem quality**: Description quality depends on stem separation quality. Bad separation → misleading descriptions.

---

## Dependencies

- Multi-model stem separation (MDX23C-InstVoc + DrumSep + Demucs 6s) — for isolated stems
- Gemini API (google-genai) — for audio analysis
- Existing `audio_describer.py` — extended, not replaced

---

## Migration Path

1. **Phase 1** (immediate): Fix chunking in `audio_describer.py` so full-mix descriptions work on long segments. Remove section cap.
2. **Phase 2**: Add `describe_with_stems()` for per-stem descriptions when stems are available.
3. **Phase 3**: Unify with `audio_intelligence.py` Layer 2 — single code path for all Gemini audio analysis.

---

**Status**: Proposal
**Recommendation**: Implement Phase 1 immediately (chunking fix), Phase 2 after stems are cached for beyond_the_veil_v2.
**Related Documents**: [local.multi-model-stem-pipeline.md](local.multi-model-stem-pipeline.md), [local.multi-layer-audio-intelligence.md](local.multi-layer-audio-intelligence.md)
