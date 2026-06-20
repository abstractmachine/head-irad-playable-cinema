"""
services/analysis.py — Archive analysis services for Crossing / Playable Cinema.

All functions are pure read-only analysis over existing cached data.
No writes, no re-computation, no model inference.

Functions
---------
compare_motifs           compare motif usage / sequences / overlap across films
get_all_motifs           global motif frequency index across the archive
search_palette           filter shots by colour-space characteristics
search_cooccurrence      multi-term AND/OR shot filtering
get_shot_context         neighboring shots around a given shot_id
align_subtitles_to_shots join subtitle cues to shots by time overlap
get_archive_stats        archive-level coverage statistics

Colour-space conventions (palette data)
----------------------------------------
The palette JSON stores pre-computed values per region (foreground / background):
  luminance : float  L* / 100  ∈ [0, 1]   (0 = black, 1 = white)
  chroma    : float  √(a*²+b*²) / 100  ∈ [0, ~1]
  lab       : [L*, a*, b*]  in standard CIE LAB units

Warm/cold heuristics
  --warm  : a* > 5  (reddish) OR b* > 10 (yellowish)
  --cold  : b* < -10 (bluish) OR (a* < -5 AND b* < 5)  (greenish/cyan)

Adjust WB_A, WB_B, CB_A, CB_B, CB_B2 constants below for project-specific tuning.
"""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Colour-space thresholds (project-tunable)
# ---------------------------------------------------------------------------

_WARM_A_MIN = 5.0    # a* > this → reddish
_WARM_B_MIN = 10.0   # b* > this → yellowish
_COLD_B_MAX = -10.0  # b* < this → bluish
_COLD_A_MAX = -5.0   # a* < this (when b* also low) → greenish/cyan


# ===========================================================================
# 1. compare_motifs
# ===========================================================================

def compare_motifs(
    project_path: str,
    media_type: str = "movie",
    films: list[str] | None = None,
    mode: str = "overlap",
    limit: int | None = None,
) -> dict:
    """Compare motif usage across films.

    Parameters
    ----------
    project_path : Project root directory.
    media_type   : "movie" or "gameplay".
    films        : Film title substrings / filenames to include.
                   None → all films that have a motif file.
    mode         : One of:
                     "sequence"  — motif word list per film (ordered)
                     "frequency" — per-film word-count dicts
                     "overlap"   — words shared across ≥ 2 films, sorted by
                                   number of films that use them
                     "rare"      — words appearing in exactly 1 film
    limit        : Cap on the number of returned entries per film (sequence /
                   frequency modes) or total rows (overlap / rare modes).

    Returns
    -------
    dict with keys: mode, films, result
    """
    entries, _ = _resolve_films(project_path, media_type, films)

    # Collect per-film motif sequences
    per_film: dict[str, list[str]] = {}
    for entry in entries:
        filename = entry.get("filename", "")
        if not filename:
            continue
        from data.motif import load_motif_words
        words = load_motif_words(project_path, filename, media_type)
        if words:
            title = entry.get("title") or Path(filename).stem
            per_film[title] = words

    if mode == "sequence":
        result = {
            title: (words[:limit] if limit else words)
            for title, words in per_film.items()
        }
        return {"mode": mode, "films": list(per_film.keys()), "result": result}

    if mode == "frequency":
        result = {}
        for title, words in per_film.items():
            counts = Counter(words)
            ranked = sorted(counts.items(), key=lambda kv: -kv[1])
            if limit:
                ranked = ranked[:limit]
            result[title] = [{"word": w, "count": c} for w, c in ranked]
        return {"mode": mode, "films": list(per_film.keys()), "result": result}

    # Build global word → set of film titles
    word_films: dict[str, set[str]] = defaultdict(set)
    for title, words in per_film.items():
        for w in set(words):
            word_films[w].add(title)

    if mode == "overlap":
        shared = {
            w: sorted(film_set)
            for w, film_set in word_films.items()
            if len(film_set) >= 2
        }
        ranked = sorted(shared.items(), key=lambda kv: -len(kv[1]))
        if limit:
            ranked = ranked[:limit]
        result = [
            {"word": w, "film_count": len(film_list), "films": film_list}
            for w, film_list in ranked
        ]
        return {"mode": mode, "films": list(per_film.keys()), "result": result}

    if mode == "rare":
        rare = {
            w: sorted(film_set)
            for w, film_set in word_films.items()
            if len(film_set) == 1
        }
        ranked = sorted(rare.items(), key=lambda kv: kv[0])  # alphabetical
        if limit:
            ranked = ranked[:limit]
        result = [
            {"word": w, "film": film_list[0]}
            for w, film_list in ranked
        ]
        return {"mode": mode, "films": list(per_film.keys()), "result": result}

    raise ValueError(f"Unknown mode {mode!r}. Valid: sequence, frequency, overlap, rare")


# ===========================================================================
# 2. get_all_motifs
# ===========================================================================

def get_all_motifs(
    project_path: str,
    media_type: str = "movie",
    films: list[str] | None = None,
    sort: str = "frequency",
    limit: int | None = None,
) -> dict:
    """Return a global motif frequency index across the archive.

    Parameters
    ----------
    project_path : Project root directory.
    media_type   : "movie" or "gameplay".
    films        : Restrict to these films (None → all).
    sort         : "frequency" (most common first), "alphabetical", or "rarity"
                   (least common first — same as frequency but reversed).
    limit        : Cap on returned entries.

    Returns
    -------
    dict with keys: total_shots, total_words, unique_words, films, vocabulary
    """
    entries, _ = _resolve_films(project_path, media_type, films)

    counts: Counter = Counter()
    film_titles: list[str] = []
    total_shots = 0

    for entry in entries:
        filename = entry.get("filename", "")
        if not filename:
            continue
        from data.motif import load_motif_words
        words = load_motif_words(project_path, filename, media_type)
        if words:
            title = entry.get("title") or Path(filename).stem
            film_titles.append(title)
            total_shots += len(words)
            counts.update(words)

    if sort == "frequency":
        ranked = counts.most_common()
    elif sort == "rarity":
        ranked = list(reversed(counts.most_common()))
    else:  # alphabetical
        ranked = sorted(counts.items(), key=lambda kv: kv[0].lower())

    if limit:
        ranked = ranked[:limit]

    vocabulary = [{"word": w, "count": c} for w, c in ranked]

    return {
        "total_shots": total_shots,
        "total_words": sum(counts.values()),
        "unique_words": len(counts),
        "films": film_titles,
        "vocabulary": vocabulary,
    }


# ===========================================================================
# 3. search_palette
# ===========================================================================

def search_palette(
    project_path: str,
    media_type: str = "movie",
    films: list[str] | None = None,
    *,
    warm: bool = False,
    cold: bool = False,
    dark: bool = False,
    bright: bool = False,
    low_chroma: bool = False,
    high_chroma: bool = False,
    foreground_only: bool = False,
    background_only: bool = False,
    luminance_min: float | None = None,
    luminance_max: float | None = None,
    chroma_min: float | None = None,
    chroma_max: float | None = None,
    limit: int | None = None,
) -> dict:
    """Filter shots by colour-space characteristics from existing palette cache.

    All thresholds operate on palette luminance / chroma / LAB values.
    No image re-analysis is performed.

    Returns
    -------
    dict with keys: filters, total_films_searched, result_count, results
    Each result: {film, filename, shot_id, start_time, end_time, region,
                  luminance, chroma, rgb, lab}
    """
    entries, _ = _resolve_films(project_path, media_type, films)

    from data.palette import load_palette
    from data.shotlist import get_shotlist_path, read_shotlist

    active_filters: list[str] = []
    if warm:        active_filters.append("warm")
    if cold:        active_filters.append("cold")
    if dark:        active_filters.append("dark")
    if bright:      active_filters.append("bright")
    if low_chroma:  active_filters.append("low_chroma")
    if high_chroma: active_filters.append("high_chroma")
    if foreground_only: active_filters.append("foreground_only")
    if background_only: active_filters.append("background_only")
    if luminance_min is not None: active_filters.append(f"luminance_min={luminance_min}")
    if luminance_max is not None: active_filters.append(f"luminance_max={luminance_max}")
    if chroma_min is not None:    active_filters.append(f"chroma_min={chroma_min}")
    if chroma_max is not None:    active_filters.append(f"chroma_max={chroma_max}")

    if not active_filters:
        raise ValueError(
            "At least one palette filter must be specified. "
            "Use --warm, --cold, --dark, --bright, --low-chroma, --high-chroma, "
            "--luminance-min/max, --chroma-min/max."
        )

    results: list[dict] = []

    for entry in entries:
        filename = entry.get("filename", "")
        if not filename:
            continue
        title = entry.get("title") or Path(filename).stem

        palette_doc = load_palette(project_path, filename, media_type)
        if not palette_doc:
            continue

        # Build shot timing lookup (start_frame → timing)
        timing_by_id: dict[str, dict] = {}
        try:
            shots_csv = read_shotlist(project_path, filename, media_type)
            for row in shots_csv:
                sid = row.get("shot_id", "")
                if sid:
                    timing_by_id[sid] = {
                        "start_time":  row.get("start_time", ""),
                        "end_time":    row.get("end_time", ""),
                        "start_frame": row.get("start_frame"),
                        "end_frame":   row.get("end_frame"),
                    }
        except FileNotFoundError:
            pass

        for shot in palette_doc.get("shots", []):
            shot_id = shot.get("shot_id", "")
            timing = timing_by_id.get(shot_id, {})

            regions: list[tuple[str, dict]] = []
            if not background_only:
                fg = shot.get("foreground")
                if fg:
                    regions.append(("foreground", fg))
            if not foreground_only:
                bg = shot.get("background")
                if bg:
                    regions.append(("background", bg))

            for region_name, region in regions:
                lum = region.get("luminance")
                chroma_val = region.get("chroma")
                lab = region.get("lab") or []
                a_star = lab[1] if len(lab) >= 2 else 0.0
                b_star = lab[2] if len(lab) >= 3 else 0.0

                if not _region_passes(
                    lum, chroma_val, a_star, b_star,
                    warm=warm, cold=cold, dark=dark, bright=bright,
                    low_chroma=low_chroma, high_chroma=high_chroma,
                    luminance_min=luminance_min, luminance_max=luminance_max,
                    chroma_min=chroma_min, chroma_max=chroma_max,
                ):
                    continue

                results.append({
                    "film":        title,
                    "filename":    filename,
                    "shot_id":     shot_id,
                    "start_time":  timing.get("start_time", ""),
                    "end_time":    timing.get("end_time", ""),
                    "start_frame": timing.get("start_frame"),
                    "end_frame":   timing.get("end_frame"),
                    "region":      region_name,
                    "luminance":   lum,
                    "chroma":      chroma_val,
                    "rgb":         region.get("rgb"),
                    "lab":         lab,
                })

        if limit and len(results) >= limit:
            results = results[:limit]
            break

    return {
        "filters": active_filters,
        "total_films_searched": len(entries),
        "result_count": len(results),
        "results": results,
    }


def _region_passes(
    lum, chroma_val, a_star, b_star, *,
    warm, cold, dark, bright, low_chroma, high_chroma,
    luminance_min, luminance_max, chroma_min, chroma_max,
) -> bool:
    """Return True iff the region satisfies ALL active filter flags."""
    if lum is None:
        return False

    if warm and not (a_star > _WARM_A_MIN or b_star > _WARM_B_MIN):
        return False
    if cold and not (b_star < _COLD_B_MAX or (a_star < _COLD_A_MAX and b_star < 5)):
        return False
    if dark and not (lum < 0.3):
        return False
    if bright and not (lum > 0.7):
        return False
    if low_chroma:
        if chroma_val is None or not (chroma_val < 0.15):
            return False
    if high_chroma:
        if chroma_val is None or not (chroma_val > 0.30):
            return False
    if luminance_min is not None and lum < luminance_min:
        return False
    if luminance_max is not None and lum > luminance_max:
        return False
    if chroma_min is not None:
        if chroma_val is None or chroma_val < chroma_min:
            return False
    if chroma_max is not None:
        if chroma_val is None or chroma_val > chroma_max:
            return False

    return True


# ===========================================================================
# 4. search_cooccurrence
# ===========================================================================

def search_cooccurrence(
    project_path: str,
    terms: list[str],
    media_type: str = "movie",
    films: list[str] | None = None,
    fields: list[str] | None = None,
    operator: str = "AND",
    limit: int | None = None,
) -> dict:
    """Find shots containing multiple annotation terms simultaneously.

    Uses the existing search_shots() infrastructure — no second search engine.

    Parameters
    ----------
    project_path : Project root directory.
    terms        : List of search terms (at least 1 required).
    media_type   : "movie" or "gameplay".
    films        : Restrict to these film title substrings.
    fields       : Restrict each term search to these annotation fields.
                   None → search all fields.
    operator     : "AND" (shots matching ALL terms) or
                   "OR"  (shots matching ANY term).
    limit        : Cap on returned results.

    Returns
    -------
    dict with keys: terms, fields, operator, result_count, results
    Each result: the search_shots result dict enriched with "matched_terms"
    """
    if not terms:
        raise ValueError("At least one search term is required.")
    if operator not in ("AND", "OR"):
        raise ValueError(f"operator must be AND or OR, got {operator!r}")

    from services.search import search_shots

    use_all = not films

    # Run a separate search for each term
    per_term: dict[str, dict[str, dict]] = {}  # term → {shot_id: result_dict}
    for term in terms:
        for field in (fields or [None]):  # type: ignore[list-item]
            search_res = search_shots(
                query=term,
                scopes=films or [],
                field=field,
                limit=None,
                limit_per_item=None,
                use_all=use_all,
                project_path=project_path,
                media_type=media_type,
            )
            term_key = f"{term}:{field}" if field else term
            per_term.setdefault(term_key, {})
            for r in search_res.get("results", []):
                sid = r.get("shot_id", "")
                if sid:
                    per_term[term_key][sid] = r

    # Collapse: group by canonical term (ignore per-field suffix for set ops)
    by_term: dict[str, dict[str, dict]] = defaultdict(dict)
    for term_key, shots in per_term.items():
        canonical_term = term_key.split(":")[0]
        by_term[canonical_term].update(shots)

    # Compute intersection or union
    shot_id_sets = [set(v.keys()) for v in by_term.values()]
    if not shot_id_sets:
        return {"terms": terms, "fields": fields, "operator": operator,
                "result_count": 0, "results": []}

    if operator == "AND":
        combined_ids = shot_id_sets[0].intersection(*shot_id_sets[1:])
    else:  # OR
        combined_ids = shot_id_sets[0].union(*shot_id_sets[1:])

    # Collect result dicts and annotate with which terms matched
    all_shots: dict[str, dict] = {}
    for term_shots in by_term.values():
        for sid, r in term_shots.items():
            if sid in combined_ids:
                all_shots.setdefault(sid, dict(r))

    for sid in combined_ids:
        if sid in all_shots:
            matched = [t for t, shots in by_term.items() if sid in shots]
            all_shots[sid]["matched_terms"] = sorted(matched)

    results = sorted(
        all_shots.values(),
        key=lambda r: (-r.get("score", 0), r.get("movie_title", ""), r.get("start_time", "")),
    )
    if limit:
        results = results[:limit]

    return {
        "terms": terms,
        "fields": fields,
        "operator": operator,
        "result_count": len(results),
        "results": results,
    }


# ===========================================================================
# 5. get_shot_context
# ===========================================================================

def get_shot_context(
    project_path: str,
    film: str,
    shot_id: str,
    media_type: str = "movie",
    window: int = 3,
    include_subtitles: bool = False,
    include_motif: bool = False,
    include_palette: bool = False,
) -> dict:
    """Return neighboring shots around a given shot_id.

    Parameters
    ----------
    project_path      : Project root directory.
    film              : Film title substring, filename, or TMDb ID.
    shot_id           : Canonical shot identifier (e.g. "tmdb_4638@f001234-f001456").
    media_type        : "movie" or "gameplay".
    window            : Number of shots to include on each side of the central shot.
    include_subtitles : Attach overlapping subtitle cues to each shot.
    include_motif     : Attach the motif word to each shot.
    include_palette   : Attach fg/bg dominant colour to each shot.

    Returns
    -------
    dict with keys: film, filename, shot_id, center_index, window,
                    total_shots, shots
    Each shot in shots: shot CSV fields + optional subtitle/motif/palette
    """
    from data.metadata import get_metadata as _get_metadata
    from data.shotlist import read_shotlist

    entries = _get_metadata(project_path, query=film, media_type=media_type)
    if not entries:
        raise ValueError(f"No film found matching {film!r}.")
    if len(entries) > 1:
        titles = [e.get("title", e.get("filename", "")) for e in entries]
        raise ValueError(
            f"Ambiguous: {len(entries)} films match {film!r}. "
            f"Matches: {titles}"
        )
    entry = entries[0]
    filename = entry["filename"]
    title = entry.get("title", Path(filename).stem)

    shots = read_shotlist(project_path, filename, media_type)

    # Find the central shot by shot_id; fall back to integer index if shot_id
    # looks like a plain integer.
    center_idx: int | None = None
    try:
        center_idx = int(shot_id)
    except ValueError:
        pass

    if center_idx is None:
        for i, shot in enumerate(shots):
            if shot.get("shot_id", "") == shot_id:
                center_idx = i
                break

    if center_idx is None:
        raise ValueError(
            f"Shot {shot_id!r} not found in shotlist for {filename!r}."
        )

    lo = max(0, center_idx - window)
    hi = min(len(shots), center_idx + window + 1)
    window_shots = shots[lo:hi]

    # Build enrichment lookup tables if requested
    subtitle_lookup: dict[int, list[str]] = {}
    motif_lookup: dict[str, str] = {}
    palette_lookup: dict[str, dict] = {}

    if include_subtitles:
        from data.subtitles import load_subtitle_cues
        cues = load_subtitle_cues(project_path, media_type, filename)
        for idx, shot in enumerate(shots[lo:hi], start=lo):
            st = _to_secs(shot.get("start_time", ""))
            et = _to_secs(shot.get("end_time", ""))
            if st is None and shot.get("start_frame"):
                pass  # no time data, skip subtitle matching
            overlapping = [
                c.text for c in cues
                if st is not None and et is not None
                and c.start_secs < et and c.end_secs > st
            ]
            subtitle_lookup[idx] = overlapping

    if include_motif:
        from data.motif import load_motif_doc
        motif_doc = load_motif_doc(project_path, filename, media_type)
        for i, s in enumerate(motif_doc.get("shots", [])):
            motif_lookup[str(i)] = s.get("value", "")

    if include_palette:
        from data.palette import load_palette
        palette_doc = load_palette(project_path, filename, media_type)
        if palette_doc:
            for s in palette_doc.get("shots", []):
                sid = s.get("shot_id", "")
                if sid:
                    fg = s.get("foreground") or {}
                    bg = s.get("background") or {}
                    palette_lookup[sid] = {
                        "fg_rgb": fg.get("rgb"),
                        "bg_rgb": bg.get("rgb"),
                        "fg_lum": fg.get("luminance"),
                        "bg_lum": bg.get("luminance"),
                    }

    enriched: list[dict] = []
    for rel_idx, shot in enumerate(window_shots):
        abs_idx = lo + rel_idx
        row = dict(shot)
        row["index"] = abs_idx
        row["is_center"] = (abs_idx == center_idx)

        if include_subtitles:
            row["subtitles"] = subtitle_lookup.get(abs_idx, [])
        if include_motif:
            row["motif"] = motif_lookup.get(str(abs_idx), "")
        if include_palette:
            shot_id_key = row.get("shot_id", "")
            row["palette"] = palette_lookup.get(shot_id_key, {})

        enriched.append(row)

    return {
        "film":         title,
        "filename":     filename,
        "shot_id":      shot_id,
        "center_index": center_idx,
        "window":       window,
        "total_shots":  len(shots),
        "shots":        enriched,
    }


def _to_secs(time_str: str) -> float | None:
    """Convert HH:MM:SS.mmm or HH:MM:SS,mmm to seconds, or None on failure."""
    if not time_str:
        return None
    import re
    m = re.match(r"(\d+):(\d{2}):(\d{2})[,.](\d+)", time_str.strip())
    if not m:
        return None
    h, mn, s, frac = m.groups()
    frac_s = int(frac) / (10 ** len(frac))
    return int(h) * 3600 + int(mn) * 60 + int(s) + frac_s


# ===========================================================================
# 6. align_subtitles_to_shots
# ===========================================================================

def align_subtitles_to_shots(
    project_path: str,
    film: str,
    media_type: str = "movie",
    scene: int | None = None,
) -> dict:
    """Join subtitle cues to shots by time overlap.

    For each shot, finds all subtitle cues whose time range overlaps the shot's
    start_time … end_time. Overlap condition:
        cue.start_secs < shot_end  AND  cue.end_secs > shot_start

    Parameters
    ----------
    project_path : Project root directory.
    film         : Film title substring, filename, or TMDb ID.
    media_type   : "movie" or "gameplay".
    scene        : Restrict to one scene number (int). None → all scenes.

    Returns
    -------
    dict with keys: film, filename, scene, total_shots, aligned_shots
    Each aligned shot: shot CSV fields + "subtitle_cues" (list of {text, start_secs, end_secs,
    overlap_secs})
    """
    from data.metadata import get_metadata as _get_metadata
    from data.shotlist import read_shotlist
    from data.subtitles import load_subtitle_cues, subtitle_exists

    entries = _get_metadata(project_path, query=film, media_type=media_type)
    if not entries:
        raise ValueError(f"No film found matching {film!r}.")
    if len(entries) > 1:
        titles = [e.get("title", e.get("filename", "")) for e in entries]
        raise ValueError(
            f"Ambiguous: {len(entries)} films match {film!r}. Matches: {titles}"
        )
    entry = entries[0]
    filename = entry["filename"]
    title = entry.get("title", Path(filename).stem)

    shots = read_shotlist(project_path, filename, media_type)
    if scene is not None:
        shots = [s for s in shots if str(s.get("Scene", "")) == str(scene)]

    if not subtitle_exists(project_path, media_type, filename):
        return {
            "film":          title,
            "filename":      filename,
            "scene":         scene,
            "total_shots":   len(shots),
            "has_subtitles": False,
            "aligned_shots": [],
        }

    cues = load_subtitle_cues(project_path, media_type, filename)

    aligned: list[dict] = []
    for shot in shots:
        row = dict(shot)
        st = _to_secs(shot.get("start_time", ""))
        et = _to_secs(shot.get("end_time", ""))

        matched: list[dict] = []
        if st is not None and et is not None:
            for cue in cues:
                if cue.start_secs < et and cue.end_secs > st:
                    overlap = min(cue.end_secs, et) - max(cue.start_secs, st)
                    matched.append({
                        "text":        cue.text,
                        "start_secs":  cue.start_secs,
                        "end_secs":    cue.end_secs,
                        "overlap_secs": round(overlap, 3),
                    })

        row["subtitle_cues"] = matched
        aligned.append(row)

    return {
        "film":          title,
        "filename":      filename,
        "scene":         scene,
        "total_shots":   len(shots),
        "has_subtitles": True,
        "aligned_shots": aligned,
    }


# ===========================================================================
# 7. get_archive_stats
# ===========================================================================

def get_archive_stats(
    project_path: str,
    media_type: str = "movie",
) -> dict:
    """Return archive-level coverage statistics.

    Scans all data directories without loading full file contents where possible.
    Uses the vocabulary index meta when available for token counts.

    Returns
    -------
    dict with film, shot, annotation, motif, palette, silhouette, subtitle,
    and vocabulary coverage figures.
    """
    from data.metadata import get_metadata as _get_metadata
    from data.shotlist import get_shotlist_path
    from data.palette import get_palette_path
    from data.subtitles import subtitle_exists

    base = Path(project_path)
    ann_dir = base / "data" / "annotations" / "shots" / media_type
    sil_dir = base / "data" / "silhouettes" / media_type

    entries = _get_metadata(project_path, media_type=media_type)
    film_count = len(entries)

    shot_count = 0
    annotated_shots = 0
    films_with_shotlist = 0
    films_with_annotations = 0
    films_with_motifs = 0
    films_with_palettes = 0
    films_with_subtitles = 0

    for entry in entries:
        filename = entry.get("filename", "")
        if not filename:
            continue

        sl_path = get_shotlist_path(project_path, filename, media_type)
        if sl_path.exists():
            films_with_shotlist += 1
            try:
                import csv
                with open(sl_path, encoding="utf-8") as f:
                    shot_count += sum(1 for _ in csv.DictReader(f))
            except Exception:
                pass

        stem = Path(filename).stem
        ann_path = ann_dir / f"{stem}.annotations.json"
        if ann_path.exists():
            films_with_annotations += 1
            try:
                data = json.loads(ann_path.read_text(encoding="utf-8"))
                annotated_shots += len(data)
                if any(
                    isinstance(e.get("shot"), dict)
                    and isinstance(e["shot"].get("motif"), str)
                    and e["shot"]["motif"].strip()
                    for e in data if isinstance(e, dict)
                ):
                    films_with_motifs += 1
            except Exception:
                pass

        if get_palette_path(project_path, filename, media_type).exists():
            films_with_palettes += 1

        if subtitle_exists(project_path, media_type, filename):
            films_with_subtitles += 1

    # Silhouette coverage: count unique (scope, field, word) directories
    silhouette_entries = 0
    if sil_dir.exists():
        silhouette_entries = sum(
            1 for p in sil_dir.rglob("*.json")
            if p.name.startswith("best__")
        )

    # Vocabulary stats from index meta
    vocab_stats: dict = {}
    vocab_index_path = base / "data" / "vocabulary" / f"vocabulary_{media_type}.json"
    if vocab_index_path.exists():
        try:
            meta = json.loads(vocab_index_path.read_text(encoding="utf-8")).get("meta", {})
            vocab_stats = {
                "built_at":           meta.get("built_at", ""),
                "files_processed":    meta.get("files_processed", 0),
                "total_tokens":       meta.get("total_tokens", 0),
                "vocabulary_fields":  meta.get("vocabulary_fields", []),
            }
        except Exception:
            pass

    return {
        "media_type":              media_type,
        "film_count":              film_count,
        "films_with_shotlist":     films_with_shotlist,
        "films_with_annotations":  films_with_annotations,
        "films_with_motifs":       films_with_motifs,
        "films_with_palettes":     films_with_palettes,
        "films_with_subtitles":    films_with_subtitles,
        "total_shots":             shot_count,
        "annotated_shots":         annotated_shots,
        "silhouette_entries":      silhouette_entries,
        "vocabulary":              vocab_stats,
    }


# ===========================================================================
# Internal helpers
# ===========================================================================

def _resolve_films(
    project_path: str,
    media_type: str,
    films: list[str] | None,
) -> tuple[list[dict], list[str]]:
    """Return (entries, effective_scopes) for the requested film scope."""
    from data.metadata import get_metadata as _get_metadata

    all_entries = _get_metadata(project_path, media_type=media_type)
    if not films:
        return all_entries, []

    from services.search import _resolve_movies_exact_first
    selected, scopes = _resolve_movies_exact_first(films, False, all_entries)
    return selected, scopes
