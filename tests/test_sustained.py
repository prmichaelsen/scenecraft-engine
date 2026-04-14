"""Tests for sustained effects, hold keyframes, and ColorCorrector."""

from scenecraft.fusion.keyframes import KeyframeTrack
from scenecraft.fusion.nodes import make_color_corrector
from scenecraft.fusion.setting_writer import FusionComp
from scenecraft.ai.plan import EffectPlan, SectionPlan, parse_effect_plan
from scenecraft.generator import generate_comp


class TestHoldKeyframes:
    def test_add_hold_produces_4_keyframes(self):
        track = KeyframeTrack()
        track.add_hold(start_frame=0, end_frame=100, value=1.3, base_value=1.0)
        assert len(track.keyframes) == 4

    def test_add_hold_values(self):
        track = KeyframeTrack()
        track.add_hold(start_frame=0, end_frame=100, value=0.8, base_value=1.0, transition_frames=10)
        kfs = sorted(track.keyframes, key=lambda k: k.frame)
        # transition in: base at 0, target at 10
        assert kfs[0].value == 1.0
        assert kfs[0].frame == 0
        assert kfs[1].value == 0.8
        assert kfs[1].frame == 10
        # hold then transition out: target at 90, base at 100
        assert kfs[2].value == 0.8
        assert kfs[2].frame == 90
        assert kfs[3].value == 1.0
        assert kfs[3].frame == 100

    def test_add_hold_short_section_clamps_transition(self):
        track = KeyframeTrack()
        track.add_hold(start_frame=0, end_frame=10, value=1.5, base_value=1.0, transition_frames=30)
        # transition_frames should be clamped to 1/3 of section duration = 3
        kfs = sorted(track.keyframes, key=lambda k: k.frame)
        assert len(kfs) == 4
        assert kfs[1].frame == 3  # clamped transition

    def test_hold_serializes(self):
        track = KeyframeTrack()
        track.add_hold(start_frame=0, end_frame=90, value=1.2, base_value=1.0)
        entries = track.to_lua_entries()
        assert len(entries) == 4
        assert "[0]" in entries[0]


class TestColorCorrectorNode:
    def test_create_node(self):
        node = make_color_corrector(name="CC1", source_op="MediaIn1")
        assert node.tool_type == "ColorCorrector"
        assert node.inputs["Input"]["SourceOp"] == "MediaIn1"

    def test_serialize_with_static_params(self):
        node = make_color_corrector(name="CC1")
        node.inputs["MasterSaturation"] = 1.3
        lua = node.to_lua()
        assert "ColorCorrector" in lua
        assert "MasterSaturation" in lua
        assert "1.3" in lua

    def test_serialize_with_animated_params(self):
        comp = FusionComp()
        node = make_color_corrector(name="CC1")
        track = KeyframeTrack()
        track.add_hold(0, 100, 1.3, 1.0)
        node.animated["MasterSaturation"] = track

        track2 = KeyframeTrack()
        track2.add_hold(0, 100, -0.02, 0.0)
        node.animated["MasterLift"] = track2

        comp.add_node(node)
        output = comp.serialize()
        assert "CC1MasterSaturation = BezierSpline" in output
        assert "CC1MasterLift = BezierSpline" in output
        assert "ColorCorrector" in output


class TestPlanWithSustained:
    def test_parse_sustained_effects(self):
        import json
        text = json.dumps({
            "sections": [{
                "section_index": 0,
                "presets": ["zoom_pulse"],
                "sustained_effects": [{
                    "node_type": "ColorCorrector",
                    "parameters": {"MasterSaturation": 0.8, "MasterContrast": 0.2},
                    "transition_frames": 15,
                }],
            }]
        })
        plan = parse_effect_plan(text)
        assert len(plan.sections[0].sustained_effects) == 1
        assert plan.sections[0].sustained_effects[0]["parameters"]["MasterSaturation"] == 0.8

    def test_generate_with_sustained(self):
        beat_map = {
            "version": "1.2", "source_file": "test.mp3",
            "duration": 4.0, "tempo": 120.0, "fps": 30,
            "beats": [
                {"time": 0.5, "frame": 15, "intensity": 0.8},
                {"time": 1.0, "frame": 30, "intensity": 1.0},
            ],
            "sections": [
                {"start_time": 0.0, "end_time": 2.0, "start_frame": 0, "end_frame": 60,
                 "type": "low_energy", "label": "verse"},
                {"start_time": 2.0, "end_time": 4.0, "start_frame": 60, "end_frame": 120,
                 "type": "high_energy", "label": "chorus"},
            ],
        }
        plan = EffectPlan(sections=[
            SectionPlan(
                section_index=0,
                presets=["zoom_pulse"],
                sustained_effects=[{
                    "node_type": "ColorCorrector",
                    "parameters": {"MasterSaturation": 0.8, "MasterLift": -0.02},
                    "transition_frames": 10,
                }],
            ),
            SectionPlan(
                section_index=1,
                presets=["flash"],
                sustained_effects=[{
                    "node_type": "ColorCorrector",
                    "parameters": {"MasterSaturation": 1.3, "MasterContrast": 0.2},
                    "transition_frames": 10,
                }],
            ),
        ])
        comp = generate_comp(beat_map, effect_plan=plan)
        output = comp.serialize()
        # Should have pulse nodes + ColorCorrector with sustained keyframes
        assert "ColorCorrector" in output
        assert "BezierSpline" in output
        # Should have both pulse and sustained nodes
        assert "Transform" in output or "BrightnessContrast" in output


class TestPromptIncludesColor:
    def test_system_prompt_has_color_params(self):
        from scenecraft.ai.prompt import build_system_prompt
        prompt = build_system_prompt()
        assert "ColorCorrector" in prompt
        assert "MasterSaturation" in prompt
        assert "MasterHueAngle" in prompt
        assert "GainR" in prompt
        assert "LiftR" in prompt
        assert "sustained_effects" in prompt
