"""Typed row shapes for M13 effect-curves + macro-panel tables.

Sibling module to `db.py` (which is a flat module, not a package). Mirrors the
TypeScript interfaces in `scenecraft/src/lib/audio-effect-types.ts` exactly —
field names are identical so client-side code can deserialize JSON responses
into these shapes without remapping.

These dataclasses are *return shapes* from the CRUD helpers. They are NOT the
SQL row objects — callers receive them as fully-decoded Python values
(``points`` is a list of ``[time, value]`` pairs, not a JSON string;
``enabled`` / ``visible`` are ``bool``, not ``0``/``1``). If you need the raw
row for bulk processing, read from the connection directly.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


# CurvePoint = [time_seconds, value_normalized_0_to_1]
CurvePoint = list[float]


@dataclass
class TrackEffect:
    id: str
    track_id: str
    effect_type: str
    order_index: int
    enabled: bool
    static_params: dict[str, Any] = field(default_factory=dict)
    created_at: str = ""


@dataclass
class EffectCurve:
    id: str
    effect_id: str
    param_name: str
    points: list[CurvePoint] = field(default_factory=list)
    interpolation: str = "bezier"  # 'bezier' | 'linear' | 'step'
    visible: bool = False


@dataclass
class SendBus:
    id: str
    bus_type: str  # 'reverb' | 'delay' | 'echo'
    label: str
    order_index: int
    static_params: dict[str, Any] = field(default_factory=dict)


@dataclass
class TrackSend:
    track_id: str
    bus_id: str
    level: float  # 0..1, animatable via a curve on synthetic effect_type '__send'


@dataclass
class FrequencyLabel:
    id: str
    label: str
    freq_min_hz: float
    freq_max_hz: float


__all__ = [
    "CurvePoint",
    "TrackEffect",
    "EffectCurve",
    "SendBus",
    "TrackSend",
    "FrequencyLabel",
]
