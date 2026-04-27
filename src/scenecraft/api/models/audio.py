"""Pydantic request bodies for the audio routers (M16 T62).

Every model declares ``model_config = ConfigDict(extra="ignore")`` so extra
fields from the legacy clients (camelCase aliases, experimental flags) don't
400 the request. This matches the legacy server's loose `_read_json_body()`
behavior where handlers fish out the fields they recognise and silently drop
the rest.

Schema naming uses the ``Audio*Body`` prefix to avoid colliding with other
M16-wave2 routers landing in parallel (tracks, transitions). Example:
``AddAudioTrackBody`` not ``AddTrackBody`` so the keyframe-track add body
(T61) and the audio-track add body don't both compile to the same OpenAPI
``AddTrackBody`` name.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


# ---------------------------------------------------------------------------
# Video tracks (the /tracks surface is still an audio-adjacent concern because
# the chat-tool aliases live on this router — leaving them here keeps T62's
# "one slice = one router file set" rule).
# ---------------------------------------------------------------------------


class AddTrackBody(BaseModel):
    model_config = ConfigDict(extra="ignore")

    name: str | None = None
    blend_mode: str | None = None
    base_opacity: float | None = None
    muted: bool | None = None


class UpdateTrackBody(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    id: str
    name: str | None = None
    blend_mode: str | None = Field(default=None, alias="blendMode")
    base_opacity: float | None = Field(default=None, alias="baseOpacity")
    muted: bool | None = None
    z_order: int | None = None
    chroma_key: Any | None = Field(default=None, alias="chromaKey")
    hidden: bool | None = None
    solo: bool | None = None


class DeleteTrackBody(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str


class ReorderTracksBody(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    trackIds: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Audio tracks
# ---------------------------------------------------------------------------


class AddAudioTrackBody(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    name: str | None = None
    hidden: bool | None = None
    muted: bool | None = None
    solo: bool | None = None
    volume_curve: Any | None = Field(default=None, alias="volumeCurve")


class UpdateAudioTrackBody(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    id: str
    name: str | None = None
    display_order: int | None = Field(default=None, alias="displayOrder")
    hidden: bool | None = None
    muted: bool | None = None
    solo: bool | None = None
    volume_curve: Any | None = Field(default=None, alias="volumeCurve")


class DeleteAudioTrackBody(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str


class ReorderAudioTracksBody(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    trackIds: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Audio clips
# ---------------------------------------------------------------------------


class AddAudioClipBody(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    trackId: str | None = Field(default=None, alias="track_id")
    sourcePath: str | None = Field(default=None, alias="source_path")
    startTime: float | None = Field(default=None, alias="start_time")
    endTime: float | None = Field(default=None, alias="end_time")
    sourceOffset: float | None = Field(default=None, alias="source_offset")
    volumeCurve: Any | None = Field(default=None, alias="volume_curve")
    muted: bool | None = None
    remap: dict[str, Any] | None = None
    label: str | None = None


class AddAudioClipFromPoolBody(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    trackId: str | None = Field(default=None, alias="track_id")
    startTime: float | None = Field(default=None, alias="start_time")
    poolSegmentId: str | None = Field(default=None, alias="pool_segment_id")
    poolPath: str | None = Field(default=None, alias="pool_path")


class UpdateAudioClipBody(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    id: str
    trackId: str | None = Field(default=None, alias="track_id")
    sourcePath: str | None = Field(default=None, alias="source_path")
    startTime: float | None = Field(default=None, alias="start_time")
    endTime: float | None = Field(default=None, alias="end_time")
    sourceOffset: float | None = Field(default=None, alias="source_offset")
    volumeCurve: Any | None = Field(default=None, alias="volume_curve")
    muted: bool | None = None
    remap: dict[str, Any] | None = None
    label: str | None = None


class DeleteAudioClipBody(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str


class AudioClipsBatchOpsBody(BaseModel):
    model_config = ConfigDict(extra="ignore")

    label: str | None = None
    ops: list[dict[str, Any]]


class AudioClipAlignDetectBody(BaseModel):
    model_config = ConfigDict(extra="ignore")

    anchorClipId: str
    clipIds: list[str]


# ---------------------------------------------------------------------------
# M13: effect chains + curves + send buses + track sends + frequency labels
# ---------------------------------------------------------------------------


class TrackEffectCreateBody(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    track_id: str | None = Field(default=None, alias="trackId")
    effect_type: str | None = Field(default=None, alias="effectType")
    static_params: dict[str, Any] | None = Field(default=None, alias="staticParams")
    order_index: int | None = Field(default=None, alias="orderIndex")


class TrackEffectUpdateBody(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    order_index: int | None = Field(default=None, alias="orderIndex")
    enabled: bool | None = None
    static_params: dict[str, Any] | None = Field(default=None, alias="staticParams")


class EffectCurveCreateBody(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    effect_id: str | None = Field(default=None, alias="effectId")
    param_name: str | None = Field(default=None, alias="paramName")
    points: list[Any] | None = None
    interpolation: str = "bezier"
    visible: bool = False


class EffectCurveUpdateBody(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    points: list[Any] | None = None
    interpolation: str | None = None
    visible: bool | None = None


class EffectCurveBatchUpdateItem(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    curve_id: str | None = Field(default=None, alias="curveId")
    points: list[Any] | None = None
    interpolation: str | None = None
    visible: bool | None = None


class EffectCurveBatchUpdateBody(BaseModel):
    model_config = ConfigDict(extra="ignore")

    description: str | None = None
    updates: list[EffectCurveBatchUpdateItem] = Field(default_factory=list)


class SendBusCreateBody(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    bus_type: str | None = Field(default=None, alias="busType")
    label: str | None = None
    static_params: dict[str, Any] | None = Field(default=None, alias="staticParams")
    order_index: int | None = Field(default=None, alias="orderIndex")


class SendBusUpdateBody(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    label: str | None = None
    order_index: int | None = Field(default=None, alias="orderIndex")
    static_params: dict[str, Any] | None = Field(default=None, alias="staticParams")


class TrackSendUpsertBody(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    track_id: str | None = Field(default=None, alias="trackId")
    bus_id: str | None = Field(default=None, alias="busId")
    level: float | None = None


class FrequencyLabelCreateBody(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    label: str | None = None
    freq_min_hz: float | None = Field(default=None, alias="freqMinHz")
    freq_max_hz: float | None = Field(default=None, alias="freqMaxHz")


# ---------------------------------------------------------------------------
# M15: master-bus effects (chat-tool aligned)
# ---------------------------------------------------------------------------


class AddMasterBusEffectBody(BaseModel):
    model_config = ConfigDict(extra="ignore")

    effect_type: str
    static_params: dict[str, Any] | None = None
    order_index: int | None = None
    enabled: bool = True


class RemoveMasterBusEffectBody(BaseModel):
    model_config = ConfigDict(extra="ignore")

    effect_id: str


# ---------------------------------------------------------------------------
# Volume curve (chat-tool aligned — update_volume_curve)
# ---------------------------------------------------------------------------


class UpdateVolumeCurveBody(BaseModel):
    """Body for the chat-tool-aligned volume-curve route.

    ``target_type`` defaults to ``"track"`` so callers hitting the track-
    scoped URL can omit it. ``target_id`` defaults to the path segment but
    is still accepted in the body for chat-tool parity.
    """

    model_config = ConfigDict(extra="ignore")

    target_type: str = "track"
    target_id: str | None = None
    interpolation: str = "bezier"
    points: Any = None


# ---------------------------------------------------------------------------
# Audio intelligence stubs
# ---------------------------------------------------------------------------


class UpdateRulesBody(BaseModel):
    model_config = ConfigDict(extra="ignore")


class ReapplyRulesBody(BaseModel):
    model_config = ConfigDict(extra="ignore")


# ---------------------------------------------------------------------------
# DSP / descriptions (chat-tool wrappers — generate_dsp / generate_descriptions)
# ---------------------------------------------------------------------------


class GenerateDspBody(BaseModel):
    model_config = ConfigDict(extra="ignore")

    source_segment_id: str
    analyses: list[str] | None = None
    force_rerun: bool = False


class GenerateDescriptionsBody(BaseModel):
    model_config = ConfigDict(extra="ignore")

    source_segment_id: str
    model: str | None = None
    chunk_size_s: float | None = None
    prompt_version: str | None = None
    force_rerun: bool = False


__all__ = [
    "AddTrackBody",
    "UpdateTrackBody",
    "DeleteTrackBody",
    "ReorderTracksBody",
    "AddAudioTrackBody",
    "UpdateAudioTrackBody",
    "DeleteAudioTrackBody",
    "ReorderAudioTracksBody",
    "AddAudioClipBody",
    "AddAudioClipFromPoolBody",
    "UpdateAudioClipBody",
    "DeleteAudioClipBody",
    "AudioClipsBatchOpsBody",
    "AudioClipAlignDetectBody",
    "TrackEffectCreateBody",
    "TrackEffectUpdateBody",
    "EffectCurveCreateBody",
    "EffectCurveUpdateBody",
    "EffectCurveBatchUpdateBody",
    "EffectCurveBatchUpdateItem",
    "SendBusCreateBody",
    "SendBusUpdateBody",
    "TrackSendUpsertBody",
    "FrequencyLabelCreateBody",
    "AddMasterBusEffectBody",
    "RemoveMasterBusEffectBody",
    "UpdateVolumeCurveBody",
    "UpdateRulesBody",
    "ReapplyRulesBody",
    "GenerateDspBody",
    "GenerateDescriptionsBody",
]
