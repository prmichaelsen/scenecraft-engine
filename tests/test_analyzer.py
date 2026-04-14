"""Tests for the audio analyzer and beat map modules."""

import pytest
from scenecraft.beat_map import time_to_frame


class TestTimeToFrame:
    def test_zero_time(self):
        assert time_to_frame(0.0, 30.0) == 0

    def test_one_second_at_30fps(self):
        assert time_to_frame(1.0, 30.0) == 30

    def test_one_second_at_24fps(self):
        assert time_to_frame(1.0, 24.0) == 24

    def test_fractional_rounds_nearest(self):
        # 0.5s at 30fps = frame 15
        assert time_to_frame(0.5, 30.0) == 15

    def test_29_97_fps(self):
        # 1s at 29.97 = frame 30 (rounds from 29.97)
        assert time_to_frame(1.0, 29.97) == 30

    def test_60fps(self):
        assert time_to_frame(2.0, 60.0) == 120
