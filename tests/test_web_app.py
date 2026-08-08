import errno
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fastapi import HTTPException
from fastapi.testclient import TestClient

from mra.difficulty import (legacy_sweep_maidata_path,
                            sweep_maidata_path)
from mra.simai_parser import parse_maidata_content
from mra.sweep_marks import (extract_sweep_difficulty,
                             sweep_maidata_semantically_equal)
from mra.web_app import _atomic_text, _safe_song, create_app


MAIDATA = """&title=Web Test
&artist=Tester
&wholebpm=150
&first=0
&lv_5=13+
&des_5=Chart Author
&inote_5=(150){4}1,2,3,E
"""

MULTI_MAIDATA = MAIDATA + """&lv_6=14+
&des_6=Another Author
&inote_6=(150){4}4,5,6,E
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
        self.assertFalse(song["difficulties"][0]["outputs"]["sweep"])

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
        displayed = self.client.get("/api/songs/Web%20Test/sweep/5").json()["content"]
        marked = displayed.replace("1,2,3", "1/S,2,3")
        with mock.patch(
            "mra.web_app.parse_maidata_content",
            wraps=parse_maidata_content,
        ) as parse_content:
            response = self.client.put(
                "/api/songs/Web%20Test/sweep/5", json={"content": marked},
            )
        self.assertEqual(response.status_code, 200)
        parse_content.assert_any_call(marked)
        saved = response.json()
        self.assertEqual(saved["markers"], 1)
        self.assertEqual(saved["content"], marked)
        self.assertEqual(
            sweep_maidata_path(self.song, 5).read_text(encoding="utf-8"),
            marked,
        )
        self.assertEqual(
            self.client.get("/api/songs/Web%20Test/sweep/5").json()["content"],
            marked,
        )

    def test_each_difficulty_uses_an_independent_sweep_file(self):
        (self.song / "maidata.txt").write_text(MULTI_MAIDATA, encoding="utf-8")

        master = self.client.get("/api/songs/Web%20Test/sweep/5").json()
        remaster = self.client.get("/api/songs/Web%20Test/sweep/6").json()

        self.assertEqual(master["difficulty"], 5)
        self.assertEqual(remaster["difficulty"], 6)
        self.assertEqual(master["source"], "maidata")
        self.assertEqual(remaster["source"], "maidata")
        self.assertIn("&inote_5=", master["content"])
        self.assertNotIn("&inote_6=", master["content"])
        self.assertIn("&inote_6=", remaster["content"])
        self.assertNotIn("&inote_5=", remaster["content"])

        marked = master["content"].replace("1,2,3", "1/S,2,3")
        saved = self.client.put(
            "/api/songs/Web%20Test/sweep/5", json={"content": marked},
        )

        self.assertEqual(saved.status_code, 200)
        self.assertTrue(sweep_maidata_path(self.song, 5).is_file())
        self.assertFalse(sweep_maidata_path(self.song, 6).exists())
        self.assertFalse(legacy_sweep_maidata_path(self.song).exists())
        self.assertEqual(
            self.client.get("/api/songs/Web%20Test/sweep/6").json()["content"],
            remaster["content"],
        )

    def test_sweep_payload_must_match_route_difficulty(self):
        (self.song / "maidata.txt").write_text(MULTI_MAIDATA, encoding="utf-8")

        wrong = self.client.put(
            "/api/songs/Web%20Test/sweep/5",
            json={"content": MULTI_MAIDATA},
        )

        self.assertEqual(wrong.status_code, 422)
        self.assertIn("只包含难度 5", wrong.json()["detail"])
        self.assertEqual(
            self.client.get("/api/songs/Web%20Test/sweep/7").status_code,
            404,
        )
        self.assertEqual(
            self.client.get("/api/songs/Web%20Test/sweep").status_code,
            400,
        )

    def test_legacy_aggregate_is_projected_read_only_then_saved_separately(self):
        (self.song / "maidata.txt").write_text(MULTI_MAIDATA, encoding="utf-8")
        legacy_content = MULTI_MAIDATA.replace(
            "1,2,3,E", "1/S,2,3,E",
        ).replace("4,5,6,E", "4/S,5,6,E")
        legacy_path = legacy_sweep_maidata_path(self.song)
        legacy_path.write_bytes(legacy_content.replace("\n", "\r\n").encode("utf-8"))
        preserved = legacy_path.read_bytes()

        master = self.client.get("/api/songs/Web%20Test/sweep/5").json()

        self.assertFalse(master["exists"])
        self.assertEqual(master["source"], "legacy")
        self.assertIn("1/S,2,3", master["content"])
        self.assertNotIn("&inote_6=", master["content"])
        self.assertTrue(sweep_maidata_semantically_equal(
            extract_sweep_difficulty(legacy_content, 5), master["content"],
        ))
        self.assertEqual(legacy_path.read_bytes(), preserved)

        saved = self.client.put(
            "/api/songs/Web%20Test/sweep/5",
            json={"content": master["content"]},
        )

        self.assertEqual(saved.status_code, 200)
        self.assertTrue(sweep_maidata_path(self.song, 5).is_file())
        self.assertFalse(sweep_maidata_path(self.song, 6).exists())
        self.assertEqual(legacy_path.read_bytes(), preserved)
        remaster = self.client.get("/api/songs/Web%20Test/sweep/6").json()
        self.assertEqual(remaster["source"], "legacy")
        self.assertIn("4/S,5,6", remaster["content"])

    def test_sweep_editor_cannot_change_chart_structure(self):
        changed = MAIDATA.replace("1,2,3,E", "1,2,4,E")
        response = self.client.put(
            "/api/songs/Web%20Test/sweep/5", json={"content": changed},
        )
        self.assertEqual(response.status_code, 422)
        self.assertFalse(sweep_maidata_path(self.song, 5).exists())

    def test_sweep_save_tolerates_temp_cleanup_failure(self):
        # 杀毒软件锁定或沙箱拦截删除时，清理临时文件失败不应使保存失败
        displayed = self.client.get("/api/songs/Web%20Test/sweep/5").json()["content"]
        marked = displayed.replace("1,2,3", "1/S,2,3")
        real_unlink = Path.unlink

        def flaky_unlink(self, *args, **kwargs):
            if self.name.startswith(".maidata_sweep."):
                raise OSError("simulated temp-file lock")
            return real_unlink(self, *args, **kwargs)

        with mock.patch.object(Path, "unlink", flaky_unlink):
            response = self.client.put(
                "/api/songs/Web%20Test/sweep/5", json={"content": marked},
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["markers"], 1)
        self.assertEqual(
            self.client.get("/api/songs/Web%20Test/sweep/5").json()["content"],
            marked,
        )

    def test_sweep_get_reflows_variable_meter_without_writing_source(self):
        coarse = MAIDATA.replace("(150){4}1,2,3,E", "(150){2}1,2,3,E")
        (self.song / "maidata.txt").write_text(coarse, encoding="utf-8")
        meter = {
            "data": {
                "default": "4/4",
                "sections": [
                    {"start_beat": 0, "signature": "4/4"},
                    {"start_beat": 5, "signature": "3/4"},
                ],
            },
        }
        self.assertEqual(
            self.client.put(
                "/api/songs/Web%20Test/meter/5", json=meter,
            ).status_code,
            200,
        )

        response = self.client.get("/api/songs/Web%20Test/sweep/5")

        self.assertEqual(response.status_code, 200)
        content = response.json()["content"]
        self.assertFalse(response.json()["exists"])
        self.assertFalse(sweep_maidata_path(self.song, 5).exists())
        self.assertIn('1,2,\n3{#0.4},\n{#0.4},\nE', content)
        self.assertTrue(sweep_maidata_semantically_equal(coarse, content))

        saved = self.client.put(
            "/api/songs/Web%20Test/sweep/5", json={"content": content},
        )
        self.assertEqual(saved.status_code, 200)
        self.assertEqual(saved.json()["content"], content)
        self.assertEqual(
            sweep_maidata_path(self.song, 5).read_text(encoding="utf-8"),
            content,
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

    def test_atomic_text_retries_transient_replace_lock(self):
        target = self.root / "retry.txt"
        real_replace = os.replace
        attempts = 0

        def flaky_replace(source, destination):
            nonlocal attempts
            attempts += 1
            if attempts < 3:
                raise PermissionError(errno.EACCES, "simulated file lock")
            return real_replace(source, destination)

        with mock.patch("mra.web_app.os.replace", flaky_replace):
            _atomic_text(target, "saved", backup=False)

        self.assertEqual(attempts, 3)
        self.assertEqual(target.read_text(encoding="utf-8"), "saved")

    def test_atomic_text_does_not_reuse_legacy_pid_temp_file(self):
        target = self.root / "fresh.txt"
        legacy_temp = target.with_name(f".{target.name}.{os.getpid()}.tmp")
        legacy_temp.write_text("orphaned", encoding="utf-8")

        _atomic_text(target, "saved", backup=False)

        self.assertEqual(target.read_text(encoding="utf-8"), "saved")
        self.assertEqual(legacy_temp.read_text(encoding="utf-8"), "orphaned")

    def test_sweep_write_error_returns_actionable_status(self):
        marked = MAIDATA.replace("1,2,3,E", "1/S,2,3,E")
        with mock.patch(
            "mra.web_app._atomic_text",
            side_effect=PermissionError(errno.EACCES, "simulated file lock"),
        ):
            response = self.client.put(
                "/api/songs/Web%20Test/sweep/5", json={"content": marked},
            )

        self.assertEqual(response.status_code, 503)
        self.assertIn("请稍后重试", response.json()["detail"])
        self.assertFalse(sweep_maidata_path(self.song, 5).exists())

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
