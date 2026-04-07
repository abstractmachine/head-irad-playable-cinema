import csv
import json
import re
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any
import warnings

_MEDIA_TYPES = ("movies", "gameplay")

MOVIE_REQUIRED = {"title", "year"}


def _csv_path(project_path: str, media_type: str) -> Path:
    return Path(project_path) / "data" / "metadata" / f"{media_type}.csv"


def _all_metadata(project_path: str, media_type: str | None = None) -> list[dict]:
    types = (media_type,) if media_type else _MEDIA_TYPES
    results = []
    for mt in types:
        path = _csv_path(project_path, mt)
        if not path.exists():
            continue
        with path.open(newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                results.append(dict(row))
    return results


def get_metadata(project_path: str, query: str | None = None, media_type: str = "movies") -> list[dict]:
    """Return metadata entries.

    query=None  → all entries
    query=int   → entry at that index
    query=str   → entries whose filename or title contains the query
    """
    all_items = _all_metadata(project_path, media_type)

    if query is None:
        return all_items

    # Try integer index
    try:
        idx = int(query)
        if 0 <= idx < len(all_items):
            return [all_items[idx]]
        return []
    except ValueError:
        pass

    # Substring match on filename or title
    q = query.lower()
    return [
        item for item in all_items
        if q in str(item.get("filename", "")).lower()
        or q in str(item.get("title", "")).lower()
    ]


def set_metadata(project_path: str, data: dict, match_filename: str | None = None) -> Path:
    """Write metadata to the CSV file for the appropriate media type.

    The data dict must contain 'filename'. 'media_type' defaults to 'movies'.
    If a row with the same filename exists it is replaced; otherwise appended.
    match_filename lets callers rename an existing row (pass the old filename).
    """
    media_type = data.get("media_type", "movies")
    filename = data.get("filename")
    if not filename:
        raise ValueError("metadata must include a 'filename' field")

    dest = _csv_path(project_path, media_type)
    dest.parent.mkdir(parents=True, exist_ok=True)

    existing_rows: list[dict] = []
    fieldnames: list[str] = []
    if dest.exists():
        with dest.open(newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            fieldnames = list(reader.fieldnames or [])
            existing_rows = [{k: v for k, v in row.items() if k is not None} for row in reader]

    # Strip internal routing key before writing
    row_data = {k: v for k, v in data.items() if k != "media_type"}

    # Extend fieldnames with any new keys from data
    for key in row_data:
        if key not in fieldnames:
            fieldnames.append(key)

    # Upsert by filename (match_filename lets callers rename an existing row)
    lookup = match_filename if match_filename is not None else filename
    updated = False
    for i, row in enumerate(existing_rows):
        if row.get("filename") == lookup:
            existing_rows[i] = {**row, **row_data}
            updated = True
            break
    if not updated:
        existing_rows.append(row_data)

    existing_rows.sort(key=lambda r: r.get("title", "").lower())

    with dest.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, restval="")
        writer.writeheader()
        writer.writerows(existing_rows)

    return dest


def prune_metadata(project_path: str, media_type: str = "movies") -> list[dict]:
    """Delete metadata rows whose file no longer exists on disk.

    Returns the list of pruned rows.
    """
    # Canonical videos location. Legacy media/<media_type> is deprecated.
    media_dir_new = Path(project_path) / "media" / "videos" / media_type
    media_dir_legacy = Path(project_path) / "media" / media_type
    dest = _csv_path(project_path, media_type)

    if not dest.exists():
        return []

    with dest.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        fieldnames = list(reader.fieldnames or [])
        rows = [{k: v for k, v in row.items() if k is not None} for row in reader]

    kept, pruned = [], []
    legacy_detected: list[str] = []
    for row in rows:
        filename = row.get("filename", "")
        if not filename:
            pruned.append(row)
            continue
        # Only canonical location is accepted. Legacy files are treated as missing.
        if (media_dir_new / filename).exists():
            kept.append(row)
        else:
            # If a legacy file exists, record it for a single warning and treat as missing.
            if (media_dir_legacy / filename).exists():
                legacy_detected.append(filename)
            pruned.append(row)

    if legacy_detected:
        warnings.warn(
            f"Found {len(legacy_detected)} media file(s) in legacy location {media_dir_legacy}. "
            f"These files will be treated as missing. Move them to {media_dir_new}.",
            UserWarning,
        )

    with dest.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, restval="")
        writer.writeheader()
        writer.writerows(kept)

    return pruned


def validate_metadata(data: dict) -> tuple[bool, list[str]]:
    """Validate a metadata dict. Returns (ok, [error_messages])."""
    errors: list[str] = []
    media_type = data.get("media_type", "movies")

    if media_type == "movies":
        for field in MOVIE_REQUIRED:
            if field not in data:
                errors.append(f"missing required field: {field!r}")
        if "year" in data and not isinstance(data["year"], int):
            errors.append("'year' must be an integer")
        if "runtime" in data and not isinstance(data["runtime"], int):
            errors.append("'runtime' must be an integer")

    return len(errors) == 0, errors


def _parse_filename(filename: str) -> dict[str, Any]:
    """Infer title, year, and tmdb_id from a filename without network calls."""
    stem = Path(filename).stem

    year_match = re.search(r'\((\d{4})\)', stem)
    tmdb_match = re.search(r'\{tmdb-(\d+)\}', stem)

    title_end = len(stem)
    if year_match:
        title_end = min(title_end, year_match.start())
    if tmdb_match:
        title_end = min(title_end, tmdb_match.start())

    title_raw = stem[:title_end].strip().rstrip('-').strip()
    title = title_raw.replace('-', ' ').strip().title()

    stub: dict[str, Any] = {
        "filename": filename,
        "media_type": "movies",
        "title": title,
    }
    if year_match:
        stub["year"] = int(year_match.group(1))
    if tmdb_match:
        stub["tmdb_id"] = int(tmdb_match.group(1))

    return stub


def _get_video_duration(video_path: Path) -> int | None:
    """Get video duration in minutes using ffprobe.
    
    Returns duration in minutes, or None if ffprobe fails.
    """
    try:
        cmd = [
            'ffprobe',
            '-v', 'quiet',
            '-print_format', 'json',
            '-show_format',
            str(video_path)
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if result.returncode == 0:
            data = json.loads(result.stdout)
            duration_seconds = float(data['format']['duration'])
            duration_minutes = int(duration_seconds / 60)
            return duration_minutes
        return None
    except (subprocess.TimeoutExpired, FileNotFoundError, KeyError, ValueError, json.JSONDecodeError):
        return None


def _read_key(project_path: str, name: str) -> str:
    """Read a key from <project_path>/preferences/keys/<name>.txt."""
    key_file = Path(project_path) / "preferences" / "keys" / f"{name}.txt"
    if not key_file.exists():
        raise RuntimeError(
            f"{key_file} not found. "
            f"Run: crossing api_key set {name.replace('_api_key', '')} <your-key>"
        )
    return key_file.read_text().strip()


def fetch_subtitle(filename: str, project_path: str, media_type: str, imdb_id: str | None, title: str, year: int | None, *, force: bool = False) -> Path | None:
    """Download English subtitle for a file if not already present.

    Checks for existing subtitle in both new format (spaces) and old format (dashes).
    Downloads from OpenSubtitles if missing. Returns path to subtitle or None if unavailable.
    Pass force=True to re-download even when a subtitle file already exists.
    """
    from services.normalize import normalize_filename

    api_key = _read_key(project_path, "opensubtitles_api_key")
    subtitle_dir = Path(project_path) / "media" / "subtitles" / media_type
    subtitle_dir.mkdir(parents=True, exist_ok=True)

    # Expected new filename: change extension from .mp4 to .srt
    subtitle_name = Path(filename).stem + ".srt"
    subtitle_path = subtitle_dir / subtitle_name

    # Also check if old dash-separated version exists
    old_subtitle_name = subtitle_name.replace(" ", "-")
    old_subtitle_path = subtitle_dir / old_subtitle_name

    if not force:
        if subtitle_path.exists():
            return subtitle_path
        if old_subtitle_path.exists():
            return old_subtitle_path

    # OpenSubtitles API requires IMDB ID
    if not imdb_id:
        return None

    # OpenSubtitles.com REST API
    BASE = "https://api.opensubtitles.com/api/v1"
    
    _HEADERS = {
        "Api-Key": api_key,
        "Content-Type": "application/json",
        "User-Agent": "crossing-tool/2.0",
    }

    def _search(params: dict) -> list:
        url = f"{BASE}/subtitles?{urllib.parse.urlencode(params)}"
        req = urllib.request.Request(url, headers=_HEADERS)
        try:
            with urllib.request.urlopen(req) as resp:
                return json.loads(resp.read().decode()).get("data", [])
        except urllib.error.HTTPError as exc:
            body = exc.read().decode(errors="replace")[:200]
            raise RuntimeError(f"OpenSubtitles search failed: HTTP {exc.code} {exc.reason} — {body}")
        except urllib.error.URLError as exc:
            raise RuntimeError(f"OpenSubtitles search failed: {exc.reason}")

    # Search by IMDb ID first, fall back to title+year
    results = _search({"imdb_id": imdb_id.replace("tt", ""), "languages": "en"})
    if not results and title:
        fallback_params: dict[str, str] = {"query": title, "languages": "en"}
        if year:
            fallback_params["year"] = str(year)
        results = _search(fallback_params)

    if not results:
        return None

    # Get the first English subtitle file
    file_id = results[0].get("attributes", {}).get("files", [{}])[0].get("file_id")
    if not file_id:
        raise RuntimeError("OpenSubtitles returned a result but no file_id in the first entry")

    # Request a download link
    download_url = f"{BASE}/download"
    download_body = json.dumps({"file_id": file_id}).encode("utf-8")

    req = urllib.request.Request(download_url, data=download_body, headers=_HEADERS)
    
    for attempt in range(2):
        try:
            with urllib.request.urlopen(req) as resp:
                download_data = json.loads(resp.read().decode())
            break
        except urllib.error.HTTPError as exc:
            body = exc.read().decode(errors="replace")[:200]
            if exc.code >= 500 and attempt == 0:
                time.sleep(3)
                continue
            raise RuntimeError(f"OpenSubtitles download request failed: HTTP {exc.code} {exc.reason} — {body}")
        except urllib.error.URLError as exc:
            raise RuntimeError(f"OpenSubtitles download request failed: {exc.reason}")

    link = download_data.get("link")
    if not link:
        raise RuntimeError("OpenSubtitles download response contained no link")

    try:
        with urllib.request.urlopen(link) as resp:
            subtitle_content = resp.read()
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"Subtitle file download failed: HTTP {exc.code} {exc.reason}")
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Subtitle file download failed: {exc.reason}")

    subtitle_path.write_bytes(subtitle_content)
    return subtitle_path


def fetch_thumbnail(filename: str, project_path: str, media_type: str, tmdb_id: int) -> Path | None:
    """Download thumbnail for a file if not already present.

    Checks for existing thumbnail in both new format (spaces) and old format (dashes).
    Downloads from TMDb if missing. Returns path to thumbnail or None if unavailable.
    """
    from services.normalize import normalize_filename

    api_key = _read_key(project_path, "tmdb_api_key")
    thumbnail_dir = Path(project_path) / "media" / "thumbnails" / media_type
    thumbnail_dir.mkdir(parents=True, exist_ok=True)

    # Expected new filename: change extension from .mp4 to .jpg
    thumbnail_name = Path(filename).stem + ".jpg"
    thumbnail_path = thumbnail_dir / thumbnail_name

    # Also check if old dash-separated version exists
    old_thumbnail_name = thumbnail_name.replace(" ", "-")
    old_thumbnail_path = thumbnail_dir / old_thumbnail_name

    if thumbnail_path.exists():
        return thumbnail_path
    if old_thumbnail_path.exists():
        return old_thumbnail_path

    # Download from TMDb
    BASE = "https://api.themoviedb.org/3"
    url = f"{BASE}/movie/{tmdb_id}?api_key={api_key}&language=en-US"
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req) as resp:
        movie = json.loads(resp.read().decode())

    poster_url_path = movie.get("poster_path")
    if not poster_url_path:
        return None

    # Download thumbnail image
    thumbnail_url = f"https://image.tmdb.org/t/p/w500{poster_url_path}"
    req = urllib.request.Request(thumbnail_url)
    with urllib.request.urlopen(req) as resp:
        thumbnail_data = resp.read()

    thumbnail_path.write_bytes(thumbnail_data)
    return thumbnail_path


def fetch_metadata(filename: str, project_path: str) -> dict[str, Any]:
    """Fetch full movie metadata from TMDb by parsing the filename for clues.

    Reads the API key from preferences/tmdb_api_key.txt in the repo root.
    If a tmdb_id is embedded in the filename it is used directly; otherwise
    the title (and year, if present) are used to search TMDb.
    """
    api_key = _read_key(project_path, "tmdb_api_key")

    stub = _parse_filename(filename)
    tmdb_id = stub.get("tmdb_id")
    title = stub["title"]
    year = stub.get("year")

    BASE = "https://api.themoviedb.org/3"

    def _get(url: str) -> dict:
        sep = "&" if "?" in url else "?"
        req = urllib.request.Request(
            f"{url}{sep}api_key={api_key}",
            headers={"Accept": "application/json"},
        )
        with urllib.request.urlopen(req) as resp:
            return json.loads(resp.read().decode())

    if tmdb_id is None:
        params: dict[str, str] = {"query": title, "language": "en-US", "page": "1"}
        if year:
            params["year"] = str(year)
        data = _get(f"{BASE}/search/movie?{urllib.parse.urlencode(params)}")
        results = data.get("results", [])
        if not results:
            raise LookupError(
                f"No TMDb results for {title!r}" + (f" ({year})" if year else "")
            )
        tmdb_id = results[0]["id"]

    movie = _get(f"{BASE}/movie/{tmdb_id}?language=en-US&append_to_response=credits")

    release_year: int | None = None
    if movie.get("release_date"):
        try:
            release_year = int(movie["release_date"][:4])
        except (ValueError, IndexError):
            pass

    directors = [
        c["name"]
        for c in movie.get("credits", {}).get("crew", [])
        if c.get("job") == "Director"
    ]

    # Get actual video file duration using ffprobe
    video_path = Path(project_path) / "media" / "videos" / "movies" / filename
    actual_duration = None
    if video_path.exists():
        actual_duration = _get_video_duration(video_path)

    return {
        "title": movie["title"],
        "year": release_year,
        "director": ", ".join(directors),
        "tmdb": movie["id"],
        "imdb": movie.get("imdb_id", ""),
        "filename": filename,
        "duration": actual_duration if actual_duration is not None else movie.get("runtime"),
        "overview": movie.get("overview", ""),
        "tagline": movie.get("tagline", ""),
    }
