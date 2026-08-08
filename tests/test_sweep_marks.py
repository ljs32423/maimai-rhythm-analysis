import tempfile
import unittest
from pathlib import Path

from mra.difficulty import (legacy_sweep_maidata_path, rhythm_svg_path,
                            sweep_maidata_path)
from mra.meter import MeterMap
from mra.simai_parser import parse_maidata
from mra.sweep_marks import (apply_sweep_maidata, extract_sweep_difficulty,
                             reflow_sweep_maidata, strip_sweep_markers,
                             sweep_maidata_semantically_equal)
from mra.visualize import (BREAK_RING_COLOR, SWEEP_RING_COLOR, build_primitives,
                           compute_rhythm_events, ensure_sweep_maidata_for_song,
                           process_song)


MAIDATA = """&title=Sweep Test
&artist=Tester
&wholebpm=120
&first=0
&lv_5=13
&inote_5=(120){24}8,1,2,E
&lv_6=14
&inote_6=(120){16}4,6,8,E
"""


class SweepMarksTests(unittest.TestCase):
    def make_song(self, root: Path, content: str = MAIDATA) -> Path:
        song = root / 'song'
        song.mkdir()
        (song / 'maidata.txt').write_text(content, encoding='utf-8')
        return song

    def parsed_song(self, song: Path):
        return parse_maidata(str(song / 'maidata.txt'))

    def initialize(self, song: Path, difficulty: int):
        return ensure_sweep_maidata_for_song(
            song, self.parsed_song(song), difficulty,
        )

    def chart_events(self, song: Path, difficulty: int):
        chart = self.parsed_song(song).charts[difficulty]
        return compute_rhythm_events(chart)

    @staticmethod
    def meter(default: str, *sections: tuple[float, str]) -> MeterMap:
        return MeterMap.from_dict({
            'default': default,
            'sections': [
                {'start_beat': start, 'signature': signature}
                for start, signature in sections
            ],
        })

    def test_reflow_splits_coarse_steps_without_changing_chart_semantics(self):
        content = """&title=Reflow Test
&wholebpm=120
&lv_5=13
&inote_5=(120){2}1,2/S,3,E
"""
        meter = self.meter('3/4', (0, '3/4'))

        reflowed = reflow_sweep_maidata(content, 120, {5: meter})

        self.assertIn('2{#0.5}/S,\n{#0.5},{2}3,\nE', reflowed)
        self.assertTrue(sweep_maidata_semantically_equal(content, reflowed))
        self.assertEqual(reflowed.count('/S'), 1)
        self.assertEqual(
            reflow_sweep_maidata(reflowed, 120, {5: meter}),
            reflowed,
        )

    def test_reflow_uses_each_difficultys_meter_and_truncates_at_changes(self):
        content = """&title=Multi Meter
&wholebpm=120
&lv_5=13
&inote_5=(120){2}1,2,3,E
&lv_6=14
&inote_6=(120){2}4,5,6,E
"""
        maps = {
            5: self.meter('4/4', (0, '4/4'), (5, '3/4')),
            6: self.meter('3/4', (0, '3/4')),
        }

        reflowed = reflow_sweep_maidata(content, 120, maps)
        master, remaster = reflowed.split('&inote_6=')

        # MASTER: 0..4, 4..5 (truncated by the change), 5..E.
        self.assertIn('1,2,\n3{#0.5},\n{#0.5},\nE', master)
        # Re:MASTER: 0..3 and 3..6 under its independent 3/4 map.
        self.assertIn('4,5{#0.5},\n{#0.5},{2}6,\nE', remaster)
        self.assertTrue(sweep_maidata_semantically_equal(content, reflowed))

    def test_semantic_comparison_rejects_real_song_or_timing_changes(self):
        formatted = reflow_sweep_maidata(
            MAIDATA, 120, {5: self.meter('3/4', (0, '3/4'))},
        )

        self.assertTrue(sweep_maidata_semantically_equal(MAIDATA, formatted))
        self.assertTrue(sweep_maidata_semantically_equal(
            MAIDATA, formatted.replace('8,1,2', '8/S,1,2'),
        ))
        self.assertFalse(sweep_maidata_semantically_equal(
            MAIDATA, formatted.replace('Tester', 'Changed'),
        ))
        self.assertFalse(sweep_maidata_semantically_equal(
            MAIDATA, formatted.replace('8,1,2', '8,1,3'),
        ))
        self.assertFalse(sweep_maidata_semantically_equal(
            MAIDATA, formatted.replace('2,\nE', '2,,\nE'),
        ))
        self.assertFalse(sweep_maidata_semantically_equal(
            MAIDATA, formatted.replace('\nE\n', '\nX\n', 1),
        ))
        self.assertFalse(sweep_maidata_semantically_equal(
            MAIDATA, formatted.replace('8,1,2', '8,1foo,2'),
        ))
        self.assertFalse(sweep_maidata_semantically_equal(
            MAIDATA, formatted.replace('8,1,2', '8,1/S?,2'),
        ))

    def test_scientific_notation_bpm_is_not_mistaken_for_end_marker(self):
        content = """&title=Scientific BPM
&wholebpm=120
&lv_5=13
&inote_5=(120){4}1,2,3,4,(1E+2)5,6,E
"""
        meter = self.meter('7/8', (0, '7/8'))

        reflowed = reflow_sweep_maidata(content, 120, {5: meter})

        self.assertIn('{#0.25},{4}(1E+2)5,6,\nE', reflowed)
        self.assertTrue(sweep_maidata_semantically_equal(content, reflowed))

    def test_reflow_preserves_explicit_same_signature_measure_anchor(self):
        content = """&title=Reset Anchor
&wholebpm=120
&lv_5=13
&inote_5=(120){4}1,2,3,4,5,6,7,8,E
"""
        meter = MeterMap.from_dict({
            'default': '4/4',
            'sections': [
                {'start_beat': 0, 'signature': '4/4'},
                {'start_beat': 7, 'signature': '4/4'},
            ],
        })

        reflowed = reflow_sweep_maidata(content, 120, {5: meter})

        self.assertIn('1,2,3,4,\n5,6,7,\n8,\nE', reflowed)
        self.assertTrue(sweep_maidata_semantically_equal(content, reflowed))

    def test_meter_reflow_alone_does_not_make_manual_file_stale(self):
        with tempfile.TemporaryDirectory() as tmp:
            song = self.make_song(Path(tmp))
            marker_path, _ = self.initialize(song, 5)
            marker_content = marker_path.read_text(encoding='utf-8')
            meter = self.meter('3/4', (0, '3/4'))
            marker_path.write_text(
                reflow_sweep_maidata(marker_content, 120, {5: meter}),
                encoding='utf-8',
            )

            result = apply_sweep_maidata(self.chart_events(song, 5), song, 5)

            self.assertFalse(result.stale)
            self.assertFalse(any('不一致' in warning for warning in result.warnings))

    def test_first_run_seeds_machine_markers_and_never_overwrites(self):
        with tempfile.TemporaryDirectory() as tmp:
            song = self.make_song(Path(tmp))
            source = song / 'maidata.txt'
            source.write_bytes(source.read_bytes().replace(b'\n', b'\r\n'))

            marker_path, created = self.initialize(song, 5)
            self.assertTrue(created)
            marker_content = marker_path.read_text(encoding='utf-8')
            self.assertIn('{24}8/S,1,2', marker_content)
            self.assertNotIn('&inote_6=', marker_content)
            self.assertEqual(marker_path, sweep_maidata_path(song, 5))
            self.assertFalse(sweep_maidata_path(song, 6).exists())
            self.assertFalse(legacy_sweep_maidata_path(song).exists())
            self.assertEqual(
                strip_sweep_markers(marker_content),
                extract_sweep_difficulty(
                    source.read_text(encoding='utf-8'), 5,
                ),
            )

            marker_path.write_text(
                marker_content.replace('8/S,1,2', '8,1,2'),
                encoding='utf-8',
            )
            edited = marker_path.read_bytes()
            same_path, created_again = self.initialize(song, 5)
            self.assertFalse(created_again)
            self.assertEqual(same_path.read_bytes(), edited)

    def test_legacy_aggregate_is_losslessly_migrated_per_difficulty(self):
        with tempfile.TemporaryDirectory() as tmp:
            song = self.make_song(Path(tmp))
            legacy_content = MAIDATA.replace(
                '{24}8,1,2', '{24}8/S,1,2',
            ).replace(
                '{16}4,6,8', '{16}4/S,6,8',
            ).replace('\n', '\r\n')
            legacy_path = legacy_sweep_maidata_path(song)
            legacy_path.write_bytes(legacy_content.encode('utf-8'))
            legacy_before = legacy_path.read_bytes()

            master_path, master_created = self.initialize(song, 5)
            remaster_path, remaster_created = self.initialize(song, 6)

            master_expected = """&title=Sweep Test
&artist=Tester
&wholebpm=120
&first=0
&lv_5=13
&inote_5=(120){24}8/S,1,2,E
""".replace('\n', '\r\n').encode('utf-8')
            remaster_expected = """&title=Sweep Test
&artist=Tester
&wholebpm=120
&first=0
&lv_6=14
&inote_6=(120){16}4/S,6,8,E
""".replace('\n', '\r\n').encode('utf-8')
            self.assertTrue(master_created)
            self.assertTrue(remaster_created)
            self.assertEqual(master_path.read_bytes(), master_expected)
            self.assertEqual(remaster_path.read_bytes(), remaster_expected)
            self.assertEqual(legacy_path.read_bytes(), legacy_before)

            master_events = self.chart_events(song, 5)
            remaster_events = self.chart_events(song, 6)
            apply_sweep_maidata(master_events, song, 5)
            apply_sweep_maidata(remaster_events, song, 6)
            self.assertTrue(master_events[0]['is_sweep_start'])
            self.assertTrue(remaster_events[0]['is_sweep_start'])
            self.assertEqual(legacy_path.read_bytes(), legacy_before)

    def test_unprojectable_legacy_target_never_falls_back_to_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            song = self.make_song(Path(tmp))
            legacy_path = legacy_sweep_maidata_path(song)
            legacy_path.write_text(
                MAIDATA.replace(
                    '&inote_5=(120){24}8,1,2,E',
                    ' &inote_5=(120){24}8/S,1,2,E',
                ),
                encoding='utf-8',
            )
            preserved = legacy_path.read_bytes()

            with self.assertRaisesRegex(ValueError, '存在，但无法安全投影'):
                self.initialize(song, 5)

            self.assertEqual(legacy_path.read_bytes(), preserved)
            self.assertFalse(sweep_maidata_path(song, 5).exists())

    def test_marker_presence_is_authoritative_for_add_and_remove(self):
        with tempfile.TemporaryDirectory() as tmp:
            song = self.make_song(Path(tmp))
            master_path, _ = self.initialize(song, 5)
            remaster_path, _ = self.initialize(song, 6)
            master_path.write_text(
                master_path.read_text(encoding='utf-8').replace(
                    '{24}8/S,1,2', '{24}8,1,2',
                ),
                encoding='utf-8',
            )
            remaster_path.write_text(
                remaster_path.read_text(encoding='utf-8').replace(
                    '{16}4,6,8', '{16}4/S,6,8',
                ),
                encoding='utf-8',
            )
            master_before = master_path.read_bytes()
            remaster_before = remaster_path.read_bytes()

            master_events = self.chart_events(song, 5)
            master_result = apply_sweep_maidata(master_events, song, 5)
            remaster_events = self.chart_events(song, 6)
            remaster_result = apply_sweep_maidata(remaster_events, song, 6)

            self.assertFalse(master_events[0]['is_sweep_start'])
            self.assertTrue(remaster_events[0]['is_sweep_start'])
            self.assertFalse(master_result.stale)
            self.assertFalse(remaster_result.stale)
            self.assertNotEqual(master_path, remaster_path)
            self.assertEqual(master_path.read_bytes(), master_before)
            self.assertEqual(remaster_path.read_bytes(), remaster_before)

    def test_bpm_change_and_double_head_are_seeded_at_the_existing_event(self):
        content = MAIDATA.replace(
            '(120){24}8,1,2,E',
            '(120){16}1,2,(240){24}8/4,7,6,E',
        )
        with tempfile.TemporaryDirectory() as tmp:
            song = self.make_song(Path(tmp), content)
            marker_path, _ = self.initialize(song, 5)
            self.assertIn('8/4/S,7,6', marker_path.read_text(encoding='utf-8'))

            events = self.chart_events(song, 5)
            result = apply_sweep_maidata(events, song, 5)

            marked = [event for event in events if event['is_sweep_start']]
            self.assertEqual(len(marked), 1)
            self.assertEqual(sorted(note.button for note in marked[0]['notes']), [4, 8])
            self.assertFalse(result.warnings)

    def test_duplicate_marker_and_empty_beat_warn_without_breaking(self):
        content = MAIDATA.replace('{24}8,1,2', '{16},,1,4,6')
        with tempfile.TemporaryDirectory() as tmp:
            song = self.make_song(Path(tmp), content)
            marker_path, _ = self.initialize(song, 5)
            marker_path.write_text(
                marker_path.read_text(encoding='utf-8').replace(
                    '{16},,1,4,6', '{16}/S,,1/S/S,4,6',
                ),
                encoding='utf-8',
            )
            events = self.chart_events(song, 5)
            result = apply_sweep_maidata(events, song, 5)

            self.assertTrue(events[0]['is_sweep_start'])
            self.assertTrue(any('没有对应音符事件' in warning for warning in result.warnings))
            self.assertTrue(any('重复 /S' in warning for warning in result.warnings))

    def test_malformed_marker_and_source_change_warn_without_overwrite(self):
        with tempfile.TemporaryDirectory() as tmp:
            song = self.make_song(Path(tmp))
            marker_path, _ = self.initialize(song, 5)
            marker_path.write_text(
                marker_path.read_text(encoding='utf-8').replace('8/S,1,2', '8/S?,1,2'),
                encoding='utf-8',
            )
            preserved = marker_path.read_bytes()
            (song / 'maidata.txt').write_text(MAIDATA.replace('Tester', 'Changed'), encoding='utf-8')
            events = self.chart_events(song, 5)
            result = apply_sweep_maidata(events, song, 5)

            self.assertTrue(result.stale)
            self.assertEqual(marker_path.read_bytes(), preserved)
            self.assertFalse(events[0]['is_sweep_start'])
            self.assertTrue(any('无效扫键标记' in warning for warning in result.warnings))
            self.assertTrue(any('不一致' in warning for warning in result.warnings))

    def test_markers_are_isolated_by_difficulty(self):
        with tempfile.TemporaryDirectory() as tmp:
            song = self.make_song(Path(tmp))
            marker_path, _ = self.initialize(song, 5)
            marker_path.write_text(
                marker_path.read_text(encoding='utf-8').replace('{24}8/S,1,2', '{24}8,1,2'),
                encoding='utf-8',
            )
            events = self.chart_events(song, 6)
            apply_sweep_maidata(events, song, 6)

            self.assertFalse(any(event['is_sweep_start'] for event in events))

    def test_manual_break_head_keeps_break_ring_priority(self):
        content = MAIDATA.replace('{24}8,1,2', '{16}1b,4,6')
        with tempfile.TemporaryDirectory() as tmp:
            song = self.make_song(Path(tmp), content)
            marker_path, _ = self.initialize(song, 5)
            marker_path.write_text(
                marker_path.read_text(encoding='utf-8').replace('1b,4,6', '1b/S,4,6'),
                encoding='utf-8',
            )
            chart = self.parsed_song(song).charts[5]
            events = compute_rhythm_events(chart)
            apply_sweep_maidata(events, song, 5)
            primitives, _ = build_primitives(events, 4, 2, 120, chart)
            rings = [primitive for primitive in primitives if primitive[0] == 'ring']

            self.assertTrue(events[0]['is_sweep_start'])
            self.assertEqual(rings[0][4], BREAK_RING_COLOR)
            self.assertNotEqual(rings[0][4], SWEEP_RING_COLOR)

    def test_visualize_force_preserves_manual_file_and_renders_marker(self):
        content = MAIDATA.replace('{24}8,1,2', '{16}1,4,6')
        with tempfile.TemporaryDirectory() as tmp:
            song = self.make_song(Path(tmp), content)
            marker_path, _ = self.initialize(song, 5)
            marker_path.write_text(
                marker_path.read_text(encoding='utf-8').replace('1,4,6', '1/S,4,6'),
                encoding='utf-8',
            )
            preserved = marker_path.read_bytes()

            result = process_song(str(song), 'song', force=True, difficulties=[5])

            self.assertFalse(result.get('errors'))
            self.assertEqual(marker_path.read_bytes(), preserved)
            self.assertIn(SWEEP_RING_COLOR, rhythm_svg_path(song, 5).read_text(encoding='utf-8'))


if __name__ == '__main__':
    unittest.main()
