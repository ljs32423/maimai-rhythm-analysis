import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import mra.make_html as make_html
import mra.render_preview as render_preview
import mra.visualize as visualize


MAIDATA_MASTER_AND_REMASTER = """&title=Test
&artist=Tester
&wholebpm=120
&first=0
&lv_5=12
&lv_6=13
&inote_5=(120){4}1,2,E
&inote_6=(120){4}1,2,E
"""


class GenerationDefaultTests(unittest.TestCase):
    def test_render_preview_default_cli_processes_all_chart_difficulties(self):
        with tempfile.TemporaryDirectory() as tmp:
            song_dir = Path(tmp) / "song"
            song_dir.mkdir()
            (song_dir / "maidata.txt").write_text(MAIDATA_MASTER_AND_REMASTER, encoding="utf-8")

            with mock.patch.object(sys, "argv", ["render_preview.py", "-i", tmp]), \
                 mock.patch.object(render_preview, "install_majdata_view", return_value=Path("C:/Majdata")), \
                 mock.patch.object(render_preview, "record_preview", return_value=Path("done.mp4")) as record_preview:
                result = render_preview.main()

        self.assertEqual(result, 0)
        self.assertEqual([call.args[2] for call in record_preview.call_args_list], [5, 6])

    def test_make_html_default_cli_processes_all_chart_difficulties(self):
        with tempfile.TemporaryDirectory() as tmp:
            song_dir = Path(tmp) / "song"
            song_dir.mkdir()
            (song_dir / "maidata.txt").write_text(MAIDATA_MASTER_AND_REMASTER, encoding="utf-8")

            with mock.patch.object(sys, "argv", ["make_html.py", "-i", tmp]), \
                 mock.patch.object(make_html, "find_song_dirs", return_value=[(str(song_dir), "song")]), \
                 mock.patch.object(make_html, "generate_html", side_effect=["a.html", "b.html"]) as generate_html:
                result = make_html.main()

        self.assertEqual(result, 0)
        self.assertEqual([call.args[2] for call in generate_html.call_args_list], [5, 6])

    def test_visualize_default_cli_processes_master_and_remaster_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            song_dir = Path(tmp) / "song"
            song_dir.mkdir()
            (song_dir / "maidata.txt").write_text(
                MAIDATA_MASTER_AND_REMASTER.replace(
                    "&lv_5=12\n&lv_6=13\n&inote_5=(120){4}1,2,E\n&inote_6=(120){4}1,2,E",
                    "&lv_4=11\n&lv_5=12\n&lv_6=13\n"
                    "&inote_4=(120){4}1,2,E\n&inote_5=(120){4}1,2,E\n&inote_6=(120){4}1,2,E",
                ),
                encoding="utf-8",
            )

            with mock.patch.object(sys, "argv", ["visualize.py", "-i", tmp]), \
                 mock.patch.object(visualize, "find_song_dirs", return_value=[(str(song_dir), "song")]), \
                 mock.patch.object(
                     visualize,
                     "process_song",
                     return_value={
                         "song_id": "song",
                         "title": "Test",
                         "difficulties": {5: {"notes": 2}, 6: {"notes": 2}},
                         "errors": [],
                     },
                 ) as process_song:
                result = visualize.main()

        self.assertEqual(result, 0)
        self.assertEqual(process_song.call_args.args[3], [5, 6])


if __name__ == "__main__":
    unittest.main()
