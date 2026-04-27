"""Regression tests for `local.engine-render-pipeline` (M18 task-81).

Covers:

* `scenecraft.render.schedule.build_schedule`
* `scenecraft.render.compositor.render_frame_at` + `_apply_transform`
* `scenecraft.render.narrative.assemble_final` + `_evaluate_curve`
* `scenecraft.render.cache_invalidation.invalidate_frames_for_mutation`
  (cross-ref task-74's full coverage; this file only spot-checks the
  parts that the render-pipeline spec calls out — wholesale and the
  3-block non-fatal invariant).

Test strategy
-------------

Heavy integration paths (cv2 + ffmpeg) are mocked to keep the suite
fast (seconds, not minutes) and deterministic across hosts:

* `cv2.VideoWriter`, `cv2.VideoCapture` → `MagicMock`
* `cv2.imread` → returns synthetic numpy arrays (3-ch BGR uint8)
* `subprocess.run` (ffmpeg / ffprobe) → mocked when assembled flow runs
* Pure functions (`_apply_transform`, `_evaluate_curve`, `_apply_color_grading`)
  exercise real cv2/numpy.

Target-state behaviors (per spec OQ resolutions) are marked
`@pytest.mark.xfail(strict=False, reason=...)` so they light up
automatically when the engine ships them.

Fixtures prefixed `render_` per task directive.
"""
from __future__ import annotations

import json as _json
import sys
import types
from pathlib import Path
from unittest import mock

import numpy as np
import pytest

from scenecraft import db as scdb
from scenecraft.render import cache_invalidation as ci
from scenecraft.render import compositor as cmp
from scenecraft.render import narrative as nar
from scenecraft.render import schedule as sched_mod


# ---------------------------------------------------------------------------
# Domain-scoped fixtures
# ---------------------------------------------------------------------------


def _render_write_png(path: Path, color: tuple[int, int, int] = (10, 20, 30)) -> Path:
    """Write a tiny solid-color PNG via PIL (cv2 not required for input fixtures)."""
    import cv2 as _cv2
    path.parent.mkdir(parents=True, exist_ok=True)
    arr = np.zeros((8, 16, 3), dtype=np.uint8)
    arr[:, :] = color  # BGR
    _cv2.imwrite(str(path), arr)
    return path


def _render_seed_kf(project_dir: Path, kf_id: str, ts: str, *, track_id: str = "track_1") -> None:
    scdb.add_keyframe(project_dir, {"id": kf_id, "timestamp": ts, "track_id": track_id})


def _render_seed_tr(
    project_dir: Path,
    tr_id: str,
    from_id: str,
    to_id: str,
    *,
    track_id: str = "track_1",
    selected: int | None = 1,
    duration: float = 5.0,
    extra: dict | None = None,
) -> None:
    payload = {
        "id": tr_id,
        "from": from_id,
        "to": to_id,
        "duration_seconds": duration,
        "slots": 1,
        "selected": selected,
        "track_id": track_id,
    }
    if extra:
        payload.update(extra)
    scdb.add_transition(project_dir, payload)


def _render_make_selected_video(project_dir: Path, tr_id: str) -> Path:
    """Drop a placeholder mp4 — schedule only checks `.exists()`."""
    p = project_dir / "selected_transitions" / f"{tr_id}_slot_0.mp4"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"\x00\x00\x00\x18ftypmp42")  # arbitrary
    return p


def _render_make_selected_kf_image(project_dir: Path, kf_id: str, color=(40, 80, 120)) -> Path:
    return _render_write_png(project_dir / "selected_keyframes" / f"{kf_id}.png", color)


@pytest.fixture
def render_proj(project_dir, db_conn):  # noqa: ARG001 — db_conn ensures schema bootstrap.
    """Project with schema applied; tracks table contains track_1."""
    return project_dir


@pytest.fixture
def render_videocap_stub(monkeypatch):
    """Patch cv2.VideoCapture used inside schedule.build_schedule for resolution probe.

    Returns a configurable factory: ``factory(width=640, height=480, nframes=120)``.
    """
    from scenecraft.render import schedule as sched_mod_

    class _StubCap:
        def __init__(self, width=640, height=480, nframes=120):
            self._props = {3: width, 4: height, 7: nframes}  # 3=W, 4=H, 7=COUNT

        def get(self, prop_id):  # noqa: D401
            return self._props.get(prop_id, 0.0)

        def set(self, *_a, **_k):
            return True

        def read(self):
            return False, None

        def release(self):
            return None

    def _factory(width=640, height=480, nframes=120):
        import cv2 as _cv2
        instance = _StubCap(width, height, nframes)
        monkeypatch.setattr(_cv2, "VideoCapture", lambda *_a, **_k: instance)
        return instance

    return _factory


# ---------------------------------------------------------------------------
# Unit — Schedule Build
# ---------------------------------------------------------------------------


class TestScheduleBuild:
    """Spec §Schedule Build (R1–R17). Behavior table rows 1–9."""

    def test_basic_three_segments(self, render_proj, render_videocap_stub):
        """covers R1, R2, R5, R7 — schedule-basic-three-segments."""
        render_videocap_stub(width=640, height=480)
        for i, ts in enumerate(("0:00", "0:10", "0:20", "0:30")):
            _render_seed_kf(render_proj, f"kf_{i}", ts)
        for i in range(3):
            _render_seed_tr(render_proj, f"tr_{i}", f"kf_{i}", f"kf_{i+1}", duration=10.0)
            _render_make_selected_video(render_proj, f"tr_{i}")
        sch = sched_mod.build_schedule(render_proj)
        assert len(sch.segments) == 3
        assert [s["from_ts"] for s in sch.segments] == [0.0, 10.0, 20.0]
        assert all(not s["is_still"] for s in sch.segments)
        assert all(s["source"].endswith("_slot_0.mp4") for s in sch.segments)

    def test_dedup_overlapping(self, render_proj, render_videocap_stub):
        """covers R6 — schedule-dedup-overlapping-segments. Longer segment wins."""
        render_videocap_stub()
        _render_seed_kf(render_proj, "kf_a", "0:05")
        _render_seed_kf(render_proj, "kf_b", "0:15")
        _render_seed_kf(render_proj, "kf_c", "0:10")
        _render_seed_kf(render_proj, "kf_d", "0:12")
        _render_seed_tr(render_proj, "tr_long", "kf_a", "kf_b", duration=10.0)
        _render_seed_tr(render_proj, "tr_short", "kf_c", "kf_d", duration=2.0)
        _render_make_selected_video(render_proj, "tr_long")
        _render_make_selected_video(render_proj, "tr_short")
        sch = sched_mod.build_schedule(render_proj)
        assert len(sch.segments) == 1
        assert sch.segments[0]["from_ts"] == 5.0
        assert sch.segments[0]["to_ts"] == 15.0

    def test_clamps_to_max_time(self, render_proj, render_videocap_stub):
        """covers R4 — schedule-clamps-to-max-time."""
        render_videocap_stub()
        for i, ts in enumerate(("0:00", "0:10", "0:20", "0:30")):
            _render_seed_kf(render_proj, f"kf_{i}", ts)
        for i in range(3):
            _render_seed_tr(render_proj, f"tr_{i}", f"kf_{i}", f"kf_{i+1}", duration=10.0)
            _render_make_selected_video(render_proj, f"tr_{i}")
        sch = sched_mod.build_schedule(render_proj, max_time=15.0)
        assert all(s["from_ts"] < 15.0 for s in sch.segments)
        clamped = [s for s in sch.segments if s["from_ts"] == 10.0]
        assert clamped and clamped[0]["to_ts"] == 15.0

    def test_falls_back_to_still(self, render_proj):
        """covers R5 — schedule-falls-back-to-still."""
        _render_seed_kf(render_proj, "kf_a", "0:00")
        _render_seed_kf(render_proj, "kf_b", "0:05")
        _render_seed_tr(render_proj, "tr_a", "kf_a", "kf_b", selected=None)
        _render_make_selected_kf_image(render_proj, "kf_a")
        sch = sched_mod.build_schedule(render_proj)
        assert len(sch.segments) == 1
        assert sch.segments[0]["is_still"] is True
        assert sch.segments[0]["source"].endswith(".png")

    def test_drops_missing_media(self, render_proj):
        """covers R5 — schedule-drops-missing-media."""
        _render_seed_kf(render_proj, "kf_a", "0:00")
        _render_seed_kf(render_proj, "kf_b", "0:05")
        _render_seed_tr(render_proj, "tr_a", "kf_a", "kf_b", selected=None)
        sch = sched_mod.build_schedule(render_proj)
        assert sch.segments == []

    def test_preview_halves_resolution(self, render_proj, render_videocap_stub):
        """covers R12 — schedule-preview-halves-resolution."""
        render_videocap_stub(width=1920, height=1080)
        _render_seed_kf(render_proj, "kf_a", "0:00")
        _render_seed_kf(render_proj, "kf_b", "0:05")
        _render_seed_tr(render_proj, "tr_a", "kf_a", "kf_b")
        _render_make_selected_video(render_proj, "tr_a")
        sch = sched_mod.build_schedule(render_proj, preview=True)
        assert sch.width == 960
        assert sch.height == 540
        assert sch.preview is True

    def test_crossfade_resolution_order(self, render_proj):
        """covers R13 — CLI > meta > default 8."""
        scdb.set_meta_bulk(render_proj, {"crossfade_frames": 4})
        # No segments needed for crossfade resolution
        sch = sched_mod.build_schedule(render_proj, crossfade_frames=12)
        assert sch.crossfade_frames == 12
        sch2 = sched_mod.build_schedule(render_proj)
        assert sch2.crossfade_frames == 4

    def test_overlay_mute_solo_hidden(self, render_proj, render_videocap_stub):
        """covers R8 — solo + mute + hidden interaction."""
        render_videocap_stub()
        # Seed track_1 base
        _render_seed_kf(render_proj, "kf_a", "0:00")
        _render_seed_kf(render_proj, "kf_b", "0:05")
        _render_seed_tr(render_proj, "tr_base", "kf_a", "kf_b")
        _render_make_selected_video(render_proj, "tr_base")
        # Overlay tracks
        scdb.add_track(render_proj, {"id": "track_2", "name": "Muted", "z_order": 1, "muted": True})
        scdb.add_track(render_proj, {"id": "track_3", "name": "Solo", "z_order": 2})
        scdb.add_track(render_proj, {"id": "track_4", "name": "Normal", "z_order": 3})
        scdb.update_track(render_proj, "track_3", solo=True)
        # Add at least one clip on each so the track survives the "no clips" filter
        for tid in ("track_2", "track_3", "track_4"):
            _render_seed_kf(render_proj, f"kf_{tid}_a", "0:00", track_id=tid)
            _render_seed_kf(render_proj, f"kf_{tid}_b", "0:03", track_id=tid)
            _render_seed_tr(render_proj, f"tr_{tid}", f"kf_{tid}_a", f"kf_{tid}_b", track_id=tid)
            _render_make_selected_video(render_proj, f"tr_{tid}")
        sch = sched_mod.build_schedule(render_proj)
        # Implementation: schedule.overlay_tracks lacks the track id (only fields:
        # blend_mode, opacity, clips). What we can assert: only ONE overlay track
        # survives — track_3 — given track_2 is muted and track_4 is non-solo
        # while a solo exists.
        assert len(sch.overlay_tracks) == 1

    def test_defaults_resolution_no_videos(self, render_proj):
        """covers R12 — only stills → default 1920x1080."""
        _render_seed_kf(render_proj, "kf_a", "0:00")
        _render_seed_kf(render_proj, "kf_b", "0:05")
        _render_seed_tr(render_proj, "tr_a", "kf_a", "kf_b", selected=None)
        _render_make_selected_kf_image(render_proj, "kf_a")
        sch = sched_mod.build_schedule(render_proj)
        assert (sch.width, sch.height) == (1920, 1080)

    def test_audio_path_resolution(self, render_proj):
        """covers R16 — audio_path = meta._audio_resolved or ''. """
        sch = sched_mod.build_schedule(render_proj)
        assert sch.audio_path == ""

    def test_effect_events_hard_cut_stripped_and_sorted(
        self, render_proj, render_videocap_stub, monkeypatch,
    ):
        """covers R14 — sort + hard_cut strip."""
        render_videocap_stub()
        # Drop an audio_intelligence file so the auto-discover branch fires.
        intel = {
            "layer1": {"drums": {"kick": {"onsets": [{"t": 0.5}]}}},
            "layer3_rules": [{"type": "noop"}],
        }
        (render_proj / "audio_intelligence_v2.json").write_text(_json.dumps(intel))

        # Stub the rules client → return events deliberately out-of-order with hard_cut.
        events = [
            {"time": 2.0, "effect": "zoom_pulse", "duration": 0.2, "intensity": 0.5},
            {"time": 0.5, "effect": "hard_cut", "duration": 0.1, "intensity": 1.0},
            {"time": 1.0, "effect": "shake_x", "duration": 0.2, "intensity": 0.4},
        ]
        from scenecraft.render import effects_opencv as fx_oc
        monkeypatch.setattr(fx_oc, "_apply_rules_client", lambda *_a, **_k: events)

        sch = sched_mod.build_schedule(render_proj)
        evs = sch.effect_events
        assert all(e["effect"] != "hard_cut" for e in evs)
        assert [e["time"] for e in evs] == sorted([e["time"] for e in evs])

    def test_curve_fields_json_parsed(self, render_proj, render_videocap_stub):
        """covers R7 — curve_points / opacity_curve are JSON-parsed."""
        render_videocap_stub()
        _render_seed_kf(render_proj, "kf_a", "0:00")
        _render_seed_kf(render_proj, "kf_b", "0:05")
        _render_seed_tr(
            render_proj,
            "tr_a",
            "kf_a",
            "kf_b",
            extra={
                "remap": {"method": "curve", "curve_points": [[0, 0], [1, 1]]},
                "opacity_curve": [[0, 0.0], [1, 1.0]],
            },
        )
        _render_make_selected_video(render_proj, "tr_a")
        sch = sched_mod.build_schedule(render_proj)
        seg = sch.segments[0]
        assert seg["remap_method"] == "curve"
        assert seg["curve_points"] == [[0, 0], [1, 1]]
        assert seg["opacity_curve"] == [[0, 0.0], [1, 1.0]]


# ---------------------------------------------------------------------------
# Unit — Per-Frame Compositor
# ---------------------------------------------------------------------------


def _render_make_still_schedule(
    project_dir: Path,
    *,
    n: int = 1,
    width: int = 16,
    height: int = 8,
    color=(0, 0, 200),
) -> sched_mod.Schedule:
    """Build a minimal Schedule containing `n` abutting still segments.

    Bypasses build_schedule so we can avoid DB seeding and run the
    compositor in pure-pixel land.
    """
    segments: list[dict] = []
    for i in range(n):
        png = project_dir / f"still_{i}.png"
        _render_write_png(png, color)
        segments.append({
            "from_ts": float(i * 1.0),
            "to_ts": float((i + 1) * 1.0),
            "source": str(png),
            "is_still": True,
            "remap_method": "linear",
            "curve_points": None,
            "effects": [],
            "opacity_curve": None,
        })
    return sched_mod.Schedule(
        segments=segments,
        overlay_tracks=[],
        effect_events=[],
        suppressions=[],
        meta={},
        fps=24.0,
        width=width,
        height=height,
        duration_seconds=segments[-1]["to_ts"] if segments else 0.0,
        crossfade_frames=8,
        work_dir=project_dir,
        audio_path="",
        preview=False,
    )


class TestPerFrameCompositor:
    """Spec §Per-Frame Composition (R18–R37)."""

    def test_t_outside_segments_returns_black(self, render_proj):
        """covers R18, R19, R37 — out-of-range t → black frame."""
        sch = _render_make_still_schedule(render_proj, n=1)
        frame = cmp.render_frame_at(sch, 20.0)
        assert frame.shape == (sch.height, sch.width, 3)
        assert frame.dtype == np.uint8
        assert int(frame.sum()) == 0

    def test_deterministic_same_inputs(self, render_proj):
        """covers R36 — byte-equal across two cold-cache calls."""
        sch = _render_make_still_schedule(render_proj, n=1)
        a = cmp.render_frame_at(sch, 0.5, frame_cache={})
        b = cmp.render_frame_at(sch, 0.5, frame_cache={})
        assert np.array_equal(a, b)

    def test_opacity_curve_zero_blackens_base(self, render_proj):
        """covers R24 — opacity_curve → 0 → frame fully black."""
        sch = _render_make_still_schedule(render_proj, n=1, color=(255, 255, 255))
        sch.segments[0]["opacity_curve"] = [[0.0, 0.0], [1.0, 0.0]]
        frame = cmp.render_frame_at(sch, 0.5)
        assert int(frame.sum()) == 0

    def test_strobe_blacks_base_at_off_phase(self, render_proj):
        """covers R26 — (progress*freq) % 1 > duty zeros the frame."""
        sch = _render_make_still_schedule(render_proj, n=1, color=(255, 255, 255))
        # freq=8, duty=0.1. progress at t=0.5 is mid-segment; (mid * 8) % 1
        # cycles through 0..1 — pick a t s.t. the phase lands in 'off'.
        sch.segments[0]["effects"] = [
            {"type": "strobe", "enabled": True, "params": {"frequency": 8, "duty": 0.1}},
        ]
        # Try several t's; at least one will land in the off portion.
        results = [int(cmp.render_frame_at(sch, t).sum()) for t in (0.05, 0.15, 0.25, 0.35, 0.45)]
        assert any(r == 0 for r in results), f"strobe never blacks: {results}"

    def test_zorder_respected_for_overlays(self, render_proj):
        """covers R28 — composite respects zOrder ascending (last painted wins)."""
        sch = _render_make_still_schedule(render_proj, n=1, color=(0, 0, 0))
        green_png = render_proj / "ov_green.png"
        red_png = render_proj / "ov_red.png"
        _render_write_png(green_png, (0, 255, 0))
        _render_write_png(red_png, (0, 0, 255))
        # Build two overlay tracks; per-spec they're already sorted ascending.
        sch.overlay_tracks = [
            {
                "blend_mode": "normal", "opacity": 1.0,
                "clips": [{
                    "from_ts": 0.0, "to_ts": 1.0,
                    "video": None, "still": str(green_png),
                    "blend_mode": "normal", "opacity": 1.0,
                    "effects": [],
                }],
            },
            {
                "blend_mode": "normal", "opacity": 1.0,
                "clips": [{
                    "from_ts": 0.0, "to_ts": 1.0,
                    "video": None, "still": str(red_png),
                    "blend_mode": "normal", "opacity": 1.0,
                    "effects": [],
                }],
            },
        ]
        frame = cmp.render_frame_at(sch, 0.5)
        # Last-painted (red BGR=0,0,255) should dominate the centre pixel.
        cy, cx = frame.shape[0] // 2, frame.shape[1] // 2
        assert frame[cy, cx, 2] > frame[cy, cx, 1]  # Red > Green

    def test_scrub_does_not_batch_load(self, render_proj):
        """covers R35 — scrub=True keeps loaded_segs empty (still segments still
        prime via _ensure_loaded; for stills the contract is permissive — what
        we assert is that the call returns a valid frame and does not crash)."""
        sch = _render_make_still_schedule(render_proj, n=1)
        cache: dict = {}
        frame = cmp.render_frame_at(sch, 0.5, frame_cache=cache, scrub=True)
        assert frame.shape == (sch.height, sch.width, 3)


# ---------------------------------------------------------------------------
# Unit — _apply_transform
# ---------------------------------------------------------------------------


class TestApplyTransform:
    """Spec §Transform Application (R38–R41) + OQ-5/6/7 codifications."""

    @pytest.fixture
    def red_img(self):
        a = np.zeros((40, 80, 3), dtype=np.uint8)
        a[:, :] = (0, 0, 255)  # solid red BGR
        return a

    def test_noop_returns_input(self, red_img):
        """covers R38, R39, R41 — no curves + no scalars → identity."""
        out = cmp._apply_transform(red_img.copy(), {}, 0.5)
        assert np.array_equal(out, red_img)

    def test_scale_half_keeps_dims(self, red_img):
        """covers R40 — scaled image same (h,w), zero borders."""
        clip = {
            "transform_scale_x_curve": [[0, 0.5], [1, 0.5]],
            "transform_scale_y_curve": [[0, 0.5], [1, 0.5]],
            "anchor_x": 0.5, "anchor_y": 0.5,
        }
        out = cmp._apply_transform(red_img, clip, 0.5)
        assert out.shape == red_img.shape
        # Corners are zero-padded.
        assert int(out[0, 0].sum()) == 0
        assert int(out[-1, -1].sum()) == 0
        # Centre still has color.
        ch, cw = out.shape[0] // 2, out.shape[1] // 2
        assert int(out[ch, cw, 2]) > 0

    def test_translate_right_black_pad(self, red_img):
        """covers R41 — tx > 0 shifts right, leftmost pixels become black."""
        clip = {"transform_x": 0.25, "is_adjustment": False}
        out = cmp._apply_transform(red_img, clip, 0.0)
        h, w = out.shape[:2]
        dx = int(0.25 * w)
        # Leftmost dx columns are zero (black border).
        assert int(out[:, :dx].sum()) == 0
        # Right side preserves red.
        assert int(out[:, dx:, 2].sum()) > 0

    def test_ty_sign_flips_for_non_adjustment(self):
        """covers R41 — non-adjustment ty positive → image moves UP.

        Codifies the audit gotcha: dy = int((-ty if not is_adjustment else ty) * h).
        """
        h, w = 40, 20
        img = np.zeros((h, w, 3), dtype=np.uint8)
        img[: h // 2, :] = (0, 0, 255)  # top half red
        img[h // 2:, :] = (255, 0, 0)   # bottom half blue (BGR)
        clip = {"transform_y": 0.25, "is_adjustment": False}
        out = cmp._apply_transform(img.copy(), clip, 0.0)
        # ty positive + non-adjustment → dy NEGATIVE → image translated UP.
        # Pixel that was at row h//2 (formerly blue) should now be RED
        # because the top-half red shifted up but warpAffine pulls upper rows
        # down — actually warpAffine with negative dy moves the image up
        # i.e., the NEW row at h//2 contains content from row (h//2 + |dy|),
        # which is the original BLUE region.
        # The contractual assertion: the SIGN FLIP path is taken — verify by
        # running the mirror (is_adjustment=True) and ensuring the two outputs
        # differ at the relevant rows.
        clip_adj = dict(clip)
        clip_adj["is_adjustment"] = True
        out_adj = cmp._apply_transform(img.copy(), clip_adj, 0.0)
        assert not np.array_equal(out, out_adj), (
            "non-adjustment ty must use -ty (sign flip), differing from adjustment"
        )

    def test_ty_preserved_for_adjustment(self):
        """covers R41 — is_adjustment=True preserves ty sign."""
        img = np.zeros((40, 20, 3), dtype=np.uint8)
        img[:20, :] = (0, 0, 255)
        clip_adj = {"transform_y": 0.25, "is_adjustment": True}
        out = cmp._apply_transform(img.copy(), clip_adj, 0.0)
        # ty=+0.25, adjustment → dy = int(0.25 * h) = +10 → image moves DOWN.
        # Top 10 rows should now be black border (warpAffine BORDER_CONSTANT 0).
        assert int(out[:10].sum()) == 0

    @pytest.mark.xfail(
        reason="target-state OQ-6/R55: scale=0 returns black frame; current code short-circuits to identity",
        strict=False,
    )
    def test_scale_zero_returns_black_frame(self, red_img):
        """covers R55 — scale_x=0 → black frame (target). Current code returns identity (bug)."""
        clip = {
            "transform_scale_x_curve": [[0, 0.0], [1, 0.0]],
            "transform_scale_y_curve": [[0, 1.0], [1, 1.0]],
        }
        out = cmp._apply_transform(red_img, clip, 0.5)
        assert out.shape == red_img.shape
        assert out.dtype == np.uint8
        assert int(out.sum()) == 0  # all black

    @pytest.mark.xfail(
        reason="target-state OQ-7/R56: cv2.resize uses INTER_AREA for downscale, INTER_LINEAR for upscale; current mix is transitional",
        strict=False,
    )
    def test_resize_interpolation_by_direction(self, red_img, monkeypatch):
        """covers R56 — direction-based interpolation. Currently INTER_LINEAR
        is hard-coded in `_apply_transform` (transitional)."""
        import cv2 as _cv2
        calls: list[dict] = []
        real_resize = _cv2.resize

        def _spy_resize(src, dsize, *a, **k):
            calls.append({"in_shape": src.shape, "dsize": dsize, "interp": k.get("interpolation")})
            return real_resize(src, dsize, *a, **k)

        monkeypatch.setattr(_cv2, "resize", _spy_resize)
        # Downscale.
        clip_dn = {
            "transform_scale_x_curve": [[0, 0.5], [1, 0.5]],
            "transform_scale_y_curve": [[0, 0.5], [1, 0.5]],
        }
        cmp._apply_transform(red_img.copy(), clip_dn, 0.5)
        clip_up = {
            "transform_scale_x_curve": [[0, 1.5], [1, 1.5]],
            "transform_scale_y_curve": [[0, 1.5], [1, 1.5]],
        }
        cmp._apply_transform(red_img.copy(), clip_up, 0.5)
        # Target invariant: downscale → INTER_AREA, upscale → INTER_LINEAR.
        downscale_calls = [c for c in calls if c["dsize"][0] < c["in_shape"][1]]
        upscale_calls = [c for c in calls if c["dsize"][0] > c["in_shape"][1]]
        assert all(c["interp"] == _cv2.INTER_AREA for c in downscale_calls), downscale_calls
        assert all(c["interp"] == _cv2.INTER_LINEAR for c in upscale_calls), upscale_calls


class TestEvaluateCurve:
    """covers R54 — _evaluate_curve clamps x to [0,1]."""

    def test_clamps_x_to_0_1(self):
        curve = [[0.0, 0.0], [0.5, 0.5], [1.0, 1.0]]
        assert nar._evaluate_curve(curve, -0.5) == 0.0
        assert nar._evaluate_curve(curve, 0.0) == 0.0
        assert nar._evaluate_curve(curve, 1.0) == 1.0
        assert nar._evaluate_curve(curve, 1.5) == 1.0


# ---------------------------------------------------------------------------
# Unit — Final Assembly
# ---------------------------------------------------------------------------


class _FakeVideoWriter:
    """Drop-in for cv2.VideoWriter in assemble_final tests.

    Records frame shapes; configurable `write_returns` controls per-call
    return value (used to exercise R52).
    """

    def __init__(self, *args, write_returns=True, **kwargs):
        self.args = args
        self.frames_written = 0
        self.released = False
        self._returns = write_returns
        # First positional arg is the tmp path — touch it on first write so
        # tmp-cleanup tests have something real to assert against.
        self._tmp_path = Path(args[0]) if args else None

    def write(self, frame):
        self.frames_written += 1
        if self._tmp_path is not None and not self._tmp_path.exists():
            self._tmp_path.parent.mkdir(parents=True, exist_ok=True)
            self._tmp_path.write_bytes(b"\x00")  # non-empty tmp marker
        if callable(self._returns):
            return self._returns(self.frames_written)
        return self._returns

    def release(self):
        self.released = True


@pytest.fixture
def render_assemble_stubs(monkeypatch, render_proj, render_videocap_stub):
    """Stub all cv2/ffmpeg integration points in narrative.assemble_final.

    Returns a struct with:
      - writer:           the FakeVideoWriter instance
      - subprocess_calls: list of subprocess.run argv tuples
      - schedule:         the schedule that build_schedule will return
    """
    render_videocap_stub()

    # Build a minimal schedule via the still-only path so build_schedule itself works.
    _render_seed_kf(render_proj, "kf_a", "0:00")
    _render_seed_kf(render_proj, "kf_b", "0:01")
    _render_seed_tr(render_proj, "tr_a", "kf_a", "kf_b", duration=1.0, selected=None)
    _render_make_selected_kf_image(render_proj, "kf_a")

    writer_holder: dict = {"instance": None, "write_returns": True}

    def _writer_factory(*args, **kwargs):
        w = _FakeVideoWriter(*args, write_returns=writer_holder["write_returns"], **kwargs)
        writer_holder["instance"] = w
        return w

    import cv2 as _cv2
    monkeypatch.setattr(_cv2, "VideoWriter", _writer_factory)
    monkeypatch.setattr(_cv2, "VideoWriter_fourcc", lambda *_a, **_k: 0)

    sub_calls: list = []

    def _fake_subprocess_run(argv, *a, **k):
        sub_calls.append(tuple(argv))

        class _R:
            returncode = 0
            stdout = ""
            stderr = ""
        return _R()

    monkeypatch.setattr("subprocess.run", _fake_subprocess_run)

    # Force the multi-track mixdown path to fail-fast → fall back to schedule.audio_path.
    def _raise_mixdown(*_a, **_k):
        raise RuntimeError("no audio tracks (test stub)")
    import scenecraft.audio.mixdown as mx
    monkeypatch.setattr(mx, "render_project_audio", _raise_mixdown)

    return types.SimpleNamespace(
        writer_holder=writer_holder, subprocess_calls=sub_calls,
    )


class TestFinalAssembly:
    """Spec §Final Assembly (R42–R44, R52–R58)."""

    def test_happy_path_writes_frames_and_muxes(self, render_proj, render_assemble_stubs):
        """covers R42 — opens VideoWriter, writes per-frame, runs ffmpeg mux."""
        out = render_proj / "out.mp4"
        result = nar.assemble_final(render_proj, str(out))
        assert result == str(out)
        w = render_assemble_stubs.writer_holder["instance"]
        assert w is not None
        assert w.frames_written > 0
        assert w.released is True
        # ffmpeg invoked at least once (the mux call).
        assert any(argv[0] == "ffmpeg" for argv in render_assemble_stubs.subprocess_calls)

    def test_preview_path_triggers_preview_flag(self, render_proj, render_assemble_stubs):
        """covers R42 — `_preview.mp4` suffix → preview encode opts."""
        out = render_proj / "out_preview.mp4"
        nar.assemble_final(render_proj, str(out))
        # At least one ffmpeg call should carry "ultrafast" preset.
        ffmpeg_calls = [argv for argv in render_assemble_stubs.subprocess_calls if argv[0] == "ffmpeg"]
        assert any("ultrafast" in argv for argv in ffmpeg_calls)

    def test_mixdown_failure_falls_back(self, render_proj, render_assemble_stubs):
        """covers R42 — mixdown raises → schedule.audio_path used; no exception."""
        # render_assemble_stubs already forces mixdown to raise; happy completion
        # is the witness.
        out = render_proj / "out.mp4"
        nar.assemble_final(render_proj, str(out))
        assert render_assemble_stubs.writer_holder["instance"].released is True

    @pytest.mark.xfail(
        reason="target-state OQ-2/R52: VideoWriter.write False → RenderError; current code never checks return",
        strict=False,
    )
    def test_videowriter_false_raises_render_error(self, render_proj, render_assemble_stubs):
        """covers R52 — write returning False mid-loop → RenderError; no mux."""
        render_assemble_stubs.writer_holder["write_returns"] = (
            lambda frame_num: frame_num != 5  # 5th frame fails
        )
        # Target: a RenderError class exists; raised on first False.
        from scenecraft.render.narrative import RenderError  # type: ignore[attr-defined]
        with pytest.raises(RenderError):
            nar.assemble_final(render_proj, str(render_proj / "out.mp4"))
        # And: no ffmpeg mux was invoked.
        ff = [a for a in render_assemble_stubs.subprocess_calls if a[0] == "ffmpeg"]
        assert ff == []

    @pytest.mark.xfail(
        reason="target-state OQ-3/R44: shutil.which preflight raises MissingDependencyError; current code raises FileNotFoundError unguarded",
        strict=False,
    )
    def test_ffmpeg_missing_preflight_error(self, render_proj, render_assemble_stubs, monkeypatch):
        """covers R44 — shutil.which('ffmpeg') is None → MissingDependencyError before any work."""
        import shutil as _sh
        monkeypatch.setattr(_sh, "which", lambda name: None if name == "ffmpeg" else "/usr/bin/" + name)
        from scenecraft.render.narrative import MissingDependencyError  # type: ignore[attr-defined]
        with pytest.raises(MissingDependencyError, match="ffmpeg"):
            nar.assemble_final(render_proj, str(render_proj / "out.mp4"))

    @pytest.mark.xfail(
        reason="target-state OQ-4/R53: try/finally unlinks .tmp.mp4; current code only unlinks on _mux_audio happy path",
        strict=False,
    )
    def test_tmp_cleanup_on_mux_failure(self, render_proj, render_assemble_stubs):
        """covers R53 — finally-block unlinks `.tmp.mp4` even when mux raises."""
        # Make subprocess.run raise to simulate ffmpeg failure.
        def _raise(*_a, **_k):
            raise RuntimeError("ffmpeg failed")

        with mock.patch("subprocess.run", _raise):
            with pytest.raises(RuntimeError):
                nar.assemble_final(render_proj, str(render_proj / "out.mp4"))
        # tmp should not survive.
        assert not (render_proj / "out.mp4.tmp.mp4").exists()

    @pytest.mark.xfail(
        reason="target-state OQ-8/R57: zero-duration short-circuits before opening writer; current code opens an empty writer",
        strict=False,
    )
    def test_zero_duration_short_circuits(self, render_proj, render_assemble_stubs):
        """covers R57 — duration_seconds == 0 → no writer opened, no output file."""
        # Force schedule with no segments by patching build_schedule.
        empty_sched = sched_mod.Schedule(
            segments=[], overlay_tracks=[], effect_events=[], suppressions=[],
            meta={}, fps=24.0, width=16, height=8, duration_seconds=0.0,
            crossfade_frames=8, work_dir=render_proj, audio_path="", preview=False,
        )
        with mock.patch.object(sched_mod, "build_schedule", return_value=empty_sched):
            nar.assemble_final(render_proj, str(render_proj / "out.mp4"))
        assert render_assemble_stubs.writer_holder["instance"] is None
        assert not (render_proj / "out.mp4").exists()

    def test_no_lock_held_across_render_loop(self):
        """covers R58 (negative-assertion) — no threading.Lock acquired across the render loop."""
        import inspect
        src = inspect.getsource(nar.assemble_final)
        # Spec invariant: no per-project mutex spans the loop.
        assert "_render_locks[" not in src
        assert "Lock()" not in src or "with lock" not in src


# ---------------------------------------------------------------------------
# Unit — Cache Invalidation (spot checks; full coverage in test_engine_cache_invalidation.py)
# ---------------------------------------------------------------------------


class TestCacheInvalidation:
    """Spec §Cache Invalidation (R45–R51) — render-pipeline-level invariants."""

    def test_returns_int_int_tuple(self, project_dir):
        """covers R45 — return shape is `(int, int)`."""
        result = ci.invalidate_frames_for_mutation(project_dir, None)
        assert isinstance(result, tuple) and len(result) == 2
        assert all(isinstance(x, int) for x in result)

    def test_never_raises_on_arbitrary_failure(self, project_dir, monkeypatch):
        """covers R50 — every step is independently `try/except`'d."""
        # Force every collaborator to raise.
        from scenecraft.render import frame_cache as fc, fragment_cache as gc
        monkeypatch.setattr(fc.global_cache, "invalidate_project", lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("frames")))
        monkeypatch.setattr(gc.global_fragment_cache, "invalidate_project", lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("frags")))
        # The function must not raise.
        ci.invalidate_frames_for_mutation(project_dir, None)

    def test_three_independent_try_except_blocks(self):
        """covers R50 — source has THREE separate try/except blocks (frame, fragment, coordinator)."""
        import inspect
        src = inspect.getsource(ci.invalidate_frames_for_mutation)
        # Three distinct `except Exception:` swallowers.
        assert src.count("except Exception:") >= 3

    def test_wholesale_invalidation_skips_bg_requeue(self, project_dir, monkeypatch):
        """covers R46, R49 — ranges=None → coordinator.invalidate_project but NO bg requeue."""
        log: list[str] = []
        import scenecraft.render.preview_worker as pw

        class _Coord:
            def invalidate_project(self, _p):
                log.append("invalidate_project")

            def invalidate_ranges_in_background(self, _p, _r):
                log.append("invalidate_ranges_in_background")

        class _CoordCls:
            @classmethod
            def instance(cls):
                return _Coord()

        monkeypatch.setattr(pw, "RenderCoordinator", _CoordCls)
        ci.invalidate_frames_for_mutation(project_dir, None)
        assert "invalidate_project" in log
        assert "invalidate_ranges_in_background" not in log

    def test_range_triggers_bg_requeue(self, project_dir, monkeypatch):
        """covers R48 — ranges=[(a,b)] → coordinator gets bg requeue nudge."""
        log: list[str] = []
        import scenecraft.render.preview_worker as pw

        class _Coord:
            def invalidate_project(self, _p):
                log.append("invalidate_project")

            def invalidate_ranges_in_background(self, _p, _r):
                log.append(f"invalidate_ranges_in_background:{_r}")

        class _CoordCls:
            @classmethod
            def instance(cls):
                return _Coord()

        monkeypatch.setattr(pw, "RenderCoordinator", _CoordCls)
        ci.invalidate_frames_for_mutation(project_dir, [(5.0, 9.0)])
        assert any(s.startswith("invalidate_ranges_in_background") for s in log)


# ---------------------------------------------------------------------------
# E2E — observational via HTTP /render-state and /render-cache/stats
# ---------------------------------------------------------------------------


class TestEndToEnd:
    """End-to-end coverage via the live engine_server.

    The render pipeline is a CLI/programmatic surface (assemble_final +
    render_frame_at). The only HTTP-observable surfaces are:
      - GET /api/render-cache/stats
      - GET /api/projects/:name/render-state
      - GET /api/projects/:name/render-frame?t=...

    Real video render is too slow + fragile for CI, so e2e tests here
    drive the observational endpoints and assert their shape /
    cache-invalidation effects.
    """

    def test_render_cache_stats_endpoint_shape(self, engine_server):
        """GET /api/render-cache/stats returns frame_cache + fragment_cache stats."""
        status, body = engine_server.json("GET", "/api/render-cache/stats")
        assert status == 200
        assert isinstance(body, dict)
        assert "frame_cache" in body
        assert "fragment_cache" in body

    def test_render_state_endpoint_for_empty_project(self, engine_server, project_name):
        """GET /api/projects/:name/render-state returns a snapshot (empty timeline OK)."""
        status, body = engine_server.json(
            "GET", f"/api/projects/{project_name}/render-state",
        )
        assert status == 200
        assert isinstance(body, (dict, list))

    def test_invalidate_cache_then_stats_still_responsive(self, engine_server, project_name):
        """After explicitly invalidating, /render-cache/stats remains queryable.

        Drives the render-pipeline cache_invalidation chokepoint indirectly:
        any DB mutation through the API funnels through it; the stats
        endpoint must not 500 after that path runs.
        """
        # Trigger a DB mutation that lands in the chokepoint.
        # Adding a track is a known mutating endpoint.
        status, _body = engine_server.json(
            "POST", f"/api/projects/{project_name}/tracks",
            {"name": "Test Track", "z_order": 1},
        )
        # Either the endpoint exists (200) or doesn't (404) — we only
        # care that the subsequent stats call succeeds.
        assert status in (200, 201, 400, 404, 405)

        status2, body2 = engine_server.json("GET", "/api/render-cache/stats")
        assert status2 == 200
        assert "frame_cache" in body2 and "fragment_cache" in body2

    @pytest.mark.xfail(
        reason="target-state: byte-identical /render-frame golden requires a full project fixture and proxy plumbing; covered by sibling proxy/preview specs",
        strict=False,
    )
    def test_render_frame_byte_identical_twice(self, engine_server, project_name):
        status1, _h1, body1 = engine_server.request(
            "GET", f"/api/projects/{project_name}/render-frame?t=0",
        )
        status2, _h2, body2 = engine_server.request(
            "GET", f"/api/projects/{project_name}/render-frame?t=0",
        )
        assert status1 == 200 and status2 == 200
        assert body1 == body2  # byte-identical
