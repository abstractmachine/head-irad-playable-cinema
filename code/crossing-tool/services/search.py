"""Shot annotation search – v1.

Core reusable function: search_shots()
CLI wrapper: see cli.py  ``crossing search``

Annotation files live at:
    <project>/data/annotations/shots/<media_type>/<stem>.json

Each file is a JSON list of entries shaped like:
    {"shot": {"shot_id": 1, "annotation": {"setting": "...", "objects": [...], ...}}}

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
    media_type: str = "movies",
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
    media_type:     "movies" or "gameplay".

    Returns
    -------
    dict with keys: query, scopes, field, limit, limit_per_item, results
    """
    from services.metadata import get_metadata
    from services.shotlist import read_shotlist

    if not project_path:
        raise RuntimeError("project_path is required")

    # Step 1 – resolve movies
    all_entries = get_metadata(project_path, media_type=media_type)
    selected, effective_scopes = _resolve_movies(scopes, use_all, all_entries)

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

        ann_path = ann_base / f"{stem}.json"
        if not ann_path.exists():
            continue

        try:
            ann_entries: list[Any] = json.loads(ann_path.read_text(encoding="utf-8"))
        except Exception:
            continue

        # Build shot_id (1-based) → timing dict from shotlist CSV
        shot_timing: dict[int, dict] = {}
        try:
            shots_csv = read_shotlist(project_path, filename, media_type)
            for idx, row in enumerate(shots_csv):
                shot_timing[idx] = {
                    "start_time": row.get("start_time", ""),
                    "end_time": row.get("end_time", ""),
                    "start_frame": _safe_int(row.get("start_frame")),
                    "end_frame": _safe_int(row.get("end_frame")),
                }
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
            shot_id = int(shot_id_raw)  # 1-based

            searchable, _ = _annotation_searchable_text(ann, field)
            score = _score_text(query, searchable)
            if score <= 0.0:
                continue

            matched_fields = _find_matched_fields(query, ann, field)
            matched_text = _build_matched_text(ann, matched_fields)

            # shot_id is 1-based; CSV rows are 0-based
            timing = shot_timing.get(shot_id - 1, {})

            results.append({
                "movie_id": movie_id,
                "movie_title": movie_title,
                "shot_id": f"shot_{shot_id:05d}",
                "start_time": timing.get("start_time", ""),
                "end_time": timing.get("end_time", ""),
                "start_frame": timing.get("start_frame"),
                "end_frame": timing.get("end_frame"),
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

