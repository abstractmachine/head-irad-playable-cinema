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

_MEDIA_TYPES = ("movie", "gameplay")


def normalize_media_type(s: str) -> str:
    """Return the canonical media type for *s*.

    Only ``"movie"`` and ``"gameplay"`` are valid.  ``"movies"`` and any other
    value raise ``ValueError``.
    """
    if s in _MEDIA_TYPES:
        return s
    raise ValueError(
        f"Invalid media type: {s!r}. "
        f"Valid media types: {', '.join(_MEDIA_TYPES)}"
    )


MOVIE_REQUIRED = {"title", "year"}


def _csv_path(project_path: str, media_type: str) -> Path:
    return Path(project_path) / "data" / "metadata" / f"{media_type}.csv"


def _all_metadata(project_path: str, media_type: str | None = None) -> list[dict]:
    types = (media_type,) if media_type else _MEDIA_TYPES
    results = []
    for mt in types:
        results.extend(load_json_metadata(project_path, mt))
    return results


def get_metadata(project_path: str, query: str | None = None, media_type: str = "movie") -> list[dict]:
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
    """Write metadata to the JSON file for the appropriate media type.

    The data dict must contain 'filename'. 'media_type' defaults to 'movie'.
    If a record with the same media_id exists it is replaced; otherwise appended.
    match_filename lets callers rename an existing record (pass the old filename).
    """
    from data.media_id import compute_media_id
    media_type = normalize_media_type(data.get("media_type", "movie"))
    filename = data.get("filename")
    if not filename:
        raise ValueError("metadata must include a 'filename' field")

    record = {k: v for k, v in data.items() if k != "media_type"}

    # Ensure media_id is present
    if not record.get("media_id"):
        record["media_id"] = compute_media_id(record, media_type)

    # Ensure original_filename is set
    if not record.get("original_filename"):
        record["original_filename"] = filename

    # Handle rename: find by old filename and replace in-place
    if match_filename and match_filename != filename:
        records = load_json_metadata(project_path, media_type)
        updated = False
        for i, r in enumerate(records):
            if r.get("filename") == match_filename:
                records[i] = {**r, **record}
                updated = True
                break
        if not updated:
            records.append(record)
        records.sort(key=lambda r: str(r.get("title", "")).lower())
        return save_json_metadata(project_path, media_type, records)

    return upsert_json_record(project_path, record, media_type, match_key="media_id")


def prune_metadata(project_path: str, media_type: str = "movie") -> list[dict]:
    """Delete metadata records whose file no longer exists on disk.

    Returns the list of pruned records.
    """
    records = load_json_metadata(project_path, media_type)
    media_dir = Path(project_path) / "media" / "videos" / media_type
    kept, pruned = [], []
    for r in records:
        filename = r.get("filename", "")
        if filename and (media_dir / filename).exists():
            kept.append(r)
        else:
            pruned.append(r)
    if pruned:
        save_json_metadata(project_path, media_type, kept)
    return pruned


def validate_metadata(data: dict) -> tuple[bool, list[str]]:
    """Validate a metadata dict. Returns (ok, [error_messages])."""
    errors: list[str] = []
    media_type = normalize_media_type(data.get("media_type", "movie"))

    if media_type == "movie":
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
        "media_type": "movie",
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
    video_path = Path(project_path) / "media" / "videos" / "movie" / filename
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


# ---------------------------------------------------------------------------
# JSON metadata layer  (additive — CSV files are kept in place)
# ---------------------------------------------------------------------------

_JSON_VERSION = "1"


def _json_path(project_path: str, media_type: str) -> Path:
    return Path(project_path) / "data" / "metadata" / f"{media_type}.json"


def load_json_metadata(project_path: str, media_type: str) -> list[dict]:
    """Load records from the JSON metadata file.  Returns [] if the file does not exist.

    Accepts ``"movie"`` or ``"gameplay"`` (canonical media types only).
    When the canonical ``movie.json`` does not exist, falls back to reading the
    legacy ``movies.json`` so that existing project data continues to load.
    """
    media_type = normalize_media_type(media_type)
    path = _json_path(project_path, media_type)
    if not path.exists():
        return []
    data = json.loads(path.read_text(encoding="utf-8"))
    return data.get("media", [])


def save_json_metadata(project_path: str, media_type: str, records: list[dict]) -> Path:
    """Write *records* to the JSON metadata file (overwrites atomically)."""
    from data.annotate import atomic_write_text
    path = _json_path(project_path, media_type)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"version": _JSON_VERSION, "media": records}
    atomic_write_text(path, json.dumps(payload, indent=2, ensure_ascii=False) + "\n")
    return path


def upsert_json_record(
    project_path: str,
    record: dict,
    media_type: str,
    match_key: str = "media_id",
) -> Path:
    """Insert or update a single record in the JSON file, keyed by *match_key*.

    If a record with the same match_key value already exists it is replaced;
    otherwise the new record is appended.  The list is sorted by title after
    the upsert.
    """
    records = load_json_metadata(project_path, media_type)
    key_val = record.get(match_key)

    updated = False
    if key_val:
        for i, r in enumerate(records):
            if r.get(match_key) == key_val:
                records[i] = {**r, **record}
                updated = True
                break
    if not updated:
        records.append(record)

    records.sort(key=lambda r: str(r.get("title", "")).lower())
    return save_json_metadata(project_path, media_type, records)


def migrate_csv_to_json(project_path: str, media_type: str) -> dict:
    """Read the CSV for *media_type*, assign media_ids, and write a JSON file.

    Records already in the JSON that have no corresponding CSV entry (i.e. new
    ingests added via ``ingest_gameplay``) are preserved unchanged.  CSV rows
    take precedence for records present in both.

    The CSV file is left untouched.  Returns a summary dict:
        {"written": int, "path": str, "skipped_ids": [str, ...]}
    """
    from data.media_id import compute_media_id

    csv_path = _csv_path(project_path, media_type)
    if not csv_path.exists():
        return {"written": 0, "path": None, "skipped_ids": []}

    with csv_path.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        rows = [{k: v for k, v in row.items() if k is not None} for row in reader]

    # Build a map of existing JSON records keyed by original_filename so that
    # JSON-only entries (new ingests) are preserved after the migration.
    existing_json = {
        (r.get("original_filename") or r.get("filename", "")): r
        for r in load_json_metadata(project_path, media_type)
    }

    records: list[dict] = []
    csv_filenames: set[str] = set()
    skipped: list[str] = []
    for row in rows:
        mid = compute_media_id(row, media_type=media_type)
        if not mid:
            skipped.append(row.get("filename", "?"))
            continue
        record = {"media_id": mid, **row}
        if "original_filename" not in record:
            record["original_filename"] = row.get("filename", "")
        records.append(record)
        csv_filenames.add(record["original_filename"])

    # Re-add JSON-only records that are not present in the CSV
    for orig_fn, json_rec in existing_json.items():
        if orig_fn not in csv_filenames:
            records.append(json_rec)

    records.sort(key=lambda r: str(r.get("title", "")).lower())
    path = save_json_metadata(project_path, media_type, records)
    return {"written": len(records), "path": str(path), "skipped_ids": skipped}


def check_metadata_sync(project_path: str, media_type: str) -> dict:
    """Compare the CSV and JSON metadata for *media_type*.

    Returns a report dict with keys:
        csv_count, json_count,
        missing_media_id,       # JSON records without media_id (error)
        in_csv_not_json,        # filenames in CSV but absent from JSON (error: migration gap)
        in_json_not_csv,        # filenames in JSON but absent from CSV (informational: new ingest)
        ok                      # True when all *error* checks pass
    """
    csv_rows = _all_metadata(project_path, media_type)
    json_records = load_json_metadata(project_path, media_type)

    csv_filenames  = {r.get("filename", "") for r in csv_rows  if r.get("filename")}
    json_filenames = {r.get("original_filename") or r.get("filename", "")
                      for r in json_records if r.get("original_filename") or r.get("filename")}

    missing_media_id = [
        r.get("media_id", "(none)") or r.get("filename", "?")
        for r in json_records
        if not r.get("media_id")
    ]
    in_csv_not_json  = sorted(csv_filenames  - json_filenames)
    in_json_not_csv  = sorted(json_filenames - csv_filenames)

    ok = (
        len(missing_media_id) == 0
        and len(in_csv_not_json) == 0
        # in_json_not_csv is informational: records added via the new ingest
        # path are JSON-only by design and do not constitute an error.
    )

    return {
        "media_type":      media_type,
        "csv_count":       len(csv_rows),
        "json_count":      len(json_records),
        "missing_media_id": missing_media_id,
        "in_csv_not_json": in_csv_not_json,
        "in_json_not_csv": in_json_not_csv,
        "ok":              ok,
    }


def ingest_gameplay(
    src_path: "str | Path",
    project_path: str,
    title: str | None = None,
    game: str | None = None,
) -> dict:
    """Ingest a gameplay clip into the project.

    Steps:
      1. Compute a stable media_id from the original filename.
      2. Build the on-disk destination: ``<media_id> - <title>.<ext>``
      3. Copy the file to ``media/videos/gameplay/``.
      4. Upsert a metadata record in ``gameplay.json``.

    Returns the metadata record that was written (including media_id and
    both filenames).
    """
    import shutil
    from data.media_id import compute_media_id

    src = Path(src_path).resolve()
    if not src.exists():
        raise FileNotFoundError(f"Source file not found: {src}")

    original_filename = src.name
    ext = src.suffix.lower() or ".mp4"

    # Derive a display title if none supplied
    if not title:
        stem = re.sub(r"[-_]+", " ", src.stem)
        title = stem.title()

    # Build a minimal record so compute_media_id can inspect it
    partial = {
        "title": title,
        "filename": original_filename,
    }
    if game:
        partial["game"] = game

    media_id = compute_media_id(partial, media_type="gameplay")

    # On-disk filename: "<media_id> - <title><ext>"
    dest_filename = f"{media_id} - {title}{ext}"
    dest_dir = Path(project_path) / "media" / "videos" / "gameplay"
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest_path = dest_dir / dest_filename

    if not dest_path.exists():
        shutil.copy2(str(src), str(dest_path))

    # Extract thumbnail from ~5% into the video
    from services.transcode import extract_video_thumbnail
    thumb_dir = Path(project_path) / "media" / "thumbnails" / "gameplay"
    thumb_path = thumb_dir / (Path(dest_filename).stem + ".jpg")
    extract_video_thumbnail(dest_path, thumb_path)
    record: dict = {
        "media_id":          media_id,
        "title":             title,
        "filename":          dest_filename,
        "original_filename": original_filename,
        "duration":          "",
        "overview":          "",
        "tagline":           "",
        "shotlist":          "false",
        "encodings":         "false",
    }
    if game:
        record["game"] = game

    upsert_json_record(project_path, record, media_type="gameplay")
    return record
