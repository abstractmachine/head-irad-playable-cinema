import os, csv, random

# Prefer fixed project root; allow override via env; fallback to local folder.
DEFAULT_ROOT = "/media/ssd/playable/project"
PROJECT_ROOT = os.environ.get("root")   or (DEFAULT_ROOT if os.path.isdir(DEFAULT_ROOT) else os.path.abspath(os.path.dirname(__file__)))
CSV_PATH     = os.environ.get("csv")    or os.path.join(PROJECT_ROOT, "metadata", "cinematheque.csv")
MOVIES_DIR   = os.environ.get("movies") or os.path.join(PROJECT_ROOT, "movies")

_MOVIES: list[str] = []

def _csv_exists() -> bool:
    return os.path.exists(CSV_PATH)

def _movie_path(filename: str) -> str:
    return os.path.join(MOVIES_DIR, filename)

def load_movies() -> list[str]:
    movies: list[str] = []
    if not _csv_exists():
        print(f"[cinematheque] CSV missing: {CSV_PATH}")
        return movies
    try:
        with open(CSV_PATH, newline="") as f:
            f.seek(0); dr = csv.DictReader(f)
            has_col = dr.fieldnames and "filename" in [h.lower() for h in dr.fieldnames]
            f.seek(0)
            if has_col:
                for row in csv.DictReader(f):
                    fn = (row.get("filename") or "").strip()
                    if not fn or fn.startswith("#"):
                        continue
                    if os.path.exists(_movie_path(fn)):
                        movies.append(fn)
                    else:
                        print(f"[cinematheque] missing file: {fn}")
            else:
                for row in csv.reader(f):
                    if not row:
                        continue
                    fn = (row[0] or "").strip()
                    if not fn or fn.startswith("#") or fn.lower() == "filename":
                        continue
                    if os.path.exists(_movie_path(fn)):
                        movies.append(fn)
                    else:
                        print(f"[cinematheque] missing file: {fn}")
    except Exception as e:
        print(f"[cinematheque] error reading CSV: {e}")
    return movies

def refresh() -> list[str]:
    global _MOVIES
    _MOVIES = load_movies()
    return list(_MOVIES)

def get_movies() -> list[str]:
    if not _MOVIES:
        refresh()
    return list(_MOVIES)

def get_index_for_filename(filename: str) -> int | None:
    movies = get_movies()
    try:
        return movies.index(filename)
    except ValueError:
        return None

def pick_by_index(index: int | None) -> tuple[int, str] | None:
    movies = get_movies()
    if not movies or index is None:
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
    if index is not None:
        return pick_by_index(index)
    if filename:
        movies = get_movies()
        if filename in movies:
            return movies.index(filename), filename
    return None

def fullpath(filename: str) -> str:
    return _movie_path(filename)