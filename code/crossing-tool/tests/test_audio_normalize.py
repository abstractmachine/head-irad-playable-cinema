import unittest

from services.audio_normalize import compute_audio_gain_db, parse_integrated_lufs


class AudioNormalizeTests(unittest.TestCase):
    def test_parse_integrated_lufs_from_loudnorm_output(self):
        stderr = """
[Parsed_loudnorm_0 @ 0x55df] {
    \"input_i\" : \"-18.74\",
    \"input_tp\" : \"-1.20\",
    \"input_lra\" : \"5.10\",
    \"input_thresh\" : \"-29.00\"
}
"""
        self.assertAlmostEqual(parse_integrated_lufs(stderr), -18.74)

    def test_compute_audio_gain_db_uses_target_minus_integrated(self):
        gain_db = compute_audio_gain_db(integrated_lufs=-18.74, target_lufs=-23.0)
        self.assertEqual(gain_db, -4.26)

    def test_parse_integrated_lufs_raises_when_missing(self):
        with self.assertRaises(RuntimeError):
            parse_integrated_lufs("no loudnorm payload here")


if __name__ == "__main__":
    unittest.main()
