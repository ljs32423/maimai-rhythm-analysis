import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from mra import ffmpeg_capabilities as ff
from mra.config import DEFAULT_CONFIG, ConfigError, load_config, save_config


class ConfigTests(unittest.TestCase):
    def test_missing_config_uses_defaults(self):
        with tempfile.TemporaryDirectory() as tmp:
            config, warnings = load_config(Path(tmp) / "missing.json")
        self.assertEqual(config["encoder"], "auto")
        self.assertEqual(config["recording"]["fps"], 60)
        self.assertEqual(warnings, [])

    def test_config_is_validated_and_saved_atomically(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            data = {**DEFAULT_CONFIG, "encoder": "h264_nvenc"}
            saved = save_config(data, path)
            loaded, warnings = load_config(path)
        self.assertEqual(saved, loaded)
        self.assertEqual(loaded["encoder"], "h264_nvenc")
        self.assertEqual(warnings, [])

    def test_remote_web_binding_is_rejected(self):
        data = {**DEFAULT_CONFIG, "web": {**DEFAULT_CONFIG["web"], "host": "0.0.0.0"}}
        with self.assertRaises(ConfigError):
            save_config(data, Path("unused.json"))


class EncoderTests(unittest.TestCase):
    def tearDown(self):
        ff.clear_capability_cache()

    def test_auto_selection_prefers_nvenc_then_qsv(self):
        probes = {
            "h264_nvenc": ff.EncoderProbe("h264_nvenc", "NVIDIA NVENC", False, "no gpu"),
            "h264_qsv": ff.EncoderProbe("h264_qsv", "Intel Quick Sync", True),
            "h264_amf": ff.EncoderProbe("h264_amf", "AMD AMF", False, "no amf"),
            "libx264": ff.EncoderProbe("libx264", "CPU Software", True),
        }
        with mock.patch.object(ff, "_probe_encoder", side_effect=lambda _path, codec: probes[codec]), \
             mock.patch.object(ff, "_ffmpeg_version", return_value="7.1"):
            result = ff.detect_capabilities(
                ffmpeg="ffmpeg.exe", ffprobe="ffprobe.exe", preference="auto",
            )
        self.assertEqual(result.selected, "h264_qsv")
        self.assertEqual(result.version, "7.1")

    def test_unavailable_manual_preference_falls_back_to_first_available(self):
        def probe(_path, codec):
            return ff.EncoderProbe(
                codec, ff.ENCODER_NAMES[codec], codec in {"h264_qsv", "libx264"},
            )
        with mock.patch.object(ff, "_probe_encoder", side_effect=probe), \
             mock.patch.object(ff, "_ffmpeg_version", return_value="5.1"):
            result = ff.detect_capabilities(
                ffmpeg="ffmpeg.exe", preference="h264_nvenc",
            )
        self.assertEqual(result.selected, "h264_qsv")

    def test_failed_hardware_command_retries_with_cpu(self):
        capabilities = ff.FFmpegCapabilities(
            ffmpeg="ffmpeg.exe",
            ffprobe=None,
            version="7.1",
            encoders=(),
            selected="h264_nvenc",
        )
        failed = subprocess.CompletedProcess(
            ["ffmpeg", "h264_nvenc"], 1, "", "nvenc failed",
        )
        passed = subprocess.CompletedProcess(
            ["ffmpeg", "libx264"], 0, "", "",
        )
        with mock.patch.object(ff, "detect_capabilities", return_value=capabilities), \
             mock.patch.object(ff.subprocess, "run", side_effect=[failed, passed]) as run:
            result, codec = ff.run_with_fallback(
                lambda selected: ["ffmpeg", selected],
                ffmpeg="ffmpeg.exe",
            )
        self.assertEqual(result.returncode, 0)
        self.assertEqual(codec, "libx264")
        self.assertEqual(run.call_count, 2)

    def test_encoder_arguments_keep_cpu_crf_and_hardware_quality(self):
        self.assertIn("-crf", ff.encoder_args("libx264", "high"))
        self.assertIn("-cq:v", ff.encoder_args("h264_nvenc", "high"))
        self.assertIn("-global_quality", ff.encoder_args("h264_qsv", "high"))


if __name__ == "__main__":
    unittest.main()
