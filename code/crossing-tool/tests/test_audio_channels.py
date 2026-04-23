import unittest

from services.audio_channels import suggest_audio_channels_mapping


class AudioChannelsTests(unittest.TestCase):
    def test_suggest_mapping_for_mono(self):
        self.assertEqual(suggest_audio_channels_mapping(1), {"mono": 0})

    def test_suggest_mapping_for_stereo_and_multichannel(self):
        self.assertEqual(suggest_audio_channels_mapping(2), {"left": 0, "right": 1})
        self.assertEqual(suggest_audio_channels_mapping(6), {"left": 0, "right": 1})


if __name__ == "__main__":
    unittest.main()
