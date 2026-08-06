"""Tests for generators/_common.py — shared font-loading fallback helper.

Covers:
- load_font_with_fallback: preferred path used when it loads successfully.
- load_font_with_fallback: falls through to FONT_FALLBACKS when preferred
  path(s) are missing/invalid.
- load_font_with_fallback: falls back to ImageFont.load_default() when every
  candidate path fails.
- Each generator's _load_font()/_load_clarendon() still returns a usable
  Pillow font object (regression check after delegating to the shared helper).
"""

import unittest
from unittest.mock import patch

from PIL import ImageFont

from generators._common import FONT_FALLBACKS, FONTS_DIR, load_font_with_fallback

_REAL_BUNDLED_FONT = str(FONTS_DIR / "Hanken_Grotesk" / "HankenGrotesk-VariableFont_wght.ttf")


class TestLoadFontWithFallback(unittest.TestCase):
    def test_preferred_path_used_when_valid(self):
        font = load_font_with_fallback(20, preferred_paths=[_REAL_BUNDLED_FONT])
        self.assertIsInstance(font, ImageFont.FreeTypeFont)

    def test_falls_through_to_fallback_chain(self):
        with patch(
            "generators._common.FONT_FALLBACKS",
            [_REAL_BUNDLED_FONT],
        ):
            font = load_font_with_fallback(20, preferred_paths=["/no/such/font.ttf"])
        self.assertIsInstance(font, ImageFont.FreeTypeFont)

    def test_no_preferred_paths_uses_fallback_chain(self):
        font = load_font_with_fallback(20)
        self.assertIsInstance(font, ImageFont.FreeTypeFont)

    def test_all_paths_fail_falls_back_to_default(self):
        with patch("generators._common.FONT_FALLBACKS", ["/no/such/font.ttf"]):
            font = load_font_with_fallback(20, preferred_paths=["/also/missing.ttf"])
        # ImageFont.load_default() should succeed in any environment with Pillow installed.
        self.assertIsNotNone(font)

    def test_total_failure_returns_none(self):
        with patch("generators._common.FONT_FALLBACKS", ["/no/such/font.ttf"]), \
             patch("generators._common.ImageFont.load_default", side_effect=Exception("boom")):
            font = load_font_with_fallback(20, preferred_paths=["/also/missing.ttf"])
        self.assertIsNone(font)

    def test_fallback_chain_is_the_documented_paths(self):
        self.assertEqual(FONT_FALLBACKS[0], _REAL_BUNDLED_FONT)


class TestGeneratorFontLoadersDelegate(unittest.TestCase):
    def test_cloud_load_font_returns_font(self):
        from generators.cloud import _load_font

        self.assertIsNotNone(_load_font(20))

    def test_flipbook_load_font_returns_font(self):
        from generators.flipbook import _FONT_REGULAR, _load_font

        self.assertIsNotNone(_load_font(20, _FONT_REGULAR))

    def test_mosaic_load_font_returns_font(self):
        from generators.mosaic import _load_font

        self.assertIsNotNone(_load_font(20))

    def test_mosaic_load_clarendon_returns_font(self):
        from generators.mosaic import _load_clarendon

        self.assertIsNotNone(_load_clarendon(True, 20))
        self.assertIsNotNone(_load_clarendon(False, 20))


if __name__ == "__main__":
    unittest.main()
