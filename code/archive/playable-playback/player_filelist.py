#!/usr/bin/env python3
# player_filelist.py — load filenames from cinematheque.csv

import os, csv, random

# adjust if needed
PROJECT_ROOT = "/media/playback/PLAYABLE-D/project"
CSV_PATH     = os.path.join(PROJECT_ROOT, "metadata", "cinematheque.csv")
MOVIES_DIR   = os.path.join(PROJECT_ROOT, "movies")

# in-memory cache
_MOVIES: list[str] = []

def _csv_exists() -> bool:
    return os.path.exists(CSV_PATH)

def _movie_path(filename: str) -> str:
    return os.path.join(MOVIES_DIR, filename)

def load_movies() -> list[str]:
    """
    Load filenames from cinematheque.csv.
    - Supports either a 'filename' header or a single-column CSV.
    - Ignores blank lines and lines starting with '#'.
    - Only keeps entries that actually exist in MOVIES_DIR.
    """
    movies: list[str] = []
    if not _csv_exists():
        return movies

    with open(CSV_PATH, newline="") as f:
        # detect header
        f.seek(0); dr = csv.DictReader(f)
        has_col = dr.fieldnames and "filename" in dr.fieldnames
        f.seek(0)
        if has_col:
            for row in csv.DictReader(f):
                fn = (row.get("filename") or "").strip()
                if not fn or fn.startswith("#"):
                    continue
                if os.path.exists(_movie_path(fn)):
                    movies.append(fn)
        else:
            for row in csv.reader(f):
                if not row: continue
                fn = (row[0] or "").strip()
                if not fn or fn.startswith("#"):
                    continue
                if os.path.exists(_movie_path(fn)):
                    movies.append(fn)
    return movies

def refresh() -> list[str]:
    """Reload the CSV into the in-memory cache."""
    global _MOVIES
    _MOVIES = load_movies()
    return list(_MOVIES)

def get_movies() -> list[str]:
    """Return cached list; if empty, attempt to load once."""
    if not _MOVIES:
        refresh()
    return list(_MOVIES)

def get_index_for_filename(filename: str) -> int | None:
    """Return index for filename in the current list, else None."""
    movies = get_movies()
    try:
        return movies.index(filename)
    except ValueError:
        return None

def pick_by_index(index: int | None) -> tuple[int, str] | None:
    """
    Resolve an index to (index, filename).
    - If index is -1, choose a random entry.
    - If index is None or out of range, return None.
    """
    movies = get_movies()
    if not movies:
        return None
    if index is None:
        return None
    try:
        idx = int(index)
    except Exception:
        return None
    if idx == -1:
        idx = random.randrange(len(movies))
    if 0 <= idx < len(movies):
        return idx, movies[idx]
    return None

def resolve_filename_or_index(filename: str | None, index: int | None) -> tuple[int | None, str] | None:
    """
    Resolve either a filename or an index into (index, filename).
    - If both given, index wins.
    - If filename given, it must exist in the list.
    """
    out = None
    if index is not None:
        out = pick_by_index(index)
    elif filename:
        movies = get_movies()
        if filename in movies:
            out = (movies.index(filename), filename)
    return out

def fullpath(filename: str) -> str:
    return _movie_path(filename)