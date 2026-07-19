import tempfile
import unittest
from pathlib import Path

from mra.difficulty import rhythm_svg_path
from mra.simai_parser import parse_maidata
from mra.sweep_marks import apply_sweep_maidata, strip_sweep_markers
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

    def initialize(self, song: Path):
        return ensure_sweep_maidata_for_song(song, self.parsed_song(song))

    def chart_events(self, song: Path, difficulty: int):
        chart = self.parsed_song(song).charts[difficulty]
        return compute_rhythm_events(chart)

    def test_first_run_seeds_machine_markers_and_never_overwrites(self):
        with tempfile.TemporaryDirectory() as tmp:
            song = self.make_song(Path(tmp))
            source = song / 'maidata.txt'
            source.write_bytes(source.read_bytes().replace(b'\n', b'\r\n'))

            marker_path, created = self.initialize(song)
            self.assertTrue(created)
            marker_content = marker_path.read_text(encoding='utf-8')
            self.assertIn('{24}8/S,1,2', marker_content)
            self.assertNotIn('{16}4/S,6,8', marker_content)
            self.assertEqual(
                strip_sweep_markers(marker_content),
                source.read_text(encoding='utf-8'),
            )

            marker_path.write_text(
                marker_content.replace('8/S,1,2', '8,1,2'),
                encoding='utf-8',
            )
            edited = marker_path.read_bytes()
            same_path, created_again = self.initialize(song)
            self.assertFalse(created_again)
            self.assertEqual(same_path.read_bytes(), edited)

    def test_marker_presence_is_authoritative_for_add_and_remove(self):
        with tempfile.TemporaryDirectory() as tmp:
            song = self.make_song(Path(tmp))
            marker_path, _ = self.initialize(song)
            marked = marker_path.read_text(encoding='utf-8')
            marked = marked.replace('{24}8/S,1,2', '{24}8,1,2')
            marked = marked.replace('{16}4,6,8', '{16}4/S,6,8')
            marker_path.write_text(marked, encoding='utf-8')

            master_events = self.chart_events(song, 5)
            master_result = apply_sweep_maidata(master_events, song, 5)
            remaster_events = self.chart_events(song, 6)
            remaster_result = apply_sweep_maidata(remaster_events, song, 6)

            self.assertFalse(master_events[0]['is_sweep_start'])
            self.assertTrue(remaster_events[0]['is_sweep_start'])
            self.assertFalse(master_result.stale)
            self.assertFalse(remaster_result.stale)

    def test_bpm_change_and_double_head_are_seeded_at_the_existing_event(self):
        content = MAIDATA.replace(
            '(120){24}8,1,2,E',
            '(120){16}1,2,(240){24}8/4,7,6,E',
        )
        with tempfile.TemporaryDirectory() as tmp:
            song = self.make_song(Path(tmp), content)
            marker_path, _ = self.initialize(song)
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
            marker_path, _ = self.initialize(song)
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
            marker_path, _ = self.initialize(song)
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
            marker_path, _ = self.initialize(song)
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
            marker_path, _ = self.initialize(song)
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
            marker_path, _ = self.initialize(song)
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
