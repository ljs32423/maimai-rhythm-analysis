import tempfile
import unittest
from pathlib import Path

from mra.simai_parser import (
    NoteType,
    parse_hold_duration,
    parse_inote,
    parse_level_text,
    parse_maidata,
    parse_slide_duration,
    time_to_beat,
)


class ParserTests(unittest.TestCase):
    def test_quarter_note_duration(self):
        self.assertAlmostEqual(parse_hold_duration("4:1", 120), 0.5)

    def test_duration_with_explicit_bpm(self):
        self.assertAlmostEqual(parse_hold_duration("240#8:1", 120), 0.125)

    def test_slide_uses_one_beat_default_wait(self):
        total, wait, travel = parse_slide_duration("4:1", 120)
        self.assertAlmostEqual(wait, 0.5)
        self.assertAlmostEqual(travel, 0.5)
        self.assertAlmostEqual(total, 1.0)

    def test_slide_supports_explicit_wait(self):
        total, wait, travel = parse_slide_duration("8:1##4:1", 120)
        self.assertAlmostEqual(wait, 0.25)
        self.assertAlmostEqual(travel, 0.5)
        self.assertAlmostEqual(total, 0.75)

    def test_time_to_beat_across_bpm_change(self):
        timeline = [(0.0, 120), (1.0, 240)]
        self.assertAlmostEqual(time_to_beat(1.5, timeline), 4.0)

    def test_comma_advances_by_division(self):
        notes, _, division = parse_inote("(120){4}1,2,E", 120)
        self.assertEqual(division, 4)
        self.assertEqual(len(notes), 2)
        self.assertAlmostEqual(notes[1].time_sec, 0.5)

    def test_free_comma_duration(self):
        notes, _, _ = parse_inote("{#0.125}1,2,E", 120)
        self.assertAlmostEqual(notes[1].time_sec, 0.125)

    def test_bpm_change_is_recorded(self):
        notes, timeline, _ = parse_inote("(120){4}1,(240)2,E", 120)
        self.assertEqual(timeline, [(0.0, 120.0), (0.5, 240.0)])
        self.assertAlmostEqual(notes[1].time_sec, 0.5)

    def test_notes_keep_equal_beat_spacing_across_bpm_change(self):
        notes, timeline, _ = parse_inote("(120){4}1,(240)2,3,E", 120)
        beats = [time_to_beat(note.time_sec, timeline) for note in notes]
        self.assertEqual(beats, [0.0, 1.0, 2.0])

    def test_touch_hold_and_firework(self):
        notes, _, _ = parse_inote("A1hf[4:1],E", 120)
        self.assertEqual(notes[0].note_type, NoteType.TOUCH_HOLD)
        self.assertEqual(notes[0].extra["sensor"], "A1")
        self.assertTrue(notes[0].is_firework)
        self.assertAlmostEqual(notes[0].duration_sec, 0.5)

    def test_center_touch_c1_consumes_optional_number(self):
        notes, _, _ = parse_inote("{16}C1,,5,E", 120)

        self.assertEqual(len(notes), 2)
        self.assertEqual(notes[0].note_type, NoteType.TOUCH)
        self.assertEqual(notes[0].extra["sensor"], "C")
        self.assertEqual(notes[1].note_type, NoteType.TAP)
        self.assertEqual(notes[1].button, 5)

    def test_break_ex_note(self):
        notes, _, _ = parse_inote("1bx,E", 120)
        self.assertEqual(notes[0].note_type, NoteType.BREAK)
        self.assertTrue(notes[0].is_ex)

    def test_sweep_markers_keep_group_timing(self):
        notes, _, _ = parse_inote("{24}1/S,2/S,E", 120)
        markers = [note for note in notes if note.note_type == NoteType.SWEEP_MARKER]

        self.assertEqual(len(markers), 2)
        self.assertAlmostEqual(markers[0].time_sec, 0.0)
        self.assertAlmostEqual(markers[1].time_sec, 1 / 12)

    def test_break_hold_keeps_break_flag_before_or_after_hold_marker(self):
        notes, _, _ = parse_inote("1bh[4:1],2hb[4:1],E", 120)

        self.assertEqual(notes[0].note_type, NoteType.HOLD)
        self.assertTrue(notes[0].is_break)
        self.assertEqual(notes[1].note_type, NoteType.HOLD)
        self.assertTrue(notes[1].is_break)

    def test_break_hold_star_slide_head_keeps_break_flag(self):
        notes, _, _ = parse_inote("1bh[4:1]-4[8:1],E", 120)
        head = notes[0]

        self.assertEqual(head.note_type, NoteType.HOLD)
        self.assertTrue(head.is_break)
        self.assertTrue(head.is_star)

    def test_connected_slide(self):
        notes, _, _ = parse_inote("1-4q7-2[4:1],E", 120)
        slide = next(note for note in notes if note.note_type == NoteType.SLIDE)
        self.assertTrue(slide.extra["connected"])
        self.assertEqual(slide.end_button, 2)
        self.assertEqual(len(slide.extra["path"]), 3)

    def test_same_origin_slides(self):
        notes, _, _ = parse_inote("1-4[4:1]*-6[8:1],E", 120)
        slides = [note for note in notes if note.note_type == NoteType.SLIDE]
        self.assertEqual([note.end_button for note in slides], [4, 6])

    def test_parse_maidata(self):
        content = """&title=Test Song
&artist=Tester
&wholebpm=120
&first=0.25
&genre=GAME
&version=TEST
&lv_5=12.5
&des_5=Chart Author
&inote_5=(120){4}1,2,E
"""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "maidata.txt"
            path.write_text(content, encoding="utf-8")
            song = parse_maidata(str(path))
        self.assertEqual(song.title, "Test Song")
        self.assertEqual(song.charts[5].designer, "Chart Author")
        self.assertEqual(len(song.charts[5].notes), 2)

    def test_parse_level_text_keeps_plus_levels(self):
        self.assertEqual(parse_level_text("10+"), "10+")
        self.assertEqual(parse_level_text("13.5"), "13.5")

    def test_parse_maidata_accepts_plus_level(self):
        content = """&title=Plus Song
&artist=Tester
&wholebpm=120
&lv_5=10+
&inote_5=(120){4}1,2,E
"""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "maidata.txt"
            path.write_text(content, encoding="utf-8")
            song = parse_maidata(str(path))
        self.assertEqual(song.charts[5].level, "10+")


if __name__ == "__main__":
    unittest.main()
