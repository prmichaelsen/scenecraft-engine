"""Volume curve evaluation helpers (M9 task-91).

Curves are stored as `[[x, db], ...]`. Clip curves use normalized x ∈ [0, 1];
track curves use absolute seconds. Evaluation: linear interpolation between
points; clamps to the nearest endpoint outside the point range.
"""

from __future__ import annotations

import numpy as np


def evaluate_curve_db(
    curve: list[list[float]] | None,
    t: np.ndarray,
    x_normalised: bool,
    clip_start: float = 0.0,
    clip_end: float = 0.0,
) -> np.ndarray:
    """Sample a dB curve at timeline positions `t` (seconds).

    - If `x_normalised=True`, the curve's x is mapped over `[clip_start, clip_end]`
      so sample at `t` means `(t - clip_start) / (clip_end - clip_start)`.
    - Else `t` is in absolute seconds and the curve's x is too.

    Returns dB values (float32 array, same shape as `t`). Default curve is 0 dB
    everywhere (unity gain) when `curve` is None/empty.
    """
    if not curve:
        return np.zeros_like(t, dtype=np.float32)

    pts = sorted(((float(p[0]), float(p[1])) for p in curve), key=lambda p: p[0])
    xs = np.array([p[0] for p in pts], dtype=np.float32)
    ys = np.array([p[1] for p in pts], dtype=np.float32)

    if x_normalised:
        span = max(clip_end - clip_start, 1e-9)
        sample_x = ((t - clip_start) / span).astype(np.float32)
    else:
        sample_x = t.astype(np.float32)

    # np.interp already clamps to endpoint y-values outside the x range
    return np.interp(sample_x, xs, ys).astype(np.float32)


def db_to_linear(db: np.ndarray) -> np.ndarray:
    """Convert dB (signed) → linear gain factor. 0 dB → 1.0, -6 dB → ~0.501, -60 dB → ~0.001."""
    return np.power(10.0, db / 20.0).astype(np.float32)


def evaluate_curve_linear(
    curve: list[list[float]] | None,
    t: np.ndarray,
    x_normalised: bool,
    clip_start: float = 0.0,
    clip_end: float = 0.0,
) -> np.ndarray:
    """Convenience: evaluate dB curve and convert to linear gain."""
    return db_to_linear(evaluate_curve_db(curve, t, x_normalised, clip_start, clip_end))
