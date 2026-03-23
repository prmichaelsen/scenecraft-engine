# AI Effect Director

**Concept**: LLM-powered effect selection that analyzes audio sections and chooses beat-synced visual effects with intelligent variation and layering
**Created**: 2026-03-23
**Status**: Design Specification

---

## Overview

The AI Effect Director adds an `--ai` flag to the beatlab CLI that sends audio analysis data (sections, spectral features, beat intensities) to an LLM, which returns a structured JSON "effect plan" mapping each musical section to specific presets, parameters, and layering decisions. The existing generator then consumes this plan to produce the Fusion .setting file.

This is the key differentiator — instead of uniform effects across the whole track or simple rule-based section mapping, the LLM makes creative decisions: subtle glow for an intro, layered flash + zoom_bounce for drops, variations on repeated choruses, and parameter tuning (attack/release, intensity curves) per section.

---

## Problem Statement

The current system applies the same effect(s) uniformly to every beat, or uses simple energy-threshold rules to vary effects by section. This produces technically correct but artistically flat results. A human editor would make creative choices — gentler effects for quiet passages, harder hits for drops, variation across repeated sections, and layering multiple effects for impact. The AI Effect Director automates these creative decisions.

---

## Solution

### Two-Step Architecture

1. **Analyzer** (existing) produces a beat map with section-level spectral summaries
2. **AI Director** (new) sends section data to Claude, receives a structured JSON effect plan
3. **Generator** (existing, extended) consumes the effect plan and produces .setting file

The LLM never touches .setting files directly. It operates purely as a creative decision-maker that outputs structured data the generator already knows how to consume.

### Effect Plan Schema

```json
{
  "sections": [
    {
      "section_index": 0,
      "presets": ["zoom_pulse"],
      "custom_effects": [],
      "intensity_curve": "linear",
      "attack_frames": 2,
      "release_frames": 4,
      "notes": "Gentle intro, minimal visual movement"
    },
    {
      "section_index": 3,
      "presets": ["flash", "zoom_bounce"],
      "custom_effects": [
        {
          "node_type": "Transform",
          "parameter": "Size",
          "base_value": 1.0,
          "peak_value": 1.25,
          "attack_frames": 1,
          "release_frames": 8,
          "curve": "smooth"
        }
      ],
      "intensity_curve": "exponential",
      "attack_frames": 1,
      "release_frames": 3,
      "notes": "High-energy drop, layered effects with custom zoom"
    }
  ]
}
```

### LLM Prompt Design

The system prompt includes:
1. **Preset catalog** — all registered presets with descriptions, parameters, and guidance on when each looks good
2. **Section data** — per-section summaries with: type (low/mid/high energy), duration, beat count, avg intensity, spectral features (centroid, RMS energy, rolloff, contrast)
3. **Coherence instruction** — maintain visual consistency across similar sections, introduce variation on repeats
4. **Output format** — JSON schema with examples

The user prompt (`--prompt`) is appended as additional creative direction.

---

## Implementation

### New Files

```
src/beatlab/
├── ai/
│   ├── __init__.py
│   ├── provider.py        # LLMProvider ABC + AnthropicProvider
│   ├── director.py        # Effect plan generation logic
│   ├── prompt.py          # System/user prompt construction
│   └── plan.py            # EffectPlan schema + validation
```

### Provider Abstraction

```python
from abc import ABC, abstractmethod

class LLMProvider(ABC):
    @abstractmethod
    def complete(self, system: str, user: str) -> str:
        """Send a prompt and return the response text."""
        ...

class AnthropicProvider(LLMProvider):
    def __init__(self, api_key: str | None = None, model: str = "claude-sonnet-4-20250514"):
        self.client = anthropic.Anthropic(api_key=api_key)  # falls back to ANTHROPIC_API_KEY env
        self.model = model

    def complete(self, system: str, user: str) -> str:
        response = self.client.messages.create(
            model=self.model,
            max_tokens=4096,
            system=system,
            messages=[{"role": "user", "content": user}],
        )
        return response.content[0].text
```

### Spectral Features (Analyzer Extension)

Add to `detect_sections()` — per-section spectral summaries:

```python
# Per section, compute:
spectral_centroid = np.mean(librosa.feature.spectral_centroid(y=segment, sr=sr))
rms_energy = np.mean(librosa.feature.rms(y=segment))
spectral_rolloff = np.mean(librosa.feature.spectral_rolloff(y=segment, sr=sr))
spectral_contrast = np.mean(librosa.feature.spectral_contrast(y=segment, sr=sr))
```

These are included in the section data sent to the LLM.

### CLI Integration

```
beatlab run track.mp3 --ai --prompt "cinematic with hard drops" -o comp.setting
beatlab run track.mp3 --ai -o comp.setting          # no user prompt, LLM decides freely
beatlab generate beats.json --ai -o comp.setting     # from existing beat map
```

- `--ai` flag activates the AI director
- `--prompt` optional freeform creative direction
- Requires `ANTHROPIC_API_KEY` env var (or passed programmatically)
- Errors out clearly if API key missing or API call fails

### Generator Extension

The generator's `generate_comp()` gets a new `effect_plan` parameter:

```python
def generate_comp(
    beat_map: dict,
    effect_plan: dict | None = None,  # NEW: from AI director
    ...
) -> FusionComp:
```

When `effect_plan` is provided, each section's beats use the plan's presets/params instead of the uniform or rule-based selection.

---

## Benefits

- **Creative automation** — LLM makes nuanced effect choices a human editor would make
- **Section awareness** — different effects for different parts of the song
- **Variation** — repeated sections get subtle differences to avoid monotony
- **Customizable** — `--prompt` lets users guide the creative direction
- **Composable** — builds on top of existing presets and generator, no architectural changes

---

## Trade-offs

- **API dependency** — requires Anthropic API key and network access for `--ai` mode
- **Cost** — ~$0.01-0.05 per track (acceptable per user confirmation)
- **Non-deterministic** — same track + prompt may produce different plans on different runs
- **Latency** — adds 2-5 seconds for the API call on top of analysis time

---

## Dependencies

- `anthropic` Python SDK (optional dependency, only for `--ai`)
- `ANTHROPIC_API_KEY` environment variable
- Existing section detection (`detect_sections()` in analyzer.py)
- Existing preset system (`presets.py`)

---

## Testing Strategy

- Unit tests for effect plan schema validation
- Unit tests for prompt construction (verify section data and preset catalog are included)
- Unit tests for plan-to-generator integration (mock LLM, verify correct presets applied per section)
- Integration test with mock provider returning a known plan
- Manual test: run `--ai` on test.wav and verify .setting output varies by section

---

## Key Design Decisions

### LLM Integration

| Decision | Choice | Rationale |
|---|---|---|
| Provider | Agnostic abstraction, Claude concrete | Keeps door open for other providers without over-engineering |
| API key | Required for --ai, from env or caller | Standard pattern, clear error on missing key |
| Output format | Structured JSON effect plan (Option A) | Deterministic, parseable, type-safe; generator already consumes structured data |
| Cost optimization | Section-level summaries, not per-beat | ~2-3K tokens vs ~20K; LLM doesn't need per-beat granularity |

### User Experience

| Decision | Choice | Rationale |
|---|---|---|
| Flag name | `--ai` | Plays up LLM capability |
| User prompt | `--prompt "freeform text"` | Maximum flexibility, no predefined style presets |
| Style presets | No `--style` flag | Covered by --prompt freeform text |
| Summary before generate | P1 (not P0) | Nice-to-have; section labels work for all genres including EDM |
| Accept/reject LLM choices | P1 (not P0) | Reduces P0 scope |
| Config file profiles | P2 | Deferred |

### Scope of LLM Decisions

| Decision | Choice | Rationale |
|---|---|---|
| Preset selection per section | Yes, LLM decides | Prompt includes preset descriptions and guidance |
| Parameter tuning | Yes | Attack/release, intensity curve per section |
| Custom effects | Yes | LLM can define ad-hoc presets beyond the catalog |
| Layering | Yes | Multiple presets per section for impact |
| Variation on repeats | Yes | Instruction in system prompt for coherence with variation |
| Context sent to LLM | Sections + raw spectral features | Richer signal for creative decisions |

### Error Handling

| Decision | Choice | Rationale |
|---|---|---|
| API failure | Error out | User explicitly requested AI; silent fallback would be surprising |
| Missing API key | Error out | Clear message about setting ANTHROPIC_API_KEY |

---

## Future Considerations (P1/P2)

- **P1**: Human-readable summary of LLM choices before generating
- **P1**: Accept/reject/modify LLM choices interactively
- **P1**: Tool-use (Option C) if JSON parsing proves unreliable
- **P2**: Config file for reusable style profiles
- **P2**: Support for additional LLM providers (OpenAI, local models)

---

**Status**: Design Specification
**Recommendation**: Plan M5 milestone and tasks, then implement
**Related Documents**: [Requirements](requirements.md), [Clarification 1](../clarifications/clarification-1-ai-effect-director.md)
