"""Integration tests for the master-bus effects GET endpoint + WS invalidation.

Covers:
  - ``GET /api/projects/:name/master-bus-effects`` on an empty project
    returns ``{"effects": []}``.
  - After ``add_master_bus_effect`` (direct DB call), the GET endpoint
    returns the new effect with the correct M13 effect shape.
  - Multiple master-bus effects are returned in ``order_index`` order.
  - ``delete_track_effect`` on a master-bus effect removes it from the
    GET response.
  - ``_exec_add_master_bus_effect`` with a FakeWs emits a
    ``master_bus_effects_changed`` message after the DB write.
  - ``_exec_remove_master_bus_effect`` with a FakeWs emits a
    ``master_bus_effects_changed`` message after the DB delete.
  - Missing project returns 404 on the GET endpoint.

The HTTP round-trip pattern mirrors ``tests/test_mix_render_upload.py``:
spin up an HTTPServer on a random port and issue real HTTP requests.
The chat-tool WS pattern mirrors ``tests/test_analyze_master_bus_roundtrip.py``:
FakeWs captures every ``send()`` payload so tests can assert message shape.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from fastapi.testclient import TestClient
from scenecraft.api.app import create_app


# ── Fixtures ────────────────────────────────────────────────────────


@pytest.fixture
def server(tmp_path):
    work_dir = tmp_path / "work"
    work_dir.mkdir()

    app = create_app(work_dir=work_dir)
    client = TestClient(app, raise_server_exceptions=False)

    yield {"work_dir": work_dir, "client": client}


def _make_project(work_dir: Path, name: str) -> Path:
    """Create a fresh project with a bare DB + one audio track so
    track-effect code paths have a valid target when we need them."""
    from scenecraft.db import add_audio_track, close_db, get_db

    p = work_dir / name
    p.mkdir()
    get_db(p)
    add_audio_track(p, {"id": "at1", "name": "Track 1", "display_order": 0})
    close_db(p)
    return p


# ── HTTP helpers ────────────────────────────────────────────────────


def _get_json(srv, path: str) -> tuple[int, dict]:
    resp = srv["client"].get(path)
    try:
        return resp.status_code, resp.json()
    except Exception:
        return resp.status_code, {"raw": resp.text}


# ── FakeWs (mirrors test_analyze_master_bus_roundtrip.py) ───────────


class FakeWs:
    """Minimal async-ws stand-in. Captures every send() payload so tests can
    assert message shape."""

    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send(self, data: str) -> None:
        self.sent.append(json.loads(data))


# ── GET endpoint: empty project ─────────────────────────────────────


def test_get_master_bus_effects_empty(server):
    """GET on a project with no master-bus effects returns an empty list,
    not an error. The endpoint must work before the first effect is added."""
    _make_project(server["work_dir"], "empty")

    status, resp = _get_json(
        server, "/api/projects/empty/master-bus-effects",
    )
    assert status == 200, resp
    assert resp == {"effects": []}


# ── GET endpoint: returns added effect with correct shape ───────────


def test_get_master_bus_effects_returns_added_effect(server):
    """After a direct DB add, the endpoint returns the effect with the
    M13 effect shape: id, effect_type, order_index, enabled, static_params,
    created_at."""
    from scenecraft.db import add_master_bus_effect, close_db

    project = _make_project(server["work_dir"], "p_added")
    eff = add_master_bus_effect(
        project,
        effect_type="limiter",
        static_params={"threshold": -1.0},
    )
    close_db(project)

    status, resp = _get_json(
        server, "/api/projects/p_added/master-bus-effects",
    )
    assert status == 200, resp
    assert len(resp["effects"]) == 1
    row = resp["effects"][0]
    assert row["id"] == eff.id
    assert row["effect_type"] == "limiter"
    assert row["order_index"] == 0
    assert row["enabled"] is True
    assert row["static_params"] == {"threshold": -1.0}
    assert isinstance(row["created_at"], str) and row["created_at"]
    # track_id is surfaced but must be None for master-bus rows — useful
    # sanity check that we didn't accidentally return a track-scoped row.
    assert row.get("track_id") is None


# ── GET endpoint: multiple effects ordered by order_index ───────────


def test_get_master_bus_effects_ordered(server):
    """Multiple master-bus effects come back sorted by order_index
    ascending — same contract as list_master_bus_effects."""
    from scenecraft.db import add_master_bus_effect, close_db

    project = _make_project(server["work_dir"], "p_ordered")
    a = add_master_bus_effect(project, effect_type="compressor")
    b = add_master_bus_effect(project, effect_type="limiter")
    c = add_master_bus_effect(project, effect_type="eq_band")
    close_db(project)

    status, resp = _get_json(
        server, "/api/projects/p_ordered/master-bus-effects",
    )
    assert status == 200, resp
    ids = [e["id"] for e in resp["effects"]]
    orders = [e["order_index"] for e in resp["effects"]]
    assert ids == [a.id, b.id, c.id]
    assert orders == [0, 1, 2]


# ── GET endpoint: delete removes from response ──────────────────────


def test_get_master_bus_effects_reflects_deletes(server):
    """delete_track_effect on a master-bus effect must remove it from the
    GET response — there's no separate delete_master_bus_effect fn."""
    from scenecraft.db import add_master_bus_effect, close_db, delete_track_effect

    project = _make_project(server["work_dir"], "p_del")
    a = add_master_bus_effect(project, effect_type="compressor")
    b = add_master_bus_effect(project, effect_type="limiter")
    delete_track_effect(project, a.id)
    close_db(project)

    status, resp = _get_json(
        server, "/api/projects/p_del/master-bus-effects",
    )
    assert status == 200, resp
    ids = [e["id"] for e in resp["effects"]]
    assert ids == [b.id]


# ── Chat tool: WS emit on add ───────────────────────────────────────


@pytest.mark.asyncio
async def test_exec_add_emits_ws_message(tmp_path):
    """``_exec_add_master_bus_effect`` must emit a
    ``master_bus_effects_changed`` WS message after the successful DB
    write so the frontend mixer can refetch."""
    from scenecraft.chat import _exec_add_master_bus_effect
    from scenecraft.db import add_audio_track, get_db, list_master_bus_effects

    project = tmp_path / "proj"
    project.mkdir()
    get_db(project)
    add_audio_track(project, {"id": "at1", "name": "T", "display_order": 0})

    ws = FakeWs()
    result = await _exec_add_master_bus_effect(
        project,
        {"effect_type": "limiter"},
        ws=ws,
        project_name="proj",
    )

    assert "error" not in result
    # DB write actually happened.
    assert len(list_master_bus_effects(project)) == 1
    # And the WS emit landed.
    assert len(ws.sent) == 1
    assert ws.sent[0] == {
        "type": "master_bus_effects_changed",
        "project": "proj",
    }


# ── Chat tool: WS emit on remove ────────────────────────────────────


@pytest.mark.asyncio
async def test_exec_remove_emits_ws_message(tmp_path):
    """``_exec_remove_master_bus_effect`` must also emit the invalidation
    message so the frontend refetches after a chat-driven removal."""
    from scenecraft.chat import (
        _exec_add_master_bus_effect,
        _exec_remove_master_bus_effect,
    )
    from scenecraft.db import add_audio_track, get_db, list_master_bus_effects

    project = tmp_path / "proj"
    project.mkdir()
    get_db(project)
    add_audio_track(project, {"id": "at1", "name": "T", "display_order": 0})

    # Seed an effect via the chat tool so its own emit is part of the
    # test's pre-condition; then clear the WS log before the remove.
    ws = FakeWs()
    added = await _exec_add_master_bus_effect(
        project,
        {"effect_type": "limiter"},
        ws=ws,
        project_name="proj",
    )
    assert "error" not in added
    assert len(ws.sent) == 1  # the add emitted

    ws.sent.clear()
    removed = await _exec_remove_master_bus_effect(
        project,
        {"effect_id": added["effect_id"]},
        ws=ws,
        project_name="proj",
    )
    assert removed == {"ok": True}
    assert list_master_bus_effects(project) == []

    assert len(ws.sent) == 1
    assert ws.sent[0] == {
        "type": "master_bus_effects_changed",
        "project": "proj",
    }


# ── Chat tool: direct-call without WS is silent ─────────────────────


@pytest.mark.asyncio
async def test_exec_add_without_ws_is_silent(tmp_path):
    """Sanity: direct-call tests (ws=None) must NOT require a WS. The DB
    write still lands; the emit is simply skipped."""
    from scenecraft.chat import _exec_add_master_bus_effect
    from scenecraft.db import add_audio_track, get_db, list_master_bus_effects

    project = tmp_path / "proj"
    project.mkdir()
    get_db(project)
    add_audio_track(project, {"id": "at1", "name": "T", "display_order": 0})

    # No ws, no project_name — must not raise.
    result = await _exec_add_master_bus_effect(project, {"effect_type": "limiter"})
    assert "error" not in result
    assert len(list_master_bus_effects(project)) == 1


# ── GET endpoint: missing project → 404 ─────────────────────────────


def test_get_master_bus_effects_missing_project(server):
    """Requesting a non-existent project must return 404 with the
    standard error shape, not a 500 or empty list."""
    status, resp = _get_json(
        server, "/api/projects/does_not_exist/master-bus-effects",
    )
    assert status == 404, resp
    assert resp.get("code") == "NOT_FOUND"
