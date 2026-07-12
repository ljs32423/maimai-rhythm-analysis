import unittest
from unittest import mock
import sys

import numpy as np

import mra.align_audio as align_audio
from mra.align_audio import estimate_track_offset


class AudioAlignmentTests(unittest.TestCase):
    def test_full_track_correlation_finds_preview_lead_in(self):
        sr = 1000
        rng = np.random.default_rng(42)
        seconds = 35
        time = np.arange(sr * seconds) / sr
        envelope = 0.25 + 0.55 * (np.sin(time * 0.71) ** 2)
        envelope += 0.2 * (np.sin(time * 0.137 + 0.4) ** 2)
        track = (rng.normal(size=len(time)) * envelope).astype(np.float32)

        expected_offset = 1.37
        lead = np.zeros(int(sr * expected_offset), dtype=np.float32)
        video = np.concatenate([lead, track, np.zeros(sr, dtype=np.float32)])
        video += rng.normal(scale=0.02, size=len(video)).astype(np.float32)

        offset, confidence = estimate_track_offset(video, track, sr)
        self.assertAlmostEqual(offset, expected_offset, delta=0.025)
        self.assertGreater(confidence, 0.5)

    def test_default_cli_prefers_master_and_remaster_when_available(self):
        with mock.patch.object(sys, "argv", ["align_audio.py"]), \
             mock.patch.object(align_audio, "find_song_dirs", return_value=[("song", "song")]), \
             mock.patch.object(
                 align_audio,
                 "find_preview_video",
                 side_effect=lambda _song, difficulty: "preview.mp4" if difficulty in (4, 5, 6) else None,
             ), \
             mock.patch.object(align_audio, "align_song", return_value=1.0) as align_song:
            result = align_audio.main()

        self.assertEqual(result, 0)
        self.assertEqual([call.args[2] for call in align_song.call_args_list], [5, 6])


if __name__ == "__main__":
    unittest.main()
