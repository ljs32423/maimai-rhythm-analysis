import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fastapi import HTTPException
from fastapi.testclient import TestClient

from mra.web_app import _atomic_text, _safe_song, create_app


MAIDATA = """&title=Web Test
&artist=Tester
&wholebpm=150
&first=0
&lv_5=13+
&des_5=Chart Author
&inote_5=(150){4}1,2,3,E
"""


class WebAppTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name) / "songs"
        self.song = self.root / "Web Test"
        self.song.mkdir(parents=True)
        (self.song / "maidata.txt").write_text(MAIDATA, encoding="utf-8")
        self.config_path = Path(self.temporary.name) / "config.json"
        self.app = create_app(config_file=self.config_path, songs_root=self.root)
        self.client = TestClient(self.app)

    def tearDown(self):
        self.temporary.cleanup()

    def test_health_and_song_library(self):
        self.assertEqual(self.client.get("/api/health").json(), {"status": "ok"})
        response = self.client.get("/api/songs")
        self.assertEqual(response.status_code, 200)
        song = response.json()["songs"][0]
        self.assertEqual(song["title"], "Web Test")
        self.assertEqual(song["difficulties"][0]["id"], 5)

    def test_meter_can_be_saved_and_reloaded(self):
        payload = {
            "data": {
                "default": "4/4",
                "sections": [
                    {"start_beat": 0, "signature": "4/4"},
                    {"start_beat": 16, "signature": "7/8"},
                ],
            },
        }
        response = self.client.put("/api/songs/Web%20Test/meter/5", json=payload)
        self.assertEqual(response.status_code, 200)
        loaded = self.client.get("/api/songs/Web%20Test/meter/5").json()
        self.assertTrue(loaded["exists"])
        self.assertEqual(loaded["data"]["sections"][1]["signature"], "7/8")

    def test_sweep_file_is_validated_and_saved(self):
        marked = MAIDATA.replace("1,2,3,E", "1/S,2,3,E")
        response = self.client.put(
            "/api/songs/Web%20Test/sweep", json={"content": marked},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["markers"], 1)
        self.assertEqual(
            self.client.get("/api/songs/Web%20Test/sweep").json()["content"],
            marked,
        )

    def test_sweep_editor_cannot_change_chart_structure(self):
        changed = MAIDATA.replace("1,2,3,E", "1,2,4,E")
        response = self.client.put(
            "/api/songs/Web%20Test/sweep", json={"content": changed},
        )
        self.assertEqual(response.status_code, 422)
        self.assertFalse((self.song / "maidata_sweep.txt").exists())

    def test_sweep_save_tolerates_temp_cleanup_failure(self):
        # 杀毒软件锁定或沙箱拦截删除时，清理临时文件失败不应使保存失败
        marked = MAIDATA.replace("1,2,3,E", "1/S,2,3,E")
        real_unlink = Path.unlink

        def flaky_unlink(self, *args, **kwargs):
            if self.name.startswith(".maidata_sweep."):
                raise OSError("simulated temp-file lock")
            return real_unlink(self, *args, **kwargs)

        with mock.patch.object(Path, "unlink", flaky_unlink):
            response = self.client.put(
                "/api/songs/Web%20Test/sweep", json={"content": marked},
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["markers"], 1)
        self.assertEqual(
            self.client.get("/api/songs/Web%20Test/sweep").json()["content"],
            marked,
        )

    def test_atomic_text_reports_real_error_when_cleanup_fails(self):
        # 写入失败时，临时文件清理失败不能掩盖真正的错误
        def bad_replace(*args, **kwargs):
            raise OSError("simulated replace failure")

        def bad_unlink(self, *args, **kwargs):
            raise OSError("simulated cleanup failure")

        with mock.patch("mra.web_app.os.replace", bad_replace), \
             mock.patch.object(Path, "unlink", bad_unlink):
            with self.assertRaises(OSError) as context:
                _atomic_text(self.root / "dummy.txt", "x")
        self.assertIn("replace failure", str(context.exception))

    def test_invalid_meter_is_rejected_without_overwriting(self):
        response = self.client.put(
            "/api/songs/Web%20Test/meter/5",
            json={"data": {"default": "4/3", "sections": []}},
        )
        self.assertEqual(response.status_code, 422)
        self.assertFalse((self.song / "outputs" / "MASTER" / "meter" / "meter.json").exists())

    def test_song_resolution_rejects_parent_traversal(self):
        with self.assertRaises(HTTPException):
            _safe_song(self.root, "..")

    def test_settings_are_persisted(self):
        current = self.client.get("/api/settings").json()
        current["encoder"] = "libx264"
        response = self.client.put("/api/settings", json={"data": current})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            json.loads(self.config_path.read_text(encoding="utf-8"))["encoder"],
            "libx264",
        )

    def test_job_request_uses_validated_song_path(self):
        fake_job = mock.Mock()
        fake_job.to_dict.return_value = {"id": "job"}
        with mock.patch.object(self.app.state.jobs, "submit", return_value=fake_job) as submit:
            response = self.client.post(
                "/api/jobs",
                json={"song_id": "Web Test", "difficulty": 5, "force": True},
            )
        self.assertEqual(response.json(), {"id": "job"})
        submit.assert_called_once_with(self.song.resolve(), 5, True)


if __name__ == "__main__":
    unittest.main()
