import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import numpy as np

import mra.meter as meter
from mra.meter import (MeterMap, TimeSignature, analyze_chart_meter,
                       beat_to_time)
from mra.simai_parser import Chart, Note
import mra.visualize as visualize


class MeterTests(unittest.TestCase):
    def test_time_signature_uses_quarter_note_beat_axis(self):
        self.assertEqual(TimeSignature.parse("4/4").measure_beats, 4.0)
        self.assertEqual(TimeSignature.parse("7/8").measure_beats, 3.5)

    def test_manual_sections_expand_variable_measure_boundaries(self):
        meter_map = MeterMap.from_dict({
            "default": "4/4",
            "sections": [
                {"start_beat": 0, "signature": "4/4"},
                {"start_beat": 8, "signature": "7/8"},
                {"start_beat": 15, "signature": "3/4"},
            ],
        }, total_beats=21)

        self.assertEqual(
            meter_map.boundaries(0, 21),
            [0.0, 4.0, 8.0, 11.5, 15.0, 18.0, 21.0],
        )

    def test_manual_change_point_can_reset_measure_anchor(self):
        meter_map = MeterMap.from_dict({
            "sections": [
                {"start_beat": 0, "signature": "4/4"},
                {"start_beat": 7, "signature": "3/4"},
            ],
        }, total_beats=13)
        self.assertEqual(meter_map.boundaries(0, 13), [0.0, 4.0, 7.0, 10.0, 13.0])

    def test_beat_to_time_handles_bpm_change(self):
        self.assertAlmostEqual(beat_to_time(4, [(0, 120), (1, 240)]), 1.5)

    def test_beatnet_plus_profile_is_cached_across_difficulties(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            audio = root / "track.mp3"
            cache = root / "outputs" / "_shared" / "meter" / "beatnet-plus.json"
            audio.write_bytes(b"audio")
            predicted = [
                {"time": 0.1, "beats_per_bar": 4, "confidence": 0.8},
                {"time": 8.1, "beats_per_bar": 3, "confidence": 0.7},
            ]
            meter._SESSION_BEATNET_PLUS_PROFILES.clear()
            with mock.patch.object(
                meter, "_run_beatnet_plus", return_value=(predicted, "bb90eb0"),
            ) as run:
                first, warning = meter._beatnet_plus_profile(audio, cache)
                second, second_warning = meter._beatnet_plus_profile(audio, cache)

        self.assertIsNone(warning)
        self.assertIsNone(second_warning)
        self.assertEqual(run.call_count, 1)
        self.assertEqual(first["sections"], predicted)
        self.assertEqual(second, first)

    def test_beatnet_plus_profile_maps_audio_changes_without_using_chart_notes(self):
        chart = Chart("13", "Tester", [], [(0.0, 120.0)])
        profile = {
            "sections": [
                {"time": 0.2, "beats_per_bar": 4, "confidence": 0.8},
                {"time": 8.0, "beats_per_bar": 3, "confidence": 0.7},
            ],
        }
        meter_map = meter._profile_to_meter_map(profile, chart, 24, 0.0)
        self.assertEqual(
            [(item["start_beat"], item["signature"])
             for item in meter_map.signature_sections()],
            [(0.0, "4/4"), (16.0, "3/4")],
        )

    def test_song_meter_json_overrides_auto_analysis(self):
        chart = Chart("13", "Tester", [Note("tap", 1, 0.0)], [(0.0, 120.0)])
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "meter.json").write_text(json.dumps({
                "sections": [{"start_beat": 0, "signature": "3/4"}],
            }), encoding="utf-8")

            meter_map = analyze_chart_meter(root, 5, chart, 12)
            generated = json.loads(
                (root / "outputs" / "MASTER" / "meter" / "meter.json")
                .read_text(encoding="utf-8")
            )

        self.assertEqual(meter_map.measures[0].signature.label, "3/4")
        self.assertNotIn("measures", generated)
        self.assertEqual(generated["version"], 2)
        self.assertTrue(all(item["source"] == "manual" for item in generated["sections"]))

    def test_meter_json_serializes_only_signature_changes(self):
        meter_map = MeterMap.from_dict({
            "sections": [
                {"start_beat": 0, "signature": "4/4", "source": "beatnet-plus"},
                {"start_beat": 8, "signature": "3/4", "source": "beatnet-plus"},
            ],
        }, total_beats=17)
        data = meter_map.to_dict(5)
        self.assertNotIn("measures", data)
        self.assertEqual(
            [(item["start_beat"], item["signature"]) for item in data["sections"]],
            [(0.0, "4/4"), (8.0, "3/4")],
        )

    def test_renderer_draws_half_beat_measure_boundary_and_change_label(self):
        meter_map = MeterMap.from_dict({
            "sections": [
                {"start_beat": 0, "signature": "4/4"},
                {"start_beat": 8, "signature": "7/8"},
            ],
        }, total_beats=15)
        chart = Chart("13", "Tester", [], [(0.0, 120.0)])

        primitives, _ = visualize.build_primitives(
            [], 16, 15, 120, chart, meter_map,
        )
        boundary_x = visualize.PAD_X + 11.5 * visualize.PX_PER_BEAT

        self.assertTrue(any(
            primitive[0] == "line" and abs(primitive[1] - boundary_x) < 1e-6
            and primitive[-1] == 2.0
            for primitive in primitives
        ))
        self.assertTrue(any(
            primitive[0] == "text" and primitive[3] == "7/8"
            for primitive in primitives
        ))


if __name__ == "__main__":
    unittest.main()
