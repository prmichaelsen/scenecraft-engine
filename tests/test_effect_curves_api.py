"""Tests for M13 task-52: effect-curves + macro-panel HTTP endpoints.

Spec: local.effect-curves-macro-panel.md §Interfaces + R_V1.

Covers the full R_V1 POST-validation contract (task doc §5 test list):

  * happy-path POST/DELETE round-trip for each of 13 endpoints
  * unknown ``effect_type`` → 400 (spec test ``unknown-effect-type-rejected``)
  * ``__send`` POSTed to /track-effects → 400 (R8a)
  * POST /effect-curves with missing ``effect_id`` → 404
  * POST /effect-curves for non-animatable param → 400 naming the param
  * curve points out-of-range clamped to [0, 1] (spec test ``curve-point-values-out-of-range-clamped``)
  * DELETE on non-existent id → 200 empty body (spec test ``delete-nonexistent-effect-idempotent``)
  * order_index collision → atomic swap in one txn (spec test
    ``order-index-collision-resolved-atomically``)
  * DELETE /track-effects cascades to effect_curves (spec test
    ``orphan-curve-cleaned-on-effect-delete``)
  * ``mixer.chain-rebuilt`` fires exactly once per add/update/delete
"""

from __future__ import annotations

import json
import shutil
import tempfile
import threading
from http.server import HTTPServer
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.error import HTTPError

import pytest

from scenecraft.api_server import make_handler
from scenecraft.db import (
    _migrated_dbs,
    add_audio_track,
    add_effect_curve,
    add_track_effect,
    close_db,
    get_db,
    get_effect_curve,
    get_track_effect,
    list_curves_for_effect,
    list_send_buses,
    list_track_effects,
    list_track_sends,
    set_meta,
)


# ── Fixtures ────────────────────────────────────────────────────────


@pytest.fixture
def env(tmp_path, monkeypatch):
    work_dir = Path(tempfile.mkdtemp())
    project_name = "m13_api"
    project_dir = work_dir / project_name
    project_dir.mkdir()

    set_meta(project_dir, "title", "M13 API Test")
    set_meta(project_dir, "fps", 24)

    Handler = make_handler(work_dir, no_auth=True)
    server = HTTPServer(("127.0.0.1", 0), Handler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    # Seed an audio track so track_sends auto-seed for the default buses fires.
    track_id = "track_fx_api"
    add_audio_track(project_dir, {"id": track_id, "name": "FX", "display_order": 0})

    yield {
        "work_dir": work_dir,
        "project_dir": project_dir,
        "project_name": project_name,
        "track_id": track_id,
        "base_url": f"http://127.0.0.1:{port}",
    }

    server.shutdown()
    close_db(project_dir)
    _migrated_dbs.discard(str(project_dir / "project.db"))
    shutil.rmtree(work_dir, ignore_errors=True)


def _req(
    env: dict,
    method: str,
    path: str,
    body=None,
    *,
    expect_status: int | None = None,
):
    """Raw HTTP request returning (status, body). Accepts an expected status;
    raises AssertionError if the returned status does not match."""
    url = f"{env['base_url']}{path}"
    data = json.dumps(body).encode() if body is not None else None
    req = Request(url, data=data, method=method)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        resp = urlopen(req)
        raw = resp.read()
        status = resp.status
    except HTTPError as e:
        raw = e.read()
        status = e.code
    try:
        parsed = json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        parsed = {"_raw": raw}
    if expect_status is not None:
        assert status == expect_status, (
            f"{method} {path}: expected {expect_status}, got {status}: {parsed}"
        )
    return status, parsed


def _project_path(env: dict, sub: str) -> str:
    return f"/api/projects/{env['project_name']}{sub}"


# ── /track-effects ──────────────────────────────────────────────────


class TestTrackEffectsCreate:
    def test_happy_path_returns_effect(self, env):
        status, body = _req(
            env, "POST", _project_path(env, "/track-effects"),
            {
                "track_id": env["track_id"],
                "effect_type": "compressor",
                "static_params": {"ratio": 4, "attack": 10},
            },
            expect_status=200,
        )
        assert body["track_id"] == env["track_id"]
        assert body["effect_type"] == "compressor"
        assert body["order_index"] == 0
        assert body["enabled"] is True
        assert body["static_params"] == {"ratio": 4, "attack": 10}
        # DB row is present.
        effects = list_track_effects(env["project_dir"], env["track_id"])
        assert len(effects) == 1
        assert effects[0].id == body["id"]

    def test_unknown_effect_type_rejected(self, env):
        """Spec test ``unknown-effect-type-rejected`` — R_V1."""
        status, body = _req(
            env, "POST", _project_path(env, "/track-effects"),
            {"track_id": env["track_id"], "effect_type": "timewarp"},
            expect_status=400,
        )
        assert "timewarp" in body.get("error", "")
        assert list_track_effects(env["project_dir"], env["track_id"]) == []

    def test_synthetic_send_type_rejected(self, env):
        """R8a: ``__send`` is synthetic and rejected by POST /track-effects."""
        status, body = _req(
            env, "POST", _project_path(env, "/track-effects"),
            {"track_id": env["track_id"], "effect_type": "__send"},
            expect_status=400,
        )
        assert "__send" in body.get("error", "")

    def test_missing_track_id_rejected(self, env):
        status, body = _req(
            env, "POST", _project_path(env, "/track-effects"),
            {"effect_type": "drive"},
            expect_status=400,
        )

    def test_unknown_track_id_404(self, env):
        status, body = _req(
            env, "POST", _project_path(env, "/track-effects"),
            {"track_id": "ghost_track", "effect_type": "drive"},
            expect_status=404,
        )


class TestTrackEffectsUpdate:
    def test_update_enabled(self, env):
        _, created = _req(
            env, "POST", _project_path(env, "/track-effects"),
            {"track_id": env["track_id"], "effect_type": "drive"},
            expect_status=200,
        )
        eff_id = created["id"]
        _, updated = _req(
            env, "POST", _project_path(env, f"/track-effects/{eff_id}"),
            {"enabled": False},
            expect_status=200,
        )
        assert updated["enabled"] is False

    def test_update_nonexistent_404(self, env):
        _req(
            env, "POST", _project_path(env, "/track-effects/ghost"),
            {"enabled": False},
            expect_status=404,
        )

    def test_order_index_collision_atomic_swap(self, env):
        """Spec test ``order-index-collision-resolved-atomically``: POST to
        an existing effect with an order_index already held by a sibling
        MUST swap atomically. Final ordering has no duplicates."""
        track = env["track_id"]
        _, e1 = _req(
            env, "POST", _project_path(env, "/track-effects"),
            {"track_id": track, "effect_type": "compressor"},
        )
        _, e2 = _req(
            env, "POST", _project_path(env, "/track-effects"),
            {"track_id": track, "effect_type": "drive"},
        )
        _, e3 = _req(
            env, "POST", _project_path(env, "/track-effects"),
            {"track_id": track, "effect_type": "limiter"},
        )
        assert (e1["order_index"], e2["order_index"], e3["order_index"]) == (0, 1, 2)

        # Move E3 to position 0.
        _req(
            env, "POST", _project_path(env, f"/track-effects/{e3['id']}"),
            {"order_index": 0},
            expect_status=200,
        )
        final = list_track_effects(env["project_dir"], track)
        order = {eff.id: eff.order_index for eff in final}
        # All three have distinct order_index values (no duplicates).
        assert len(set(order.values())) == 3
        # E3 is first.
        assert order[e3["id"]] == 0
        # E1/E2 shifted to positions 1 and 2 (order preserved).
        assert {order[e1["id"]], order[e2["id"]]} == {1, 2}
        assert order[e1["id"]] < order[e2["id"]]


class TestTrackEffectsDelete:
    def test_delete_happy_path(self, env):
        _, created = _req(
            env, "POST", _project_path(env, "/track-effects"),
            {"track_id": env["track_id"], "effect_type": "drive"},
        )
        _, resp = _req(
            env, "DELETE", _project_path(env, f"/track-effects/{created['id']}"),
            expect_status=200,
        )
        assert resp == {}
        assert list_track_effects(env["project_dir"], env["track_id"]) == []

    def test_delete_nonexistent_is_idempotent(self, env):
        """Spec test ``delete-nonexistent-effect-idempotent`` — R_V1."""
        _, resp = _req(
            env, "DELETE", _project_path(env, "/track-effects/ghost-123"),
            expect_status=200,
        )
        assert resp == {}

    def test_delete_cascades_to_curves(self, env):
        """Spec test ``orphan-curve-cleaned-on-effect-delete``."""
        _, created = _req(
            env, "POST", _project_path(env, "/track-effects"),
            {"track_id": env["track_id"], "effect_type": "drive"},
        )
        eff_id = created["id"]
        _req(
            env, "POST", _project_path(env, "/effect-curves"),
            {"effect_id": eff_id, "param_name": "amount",
             "points": [[0.0, 0.2], [1.0, 0.8]], "interpolation": "bezier"},
            expect_status=200,
        )
        _req(
            env, "POST", _project_path(env, "/effect-curves"),
            {"effect_id": eff_id, "param_name": "tone",
             "points": [[0.0, 0.4]], "interpolation": "linear"},
            expect_status=200,
        )
        assert len(list_curves_for_effect(env["project_dir"], eff_id)) == 2

        _req(
            env, "DELETE", _project_path(env, f"/track-effects/{eff_id}"),
            expect_status=200,
        )
        # Curves cascade-deleted.
        assert list_curves_for_effect(env["project_dir"], eff_id) == []


# ── /effect-curves ──────────────────────────────────────────────────


class TestEffectCurvesCreate:
    def test_happy_path(self, env):
        _, eff = _req(
            env, "POST", _project_path(env, "/track-effects"),
            {"track_id": env["track_id"], "effect_type": "highpass"},
        )
        _, curve = _req(
            env, "POST", _project_path(env, "/effect-curves"),
            {
                "effect_id": eff["id"], "param_name": "cutoff",
                "points": [[0.0, 0.2], [1.0, 0.8]],
                "interpolation": "bezier", "visible": True,
            },
            expect_status=200,
        )
        assert curve["effect_id"] == eff["id"]
        assert curve["param_name"] == "cutoff"
        assert curve["points"] == [[0.0, 0.2], [1.0, 0.8]]
        assert curve["interpolation"] == "bezier"
        assert curve["visible"] is True

    def test_nonexistent_effect_id_404(self, env):
        """Spec test ``animating-static-param-rejected`` case (b) — R_V1."""
        _, body = _req(
            env, "POST", _project_path(env, "/effect-curves"),
            {"effect_id": "ghost_effect", "param_name": "cutoff",
             "points": [[0, 0.5]]},
            expect_status=404,
        )

    def test_static_param_rejected_on_drive_character(self, env):
        """Spec test ``animating-static-param-rejected`` case (a) — R9 static params."""
        _, eff = _req(
            env, "POST", _project_path(env, "/track-effects"),
            {"track_id": env["track_id"], "effect_type": "drive"},
        )
        _, body = _req(
            env, "POST", _project_path(env, "/effect-curves"),
            {"effect_id": eff["id"], "param_name": "character",
             "points": [[0, 0.5]]},
            expect_status=400,
        )
        assert "character" in body.get("error", "")

    def test_static_param_rejected_on_lfo_rate(self, env):
        """Per R9: ``rate`` on LFO modulation effects is static."""
        _, eff = _req(
            env, "POST", _project_path(env, "/track-effects"),
            {"track_id": env["track_id"], "effect_type": "tremolo"},
        )
        _, body = _req(
            env, "POST", _project_path(env, "/effect-curves"),
            {"effect_id": eff["id"], "param_name": "rate",
             "points": [[0, 0.5]]},
            expect_status=400,
        )
        assert "rate" in body.get("error", "")

    def test_static_param_rejected_on_send_bus_id(self, env):
        """R9: ``bus_id`` on sends is static (routing target)."""
        _, eff = _req(
            env, "POST", _project_path(env, "/track-effects"),
            {"track_id": env["track_id"], "effect_type": "reverb_send"},
        )
        _, body = _req(
            env, "POST", _project_path(env, "/effect-curves"),
            {"effect_id": eff["id"], "param_name": "bus_id",
             "points": [[0, 0.5]]},
            expect_status=400,
        )
        assert "bus_id" in body.get("error", "")

    def test_unknown_param_rejected(self, env):
        _, eff = _req(
            env, "POST", _project_path(env, "/track-effects"),
            {"track_id": env["track_id"], "effect_type": "drive"},
        )
        _, body = _req(
            env, "POST", _project_path(env, "/effect-curves"),
            {"effect_id": eff["id"], "param_name": "gremlin",
             "points": [[0, 0.5]]},
            expect_status=400,
        )

    def test_invalid_interpolation_rejected(self, env):
        _, eff = _req(
            env, "POST", _project_path(env, "/track-effects"),
            {"track_id": env["track_id"], "effect_type": "drive"},
        )
        _req(
            env, "POST", _project_path(env, "/effect-curves"),
            {"effect_id": eff["id"], "param_name": "amount",
             "points": [], "interpolation": "cubic"},
            expect_status=400,
        )

    def test_points_clamped_to_unit_range(self, env, caplog):
        """Spec test ``curve-point-values-out-of-range-clamped`` — R6/R17.
        Clamp is NOT an error (200) and a warning is logged."""
        _, eff = _req(
            env, "POST", _project_path(env, "/track-effects"),
            {"track_id": env["track_id"], "effect_type": "drive"},
        )
        _, curve = _req(
            env, "POST", _project_path(env, "/effect-curves"),
            {
                "effect_id": eff["id"], "param_name": "amount",
                "points": [[1.0, -0.5], [2.0, 1.8], [3.0, 0.5]],
                "interpolation": "bezier",
            },
            expect_status=200,
        )
        pts = curve["points"]
        assert pts[0] == [1.0, 0.0]
        assert pts[1] == [2.0, 1.0]
        assert pts[2] == [3.0, 0.5]


class TestEffectCurvesUpdate:
    def test_update_visible(self, env):
        _, eff = _req(
            env, "POST", _project_path(env, "/track-effects"),
            {"track_id": env["track_id"], "effect_type": "drive"},
        )
        _, curve = _req(
            env, "POST", _project_path(env, "/effect-curves"),
            {"effect_id": eff["id"], "param_name": "amount",
             "points": [[0, 0.5]], "visible": False},
        )
        _, updated = _req(
            env, "POST", _project_path(env, f"/effect-curves/{curve['id']}"),
            {"visible": True},
            expect_status=200,
        )
        assert updated["visible"] is True

    def test_update_nonexistent_404(self, env):
        _req(
            env, "POST", _project_path(env, "/effect-curves/ghost"),
            {"visible": True},
            expect_status=404,
        )

    def test_update_clamps_points(self, env):
        _, eff = _req(
            env, "POST", _project_path(env, "/track-effects"),
            {"track_id": env["track_id"], "effect_type": "drive"},
        )
        _, curve = _req(
            env, "POST", _project_path(env, "/effect-curves"),
            {"effect_id": eff["id"], "param_name": "amount",
             "points": [[0, 0.2]]},
        )
        _, updated = _req(
            env, "POST", _project_path(env, f"/effect-curves/{curve['id']}"),
            {"points": [[1.0, -1.0], [2.0, 2.0]]},
            expect_status=200,
        )
        assert updated["points"] == [[1.0, 0.0], [2.0, 1.0]]


class TestEffectCurvesDelete:
    def test_delete_happy_path(self, env):
        _, eff = _req(
            env, "POST", _project_path(env, "/track-effects"),
            {"track_id": env["track_id"], "effect_type": "drive"},
        )
        _, curve = _req(
            env, "POST", _project_path(env, "/effect-curves"),
            {"effect_id": eff["id"], "param_name": "amount",
             "points": [[0, 0.3]]},
        )
        _, resp = _req(
            env, "DELETE", _project_path(env, f"/effect-curves/{curve['id']}"),
            expect_status=200,
        )
        assert resp == {}
        assert get_effect_curve(env["project_dir"], curve["id"]) is None

    def test_delete_nonexistent_idempotent(self, env):
        _, resp = _req(
            env, "DELETE", _project_path(env, "/effect-curves/ghost-curve"),
            expect_status=200,
        )
        assert resp == {}


# ── /send-buses ─────────────────────────────────────────────────────


class TestSendBuses:
    def test_create_happy_path(self, env):
        _, bus = _req(
            env, "POST", _project_path(env, "/send-buses"),
            {"bus_type": "reverb", "label": "Chamber",
             "static_params": {"ir": "chamber.wav"}},
            expect_status=200,
        )
        assert bus["bus_type"] == "reverb"
        assert bus["label"] == "Chamber"
        buses = list_send_buses(env["project_dir"])
        # Default seed is 4 + the new one = 5.
        assert len(buses) == 5

    def test_create_invalid_bus_type(self, env):
        _req(
            env, "POST", _project_path(env, "/send-buses"),
            {"bus_type": "warp", "label": "Broken"},
            expect_status=400,
        )

    def test_create_missing_label(self, env):
        _req(
            env, "POST", _project_path(env, "/send-buses"),
            {"bus_type": "delay"},
            expect_status=400,
        )

    def test_update_label(self, env):
        buses = list_send_buses(env["project_dir"])
        bus_id = buses[0].id
        _, updated = _req(
            env, "POST", _project_path(env, f"/send-buses/{bus_id}"),
            {"label": "Renamed"},
            expect_status=200,
        )
        assert updated["label"] == "Renamed"

    def test_update_nonexistent_404(self, env):
        _req(
            env, "POST", _project_path(env, "/send-buses/ghost-bus"),
            {"label": "x"},
            expect_status=404,
        )

    def test_delete_happy_path(self, env):
        buses_before = list_send_buses(env["project_dir"])
        bus_id = buses_before[0].id
        _req(
            env, "DELETE", _project_path(env, f"/send-buses/{bus_id}"),
            expect_status=200,
        )
        buses_after = list_send_buses(env["project_dir"])
        assert len(buses_after) == len(buses_before) - 1

    def test_delete_nonexistent_idempotent(self, env):
        _, resp = _req(
            env, "DELETE", _project_path(env, "/send-buses/ghost-bus"),
            expect_status=200,
        )
        assert resp == {}


# ── /track-sends ────────────────────────────────────────────────────


class TestTrackSends:
    def test_upsert_happy_path(self, env):
        buses = list_send_buses(env["project_dir"])
        bus_id = buses[0].id
        _, resp = _req(
            env, "POST", _project_path(env, "/track-sends"),
            {"track_id": env["track_id"], "bus_id": bus_id, "level": 0.7},
            expect_status=200,
        )
        assert resp["track_id"] == env["track_id"]
        assert resp["bus_id"] == bus_id
        assert resp["level"] == 0.7
        sends = list_track_sends(env["project_dir"], track_id=env["track_id"], bus_id=bus_id)
        assert len(sends) == 1
        assert sends[0].level == 0.7

    def test_upsert_overwrites_existing_level(self, env):
        buses = list_send_buses(env["project_dir"])
        bus_id = buses[0].id
        _req(
            env, "POST", _project_path(env, "/track-sends"),
            {"track_id": env["track_id"], "bus_id": bus_id, "level": 0.3},
        )
        _req(
            env, "POST", _project_path(env, "/track-sends"),
            {"track_id": env["track_id"], "bus_id": bus_id, "level": 0.9},
        )
        sends = list_track_sends(env["project_dir"], track_id=env["track_id"], bus_id=bus_id)
        assert sends[0].level == 0.9

    def test_unknown_track_404(self, env):
        buses = list_send_buses(env["project_dir"])
        _req(
            env, "POST", _project_path(env, "/track-sends"),
            {"track_id": "ghost", "bus_id": buses[0].id, "level": 0.5},
            expect_status=404,
        )

    def test_unknown_bus_404(self, env):
        _req(
            env, "POST", _project_path(env, "/track-sends"),
            {"track_id": env["track_id"], "bus_id": "ghost", "level": 0.5},
            expect_status=404,
        )

    def test_missing_level(self, env):
        buses = list_send_buses(env["project_dir"])
        _req(
            env, "POST", _project_path(env, "/track-sends"),
            {"track_id": env["track_id"], "bus_id": buses[0].id},
            expect_status=400,
        )


# ── /frequency-labels ───────────────────────────────────────────────


class TestFrequencyLabels:
    def test_create_happy_path(self, env):
        _, resp = _req(
            env, "POST", _project_path(env, "/frequency-labels"),
            {"label": "My Hat", "freq_min_hz": 11000, "freq_max_hz": 13000},
            expect_status=200,
        )
        assert resp["label"] == "My Hat"
        assert resp["freq_min_hz"] == 11000
        assert resp["freq_max_hz"] == 13000

    def test_create_invalid_range(self, env):
        _req(
            env, "POST", _project_path(env, "/frequency-labels"),
            {"label": "Bad", "freq_min_hz": 5000, "freq_max_hz": 1000},
            expect_status=400,
        )

    def test_create_missing_label(self, env):
        _req(
            env, "POST", _project_path(env, "/frequency-labels"),
            {"freq_min_hz": 100, "freq_max_hz": 500},
            expect_status=400,
        )

    def test_delete_happy_path(self, env):
        _, created = _req(
            env, "POST", _project_path(env, "/frequency-labels"),
            {"label": "Keep", "freq_min_hz": 100, "freq_max_hz": 500},
        )
        _, resp = _req(
            env, "DELETE", _project_path(env, f"/frequency-labels/{created['id']}"),
            expect_status=200,
        )
        assert resp == {}

    def test_delete_nonexistent_idempotent(self, env):
        _, resp = _req(
            env, "DELETE", _project_path(env, "/frequency-labels/ghost"),
            expect_status=200,
        )
        assert resp == {}


# ── mixer.chain-rebuilt event broadcast ─────────────────────────────


class TestMixerEvents:
    """The ``mixer.chain-rebuilt`` event is broadcast via the ws_server's
    job_manager when a track-effect is added, updated (including order-index
    swap), or deleted. Spec test ``order-index-collision-resolved-atomically``
    asserts exactly ONE event per POST."""

    def test_create_broadcasts_chain_rebuilt(self, env, monkeypatch):
        events = self._patch_broadcast(monkeypatch)
        _req(
            env, "POST", _project_path(env, "/track-effects"),
            {"track_id": env["track_id"], "effect_type": "drive"},
            expect_status=200,
        )
        chain_events = [e for e in events if e.get("type") == "mixer.chain-rebuilt"]
        assert len(chain_events) == 1
        assert chain_events[0]["track_id"] == env["track_id"]

    def test_order_index_swap_broadcasts_exactly_once(self, env, monkeypatch):
        track = env["track_id"]
        _, e1 = _req(
            env, "POST", _project_path(env, "/track-effects"),
            {"track_id": track, "effect_type": "compressor"},
        )
        _, e2 = _req(
            env, "POST", _project_path(env, "/track-effects"),
            {"track_id": track, "effect_type": "drive"},
        )
        _, e3 = _req(
            env, "POST", _project_path(env, "/track-effects"),
            {"track_id": track, "effect_type": "limiter"},
        )
        # Start capturing AFTER creates so we only see the update's event.
        events = self._patch_broadcast(monkeypatch)
        _req(
            env, "POST", _project_path(env, f"/track-effects/{e3['id']}"),
            {"order_index": 0},
            expect_status=200,
        )
        chain_events = [e for e in events if e.get("type") == "mixer.chain-rebuilt"]
        assert len(chain_events) == 1

    def test_delete_broadcasts_chain_rebuilt(self, env, monkeypatch):
        _, created = _req(
            env, "POST", _project_path(env, "/track-effects"),
            {"track_id": env["track_id"], "effect_type": "drive"},
        )
        events = self._patch_broadcast(monkeypatch)
        _req(
            env, "DELETE", _project_path(env, f"/track-effects/{created['id']}"),
            expect_status=200,
        )
        chain_events = [e for e in events if e.get("type") == "mixer.chain-rebuilt"]
        assert len(chain_events) == 1

    def test_idempotent_delete_no_broadcast(self, env, monkeypatch):
        """DELETE on non-existent id is a no-op and must NOT broadcast."""
        events = self._patch_broadcast(monkeypatch)
        _req(
            env, "DELETE", _project_path(env, "/track-effects/ghost"),
            expect_status=200,
        )
        chain_events = [e for e in events if e.get("type") == "mixer.chain-rebuilt"]
        assert chain_events == []

    @staticmethod
    def _patch_broadcast(monkeypatch) -> list:
        """Patch ``job_manager._broadcast`` to capture events into a list.
        Returns the list (mutated in place as events arrive)."""
        captured: list = []
        from scenecraft.ws_server import job_manager
        original = job_manager._broadcast

        def _capture(msg):
            captured.append(msg)
            # Do not call original — it writes to real WS connections and
            # would try to push to listeners that don't exist in tests.

        monkeypatch.setattr(job_manager, "_broadcast", _capture)
        return captured
