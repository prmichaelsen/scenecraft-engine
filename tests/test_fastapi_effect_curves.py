"""M16 T62 effect-curves + master-bus + track-sends + frequency-labels tests."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from scenecraft.api.app import create_app
from scenecraft.db import (
    add_audio_track as db_add_audio_track,
    close_db,
    get_db,
)


@pytest.fixture
def project(tmp_path: Path):
    work_dir = tmp_path / "work"
    name = "P"
    pd = work_dir / name
    pd.mkdir(parents=True)
    get_db(pd)
    db_add_audio_track(pd, {"id": "t1", "name": "T1", "display_order": 0})
    yield (work_dir, name)
    close_db(pd)


@pytest.fixture
def client(project):
    work_dir, _ = project
    return TestClient(create_app(work_dir=work_dir))


# ---------------------------------------------------------------------------
# Track effects
# ---------------------------------------------------------------------------


def test_create_track_effect(client, project):
    _, name = project
    r = client.post(
        f"/api/projects/{name}/track-effects",
        json={"track_id": "t1", "effect_type": "lowpass"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["track_id"] == "t1"
    assert body["effect_type"] == "lowpass"
    assert body["id"]


def test_list_track_effects(client, project):
    _, name = project
    client.post(
        f"/api/projects/{name}/track-effects",
        json={"track_id": "t1", "effect_type": "lowpass"},
    )
    r = client.get(f"/api/projects/{name}/track-effects?track_id=t1")
    assert r.status_code == 200
    body = r.json()
    assert "effects" in body
    assert len(body["effects"]) == 1


def test_track_effects_missing_track_id_query_400(client, project):
    _, name = project
    r = client.get(f"/api/projects/{name}/track-effects")
    assert r.status_code == 400


def test_delete_track_effect_idempotent(client, project):
    _, name = project
    # DELETE non-existent id must return 200 empty (M13 spec R6).
    r = client.delete(f"/api/projects/{name}/track-effects/does_not_exist")
    assert r.status_code == 200
    assert r.json() == {}


def test_create_track_effect_unknown_type_400(client, project):
    _, name = project
    r = client.post(
        f"/api/projects/{name}/track-effects",
        json={"track_id": "t1", "effect_type": "not_a_real_effect"},
    )
    assert r.status_code == 400


# ---------------------------------------------------------------------------
# Effect curves
# ---------------------------------------------------------------------------


def test_effect_curve_create_and_batch(client, project):
    _, name = project
    # Need a real effect first.
    eff = client.post(
        f"/api/projects/{name}/track-effects",
        json={"track_id": "t1", "effect_type": "lowpass"},
    ).json()
    eff_id = eff["id"]

    r = client.post(
        f"/api/projects/{name}/effect-curves",
        json={
            "effect_id": eff_id,
            "param_name": "cutoff",
            "points": [[0.0, 0.5], [1.0, 0.8]],
            "interpolation": "linear",
        },
    )
    assert r.status_code == 200, r.text
    curve = r.json()
    cid = curve["id"]

    # Batch update
    r = client.post(
        f"/api/projects/{name}/effect-curves/batch",
        json={
            "description": "paste",
            "updates": [{"curve_id": cid, "points": [[0.0, 0.1], [1.0, 0.2]]}],
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["success"] is True
    assert cid in body["updated"]


def test_effect_curve_delete_idempotent(client, project):
    _, name = project
    r = client.delete(f"/api/projects/{name}/effect-curves/nope")
    assert r.status_code == 200
    assert r.json() == {}


# ---------------------------------------------------------------------------
# Send buses
# ---------------------------------------------------------------------------


def test_send_bus_create_list_update_delete(client, project):
    _, name = project
    r = client.post(
        f"/api/projects/{name}/send-buses",
        json={"bus_type": "reverb", "label": "R1"},
    )
    assert r.status_code == 200, r.text
    bus = r.json()
    bid = bus["id"]

    r = client.get(f"/api/projects/{name}/send-buses")
    assert r.status_code == 200
    body = r.json()
    assert any(b["id"] == bid for b in body["buses"])

    r = client.post(
        f"/api/projects/{name}/send-buses/{bid}", json={"label": "Renamed"}
    )
    assert r.status_code == 200
    assert r.json()["label"] == "Renamed"

    r = client.delete(f"/api/projects/{name}/send-buses/{bid}")
    assert r.status_code == 200


def test_send_bus_invalid_type_400(client, project):
    _, name = project
    r = client.post(
        f"/api/projects/{name}/send-buses",
        json={"bus_type": "phaser", "label": "bad"},
    )
    assert r.status_code == 400


# ---------------------------------------------------------------------------
# Track sends
# ---------------------------------------------------------------------------


def test_track_send_upsert(client, project):
    _, name = project
    bus = client.post(
        f"/api/projects/{name}/send-buses",
        json={"bus_type": "delay", "label": "D1"},
    ).json()
    r = client.post(
        f"/api/projects/{name}/track-sends",
        json={"track_id": "t1", "bus_id": bus["id"], "level": 0.4},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["track_id"] == "t1"
    assert body["bus_id"] == bus["id"]
    assert body["level"] == 0.4


# ---------------------------------------------------------------------------
# Frequency labels
# ---------------------------------------------------------------------------


def test_frequency_label_create_and_delete(client, project):
    _, name = project
    r = client.post(
        f"/api/projects/{name}/frequency-labels",
        json={"label": "low", "freq_min_hz": 20.0, "freq_max_hz": 200.0},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    lid = body["id"]

    r = client.delete(f"/api/projects/{name}/frequency-labels/{lid}")
    assert r.status_code == 200
    assert r.json() == {}


# ---------------------------------------------------------------------------
# Master bus
# ---------------------------------------------------------------------------


def test_list_master_bus_effects_empty(client, project):
    _, name = project
    r = client.get(f"/api/projects/{name}/master-bus-effects")
    assert r.status_code == 200
    body = r.json()
    assert body == {"effects": []}


def test_add_master_bus_effect(client, project):
    _, name = project
    r = client.post(
        f"/api/projects/{name}/master-bus-effects/add",
        json={"effect_type": "compressor"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["effect_type"] == "compressor"
    assert "effect_id" in body


def test_add_master_bus_effect_unknown_type_400(client, project):
    _, name = project
    r = client.post(
        f"/api/projects/{name}/master-bus-effects/add",
        json={"effect_type": "noop"},
    )
    assert r.status_code == 400


def test_remove_master_bus_effect(client, project):
    _, name = project
    add = client.post(
        f"/api/projects/{name}/master-bus-effects/add",
        json={"effect_type": "compressor"},
    ).json()
    eid = add["effect_id"]

    r = client.post(
        f"/api/projects/{name}/master-bus-effects/remove",
        json={"effect_id": eid},
    )
    assert r.status_code == 200
    assert r.json() == {"ok": True}


def test_remove_master_bus_effect_not_found_400(client, project):
    _, name = project
    r = client.post(
        f"/api/projects/{name}/master-bus-effects/remove",
        json={"effect_id": "no_such"},
    )
    # _exec returns {"error": ...} → we surface as 400 BAD_REQUEST envelope.
    assert r.status_code == 400


# ---------------------------------------------------------------------------
# Operation IDs
# ---------------------------------------------------------------------------


def test_operation_ids(client):
    spec = client.get("/openapi.json").json()
    ops = {
        op["operationId"]
        for path in spec["paths"].values()
        for op in path.values()
        if isinstance(op, dict) and "operationId" in op
    }
    expected = {
        "add_audio_effect",
        "list_track_effects",
        "update_track_effect",
        "delete_track_effect",
        "create_effect_curve",
        "update_effect_param_curve",  # chat-tool alignment (batch)
        "update_effect_curve",
        "delete_effect_curve",
        "create_send_bus",
        "list_send_buses",
        "update_send_bus",
        "delete_send_bus",
        "upsert_track_send",
        "create_frequency_label",
        "list_master_bus_effects",
        "add_master_bus_effect",
        "remove_master_bus_effect",
    }
    missing = expected - ops
    assert not missing, f"missing operation_ids: {missing}"
