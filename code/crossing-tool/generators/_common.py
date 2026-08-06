"""Shared helpers for the generators package.

Currently holds the font-loading fallback chain used by ``cloud.py``,
``flipbook.py`` and ``mosaic.py``.  Extracted because all three generators
implemented the same "try candidate paths, then Pillow's built-in default
font" logic independently.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from PIL import ImageFont

# Directory holding bundled project fonts (Libre Clarendon, Hanken Grotesk).
FONTS_DIR = Path(__file__).parent.parent / "styles" / "fonts"

# Fallback chain used when a generator's preferred font file is not present:
# bundled Hanken Grotesk first, then a handful of common system font paths.
FONT_FALLBACKS = [
    str(FONTS_DIR / "Hanken_Grotesk" / "HankenGrotesk-VariableFont_wght.ttf"),
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    "/usr/share/fonts/truetype/freefont/FreeSans.ttf",
    "/usr/share/fonts/TTF/DejaVuSans.ttf",
]


def load_font_with_fallback(size: int, preferred_paths: "list[str] | None" = None) -> Any:
    """Load a Pillow font at *size*, falling back gracefully.

    Tries each path in *preferred_paths* (if given) first, then the
    bundled/system ``FONT_FALLBACKS`` chain, then Pillow's built-in default
    font.  Returns ``None`` only if even ``ImageFont.load_default()`` raises.
    """
    for path in list(preferred_paths or []) + FONT_FALLBACKS:
        try:
            return ImageFont.truetype(path, size)
        except (IOError, OSError):
            continue
    try:
        return ImageFont.load_default()
    except Exception:
        return None
