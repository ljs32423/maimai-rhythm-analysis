import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from mra import desktop_backend
from mra.export_player import SCHEMA_VERSION, export_player


MAIDATA = """&title=播放器 测试
&artist=Tester
&wholebpm=120
&first=0
&genre=maimai
&version=TEST
&des_5=Designer
&lv_5=13+
&inote_5=(120){4}1,A1,3b,4x,E
"""


class PlayerExportTests(unittest.TestCase):
    def make_song(self, root: Path) -> Path:
        song = root / "曲名 with spaces [DX]"
        song.mkdir()
        (song / "maidata.txt").write_text(MAIDATA, encoding="utf-8")
        (song / "bg.png").write_bytes(b"cover")
        (song / "track.mp3").write_bytes(b"audio")
        preview = song / "outputs" / "MASTER" / "video" / "preview.mp4"
        preview.parent.mkdir(parents=True)
        preview.write_bytes(b"video")
        offset = song / "outputs" / "MASTER" / "sync" / "offset.txt"
        offset.parent.mkdir(parents=True)
        offset.write_text("0.125\n", encoding="utf-8")
        return song

    def test_export_writes_versioned_manifest_and_shared_scene(self):
        with tempfile.TemporaryDirectory() as tmp:
            song = self.make_song(Path(tmp))
            manifest_path = export_player(song, 5)
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            scene_path = manifest_path.parent / manifest["scene"]["path"]
            scene = json.loads(scene_path.read_text(encoding="utf-8"))

            self.assertEqual(manifest["schema_version"], SCHEMA_VERSION)
            self.assertEqual(scene["schema_version"], SCHEMA_VERSION)
            self.assertEqual(manifest["song"]["title"], "播放器 测试")
            self.assertEqual(manifest["chart"]["level"], "13+")
            self.assertEqual(manifest["media"]["video_offset_sec"], 0.125)
            self.assertTrue(manifest["media"]["preview_video"].endswith("preview.mp4"))
            self.assertEqual(scene["primitive_count"], len(scene["primitives"]))
            self.assertGreater(scene["primitive_count"], 0)
            self.assertEqual(
                scene["primitives"],
                sorted(scene["primitives"], key=lambda item: (item["x0"], item["order"])),
            )
            types = {item["type"] for item in scene["primitives"]}
            self.assertTrue({"circle", "ring", "text"}.issubset(types))

    def test_fingerprint_skips_unchanged_scene_and_rebuilds_changed_input(self):
        with tempfile.TemporaryDirectory() as tmp:
            song = self.make_song(Path(tmp))
            manifest_path = export_player(song, 5)
            scene_path = manifest_path.parent / "scene.json"
            original_mtime = scene_path.stat().st_mtime_ns
            export_player(song, 5)
            self.assertEqual(scene_path.stat().st_mtime_ns, original_mtime)

            maidata = song / "maidata.txt"
            maidata.write_text(MAIDATA.replace("Tester", "Changed"), encoding="utf-8")
            export_player(song, 5)
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(manifest["song"]["artist"], "Changed")

    def test_backend_export_stdout_is_json_lines_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            song = self.make_song(Path(tmp))
            with mock.patch("sys.stdout") as stdout:
                stdout.write.side_effect = None
                result = desktop_backend.main([
                    "export", "--song-dir", str(song), "--difficulty", "5",
                    "--json-progress",
                ])
            self.assertEqual(result, 0)
            emitted = "".join(call.args[0] for call in stdout.write.call_args_list
                              if call.args)
            lines = [json.loads(line) for line in emitted.splitlines() if line.strip()]
            self.assertEqual(lines[0]["event"], "started")
            self.assertEqual(lines[-1]["event"], "completed")


if __name__ == "__main__":
    unittest.main()
