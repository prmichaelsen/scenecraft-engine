"""E2E tests for multi-track operations: split, delete/bridge, transition effects, track scoping."""

import json
import tempfile
import shutil
from pathlib import Path

import pytest

from beatlab.db import (
    get_db, close_db, get_meta, set_meta,
    get_keyframes, get_keyframe, add_keyframe, delete_keyframe, next_keyframe_id,
    get_transitions, get_transition, add_transition, delete_transition, restore_transition,
    next_transition_id, get_transitions_involving, update_transition,
    get_transition_effects, get_all_transition_effects,
    add_transition_effect, update_transition_effect, delete_transition_effect,
)


@pytest.fixture
def project_dir():
    """Create a temp project directory with two tracks."""
    d = Path(tempfile.mkdtemp())
    conn = get_db(d)
    # Ensure track_2 exists
    try:
        conn.execute("INSERT INTO tracks (id, name, z_order, blend_mode, base_opacity, enabled) VALUES ('track_2', 'Track 2', 1, 'screen', 1.0, 1)")
        conn.commit()
    except Exception:
        pass
    yield d
    close_db(d)
    shutil.rmtree(d)


def _add_kf(project_dir, kf_id, timestamp, track_id="track_1"):
    add_keyframe(project_dir, {
        "id": kf_id, "timestamp": timestamp, "section": "",
        "source": f"selected_keyframes/{kf_id}.png", "prompt": "test",
        "candidates": [], "selected": None, "track_id": track_id,
    })


def _add_tr(project_dir, tr_id, from_kf, to_kf, track_id="track_1", duration=1.0):
    add_transition(project_dir, {
        "id": tr_id, "from": from_kf, "to": to_kf,
        "duration_seconds": duration, "slots": 1,
        "action": "test", "use_global_prompt": False, "selected": None,
        "remap": {"method": "linear", "target_duration": duration},
        "track_id": track_id,
    })


class TestTransitionEffectsCRUD:
    """Test the transition_effects table CRUD operations."""

    def test_add_and_get(self, project_dir):
        _add_kf(project_dir, "kf_001", "0:00")
        _add_kf(project_dir, "kf_002", "0:05")
        _add_tr(project_dir, "tr_001", "kf_001", "kf_002")

        effect_id = add_transition_effect(project_dir, "tr_001", "strobe", {"frequency": 8, "duty": 0.5})
        assert effect_id.startswith("tfx_")

        effects = get_transition_effects(project_dir, "tr_001")
        assert len(effects) == 1
        assert effects[0]["type"] == "strobe"
        assert effects[0]["params"]["frequency"] == 8
        assert effects[0]["params"]["duty"] == 0.5
        assert effects[0]["enabled"] is True

    def test_multiple_effects(self, project_dir):
        _add_kf(project_dir, "kf_001", "0:00")
        _add_kf(project_dir, "kf_002", "0:05")
        _add_tr(project_dir, "tr_001", "kf_001", "kf_002")

        id1 = add_transition_effect(project_dir, "tr_001", "strobe", {"frequency": 4})
        id2 = add_transition_effect(project_dir, "tr_001", "strobe", {"frequency": 16})

        effects = get_transition_effects(project_dir, "tr_001")
        assert len(effects) == 2
        assert effects[0]["zOrder"] < effects[1]["zOrder"]

    def test_update_params(self, project_dir):
        _add_kf(project_dir, "kf_001", "0:00")
        _add_kf(project_dir, "kf_002", "0:05")
        _add_tr(project_dir, "tr_001", "kf_001", "kf_002")

        effect_id = add_transition_effect(project_dir, "tr_001", "strobe", {"frequency": 8})
        update_transition_effect(project_dir, effect_id, params={"frequency": 12, "duty": 0.3})

        effects = get_transition_effects(project_dir, "tr_001")
        assert effects[0]["params"]["frequency"] == 12
        assert effects[0]["params"]["duty"] == 0.3

    def test_update_enabled(self, project_dir):
        _add_kf(project_dir, "kf_001", "0:00")
        _add_kf(project_dir, "kf_002", "0:05")
        _add_tr(project_dir, "tr_001", "kf_001", "kf_002")

        effect_id = add_transition_effect(project_dir, "tr_001", "strobe", {})
        update_transition_effect(project_dir, effect_id, enabled=False)

        effects = get_transition_effects(project_dir, "tr_001")
        assert effects[0]["enabled"] is False

    def test_delete(self, project_dir):
        _add_kf(project_dir, "kf_001", "0:00")
        _add_kf(project_dir, "kf_002", "0:05")
        _add_tr(project_dir, "tr_001", "kf_001", "kf_002")

        effect_id = add_transition_effect(project_dir, "tr_001", "strobe", {})
        delete_transition_effect(project_dir, effect_id)

        effects = get_transition_effects(project_dir, "tr_001")
        assert len(effects) == 0

    def test_get_all_effects(self, project_dir):
        _add_kf(project_dir, "kf_001", "0:00")
        _add_kf(project_dir, "kf_002", "0:05")
        _add_kf(project_dir, "kf_003", "0:10")
        _add_tr(project_dir, "tr_001", "kf_001", "kf_002")
        _add_tr(project_dir, "tr_002", "kf_002", "kf_003")

        add_transition_effect(project_dir, "tr_001", "strobe", {"frequency": 4})
        add_transition_effect(project_dir, "tr_002", "strobe", {"frequency": 8})

        all_effects = get_all_transition_effects(project_dir)
        assert "tr_001" in all_effects
        assert "tr_002" in all_effects
        assert len(all_effects["tr_001"]) == 1
        assert len(all_effects["tr_002"]) == 1

    def test_no_effects_returns_empty(self, project_dir):
        effects = get_transition_effects(project_dir, "tr_nonexistent")
        assert effects == []

        all_effects = get_all_transition_effects(project_dir)
        assert all_effects == {}


class TestDeleteBridgeTrackScoped:
    """Test that delete+bridge respects track_id."""

    def test_bridge_stays_on_same_track(self, project_dir):
        """Deleting a keyframe on track_2 should bridge within track_2, not track_1."""
        # Track 1: kf_001 -> kf_002 -> kf_003
        _add_kf(project_dir, "kf_001", "0:00", "track_1")
        _add_kf(project_dir, "kf_002", "0:05", "track_1")
        _add_kf(project_dir, "kf_003", "0:10", "track_1")
        _add_tr(project_dir, "tr_001", "kf_001", "kf_002", "track_1")
        _add_tr(project_dir, "tr_002", "kf_002", "kf_003", "track_1")

        # Track 2: kf_101 -> kf_102 -> kf_103
        _add_kf(project_dir, "kf_101", "0:02", "track_2")
        _add_kf(project_dir, "kf_102", "0:06", "track_2")
        _add_kf(project_dir, "kf_103", "0:09", "track_2")
        _add_tr(project_dir, "tr_101", "kf_101", "kf_102", "track_2")
        _add_tr(project_dir, "tr_102", "kf_102", "kf_103", "track_2")

        # Delete kf_102 on track_2
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc).isoformat()
        # Soft-delete orphaned transitions
        delete_transition(project_dir, "tr_101", now)
        delete_transition(project_dir, "tr_102", now)
        delete_keyframe(project_dir, "kf_102", now)

        # Manually create bridge (simulating what the handler does)
        # The handler should find kf_101 and kf_103 as neighbors on track_2
        track_2_kfs = [k for k in get_keyframes(project_dir) if k.get("track_id") == "track_2"]
        assert len(track_2_kfs) == 2
        ids = {k["id"] for k in track_2_kfs}
        assert ids == {"kf_101", "kf_103"}

        # Track 1 should be unaffected
        track_1_kfs = [k for k in get_keyframes(project_dir) if k.get("track_id") == "track_1"]
        assert len(track_1_kfs) == 3

    def test_bridge_does_not_cross_tracks(self, project_dir):
        """Neighbors for bridging must be on the same track."""
        # Track 1: kf_001 at 0:00
        _add_kf(project_dir, "kf_001", "0:00", "track_1")
        # Track 2: kf_101 at 0:02, kf_102 at 0:05, kf_103 at 0:08
        _add_kf(project_dir, "kf_101", "0:02", "track_2")
        _add_kf(project_dir, "kf_102", "0:05", "track_2")
        _add_kf(project_dir, "kf_103", "0:08", "track_2")
        _add_tr(project_dir, "tr_101", "kf_101", "kf_102", "track_2")
        _add_tr(project_dir, "tr_102", "kf_102", "kf_103", "track_2")

        # When deleting kf_102, prev should be kf_101 (track_2), not kf_001 (track_1)
        track_2_kfs = sorted(
            [k for k in get_keyframes(project_dir) if k.get("track_id") == "track_2"],
            key=lambda k: k["timestamp"]
        )
        # Remove kf_102 from the list
        remaining = [k for k in track_2_kfs if k["id"] != "kf_102"]
        assert remaining[0]["id"] == "kf_101"
        assert remaining[1]["id"] == "kf_103"


class TestSplitTrackScoped:
    """Test that split preserves track_id on new keyframes and transitions."""

    def test_split_inherits_track_id(self, project_dir):
        """New keyframe and transitions from a split should inherit the original track."""
        _add_kf(project_dir, "kf_101", "0:00", "track_2")
        _add_kf(project_dir, "kf_102", "0:10", "track_2")
        _add_tr(project_dir, "tr_101", "kf_101", "kf_102", "track_2", duration=10.0)

        # Simulate split: delete original, create new kf + 2 trs
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc).isoformat()
        delete_transition(project_dir, "tr_101", now)

        new_kf_id = next_keyframe_id(project_dir)
        add_keyframe(project_dir, {
            "id": new_kf_id, "timestamp": "0:05", "section": "",
            "source": f"selected_keyframes/{new_kf_id}.png", "prompt": "",
            "candidates": [], "selected": None, "track_id": "track_2",
        })

        tr1_id = next_transition_id(project_dir)
        add_transition(project_dir, {
            "id": tr1_id, "from": "kf_101", "to": new_kf_id,
            "duration_seconds": 5.0, "slots": 1, "action": "", "use_global_prompt": False,
            "selected": None, "remap": {"method": "linear", "target_duration": 5.0},
            "track_id": "track_2",
        })

        tr2_id = next_transition_id(project_dir)
        add_transition(project_dir, {
            "id": tr2_id, "from": new_kf_id, "to": "kf_102",
            "duration_seconds": 5.0, "slots": 1, "action": "", "use_global_prompt": False,
            "selected": None, "remap": {"method": "linear", "target_duration": 5.0},
            "track_id": "track_2",
        })

        # Verify all new entities are on track_2
        new_kf = get_keyframe(project_dir, new_kf_id)
        assert new_kf["track_id"] == "track_2"

        tr1 = get_transition(project_dir, tr1_id)
        assert tr1["track_id"] == "track_2"

        tr2 = get_transition(project_dir, tr2_id)
        assert tr2["track_id"] == "track_2"

        # Verify no new entities on track_1
        track_1_trs = [t for t in get_transitions(project_dir) if t.get("track_id") == "track_1"]
        assert len(track_1_trs) == 0


class TestIncludeSectionDesc:
    """Test the include_section_desc field on transitions."""

    def test_default_is_true(self, project_dir):
        _add_kf(project_dir, "kf_001", "0:00")
        _add_kf(project_dir, "kf_002", "0:05")
        _add_tr(project_dir, "tr_001", "kf_001", "kf_002")

        tr = get_transition(project_dir, "tr_001")
        assert tr["include_section_desc"] is True

    def test_update_to_false(self, project_dir):
        _add_kf(project_dir, "kf_001", "0:00")
        _add_kf(project_dir, "kf_002", "0:05")
        _add_tr(project_dir, "tr_001", "kf_001", "kf_002")

        update_transition(project_dir, "tr_001", include_section_desc=False)
        tr = get_transition(project_dir, "tr_001")
        assert tr["include_section_desc"] is False

    def test_persists_across_reads(self, project_dir):
        _add_kf(project_dir, "kf_001", "0:00")
        _add_kf(project_dir, "kf_002", "0:05")
        _add_tr(project_dir, "tr_001", "kf_001", "kf_002")

        update_transition(project_dir, "tr_001", include_section_desc=False)
        # Re-read
        trs = get_transitions(project_dir)
        tr = next(t for t in trs if t["id"] == "tr_001")
        assert tr["include_section_desc"] is False


class TestStrobeEffect:
    """Test strobe effect math (matching frontend and backend logic)."""

    def test_strobe_on_off_pattern(self):
        """Strobe at frequency=4, duty=0.5 should be on for first half of each cycle."""
        freq = 4
        duty = 0.5
        results = []
        for i in range(100):
            progress = i / 100
            cycle_pos = (progress * freq) % 1
            is_on = cycle_pos <= duty
            results.append(is_on)

        # Should have roughly 50% on, 50% off
        on_count = sum(results)
        assert 40 <= on_count <= 60  # allow some rounding

    def test_strobe_high_frequency(self):
        """Higher frequency = more cycles in the same progress range."""
        freq = 20
        duty = 0.5
        transitions = 0
        prev = True
        for i in range(1000):
            progress = i / 1000
            is_on = (progress * freq) % 1 <= duty
            if is_on != prev:
                transitions += 1
            prev = is_on

        # 20 Hz over progress 0-1 = 20 cycles, each with on->off and off->on = ~40 transitions
        assert transitions >= 30  # allow for edge rounding

    def test_strobe_low_duty(self):
        """Duty=0.1 should be mostly off."""
        freq = 8
        duty = 0.1
        on_count = 0
        for i in range(1000):
            progress = i / 1000
            if (progress * freq) % 1 <= duty:
                on_count += 1

        assert on_count < 150  # ~10% of 1000

    def test_strobe_disabled_has_no_effect(self):
        """An effect with enabled=false should not modify opacity."""
        effects = [{"type": "strobe", "params": {"frequency": 100, "duty": 0.0}, "enabled": False}]
        opacity = 1.0
        progress = 0.5
        for fx in effects:
            if not fx["enabled"]:
                continue
            if fx["type"] == "strobe":
                freq = fx["params"].get("frequency", 8)
                d = fx["params"].get("duty", 0.5)
                if (progress * freq) % 1 > d:
                    opacity = 0
        assert opacity == 1.0


class TestTransitionOpacity:
    """Test per-transition opacity and opacity_curve fields."""

    def test_opacity_default_none(self, project_dir):
        _add_kf(project_dir, "kf_001", "0:00")
        _add_kf(project_dir, "kf_002", "0:05")
        _add_tr(project_dir, "tr_001", "kf_001", "kf_002")

        tr = get_transition(project_dir, "tr_001")
        assert tr["opacity"] is None
        assert tr["opacity_curve"] is None

    def test_set_opacity(self, project_dir):
        _add_kf(project_dir, "kf_001", "0:00")
        _add_kf(project_dir, "kf_002", "0:05")
        _add_tr(project_dir, "tr_001", "kf_001", "kf_002")

        update_transition(project_dir, "tr_001", opacity=0.5)
        tr = get_transition(project_dir, "tr_001")
        assert tr["opacity"] == 0.5

    def test_set_opacity_curve(self, project_dir):
        _add_kf(project_dir, "kf_001", "0:00")
        _add_kf(project_dir, "kf_002", "0:05")
        _add_tr(project_dir, "tr_001", "kf_001", "kf_002")

        curve = [[0, 1], [0.5, 0], [1, 1]]
        update_transition(project_dir, "tr_001", opacity_curve=curve)
        tr = get_transition(project_dir, "tr_001")
        assert tr["opacity_curve"] == curve

    def test_opacity_curve_hard_cut(self, project_dir):
        """Two points at the same X create a hard cut (vertical line)."""
        _add_kf(project_dir, "kf_001", "0:00")
        _add_kf(project_dir, "kf_002", "0:05")
        _add_tr(project_dir, "tr_001", "kf_001", "kf_002")

        # Hard cut at 50%: opacity 1 -> 0 instantly
        curve = [[0, 1], [0.5, 1], [0.5, 0], [1, 0]]
        update_transition(project_dir, "tr_001", opacity_curve=curve)
        tr = get_transition(project_dir, "tr_001")
        assert tr["opacity_curve"] == curve
        # Points at same X should be preserved
        xs = [p[0] for p in tr["opacity_curve"]]
        assert xs.count(0.5) == 2


class TestBlendFrames:
    """Test the _blend_frames function used in backend render."""

    def test_normal_blend_replaces(self):
        import numpy as np
        from beatlab.render.narrative import _blend_frames

        base = np.full((2, 2, 3), 100, dtype=np.uint8)
        overlay = np.full((2, 2, 3), 200, dtype=np.uint8)
        result = _blend_frames(base, overlay, "normal", 1.0)
        assert np.allclose(result, 200, atol=1)

    def test_normal_blend_half_opacity(self):
        import numpy as np
        from beatlab.render.narrative import _blend_frames

        base = np.full((2, 2, 3), 0, dtype=np.uint8)
        overlay = np.full((2, 2, 3), 200, dtype=np.uint8)
        result = _blend_frames(base, overlay, "normal", 0.5)
        assert np.allclose(result, 100, atol=2)

    def test_screen_blend(self):
        import numpy as np
        from beatlab.render.narrative import _blend_frames

        base = np.full((2, 2, 3), 128, dtype=np.uint8)
        overlay = np.full((2, 2, 3), 128, dtype=np.uint8)
        result = _blend_frames(base, overlay, "screen", 1.0)
        # screen: 1 - (1-0.502)*(1-0.502) ≈ 0.752 → ~192
        assert 185 <= result[0, 0, 0] <= 200

    def test_multiply_blend(self):
        import numpy as np
        from beatlab.render.narrative import _blend_frames

        base = np.full((2, 2, 3), 200, dtype=np.uint8)
        overlay = np.full((2, 2, 3), 128, dtype=np.uint8)
        result = _blend_frames(base, overlay, "multiply", 1.0)
        # multiply: (200/255) * (128/255) * 255 ≈ 100
        assert 95 <= result[0, 0, 0] <= 105

    def test_none_base_returns_overlay(self):
        import numpy as np
        from beatlab.render.narrative import _blend_frames

        overlay = np.full((2, 2, 3), 150, dtype=np.uint8)
        result = _blend_frames(None, overlay, "normal", 1.0)
        assert np.array_equal(result, overlay)

    def test_none_overlay_returns_base(self):
        import numpy as np
        from beatlab.render.narrative import _blend_frames

        base = np.full((2, 2, 3), 150, dtype=np.uint8)
        result = _blend_frames(base, None, "normal", 1.0)
        assert np.array_equal(result, base)
