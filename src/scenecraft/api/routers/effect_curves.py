"""M13 effect-chain + curve routes (M16 T62).

Covers: ``/track-effects`` (CRUD), ``/effect-curves`` (CRUD + batch),
``/send-buses`` (CRUD), ``/track-sends`` (upsert), ``/frequency-labels``
(CRUD). All operation_ids that are ``🔧`` are load-bearing for T67.

Idempotent DELETE: ``DELETE /track-effects/{id}`` (and siblings) on a
non-existent id returns 200 empty body, NOT 404 (spec R6 / M13 behavior
table row 4). The tests assert this explicitly.

No ``project_lock`` on these routes — none of the tails are in
``STRUCTURAL_ROUTES``. The M13 handlers perform their own cache-
invalidation + WS broadcasts (``mixer.chain-rebuilt``,
``master_bus_effects_changed``) — those are internal implementation
details, not lock-protected write barriers.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from fastapi import APIRouter, Depends, Query

from scenecraft.api.deps import current_user, project_dir
from scenecraft.api.errors import ApiError
from scenecraft.api.models.audio import (
    AddMasterBusEffectBody,
    EffectCurveBatchUpdateBody,
    EffectCurveCreateBody,
    EffectCurveUpdateBody,
    FrequencyLabelCreateBody,
    RemoveMasterBusEffectBody,
    SendBusCreateBody,
    SendBusUpdateBody,
    TrackEffectCreateBody,
    TrackEffectUpdateBody,
    TrackSendUpsertBody,
)


router = APIRouter(prefix="/api/projects", tags=["audio"], dependencies=[Depends(current_user)])


# ---------------------------------------------------------------------------
# Row → JSON helpers (mirrors _m13_*_as_json from api_server.py)
# ---------------------------------------------------------------------------


def _effect_as_json(eff) -> dict:
    return {
        "id": eff.id,
        "track_id": eff.track_id,
        "effect_type": eff.effect_type,
        "order_index": eff.order_index,
        "enabled": bool(eff.enabled),
        "static_params": eff.static_params,
        "created_at": eff.created_at,
    }


def _curve_as_json(curve) -> dict:
    return {
        "id": curve.id,
        "effect_id": curve.effect_id,
        "param_name": curve.param_name,
        "points": curve.points,
        "interpolation": curve.interpolation,
        "visible": bool(curve.visible),
    }


def _bus_as_json(bus) -> dict:
    return {
        "id": bus.id,
        "bus_type": bus.bus_type,
        "label": bus.label,
        "order_index": bus.order_index,
        "static_params": bus.static_params,
    }


def _send_as_json(send) -> dict:
    return {
        "track_id": send.track_id,
        "bus_id": send.bus_id,
        "level": send.level,
    }


def _label_as_json(lbl) -> dict:
    return {
        "id": lbl.id,
        "label": lbl.label,
        "freq_min_hz": lbl.freq_min_hz,
        "freq_max_hz": lbl.freq_max_hz,
    }


def _clamp_points(points, effect_id: str, param_name: str) -> list:
    if not isinstance(points, list):
        return []
    out = []
    for p in points:
        if not isinstance(p, (list, tuple)) or len(p) < 2:
            continue
        try:
            t = float(p[0])
            v = float(p[1])
        except (TypeError, ValueError):
            continue
        if v < 0.0:
            v = 0.0
        elif v > 1.0:
            v = 1.0
        out.append([t, v])
    return out


# ---------------------------------------------------------------------------
# Track effects
# ---------------------------------------------------------------------------


@router.get("/{name}/track-effects", operation_id="list_track_effects")
async def list_track_effects(
    name: str,
    track_id: str | None = Query(default=None),
    trackId: str | None = Query(default=None),
    pd: Path = Depends(project_dir),
) -> dict:
    from scenecraft.db import list_curves_for_effect, list_track_effects as db_list

    tid = track_id or trackId
    if not tid:
        raise ApiError("BAD_REQUEST", "Missing 'track_id' query param", status_code=400)
    effects = db_list(pd, tid)
    payload: list[dict] = []
    for eff in effects:
        d = _effect_as_json(eff)
        d["curves"] = [_curve_as_json(c) for c in list_curves_for_effect(pd, eff.id)]
        payload.append(d)
    return {"effects": payload}


@router.post(
    "/{name}/track-effects",
    operation_id="add_audio_effect",  # 🔧 chat-tool
)
async def create_track_effect(
    name: str,
    body: TrackEffectCreateBody,
    pd: Path = Depends(project_dir),
) -> dict:
    from scenecraft.audio_effect_registry import (
        SEND_SYNTHETIC_EFFECT_TYPE,
        is_valid_effect_type,
    )
    from scenecraft.db import add_track_effect, get_audio_tracks

    if not body.track_id:
        raise ApiError("BAD_REQUEST", "Missing 'track_id'", status_code=400)
    if not body.effect_type:
        raise ApiError("BAD_REQUEST", "Missing 'effect_type'", status_code=400)
    if body.effect_type == SEND_SYNTHETIC_EFFECT_TYPE:
        raise ApiError(
            "BAD_REQUEST",
            f"'{SEND_SYNTHETIC_EFFECT_TYPE}' is reserved for effect_curves "
            f"(per-bus send animation) — not instantiable as a track effect",
            status_code=400,
        )
    if not is_valid_effect_type(body.effect_type):
        raise ApiError("BAD_REQUEST", f"Unknown effect_type: '{body.effect_type}'", status_code=400)

    tracks = {t["id"] for t in get_audio_tracks(pd)}
    if body.track_id not in tracks:
        raise ApiError("NOT_FOUND", f"Audio track not found: {body.track_id}", status_code=404)

    try:
        eff = add_track_effect(
            pd,
            track_id=body.track_id,
            effect_type=body.effect_type,
            static_params=body.static_params or {},
            order_index=body.order_index,
        )
    except Exception as e:
        raise ApiError("INTERNAL_ERROR", str(e), status_code=500)
    return _effect_as_json(eff)


@router.post(
    "/{name}/track-effects/{effect_id}",
    operation_id="update_track_effect",
)
async def update_track_effect(
    name: str,
    effect_id: str,
    body: TrackEffectUpdateBody,
    pd: Path = Depends(project_dir),
) -> dict:
    from scenecraft.db import (
        get_db,
        get_track_effect,
        update_track_effect as db_update,
    )

    existing = get_track_effect(pd, effect_id)
    if existing is None:
        raise ApiError("NOT_FOUND", f"Effect not found: {effect_id}", status_code=404)

    fields: dict = {}
    if body.order_index is not None:
        fields["order_index"] = int(body.order_index)
    if body.enabled is not None:
        fields["enabled"] = bool(body.enabled)
    if body.static_params is not None:
        fields["static_params"] = body.static_params

    if "order_index" in fields:
        new_idx = fields["order_index"]
        conn = get_db(pd)
        try:
            conn.execute("BEGIN IMMEDIATE")
            siblings = conn.execute(
                "SELECT id, order_index FROM track_effects "
                "WHERE track_id = ? AND id != ? ORDER BY order_index",
                (existing.track_id, effect_id),
            ).fetchall()
            ordered = [dict(r) for r in siblings]
            final_sequence: list[str] = []
            insert_pos = max(0, min(new_idx, len(ordered)))
            for i, s in enumerate(ordered):
                if i == insert_pos:
                    final_sequence.append(effect_id)
                final_sequence.append(s["id"])
            if insert_pos >= len(ordered):
                final_sequence.append(effect_id)
            for i, eid in enumerate(final_sequence):
                conn.execute(
                    "UPDATE track_effects SET order_index = ? WHERE id = ?",
                    (-(i + 1), eid),
                )
            for i, eid in enumerate(final_sequence):
                conn.execute(
                    "UPDATE track_effects SET order_index = ? WHERE id = ?",
                    (i, eid),
                )
            remaining = {k: v for k, v in fields.items() if k != "order_index"}
            if remaining:
                sets: list[str] = []
                values: list = []
                for key, val in remaining.items():
                    if key == "enabled":
                        val = 1 if val else 0
                    elif key == "static_params" and not isinstance(val, str):
                        val = json.dumps(val)
                    sets.append(f"{key} = ?")
                    values.append(val)
                values.append(effect_id)
                conn.execute(
                    f"UPDATE track_effects SET {', '.join(sets)} WHERE id = ?",
                    values,
                )
            conn.commit()
        except Exception as e:
            try:
                conn.rollback()
            except Exception:
                pass
            raise ApiError("INTERNAL_ERROR", f"order-index swap failed: {e}", status_code=500)
    elif fields:
        try:
            db_update(pd, effect_id, **fields)
        except Exception as e:
            raise ApiError("INTERNAL_ERROR", str(e), status_code=500)

    updated = get_track_effect(pd, effect_id)
    assert updated is not None
    return _effect_as_json(updated)


@router.delete(
    "/{name}/track-effects/{effect_id}",
    operation_id="delete_track_effect",
)
async def delete_track_effect(
    name: str,
    effect_id: str,
    pd: Path = Depends(project_dir),
) -> dict:
    from scenecraft.db import delete_track_effect as db_delete, get_track_effect

    existing = get_track_effect(pd, effect_id)
    if existing is None:
        return {}  # idempotent — R6
    try:
        db_delete(pd, effect_id)
    except Exception as e:
        raise ApiError("INTERNAL_ERROR", str(e), status_code=500)
    return {}


# ---------------------------------------------------------------------------
# Effect curves
# ---------------------------------------------------------------------------


@router.post("/{name}/effect-curves", operation_id="create_effect_curve")
async def create_effect_curve(
    name: str,
    body: EffectCurveCreateBody,
    pd: Path = Depends(project_dir),
) -> dict:
    from scenecraft.audio_effect_registry import is_param_animatable
    from scenecraft.db import get_track_effect, upsert_effect_curve

    if not body.effect_id:
        raise ApiError("BAD_REQUEST", "Missing 'effect_id'", status_code=400)
    if not body.param_name:
        raise ApiError("BAD_REQUEST", "Missing 'param_name'", status_code=400)
    if body.interpolation not in ("bezier", "linear", "step"):
        raise ApiError(
            "BAD_REQUEST",
            f"Invalid interpolation '{body.interpolation}' (expected bezier|linear|step)",
            status_code=400,
        )

    eff = get_track_effect(pd, body.effect_id)
    if eff is None:
        raise ApiError("NOT_FOUND", f"Effect not found: {body.effect_id}", status_code=404)

    animatable = is_param_animatable(eff.effect_type, body.param_name)
    if animatable is None:
        raise ApiError(
            "BAD_REQUEST",
            f"Unknown param '{body.param_name}' for effect_type '{eff.effect_type}'",
            status_code=400,
        )
    if not animatable:
        raise ApiError(
            "BAD_REQUEST",
            f"Param '{body.param_name}' on '{eff.effect_type}' is static — not animatable",
            status_code=400,
        )

    clamped = _clamp_points(body.points or [], body.effect_id, body.param_name)
    try:
        curve = upsert_effect_curve(
            pd,
            effect_id=body.effect_id,
            param_name=body.param_name,
            points=clamped,
            interpolation=body.interpolation,
            visible=body.visible,
        )
    except Exception as e:
        raise ApiError("INTERNAL_ERROR", str(e), status_code=500)
    return _curve_as_json(curve)


@router.post(
    "/{name}/effect-curves/batch",
    operation_id="update_effect_param_curve",  # 🔧 chat-tool alignment
)
async def effect_curves_batch_update(
    name: str,
    body: EffectCurveBatchUpdateBody,
    pd: Path = Depends(project_dir),
) -> dict:
    from scenecraft.db import (
        get_effect_curve,
        undo_begin,
        update_effect_curve,
    )

    updates = body.updates or []
    if not isinstance(updates, list):
        raise ApiError("BAD_REQUEST", "'updates' must be a list", status_code=400)
    description = body.description or f"Batch update {len(updates)} curves"

    updated: list[str] = []
    missing: list[str] = []

    undo_begin(pd, description)

    for u in updates:
        curve_id = u.curve_id
        if not curve_id:
            continue
        existing = get_effect_curve(pd, curve_id)
        if existing is None:
            missing.append(curve_id)
            continue

        fields: dict = {}
        if u.points is not None:
            fields["points"] = _clamp_points(
                u.points, existing.effect_id, existing.param_name
            )
        if u.interpolation is not None:
            if u.interpolation not in ("bezier", "linear", "step"):
                raise ApiError(
                    "BAD_REQUEST",
                    f"Invalid interpolation '{u.interpolation}' (expected bezier|linear|step)",
                    status_code=400,
                )
            fields["interpolation"] = u.interpolation
        if u.visible is not None:
            fields["visible"] = bool(u.visible)

        if fields:
            try:
                update_effect_curve(pd, curve_id, **fields)
            except Exception as e:
                raise ApiError("INTERNAL_ERROR", str(e), status_code=500)
            updated.append(curve_id)

    return {"success": True, "updated": updated, "missing": missing}


@router.post(
    "/{name}/effect-curves/{curve_id}",
    operation_id="update_effect_curve",
)
async def update_effect_curve(
    name: str,
    curve_id: str,
    body: EffectCurveUpdateBody,
    pd: Path = Depends(project_dir),
) -> dict:
    from scenecraft.db import (
        get_effect_curve,
        update_effect_curve as db_update,
    )

    existing = get_effect_curve(pd, curve_id)
    if existing is None:
        raise ApiError("NOT_FOUND", f"Curve not found: {curve_id}", status_code=404)

    fields: dict = {}
    if body.points is not None:
        fields["points"] = _clamp_points(
            body.points, existing.effect_id, existing.param_name
        )
    if body.interpolation is not None:
        if body.interpolation not in ("bezier", "linear", "step"):
            raise ApiError(
                "BAD_REQUEST",
                f"Invalid interpolation '{body.interpolation}' (expected bezier|linear|step)",
                status_code=400,
            )
        fields["interpolation"] = body.interpolation
    if body.visible is not None:
        fields["visible"] = bool(body.visible)

    if fields:
        try:
            db_update(pd, curve_id, **fields)
        except Exception as e:
            raise ApiError("INTERNAL_ERROR", str(e), status_code=500)

    updated = get_effect_curve(pd, curve_id)
    assert updated is not None
    return _curve_as_json(updated)


@router.delete(
    "/{name}/effect-curves/{curve_id}",
    operation_id="delete_effect_curve",
)
async def delete_effect_curve(
    name: str,
    curve_id: str,
    pd: Path = Depends(project_dir),
) -> dict:
    from scenecraft.db import delete_effect_curve as db_delete, get_effect_curve

    existing = get_effect_curve(pd, curve_id)
    if existing is None:
        return {}  # idempotent
    try:
        db_delete(pd, curve_id)
    except Exception as e:
        raise ApiError("INTERNAL_ERROR", str(e), status_code=500)
    return {}


# ---------------------------------------------------------------------------
# Send buses
# ---------------------------------------------------------------------------


@router.get("/{name}/send-buses", operation_id="list_send_buses")
async def list_send_buses(name: str, pd: Path = Depends(project_dir)) -> dict:
    from scenecraft.db import list_send_buses as db_list

    return {"buses": [_bus_as_json(b) for b in db_list(pd)]}


@router.post("/{name}/send-buses", operation_id="create_send_bus")
async def create_send_bus(
    name: str,
    body: SendBusCreateBody,
    pd: Path = Depends(project_dir),
) -> dict:
    from scenecraft.db import add_send_bus

    if body.bus_type not in ("reverb", "delay", "echo"):
        raise ApiError(
            "BAD_REQUEST",
            f"Invalid bus_type '{body.bus_type}' (expected reverb|delay|echo)",
            status_code=400,
        )
    if not body.label:
        raise ApiError("BAD_REQUEST", "Missing 'label'", status_code=400)

    try:
        bus = add_send_bus(
            pd,
            bus_type=body.bus_type,
            label=body.label,
            static_params=body.static_params or {},
            order_index=body.order_index,
        )
    except Exception as e:
        raise ApiError("INTERNAL_ERROR", str(e), status_code=500)
    return _bus_as_json(bus)


@router.post(
    "/{name}/send-buses/{bus_id}",
    operation_id="update_send_bus",
)
async def update_send_bus(
    name: str,
    bus_id: str,
    body: SendBusUpdateBody,
    pd: Path = Depends(project_dir),
) -> dict:
    from scenecraft.db import get_send_bus, update_send_bus as db_update

    existing = get_send_bus(pd, bus_id)
    if existing is None:
        raise ApiError("NOT_FOUND", f"Bus not found: {bus_id}", status_code=404)

    fields: dict = {}
    if body.label is not None:
        fields["label"] = str(body.label)
    if body.order_index is not None:
        fields["order_index"] = int(body.order_index)
    if body.static_params is not None:
        fields["static_params"] = body.static_params

    if fields:
        try:
            db_update(pd, bus_id, **fields)
        except Exception as e:
            raise ApiError("INTERNAL_ERROR", str(e), status_code=500)
    updated = get_send_bus(pd, bus_id)
    assert updated is not None
    return _bus_as_json(updated)


@router.delete(
    "/{name}/send-buses/{bus_id}",
    operation_id="delete_send_bus",
)
async def delete_send_bus(
    name: str,
    bus_id: str,
    pd: Path = Depends(project_dir),
) -> dict:
    from scenecraft.db import delete_send_bus as db_delete, get_send_bus

    existing = get_send_bus(pd, bus_id)
    if existing is None:
        return {}
    try:
        db_delete(pd, bus_id)
    except Exception as e:
        raise ApiError("INTERNAL_ERROR", str(e), status_code=500)
    return {}


# ---------------------------------------------------------------------------
# Track sends
# ---------------------------------------------------------------------------


@router.post("/{name}/track-sends", operation_id="upsert_track_send")
async def upsert_track_send(
    name: str,
    body: TrackSendUpsertBody,
    pd: Path = Depends(project_dir),
) -> dict:
    from scenecraft.db import (
        get_audio_tracks,
        get_send_bus,
        upsert_track_send as db_upsert,
    )

    if not body.track_id:
        raise ApiError("BAD_REQUEST", "Missing 'track_id'", status_code=400)
    if not body.bus_id:
        raise ApiError("BAD_REQUEST", "Missing 'bus_id'", status_code=400)
    if body.level is None:
        raise ApiError("BAD_REQUEST", "Missing 'level'", status_code=400)
    try:
        level = float(body.level)
    except (TypeError, ValueError):
        raise ApiError("BAD_REQUEST", "Invalid 'level' — must be a number", status_code=400)

    tracks = {t["id"] for t in get_audio_tracks(pd)}
    if body.track_id not in tracks:
        raise ApiError("NOT_FOUND", f"Audio track not found: {body.track_id}", status_code=404)
    if get_send_bus(pd, body.bus_id) is None:
        raise ApiError("NOT_FOUND", f"Send bus not found: {body.bus_id}", status_code=404)

    try:
        send = db_upsert(pd, track_id=body.track_id, bus_id=body.bus_id, level=level)
    except Exception as e:
        raise ApiError("INTERNAL_ERROR", str(e), status_code=500)
    return _send_as_json(send)


# ---------------------------------------------------------------------------
# Frequency labels
# ---------------------------------------------------------------------------


@router.post(
    "/{name}/frequency-labels",
    operation_id="create_frequency_label",
)
async def create_frequency_label(
    name: str,
    body: FrequencyLabelCreateBody,
    pd: Path = Depends(project_dir),
) -> dict:
    from scenecraft.db import add_frequency_label

    if not body.label:
        raise ApiError("BAD_REQUEST", "Missing 'label'", status_code=400)
    if body.freq_min_hz is None or body.freq_max_hz is None:
        raise ApiError(
            "BAD_REQUEST", "Missing 'freq_min_hz' / 'freq_max_hz'", status_code=400
        )
    try:
        freq_min = float(body.freq_min_hz)
        freq_max = float(body.freq_max_hz)
    except (TypeError, ValueError):
        raise ApiError("BAD_REQUEST", "freq_* must be numbers", status_code=400)
    if freq_min < 0 or freq_max < 0 or freq_max < freq_min:
        raise ApiError(
            "BAD_REQUEST", "freq_max_hz must be >= freq_min_hz >= 0", status_code=400
        )

    try:
        lbl = add_frequency_label(
            pd, label=body.label, freq_min_hz=freq_min, freq_max_hz=freq_max
        )
    except Exception as e:
        raise ApiError("INTERNAL_ERROR", str(e), status_code=500)
    return _label_as_json(lbl)


@router.delete(
    "/{name}/frequency-labels/{label_id}",
    operation_id="delete_frequency_label",
)
async def delete_frequency_label(
    name: str,
    label_id: str,
    pd: Path = Depends(project_dir),
) -> dict:
    from scenecraft.db import (
        delete_frequency_label as db_delete,
        get_frequency_label,
    )

    existing = get_frequency_label(pd, label_id)
    if existing is None:
        return {}
    try:
        db_delete(pd, label_id)
    except Exception as e:
        raise ApiError("INTERNAL_ERROR", str(e), status_code=500)
    return {}


# ---------------------------------------------------------------------------
# Master bus effects
# ---------------------------------------------------------------------------


@router.get(
    "/{name}/master-bus-effects",
    operation_id="list_master_bus_effects",
)
async def list_master_bus_effects(name: str, pd: Path = Depends(project_dir)) -> dict:
    from scenecraft.db import list_master_bus_effects as db_list

    return {"effects": [_effect_as_json(e) for e in db_list(pd)]}


@router.post(
    "/{name}/master-bus-effects/add",
    operation_id="add_master_bus_effect",  # 🔧 chat-tool
)
async def add_master_bus_effect(
    name: str,
    body: AddMasterBusEffectBody,
    pd: Path = Depends(project_dir),
) -> dict:
    from scenecraft.chat import _exec_add_master_bus_effect

    input_data = {
        "effect_type": body.effect_type,
        "static_params": body.static_params,
        "order_index": body.order_index,
        "enabled": body.enabled,
    }
    result = await _exec_add_master_bus_effect(pd, input_data)
    if isinstance(result, dict) and "error" in result:
        raise ApiError("BAD_REQUEST", str(result["error"]), status_code=400)
    return result


@router.post(
    "/{name}/master-bus-effects/remove",
    operation_id="remove_master_bus_effect",  # 🔧 chat-tool
)
async def remove_master_bus_effect(
    name: str,
    body: RemoveMasterBusEffectBody,
    pd: Path = Depends(project_dir),
) -> dict:
    from scenecraft.chat import _exec_remove_master_bus_effect

    result = await _exec_remove_master_bus_effect(pd, {"effect_id": body.effect_id})
    if isinstance(result, dict) and "error" in result:
        raise ApiError("BAD_REQUEST", str(result["error"]), status_code=400)
    return result
