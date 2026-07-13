import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import mra.align_audio as align_audio
import mra.make_html as make_html
import numpy as np
import mra.render_preview as render_preview
import mra.run_all as run_all
import mra.visualize as visualize
from mra.difficulty import (analysis_html_path, difficulty_file_stem, difficulty_name,
                            find_preview_video, offset_file_path, preview_video_path,
                            preview_video_candidates, rhythm_png_path, rhythm_svg_path,
                            strip_svg_path)
from mra.simai_parser import Chart, Note, NoteType, parse_inote
from mra.song_library import SongFolder, discover_song_folders, safe_folder_name


MAIDATA_WITH_REMASTER = """&title=Test
&artist=Tester
&wholebpm=120
&first=0
&lv_6=13
&inote_6=(120){4}1,2,E
"""

MAIDATA_WITH_TOUCH = """&title=DX Song
&artist=Tester
&wholebpm=120
&first=0
&lv_5=13
&inote_5=(120){4}A1,2,E
"""


class DifficultyTests(unittest.TestCase):
    def test_remaster_keeps_display_label(self):
        self.assertEqual(difficulty_name(6), "Re:MASTER")

    def test_remaster_uses_safe_file_stem(self):
        self.assertEqual(difficulty_file_stem(6), "ReMASTER")
        self.assertNotIn(":", difficulty_file_stem(6))

    def test_remaster_preview_candidates_include_safe_name(self):
        candidates = preview_video_candidates(6)
        self.assertEqual(candidates[0], "outputs/ReMASTER/video/preview.mp4")
        self.assertIn("ReMASTER_preview.mp4", candidates)

    def test_remaster_preview_finder_accepts_punctuation_and_case(self):
        with tempfile.TemporaryDirectory() as tmp:
            actual = Path(tmp) / "re.master_PREVIEW.MP4"
            actual.touch()
            self.assertEqual(find_preview_video(tmp, 6), actual.name)


class WorkflowTests(unittest.TestCase):
    def test_song_folders_move_into_library_and_use_maidata_title(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            legacy = root / "11820"
            legacy.mkdir()
            (legacy / "maidata.txt").write_text(
                MAIDATA_WITH_REMASTER.replace("&title=Test", "&title=Xaleid◆scopiX"),
                encoding="utf-8",
            )

            first = discover_song_folders(root, "11820", project_root=root)
            expected = root / "songs" / "Xaleid◆scopiX"
            self.assertEqual([song.path for song in first], [expected])
            self.assertTrue((expected / "maidata.txt").exists())
            self.assertFalse(legacy.exists())

            second = discover_song_folders(root, "Xaleid◆scopiX", project_root=root)
            self.assertEqual([song.path for song in second], [expected])

    def test_song_folder_name_replaces_windows_invalid_characters(self):
        self.assertEqual(safe_folder_name('A:B/C?'), 'A：B／C？')

    def test_song_folder_name_appends_dx_suffix_when_touch_exists(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            legacy = root / "12345"
            legacy.mkdir()
            (legacy / "maidata.txt").write_text(MAIDATA_WITH_TOUCH, encoding="utf-8")

            songs = discover_song_folders(root, "12345", project_root=root)
            expected = root / "songs" / "DX Song [DX]"
            self.assertEqual([song.path for song in songs], [expected])
            self.assertTrue((expected / "maidata.txt").exists())

    def test_song_selector_prefers_directory_name_before_shared_title(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            library = root / "songs"
            original = library / "PANDORA PARADOXXX"
            variant = library / "PANDORA PARADOXXX [PANDORA PARADOXXX（改bpm版）]"
            original.mkdir(parents=True)
            variant.mkdir()
            data = MAIDATA_WITH_REMASTER.replace("&title=Test", "&title=PANDORA PARADOXXX")
            (original / "maidata.txt").write_text(data, encoding="utf-8")
            (variant / "maidata.txt").write_text(data, encoding="utf-8")

            songs = discover_song_folders(root, "PANDORA PARADOXXX", project_root=root)
            self.assertEqual([song.path for song in songs], [original])

            variant_songs = discover_song_folders(
                root, "PANDORA PARADOXXX [PANDORA PARADOXXX（改bpm版）]",
                project_root=root,
            )
            self.assertEqual([song.path for song in variant_songs], [variant])

    def test_preview_recording_defaults_match_requested_view_settings(self):
        response = mock.MagicMock()
        response.status = 200
        response.__enter__.return_value = response
        with mock.patch.object(render_preview.urllib.request, "urlopen", return_value=response) as urlopen:
            render_preview.post_record(Path("C:/song/majdata.json"))

        payload = __import__('json').loads(urlopen.call_args.args[0].data)
        self.assertEqual(payload['noteSpeed'], 7.5)
        self.assertEqual(payload['touchSpeed'], 7.5)
        self.assertEqual(payload['backgroundCover'], 0.5)

    def test_recording_disables_pv_playback_by_default(self):
        self.assertFalse(render_preview.ENABLE_PV_PLAYBACK)

    def test_audio_alignment_uses_higher_sample_rate(self):
        self.assertEqual(align_audio.ALIGN_SAMPLE_RATE, 22050)

    def test_default_majdata_home_prefers_sibling_required_programs(self):
        with tempfile.TemporaryDirectory() as tmp:
            app_root = Path(tmp) / "app"
            sibling_tools = Path(tmp) / "required-programs" / ".tools" / "majdataviewx" / render_preview.MAJDATA_VERSION
            local_tools = app_root / ".tools" / "majdataviewx" / render_preview.MAJDATA_VERSION
            sibling_tools.mkdir(parents=True)
            local_tools.mkdir(parents=True)
            (sibling_tools / "MajdataView.exe").write_bytes(b"x")
            (local_tools / "MajdataView.exe").write_bytes(b"x")

            with mock.patch.object(render_preview, "ROOT", app_root), \
                 mock.patch.object(render_preview, "LOCAL_TOOLS_ROOT", app_root / ".tools"), \
                 mock.patch.object(render_preview, "SIBLING_TOOLS_ROOT", Path(tmp) / "required-programs" / ".tools"):
                self.assertEqual(render_preview.default_majdata_home(), sibling_tools)

    def test_preview_recording_uses_2k_at_60_fps(self):
        self.assertEqual(render_preview.render_window_size(), (2560, 1440))
        arguments = render_preview.RECORDER_FFMPEG_ARGUMENTS
        self.assertIn('-s "{0}x{1}"', arguments)
        self.assertIn('-r 60', arguments)
        self.assertIn('-preset ultrafast', arguments)
        self.assertIn('-fps_mode cfr', arguments)

    def test_recording_skips_pv_preparation_when_disabled(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            song = root / "song"
            work = root / "work"
            song.mkdir()
            work.mkdir()
            (song / "pv.mp4").write_bytes(b"source")

            with mock.patch.object(render_preview.subprocess, "run") as run:
                output = render_preview.prepare_recording_assets("ffmpeg", song, work)

            self.assertIsNone(output)
            run.assert_not_called()
            self.assertFalse((work / "pv.mp4").exists())

    def test_recording_pv_is_normalized_without_overwriting_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            song = root / "song"
            work = root / "work"
            song.mkdir()
            work.mkdir()
            source = song / "pv.mp4"
            source.write_bytes(b"source")

            with mock.patch.object(render_preview.subprocess, "run") as run, \
                 mock.patch.object(render_preview, "ENABLE_PV_PLAYBACK", True):
                output = render_preview.prepare_recording_assets("ffmpeg", song, work)

            self.assertEqual(output, work / "pv.mp4")
            self.assertEqual(source.read_bytes(), b"source")
            command = run.call_args.args[0]
            self.assertIn("baseline", command)
            self.assertIn("setpts=PTS-STARTPTS,fps=60", command)
            self.assertEqual(run.call_args.kwargs["cwd"], work)
            self.assertTrue(run.call_args.kwargs["check"])

    def test_recording_closes_majdata_explorer_window_after_success(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "Majdata"
            song = root / "song"
            home.mkdir()
            song.mkdir()
            viewer = home / "MajdataView.exe"
            viewer.write_bytes(b"")
            output = preview_video_path(song, 5)

            process = mock.Mock()
            process.pid = 1234
            process.poll.side_effect = [None]
            fake_response = mock.MagicMock()
            fake_response.status = 200
            fake_response.__enter__.return_value = fake_response

            with mock.patch.object(render_preview, "find_executable", side_effect=["ffprobe", "ffmpeg"]), \
                 mock.patch.object(render_preview, "configure_recorder"), \
                 mock.patch.object(render_preview, "convert_maidata", return_value=song / "majdata.json"), \
                 mock.patch.object(render_preview, "prepare_audio"), \
                 mock.patch.object(render_preview, "prepare_recording_assets", return_value=None), \
                 mock.patch.object(render_preview, "render_window_size", return_value=(2560, 1440)), \
                 mock.patch.object(render_preview.subprocess, "Popen", return_value=process), \
                 mock.patch.object(render_preview, "http_ready", return_value=True), \
                 mock.patch.object(render_preview, "post_record"), \
                 mock.patch.object(render_preview, "video_is_complete", side_effect=[True]), \
                 mock.patch.object(render_preview, "video_has_picture", return_value=True), \
                 mock.patch.object(render_preview, "crop_recorded_preview",
                                   side_effect=lambda _ffmpeg, src, dst: Path(dst).write_bytes(Path(src).read_bytes())), \
                 mock.patch.object(render_preview, "close_explorer_window_for_path") as close_window, \
                 mock.patch.object(render_preview, "stop_process_tree"), \
                 mock.patch.object(render_preview.urllib.request, "urlopen", return_value=fake_response), \
                 mock.patch.object(render_preview.tempfile, "TemporaryDirectory") as tempdir:
                work = root / "work"
                work.mkdir()
                raw = work / "out.mp4"
                raw.write_bytes(b"video")
                tempdir.return_value.__enter__.return_value = str(work)
                tempdir.return_value.__exit__.return_value = False
                result = render_preview.record_preview(home, song, 5, force=True, timeout=1)

            self.assertEqual(result, output)
            self.assertEqual(output.read_bytes(), b"video")
            close_window.assert_called_once_with(work)
            self.assertTrue(output.exists())

    def test_key_sounds_are_enabled_and_follow_majdata_note_types(self):
        self.assertTrue(render_preview.ENABLE_KEY_SOUNDS)
        chart = {
            "timingList": [{
                "time": 1.0,
                "noteList": [
                    {"noteType": 0, "isBreak": False, "isEx": False},
                    {"noteType": 3, "isHanabi": True},
                    {"noteType": 1, "slideStartTime": 1.5,
                     "slideTime": 0.5, "isSlideBreak": True,
                     "isSlideNoHead": False, "isBreak": False, "isEx": False},
                ],
            }],
        }
        events, touch_holds = render_preview.build_key_sound_events(chart)
        self.assertEqual(touch_holds, [])
        self.assertTrue({"answer", "judge", "touch", "hanabi"} <= events[1.0])
        self.assertIn("break_slide_start", events[1.5])
        self.assertEqual(events[2.0], {"break_slide", "judge_break_slide"})

    def test_key_sound_mixer_adds_lead_in_and_note_audio(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "Majdata"
            sfx = home / "SFX"
            sfx.mkdir(parents=True)
            rate = 100
            tone = render_preview.np.full((10, 2), 10000, dtype=render_preview.np.int16)
            for name in ("track_start", "answer", "judge"):
                render_preview.wavfile.write(sfx / f"{name}.wav", rate, tone)

            output = root / "out.wav"
            silence = render_preview.np.zeros((600, 2), dtype=render_preview.np.int16)
            render_preview.wavfile.write(output, rate, silence)
            chart_json = root / "majdata.json"
            chart_json.write_text(
                '{"timingList":[{"time":0,"noteList":['
                '{"noteType":0,"isBreak":false,"isEx":false}]}]}',
                encoding="utf-8",
            )

            render_preview.mix_key_sounds(home, chart_json, output)
            _, mixed = render_preview.wavfile.read(output)
            self.assertGreater(abs(int(mixed[0, 0])), 0)
            self.assertGreater(abs(int(mixed[500, 0])), 0)

    def test_long_svgs_have_trailing_measures_but_folded_png_does_not(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "maidata.txt").write_text(MAIDATA_WITH_REMASTER, encoding="utf-8")

            with mock.patch.object(visualize, "render_strip_svg") as svg_render, \
                 mock.patch.object(visualize, "render_strip_png") as png_render:
                visualize.process_song(str(root), "test")

            self.assertEqual(svg_render.call_count, 2)
            svg_total_beats = {call.args[1] for call in svg_render.call_args_list}
            self.assertEqual(len(svg_total_beats), 1)
            png_total_beats = png_render.call_args.args[1]
            self.assertEqual(
                svg_total_beats.pop() - png_total_beats,
                visualize.LONG_IMAGE_EXTRA_MEASURES * visualize.BEATS_PER_MEASURE,
            )

    def test_rhythm_shape_uses_nearest_neighbor_not_displayed_next_gap(self):
        chart = Chart(
            level=12,
            designer="Tester",
            notes=[
                Note(NoteType.TAP, 1, 0.0),
                Note(NoteType.TAP, 2, 0.125),
                Note(NoteType.TAP, 3, 0.875),
            ],
            bpm_timeline=[(0.0, 120.0)],
        )
        events = visualize.compute_rhythm_events(chart)
        middle = events[1]
        self.assertEqual(middle['nv_label'], '4.')
        self.assertEqual(middle['style_label'], '16')

        primitives, _ = visualize.build_primitives(events, 4, 4, 120, chart)
        middle_x = visualize.PAD_X + middle['beat'] * visualize.PX_PER_BEAT
        self.assertTrue(any(p[0] == 'circle' and p[1] == middle_x for p in primitives))
        self.assertFalse(any(p[0] == 'diamond' and p[1] == middle_x for p in primitives))

    def test_rhythm_shape_can_use_dotted_previous_gap(self):
        chart = Chart(
            level=12,
            designer="Tester",
            notes=[
                Note(NoteType.TAP, 1, 0.0),
                Note(NoteType.TAP, 2, 0.75),
                Note(NoteType.TAP, 3, 1.75),
            ],
            bpm_timeline=[(0.0, 120.0)],
        )
        events = visualize.compute_rhythm_events(chart)
        middle = events[1]
        self.assertEqual(middle['nv_label'], '2')
        self.assertEqual(middle['style_label'], '4.')

        primitives, _ = visualize.build_primitives(events, 4, 4, 120, chart)
        middle_x = visualize.PAD_X + middle['beat'] * visualize.PX_PER_BEAT
        self.assertTrue(any(p[0] == 'diamond' and p[1] == middle_x for p in primitives))

    def test_rhythm_infers_quintuplet_from_fine_grained_approximation_run(self):
        deltas = [19 / 96, 19 / 96, 19 / 96, 20 / 96]
        beat_positions = [0.0]
        for delta in deltas:
            beat_positions.append(beat_positions[-1] + delta)
        chart = Chart(
            level=12,
            designer="Tester",
            notes=[Note(NoteType.TAP, idx + 1, beat * 0.5) for idx, beat in enumerate(beat_positions)],
            bpm_timeline=[(0.0, 120.0)],
        )
        events = visualize.compute_rhythm_events(chart)
        self.assertEqual([event['nv_label'] for event in events[:-1]], ['20', '20', '20', '20'])
        self.assertEqual([event['style_label'] for event in events], ['20', '20', '20', '20', '20'])

    def test_rhythm_infers_septuplet_from_fine_grained_approximation_run(self):
        deltas = [53 / 384, 56 / 384, 54 / 384, 56 / 384, 54 / 384, 55 / 384]
        beat_positions = [0.0]
        for delta in deltas:
            beat_positions.append(beat_positions[-1] + delta)
        chart = Chart(
            level=12,
            designer="Tester",
            notes=[Note(NoteType.TAP, (idx % 8) + 1, beat * 0.5) for idx, beat in enumerate(beat_positions)],
            bpm_timeline=[(0.0, 120.0)],
        )
        events = visualize.compute_rhythm_events(chart)
        self.assertEqual([event['nv_label'] for event in events[:-1]], ['28', '28', '28', '28', '28', '28'])
        self.assertEqual([event['style_label'] for event in events], ['28', '28', '28', '28', '28', '28', '28'])

    def test_rhythm_does_not_merge_regular_sixteenths_into_following_quintuplet_run(self):
        beat_positions = [0.0, 0.25, 0.5, 0.75, 0.947916667, 1.145833334, 1.343750001, 1.541666668, 1.75]
        chart = Chart(
            level=12,
            designer="Tester",
            notes=[Note(NoteType.TAP, (idx % 8) + 1, beat * 0.5) for idx, beat in enumerate(beat_positions)],
            bpm_timeline=[(0.0, 120.0)],
        )
        events = visualize.compute_rhythm_events(chart)
        self.assertEqual([event['nv_label'] for event in events[:3]], ['16', '16', '16'])
        self.assertEqual([event['nv_label'] for event in events[3:8]], ['20', '20', '20', '20', '20'])

    def test_rhythm_infers_quintuplet_from_96th_grid_5_5_4_pattern(self):
        deltas = [5 / 24, 5 / 24, 4 / 24, 5 / 24, 5 / 24]
        beat_positions = [0.0]
        for delta in deltas:
            beat_positions.append(beat_positions[-1] + delta)
        chart = Chart(
            level=12,
            designer="Tester",
            notes=[Note(NoteType.TAP, (idx % 8) + 1, beat * 0.4) for idx, beat in enumerate(beat_positions)],
            bpm_timeline=[(0.0, 150.0)],
        )
        events = visualize.compute_rhythm_events(chart)
        self.assertEqual([event['nv_label'] for event in events[:-1]], ['20', '20', '20', '20', '20'])

    def test_rhythm_infers_quintuplet_from_96th_grid_compensated_run(self):
        deltas = [5 / 24, 5 / 24, 4 / 24, 5 / 24, 4 / 24, 6 / 24, 5 / 24, 4 / 24, 5 / 24, 5 / 24]
        beat_positions = [0.0]
        for delta in deltas:
            beat_positions.append(beat_positions[-1] + delta)
        chart = Chart(
            level=12,
            designer="Tester",
            notes=[Note(NoteType.TAP, (idx % 8) + 1, beat * 0.4) for idx, beat in enumerate(beat_positions)],
            bpm_timeline=[(0.0, 150.0)],
        )
        events = visualize.compute_rhythm_events(chart)
        self.assertEqual([event['nv_label'] for event in events[:-1]], ['20'] * len(deltas))
        display_gaps = [
            events[index + 1]['display_beat'] - events[index]['display_beat']
            for index in range(len(deltas))
        ]
        self.assertTrue(all(abs(gap - 0.2) < 1e-9 for gap in display_gaps))

        primitives, _ = visualize.build_primitives(events, 4, 4, 150, chart)
        note_x = [
            primitive[1]
            for primitive in primitives
            if primitive[0] in ('circle', 'diamond')
        ][:len(events)]
        self.assertEqual(len(note_x), len(events))
        screen_gaps = [note_x[index + 1] - note_x[index] for index in range(len(note_x) - 1)]
        self.assertTrue(all(abs(gap - screen_gaps[0]) < 1e-9 for gap in screen_gaps))

    def test_rhythm_does_not_normalize_coarse_skipped_comma_gaps_to_quintuplets(self):
        deltas = [1.5, 1.5, 1.5, 2.0, 1.5, 1.5, 1.5]
        beat_positions = [0.0]
        for delta in deltas:
            beat_positions.append(beat_positions[-1] + delta)
        chart = Chart(
            level=12,
            designer="Tester",
            notes=[Note(NoteType.HOLD, (idx % 8) + 1, beat * 0.4) for idx, beat in enumerate(beat_positions)],
            bpm_timeline=[(0.0, 150.0)],
        )
        events = visualize.compute_rhythm_events(chart)

        self.assertEqual([event['nv_label'] for event in events[:-1]],
                         ['4.', '4.', '4.', '2', '4.', '4.', '4.'])
        display_gaps = [
            events[index + 1]['display_beat'] - events[index]['display_beat']
            for index in range(len(deltas))
        ]
        self.assertTrue(all(abs(actual - expected) < 1e-9
                            for actual, expected in zip(display_gaps, deltas)))

    def test_rhythm_does_not_normalize_clean_integer_meter_mix_to_tuplets(self):
        deltas = [1 / 4, 1 / 3, 1 / 3, 1 / 3, 1 / 4]
        beat_positions = [0.0]
        for delta in deltas:
            beat_positions.append(beat_positions[-1] + delta)
        chart = Chart(
            level=12,
            designer="Tester",
            notes=[Note(NoteType.TAP, (idx % 8) + 1, beat * 0.4) for idx, beat in enumerate(beat_positions)],
            bpm_timeline=[(0.0, 150.0)],
        )
        events = visualize.compute_rhythm_events(chart)

        self.assertEqual([event['nv_label'] for event in events[:-1]],
                         ['16', '12', '12', '12', '16'])
        display_gaps = [
            events[index + 1]['display_beat'] - events[index]['display_beat']
            for index in range(len(deltas))
        ]
        self.assertTrue(all(abs(actual - expected) < 1e-9
                            for actual, expected in zip(display_gaps, deltas)))

    def test_rhythm_does_not_promote_long_hold_gaps_to_complex_non_tuplet(self):
        beat_positions = [8 + 1 / 12, 10, 12, 14, 16, 18, 20]
        chart = Chart(
            level=12,
            designer="Tester",
            notes=[Note(NoteType.HOLD, (idx % 8) + 1, beat * 0.4) for idx, beat in enumerate(beat_positions)],
            bpm_timeline=[(0.0, 150.0)],
        )
        events = visualize.compute_rhythm_events(chart)
        self.assertEqual(events[0]['nv_label'], '48/23')
        self.assertEqual([event['nv_label'] for event in events[1:6]], ['2', '2', '2', '2', '2'])

    def test_variable_bpm_timing_segments_include_starting_beat(self):
        chart = Chart(
            level=12,
            designer="Tester",
            bpm_timeline=[(0.0, 120.0), (1.0, 240.0), (2.0, 60.0)],
        )
        self.assertEqual(
            make_html.build_timing_segments(chart),
            [
                {'beat': 0.0, 'bpm': 120.0, 'time': 0.0},
                {'beat': 2.0, 'bpm': 240.0, 'time': 1.0},
                {'beat': 6.0, 'bpm': 60.0, 'time': 2.0},
            ],
        )

    def test_audio_alignment_keeps_first_note_arrival_synced_with_scroll_marker(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "maidata.txt").write_text(MAIDATA_WITH_REMASTER, encoding="utf-8")
            (root / "MASTER_preview.mp4").write_bytes(b"video")
            (root / "track.mp3").write_bytes(b"track")

            song = mock.Mock()
            song.first_offset = 1.5
            song.charts = {5: mock.Mock()}
            audio = np.zeros(4000 * 12, dtype=np.float32)

            with mock.patch.object(align_audio, "parse_maidata", return_value=song), \
                 mock.patch.object(align_audio, "compute_rhythm_events", return_value=[{"time": 2.0}]), \
                 mock.patch.object(align_audio, "extract_audio_mono", side_effect=[(audio, 4000), (audio, 4000)]), \
                 mock.patch.object(align_audio, "estimate_track_offset", return_value=(5.0, 0.95)):
                offset = align_audio.align_song(str(root), "test", diff_id=5)

            self.assertAlmostEqual(offset, 6.5)
            self.assertEqual(offset_file_path(root, 5).read_text(encoding="utf-8").strip(), "6.5000")

    def test_audio_alignment_reuses_existing_offset_without_processing_media(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            output = offset_file_path(root, 5)
            output.parent.mkdir(parents=True)
            original = "1.2500\n"
            output.write_text(original, encoding="utf-8")

            with mock.patch.object(align_audio, "extract_audio_mono") as extract:
                offset = align_audio.align_song(str(root), "test", diff_id=5, force=False)

            self.assertEqual(offset, 1.25)
            self.assertEqual(output.read_text(encoding="utf-8"), original)
            extract.assert_not_called()

    def test_required_release_tools_are_resolved_without_downloading(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / ".tools"
            viewer = root / "majdataviewx" / render_preview.MAJDATA_VERSION
            probe = root / "ffprobe" / render_preview.FFPROBE_VERSION / "ffprobe.exe"
            viewer.mkdir(parents=True)
            probe.parent.mkdir(parents=True)
            (viewer / "MajdataView.exe").write_bytes(b"viewer")
            probe.write_bytes(b"ffprobe")
            with mock.patch.object(render_preview, "LOCAL_TOOLS_ROOT", root), \
                 mock.patch.object(render_preview, "SIBLING_TOOLS_ROOT", root / "sibling"), \
                 mock.patch.object(render_preview.shutil, "which", return_value=None):
                self.assertEqual(render_preview.require_majdata_view(), viewer)
                self.assertEqual(render_preview.require_ffprobe(), str(probe))

    def test_missing_release_tools_have_actionable_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / ".tools"
            with mock.patch.object(render_preview, "LOCAL_TOOLS_ROOT", root), \
                 mock.patch.object(render_preview, "SIBLING_TOOLS_ROOT", root / "sibling"), \
                 mock.patch.dict(os.environ, {}, clear=True), \
                 mock.patch.object(render_preview.shutil, "which", return_value=None):
                with self.assertRaisesRegex(FileNotFoundError, "Windows x64 完整包"):
                    render_preview.require_majdata_view()
                with self.assertRaisesRegex(FileNotFoundError, "Windows x64 完整包"):
                    render_preview.require_ffprobe()

    def test_variable_bpm_marker_is_rendered(self):
        chart = Chart(
            level=12,
            designer="Tester",
            bpm_timeline=[(0.0, 120.0), (1.0, 240.0)],
        )
        primitives, _ = visualize.build_primitives([], 8, 8, 120, chart)
        labels = [primitive[3] for primitive in primitives if primitive[0] == 'text']
        self.assertIn('BPM 240', labels)

    def test_measure_boundary_keeps_sixteenth_subdivision_dots(self):
        chart = Chart(level=12, designer="Tester", bpm_timeline=[(0.0, 120.0)])
        primitives, _ = visualize.build_primitives([], 8, 8, 120, chart)
        expected_x = visualize.PAD_X + 4.25 * visualize.PX_PER_BEAT
        self.assertTrue(any(p[0] == 'dot' and p[1] == expected_x for p in primitives))

    def test_left_padding_renders_empty_beat_grid(self):
        chart = Chart(level=12, designer="Tester", bpm_timeline=[(0.0, 120.0)])
        primitives, _ = visualize.build_primitives([], 8, 8, 120, chart)
        self.assertTrue(any(
            p[0] == 'line' and 0 <= p[1] < visualize.PAD_X and p[1] == p[3]
            for p in primitives
        ))
        self.assertTrue(any(
            p[0] == 'dot' and 0 <= p[1] < visualize.PAD_X
            for p in primitives
        ))

    def test_visualize_writes_safe_remaster_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "maidata.txt").write_text(MAIDATA_WITH_REMASTER, encoding="utf-8")

            def touch_output(*args, **kwargs):
                Path(args[4]).touch()

            with mock.patch.object(visualize, "render_strip_svg", side_effect=touch_output), \
                 mock.patch.object(visualize, "render_strip_png", side_effect=touch_output), \
                 mock.patch.object(visualize, "render_strip_svg_segments") as segment_render:
                visualize.process_song(str(root), "test")

            self.assertTrue(rhythm_svg_path(root, 6).exists())
            self.assertTrue(strip_svg_path(root, 6).exists())
            self.assertTrue(rhythm_png_path(root, 6).exists())
            segment_render.assert_called_once()
            self.assertFalse((root / "Re").exists())

    def test_visualize_only_fills_missing_outputs_without_force(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "maidata.txt").write_text(MAIDATA_WITH_REMASTER, encoding="utf-8")
            for suffix in ("_rhythm.svg", "_strip.svg", "_rhythm.png", "_strip_seg_000.svg"):
                (root / f"ReMASTER{suffix}").touch()

            with mock.patch.object(visualize, "render_strip_svg") as svg_render, \
                 mock.patch.object(visualize, "render_strip_png") as png_render, \
                 mock.patch.object(visualize, "render_strip_svg_segments") as segment_render:
                visualize.process_song(str(root), "test", force=False)

            svg_render.assert_not_called()
            png_render.assert_not_called()
            segment_render.assert_not_called()
            self.assertTrue(rhythm_svg_path(root, 6).exists())
            self.assertTrue(strip_svg_path(root, 6).exists())
            self.assertTrue(rhythm_png_path(root, 6).exists())
            self.assertTrue((root / "outputs" / "ReMASTER" / "strip" / "segments" / "strip_seg_000.svg").exists())

    def test_html_uses_safe_remaster_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "maidata.txt").write_text(MAIDATA_WITH_REMASTER, encoding="utf-8")
            (root / "ReMASTER_strip.svg").write_text(
                '<svg width="640" height="66"></svg>', encoding="utf-8"
            )
            output = make_html.generate_html(str(root), "test", diff_id=6)
            self.assertEqual(Path(output), analysis_html_path(root, 6))
            html = Path(output).read_text(encoding="utf-8")
            self.assertIn('data="../../../ReMASTER_strip.svg"', html)
            self.assertNotIn('data="Re:MASTER_strip.svg"', html)
            self.assertIn('class="play-marker"', html)
            self.assertNotIn('.play-marker::before', html)
            self.assertNotIn('class="right-pentagon"', html)
            self.assertIn('id="btnPlay" title="播放" aria-label="播放" disabled', html)
            self.assertIn('id="seekSlider" min="0" max="1" step="0.001" value="0" disabled', html)
            self.assertIn('id="speedSlider" min="0.25" max="2.00" step="0.01" value="1.00" disabled', html)
            self.assertIn('id="speedInput" min="0.25" max="2.00" step="0.01" value="1.00"', html)
            self.assertIn('id="seekTip">0:00<', html)
            self.assertIn('id="timeVal">0:00 / 0:00<', html)
            self.assertIn('function videoTimeToState(videoT)', html)
            self.assertIn('function updateSeekProgress(progress)', html)
            self.assertIn('function updateSpeedUi(value)', html)
            self.assertIn('function updateSeekTip(progress)', html)
            self.assertIn('function syncSeekUi(force = false)', html)
            self.assertIn('bpmNumber.textContent = formatBpm(state.bpm)', html)
            self.assertIn('--seek-progress: 0%;', html)
            self.assertIn('background: linear-gradient(to right, #4fc3f7 0%, #4fc3f7 var(--seek-progress), rgba(108, 112, 132, 0.32) var(--seek-progress), rgba(108, 112, 132, 0.32) 100%);', html)
            self.assertIn('class="control-buttons"', html)
            self.assertIn('class="seek-wrap"', html)
            self.assertIn('class="speed-wrap"', html)
            self.assertNotIn('class="offset-wrap"', html)
            self.assertNotIn('id="offsetSlider"', html)
            self.assertNotIn('id="offsetVal"', html)
            self.assertIn('class="delay-wrap"', html)
            self.assertIn('id="delaySlider" min="-1000" max="1000" step="1" value="0" disabled', html)
            self.assertIn('id="delayInput" min="-1000" max="1000" step="1" value="0"', html)
            self.assertIn('微调延迟', html)
            self.assertIn('class="scrolling-stage"', html)
            self.assertIn('.seek-wrap:hover input[type=range],', html)
            self.assertIn('.speed-wrap:hover input[type=range] {', html)
            self.assertIn(".seek-wrap.seeking input[type=range] {", html)
            self.assertIn("seekSlider.addEventListener('input'", html)
            self.assertIn("speedSlider.addEventListener('input'", html)
            self.assertIn('function setPlaybackRate(value)', html)
            self.assertIn('pv.playbackRate = rate;', html)
            self.assertIn("speedInput.addEventListener('input'", html)
            self.assertIn("seekSlider.addEventListener('pointermove'", html)
            self.assertIn("pv.addEventListener('timeupdate', () => {", html)
            self.assertIn("pv.currentTime = Math.max(0, pv.currentTime - 1);", html)
            self.assertIn("pv.currentTime = Math.min(duration, pv.currentTime + 1);", html)
            self.assertIn('const scrollDistance = state.beat * PX_PER_BEAT', html)
            self.assertIn('const displayScrollDistance = Math.round(scrollDistance * 100) / 100;', html)
            self.assertIn("svgScroll.style.transform = `translate3d(${", html)
            self.assertIn('class="virtual-strip"', html)
            self.assertIn('id="virtualStrip"', html)
            self.assertIn('const SEGMENTS = [];', html)
            self.assertIn('const USE_SEGMENTS = SEGMENTS.length > 0;', html)
            self.assertIn('function updateVisibleSegments(scrollDistance)', html)
            self.assertIn('svgScroll.hidden = true;', html)
            self.assertIn('function findTimingSegment(chartT)', html)
            self.assertIn('let timingIndex = 0;', html)
            self.assertIn('while (lo <= hi)', html)
            self.assertIn('const START_DISPLAY_BEAT = 0;', html)
            self.assertIn('return { beat: START_DISPLAY_BEAT, bpm: timings[0].bpm };', html)
            self.assertIn('window.__RHYTHM_ANALYSIS__', html)
            self.assertIn('<video id="pv" preload="metadata">', html)
            self.assertIn('src="../video/preview.mp4"', html)
            self.assertIn(':root { --rhythm-height: 108px;', html)
            self.assertIn('--marker-size: 52px;', html)
            self.assertIn('--marker-top: 15.4px;', html)
            self.assertIn('const VIDEO_OFFSET = 0.0;', html)
            self.assertIn('const chartT = videoT - VIDEO_OFFSET - delayMs / 1000;', html)
            self.assertNotIn('fetch(OFFSET_FILE', html)
            self.assertLess(html.index('class="controls"'), html.index('class="rhythm-container"'))
            self.assertIn('grid-template-columns: minmax(0, 1fr) clamp(430px, 34vw, 820px);', html)
            self.assertIn('width: 100vw; height: calc(100vh - var(--rhythm-height));', html)
            self.assertIn('padding-right: clamp(24px, 4vw, 56px);', html)
            self.assertIn('margin-top: clamp(14px, 1.4vw, 24px);', html)
            self.assertIn('padding-left: clamp(4px, 0.5vw, 8px);', html)
            self.assertNotIn('translateX(-8vw)', html)
            self.assertIn('class="video-pane"', html)
            self.assertIn('class="video-crop"', html)
            self.assertIn('class="info-pane"', html)
            self.assertIn('class="scrolling-svg"', html)
            self.assertIn('id="svgScroll"', html)
            self.assertIn('data="../../../ReMASTER_strip.svg"', html)
            self.assertIn('contain: layout paint style;', html)
            self.assertIn('backface-visibility: hidden;', html)
            self.assertIn('appearance: none; height: 2px; border-radius: 999px;', html)
            self.assertIn('background: rgba(20,22,34,0.42);', html)
            self.assertIn('.seek-wrap:hover,', html)
            self.assertIn('white-space: nowrap;', html)
            self.assertIn('class="song-meta-body"', html)
            self.assertNotIn('class="song-kicker"', html)
            self.assertIn('class="chart-details"', html)
            self.assertNotIn('class="note-stats"', html)
            self.assertIn('<span>BPM 范围</span>', html)
            self.assertIn('aspect-ratio: 1 / 1;', html)
            self.assertIn('width: min(100%, calc(100vh - var(--rhythm-height)));', html)
            self.assertIn('inset: 0;', html)
            self.assertIn('width: 100%;', html)
            self.assertIn('height: 100%;', html)
            self.assertIn('object-fit: contain;', html)
            self.assertNotIn('--video-crop-x:', html)
            self.assertIn('object-position: center center;', html)
            self.assertIn('transform: none;', html)
            self.assertNotIn('class="video-watermark-mask"', html)
            self.assertNotIn('transform: scale(1.16) translateX(-7%);', html)
            self.assertNotIn('translateX(-4.5%)', html)
            self.assertIn('mask-image: none;', html)
            self.assertIn('border-top: none;', html)
            self.assertIn('box-shadow: none;', html)
            self.assertIn('display: none;', html)
            self.assertIn('background: none;', html)
            self.assertIn('class="bpm-readout"', html)
            self.assertIn('class="measure-status"', html)
            self.assertIn('id="measureNumber">1</strong>', html)
            self.assertIn('class="meter-status"', html)
            self.assertIn('id="meterSignature">4/4</strong>', html)
            self.assertIn('const MEASURE_BOUNDARIES = ', html)
            self.assertIn('const METER_SECTIONS = ', html)
            self.assertIn('function findMeasureNumber(beat)', html)
            self.assertIn('function findMeterSignature(beat)', html)
            self.assertIn('measureNumber.textContent = String(currentMeasure);', html)
            self.assertIn('meterSignature.textContent = currentMeter;', html)
            self.assertNotIn('outer-hexagon', html)
            self.assertIn('class="left-pentagon"', html)
            self.assertNotIn('<div class="detail-item"><span>拍号</span>', html)

    def test_html_fine_tune_delay_control_present(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "maidata.txt").write_text(MAIDATA_WITH_REMASTER, encoding="utf-8")
            (root / "ReMASTER_strip.svg").write_text(
                '<svg width="640" height="66"></svg>', encoding="utf-8"
            )
            output = make_html.generate_html(str(root), "test", diff_id=6)
            html = Path(output).read_text(encoding="utf-8")

            self.assertIn('let delayMs = 0;', html)
            self.assertIn('class="speed-delay-group"', html)
            self.assertIn('function updateDelayUi(value)', html)
            self.assertIn('function setDelay(value)', html)
            self.assertIn("delaySlider.addEventListener('input'", html)
            self.assertIn("delayInput.addEventListener('input'", html)
            self.assertIn('updateDelayUi(0);', html)
            self.assertIn('delaySlider.disabled = !available;', html)
            self.assertIn('delayInput.disabled = !available;', html)
            self.assertIn('--delay-fill-start: 50%; --delay-fill-end: 50%;', html)
            self.assertIn('rgba(77,208,225,0.14)', html)

    def test_html_uses_svg_segments_when_available(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "maidata.txt").write_text(MAIDATA_WITH_REMASTER, encoding="utf-8")
            (root / "ReMASTER_strip.svg").write_text(
                '<svg width="8000" height="60"></svg>', encoding="utf-8"
            )
            (root / "ReMASTER_strip_seg_000.svg").write_text(
                '<svg width="7232" height="60"></svg>', encoding="utf-8"
            )
            output = make_html.generate_html(str(root), "test", diff_id=6)
            html = Path(output).read_text(encoding="utf-8")

            self.assertIn('"src": "../../../ReMASTER_strip_seg_000.svg"', html)
            self.assertIn('"x": 0', html)
            self.assertIn('virtualStrip.appendChild(object);', html)
            self.assertIn('if (USE_SEGMENTS) {', html)

    def test_html_regenerates_when_offset_is_newer_than_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            html_path = root / "MASTER_analysis.html"
            offset_path = root / "MASTER_offset.txt"
            html_path.write_text("old", encoding="utf-8")
            offset_path.write_text("5.0000\n", encoding="utf-8")
            old_time = 1000
            new_time = 2000
            os.utime(html_path, (old_time, old_time))
            os.utime(offset_path, (new_time, new_time))

            self.assertTrue(make_html.output_needs_regeneration(str(root), 5, str(html_path)))

    def test_html_skips_when_output_is_newer_than_offset(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            html_path = root / "MASTER_analysis.html"
            offset_path = root / "MASTER_offset.txt"
            html_path.write_text("new", encoding="utf-8")
            offset_path.write_text("5.0000\n", encoding="utf-8")
            old_time = 1000
            new_time = 2000
            os.utime(offset_path, (old_time, old_time))
            os.utime(html_path, (new_time, new_time))

            self.assertFalse(make_html.output_needs_regeneration(str(root), 5, str(html_path)))

    def test_html_render_loop_avoids_redundant_dom_work(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "maidata.txt").write_text(MAIDATA_WITH_REMASTER, encoding="utf-8")
            (root / "ReMASTER_strip.svg").write_text(
                '<svg width="640" height="66"></svg>', encoding="utf-8"
            )
            output = make_html.generate_html(str(root), "test", diff_id=6)
            html = Path(output).read_text(encoding="utf-8")

            # marker position is cached instead of measured every frame
            self.assertIn('if (cachedPlayPositionPx === null)', html)
            # transform / bpm / time / seek progress are dirty-checked
            self.assertIn('if (lastScrollDistance === null || Math.abs(displayScrollDistance - lastScrollDistance) >= 0.01)', html)
            self.assertIn('if (state.bpm !== lastBpm)', html)
            self.assertIn('if (timeText !== lastTimeText)', html)
            self.assertIn('if (percent !== lastSeekPercent)', html)
            self.assertIn('const shouldUpdateProgress = force || isSeeking || lastSeekUiTime === null || Math.abs(current - lastSeekUiTime) >= 0.1;', html)
            self.assertIn('renderFrame();', html)
            self.assertIn('renderFrame(true);', html)
            # seek tip is driven by the drag itself, not every animation frame
            self.assertIn('updateSeekTip(parseFloat(e.target.value) || 0)', html)
            # resize is coalesced into a single animation frame
            self.assertIn('cancelAnimationFrame(resizeRafId)', html)
            # pointer move reuses a cached slider rect
            self.assertIn('seekRectCache', html)

    def test_smaller_sixteenth_notes_remain_visually_tangent(self):
        visible_diameter = visualize.NOTE_OUTER_DIAMETER
        sixteenth_spacing = visualize.PX_PER_BEAT / 4
        self.assertAlmostEqual(visible_diameter, sixteenth_spacing)
        self.assertEqual(visible_diameter, 27.0)
        self.assertEqual(visualize.NOTE_AREA_H, 46)
        self.assertEqual(visualize.LABEL_AREA_H, 14)
        self.assertEqual(visualize.LABEL_GAP, 0)
        self.assertEqual(visualize.NOTE_RING_W, 1.0)
        self.assertEqual(visualize.NOTE_RING_GAP, 1.0)

    def test_protected_touch_and_break_note_rings(self):
        chart = Chart(level=12, designer="Tester", bpm_timeline=[(0.0, 120.0)])
        events = [
            {'beat': 0.0, 'nv': 16, 'nv_label': '16',
             'notes': [Note(NoteType.TAP, 1, 0.0, extra={'is_ex': True})], 'time': 0.0},
            {'beat': 1.0, 'nv': 16, 'nv_label': '16',
             'notes': [Note(NoteType.TOUCH, 0, 0.5)], 'time': 0.5},
            {'beat': 2.0, 'nv': 16, 'nv_label': '16',
             'notes': [Note(NoteType.BREAK, 2, 1.0)], 'time': 1.0},
            {'beat': 3.0, 'nv': 16, 'nv_label': '16',
             'notes': [Note(NoteType.BREAK, 3, 1.5, extra={'is_ex': True})], 'time': 1.5},
        ]
        primitives, _ = visualize.build_primitives(events, 4, 4, 120, chart)
        rings = [primitive for primitive in primitives if primitive[0] == 'ring']

        self.assertEqual(len(rings), 4)
        self.assertEqual(rings[0][6], visualize.NOTE_RING_DASH)
        self.assertEqual(rings[1][6], visualize.NOTE_RING_DASH)
        self.assertEqual(rings[2][4], visualize.BREAK_RING_COLOR)
        self.assertEqual(rings[2][6], '')
        self.assertEqual(rings[3][4], visualize.BREAK_RING_COLOR)
        self.assertEqual(rings[3][6], visualize.NOTE_RING_DASH)
        self.assertTrue(all(ring[5] == visualize.NOTE_RING_W for ring in rings))
        self.assertIn('stroke-dasharray="3 2"', visualize._prim_to_svg(rings[0], 0))
        self.assertIn(visualize.BREAK_RING_COLOR, visualize._prim_to_svg(rings[3], 0))

    def test_center_touch_c1_renders_as_protected_dashed_ring(self):
        notes, timeline, _ = parse_inote("{16}C1,,5,E", 120)
        chart = Chart(level=12, designer="Tester", notes=notes, bpm_timeline=timeline)
        events = visualize.compute_rhythm_events(chart)
        primitives, _ = visualize.build_primitives(events, 4, 4, 120, chart)
        rings = [primitive for primitive in primitives if primitive[0] == 'ring']

        self.assertEqual(events[0]['notes'][0].note_type, NoteType.TOUCH)
        self.assertEqual(len(events[0]['notes']), 1)
        self.assertEqual(rings[0][6], visualize.NOTE_RING_DASH)

    def test_note_ring_uses_all_protected_multi_and_diamond_rules(self):
        chart = Chart(level=12, designer="Tester", bpm_timeline=[(0.0, 120.0)])
        events = [
            {'beat': 0.0, 'nv': 16, 'nv_label': '16', 'style_label': '4.',
             'notes': [Note(NoteType.TAP, 1, 0.0, extra={'is_ex': True})], 'time': 0.0},
            {'beat': 1.0, 'nv': 16, 'nv_label': '16',
             'notes': [
                 Note(NoteType.TOUCH, 0, 0.5),
                 Note(NoteType.TAP, 2, 0.5),
             ], 'time': 0.5},
            {'beat': 2.0, 'nv': 16, 'nv_label': '16',
             'notes': [
                 Note(NoteType.TAP, 1, 1.0),
                 Note(NoteType.TAP, 8, 1.0),
             ], 'time': 1.0},
            {'beat': 3.0, 'nv': 16, 'nv_label': '16',
             'notes': [
                 Note(NoteType.BREAK, 1, 1.5),
                 Note(NoteType.TAP, 8, 1.5),
             ], 'time': 1.5},
        ]
        primitives, _ = visualize.build_primitives(events, 4, 4, 120, chart)
        diamond_rings = [primitive for primitive in primitives if primitive[0] == 'diamond_ring']
        rings = [primitive for primitive in primitives if primitive[0] == 'ring']

        self.assertEqual(len(diamond_rings), 1)
        self.assertEqual(diamond_rings[0][4], '#ffffff')
        self.assertEqual(diamond_rings[0][6], visualize.NOTE_RING_DASH)
        self.assertIn('<polygon', visualize._prim_to_svg(diamond_rings[0], 0))
        self.assertNotIn('<circle', visualize._prim_to_svg(diamond_rings[0], 0))

        self.assertEqual(rings[0][4], '#ffffff')
        self.assertEqual(rings[0][6], '')
        self.assertEqual(rings[1][4], '#ffffff')
        self.assertEqual(rings[2][4], visualize.BREAK_RING_COLOR)

    def test_break_hold_star_head_uses_break_ring_color(self):
        chart = Chart(level=12, designer="Tester", bpm_timeline=[(0.0, 120.0)])
        events = [
            {'beat': 0.0, 'nv': 16, 'nv_label': '16',
             'notes': [
                 Note(NoteType.HOLD, 1, 0.0, duration_sec=0.5,
                      extra={'is_break': True, 'star': True}),
                 Note(NoteType.SLIDE, 1, 0.0, duration_sec=1.0, end_button=4),
             ], 'time': 0.0},
        ]
        primitives, _ = visualize.build_primitives(events, 4, 4, 120, chart)
        rings = [primitive for primitive in primitives if primitive[0] == 'ring']

        self.assertEqual(len(rings), 1)
        self.assertEqual(rings[0][4], visualize.BREAK_RING_COLOR)

    def test_slide_objects_do_not_create_multi_press_ring_color(self):
        chart = Chart(level=12, designer="Tester", bpm_timeline=[(0.0, 120.0)])
        events = [
            {'beat': 0.0, 'nv': 16, 'nv_label': '16',
             'notes': [
                 Note(NoteType.TAP, 1, 0.0),
                 Note(NoteType.SLIDE, 1, 0.0, duration_sec=1.0, end_button=4),
             ], 'time': 0.0},
            {'beat': 1.0, 'nv': 16, 'nv_label': '16',
             'notes': [
                 Note(NoteType.TAP, 2, 0.5),
                 Note(NoteType.HOLD, 3, 0.5, duration_sec=1.0),
             ], 'time': 0.5},
            {'beat': 2.0, 'nv': 16, 'nv_label': '16',
             'notes': [
                 Note(NoteType.TOUCH, 0, 1.0),
                 Note(NoteType.SLIDE, 4, 1.0, duration_sec=1.0, end_button=7),
             ], 'time': 1.0},
        ]
        primitives, _ = visualize.build_primitives(events, 4, 4, 120, chart)
        rings = [primitive for primitive in primitives if primitive[0] == 'ring']

        self.assertEqual(rings[0][4], '#ffffff')
        self.assertEqual(rings[0][6], '')
        self.assertEqual(rings[1][4], '#ffffff')
        self.assertEqual(rings[1][6], '')
        self.assertEqual(rings[2][4], '#ffffff')
        self.assertEqual(rings[2][6], visualize.NOTE_RING_DASH)

    def test_note_value_labels_grow_without_exceeding_neighbor_space(self):
        chart = Chart(level=12, designer="Tester", bpm_timeline=[(0.0, 120.0)])
        events = [
            {'beat': beat, 'nv': 32, 'nv_label': '32', 'notes': [], 'time': beat / 2}
            for beat in (0.0, 0.125, 0.25)
        ]
        primitives, _ = visualize.build_primitives(events, 4, 4, 120, chart)
        label_sizes = [
            primitive[5]
            for primitive in primitives
            if primitive[0] == 'text' and primitive[3] == '32'
        ]
        self.assertTrue(label_sizes)
        self.assertTrue(all(6.9 < size <= 13.2 for size in label_sizes))
        estimated_width = max(label_sizes) * 0.62 * len('32')
        self.assertLess(estimated_width, 0.125 * visualize.PX_PER_BEAT)

    def test_label_style_defaults_to_bold_for_readability(self):
        self.assertEqual(visualize._label_style('16', 16), ('#111111', 'bold', 'normal'))
        self.assertEqual(visualize._label_style('3/2', None), ('#7f7f7f', 'bold', 'normal'))

    def test_html_prefers_actual_remaster_preview_filename(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "maidata.txt").write_text(MAIDATA_WITH_REMASTER, encoding="utf-8")
            (root / "ReMASTER_strip.svg").write_text(
                '<svg width="640" height="66"></svg>', encoding="utf-8"
            )
            (root / "re.master_PREVIEW.MP4").touch()
            output = make_html.generate_html(str(root), "test", diff_id=6)
            html = Path(output).read_text(encoding="utf-8")
            self.assertLess(
                html.index('src="../../../re.master_PREVIEW.MP4"'),
                html.index('src="../video/preview.mp4"'),
            )

    def test_run_all_force_defaults_to_false(self):
        selected = [SongFolder(Path("song"), "song", "song")]
        with mock.patch.object(sys, "argv", ["run_all.py"]), \
             mock.patch.object(run_all, "discover_song_folders", return_value=selected), \
             mock.patch.object(run_all, "target_difficulties_for_song", return_value=[5]), \
             mock.patch.object(run_all, "run_step", return_value=True) as run_step:
            result = run_all.main()
        self.assertEqual(result, 0)
        self.assertTrue(all(call.args[3] is False for call in run_step.call_args_list))
        self.assertEqual(len(run_step.call_args_list), 5)
        self.assertEqual(run_step.call_args_list[1].args[2], ['-d', 'song', '-diff', '5'])
        self.assertEqual(run_step.call_args_list[2].args[2], ['-d', 'song', '-diff', '5'])
        self.assertEqual(run_step.call_args_list[4].args[2], ['-d', 'song', '-diff', '5', '-offset', '0.0'])

    def test_run_all_force_preserves_meter_video_and_audio_alignment(self):
        selected = [SongFolder(Path("song"), "song", "song")]
        with mock.patch.object(sys, "argv", ["run_all.py", "-f"]), \
             mock.patch.object(run_all, "discover_song_folders", return_value=selected), \
             mock.patch.object(run_all, "target_difficulties_for_song", return_value=[5]), \
             mock.patch.object(run_all, "run_step", return_value=True) as run_step:
            result = run_all.main()

        self.assertEqual(result, 0)
        self.assertEqual(
            [call.args[3] for call in run_step.call_args_list],
            [False, True, False, False, True],
        )

    def test_run_all_returns_failure_when_a_step_fails(self):
        selected = [SongFolder(Path("song"), "song", "song")]
        with mock.patch.object(sys, "argv", ["run_all.py"]), \
             mock.patch.object(run_all, "discover_song_folders", return_value=selected), \
             mock.patch.object(run_all, "target_difficulties_for_song", return_value=[5]), \
             mock.patch.object(run_all, "run_step", side_effect=[True, False, True, True, True]):
            result = run_all.main()
        self.assertEqual(result, 1)

    def test_run_all_only_passes_difficulty_to_supporting_steps(self):
        selected = [SongFolder(Path("Xaleid◆scopiX"), "Xaleid◆scopiX", "11820")]
        with mock.patch.object(sys, "argv", ["run_all.py", "-d", "11820", "-diff", "6"]), \
             mock.patch.object(run_all, "discover_song_folders", return_value=selected), \
             mock.patch.object(run_all, "target_difficulties_for_song", return_value=[6]), \
             mock.patch.object(run_all, "run_step", return_value=True) as run_step:
            result = run_all.main()
        self.assertEqual(result, 0)
        calls = run_step.call_args_list
        self.assertEqual(calls[0].args[2], ['-d', 'Xaleid◆scopiX', '-diff', '6'])
        self.assertEqual(calls[1].args[2], ['-d', 'Xaleid◆scopiX', '-diff', '6'])
        self.assertEqual(calls[2].args[2], ['-d', 'Xaleid◆scopiX', '-diff', '6'])
        self.assertEqual(
            calls[4].args[2],
            ['-d', 'Xaleid◆scopiX', '-diff', '6', '-offset', '0.0'],
        )

    def test_run_step_returns_status_without_printing_child_output(self):
        process = mock.Mock(returncode=0, stdout="child stdout", stderr="child stderr")
        with mock.patch.object(run_all.subprocess, "run", return_value=process), \
             mock.patch("builtins.print") as fake_print:
            ok = run_all.run_step("step", "script.py", [])

        self.assertTrue(ok)
        fake_print.assert_not_called()

    def test_run_all_prints_once_after_all_steps_of_a_difficulty_finish(self):
        selected = [SongFolder(Path("song"), "song", "song")]
        with mock.patch.object(sys, "argv", ["run_all.py"]), \
             mock.patch.object(run_all, "discover_song_folders", return_value=selected), \
             mock.patch.object(run_all, "target_difficulties_for_song", return_value=[5]), \
             mock.patch.object(run_all, "run_step", side_effect=[True, True, True, True, True]), \
             mock.patch.object(run_all, "timestamp_now", return_value="12:34:56"), \
             mock.patch("builtins.print") as fake_print:
            result = run_all.main()

        self.assertEqual(result, 0)
        self.assertEqual(
            [call.args[0] for call in fake_print.call_args_list],
            ["[12:34:56] song MASTER 完成"],
        )

    def test_run_all_does_not_print_when_a_difficulty_has_failed_steps(self):
        selected = [SongFolder(Path("song"), "song", "song")]
        with mock.patch.object(sys, "argv", ["run_all.py"]), \
             mock.patch.object(run_all, "discover_song_folders", return_value=selected), \
             mock.patch.object(run_all, "target_difficulties_for_song", return_value=[5]), \
             mock.patch.object(run_all, "run_step", side_effect=[True, False, True, True, True]), \
             mock.patch("builtins.print") as fake_print:
            result = run_all.main()

        self.assertEqual(result, 1)
        fake_print.assert_not_called()


if __name__ == "__main__":
    unittest.main()
