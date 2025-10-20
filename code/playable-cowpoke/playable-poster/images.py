"""
Load random frames from N films and compose a grid image sized to the poster.

Notes:
- Requires ffmpeg/ffprobe on PATH to extract frames.
- Functions return a PIL.Image sized to the poster in the given dpi.
"""
import os
import random
import math
import subprocess
from io import BytesIO
from PIL import Image, ImageOps
import font

MOVIES_DIR = "/Volumes/PLAYABLE-D/project/movies/"

def _list_movies(folder=MOVIES_DIR, ext=".mp4"):
    try:
        names = [f for f in os.listdir(folder) if f.lower().endswith(ext)]
        return [os.path.join(folder, f) for f in sorted(names)]
    except Exception:
        return []

def _get_duration(path):
    try:
        out = subprocess.check_output([
            "ffprobe", "-v", "error", "-select_streams", "v:0",
            "-show_entries", "format=duration", "-of",
            "default=noprint_wrappers=1:nokey=1", path
        ], stderr=subprocess.DEVNULL)
        return float(out.strip())
    except Exception:
        return None

def _extract_frame_png(path, t_seconds):
    """
    Return PNG bytes of a single frame at time t_seconds using ffmpeg piping.
    """
    try:
        proc = subprocess.run([
            "ffmpeg", "-ss", str(t_seconds), "-i", path,
            "-frames:v", "1", "-f", "image2pipe", "-vcodec", "png", "-"
        ], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, check=True)
        return proc.stdout
    except Exception:
        return None

def grid_from_movies(num=16, cols=4, width_cm=68.6, height_cm=101.6, dpi=300, folder=MOVIES_DIR):
    """
    Build and return a PIL.Image composed of `num` random frames arranged in a grid.
    Image size is width_cm x height_cm at given dpi.
    """
    files = _list_movies(folder)
    if not files:
        # return an empty white poster-sized image as graceful fallback
        w = font.cm_to_px(width_cm, dpi)
        h = font.cm_to_px(height_cm, dpi)
        return Image.new("RGB", (w, h), (255, 255, 255))

    w = font.cm_to_px(width_cm, dpi)
    h = font.cm_to_px(height_cm, dpi)

    rows = math.ceil(num / cols)
    cell_w = w // cols
    cell_h = h // rows

    canvas = Image.new("RGB", (w, h), (255, 255, 255))

    # choose with replacement only if not enough files
    if len(files) >= num:
        chosen = random.sample(files, num)
    else:
        chosen = [random.choice(files) for _ in range(num)]

    for idx, path in enumerate(chosen):
        dur = _get_duration(path)
        if dur and dur > 2:
            t = random.uniform(0.25 * dur, 0.75 * dur)
        elif dur:
            t = dur / 2.0
        else:
            t = 1.0

        png = _extract_frame_png(path, t)
        if png:
            try:
                im = Image.open(BytesIO(png)).convert("RGB")
            except Exception:
                im = None
        else:
            im = None

        if im is None:
            im = Image.new("RGB", (cell_w, cell_h), (200, 200, 200))

        im = ImageOps.fit(im, (cell_w, cell_h), Image.LANCZOS)

        row = idx // cols
        col = idx % cols
        x = col * cell_w
        y = row * cell_h
        canvas.paste(im, (x, y))

    return canvas

def make_poster_from_movies(outfile="poster_frames.pdf", num=16, cols=4, width_cm=68.6, height_cm=101.6, dpi=300, folder=MOVIES_DIR):
    img = grid_from_movies(num=num, cols=cols, width_cm=width_cm, height_cm=height_cm, dpi=dpi, folder=folder)
    img.save(outfile, "PDF", resolution=dpi)