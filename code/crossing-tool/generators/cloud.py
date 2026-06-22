"""Cloud generator — builds a word-cloud PDF from annotation text.

Pipeline:
    1. ``extract_annotation_words()`` — load annotation JSON(s), tokenise, count.
    2. ``render_cloud()``             — lay out words with size ∝ frequency, save PDF/PNG.
    3. ``cloud_from_annotations()``   — convenience wrapper used by both CLI and UI.

Example::

    from generators.cloud import cloud_from_annotations

    out = cloud_from_annotations(
        project_path,
        scope=None,          # None → all movies
        field="description",
        media_type="movie",
    )
    print("Saved to:", out)
"""

from __future__ import annotations

import json
import math
import random
import re
import unicodedata
from collections import Counter
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image, ImageDraw, ImageFont


# ---------------------------------------------------------------------------
# Stopwords (basic English set, extended with common annotation noise words)
# ---------------------------------------------------------------------------

_STOPWORDS: frozenset[str] = frozenset("""
a an the and or but of in on at to for with by from as is was are were be
been being have has had do does did will would could should may might shall
can it its this that these those i me my we our you your he she him her his
they them their what which who whom when where why how all any each every more
most other some such no not only same so than too very just into over after
before out up about against between through during here there now then again
once there s t d m re ve ll also not no none neither nor even though because
although while both either another one two three four five few many much some
very quite rather still yet however moreover furthermore consequently therefore
throughout across along within without toward towards upon besides despite
hence thus also indeed besides furthermore accordingly meanwhile subsequently
two three four five six seven eight nine ten several multiple various certain
particular specific different new old large small big little high low long
short wide narrow deep shallow bright dark light heavy light full empty open
closed visible appears appears shown showing scene shot camera view looking
toward towards away near far left right top bottom front back center middle
inside outside around
""".split())


# ---------------------------------------------------------------------------
# Font / visual constants
# ---------------------------------------------------------------------------

_FONTS_DIR = Path(__file__).parent.parent / "styles" / "fonts"
_LC = _FONTS_DIR / "libre_clarendon" / "fonts"

# Libre Clarendon Normal — weight chosen per font size for visual impact.
# The number in each filename is the designed weight value (not the pixel size).
_LC_BY_WEIGHT = [
    (24,  str(_LC / "LibreClarendonNormal-42Light.otf")),   # size ≤ 24 → Light
    (50,  str(_LC / "LibreClarendonNormal-68Regular.otf")),  # size ≤ 50 → Regular
    (80,  str(_LC / "LibreClarendonNormal-110Medium.otf")),  # size ≤ 80 → Medium
    (100, str(_LC / "LibreClarendonNormal-162Bold.otf")),    # size ≤ 100 → Bold
    (999, str(_LC / "LibreClarendonNormal-212Black.otf")),   # size  > 100 → Black
]

# Fallback chain used when Libre Clarendon files are not present
_FONT_FALLBACKS = [
    str(_FONTS_DIR / "Hanken_Grotesk" / "HankenGrotesk-VariableFont_wght.ttf"),
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    "/usr/share/fonts/truetype/freefont/FreeSans.ttf",
    "/usr/share/fonts/TTF/DejaVuSans.ttf",
]

_BG_COLOR = (18, 18, 18)
_PALETTE = [
    (255, 200, 100),   # warm yellow
    (255, 150,  60),   # orange
    (255, 110, 110),   # salmon
    (140, 210, 255),   # sky blue
    (120, 210, 150),   # sage green
    (210, 170, 255),   # lavender
    (255, 255, 255),   # white
    (200, 200, 200),   # light grey
    (255, 230, 160),   # pale gold
    (170, 240, 200),   # mint
]

# ---------------------------------------------------------------------------
# Style presets
# ---------------------------------------------------------------------------

DEFAULT_STYLE   = "default"
PREFS_KEY_STYLE = "cloud_style"

_STYLES_DIR = Path(__file__).parent.parent / "preferences" / "styles"


def _to_rgb(value) -> tuple:
    """Extract an RGB tuple from a bare ``[R, G, B]`` list or a
    ``{"label": ..., "rgb": [R, G, B]}`` dict (new labelled format)."""
    if isinstance(value, dict):
        return tuple(value["rgb"])
    return tuple(value)


def _load_styles() -> dict[str, dict]:
    """Load style presets from ``preferences/styles/*.json``.

    The built-in ``default`` style is always available.  Additional presets
    are discovered by scanning the directory; each JSON file must contain
    ``background`` and ``palette``.  Both bare ``[R, G, B]`` arrays and the
    labelled ``{"label": ..., "rgb": [R, G, B]}`` format are accepted.
    """
    built_in: dict[str, dict] = {
        "default": {
            "background": _BG_COLOR,
            "palette":    _PALETTE,
        },
    }
    if not _STYLES_DIR.is_dir():
        return built_in

    for path in sorted(_STYLES_DIR.glob("*.json")):
        name = path.stem
        try:
            data = json.loads(path.read_text())
            built_in[name] = {
                "background": _to_rgb(data["background"]),
                "palette":    [_to_rgb(c) for c in data["palette"]],
            }
        except Exception:
            pass  # malformed file — skip silently

    return built_in


STYLES     = _load_styles()
STYLE_NAMES = list(STYLES.keys())


def reload_styles() -> None:
    """Re-read all style presets from ``preferences/styles/*.json``.

    Call this after editing a style JSON file to apply the changes to the
    current session without restarting the visualizer.
    """
    global STYLES, STYLE_NAMES
    STYLES = _load_styles()
    STYLE_NAMES = list(STYLES.keys())


def get_style_path(name: str) -> "Path | None":
    """Return the JSON file path for a named style preset.

    Returns ``None`` for built-in styles that have no backing file
    (e.g. ``"default"``).
    """
    p = _STYLES_DIR / f"{name}.json"
    return p if p.exists() else None


# Font-size range for word rendering
_MAX_FONT_SIZE = 120
_MIN_FONT_SIZE = 14

# Maximum words to include in the cloud
_MAX_WORDS = 150

# Canvas size used when no explicit dimensions are given
_DEFAULT_WIDTH  = 1200
_DEFAULT_HEIGHT = 840


# ---------------------------------------------------------------------------
# Font helper
# ---------------------------------------------------------------------------

def _load_font(size: int) -> Any:
    # Pick the Libre Clarendon weight appropriate for this size
    for threshold, path in _LC_BY_WEIGHT:
        if size <= threshold:
            try:
                return ImageFont.truetype(path, size)
            except (IOError, OSError):
                break  # file missing — fall through to fallbacks
    for path in _FONT_FALLBACKS:
        try:
            return ImageFont.truetype(path, size)
        except (IOError, OSError):
            continue
    try:
        return ImageFont.load_default()
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Tokenisation & counting
# ---------------------------------------------------------------------------

_TOKEN_RE = re.compile(r"[a-z][a-z'\-]*[a-z]|[a-z]", re.UNICODE)


def _tokenize(text: str) -> list[str]:
    """Lowercase, strip punctuation, remove stopwords; return clean tokens."""
    # Normalise unicode to ASCII-friendly form (curly quotes → straight, etc.)
    text = unicodedata.normalize("NFKD", text)
    return [
        tok
        for tok in _TOKEN_RE.findall(text.lower())
        if tok not in _STOPWORDS and len(tok) >= 3
    ]


def _iter_annotation_text(entries: list, field: str | None):
    """Yield text strings from annotation entries.

    *field* restricts extraction to one annotation field; ``None`` reads all
    fields whose values are strings or lists.

    Special case: ``field='motif'`` reads ``shot['motif']`` (a top-level shot
    key) rather than a key inside ``shot['annotation']``.
    """
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        shot = entry.get("shot")
        if not isinstance(shot, dict):
            continue

        # 'motif' is stored directly on the shot dict, not in annotation
        if field == "motif":
            val = shot.get("motif")
            if isinstance(val, str) and val.strip():
                yield val
            continue

        ann = shot.get("annotation")
        if not isinstance(ann, dict):
            continue

        if field is not None:
            val = ann.get(field)
            if isinstance(val, str):
                yield val
            elif isinstance(val, list):
                yield " ".join(str(v) for v in val)
        else:
            # Include motif alongside annotation fields when aggregating all
            motif_val = shot.get("motif")
            if isinstance(motif_val, str) and motif_val.strip():
                yield motif_val
            for v in ann.values():
                if isinstance(v, str):
                    yield v
                elif isinstance(v, list):
                    yield " ".join(str(x) for x in v)


def extract_annotation_words(
    project_path: str,
    *,
    scope: str | None = None,
    field: str | None = None,
    media_type: str = "movie",
    min_count: int = 1,
) -> Counter:
    """Count word frequencies from annotation JSON files.

    Args:
        project_path: Project root directory.
        scope:        Movie title / id substring to restrict to; ``None`` → all.
        field:        Annotation field name (e.g. ``"description"``); ``None``
                      → aggregate all text-bearing fields.
        media_type:   ``"movie"`` or ``"gameplay"`` (default: ``"movie"``).
        min_count:    Minimum occurrence count to retain a word.

    Returns:
        ``Counter`` mapping word → count (filtered by *min_count*).

    Raises:
        FileNotFoundError: If the annotation directory or matched file is absent.
    """
    ann_dir = (
        Path(project_path) / "data" / "annotations" / "shots" / media_type
    )
    if not ann_dir.exists():
        raise FileNotFoundError(
            f"Annotation directory not found: {ann_dir}\n"
            "  Run 'crossing annotate shot --all' to generate annotations."
        )

    # Collect JSON files to process
    if scope:
        scope_lower = scope.lower()
        json_files = [
            p for p in sorted(ann_dir.glob("*.json"))
            if scope_lower in p.stem.lower()
        ]
        if not json_files:
            raise FileNotFoundError(
                f"No annotation file matching {scope!r} found in {ann_dir}."
            )
    else:
        json_files = sorted(ann_dir.glob("*.json"))
        if not json_files:
            raise FileNotFoundError(
                f"No annotation files found in {ann_dir}."
            )

    counter: Counter = Counter()
    for json_path in json_files:
        try:
            raw = json_path.read_text(encoding="utf-8")
            entries = json.loads(raw)
        except Exception:
            continue
        if not isinstance(entries, list):
            continue
        for text in _iter_annotation_text(entries, field):
            counter.update(_tokenize(text))

    if min_count > 1:
        counter = Counter({w: c for w, c in counter.items() if c >= min_count})

    return counter


# ---------------------------------------------------------------------------
# Rendering helpers
# ---------------------------------------------------------------------------

def _font_size_for(count: int, max_count: int, min_count: int) -> int:
    """Map a word count to a font size using logarithmic scaling."""
    if max_count <= min_count:
        return (_MAX_FONT_SIZE + _MIN_FONT_SIZE) // 2
    log_lo = math.log1p(min_count)
    log_hi = math.log1p(max_count)
    log_val = math.log1p(count)
    t = (log_val - log_lo) / (log_hi - log_lo)
    return int(_MIN_FONT_SIZE + t * (_MAX_FONT_SIZE - _MIN_FONT_SIZE))


def _measure_text(font: Any, text: str) -> tuple[int, int, int, int]:
    """Return (x0, y0, x1, y1) bounding box of *text* rendered with *font*."""
    probe = Image.new("L", (1, 1))
    d = ImageDraw.Draw(probe)
    try:
        return d.textbbox((0, 0), text, font=font)
    except AttributeError:
        # Pillow < 9 fallback
        w, h = d.textsize(text, font=font)  # type: ignore[attr-defined]
        return (0, 0, w, h)


# ---------------------------------------------------------------------------
# Main renderer
# ---------------------------------------------------------------------------

def render_cloud(
    words: Counter,
    output_path: Path,
    *,
    width: int = _DEFAULT_WIDTH,
    height: int = _DEFAULT_HEIGHT,
    max_words: int = _MAX_WORDS,
    background_color: tuple = _BG_COLOR,
    palette: list | None = None,
    title: str | None = None,
    seed: int = 42,
) -> Path:
    """Render a word cloud and save it as a PDF (or PNG).

    Words are placed along an Archimedean spiral starting from the canvas
    centre.  Size is proportional to log-frequency.  Each word gets a random
    colour drawn from the built-in palette.  No word overlaps are allowed.

    Args:
        words:            ``Counter`` of word → count.
        output_path:      Destination path.  ``.pdf`` extension → PDF output;
                          any other extension → the format Pillow infers.
        width:            Canvas width in pixels (default 1200).
        height:           Canvas height in pixels (default 840).
        max_words:        Maximum number of words to include (default 150).
        background_color: RGB tuple for the canvas background.
        title:            Optional subtitle line drawn in the bottom-left.
        seed:             Random seed for reproducible colour / angle selection.

    Returns:
        *output_path* after saving.

    Raises:
        ValueError: If *words* is empty, or no word could be placed.
    """
    if not words:
        raise ValueError("No words to render — counter is empty.")

    top_words = words.most_common(max_words)
    max_count = top_words[0][1]
    min_count = top_words[-1][1]

    rng = random.Random(seed)

    active_palette = palette if palette is not None else _PALETTE

    # Occupancy bitmap: True = pixel already used
    occupied: np.ndarray = np.zeros((height, width), dtype=bool)

    canvas = Image.new("RGB", (width, height), color=background_color)
    draw = ImageDraw.Draw(canvas)

    cx = width // 2
    cy = height // 2
    placed = 0

    for word, count in top_words:
        size = _font_size_for(count, max_count, min_count)
        font = _load_font(size)
        if font is None:
            continue

        bbox = _measure_text(font, word)
        tw = bbox[2] - bbox[0]
        th = bbox[3] - bbox[1]
        if tw <= 0 or th <= 0:
            continue

        color = rng.choice(active_palette)

        # Per-word Archimedean spiral search: start at a random angle and
        # step outward.  The radial step is scaled to the word's diagonal so
        # that bigger words jump outward faster.
        word_diag = math.hypot(tw, th)
        radial_step = max(0.15, word_diag / max(width, height) * 0.8)
        angle = rng.uniform(0, 2 * math.pi)
        pad = 3  # px padding around each word bounding box

        placed_ok = False
        for i in range(4000):
            r = radial_step * i
            angle += 0.3 + rng.gauss(0, 0.05)  # slight jitter avoids grid artefacts

            x = int(cx + r * math.cos(angle)) - tw // 2
            y = int(cy + r * math.sin(angle)) - th // 2

            # Canvas bounds check (with padding)
            if x - pad < 0 or y - pad < 0:
                continue
            if x + tw + pad > width or y + th + pad > height:
                continue

            # Collision check via numpy slice
            x0 = max(0, x - pad)
            y0 = max(0, y - pad)
            x1 = min(width,  x + tw + pad)
            y1 = min(height, y + th + pad)
            if occupied[y0:y1, x0:x1].any():
                continue

            # Place word and mark region occupied
            draw.text(
                (x - bbox[0], y - bbox[1]),
                word,
                font=font,
                fill=color,
            )
            occupied[y0:y1, x0:x1] = True
            placed += 1
            placed_ok = True
            break

        # Words that don't fit after max iterations are silently skipped.

    if placed == 0:
        raise ValueError(
            "Could not place any words on the canvas.  "
            "Try increasing canvas size or reducing min-count."
        )

    output_path.parent.mkdir(parents=True, exist_ok=True)

    fmt = output_path.suffix.lower()
    if fmt == ".pdf":
        import datetime as _dt
        pdfinfo = {
            "title":    title or "",
            "author":   "Crossing Tool",
            "subject":  title or "",
            "keywords": f"word cloud, {title or ''}",
            "creator":  "generators.cloud",
            "creationdate": _dt.datetime.now(),
        }
        canvas.save(str(output_path), "PDF", resolution=150, pdfinfo=pdfinfo)
    else:
        canvas.save(str(output_path))

    return output_path, canvas


# ---------------------------------------------------------------------------
# Shared entry point (CLI + UI)
# ---------------------------------------------------------------------------

def cloud_from_annotations(
    project_path: str,
    *,
    scope: str | None = None,
    field: str | None = None,
    media_type: str = "movie",
    output_path: "str | Path | None" = None,
    max_words: int = _MAX_WORDS,
    min_count: int = 2,
    width: int = _DEFAULT_WIDTH,
    height: int = _DEFAULT_HEIGHT,
    style: str = DEFAULT_STYLE,
) -> Path:
    """Build a word-cloud PDF from project annotation data.

    This is the **single shared entry point** used by both the CLI and the
    visualizer UI.  Neither path re-implements the counting or rendering logic.

    Args:
        project_path: Project root directory.
        scope:        Movie title / id substring; ``None`` → all movies.
        field:        Annotation field to read; ``None`` → all fields.
        media_type:   ``"movie"`` or ``"gameplay"`` (default: ``"movie"``).
        output_path:  Destination file path.  Auto-generated under
                      ``output/clouds/`` when not provided.
        max_words:    Maximum words to render (default 150).
        min_count:    Minimum word occurrences to include (default 2).
        width:        Canvas width in pixels (default 1200).
        height:       Canvas height in pixels (default 840).

    Returns:
        Path to the saved PDF.

    Raises:
        FileNotFoundError: If annotation data is absent.
        ValueError:        If no words pass the filters.
    """
    import datetime

    word_counts = extract_annotation_words(
        project_path,
        scope=scope,
        field=field,
        media_type=media_type,
        min_count=min_count,
    )

    if not word_counts:
        raise ValueError(
            "No words found after filtering.  "
            "Try lowering --min-count or verify annotation files exist."
        )

    # Resolve style preset
    preset = STYLES.get(style) or STYLES[DEFAULT_STYLE]

    # Build readable subtitle stored only in PDF metadata (not drawn)
    scope_label = scope or f"{media_type} (all)"
    field_label = field or "all fields"
    title_str = f"{scope_label}  ·  {field_label}"

    if output_path is None:
        stamp = datetime.datetime.now().strftime("%Y-%m-%d-%H-%M-%S")
        scope_slug = re.sub(r"[^\w]+", "-", scope or media_type).strip("-")
        field_slug = re.sub(r"[^\w]+", "-", field or "all").strip("-")
        output_path = (
            Path(project_path) / "output" / "clouds"
            / f"{scope_slug}-{field_slug}-cloud-{stamp}.pdf"
        )
    else:
        output_path = Path(output_path)

    path, canvas = render_cloud(
        word_counts,
        output_path,
        width=width,
        height=height,
        max_words=max_words,
        background_color=preset["background"],
        palette=preset["palette"],
        title=title_str,
    )
    return path, canvas
