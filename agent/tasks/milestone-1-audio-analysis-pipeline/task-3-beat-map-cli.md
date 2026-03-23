# Task 3: Beat Map Generation & CLI

**Milestone**: [M1 - Audio Analysis Pipeline](../../milestones/milestone-1-audio-analysis-pipeline.md)
**Design Reference**: [Requirements](../../design/requirements.md)
**Estimated Time**: 2 hours
**Dependencies**: Task 2
**Status**: Not Started

---

## Objective

Define the JSON beat map schema, implement frame-rate-aware timestamp-to-frame conversion, and build the CLI interface for running audio analysis.

---

## Context

The beat map JSON is the contract between Phase 1 (analysis) and Phase 2 (generation). It must be well-structured, frame-rate aware, and include all data needed to generate effects. The CLI provides the user-facing interface.

---

## Steps

### 1. Define Beat Map JSON Schema

```json
{
  "version": "1.0",
  "source_file": "track.mp3",
  "duration": 240.5,
  "tempo": 128.0,
  "fps": 30,
  "beats": [
    {
      "time": 0.523,
      "frame": 16,
      "intensity": 0.85
    }
  ],
  "onsets": [
    {
      "time": 0.101,
      "frame": 3,
      "strength": 0.42
    }
  ]
}
```

### 2. Implement Frame-Rate Conversion

In `beat_map.py`:
- `time_to_frame(time_sec: float, fps: float) -> int`
- Support standard rates: 23.976, 24, 25, 29.97, 30, 48, 60
- Round to nearest frame (not floor/ceil — nearest is most accurate)

### 3. Implement Beat Map Builder

```python
def create_beat_map(analysis: dict, fps: float, source_file: str) -> dict:
    """Convert analysis results to frame-rate-aware beat map."""
```

### 4. Build CLI with Click

In `cli.py`:
```
Usage: beatlab analyze <audio_file> [OPTIONS]

Options:
  --fps FLOAT     Timeline frame rate (default: 30)
  --output PATH   Output JSON file (default: stdout)
  --sr INT        Sample rate for analysis (default: 22050)
```

### 5. Wire Up __main__.py

- `python -m beatlab analyze track.mp3 --fps 30 --output beats.json`

### 6. Test End-to-End

- Run CLI with sample audio
- Verify JSON output is valid and contains expected fields
- Verify frame numbers are correct for different FPS values

---

## Verification

- [ ] Beat map JSON matches defined schema
- [ ] Frame conversion is correct for 24fps and 30fps
- [ ] CLI `beatlab analyze` command works end-to-end
- [ ] JSON output is valid and parseable
- [ ] `--fps` flag correctly changes frame numbers
- [ ] `--output` writes to specified file

---

**Next Task**: [Task 4: Fusion .setting Format Research](../../tasks/milestone-2-fusion-comp-generation/task-4-fusion-format-research.md)
