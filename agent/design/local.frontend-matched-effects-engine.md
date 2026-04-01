# Frontend-Matched Effects Engine

**Concept**: Align backend OpenCV effects rendering with frontend WebGL shader behavior for consistent zoom/shake/brightness output
**Created**: 2026-04-01
**Status**: Proposal

---

## Overview

The backend effects engine (`effects_opencv.py`) and the frontend preview (`BeatEffectPreview.tsx`) render beat-synced visual effects independently using different approaches. The backend uses OpenCV (crop-zoom, warpAffine), the frontend uses WebGL shaders (UV remap). This design describes changes attempted to make the backend match the frontend's visible output, the problems encountered, and the proposed correct approach.

---

## Problem Statement

- Backend zoom effects were imperceptible — zoom_pulse and zoom_bounce produced no visible result in rendered videos
- Backend shake was pixel-based (8px fixed) — invisible at 1080p
- Frontend preview looked correct; backend renders did not match
- Pre-computed `layer3_events` had fewer events than the frontend due to backend bleed suppression
- `time_offset` parameter for trimmed clips was error-prone and repeatedly caused effects to target wrong frames

---

## Changes Attempted (to be reverted)

### 1. Zoom Factor Reduction (REVERTED)
- **Before**: `zoom_amount = max(zoom_amount, 0.12 * ei)` for zoom_pulse, `0.20 * ei` for zoom_bounce, applied as `1.0 + zoom_amount`
- **Changed to**: Raw `ei` passed through, then `1.0 + zoom_amount * 0.06` at apply time
- **Net effect**: Reduced max zoom from 12-20% to 6%
- **Problem**: 6% zoom with quadratic decay produces sub-1% actual zoom per frame — invisible

### 2. Decay Envelope Changed to Quadratic (REVERTED)
- **Before**: Linear attack/sustain/release envelope
- **Changed to**: `intensity * (1 - dt/duration)^2` matching frontend
- **Problem**: Combined with reduced zoom factor, intensity drops too fast to be visible. The frontend gets away with this because: (a) it renders at 60fps so more frames catch the peak, and (b) the shader applies zoom as UV remap which visually reads differently than crop-zoom

### 3. Brightness Pulse Added (REVERTED)
- Added `frame = cv2.convertScaleAbs(frame, alpha=1.0 + zoom_amount * 0.3, beta=0)` after zoom
- Intended to match frontend's `color.rgb *= 1.0 + u_zoom * 0.3`
- **Problem**: This is not the primary visual cue — zoom is

### 4. Shake Changed to Percentage-Based (REVERTED)
- **Before**: `int(8 * ei * math.sin(t * 47))` — fixed 8px
- **Changed to**: `ei * 0.01 * w` — 1% of frame width
- **Problem**: Direction was correct but needs tuning alongside other changes

### 5. Zoom Bounce Suppression Removed (REVERTED)
- Removed the first-pass loop that checked for active zoom_bounce before allowing zoom_pulse
- **Problem**: May have been needed to prevent zoom conflicts

### 6. `_apply_rules_client()` Added (KEEP — do not revert)
- New function that applies rules to onsets matching frontend's `applyRulesClient`
- No bleed suppression — produces more events than backend `apply_rules()`
- `intel_path` parameter added to `apply_effects_ai()` to use this mode
- **This is correct** — frontend always uses client-side rule application

### 7. Various Zoom Modes Tried (all REVERTED)
- Additive zoom (sum all active zoom events, clamp at 5.0)
- "Latest wins" (most recent zoom event cuts off all previous)
- All produced different but still unsatisfying results

---

## Root Cause Analysis

The core issue was **not** the zoom/shake factors. The effects were being computed correctly but:

1. **`time_offset` was wrong or confusing** — events have absolute timestamps (e.g., 572s) and need to be aligned with video frames that start at 0. Pre-shifting events by subtracting the clip start time and using `time_offset=0` is more reliable.

2. **The original zoom factors (12-20%) were actually reasonable** — reducing to 6% to "match frontend" was wrong because the frontend's 6% UV remap is visually more impactful than a 6% crop-zoom, and the frontend runs at 60fps capturing more peak frames.

3. **Pre-computed events miss coverage** — backend bleed suppression filters valid events. The `_apply_rules_client()` function correctly addresses this.

---

## Proposed Solution

1. **Keep `_apply_rules_client()`** — always apply rules to onsets at render time, no bleed suppression
2. **Keep original zoom/shake factors** — 12% zoom_pulse, 20% zoom_bounce, 8px shake (or tune separately)
3. **Keep original linear decay envelope** — it was working before
4. **Pre-shift events instead of using time_offset** — subtract clip start time from event times, pass `time_offset=0`
5. **Tune effects separately** once the pipeline is confirmed working end-to-end

---

## Files Modified

| File | Change | Keep/Revert |
|------|--------|-------------|
| `src/beatlab/render/effects_opencv.py` | Zoom/shake/decay changes | **REVERT** |
| `src/beatlab/render/effects_opencv.py` | `_apply_rules_client()` + `intel_path` param | **KEEP** |
| `src/beatlab/audio_intelligence.py` | Energy guidance removal from sections prompt | **KEEP** |
| `src/beatlab/audio_intelligence.py` | Disabled effects support | **KEEP** |
