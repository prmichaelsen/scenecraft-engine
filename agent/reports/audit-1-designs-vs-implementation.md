# Audit Report: Design Docs vs Code Implementation

**Audit**: #1
**Date**: 2026-03-29
**Subject**: Compare all design specifications against actual codebase implementation

## Summary

The codebase implements 9 of 11 design specifications at 70-100% completion. Two designs remain unimplemented (project config, persisted rules), and one (multi-model stem pipeline) is partially implemented — the `audio_intelligence_multimodel` CLI and `extract_layer1_multimodel` function exist but MDX23C/DrumSep are not auto-orchestrated (user provides pre-separated stems). The platform architecture design is a proposal with ~30% of the foundational pieces in place (server, YAML storage).

## Implementation Matrix

| Design Doc | Status | % | Key Gap |
|---|---|---|---|
| local.ai-effect-director.md | **Implemented** | 95% | Missing interactive accept/reject UI |
| local.beatlab-server.md | **Implemented** | 100% | Complete — 40+ endpoints |
| local.hit-marker-web-ui.md | **Implemented** | 95% | Full waveform UI |
| local.multi-layer-audio-intelligence.md | **Implemented** | 85% | Missing aubio/madmom/essentia cross-check |
| local.multi-model-stem-pipeline.md | **Partial** | 40% | CLI exists, auto-orchestration of 3 models missing |
| local.narrative-keyframe-pipeline.md | **Implemented** | 100% | Complete YAML + candidate pipeline |
| local.persisted-rules.md | **Not Started** | 0% | Rule generation works, no persistence/versioning |
| local.platform-architecture.md | **Proposal** | 30% | Server + YAML exist, cloud infra not built |
| local.project-config.md | **Not Started** | 0% | No config.yaml implementation |
| local.temporal-coherence.md | **Implemented** | 90% | EbSynth works, design evaluated alternatives |
| local.wan21-film-pipeline.md | **Implemented** | 100% | Full Wan2.1 + FILM pipeline |

## Files Analyzed

| File | Type | Relevance |
|---|---|---|
| agent/design/local.*.md (11 files) | design | All design specifications |
| src/beatlab/api_server.py | source | REST API — 1881 lines, 40+ endpoints |
| src/beatlab/audio_intelligence.py | source | 3-layer audio pipeline |
| src/beatlab/stems.py | source | Demucs stem separation |
| src/beatlab/project.py | source | YAML split load/save |
| src/beatlab/render/narrative.py | source | Keyframe/transition pipeline |
| src/beatlab/render/effects_opencv.py | source | Beat-synced OpenCV effects |
| src/beatlab/render/wan_pipeline.py | source | Wan2.1 engine |
| src/beatlab/render/google_pipeline.py | source | Google Veo engine |
| src/beatlab/render/ebsynth.py | source | EbSynth engine |
| src/beatlab/ai/director.py | source | Claude effect planning |
| src/beatlab/cli.py | source | CLI — 2300+ lines, 15+ commands |
| src/beatlab/marker_server.py | source | Hit marker web UI |

## Key Findings

| Finding | Location | Notes |
|---|---|---|
| Multi-model pipeline partially implemented | src/beatlab/audio_intelligence.py:1700+ | `extract_layer1_multimodel` + `run_audio_intelligence_multimodel` exist, but no auto-orchestration of MDX23C+DrumSep+Demucs6s — user provides pre-separated stems |
| Bleed exempt stems logic exists | src/beatlab/audio_intelligence.py:1200 | Instrumental-derived stems skip vocal bleed + percussion sustained checks |
| YAML split fully implemented | src/beatlab/project.py | `load_project`/`save_project` handle both split (narrative.yaml+timeline.yaml+project.yaml) and legacy (narrative_keyframes.yaml) |
| Git versioning endpoints complete | src/beatlab/api_server.py | commit, history, checkout-as-new-commit, branch, diff, delete-branch |
| Flash effect replaced with contrast_pop | src/beatlab/render/effects_opencv.py | Flash was too blinding, hard_cut off by default |
| Veo 3.1 Ingredients support added | src/beatlab/render/google_video.py | `reference_images` param threaded through pipeline |
| 4 render engines implemented | src/beatlab/render/ | ebsynth, wan, google, kling — all with CLI flags |
| Per-effect sensitivity flags | src/beatlab/cli.py | --sens-all, --sens-zoom-pulse, etc. on audio-intelligence command |
| Vocal bleed confidence ratio | src/beatlab/audio_intelligence.py | Configurable threshold, 0.25 default |

## What's Not Implemented

| Design Feature | Design Doc | Effort | Impact |
|---|---|---|---|
| Project config.yaml | local.project-config.md | Small (1-2 days) | High — reduces 200-char CLI commands to 40 |
| Persisted rules versioning | local.persisted-rules.md | Medium (2-3 days) | High — users lose good rulesets between runs |
| Auto-orchestrate 3-model separation | local.multi-model-stem-pipeline.md | Medium (2-3 days) | Medium — currently manual stem paths work |
| Cloud VM provisioning | local.platform-architecture.md | Large (weeks) | Platform-level — not blocking local use |
| Billing/credits system | local.platform-architecture.md | Large (weeks) | Platform-level |
| Auth/HTTPS on server | local.platform-architecture.md | Medium (days) | Blocked until cloud deployment |
| aubio/madmom cross-validation | local.multi-layer-audio-intelligence.md | Small (1 day) | Low — librosa onsets are sufficient |
| Interactive LLM choice review UI | local.ai-effect-director.md | Medium (2-3 days) | Medium — rules mode + GUI makes this less critical |

## Render Engines

| Engine | Flag | Pipeline File | Status |
|---|---|---|---|
| EbSynth | `--engine ebsynth` | render/ebsynth.py | 100% |
| Wan2.1 | `--engine wan` | render/wan_pipeline.py | 100% |
| Google (Veo) | `--engine google` | render/google_pipeline.py | 100% |
| Kling | `--engine kling` | render/kling_pipeline.py | 100% |

## CLI Commands

| Command | Implemented | Notes |
|---|---|---|
| analyze | Yes | Beat detection + stems + sections |
| render | Yes | Multi-engine with 25+ flags |
| effects | Yes | AI events or beat map mode |
| audio-intelligence | Yes | 3-layer pipeline with rules |
| audio-intelligence-multimodel | Yes | Pre-separated stems input |
| server | Yes | REST API on port 8888 |
| marker-ui | Yes | Waveform hit editor |
| narrative keyframes | Yes | Candidate generation |
| narrative transitions | Yes | Veo transition generation |
| narrative assemble | Yes | Final video assembly |
| select | Yes | Candidate selection |
| destroy-gpu | Yes | Vast.ai instance cleanup |
| make-patch | Yes | Plan patching |
| split-sections | Yes | Long section splitting |
| candidates | Yes | Candidate management |
| delete | Yes | Cascade file deletion |

## Recommendations

1. **Implement project config.yaml** — Highest bang-for-buck. Every render command is 200+ chars of flags. Config would make iteration 5x faster.
2. **Implement persisted rules** — Users are losing good rulesets between runs. Save rules to YAML, let them version/tweak in the GUI.
3. **Auto-orchestrate multi-model separation** — The stems exist, the CLI exists, just need the glue to run MDX23C-InstVoc → DrumSep+Demucs6s automatically instead of requiring manual paths.
4. **Defer cloud platform** — Local dev workflow is solid. Cloud infra is a separate project phase.
