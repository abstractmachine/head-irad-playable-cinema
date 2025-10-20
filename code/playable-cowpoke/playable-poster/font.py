"""
Font and unit helpers.
"""
import os
from PIL import ImageFont

def cm_to_px(cm, dpi=300):
    return int(cm / 2.54 * dpi)

def load_font(size, font_path=None):
    if font_path and os.path.exists(font_path):
        try:
            return ImageFont.truetype(font_path, size)
        except Exception:
            pass
    for f in ("/Library/Fonts/Helvetica.ttc", "/Library/Fonts/Arial.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"):
        if os.path.exists(f):
            try:
                return ImageFont.truetype(f, size)
            except Exception:
                continue
    return ImageFont.load_default()