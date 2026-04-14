"""Tests for keyframe selection."""

from scenecraft.render.keyframe_selector import select_keyframes


def _make_beat_map():
    return {
        "tempo": 120.0, "duration": 10.0, "fps": 30,
        "beats": [
            {"time": 0.5, "frame": 15, "intensity": 0.8},
            {"time": 1.0, "frame": 30, "intensity": 1.0},
            {"time": 1.5, "frame": 45, "intensity": 0.5},
            {"time": 2.0, "frame": 60, "intensity": 0.9},
            {"time": 3.0, "frame": 90, "intensity": 0.7},
        ],
        "sections": [
            {"start_time": 0.0, "end_time": 2.0, "type": "low_energy"},
            {"start_time": 2.0, "end_time": 5.0, "type": "high_energy"},
        ],
    }


class TestKeyframeSelection:
    def test_returns_keyframes(self):
        kfs = select_keyframes(_make_beat_map(), total_frames=150, fps=30)
        assert len(kfs) > 0

    def test_first_and_last_included(self):
        kfs = select_keyframes(_make_beat_map(), total_frames=150, fps=30)
        frames = [k["frame"] for k in kfs]
        assert 1 in frames
        assert 150 in frames

    def test_interval_keyframes(self):
        kfs = select_keyframes(_make_beat_map(), total_frames=150, fps=30, interval=10)
        interval_kfs = [k for k in kfs if k["type"] == "interval"]
        assert len(interval_kfs) > 0

    def test_beat_keyframes_included(self):
        kfs = select_keyframes(_make_beat_map(), total_frames=150, fps=30)
        beat_kfs = [k for k in kfs if k["type"] == "beat"]
        assert len(beat_kfs) > 0

    def test_beat_keyframes_have_higher_denoise(self):
        kfs = select_keyframes(
            _make_beat_map(), total_frames=150, fps=30,
            base_denoise=0.3, beat_denoise=0.6,
        )
        beat_kfs = [k for k in kfs if k["type"] == "beat"]
        interval_kfs = [k for k in kfs if k["type"] == "interval"]
        if beat_kfs and interval_kfs:
            assert max(k["denoise"] for k in beat_kfs) > min(k["denoise"] for k in interval_kfs)

    def test_section_boundaries_included(self):
        # Use a beat map where section boundary doesn't overlap with beats
        bm = _make_beat_map()
        bm["sections"] = [
            {"start_time": 0.0, "end_time": 2.5, "type": "low_energy"},
            {"start_time": 2.5, "end_time": 5.0, "type": "high_energy"},
        ]
        kfs = select_keyframes(bm, total_frames=150, fps=30, interval=20)
        # Section at t=2.5 → frame 75, should appear as section_boundary or be covered
        frames = [k["frame"] for k in kfs]
        assert 75 in frames

    def test_deduplication(self):
        kfs = select_keyframes(_make_beat_map(), total_frames=150, fps=30, min_gap=3)
        frames = [k["frame"] for k in kfs]
        for i in range(1, len(frames)):
            assert frames[i] - frames[i - 1] >= 3

    def test_sparse_selection(self):
        # Keyframes should be much fewer than total frames
        kfs = select_keyframes(_make_beat_map(), total_frames=300, fps=30, interval=12)
        assert len(kfs) < 300 * 0.2  # less than 20% of total

    def test_section_styles_applied(self):
        styles = {0: "dark moody", 1: "bright neon"}
        kfs = select_keyframes(
            _make_beat_map(), total_frames=150, fps=30,
            section_styles=styles,
        )
        # Frame 1 is in section 0
        first = next(k for k in kfs if k["frame"] == 1)
        assert first["prompt"] == "dark moody"

    def test_silent_beats_skipped(self):
        beat_map = _make_beat_map()
        beat_map["beats"].append({"time": 4.0, "frame": 120, "intensity": 0.0})
        kfs = select_keyframes(beat_map, total_frames=150, fps=30)
        # Frame 120 should not be a beat keyframe (intensity=0)
        beat_at_120 = [k for k in kfs if k["frame"] == 120 and k["type"] == "beat"]
        assert len(beat_at_120) == 0
