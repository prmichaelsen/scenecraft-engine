# Task 2: Beat Detection & Analysis

**Milestone**: [M1 - Audio Analysis Pipeline](../../milestones/milestone-1-audio-analysis-pipeline.md)
**Design Reference**: [Requirements](../../design/requirements.md)
**Estimated Time**: 3 hours
**Dependencies**: Task 1
**Status**: Not Started

---

## Objective

Implement beat tracking, onset detection, tempo estimation, and beat intensity extraction using librosa. Produce structured beat data with timestamps and normalized intensity values.

---

## Context

This is the core analysis engine. librosa provides the beat_track and onset_detect functions, but we need to extract beat strength/intensity values and normalize them for downstream use. The quality of beat detection directly determines how good the generated effects will look.

---

## Steps

### 1. Implement Beat Tracking

In `analyzer.py`:
- Use `librosa.beat.beat_track()` to get tempo and beat frame positions
- Convert beat frames to timestamps via `librosa.frames_to_time()`
- Extract beat strength using onset envelope at beat positions

### 2. Implement Onset Detection

- Use `librosa.onset.onset_detect()` for onset timestamps
- Onset strength via `librosa.onset.onset_strength()`
- These provide sub-beat detail for more responsive effects

### 3. Extract Beat Intensity

- Compute onset envelope: `librosa.onset.onset_strength(y, sr=sr)`
- Sample envelope values at beat positions
- Normalize to 0.0 - 1.0 range (min-max normalization)
- Store as `intensity` per beat

### 4. Extract Tempo

- `librosa.beat.beat_track()` returns estimated tempo
- Store as BPM in beat map metadata

### 5. Create Analysis Function

```python
def analyze_audio(path: str, sr: int = 22050) -> dict:
    """Analyze audio file and return beat data."""
    # Returns dict with: tempo, beats (list of {time, frame, intensity}),
    # onsets, duration, sample_rate
```

### 6. Test with Sample Audio

- Test with a simple, steady-beat track
- Test with a complex track (tempo changes, varied dynamics)
- Verify beat timestamps align with audible beats

---

## Verification

- [ ] Beat tracking detects beats in a steady-tempo track
- [ ] Onset detection identifies note onsets
- [ ] Tempo estimation returns reasonable BPM value
- [ ] Beat intensity values are normalized 0.0-1.0
- [ ] Analysis completes in under 30 seconds for 5-minute track
- [ ] Results are returned as structured dict

---

**Next Task**: [Task 3: Beat Map Generation & CLI](task-3-beat-map-cli.md)
