# Task 6: End-to-End Pipeline & CLI

**Milestone**: [M2 - Fusion Comp Generation](../../milestones/milestone-2-fusion-comp-generation.md)
**Design Reference**: [Requirements](../../design/requirements.md)
**Estimated Time**: 2 hours
**Dependencies**: Task 3, Task 5
**Status**: Not Started

---

## Objective

Wire up the full pipeline (audio → analysis → beat map → Fusion comp) into a single CLI command and verify end-to-end functionality.

---

## Steps

### 1. Add `generate` CLI Command

```
Usage: beatlab generate <beats.json> [OPTIONS]

Options:
  --output PATH     Output .setting file (default: output.setting)
  --effect TEXT     Effect type: zoom, flash, glow (default: zoom)
  --attack INT      Attack frames (default: 2)
  --release INT     Release frames (default: 4)
```

### 2. Add `run` CLI Command (Combined Pipeline)

```
Usage: beatlab run <audio_file> [OPTIONS]

Options:
  --fps FLOAT       Timeline frame rate (default: 30)
  --output PATH     Output .setting file (default: output.setting)
  --effect TEXT     Effect type (default: zoom)
  --beats-out PATH  Also save beat map JSON (optional)
```

### 3. Integration Test

- Run `beatlab run sample.mp3 --fps 30 --output test.setting`
- Verify both beat map and .setting are correct
- Import .setting into Resolve and verify playback

### 4. Error Handling

- Clear error for missing audio file
- Clear error for invalid beat map JSON
- Clear error for unsupported FPS values

---

## Verification

- [ ] `beatlab generate beats.json --output comp.setting` works
- [ ] `beatlab run audio.mp3 --fps 30 --output comp.setting` works end-to-end
- [ ] Error messages are clear for invalid inputs
- [ ] Generated .setting imports into Resolve
- [ ] `--beats-out` flag saves intermediate beat map

---

**Next Task**: [Task 7: Effect Preset Library](../../tasks/milestone-3-effect-library/task-7-effect-presets.md)
