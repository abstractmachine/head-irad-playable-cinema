"""Corpus index and search for the palette research laboratory.

Builds one flat, queryable record per annotated shot by joining three existing
project stores:

* ``data/annotations/shots/<type>/*.json``  — semantic annotation + best frame
* ``data/palettes/<type>/*.json``           — cached production palette
* ``data/metadata/<type>.json``             — title / year / director

The palette cache is what makes this a *chromatic* search engine as well as a
semantic one: it already holds per-shot foreground/background LAB, so queries
like "almost monochrome", "near-black frame with one saturated accent" or
"foreground and background perceptually indistinguishable" are answerable
without touching a single pixel.

Used only for corpus curation; nothing here runs during an experiment.
"""

from __future__ import annotations

import json
import math
import pickle
import re
from pathlib import Path

CACHE = Path("/tmp/palette_lab_corpus_index.pkl")


def _lab_delta(a, b) -> float:
    return math.sqrt(sum((float(x) - float(y)) ** 2 for x, y in zip(a, b)))


def _chroma(lab) -> float:
    return math.hypot(float(lab[1]), float(lab[2]))


def _film_dir_name(filename: str) -> str:
    return Path(filename).stem


def _load_metadata(project: Path, media_type: str) -> dict:
    path = project / "data" / "metadata" / f"{media_type}.json"
    entries = json.loads(path.read_text(encoding="utf-8"))["media"]
    return {entry["filename"]: entry for entry in entries}


def _load_palettes(project: Path, media_type: str) -> dict:
    """Map shot_id -> cached palette record."""
    out: dict[str, dict] = {}
    directory = project / "data" / "palettes" / media_type
    if not directory.is_dir():
        return out
    for path in directory.glob("*.json"):
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        for shot in document.get("shots", []):
            shot_id = shot.get("shot_id")
            if shot_id:
                out[shot_id] = shot
    return out


def build(project: str, media_types=("movie", "gameplay")) -> list[dict]:
    """Build the flat shot index.  Cached to /tmp; delete the file to rebuild."""
    project_path = Path(project)
    records: list[dict] = []

    for media_type in media_types:
        metadata = _load_metadata(project_path, media_type)
        palettes = _load_palettes(project_path, media_type)
        directory = project_path / "data" / "annotations" / "shots" / media_type
        if not directory.is_dir():
            continue

        for path in sorted(directory.glob("*.json")):
            try:
                entries = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                continue
            if not isinstance(entries, list):
                continue

            for entry in entries:
                film = entry.get("movie") or entry.get("media") or {}
                shot = entry.get("shot") or {}
                annotation = shot.get("annotation") or {}
                shot_id = shot.get("shot_id")
                if not shot_id or not annotation:
                    continue

                filename = film.get("filename", "")
                meta = metadata.get(filename, {})
                frame_path = (
                    project_path / "media" / "frames" / "best" / media_type
                    / _film_dir_name(filename) / f"{shot_id}.png"
                )

                record = {
                    "shot_id": shot_id,
                    "media_type": media_type,
                    "filename": filename,
                    "title": meta.get("title") or film.get("title") or "",
                    "year": str(meta.get("year") or ""),
                    "director": meta.get("director") or "",
                    "tmdb": str(meta.get("tmdb") or ""),
                    "media_id": meta.get("media_id", ""),
                    "frame": str(frame_path),
                    "motif": shot.get("motif") or "",
                    "best_frame_score": (shot.get("best_frame") or {}).get("score"),
                    "description": annotation.get("description", ""),
                    "setting": annotation.get("setting", ""),
                    "shot": annotation.get("shot", ""),
                    "camera": annotation.get("camera", ""),
                    "spatial": annotation.get("spatial", ""),
                    "time_of_day": annotation.get("time_of_day", ""),
                    "humans": annotation.get("humans") or [],
                    "animals": annotation.get("animals") or [],
                    "objects": annotation.get("objects") or [],
                    "wearing": annotation.get("wearing") or [],
                    "action": annotation.get("action") or [],
                    "text": [t for t in (annotation.get("text") or []) if t],
                    "type": annotation.get("type") or [],
                }

                palette = palettes.get(shot_id)
                if palette:
                    fg = palette.get("foreground") or {}
                    bg = palette.get("background") or {}
                    fg_lab, bg_lab = fg.get("lab"), bg.get("lab")
                    swatches = (fg.get("palette") or []) + (bg.get("palette") or [])
                    chromas = [_chroma(s["lab"]) for s in swatches if s.get("lab")]
                    lights = [float(s["lab"][0]) for s in swatches if s.get("lab")]
                    record.update({
                        "fg_rgb": fg.get("rgb"),
                        "bg_rgb": bg.get("rgb"),
                        "fg_lab": fg_lab,
                        "bg_lab": bg_lab,
                        "fg_L": round(float(fg_lab[0]), 1) if fg_lab else None,
                        "bg_L": round(float(bg_lab[0]), 1) if bg_lab else None,
                        "fg_C": round(_chroma(fg_lab), 1) if fg_lab else None,
                        "bg_C": round(_chroma(bg_lab), 1) if bg_lab else None,
                        "fg_bg_dE": round(_lab_delta(fg_lab, bg_lab), 1)
                        if fg_lab and bg_lab else None,
                        "fg_coverage": fg.get("coverage"),
                        "max_C": round(max(chromas), 1) if chromas else None,
                        "min_C": round(min(chromas), 1) if chromas else None,
                        "max_L": round(max(lights), 1) if lights else None,
                        "min_L": round(min(lights), 1) if lights else None,
                        "L_range": round(max(lights) - min(lights), 1) if lights else None,
                    })
                records.append(record)

    return records


def load(project: str, *, rebuild: bool = False) -> list[dict]:
    if CACHE.exists() and not rebuild:
        with CACHE.open("rb") as handle:
            return pickle.load(handle)
    records = build(project)
    with CACHE.open("wb") as handle:
        pickle.dump(records, handle)
    return records


# ---------------------------------------------------------------------------
# Query helpers
# ---------------------------------------------------------------------------

def has_frame(record: dict) -> bool:
    return Path(record["frame"]).exists()


def text_of(record: dict) -> str:
    parts = [
        record.get("description", ""),
        record.get("setting", ""),
        record.get("motif", ""),
        " ".join(record.get("humans") or []),
        " ".join(record.get("animals") or []),
        " ".join(record.get("objects") or []),
        " ".join(record.get("action") or []),
        " ".join(record.get("wearing") or []),
    ]
    return " ".join(str(p) for p in parts).casefold()


def search(records: list[dict], pattern: str | None = None, **filters) -> list[dict]:
    """Regex over the joined text plus exact/callable field filters.

    A filter value may be a string (exact, case-insensitive), a list (any-of),
    or a callable taking the field value.
    """
    regex = re.compile(pattern, re.I) if pattern else None
    out = []
    for record in records:
        if regex and not regex.search(text_of(record)):
            continue
        ok = True
        for field, want in filters.items():
            value = record.get(field)
            if callable(want):
                ok = bool(want(value))
            elif isinstance(want, (list, tuple, set)):
                ok = str(value).casefold() in {str(w).casefold() for w in want}
            else:
                ok = str(value).casefold() == str(want).casefold()
            if not ok:
                break
        if ok:
            out.append(record)
    return out


def summarize(record: dict) -> str:
    return (
        f"{record['shot_id']:38s} {record['title'][:30]:30s} {record['year']:5s} "
        f"{record['shot'][:11]:11s} L*{record.get('fg_L')}/{record.get('bg_L')} "
        f"C*{record.get('fg_C')}/{record.get('bg_C')} dE{record.get('fg_bg_dE')} "
        f"maxC{record.get('max_C')} | {record['description'][:80]}"
    )
