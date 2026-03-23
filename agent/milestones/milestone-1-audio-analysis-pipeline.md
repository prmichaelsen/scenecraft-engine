# Milestone 1: Audio Analysis Pipeline

**Goal**: Build the Python audio analysis pipeline that takes audio files and produces structured JSON beat maps
**Duration**: 1-2 weeks
**Dependencies**: None
**Status**: Not Started

---

## Overview

This milestone establishes the core audio analysis capability — the foundation of the entire project. Using librosa, we'll build a pipeline that accepts audio files (WAV, MP3, FLAC), performs beat tracking, onset detection, tempo estimation, and spectral analysis, then outputs a structured JSON beat map with frame-accurate timestamps.

This is Phase 1 of the two-phase architecture. Everything downstream (effect generation, Resolve integration) depends on accurate, well-structured beat maps.

---

## Deliverables

### 1. Python Project Setup
- Python package with proper structure (pyproject.toml or setup.py)
- Dependencies: librosa, soundfile, numpy
- CLI entry point for running analysis

### 2. Audio Analysis Module
- Beat tracking (librosa.beat.beat_track)
- Onset detection (librosa.onset.onset_detect)
- Tempo estimation
- Beat strength/intensity extraction
- Spectral feature extraction (for later section detection)

### 3. Beat Map Output
- JSON schema for beat map format
- Frame-rate-aware timestamp conversion (24, 25, 29.97, 30, 60 fps)
- Beat intensity normalization (0.0 - 1.0 scale)
- CLI tool: `python -m beatlab analyze <audio_file> --fps 30 --output beats.json`

---

## Success Criteria

- [ ] Accepts WAV and MP3 audio files without error
- [ ] Detects beats with reasonable accuracy on typical music tracks
- [ ] Outputs valid JSON beat map with timestamps, intensities, and tempo
- [ ] Frame numbers are correct for specified frame rate
- [ ] Processes a 5-minute audio file in under 30 seconds
- [ ] CLI tool works end-to-end

---

## Key Files to Create

```
davinci-beat-lab/
├── pyproject.toml
├── src/
│   └── beatlab/
│       ├── __init__.py
│       ├── __main__.py
│       ├── analyzer.py
│       ├── beat_map.py
│       └── cli.py
└── tests/
    ├── __init__.py
    └── test_analyzer.py
```

---

## Tasks

1. [Task 1: Project Setup & Audio Loader](../tasks/milestone-1-audio-analysis-pipeline/task-1-project-setup.md) - Python project structure, dependencies, audio file loading
2. [Task 2: Beat Detection & Analysis](../tasks/milestone-1-audio-analysis-pipeline/task-2-beat-detection.md) - librosa beat tracking, onset detection, intensity extraction
3. [Task 3: Beat Map Generation & CLI](../tasks/milestone-1-audio-analysis-pipeline/task-3-beat-map-cli.md) - JSON beat map output, frame-rate conversion, CLI interface

---

## Testing Requirements

- [ ] Unit tests for beat map schema validation
- [ ] Unit tests for frame-rate conversion math
- [ ] Integration test with a sample audio file
- [ ] CLI end-to-end test

---

## Risks and Mitigation

| Risk | Impact | Probability | Mitigation Strategy |
|------|--------|-------------|---------------------|
| librosa beat detection inaccurate for certain genres | Medium | Medium | Expose sensitivity parameters; consider madmom as alternative |
| Large audio files slow to process | Low | Low | librosa is well-optimized; can downsample if needed |

---

**Next Milestone**: [M2 - Fusion Comp Generation](milestone-2-fusion-comp-generation.md)
**Blockers**: None
**Notes**: This milestone produces a standalone useful tool — the beat map JSON can be consumed by any downstream system, not just Fusion.
