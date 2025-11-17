"""
Poster generation (keeps image/text logic here).
"""
from PIL import Image, ImageDraw, ImageOps
import font

def render_poster(width_cm=68.6, height_cm=101.6, dpi=300, color=(255,255,255),
                  title=None, credits=None, font_path=None, background=None):
    """
    Return a PIL.Image of the poster.
    - background: optional PIL.Image to use as the poster background (will be
      fit/covered to poster size). If None, a solid color is used.
    """
    w = font.cm_to_px(width_cm, dpi)
    h = font.cm_to_px(height_cm, dpi)

    if background is not None:
        bg = background.convert("RGB").copy()
        bg = ImageOps.fit(bg, (w, h), Image.LANCZOS)
        img = bg
    else:
        img = Image.new("RGB", (w, h), color)

    draw = ImageDraw.Draw(img)

    if title:
        title_size = max(20, int(h * 0.08))
        fnt = font.load_font(title_size, font_path)
        bbox = draw.textbbox((0, 0), title, font=fnt)
        text_w, text_h = bbox[2] - bbox[0], bbox[3] - bbox[1]
        x = (w - text_w) // 2
        y = int(h * 0.08)
        draw.text((x, y), title, fill=(0,0,0), font=fnt)

    if credits:
        cred_size = max(12, int(h * 0.03))
        fnt = font.load_font(cred_size, font_path)
        bbox = draw.textbbox((0, 0), credits, font=fnt)
        text_w, text_h = bbox[2] - bbox[0], bbox[3] - bbox[1]
        x = (w - text_w) // 2
        y = h - text_h - int(h * 0.04)
        draw.text((x, y), credits, fill=(0,0,0), font=fnt)

    return img

def make_poster(outfile="poster.pdf", width_cm=68.6, height_cm=101.6, dpi=300,
                color=(255,255,255), title=None, credits=None, font_path=None,
                background=None):
    """Render and save poster to a PDF file. background may be a PIL.Image."""
    img = render_poster(width_cm=width_cm, height_cm=height_cm, dpi=dpi,
                        color=color, title=title, credits=credits,
                        font_path=font_path, background=background)
    img.save(outfile, "PDF", resolution=dpi)