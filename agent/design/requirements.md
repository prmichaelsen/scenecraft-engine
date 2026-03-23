# Project Requirements

**Project Name**: davinci-beat-lab
**Created**: 2026-03-23
**Status**: Active

---

## Overview

An AI-powered DaVinci Resolve plugin that analyzes audio waveforms, detects beats, and auto-generates beat-synced visual effects (zooms, pulses, flashes, color shifts). The tool takes an audio file, produces a beat map via Python audio analysis, then generates importable Fusion .setting files with keyframed effects aligned to beat timestamps.

---

## Problem Statement

Creating beat-synced visual effects in DaVinci Resolve is tedious manual work — editors must listen to audio, mark beats by hand, and keyframe effects one at a time. This project automates that entire workflow: analyze audio, detect beats, and generate ready-to-import Fusion compositions with properly keyframed effects.

---

## Goals and Objectives

### Primary Goals
1. Detect beats, onsets, and tempo from audio files using librosa/madmom
2. Generate a structured beat map (JSON) with timestamps, intensities, and frequency bands
3. Produce Fusion .setting files with keyframed effects synced to detected beats
4. Importable into DaVinci Resolve via Workspace > Scripts or Fusion comp import

### Secondary Goals
1. Classify musical sections (verse, chorus, drop, buildup) for varied effect selection
2. Map beat energy/intensity to effect magnitude (stronger beats = bigger effects)
3. Support multiple effect types: zoom pulses, brightness flashes, glow, color shifts
4. Apply ease-in/ease-out curves for natural-looking keyframe transitions

---

## Functional Requirements

### Core Features
1. **Audio Analysis**: Accept audio files (WAV, MP3, FLAC), extract beats, onsets, tempo, and spectral features using librosa
2. **Beat Map Generation**: Output a JSON beat map with frame-accurate timestamps, beat intensity, frequency band data
3. **Fusion Comp Generation**: Produce .setting files containing Fusion compositions with keyframed Transform, BrightnessContrast, and Glow nodes
4. **Frame-Rate Aware**: Convert beat timestamps to frame numbers at the user's timeline frame rate (24, 25, 29.97, 30, 60 fps)

### Additional Features
1. **Section Detection**: Identify musical sections (intro, verse, chorus, bridge, drop) using spectral analysis or optional LLM classification
2. **Effect Palette**: Multiple effect presets (subtle pulse, hard flash, zoom bounce, color wash, glow swell)
3. **Intensity Mapping**: Scale effect magnitude based on beat strength — accented beats get stronger effects
4. **Spline Curves**: Use Fusion spline types for smooth keyframe interpolation instead of linear on/off

---

## Non-Functional Requirements

### Performance
- Process a 5-minute audio file in under 30 seconds
- Generate Fusion .setting files in under 5 seconds after analysis

### Compatibility
- DaVinci Resolve 18+ (Free and Studio)
- Python 3.10+
- Cross-platform: Windows, macOS, Linux

### Usability
- Single-command CLI invocation for MVP
- Clear error messages for unsupported audio formats
- Generated .setting files importable without manual editing

---

## Technical Requirements

### Technology Stack
- **Analysis**: Python 3.10+ with librosa for audio analysis
- **Beat Map Format**: JSON
- **Effect Generation**: Python generating Fusion .setting files (Lua-based Fusion comp format)
- **Distribution**: Script in Resolve's Scripts folder or standalone CLI tool

### Dependencies
- librosa: Beat tracking, onset detection, tempo estimation, spectral features
- soundfile: Audio file I/O
- numpy: Numerical processing
- Optional: madmom for alternative/complementary beat detection

### Architecture — Two-Phase Pipeline
- **Phase 1 (Python)**: Audio → librosa analysis → JSON beat map
- **Phase 2 (Python)**: Beat map → Fusion .setting file generation

---

## User Stories

### As a Video Editor
1. I want to drop an audio file and get beat-synced effects automatically so I don't spend hours keyframing manually
2. I want to choose the frame rate of my timeline so effects land on exact frames
3. I want to import generated effects into DaVinci Resolve's Fusion page directly

### As a Music Video Creator
1. I want different effects for different musical sections so my video feels dynamic
2. I want beat intensity reflected in effect magnitude so drops hit harder visually
3. I want smooth keyframe curves so effects don't look robotic

---

## Constraints

### Technical Constraints
- DaVinci Resolve's scripting API has incomplete keyframe control — using Fusion .setting file export (Option C) avoids this
- Resolve Studio vs Free have different scripting capabilities; .setting import works in both
- Fusion .setting format is undocumented; must reverse-engineer from exported examples

### Resource Constraints
- Single developer
- No access to DaVinci Resolve C++/OpenFX SDK (not needed for MVP)

---

## Success Criteria

### MVP Success Criteria
- [ ] Audio file → JSON beat map with accurate beat timestamps
- [ ] JSON beat map → Fusion .setting file with keyframed zoom/brightness effects
- [ ] .setting file imports into DaVinci Resolve and plays back with beat-synced effects
- [ ] Supports at least 24fps and 30fps frame rates
- [ ] Works with WAV and MP3 input

### Full Release Success Criteria
- [ ] Section-aware effect variation (chorus vs verse)
- [ ] Multiple effect presets selectable by user
- [ ] Intensity-mapped effect magnitudes
- [ ] Smooth spline-based keyframe curves
- [ ] Works as a Resolve script from Workspace > Scripts menu

---

## Out of Scope

1. **Real-time processing**: No live audio analysis; file-based only
2. **C++/OpenFX plugin**: Not building a compiled OFX plugin for MVP
3. **DCTL color transforms**: Using Fusion nodes, not DCTL
4. **GUI**: CLI-only for MVP; no graphical interface
5. **Resolve Edit page API**: Using Fusion .setting export, not Edit page scripting
6. **AI model training**: Using existing librosa algorithms, not training custom models

---

## Assumptions

1. Users have Python 3.10+ installed or can install it
2. Users have DaVinci Resolve 18+ installed
3. Fusion .setting file format is stable across Resolve 18+ versions
4. librosa provides sufficiently accurate beat detection for music production audio
5. Users can navigate to Fusion page and import .setting files

---

## Risks

| Risk | Impact | Probability | Mitigation Strategy |
|------|--------|-------------|---------------------|
| Fusion .setting format undocumented | High | Medium | Reverse-engineer from exported comps; create test fixtures |
| Beat detection accuracy varies by genre | Medium | Medium | Support multiple detection algorithms; let user adjust sensitivity |
| Resolve version incompatibilities | Medium | Low | Test with Resolve 18 and 19; keep generated comps simple |
| Keyframe interpolation mismatch | Medium | Medium | Test spline types in Resolve; fallback to linear if needed |

---

**Status**: Active
**Last Updated**: 2026-03-23
