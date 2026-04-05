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
    "start_time",
    "end_time",
    "start_frame",
    "end_frame",
    "quad",
    "text",
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

    Three checks:
    - At least 6 characters total (passes "THE END", "FIN", short titles).
    - At least 40% of characters are alphanumeric (allows currency / numbers
      like "$10.000" while still filtering symbol-only noise).
    - At least one run of 3+ consecutive alphanumeric characters.
    """
    if len(text) < 6:
        return False
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
    """Derive a text type from position and structural cues.

    Rules (in priority order):
    1. First 10 % of film + title-card + title match  → title
    1b. First 10 % of film + title-card + no match    → credits
    2. Last 5 % of film + title-card appearance       → ending
    3. Last 20 % of film + multi-line / names         → credits
    4. First 20 % of film + multi-line / names        → credits
    5. B&W card anywhere in middle                    → intertitle
    6. Fallback                                       → diegetic
    """
    if total_frames == 0:
        return "diegetic"

    mid_frame = (start_frame + end_frame) / 2
    position = mid_frame / total_frames  # 0.0 … 1.0

    line_count = len([l for l in text.splitlines() if l.strip()])

    if is_bw_card and position < 0.10:
        if movie_title is None or _title_matches(text, movie_title):
            return "title"
        return "credits"

    if is_bw_card and position > 0.95:
        return "ending"

    if position > 0.80 and line_count >= 2:
        return "credits"

    if position < 0.20 and line_count >= 2:
        return "credits"

    if is_bw_card:
        return "intertitle"

    return "diegetic"


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
                used.add(best_idx)
            else:
                new_streams.append({
                    "frame_no":     hit["frame_no"],
                    "timecode":     hit["timecode"],
                    "end_frame":    hit["frame_no"],
                    "end_timecode": hit["timecode"],
                    "text":         hit["text"],
                    "quad":         hit["quad"],
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

        result.append({
            "frame_no":     start_frame,
            "timecode":     first["timecode"],
            "end_frame":    end_frame,
            "end_timecode": last["end_timecode"],
            "text":         combined_text,
            "quad":         _union_quad([e["quad"] for e in group if e.get("quad")]),
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

                    detections = _ocr_frame_detections(pil_img, lang=lang, min_confidence=min_confidence)

                    got_frame = True

                    tc = frames_to_timecode(frame_no, fps)
                    frame_hits = []
                    for det in detections:
                        norm = _normalise_text(det["text"])
                        if _text_is_plausible(norm):
                            frame_hits.append({
                                "frame_no": frame_no,
                                "timecode": tc,
                                "text":     norm,
                                "quad":     det["quad"],
                                "is_card":  True,
                            })

                    if not frame_hits:
                        if verbose:
                            print(f"  [t={frame_time_s:.1f}s] skip (no plausible text)")
                        break

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
                "start_time": m["timecode"],
                "end_time": m["end_timecode"],
                "start_frame": m["frame_no"],
                "end_frame": m["end_frame"],
                "quad": ",".join(str(v) for v in m["quad"]) if m.get("quad") else "",
                "text": m["text"],
                "language": lang,
            }
        )

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
