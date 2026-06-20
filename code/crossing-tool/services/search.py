"""Shot annotation search – v1.

Core reusable function: search_shots()
CLI wrapper: see cli.py  ``crossing search``

Annotation files live at:
    <project>/data/annotations/shots/<media_type>/<stem>.json

Each file is a JSON list of entries shaped like:
    {"shot": {"shot_id": "tmdb_391@f000000-f000050", "annotation": {"setting": "...", "objects": [...], ...}}}

Timing (start/end time and frames) is read from the corresponding shotlist CSV
at <project>/data/shotlists/<media_type>/<stem>.csv.

Example
-------
    from services.search import search_shots

    result = search_shots(
        query="sunset",
        scopes=["7th"],
        field=None,
        limit=10,
        limit_per_item=None,
        use_all=False,
        project_path="/path/to/project",
    )
    # result["results"] is a list of match dicts sorted by score descending.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _fuzzy_movie_score(scope: str, title: str) -> float:
    """Return a 0–1 relevance score for *scope* against a movie *title*.

    Strategy (in order of priority):
      - exact match            → 1.0
      - scope is a substring   → 0.9
      - token overlap          → proportional score in [0, 0.8)
    """
    s = scope.lower().strip()
    t = title.lower().strip()
    if not s:
        return 0.0
    if s == t:
        return 1.0
    if s in t:
        return 0.9
    s_tokens = set(s.split())
    t_tokens = set(t.split())
    overlap = s_tokens & t_tokens
    if not overlap:
        return 0.0
    return len(overlap) / len(s_tokens) * 0.8


def _resolve_movies(
    scopes: list[str] | None,
    use_all: bool,
    all_entries: list[dict],
) -> tuple[list[dict], list[str]]:
    """Return (selected_entries, effective_scopes).

    If *use_all* or no scopes → return all entries.
    Otherwise fuzzy-match each scope against movie titles.
    """
    if use_all or not scopes:
        return all_entries, []

    seen: dict[str, dict] = {}
    for scope in scopes:
        for entry in all_entries:
            filename = entry.get("filename", "")
            if not filename:
                continue
            title = entry.get("title") or Path(filename).stem
            score = _fuzzy_movie_score(scope, title)
            if score > 0.0 and filename not in seen:
                seen[filename] = entry

    return list(seen.values()), list(scopes)


def _normalize_scope_text(value: str) -> str:
    """Normalize titles/queries for robust exact matching."""
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", value.lower())).strip()


def _resolve_movies_exact_first(
    scopes: list[str] | None,
    use_all: bool,
    all_entries: list[dict],
) -> tuple[list[dict], list[str]]:
    """Resolve scopes with exact matching first, then fuzzy fallback.

    Exact pass checks normalized values against:
      - metadata title
      - filename stem
      - full filename
      - tmdb id (if scope is numeric)
    """
    if use_all or not scopes:
        return all_entries, []

    normalized_scopes = [_normalize_scope_text(s) for s in scopes if s and s.strip()]
    if not normalized_scopes:
        return all_entries, []

    exact_seen: dict[str, dict] = {}
    for entry in all_entries:
        filename = entry.get("filename", "")
        if not filename:
            continue

        title = str(entry.get("title") or Path(filename).stem)
        stem = Path(filename).stem
        tmdb = str(entry.get("tmdb") or entry.get("tmdb_id") or "").strip()

        candidates = {
            _normalize_scope_text(title),
            _normalize_scope_text(stem),
            _normalize_scope_text(filename),
        }
        if tmdb:
            candidates.add(_normalize_scope_text(tmdb))

        if any(scope in candidates for scope in normalized_scopes):
            exact_seen[filename] = entry

    if exact_seen:
        return list(exact_seen.values()), list(scopes)

    # No exact matches found; preserve existing fuzzy behavior.
    return _resolve_movies(scopes, use_all, all_entries)


def _annotation_searchable_text(
    ann: dict, field: str | None
) -> tuple[str, list[str]]:
    """Return (concatenated_text, fields_included) for searching.

    If *field* is given, restrict to that single field.
    Otherwise concatenate all annotation fields.
    """
    if field is not None:
        val = ann.get(field)
        if val is None:
            return "", []
        text = " ".join(str(v) for v in val) if isinstance(val, list) else str(val)
        return text, [field]

    parts: list[str] = []
    fields_included: list[str] = []
    for key, val in ann.items():
        if isinstance(val, list):
            part = " ".join(str(v) for v in val)
        else:
            part = str(val) if val is not None else ""
        if part.strip():
            parts.append(part)
            fields_included.append(key)
    return " ".join(parts), fields_included


def _score_text(query: str, text: str) -> float:
    """Return a relevance score ≥ 0 for *query* against *text*.

    Returns 0.0 when there is no match at all.

    Scoring:
      - exact phrase (substring) match  → 0.80 + 0.05 per additional occurrence (capped at 1.0)
      - full token coverage             → proportional in (0, 0.75]
      - partial token coverage          → proportional in (0, 0.60]
    """
    q = query.lower().strip()
    t = text.lower()
    if not q or not t:
        return 0.0

    # Exact phrase match
    if q in t:
        freq = t.count(q)
        return min(1.0, 0.80 + 0.05 * freq)

    # Token overlap
    q_tokens = q.split()
    t_tokens = set(t.split())
    if not q_tokens:
        return 0.0
    matched = sum(1 for tok in q_tokens if tok in t_tokens)
    if matched == 0:
        return 0.0
    ratio = matched / len(q_tokens)
    # Full coverage gets a higher score than partial
    base = 0.75 if ratio == 1.0 else 0.60
    return round(ratio * base, 4)


def _find_matched_fields(query: str, ann: dict, field: str | None) -> list[str]:
    """Return the field names within *ann* that contain a match for *query*."""
    q = query.lower().strip()
    if not q:
        return []
    if field is not None:
        val = ann.get(field)
        if val is None:
            return []
        text = " ".join(str(v) for v in val) if isinstance(val, list) else str(val)
        return [field] if _score_text(q, text) > 0 else []

    matched: list[str] = []
    for key, val in ann.items():
        text = " ".join(str(v) for v in val) if isinstance(val, list) else str(val or "")
        if _score_text(q, text) > 0:
            matched.append(key)
    return matched


def _build_matched_text(ann: dict, matched_fields: list[str]) -> str:
    """Return a readable summary of the *matched_fields* values from *ann*."""
    parts: list[str] = []
    for f in matched_fields:
        val = ann.get(f)
        if val is None:
            continue
        if isinstance(val, list):
            parts.append(", ".join(str(v) for v in val))
        else:
            parts.append(str(val))
    return "; ".join(parts)


def _safe_int(val: Any) -> int | None:
    try:
        return int(val)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Core API
# ---------------------------------------------------------------------------

def search_shots(
    query: str,
    scopes: list[str] | None,
    field: str | None,
    limit: int | None,
    limit_per_item: int | None,
    use_all: bool,
    project_path: str | None = None,
    media_type: str = "movie",
) -> dict:
    """Search shot annotations and return a structured result dict.

    Parameters
    ----------
    query:          Search string (case-insensitive substring / token match).
    scopes:         Optional list of fuzzy movie-title matchers.
    field:          Restrict search to this annotation field (e.g. "objects").
                    If None, all fields are searched.
    limit:          Max total results to return. None → return all.
    limit_per_item: Max results per movie (applied before global limit).
    use_all:        Force search across all movies, ignoring *scopes*.
    project_path:   Project root directory.
    media_type:     "movie" or "gameplay".

    Returns
    -------
    dict with keys: query, scopes, field, limit, limit_per_item, results
    """
    from data.metadata import get_metadata
    from data.shotlist import read_shotlist

    if not project_path:
        raise RuntimeError("project_path is required")

    # Step 1 – resolve movies
    all_entries = get_metadata(project_path, media_type=media_type)
    selected, effective_scopes = _resolve_movies_exact_first(scopes, use_all, all_entries)

    if scopes and not use_all and not selected:
        print(
            f"warning: no movies matched scopes {scopes!r}",
            file=sys.stderr,
        )

    ann_base = Path(project_path) / "data" / "annotations" / "shots" / media_type

    # Steps 2–4 – load shots, build searchable text, score
    results: list[dict] = []

    for entry in selected:
        filename = entry.get("filename", "")
        if not filename:
            continue

        stem = Path(filename).stem
        movie_id = stem
        year = entry.get("year", "")
        movie_title_raw = entry.get("title") or stem
        movie_title = f"{movie_title_raw} ({year})" if year else movie_title_raw
        tmdb_id = entry.get("tmdb") or entry.get("tmdb_id")

        ann_path = ann_base / f"{stem}.annotations.json"
        if not ann_path.exists():
            continue

        try:
            ann_entries: list[Any] = json.loads(ann_path.read_text(encoding="utf-8"))
        except Exception:
            continue

        # Build timing lookups from shotlist CSV.
        # shot_timing: 0-based index → timing dict  (legacy integer shot_ids)
        # shot_timing_by_frame: start_frame → timing dict  (stable string shot_ids)
        shot_timing: dict[int, dict] = {}
        shot_timing_by_frame: dict[int, dict] = {}
        try:
            shots_csv = read_shotlist(project_path, filename, media_type)
            for idx, row in enumerate(shots_csv):
                timing_row = {
                    "start_time": row.get("start_time", ""),
                    "end_time": row.get("end_time", ""),
                    "start_frame": _safe_int(row.get("start_frame")),
                    "end_frame": _safe_int(row.get("end_frame")),
                }
                shot_timing[idx] = timing_row
                sf_key = _safe_int(row.get("start_frame"))
                if sf_key is not None:
                    shot_timing_by_frame[sf_key] = timing_row
        except FileNotFoundError:
            pass  # timing fields will be empty strings / None

        for ann_entry in ann_entries:
            shot_meta = ann_entry.get("shot") if isinstance(ann_entry, dict) else None
            if not isinstance(shot_meta, dict):
                continue
            ann = shot_meta.get("annotation")
            if not isinstance(ann, dict):
                continue
            shot_id_raw = shot_meta.get("shot_id")
            if shot_id_raw is None:
                continue

            # Resolve timing — stable IDs encode frames directly; legacy integer
            # IDs use a 1-based index into the shotlist CSV.
            from data.media_id import parse_shot_id as _parse_shot_id
            try:
                _, _sf, _ef = _parse_shot_id(str(shot_id_raw))
                # Stable ID: frames come from the ID itself
                timing = shot_timing_by_frame.get(_sf, {})
                shot_id_display = str(shot_id_raw)
                start_frame = _sf
                end_frame = _ef
            except ValueError:
                # Legacy integer ID
                try:
                    _shot_id_int = int(shot_id_raw)
                except (TypeError, ValueError):
                    continue
                timing = shot_timing.get(_shot_id_int - 1, {})
                shot_id_display = f"shot_{_shot_id_int:05d}"
                start_frame = timing.get("start_frame")
                end_frame = timing.get("end_frame")

            searchable, _ = _annotation_searchable_text(ann, field)
            score = _score_text(query, searchable)
            if score <= 0.0:
                continue

            matched_fields = _find_matched_fields(query, ann, field)
            matched_text = _build_matched_text(ann, matched_fields)

            results.append({
                "filename": filename,
                "tmdb_id": tmdb_id,
                "movie_id": movie_id,
                "movie_title": movie_title,
                "shot_id": shot_id_display,
                "start_time": timing.get("start_time", ""),
                "end_time": timing.get("end_time", ""),
                "start_frame": start_frame,
                "end_frame": end_frame,
                "matched_fields": matched_fields,
                "matched_text": matched_text,
                "score": round(score, 4),
            })

    # Step 5 – sort by score descending
    results.sort(key=lambda r: -r["score"])

    # Step 6 – apply limits
    if limit_per_item is not None:
        grouped: dict[str, list[dict]] = {}
        for r in results:
            grouped.setdefault(r["movie_id"], []).append(r)
        trimmed: list[dict] = []
        for movie_results in grouped.values():
            trimmed.extend(movie_results[:limit_per_item])
        trimmed.sort(key=lambda r: -r["score"])
        results = trimmed

    if limit is not None:
        results = results[:limit]

    return {
        "query": query,
        "scopes": effective_scopes,
        "field": field,
        "limit": limit,
        "limit_per_item": limit_per_item,
        "results": results,
    }


# ---------------------------------------------------------------------------
# Vocabulary extraction
# ---------------------------------------------------------------------------

def _vocabulary_from_cache(
    field: str,
    project_path: str,
    media_type: str,
    show_count: bool,
    sort: str,
) -> list | None:
    """Try to answer a vocabulary query from the built cache.

    Returns a formatted list on success, or None when the cache is missing,
    stale, or does not contain the requested field.
    """
    try:
        from services.vocabulary_index import (
            get_vocabulary,
            vocabulary_cache_is_stale,
        )
        if vocabulary_cache_is_stale(project_path, media_type):
            return None
        items = get_vocabulary(field, project_path, media_type, sort=sort)
    except FileNotFoundError:
        return None
    except KeyError:
        return None  # field not in vocabulary allowlist
    except Exception:
        return None

    if show_count:
        return items  # already [{"value": str, "count": int}, ...]
    else:
        return [item["value"] for item in items]


def vocabulary_from_field(
    field: str,
    scopes: list[str] | None,
    use_all: bool,
    show_count: bool,
    project_path: str,
    media_type: str = "movie",
    sort: str = "alphabetical",
) -> list:
    """Return distinct values found in *field* across all matching shots.

    When no scope filter is applied (``use_all`` or no scopes), the vocabulary
    cache built by ``crossing index vocabulary`` is consulted first.  If the
    cache is present and fresh the result is returned in O(1).  Otherwise the
    function falls back to a full corpus scan.

    Parameters
    ----------
    field:        Annotation field to enumerate (e.g. ``"objects"``).
    scopes:       Optional fuzzy movie-title filters.
    use_all:      Ignore scopes and enumerate every movie.
    show_count:   If True return ``[{"value": v, "count": n}, ...]``;
                  otherwise return ``["v1", "v2", ...]``.
    project_path: Project root directory.
    media_type:   ``"movie"`` or ``"gameplay"``.
    sort:         ``"alphabetical"`` (default) or ``"count"`` (desc).

    Returns
    -------
    JSON-serialisable list.
    """
    import re
    from data.metadata import get_metadata

    if not project_path:
        raise RuntimeError("project_path is required")

    # Fast path: use the vocabulary cache when no scope filter is active.
    if use_all or not scopes:
        cached = _vocabulary_from_cache(field, project_path, media_type, show_count, sort)
        if cached is not None:
            return cached

    all_entries = get_metadata(project_path, media_type=media_type)
    selected, _ = _resolve_movies_exact_first(scopes, use_all, all_entries)

    ann_base = Path(project_path) / "data" / "annotations" / "shots" / media_type

    def _norm_key(v: str) -> str:
        return re.sub(r"\s+", " ", v.strip()).lower()

    # Pass 1 – collect every shot's field text and every unique value (display form).
    # We store one searchable text string per shot (same concatenation used by
    # search_shots) so that pass 2 can reuse _score_text for counting.
    shot_texts: list[str] = []
    all_values: dict[str, str] = {}  # normalized_key → display_form

    for entry in selected:
        filename = entry.get("filename", "")
        if not filename:
            continue
        stem = Path(filename).stem
        ann_path = ann_base / f"{stem}.annotations.json"
        if not ann_path.exists():
            continue
        try:
            ann_entries: list = json.loads(ann_path.read_text(encoding="utf-8"))
        except Exception:
            continue

        for ann_entry in ann_entries:
            shot_meta = ann_entry.get("shot") if isinstance(ann_entry, dict) else None
            if not isinstance(shot_meta, dict):
                continue
            ann = shot_meta.get("annotation")
            if not isinstance(ann, dict):
                continue

            val = ann.get(field)
            if val is None:
                continue

            # Flatten list or treat scalar as single item
            raw_values: list[str]
            if isinstance(val, list):
                raw_values = [str(v) for v in val if v is not None and str(v).strip()]
            else:
                s = str(val).strip()
                raw_values = [s] if s else []

            if not raw_values:
                continue

            # Build the same searchable text that search_shots uses for this field
            shot_texts.append(" ".join(raw_values))

            # Record unique values (keep first-seen display form)
            for rv in raw_values:
                key = _norm_key(rv)
                if key and key not in all_values:
                    all_values[key] = rv.strip()

    # Pass 2 – for every unique value, count the shots that _score_text would
    # match. This makes the vocabulary count consistent with actual search results.
    counts: dict[str, list] = {
        key: [display, sum(1 for t in shot_texts if _score_text(key, t) > 0)]
        for key, display in all_values.items()
    }

    # Sort: alphabetical by default; count desc + alpha tiebreak if requested
    if sort == "count":
        sorted_items = sorted(counts.values(), key=lambda x: (-x[1], x[0].lower()))
    else:
        sorted_items = sorted(counts.values(), key=lambda x: x[0].lower())

    if show_count:
        return [{"value": item[0], "count": item[1]} for item in sorted_items]
    else:
        return [item[0] for item in sorted_items]

