# Task 1: Project Setup & Audio Loader

**Milestone**: [M1 - Audio Analysis Pipeline](../../milestones/milestone-1-audio-analysis-pipeline.md)
**Design Reference**: [Requirements](../../design/requirements.md)
**Estimated Time**: 2 hours
**Dependencies**: None
**Status**: Not Started

---

## Objective

Set up the Python project structure with pyproject.toml, install dependencies (librosa, soundfile, numpy), and implement audio file loading that accepts WAV, MP3, and FLAC formats.

---

## Context

This is the foundation task. All subsequent audio analysis and generation work depends on having a properly structured Python package with the right dependencies and a reliable audio loading layer.

---

## Steps

### 1. Create Python Package Structure

```
davinci-beat-lab/
├── pyproject.toml
├── src/
│   └── beatlab/
│       ├── __init__.py
│       ├── __main__.py
│       └── analyzer.py
└── tests/
    └── __init__.py
```

### 2. Configure pyproject.toml

- Project name: `davinci-beat-lab`
- Dependencies: librosa, soundfile, numpy, click (for CLI)
- Entry point: `beatlab` CLI command
- Python requires: >=3.10

### 3. Implement Audio Loader

In `analyzer.py`:
- Function `load_audio(path: str, sr: int = 22050) -> tuple[np.ndarray, int]`
- Accept WAV, MP3, FLAC via librosa.load()
- Validate file exists and is a supported format
- Return audio time series and sample rate

### 4. Verify Setup

- `pip install -e .` succeeds
- `python -c "import beatlab"` works
- Audio loader reads a test file without error

---

## Verification

- [ ] pyproject.toml exists with correct dependencies
- [ ] `pip install -e .` completes without error
- [ ] `import beatlab` works in Python
- [ ] Audio loader accepts WAV files
- [ ] Audio loader accepts MP3 files
- [ ] Proper error message for unsupported formats

---

**Next Task**: [Task 2: Beat Detection & Analysis](task-2-beat-detection.md)
