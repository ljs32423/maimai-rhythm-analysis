import json
import tempfile
import unittest
from pathlib import Path

from mra.meter import MeterMap, TimeSignature, ensure_meter_file
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

    def test_missing_meter_file_is_initialized_as_editable_4_4_template(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            meter_map = ensure_meter_file(root, 5, 24)
            generated = json.loads(
                (root / "outputs" / "MASTER" / "meter" / "meter.json")
                .read_text(encoding="utf-8")
            )

        self.assertEqual(meter_map.measures[0].signature.label, "4/4")
        self.assertEqual(generated["sections"], [{
            "start_beat": 0.0,
            "signature": "4/4",
            "confidence": 1.0,
            "source": "template",
        }])

    def test_existing_meter_file_is_never_modified_when_initializer_runs_again(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            output = root / "outputs" / "MASTER" / "meter" / "meter.json"
            output.parent.mkdir(parents=True)
            original = (
                '{\n  "version": 2,\n  "default": "3/4",\n'
                '  "sections": [{"start_beat": 0, "signature": "3/4"}],\n'
                '  "editor_note": "keep exactly"\n}\n'
            )
            output.write_text(original, encoding="utf-8")

            meter_map = ensure_meter_file(root, 5, 24)

            self.assertEqual(output.read_text(encoding="utf-8"), original)
            self.assertEqual(meter_map.measures[0].signature.label, "3/4")

    def test_initializer_does_not_consult_song_level_meter_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "meter.json").write_text(json.dumps({
                "sections": [{"start_beat": 0, "signature": "3/4"}],
            }), encoding="utf-8")

            meter_map = ensure_meter_file(root, 5, 12)
            generated = json.loads(
                (root / "outputs" / "MASTER" / "meter" / "meter.json")
                .read_text(encoding="utf-8")
            )

        self.assertEqual(meter_map.measures[0].signature.label, "4/4")
        self.assertNotIn("measures", generated)
        self.assertEqual(generated["version"], 2)
        self.assertEqual(generated["sections"][0]["source"], "template")

    def test_meter_json_serializes_only_signature_changes(self):
        meter_map = MeterMap.from_dict({
            "sections": [
                {"start_beat": 0, "signature": "4/4", "source": "manual"},
                {"start_beat": 8, "signature": "3/4", "source": "manual"},
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
