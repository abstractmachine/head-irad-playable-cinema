"""On-screen text extraction for film.

Pipeline:
  1. Decode video frames at a configurable sample rate using PyAV.
  2. Run PaddleOCR on each sampled frame — the model both detects text
     regions and reads their content, replacing all handcrafted heuristics.
  3. Discard frames that yield no plausible text (too short or too few
     alphabetic characters).
  4. Merge consecutive frames whose OCR output is similar into temporal
     events with start/end frame and timecode.
  5. Classify each event's ``type`` using position and structural heuristics.
  6. Write results to  data/text/<media_type>/<stem>.csv

CSV schema (one row per bounding-box text event):
  filename, type, start_time, end_time, start_frame, end_frame, bbox, text, language

Each row represents one OCR bounding box tracked through time.  When a
title card contains two spatially separate text regions (e.g. a chapter
number and a caption) they appear as two independent rows that may have
different start/end timecodes.  ``quad`` is stored as
``x1,y1,x2,y2,x3,y3,x4,y4`` (comma-separated pixel integers, four corners
clockwise from top-left; empty string when unavailable).

Storage path: <project_path>/data/text/<media_type>/<stem>.csv
"""

from __future__ import annotations

import bisect
import csv
import re
import unicodedata
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Public constants
# ---------------------------------------------------------------------------

TEXT_COLUMNS: list[str] = [
    "filename",
    "type",
    "ignore",
    "start_time",
    "end_time",
    "start_frame",
    "end_frame",
    "quad",
    "text",
    "score",
    "language",
]

VALID_TYPES: frozenset[str] = frozenset(
    ["meta", "title", "ending", "credits", "intertitle", "diegetic"]
)

# ---------------------------------------------------------------------------
# Timecode helpers (shared with the rest of the tool)
# ---------------------------------------------------------------------------


def frames_to_timecode(frame_number: int, fps: float) -> str:
    """Convert a frame number to HH:MM:SS.mmm string."""
    seconds = frame_number / fps
    return seconds_to_timecode(seconds)


def seconds_to_timecode(seconds: float) -> str:
    """Convert a float seconds value to HH:MM:SS.mmm string."""
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    secs = seconds % 60
    return f"{hours:02d}:{minutes:02d}:{secs:06.3f}"


# ---------------------------------------------------------------------------
# Storage paths
# ---------------------------------------------------------------------------


def get_text_csv_path(
    project_path: str, filename: str, media_type: str = "movies"
) -> Path:
    """Return the canonical path for a film's text-extraction CSV."""
    stem = Path(filename).stem
    return Path(project_path) / "data" / "text" / media_type / f"{stem}.csv"


# ---------------------------------------------------------------------------
# CSV read / write
# ---------------------------------------------------------------------------


def write_text_csv(
    project_path: str,
    filename: str,
    rows: list[dict[str, Any]],
    media_type: str = "movies",
    force: bool = False,
) -> Path:
    """Write text-event rows to the canonical CSV path.

    Args:
        project_path: Absolute path to the project root.
        filename:     Film filename (e.g. ``Fatty And Minnie He Haw (1914) …``).
        rows:         List of dicts matching TEXT_COLUMNS schema.
        media_type:   ``movies`` or ``gameplay``.
        force:        Overwrite existing file if True.

    Returns:
        Path to the written CSV.

    Raises:
        FileExistsError: If the CSV already exists and ``force`` is False.
    """
    dest = get_text_csv_path(project_path, filename, media_type)

    if dest.exists() and not force:
        raise FileExistsError(
            f"Text CSV already exists: {dest}  (use --force to overwrite)"
        )

    dest.parent.mkdir(parents=True, exist_ok=True)

    with dest.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=TEXT_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)

    return dest


def read_text_csv(
    project_path: str, filename: str, media_type: str = "movies"
) -> list[dict[str, Any]]:
    """Read a text-extraction CSV and return rows as dicts."""
    path = get_text_csv_path(project_path, filename, media_type)
    if not path.exists():
        raise FileNotFoundError(f"Text CSV not found: {path}")

    with path.open(newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        return list(reader)


# ---------------------------------------------------------------------------
# OCR engine — PaddleOCR lazy singleton
# ---------------------------------------------------------------------------

_paddleocr_engine = None


def _get_ocr_engine(lang: str = "en"):
    """Return the shared PaddleOCR 3.x engine, initialising it on first call.

    Uses GPU (device="gpu") and the PP-OCRv5 server model by default.
    Disables document preprocessing modules that are irrelevant for video frames.
    """
    global _paddleocr_engine
    if _paddleocr_engine is None:
        import logging, os, sys
        logging.disable(logging.WARNING)
        devnull = open(os.devnull, "w")
        old_stdout, old_stderr = sys.stdout, sys.stderr
        sys.stdout, sys.stderr = devnull, devnull
        try:
            from paddleocr import PaddleOCR
            _paddleocr_engine = PaddleOCR(
                use_doc_orientation_classify=False,
                use_doc_unwarping=False,
                use_textline_orientation=False,
                device="gpu",
                lang=lang,
            )
        finally:
            sys.stdout, sys.stderr = old_stdout, old_stderr
            devnull.close()
            logging.disable(logging.NOTSET)
    return _paddleocr_engine


def _ocr_frame_detections(
    frame_rgb, *, lang: str = "en", min_confidence: float = 0.75
) -> list[dict[str, Any]]:
    """Run PaddleOCR 3.x on a PIL Image and return one dict per bounding box.

    Each dict has:
        text  – recognised string
        score – recognition confidence (0–1)
        quad  – oriented quadrilateral as [x1,y1, x2,y2, x3,y3, x4,y4] pixel
                integers (four corners clockwise from top-left), taken directly
                from ``dt_polys``.  Empty list when the model does not return
                polygon data.

    Only detections with confidence >= min_confidence are returned.
    """
    import numpy as np

    engine = _get_ocr_engine(lang)
    img_array = np.asarray(frame_rgb.convert("RGB"))
    results = engine.predict(img_array)
    detections: list[dict[str, Any]] = []
    for res in results:
        rec_texts = res.get("rec_texts", [])
        rec_scores = res.get("rec_scores", [])
        dt_polys  = res.get("dt_polys",  [])
        n = len(rec_texts)
        scores = rec_scores if len(rec_scores) == n else [None] * n
        polys  = dt_polys   if len(dt_polys)   == n else [None] * n
        for text, score, poly in zip(rec_texts, scores, polys):
            if not text:
                continue
            if score is not None and score < min_confidence:
                continue
            quad: list[int] = []
            if poly is not None:
                pts = np.asarray(poly).reshape(-1, 2)
                quad = [int(v) for pt in pts for v in pt]
            detections.append({"text": text, "score": float(score or 1.0), "quad": quad})
    return detections


def _ocr_frame_raw(frame_rgb, *, lang: str = "en") -> list[tuple[str, float]]:
    """Run PaddleOCR on a PIL Image and return all (text, score) pairs with no filtering.

    Used by calibrate_text_detection to collect a full picture of what the
    engine sees before any threshold is applied.
    """
    import numpy as np

    engine = _get_ocr_engine(lang)
    img_array = np.asarray(frame_rgb.convert("RGB"))
    results = engine.predict(img_array)
    pairs: list[tuple[str, float]] = []
    for res in results:
        rec_texts = res.get("rec_texts", [])
        rec_scores = res.get("rec_scores", [])
        scores = rec_scores if len(rec_scores) == len(rec_texts) else [1.0] * len(rec_texts)
        for text, score in zip(rec_texts, scores):
            if text:
                pairs.append((text, float(score)))
    return pairs


def _text_is_plausible(text: str) -> bool:
    """Return True if the OCR output contains enough real text to keep.

    Short strings (3–5 non-whitespace chars) are accepted only when they are
    purely alphabetic.  This admits short words like "HAL", "by", "with",
    "and" that appear on title cards alongside longer anchor detections, while
    rejecting digit-only or symbol-heavy noise of the same length.

    Longer strings (≥ 6 chars) apply the original tests: at least 40%
    alphanumeric characters and at least one run of 3+ alphanumeric chars.
    """
    stripped = re.sub(r'\s+', '', text)
    if len(stripped) < 3:
        return False
    if len(stripped) < 6:
        return stripped.isalpha()
    alphanum = sum(c.isalpha() or c.isdigit() for c in text)
    if (alphanum / len(text)) < 0.40:
        return False
    return bool(re.search(r"[a-zA-Z0-9]{3,}", text))


def _image_is_blank(frame_rgb) -> bool:
    """Return True for completely black or static frames that can skip OCR.

    This is the only remaining frame-level gate — a trivial optimisation to
    avoid running the neural network on pure black fade frames.
    """
    import numpy as np

    grey = np.asarray(frame_rgb.convert("L"), dtype=np.float32)
    return float(np.std(grey)) < 3.0


def _is_neutral_card(pil_img, quads: list[list[int]]) -> bool:
    """Return True if the frame background is a plain/neutral title card.

    Masks out all text quad regions, then checks two properties of the
    remaining background pixels:

    1. **Low saturation** — the background is achromatic (B&W or colour-film
       title card) or carries a uniform tint (sepia/blue-toned silent film).
       Saturation proxy: max(R,G,B) − min(R,G,B) per pixel (0–255).
       - ``sat.mean() < 20``  → near-perfect greyscale
       - OR ``sat.mean() < 55 and sat.std() < 15``  → uniform tint (sepia/blue)

    2. **Uniform brightness** — the background is a flat surface (card), not a
       complex scene.  Real scenes — even in B&W prints — have significant
       brightness variation (shadows, objects, sky vs. ground).  A title card
       is a flat inked surface:  luminance std < 20.

    Both criteria must be satisfied.  Either alone is insufficient:
    - B&W outdoor scenes pass the saturation test (sat≈0) but fail on
      brightness uniformity (varied shadows/objects → lum_std ≥ 40).
    - Uniformly lit walls pass the uniformity test but can fail on saturation
      in colour film (painted wall with colour cast).

    The brightness uniformity check is applied to the **inner zone** of the
    frame (cropping ~12 % from each side and ~15 % from top/bottom).  This
    excludes decorative border artwork — ornate frames, distributor logos,
    header/footer banners — that some films print around every intertitle card
    (e.g. the Paramount-Arbuckle suitcase frame in *Out West*).  Such borders
    contain white linework on black which inflates the full-frame lum_std to
    35–40 even though the content area is a plain flat card.  Real scene
    backgrounds remain high-variance even within the cropped inner region.
    """
    import numpy as np

    rgb = np.asarray(pil_img.convert("RGB"), dtype=np.float32)
    h, w = rgb.shape[:2]

    # Build mask: True = background pixel (not covered by any text quad)
    mask = np.ones((h, w), dtype=bool)
    for quad in quads:
        if len(quad) == 8:
            pts = np.array(quad, dtype=np.float32).reshape(4, 2)
            cx = float(pts[:, 0].mean())
            cy = float(pts[:, 1].mean())
            # Inflate quad 40 % around centroid to fully cover letter bodies
            pts = ((pts - [cx, cy]) * 1.4 + [cx, cy]).astype(np.int32)
            x1 = int(np.clip(pts[:, 0].min(), 0, w - 1))
            y1 = int(np.clip(pts[:, 1].min(), 0, h - 1))
            x2 = int(np.clip(pts[:, 0].max(), 0, w - 1))
            y2 = int(np.clip(pts[:, 1].max(), 0, h - 1))
            mask[y1:y2, x1:x2] = False

    bg = rgb[mask]
    if bg.shape[0] < 200:
        # Too few background pixels to decide; default False (diegetic is safer)
        return False

    sat = bg.max(axis=1) - bg.min(axis=1)   # 0–255 per-pixel saturation proxy
    sat_mean = float(sat.mean())
    sat_std  = float(sat.std())

    # Criterion 1: near-neutral colour (pure greyscale or uniform tint)
    is_low_sat = sat_mean < 20 or (sat_mean < 55 and sat_std < 15)
    if not is_low_sat:
        return False

    # Criterion 2: uniform brightness — evaluated on the inner zone only so
    # that decorative border artwork around the frame edge does not inflate
    # lum_std and cause genuine title cards to be classified as diegetic.
    bx = int(w * 0.12)
    by = int(h * 0.15)
    inner_mask = mask.copy()
    inner_mask[:by, :]      = False
    inner_mask[h - by:, :]  = False
    inner_mask[:, :bx]      = False
    inner_mask[:, w - bx:]  = False
    bg_inner = rgb[inner_mask]
    # Fall back to full background if the inner zone has too few pixels
    # (very small frames or unusually large text quads covering most of the area).
    if bg_inner.shape[0] < 200:
        bg_inner = bg
    luminance = bg_inner.mean(axis=1)   # approximate lightness per background pixel
    lum_std = float(luminance.std())
    return lum_std < 35


def _normalise_text(text: str) -> str:
    """Normalise OCR output for deduplication/merging.

    - Strip leading/trailing whitespace.
    - Collapse internal whitespace runs.
    - Normalise unicode (NFC).
    """
    text = unicodedata.normalize("NFC", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _texts_are_similar(a: str, b: str) -> bool:
    """Return True if two normalised OCR strings represent the same content.

    Uses word-token overlap coefficient to absorb OCR jitter between
    consecutive frames of the same card.  Pure Jaccard similarity is too
    harsh when OCR produces spurious extra garbage words on only one frame due
    to film grain or scratches; the overlap coefficient (intersection / smaller
    set size) handles that gracefully.

    Tokenisation:
    - Split CamelCase runs (e.g. "FattyArbuckle" → "fatty arbuckle").
    - Keep only tokens of ≥ 3 alphabetic chars to ignore short noise fragments.

    Threshold: overlap ≥ 0.55 — derived empirically from Out West title-card
    frames (0.70) vs scene noise vs title-card comparisons (0.00).
    """
    if a == b:
        return True
    if not a or not b:
        return False

    # Tokenise to lowercase alphabetic words of length ≥ 3.
    # Split CamelCase first (e.g. "FattyArbuckle" → "fatty arbuckle").
    def words(s: str) -> set[str]:
        s2 = re.sub(r"([a-z])([A-Z])", r"\1 \2", s)
        return set(re.findall(r"[a-z]{3,}", s2.lower()))

    set_a = words(a)
    set_b = words(b)

    if not set_a or not set_b:
        # Fall back to short-string length comparison
        return abs(len(a) - len(b)) <= 3

    intersection = len(set_a & set_b)
    smaller = min(len(set_a), len(set_b))

    overlap = intersection / smaller
    return overlap >= 0.55


# ---------------------------------------------------------------------------
# Type classification heuristics
# ---------------------------------------------------------------------------

# Known end-card phrases (normalised to lowercase, no punctuation).
# Any neutral card whose normalised text contains one of these as a whole
# word is classified as "ending" regardless of temporal position.
_END_PHRASES: frozenset[str] = frozenset([
    "the end", "fin", "fine", "ende", "finis", "finito",
    "end", "the end.", "fin.",
])


def _is_end_card(text: str) -> bool:
    """Return True when *text* is (or contains) a known end-of-film phrase.

    Normalised (lowercase, no punctuation) so "THE END", "Fin.", "FINE" etc.
    all match.
    """
    normalised = re.sub(r'[^a-z\s]', '', text.lower()).strip()
    # Exact match of entire text (after normalisation and stripping)
    if normalised in _END_PHRASES:
        return True
    # Also match if any line by itself is an end phrase (multi-line cards)
    for line in normalised.splitlines():
        line = line.strip()
        if line in _END_PHRASES:
            return True
    return False


def _title_matches(text: str, movie_title: str) -> bool:
    """Return True if *text* is similar enough to *movie_title*.

    Both strings are normalised (lowercased, punctuation stripped) before
    comparison.  A SequenceMatcher ratio >= 0.5 counts as a match.
    """
    def _norm(s: str) -> str:
        s = s.lower()
        s = re.sub(r'[^a-z0-9\s]', '', s)
        return ' '.join(s.split())

    a, b = _norm(text), _norm(movie_title)
    if not a or not b:
        return False
    return SequenceMatcher(None, a, b).ratio() >= 0.5


def _classify_type(
    text: str,
    start_frame: int,
    end_frame: int,
    total_frames: int,
    is_bw_card: bool,
    movie_title: str | None = None,
) -> str:
    """Derive a text type from background appearance and temporal position.

    Rules (in priority order):

    1. Non-neutral background (real footage)            → diegetic
       — applies anywhere in the film; WANTED posters, signs, subtitles, etc.
       are always diegetic even if they appear near the opening.

    2. Neutral card + known end phrase                  → ending
       (text match wins over position so films that end early are handled)

    3. Neutral card + last 5 %                          → ending
    4. Neutral card + first 15 % + title match          → title
    5. Neutral card + first/last 18 %                   → credits
    6. Neutral card anywhere in the remaining middle     → intertitle
    """
    if total_frames == 0:
        return "diegetic"

    # Non-neutral background → always diegetic, regardless of position
    if not is_bw_card:
        return "diegetic"

    # Text-based ending detection (position-independent)
    if _is_end_card(text):
        return "ending"

    mid_frame = (start_frame + end_frame) / 2
    position = mid_frame / total_frames  # 0.0 … 1.0

    if position > 0.95:
        return "ending"

    if position < 0.15:
        if movie_title is None or _title_matches(text, movie_title):
            return "title"
        return "credits"

    if position < 0.18 or position > 0.82:
        return "credits"

    return "intertitle"


# ---------------------------------------------------------------------------
# Calibration helper
# ---------------------------------------------------------------------------


def calibrate_text_detection(
    video_path: str,
    expected_strings: list[str],
    *,
    window_seconds: float = 180.0,
    lang: str = "en",
    sample_fps: float = 1.0,
) -> dict[str, Any]:
    """Sweep confidence thresholds to find the optimal setting.

    Runs OCR once on the first ``window_seconds`` of the video at threshold=0
    (collecting all raw detections), then simulates filtering at each threshold
    level without re-running the model.

    For each threshold reports how many expected strings were matched and how
    many total plausible hits remain.

    Args:
        video_path:        Absolute path to the video file.
        expected_strings:  Ground-truth text strings that should appear in the
                           first ``window_seconds`` of the film.
        window_seconds:    How many seconds from the start to analyse.
        lang:              PaddleOCR language code.
        sample_fps:        Frames per second to sample (default 1.0).

    Returns:
        Dict with keys:
            frames_sampled, raw_detection_count, window_seconds, expected,
            thresholds (list of dicts: threshold, total_hits, found, missed).
    """
    import av

    video_path = str(video_path)

    with av.open(video_path) as container:
        stream = container.streams.video[0]
        fps: float = float(stream.average_rate or stream.base_rate or 25.0)

    step_s = 1.0 / sample_fps
    sample_times: list[float] = []
    t = 0.0
    while t <= window_seconds:
        sample_times.append(t)
        t += step_s

    # --- collect all raw (text, score, frame_no) within the window ----------
    raw: list[tuple[str, float, int]] = []
    frames_sampled = 0

    with av.open(video_path) as container:
        stream = container.streams.video[0]
        stream.thread_type = "AUTO"

        for target_s in sample_times:
            target_pts = int(target_s / float(stream.time_base))
            try:
                container.seek(target_pts, stream=stream)
            except av.AVError:
                break

            got_frame = False
            for packet in container.demux(stream):
                if packet.size == 0:
                    break
                for av_frame in packet.decode():
                    if av_frame.pts is None:
                        continue
                    frame_time_s = float(av_frame.pts * stream.time_base)
                    if frame_time_s < target_s - step_s * 0.5:
                        continue
                    frame_no = int(frame_time_s * fps + 0.5)
                    frames_sampled += 1
                    pil_img = av_frame.to_image()
                    if not _image_is_blank(pil_img):
                        for text, score in _ocr_frame_raw(pil_img, lang=lang):
                            raw.append((text, score, frame_no))
                    got_frame = True
                    break
                if got_frame:
                    break

    # --- helper: fuzzy word-overlap match -----------------------------------
    def _match(detected: str, expected: str) -> bool:
        """True if ≥60% of expected words (3+ chars) are found in detected."""
        def words(s: str) -> set[str]:
            return set(re.findall(r"[a-z]{3,}", s.lower()))
        exp_words = words(expected)
        if not exp_words:
            return expected.lower() in detected.lower()
        det_words = words(detected)
        return len(exp_words & det_words) / len(exp_words) >= 0.60

    # --- sweep thresholds ---------------------------------------------------
    thresholds_to_test = [0.50, 0.60, 0.70, 0.75, 0.80, 0.85, 0.90, 0.95]
    threshold_results: list[dict[str, Any]] = []

    for thresh in thresholds_to_test:
        hits = [
            (text, score, fn)
            for text, score, fn in raw
            if score >= thresh and _text_is_plausible(_normalise_text(text))
        ]
        found: list[str] = []
        for exp in expected_strings:
            for text, _score, _fn in hits:
                if _match(text, exp):
                    found.append(exp)
                    break
        threshold_results.append({
            "threshold": thresh,
            "total_hits": len(hits),
            "found": found,
            "missed": [e for e in expected_strings if e not in found],
        })

    return {
        "frames_sampled": frames_sampled,
        "raw_detection_count": len(raw),
        "window_seconds": window_seconds,
        "expected": expected_strings,
        "thresholds": threshold_results,
    }


# ---------------------------------------------------------------------------
# Bounding-box helpers
# ---------------------------------------------------------------------------


def _bbox_iou(a: list[int], b: list[int]) -> float:
    """Intersection-over-Union for two quads or AABB boxes.

    Accepts both 8-value quads (x1,y1,...,x4,y4) and 4-value AABB
    (x1,y1,x2,y2).  Quads are converted to their AABB envelope for
    a fast approximate IoU.

    Returns 0.0 when either box is empty or the boxes do not overlap.
    """
    if not a or not b:
        return 0.0

    def _to_aabb(pts: list[int]) -> tuple[int, int, int, int]:
        xs = pts[0::2]
        ys = pts[1::2]
        return min(xs), min(ys), max(xs), max(ys)

    ax1, ay1, ax2, ay2 = _to_aabb(a)
    bx1, by1, bx2, by2 = _to_aabb(b)
    ix1 = max(ax1, bx1)
    iy1 = max(ay1, by1)
    ix2 = min(ax2, bx2)
    iy2 = min(ay2, by2)
    if ix2 <= ix1 or iy2 <= iy1:
        return 0.0
    inter = (ix2 - ix1) * (iy2 - iy1)
    area_a = (ax2 - ax1) * (ay2 - ay1)
    area_b = (bx2 - bx1) * (by2 - by1)
    union = area_a + area_b - inter
    return inter / union if union > 0 else 0.0


def _merge_box_streams(
    hits: list[dict[str, Any]],
    merge_gap_frames: int,
    sample_frame_step: int,
) -> list[dict[str, Any]]:
    """Track individual bounding-box detections across frames into events.

    Each hit must have: frame_no, timecode, text, bbox, is_card.
    Two hits from different frames are merged into the same event when:
      - they are within ``merge_gap_frames`` sample steps of each other,
      - their text passes ``_texts_are_similar``, AND
      - their bounding boxes have IoU >= 0.20 (or neither carries a bbox,
        in which case text similarity alone is used).

    Returns a list of merged event dicts sorted by start frame, each
    containing: frame_no, timecode, end_frame, end_timecode, text, bbox,
    is_card.
    """
    if not hits:
        return []

    from itertools import groupby as _groupby

    max_gap = merge_gap_frames * sample_frame_step
    active: list[dict[str, Any]] = []
    closed: list[dict[str, Any]] = []

    sorted_hits = sorted(hits, key=lambda h: h["frame_no"])
    for frame_no, group in _groupby(sorted_hits, key=lambda h: h["frame_no"]):
        frame_hits = list(group)

        # Age out streams that are too far back to match anything new.
        still_active: list[dict[str, Any]] = []
        for stream in active:
            if frame_no - stream["end_frame"] > max_gap:
                closed.append(stream)
            else:
                still_active.append(stream)
        active = still_active

        used: set[int] = set()
        new_streams: list[dict[str, Any]] = []
        for hit in frame_hits:
            best_idx, best_iou = None, -1.0
            for i, stream in enumerate(active):
                if i in used:
                    continue
                if not _texts_are_similar(hit["text"], stream["text"]):
                    continue
                iou = _bbox_iou(hit["quad"], stream["quad"])
                # Accept if boxes overlap, or if neither has a quad (text-only).
                qualifies = iou >= 0.20 or (not hit["quad"] and not stream["quad"])
                if qualifies and iou > best_iou:
                    best_iou = iou
                    best_idx = i
            if best_idx is not None:
                stream = active[best_idx]
                stream["end_frame"]    = hit["frame_no"]
                stream["end_timecode"] = hit["timecode"]
                stream["quad"]         = hit["quad"] or stream["quad"]
                if len(hit["text"]) > len(stream["text"]):
                    stream["text"] = hit["text"]
                # Running average of confidence scores across all frames
                prev_score = stream.get("score", hit.get("score", 1.0))
                hit_score  = hit.get("score", 1.0)
                stream["score"] = (prev_score + hit_score) / 2.0
                used.add(best_idx)
            else:
                new_streams.append({
                    "frame_no":     hit["frame_no"],
                    "timecode":     hit["timecode"],
                    "end_frame":    hit["frame_no"],
                    "end_timecode": hit["timecode"],
                    "text":         hit["text"],
                    "quad":         hit["quad"],
                    "score":         hit.get("score", 1.0),
                    "is_card":      hit.get("is_card", True),
                })
        active.extend(new_streams)

    closed.extend(active)
    closed.sort(key=lambda s: s["frame_no"])
    return closed


def _union_quad(quads: list[list[int]]) -> list[int]:
    """Return an AABB quad that encloses all supplied quads.

    Each quad is 8 ints [x1,y1, x2,y2, x3,y3, x4,y4].  The result is the
    axis-aligned bounding box of all corners, returned as a clockwise quad:
    top-left, top-right, bottom-right, bottom-left.  Returns [] if no valid
    quads are supplied.
    """
    all_xs: list[int] = []
    all_ys: list[int] = []
    for q in quads:
        if q and len(q) == 8:
            all_xs.extend(q[0::2])
            all_ys.extend(q[1::2])
    if not all_xs:
        return []
    x1, y1 = min(all_xs), min(all_ys)
    x2, y2 = max(all_xs), max(all_ys)
    return [x1, y1, x2, y1, x2, y2, x1, y2]


def _suppress_contained_events(
    events: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Remove events whose quad is substantially inside a larger concurrent event.

    After the merging and grouping passes, OCR sometimes produces two entries
    for the same card region: one covering the whole card and a smaller one
    that only captured a sub-region (e.g. just the bottom line).  This pass
    removes the smaller event when:

    - It has temporal overlap with the larger event (the two events' frame
      ranges intersect), AND
    - At least 70 % of the smaller event's AABB area lies inside the larger
      event's AABB, AND
    - The smaller event's meaningful words are already present in the larger
      event's text (i.e. it carries no unique information).

    Events with text not found in the containing event are kept — this
    preserves additive detections such as ``$10.000`` alongside ``BLOOD MONEY``
    when OCR produces separate boxes for each line of a title card.

    The larger event is kept unchanged; only the contained duplicate is dropped.
    """
    if len(events) <= 1:
        return events

    def _aabb(quad) -> "tuple[int,int,int,int] | None":
        try:
            vals = [int(v) for v in (quad if isinstance(quad, list) else quad.split(","))]
        except (ValueError, AttributeError):
            return None
        if len(vals) < 8:
            return None
        xs, ys = vals[0::2], vals[1::2]
        return min(xs), min(ys), max(xs), max(ys)

    def _area(box: "tuple[int,int,int,int]") -> int:
        return max(0, box[2] - box[0]) * max(0, box[3] - box[1])

    def _contained_fraction(small: "tuple", big: "tuple") -> float:
        ix1 = max(small[0], big[0])
        iy1 = max(small[1], big[1])
        ix2 = min(small[2], big[2])
        iy2 = min(small[3], big[3])
        if ix2 <= ix1 or iy2 <= iy1:
            return 0.0
        inter = (ix2 - ix1) * (iy2 - iy1)
        small_area = _area(small)
        return inter / small_area if small_area > 0 else 0.0

    def _text_words(text: str) -> set[str]:
        """Lowercase alpha tokens of length ≥ 3 from a text string."""
        return set(re.findall(r"[a-z]{3,}", text.lower()))

    boxes = [_aabb(e.get("quad")) for e in events]
    areas = [_area(b) if b else 0 for b in boxes]
    suppress: set[int] = set()

    for i, ev_a in enumerate(events):
        if i in suppress or boxes[i] is None:
            continue
        a_start = int(ev_a.get("frame_no", 0))
        a_end   = int(ev_a.get("end_frame", a_start))
        words_a = _text_words(ev_a.get("text", ""))
        for j, ev_b in enumerate(events):
            if i == j or j in suppress or boxes[j] is None:
                continue
            if areas[j] <= areas[i]:
                continue  # B must have a strictly larger AABB than A
            b_start = int(ev_b.get("frame_no", 0))
            b_end   = int(ev_b.get("end_frame", b_start))
            # Temporal overlap
            if a_end < b_start or b_end < a_start:
                continue
            # If ≥70 % of A's area is inside B, A is a redundant sub-event —
            # but only if A's meaningful words are already present in B's text.
            # When A has unique words not found in B it carries additive
            # information (e.g. "$10.000" next to "BLOOD MONEY") and must be kept.
            if _contained_fraction(boxes[i], boxes[j]) >= 0.70:
                words_b = _text_words(ev_b.get("text", ""))
                # If A has no extractable alpha words (e.g. purely numeric/
                # symbolic text like "$10.000") treat it as unique and keep it.
                if words_a and words_a.issubset(words_b):
                    suppress.add(i)
                    break

    return [ev for i, ev in enumerate(events) if i not in suppress]


def _suppress_noise_events(
    events: list[dict[str, Any]],
    sample_frame_step: int,
    *,
    min_score: float = 0.60,
    min_alpha_ratio: float = 0.45,
) -> list[dict[str, Any]]:
    """Drop single-sample-frame events that are low-quality diegetic noise.

    A surprising amount of OCR output from in-scene signage is single-frame,
    low-confidence, or heavily garbled (high proportion of non-alphabetic
    chars).  This pass silently removes events that satisfy ALL of:

    - The event spans only one sample step (end_frame - frame_no < 2 * step),
    - The OCR score is below ``min_score`` (default 0.60), OR the text has
      fewer than ``min_alpha_ratio`` (default 45 %) alphabetic characters.
    - The event is NOT a neutral card (i.e. it is ``diegetic`` footage).

    Card events (title, intertitle, credits, ending) are always kept because
    they are valuable even when brief.  Only brief, low-quality diegetic
    fragments are removed.
    """
    if not events:
        return events

    result: list[dict[str, Any]] = []
    for ev in events:
        # Keep all card events regardless of quality
        if ev.get("is_card"):
            result.append(ev)
            continue

        start = int(ev.get("frame_no", 0))
        end   = int(ev.get("end_frame", start))
        span  = end - start

        # Multi-frame events are genuine — keep them
        if span >= 2 * sample_frame_step:
            result.append(ev)
            continue

        # Single-sample-frame diegetic event: apply quality gate
        score = float(ev.get("score", 1.0))
        text  = ev.get("text", "")
        stripped = re.sub(r"\s+", "", text)
        if stripped:
            alpha_ratio = sum(c.isalpha() for c in stripped) / len(stripped)
        else:
            alpha_ratio = 0.0

        if score < min_score or alpha_ratio < min_alpha_ratio:
            continue  # drop garbled/low-confidence single-frame fragment

        result.append(ev)

    return result


def _group_cooccurring_events(
    events: list[dict[str, Any]],
    sample_frame_step: int,
) -> list[dict[str, Any]]:
    """Combine box-stream events that belong to the same visual card.

    After ``_merge_box_streams`` each bounding box is its own event.  Boxes
    that were consistently present on the same card across every sample frame
    will have nearly identical start and end frame numbers.  This function
    finds those groups and merges them into a single multi-line event.

    Two events are co-occurring when:
      |a.frame_no  - b.frame_no | <= sample_frame_step  AND
      |a.end_frame - b.end_frame| <= sample_frame_step

    Within each group the texts are ordered top-to-bottom by the centroid
    Y of their bounding quads (reading order for title cards / credits).
    The stored quad becomes the AABB union of all boxes in the group.
    """
    if not events:
        return []

    n = len(events)
    events = sorted(events, key=lambda e: e["frame_no"])
    tol = sample_frame_step

    # Union-Find for transitive grouping (handles 3+ lines on the same card).
    parent = list(range(n))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(x: int, y: int) -> None:
        parent[find(x)] = find(y)

    for i in range(n):
        ei = events[i]
        for j in range(i + 1, n):
            ej = events[j]
            if ej["frame_no"] - ei["frame_no"] > tol:
                break  # Events are too far apart — and list is sorted, so done.
            if abs(ei["end_frame"] - ej["end_frame"]) <= tol:
                union(i, j)

    from collections import defaultdict
    groups: dict[int, list[int]] = defaultdict(list)
    for i in range(n):
        groups[find(i)].append(i)

    def _center_y(event: dict) -> float:
        q = event.get("quad", [])
        if q and len(q) >= 8:
            return sum(q[1::2]) / (len(q) // 2)
        return float("inf")

    result: list[dict[str, Any]] = []
    for root, members in groups.items():
        group = [events[i] for i in members]
        if len(group) == 1:
            result.append(group[0])
            continue

        # Sort top-to-bottom for natural reading order.
        group.sort(key=_center_y)
        combined_text = "\n".join(e["text"] for e in group if e["text"].strip())
        start_frame = min(e["frame_no"] for e in group)
        end_frame = max(e["end_frame"] for e in group)
        first = min(group, key=lambda e: e["frame_no"])
        last = max(group, key=lambda e: e["end_frame"])

        scores = [e["score"] for e in group if "score" in e]
        mean_score = sum(scores) / len(scores) if scores else 1.0
        result.append({
            "frame_no":     start_frame,
            "timecode":     first["timecode"],
            "end_frame":    end_frame,
            "end_timecode": last["end_timecode"],
            "text":         combined_text,
            "quad":         _union_quad([e["quad"] for e in group if e.get("quad")]),
            "score":        mean_score,
            "is_card":      any(e.get("is_card") for e in group),
        })

    result.sort(key=lambda e: e["frame_no"])
    return result


# ---------------------------------------------------------------------------
# Core extraction pipeline
# ---------------------------------------------------------------------------


def extract_text_events(
    video_path: str,
    *,
    sample_fps: float = 1.0,
    lang: str = "en",
    min_confidence: float = 0.75,
    merge_gap_frames: int = 5,
    verbose: bool = False,
    project_path: str | None = None,
    filename: str | None = None,
    media_type: str = "movies",
    cards_only: bool = True,
) -> list[dict[str, Any]]:
    """Extract all visible on-screen text events from a video file.

    Strategy
    --------
    - Sample at ``sample_fps`` frames per second (default 1 fps).
    - Skip completely blank/black frames (std < 3) to avoid wasting inference.
    - Run PaddleOCR on every other sampled frame — the model detects text
      regions and reads them in one pass, no handcrafted heuristics.
    - Discard frames whose combined OCR output fails a lightweight plausibility
      check (< 5 chars or < 25 % alphabetic).
    - Merge consecutive frames with similar text into single events.
    - Classify each event using temporal position heuristics.
    - When ``cards_only=True`` (default) only non-diegetic events
      (title cards, intertitles, credits, endings) are returned.  Diegetic text
      (signs, props, subtitles embedded in the scene) is silently discarded.
      The OCR confidence score is the sole gate for whether a detection is
      kept at all; ``_is_neutral_card`` only informs type classification, not
      whether hits are collected.
      Pass ``cards_only=False`` to include all detected text.

    Args:
        video_path:        Absolute path to the video file.
        sample_fps:        How many frames per second to sample.  1.0 is a
                           good default for silent films at any print quality.
        lang:              PaddleOCR language code (default ``"en"``).
        merge_gap_frames:  Merge events separated by this many sample steps
                           or fewer.
        verbose:           Print per-frame progress.

    Returns:
        List of dicts with keys matching TEXT_COLUMNS (except ``filename``).
    """
    import av  # PyAV — local import so the service can be imported safely.

    video_path = str(video_path)

    # --- resolve movie title for classification -----------------------------
    movie_title: str | None = None
    if project_path and filename:
        try:
            from services.metadata import get_metadata
            entries = get_metadata(project_path, filename, media_type=media_type)
            if entries:
                movie_title = entries[0].get("title")
        except Exception:
            pass

    # --- probe video metadata -----------------------------------------------
    with av.open(video_path) as container:
        stream = container.streams.video[0]
        fps: float = float(stream.average_rate or stream.base_rate or 25.0)
        total_frames_est: int = stream.frames or 0

        # Estimate duration in seconds from stream metadata or container
        if stream.duration is not None and stream.time_base is not None:
            duration_s: float = float(stream.duration * stream.time_base)
        elif container.duration is not None:
            duration_s = float(container.duration / 1_000_000)
        else:
            duration_s = 0.0

    if total_frames_est == 0 and duration_s > 0:
        total_frames_est = int(duration_s * fps)

    if duration_s == 0 and total_frames_est > 0:
        duration_s = total_frames_est / fps

    # --- build sample timestamps: every 1/sample_fps seconds ---------------
    step_s: float = 1.0 / sample_fps
    sample_times: list[float] = []
    t = 0.0
    while t <= duration_s + step_s:
        sample_times.append(t)
        t += step_s

    # --- first pass: collect OCR hits via direct seeking --------------------
    # Each hit: {frame_no, timecode, text_norm, is_card}
    hits: list[dict[str, Any]] = []

    with av.open(video_path) as container:
        stream = container.streams.video[0]
        stream.thread_type = "AUTO"

        for target_s in sample_times:
            # Seek to the target time (seeks to nearest keyframe at or before)
            target_pts = int(target_s / float(stream.time_base))
            try:
                container.seek(target_pts, stream=stream)
            except av.AVError:
                break

            # Decode forward until we get the first frame at/after the target
            got_frame = False
            for packet in container.demux(stream):
                if packet.size == 0:
                    break
                for av_frame in packet.decode():
                    if av_frame.pts is None:
                        continue

                    frame_time_s = float(av_frame.pts * stream.time_base)

                    # Skip frames that are before our target (seek may land early)
                    if frame_time_s < target_s - step_s * 0.5:
                        continue

                    frame_no = int(frame_time_s * fps + 0.5)

                    pil_img = av_frame.to_image()  # PIL Image, mode RGB

                    # Skip completely blank / black frames — only optimisation.
                    if _image_is_blank(pil_img):
                        got_frame = True
                        break

                    # Anchor+satellite confidence: collect at a lower floor so
                    # smaller/harder tokens on the same title card (e.g. "with",
                    # "GARY" alongside "HUDSON") are kept when at least one box
                    # on the frame clears the normal min_confidence threshold.
                    _SATELLITE_MARGIN = 0.18
                    floor = max(0.50, min_confidence - _SATELLITE_MARGIN)
                    all_dets = _ocr_frame_detections(pil_img, lang=lang, min_confidence=floor)
                    has_anchor = any(d["score"] >= min_confidence for d in all_dets)
                    # Without any high-confidence anchor the frame is treated as
                    # text-free (avoids admitting pure noise at low confidence).
                    detections = (
                        all_dets
                        if has_anchor
                        else [d for d in all_dets if d["score"] >= min_confidence]
                    )

                    got_frame = True

                    tc = frames_to_timecode(frame_no, fps)
                    frame_hits = []
                    plausible_quads: list[list[int]] = []
                    for det in detections:
                        norm = _normalise_text(det["text"])
                        if _text_is_plausible(norm):
                            frame_hits.append({
                                "frame_no": frame_no,
                                "timecode": tc,
                                "text":     norm,
                                "score":    det["score"],
                                "quad":     det["quad"],
                                "is_card":  None,  # filled after quad collection
                            })
                            plausible_quads.append(det["quad"])

                    if not frame_hits:
                        if verbose:
                            print(f"  [t={frame_time_s:.1f}s] skip (no plausible text)")
                        break

                    # Determine whether this frame is a plain neutral card
                    # (title card / intertitle) or real footage, then stamp
                    # every hit from this frame with the result.
                    is_card = _is_neutral_card(pil_img, plausible_quads)
                    for h in frame_hits:
                        h["is_card"] = is_card

                    if verbose:
                        for h in frame_hits:
                            print(f"  [{tc}] f{frame_no}: {h['text'][:60]!r}  quad={h['quad']}")

                    hits.extend(frame_hits)
                    break

                if got_frame:
                    break

    if not hits:
        return []

    # total_frames_est is already set above from probe
    if total_frames_est == 0 and hits:
        total_frames_est = hits[-1]["frame_no"] + int(fps)

    # --- second pass: merge hits using shot boundaries (preferred) ---------
    # Try to load the shotlist so events align with clean shot cuts.
    shots: list[dict] | None = None
    if project_path and filename:
        try:
            from services.shotlist import read_shotlist
            raw_shots = read_shotlist(project_path, filename, media_type)
            shots = [
                s for s in raw_shots
                if s.get("start_frame") is not None and s.get("end_frame") is not None
            ]
            shots.sort(key=lambda s: int(s["start_frame"]))
        except Exception:
            shots = None

    merged: list[dict[str, Any]] = []

    # Frame distance between consecutive samples — used for gap tolerance.
    sample_frame_step: int = max(1, int(fps / sample_fps))

    if shots:
        # Group hits by the shot they fall in; merge box streams within each shot
        # so events never span a shot cut.
        shot_starts = [int(s["start_frame"]) for s in shots]
        shot_hit_groups: dict[int, list[dict]] = {}
        for hit in hits:
            fn = hit["frame_no"]
            idx = bisect.bisect_right(shot_starts, fn) - 1
            if 0 <= idx < len(shots) and fn <= int(shots[idx]["end_frame"]):
                shot_hit_groups.setdefault(idx, []).append(hit)

        for shot_idx in sorted(shot_hit_groups):
            merged.extend(
                _merge_box_streams(
                    shot_hit_groups[shot_idx], merge_gap_frames, sample_frame_step
                )
            )
    else:
        merged = _merge_box_streams(hits, merge_gap_frames, sample_frame_step)

    # --- 2b: combine boxes from the same card into multi-line events -------
    merged = _group_cooccurring_events(merged, sample_frame_step)

    # --- 2c: remove sub-events already covered by a larger sibling ---------
    merged = _suppress_contained_events(merged)

    # --- 2d: drop single-frame low-quality diegetic noise ------------------
    merged = _suppress_noise_events(merged, sample_frame_step)

    # --- third pass: build output rows with type classification ------------
    events: list[dict[str, Any]] = []
    for m in merged:
        event_type = _classify_type(
            text=m["text"],
            start_frame=m["frame_no"],
            end_frame=m["end_frame"],
            total_frames=total_frames_est,
            is_bw_card=m["is_card"],
            movie_title=movie_title,
        )
        events.append(
            {
                "type": event_type,
                "ignore": "",
                "start_time": m["timecode"],
                "end_time": m["end_timecode"],
                "start_frame": m["frame_no"],
                "end_frame": m["end_frame"],
                "quad": ",".join(str(v) for v in m["quad"]) if m.get("quad") else "",
                "text": m["text"],
                "score": f"{m.get('score', 1.0):.3f}",
                "language": lang,
            }
        )

    # When cards_only, discard diegetic events — type classification already
    # used _is_neutral_card to make this determination, so OCR confidence was
    # the collection gate and pixel analysis only informs the type label.
    if cards_only:
        events = [e for e in events if e["type"] != "diegetic"]

    return events


# ---------------------------------------------------------------------------
# List / validate helpers
# ---------------------------------------------------------------------------


def list_text_csvs(
    project_path: str, media_type: str | None = None
) -> list[dict[str, Any]]:
    """Return metadata for all text CSVs on disk.

    Each entry has: filename, media_type, row_count.
    Entries are sorted by filename.
    """
    types_to_check = [media_type] if media_type else ["movies", "gameplay"]
    results: list[dict[str, Any]] = []

    for mtype in types_to_check:
        text_dir = Path(project_path) / "data" / "text" / mtype
        if not text_dir.is_dir():
            continue
        for csv_path in sorted(text_dir.glob("*.csv")):
            try:
                with csv_path.open(newline="", encoding="utf-8") as fh:
                    reader = csv.DictReader(fh)
                    rows = list(reader)
                    row_count = len(rows)
                    type_counts: dict[str, int] = {}
                    for row in rows:
                        t = row.get("type", "unknown")
                        type_counts[t] = type_counts.get(t, 0) + 1
            except Exception:
                row_count = -1
                type_counts = {}
            results.append(
                {
                    "filename": csv_path.stem,
                    "media_type": mtype,
                    "csv_path": str(csv_path),
                    "row_count": row_count,
                    "type_counts": type_counts,
                }
            )

    return results


def validate_text_csvs(
    project_path: str, media_type: str = "movies"
) -> list[dict[str, Any]]:
    """Check all text CSVs for schema compliance.

    Returns a list of issues (dicts with ``csv_path``, ``row``, ``issue``).
    An empty list means everything is valid.
    """
    import datetime

    text_dir = Path(project_path) / "data" / "text" / media_type
    issues: list[dict[str, Any]] = []

    if not text_dir.is_dir():
        return issues

    def _is_timecode(value: str) -> bool:
        try:
            datetime.datetime.strptime(value, "%H:%M:%S.%f")
            return True
        except ValueError:
            return False

    for csv_path in sorted(text_dir.glob("*.csv")):
        try:
            with csv_path.open(newline="", encoding="utf-8") as fh:
                reader = csv.DictReader(fh)
                missing_cols = set(TEXT_COLUMNS) - set(reader.fieldnames or [])
                if missing_cols:
                    issues.append(
                        {
                            "csv_path": str(csv_path),
                            "row": 0,
                            "issue": f"missing columns: {sorted(missing_cols)}",
                        }
                    )
                for i, row in enumerate(reader, start=1):
                    if row.get("type") not in VALID_TYPES:
                        issues.append(
                            {
                                "csv_path": str(csv_path),
                                "row": i,
                                "issue": f"invalid type {row.get('type')!r}",
                            }
                        )
                    for tc_field in ("start_time", "end_time"):
                        val = row.get(tc_field, "")
                        if val and not _is_timecode(val):
                            issues.append(
                                {
                                    "csv_path": str(csv_path),
                                    "row": i,
                                    "issue": f"bad {tc_field} format: {val!r}",
                                }
                            )
                    for fn_field in ("start_frame", "end_frame"):
                        val = row.get(fn_field, "")
                        if val and not val.lstrip("-").isdigit():
                            issues.append(
                                {
                                    "csv_path": str(csv_path),
                                    "row": i,
                                    "issue": f"non-integer {fn_field}: {val!r}",
                                }
                            )
        except Exception as exc:
            issues.append(
                {
                    "csv_path": str(csv_path),
                    "row": 0,
                    "issue": f"could not read file: {exc}",
                }
            )

    return issues
