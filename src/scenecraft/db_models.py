"""Typed row shapes for M13 effect-curves / macro-panel tables and
Phase 3 DSP + description analysis caches.

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
    # ``None`` means the effect processes the summed master bus (output of
    # masterGain, pre-destination). Non-None values scope to an audio track.
    track_id: str | None
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


# ── Phase 3: cached DSP analysis (librosa, quantitative) ────────────

@dataclass
class DspAnalysisRun:
    id: str
    source_segment_id: str
    analyzer_version: str
    params_hash: str
    analyses: list[str] = field(default_factory=list)  # deserialized from analyses_json
    created_at: str = ""


@dataclass
class DspDatapoint:
    run_id: str
    data_type: str          # 'onset' | 'rms' | 'spectral_centroid' | 'zcr' | ...
    time_s: float
    value: float
    extra: dict[str, Any] | None = None  # deserialized from extra_json


@dataclass
class DspSection:
    run_id: str
    start_s: float
    end_s: float
    section_type: str       # 'vocal_presence' | 'drop' | 'silence' | ...
    label: str | None = None
    confidence: float | None = None


@dataclass
class DspScalar:
    run_id: str
    metric: str             # 'tempo_bpm' | 'global_rms' | 'peak_db' | ...
    value: float


# ── Phase 3: cached LLM semantic descriptions (qualitative) ─────────

@dataclass
class AudioDescriptionRun:
    id: str
    source_segment_id: str
    model: str
    prompt_version: str
    chunk_size_s: float
    created_at: str = ""


@dataclass
class AudioDescription:
    run_id: str
    start_s: float
    end_s: float
    property: str           # 'section_type' | 'mood' | 'energy' | 'vocal_style' | 'genre' | ...
    value_text: str | None = None
    value_num: float | None = None
    confidence: float | None = None
    raw: dict[str, Any] | None = None  # deserialized from raw_json


@dataclass
class AudioDescriptionScalar:
    run_id: str
    property: str           # 'key' | 'global_genre' | 'vocal_gender' | ...
    value_text: str | None = None
    value_num: float | None = None
    confidence: float | None = None


# ── M15: cached master-bus mix analysis ─────────────────────────────

@dataclass
class MixAnalysisRun:
    id: str
    mix_graph_hash: str     # SHA-256 hex of canonical mix-graph serialization
    start_time_s: float
    end_time_s: float
    sample_rate: int
    analyzer_version: str
    analyses: list[str] = field(default_factory=list)  # deserialized from analyses_json
    rendered_path: str | None = None   # pool/mixes/{hash}.wav; None while rendering
    created_at: str = ""


@dataclass
class MixDatapoint:
    run_id: str
    data_type: str          # 'rms' | 'short_term_lufs' | 'spectral_centroid' | ...
    time_s: float
    value: float
    extra: dict[str, Any] | None = None  # deserialized from extra_json


@dataclass
class MixSection:
    run_id: str
    start_s: float
    end_s: float
    section_type: str       # 'clipping_event' | 'silence' | ...
    label: str | None = None
    confidence: float | None = None


@dataclass
class MixScalar:
    run_id: str
    metric: str             # 'peak_db' | 'true_peak_db' | 'lufs_integrated' | ...
    value: float


__all__ = [
    "CurvePoint",
    "TrackEffect",
    "EffectCurve",
    "SendBus",
    "TrackSend",
    "FrequencyLabel",
    "DspAnalysisRun",
    "DspDatapoint",
    "DspSection",
    "DspScalar",
    "AudioDescriptionRun",
    "AudioDescription",
    "AudioDescriptionScalar",
    "MixAnalysisRun",
    "MixDatapoint",
    "MixSection",
    "MixScalar",
]
