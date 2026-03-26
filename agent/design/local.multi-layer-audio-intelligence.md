# Multi-Layer Audio Intelligence

**Concept**: Three-layer audio analysis pipeline — DSP signal extraction, Gemini audio listening, Claude creative direction — for precise, musically-aware beat effect sync
**Created**: 2026-03-26
**Status**: Proposal

---

## Overview

Current beat sync uses librosa onset detection on isolated stems, but treats all onsets equally — a hi-hat ghost note triggers the same effect type as a massive synth stab. This design introduces a three-layer pipeline that extracts maximum signal from audio via multiple DSP tools, enriches it with Gemini's audio listening for musical context, and lets Claude make creative effect decisions with full knowledge of what's actually happening in the music.

---

## Problem Statement

- Librosa onsets are blunt — they detect energy transients but don't distinguish kick from snare from hi-hat from synth stab
- Sustained sounds (pad swells, held synth stabs) are missed entirely by onset detection — only the attack is captured, not the sustain
- No musical context — the effects engine doesn't know if a section is a buildup, a drop, a breakdown, or a verse
- Single-tool analysis gives one perspective — different DSP tools have different strengths

---

## Solution

### Layer 1: Signal Extraction (DSP — precise, cheap, exhaustive)

Run multiple analysis tools on each stem to extract a rich multi-dimensional signal picture.

**Per-stem frequency-band separation** (4 stems x 3 bands = 12 sub-signals):
- Low band (~20-200Hz): kicks, sub-bass
- Mid band (~200-2kHz): snares, vocals, melodic content
- High band (~2kHz+): hi-hats, cymbals, sibilance, air

**Per sub-signal extraction**:
- Onset detection (transient attack times + strength)
- RMS energy envelope (sustained vs transient — detects held synth stabs, pad swells)
- Spectral centroid over time (brightness changes)
- Spectral flux (rate of spectral change — detects timbral shifts)

**Second-opinion tools** (beyond librosa):
- aubio or madmom for beat/onset cross-validation
- essentia for rhythm patterns, tonal analysis, key detection

**Output**: Dense time-series data per stem per band — the raw truth of what's in the audio at subsample precision.

### Layer 2: Musical Context (Gemini — listens to actual audio)

Feed audio chunks (~30s windows) to Gemini with the prompt: "Describe what instruments are playing, their patterns, any sustained sounds, buildups, drops, transitions. Be specific about timing within the chunk."

**Gemini provides** (~1s precision but rich semantics):
- Instrument identification (kick pattern, snare pattern, synth stab type)
- Musical structure (buildup, drop, breakdown, verse, chorus)
- Sustained sound detection ("the synth stab enters and holds for ~3 seconds")
- Energy arc ("tension building, then explosive release at the drop")
- Genre/style context that informs effect choices

**Output**: Per-chunk natural language descriptions of musical events and patterns.

### Layer 3: Creative Direction (Claude — synthesizes everything)

Claude receives ALL data from Layers 1 and 2 plus user creative direction, and outputs frame-accurate effect assignments.

**Claude receives**:
- All DSP data (onset times, RMS envelopes, spectral features) per stem per band
- Gemini's musical descriptions per chunk
- User creative direction (style, mood, intensity preferences)
- Available effect presets and their parameters

**Claude outputs**:
- Per-onset effect assignments with exact timing, duration, and intensity
- Sustained effect regions (for held synth stabs, pad swells)
- Effect suppression zones (during vocals, quiet passages)
- Creative rationale for decisions

**Key capability**: Claude can correlate precise DSP timestamps with Gemini's semantic descriptions to make decisions like "the onset at 17:16.234 is the synth stab Gemini described as 'massive sustained chord' — apply zoom_bounce with 2.1s sustain matching the RMS envelope decay."

---

## Benefits

- **Musical awareness**: Effects respond to what's actually happening in the music, not just energy spikes
- **Sustained sound support**: RMS envelopes detect held sounds — synth stabs, pad swells, vocal holds — and effects can sustain with them
- **Instrument-specific routing**: Kick → zoom, snare → flash, hi-hat → ignore, synth stab → sustained glow
- **Multi-tool confidence**: Cross-validating onset detection across librosa + aubio/madmom reduces false positives
- **Creative intelligence**: Claude makes artistic decisions, not just algorithmic ones
- **Precision**: DSP provides frame-accurate timestamps, LLMs provide musical understanding — best of both

---

## Trade-offs

- **Cost**: Gemini API calls for audio chunks + Claude API calls for effect planning. Mitigated by chunking efficiently and caching results.
- **Latency**: Three-layer pipeline is slower than pure DSP. Mitigated by parallelizing Gemini chunks and caching all intermediate results.
- **Complexity**: More moving parts than simple onset detection. Mitigated by clear layer boundaries and cached intermediates at each stage.

---

## Dependencies

- librosa (existing)
- aubio or madmom (new DSP second opinion)
- essentia (new, optional — rhythm/tonal analysis)
- Gemini API (existing — used for audio descriptions already)
- Claude API (existing — used for AI director already)

---

**Status**: Proposal
**Recommendation**: Prototype on a 2-minute trim of beyond_the_veil to validate the approach before scaling to full track
**Related Documents**: [local.beatlab-server.md](local.beatlab-server.md), [local.platform-architecture.md](local.platform-architecture.md)
