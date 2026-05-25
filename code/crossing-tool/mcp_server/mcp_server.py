"""
mcp_server/mcp_server.py — Crossing MCP server v2 for Playable Cinema.

Access policy
-------------
READ-ONLY  : data/, media/, preferences/  (reads only, never mutated)
OUTPUT     : output/  (write to output/ subdirectories only)
FORBIDDEN  : no writes to data/annotations/, data/shotlists/, data/metadata/,
             preferences/, or media/

Transport: stdio only — launched on-demand by the MCP client.

Configuration
-------------
1. Set project path once:
       crossing tool path /path/to/your/project

2. Or override at runtime:
       CROSSING_PROJECT=/path uv run python mcp_server/mcp_server.py

3. Claude Desktop config (via SSH to Linux server):
   {
     "mcpServers": {
       "crossing": {
         "command": "ssh",
         "args": [
           "playable-cinema",
           "bash -lc 'cd /path/to/crossing-tool && uv run python mcp_server/mcp_server.py'"
         ]
       }
     }
   }

Tool tiers
----------
Tier 1 — Read access (9 tools):
  list_movies, get_metadata, get_shotlist, get_subtitles,
  list_motifs, list_palettes, list_silhouettes,
  search_shots, search_vocabulary

Tier 1 — Image retrieval (5 tools, return JPEG thumbnails directly):
  get_best_frame, get_best_frames, get_palette_frames,
  get_motif_frames, get_context_frames

Tier 2 — Generation, writes to output/ only (5 tools):
  generate_flipbook, generate_mosaic, generate_cloud,
  generate_composition, generate_catalog

Output folder convention
------------------------
  <project>/output/
    flipbooks/           ← generate_flipbook
    mosaics/             ← generate_mosaic
    clouds/              ← generate_cloud
    compositions/        ← generate_composition
    catalogs/            ← generate_catalog
    claude/              ← Claude Work folder (scratch space for Claude artifacts)
      YYYY-MM-DD/        ← date-scoped subfolder created on first write
"""

from __future__ import annotations

import json
import os
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Silence model-loading noise that would corrupt the JSON-RPC stdio transport.
# Must be set before any third-party library is imported.
# ---------------------------------------------------------------------------
os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")
os.environ.setdefault("HF_HUB_VERBOSITY", "error")
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")
os.environ.setdefault("TRANSFORMERS_VERBOSITY", "error")

# ---------------------------------------------------------------------------
# Ensure the package root is importable when run directly.
# ---------------------------------------------------------------------------
_SCRIPT_DIR = Path(__file__).resolve().parent.parent  # crossing-tool root
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))

from mcp.server.fastmcp import FastMCP
from tool import prefs as _prefs

# ---------------------------------------------------------------------------
# Project path resolution
# ---------------------------------------------------------------------------

def _resolve_project_path() -> str | None:
    """Return the active project path from env var or saved user prefs."""
    env_path = os.environ.get("CROSSING_PROJECT")
    if env_path:
        return env_path
    return _prefs.get("path")


# ---------------------------------------------------------------------------
# Response helpers — every tool returns a JSON string
# ---------------------------------------------------------------------------

def _ok(**payload: Any) -> str:
    return json.dumps({"ok": True, **payload}, indent=2, default=str)


def _err(message: str, detail: str = "") -> str:
    body: dict = {"ok": False, "error": message}
    if detail:
        body["detail"] = detail
    return json.dumps(body, indent=2)


def _ctx() -> tuple[str, str] | str:
    """Validate and return (project_path, '').

    Returns an error JSON string instead of raising so that tools can do:
        result = _ctx()
        if isinstance(result, str): return result
        project_path, _ = result
    """
    project_path = _resolve_project_path()
    if not project_path:
        return _err(
            "No project path configured.",
            "Run: crossing tool path /your/project  or set CROSSING_PROJECT env var.",
        )
    if not Path(project_path).is_dir():
        return _err(
            f"Project path does not exist or is not a directory: {project_path}"
        )
    return project_path, ""


def _output_dir(project_path: str, subdir: str) -> Path:
    """Return and create an output subdirectory under <project>/output/."""
    d = Path(project_path) / "output" / subdir
    d.mkdir(parents=True, exist_ok=True)
    return d


def _claude_dir(project_path: str) -> Path:
    """Return the date-scoped Claude Work folder, creating it if needed."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    d = Path(project_path) / "output" / "claude" / today
    d.mkdir(parents=True, exist_ok=True)
    return d


# ---------------------------------------------------------------------------
# MCP server
# ---------------------------------------------------------------------------

mcp = FastMCP("crossing")


# ===========================================================================
# TIER 1 — READ-ONLY TOOLS
# ===========================================================================

@mcp.tool()
def list_movies(
    media_type: str = "movies",
    compact: bool = False,
    limit: int = 0,
    offset: int = 0,
) -> str:
    """List all films in the archive with their metadata.

    Returns a summary list of every film. By default includes title, year,
    tmdb id, filename, media_id, runtime, director, and availability flags.
    Use compact=True for a minimal listing (title, year, filename, media_id
    only). Use limit/offset for pagination.

    Args:
        media_type: "movies" (default) or "gameplay".
        compact:    Return minimal fields only (default False).
        limit:      Max films to return (0 = no limit).
        offset:     Skip this many films before returning (0 = no skip).

    Read-only. Reads: data/metadata/<media_type>.json
    """
    if media_type not in ("movies", "gameplay"):
        return _err(f"Invalid media_type {media_type!r}. Must be 'movies' or 'gameplay'.")

    result = _ctx()
    if isinstance(result, str):
        return result
    project_path, _ = result

    try:
        from data.metadata import get_metadata
        from data.shotlist import get_shotlist_path
        from data.motif import get_motif_path

        entries = get_metadata(project_path, media_type=media_type)
        ann_base = Path(project_path) / "data" / "annotations" / "shots" / media_type

        summary = []
        for e in entries:
            filename = e.get("filename", "")
            stem = Path(filename).stem if filename else ""
            summary.append({
                "title":       e.get("title", ""),
                "year":        e.get("year", ""),
                "tmdb":        e.get("tmdb") or e.get("tmdb_id") or "",
                "director":    e.get("director", ""),
                "runtime":     e.get("runtime", ""),
                "filename":    filename,
                "media_id":    e.get("media_id", ""),
                "has_shotlist": get_shotlist_path(project_path, filename, media_type).exists() if filename else False,
                "has_annotations": (ann_base / f"{stem}.json").exists() if stem else False,
                "has_motifs":  get_motif_path(project_path, filename, media_type).exists() if filename else False,
            })

        total = len(summary)
        if compact:
            summary = [
                {
                    "title":    e["title"],
                    "year":     e["year"],
                    "filename": e["filename"],
                    "media_id": e["media_id"],
                }
                for e in summary
            ]
        if offset:
            summary = summary[offset:]
        if limit:
            summary = summary[:limit]
        return _ok(media_type=media_type, total=total, count=len(summary), movies=summary)

    except Exception as exc:
        return _err(str(exc), traceback.format_exc())


@mcp.tool()
def get_metadata(
    film: str,
    media_type: str = "movies",
) -> str:
    """Get full metadata for one film by title, filename, or TMDb ID.

    Args:
        film:       Title substring, exact filename, or numeric TMDb ID.
        media_type: "movies" (default) or "gameplay".

    Read-only. Reads: data/metadata/<media_type>.json
    """
    result = _ctx()
    if isinstance(result, str):
        return result
    project_path, _ = result

    try:
        from data.metadata import get_metadata as _get_metadata
        entries = _get_metadata(project_path, query=film, media_type=media_type)
        if not entries:
            return _err(f"No film found matching {film!r} in {media_type}.")
        if len(entries) == 1:
            return _ok(film=entries[0])
        return _ok(matches=len(entries), films=entries)

    except Exception as exc:
        return _err(str(exc), traceback.format_exc())


@mcp.tool()
def get_shotlist(
    film: str,
    media_type: str = "movies",
    scene: str = "",
    compact: bool = False,
    limit: int = 0,
    offset: int = 0,
) -> str:
    """Get the shot list for one film.

    Returns shots with timecodes, frame numbers, shot_id, and captions.
    Optionally filter to a single scene number, paginate with limit/offset,
    or request compact mode (shot_id + times + Scene only).

    Args:
        film:       Title substring, exact filename, or numeric TMDb ID.
        media_type: "movies" (default) or "gameplay".
        scene:      Scene number to filter to (e.g. "3"). Empty = all scenes.
        compact:    Return minimal fields only: shot_id, start_time, end_time,
                    Scene (default False).
        limit:      Max shots to return (0 = no limit).
        offset:     Skip this many shots before returning (0 = no skip).

    Read-only. Reads: data/shotlists/<media_type>/<stem>.csv
    """
    result = _ctx()
    if isinstance(result, str):
        return result
    project_path, _ = result

    try:
        from data.metadata import get_metadata as _get_metadata
        from data.shotlist import read_shotlist

        entries = _get_metadata(project_path, query=film, media_type=media_type)
        if not entries:
            return _err(f"No film found matching {film!r}.")
        if len(entries) > 1:
            titles = [e.get("title", e.get("filename", "")) for e in entries]
            return _err(f"Ambiguous: {len(entries)} films match {film!r}.", f"Matches: {titles}")
        entry = entries[0]
        filename = entry["filename"]

        shots = read_shotlist(project_path, filename, media_type)
        if scene:
            shots = [s for s in shots if str(s.get("Scene", "")) == scene]

        total_shots = len(shots)
        if compact:
            shots = [
                {
                    "shot_id":    s.get("shot_id", ""),
                    "start_time": s.get("start_time", ""),
                    "end_time":   s.get("end_time", ""),
                    "Scene":      s.get("Scene", ""),
                }
                for s in shots
            ]
        if offset:
            shots = shots[offset:]
        if limit:
            shots = shots[:limit]

        return _ok(
            title=entry.get("title", ""),
            filename=filename,
            media_type=media_type,
            total_shots=total_shots,
            returned=len(shots),
            scene_filter=scene or None,
            compact=compact,
            shots=shots,
        )

    except FileNotFoundError as exc:
        return _err(str(exc))
    except Exception as exc:
        return _err(str(exc), traceback.format_exc())


@mcp.tool()
def get_subtitles(
    film: str,
    media_type: str = "movies",
    start_secs: float = 0.0,
    end_secs: float = 0.0,
    limit: int = 0,
    offset: int = 0,
) -> str:
    """Get subtitle cues for one film, optionally within a time window.

    Args:
        film:       Title substring, exact filename, or numeric TMDb ID.
        media_type: "movies" (default) or "gameplay".
        start_secs: Window start in seconds (0 = from beginning).
        end_secs:   Window end in seconds (0 = to end of film).
        limit:      Max cues to return (0 = no limit).
        offset:     Skip this many cues before returning (0 = no skip).

    Read-only. Reads: media/subtitles/<media_type>/<stem>.srt
    """
    result = _ctx()
    if isinstance(result, str):
        return result
    project_path, _ = result

    try:
        from data.metadata import get_metadata as _get_metadata
        from data.subtitles import subtitle_path_for, _parse_srt

        entries = _get_metadata(project_path, query=film, media_type=media_type)
        if not entries:
            return _err(f"No film found matching {film!r}.")
        if len(entries) > 1:
            titles = [e.get("title", e.get("filename", "")) for e in entries]
            return _err(f"Ambiguous: {len(entries)} films match {film!r}.", f"Matches: {titles}")
        entry = entries[0]
        filename = entry["filename"]

        srt_path = subtitle_path_for(project_path, media_type, filename)
        if srt_path is None:
            return _err(f"No subtitle file found for {filename!r}.")

        cues = _parse_srt(srt_path.read_text(encoding="utf-8"))

        if start_secs > 0 or end_secs > 0:
            end_limit = end_secs if end_secs > 0 else float("inf")
            cues = [c for c in cues if c.end_secs >= start_secs and c.start_secs <= end_limit]

        cue_list = [
            {"start_secs": c.start_secs, "end_secs": c.end_secs, "text": c.text}
            for c in cues
        ]
        total_cues = len(cue_list)
        if offset:
            cue_list = cue_list[offset:]
        if limit:
            cue_list = cue_list[:limit]
        return _ok(
            title=entry.get("title", ""),
            filename=filename,
            subtitle_path=str(srt_path),
            total_cues=total_cues,
            returned=len(cue_list),
            window={"start_secs": start_secs, "end_secs": end_secs} if (start_secs or end_secs) else None,
            cues=cue_list,
        )

    except Exception as exc:
        return _err(str(exc), traceback.format_exc())


@mcp.tool()
def list_motifs(
    film: str,
    media_type: str = "movies",
    include_full_shots: bool = False,
) -> str:
    """Get the motif word sequence for one film.

    Returns the ordered per-shot motif word list and the film-level semantic
    title. By default only the word list is returned (compact). Pass
    include_full_shots=True for the complete per-shot motif objects (includes
    model scores, timing, etc.).

    Args:
        film:               Title substring, exact filename, or numeric TMDb ID.
        media_type:         "movies" (default) or "gameplay".
        include_full_shots: Include full per-shot motif objects (default False).

    Read-only. Reads: data/motifs/<media_type>/<stem>.json
    """
    result = _ctx()
    if isinstance(result, str):
        return result
    project_path, _ = result

    try:
        from data.metadata import get_metadata as _get_metadata
        from data.motif import load_motif_doc

        entries = _get_metadata(project_path, query=film, media_type=media_type)
        if not entries:
            return _err(f"No film found matching {film!r}.")
        if len(entries) > 1:
            titles = [e.get("title", e.get("filename", "")) for e in entries]
            return _err(f"Ambiguous: {len(entries)} films match {film!r}.", f"Matches: {titles}")
        entry = entries[0]
        filename = entry["filename"]

        doc = load_motif_doc(project_path, filename, media_type)
        shots = doc.get("shots", [])

        return _ok(
            title=entry.get("title", ""),
            filename=filename,
            film_title=doc.get("title"),
            shot_count=len(shots),
            motifs=[s.get("value", "") for s in shots],
            **( {"shots": shots} if include_full_shots else {} ),
        )

    except Exception as exc:
        return _err(str(exc), traceback.format_exc())


@mcp.tool()
def list_palettes(
    film: str,
    media_type: str = "movies",
    include_full: bool = False,
) -> str:
    """Get the colour palette data for one film's shots.

    Each shot entry contains foreground and background dominant colours in
    RGB, LAB, luminance, and chroma. Returns a compact summary (shot_id +
    fg/bg RGB + luminance) by default. Pass include_full=True to receive the
    complete palette document (large — one entry per shot with all colour
    channels).

    Args:
        film:         Title substring, exact filename, or numeric TMDb ID.
        media_type:   "movies" (default) or "gameplay".
        include_full: Include the full per-shot palette data (default False).
                      Warning: can be very large for feature-length films.

    Read-only. Reads: data/palettes/<media_type>/<stem>.json
    """
    result = _ctx()
    if isinstance(result, str):
        return result
    project_path, _ = result

    try:
        from data.metadata import get_metadata as _get_metadata
        from data.palette import get_palette_path

        entries = _get_metadata(project_path, query=film, media_type=media_type)
        if not entries:
            return _err(f"No film found matching {film!r}.")
        if len(entries) > 1:
            titles = [e.get("title", e.get("filename", "")) for e in entries]
            return _err(f"Ambiguous: {len(entries)} films match {film!r}.", f"Matches: {titles}")
        entry = entries[0]
        filename = entry["filename"]

        palette_path = get_palette_path(project_path, filename, media_type)
        if not palette_path.exists():
            return _err(f"No palette cache found for {filename!r}. Run: crossing palette build {filename!r}")

        palette_doc = json.loads(palette_path.read_text(encoding="utf-8"))
        shots = palette_doc.get("shots", [])

        # Build a compact summary: shot_id + fg/bg dominant RGB
        summary = []
        for s in shots:
            fg = s.get("foreground") or {}
            bg = s.get("background") or {}
            summary.append({
                "shot_id":  s.get("shot_id", ""),
                "fg_rgb":   fg.get("rgb"),
                "bg_rgb":   bg.get("rgb"),
                "fg_lum":   fg.get("luminance"),
                "bg_lum":   bg.get("luminance"),
            })

        return _ok(
            title=entry.get("title", ""),
            filename=filename,
            shot_count=len(shots),
            summary=summary,
            **( {"full": palette_doc} if include_full else {} ),
        )

    except Exception as exc:
        return _err(str(exc), traceback.format_exc())


@mcp.tool()
def list_silhouettes(
    word: str,
    field: str = "objects",
    scope: str = "all",
    media_type: str = "movies",
) -> str:
    """List cached silhouette extractions for a vocabulary word.

    Silhouettes are CLIP+SAM polygon masks of the best frame for a given
    word+field combination. This tool lists what has already been extracted.

    Args:
        word:       The vocabulary term to look up (e.g. "horse", "gun").
        field:      Annotation field: "objects", "animals", "humans", "wearing".
        scope:      "all" (full corpus) or "movie-<media_id>" for one film.
        media_type: "movies" (default) or "gameplay".

    Read-only. Reads: data/silhouettes/<media_type>/<scope>/<field>/<word>/
    """
    result = _ctx()
    if isinstance(result, str):
        return result
    project_path, _ = result

    try:
        from services.silhouette import silhouette_cache_dir, load_silhouette, SILHOUETTE_VERSION

        cache_dir = silhouette_cache_dir(project_path, media_type, scope, field, word)
        if not cache_dir.exists():
            return _ok(
                word=word, field=field, scope=scope,
                found=False, count=0, entries=[],
            )

        entries = sorted(cache_dir.glob(f"best__*__{SILHOUETTE_VERSION}.json"))
        loaded = []
        for p in entries:
            doc = load_silhouette(p)
            if doc:
                loaded.append({
                    "file": p.name,
                    "media_id":    doc.get("media_id", ""),
                    "shot_id":     doc.get("shot_id", ""),
                    "frame_index": doc.get("frame_index"),
                    "score":       doc.get("score"),
                    "polygon_points": len(doc.get("polygon", [])),
                })

        return _ok(
            word=word, field=field, scope=scope,
            found=len(loaded) > 0,
            count=len(loaded),
            cache_dir=str(cache_dir),
            entries=loaded,
        )

    except Exception as exc:
        return _err(str(exc), traceback.format_exc())


@mcp.tool()
def search_shots(
    query: str,
    films: list[str] | None = None,
    field: str = "",
    limit: int = 40,
    limit_per_film: int = 0,
    media_type: str = "movies",
) -> str:
    """Search shot annotations across the archive by keyword or phrase.

    Scores shots using exact phrase match → full token overlap → partial
    token overlap. Returns matched shots with timecodes and annotation text.

    Args:
        query:         Search string (case-insensitive).
        films:         Optional list of film titles/filenames to restrict search.
                       Omit or pass null to search all films.
        field:         Restrict to one annotation field: "setting", "objects",
                       "animals", "humans", "wearing", "action", "description".
                       Empty string searches all fields.
        limit:         Maximum total results (default 40, 0 = no limit).
        limit_per_film: Maximum results per film (0 = no per-film limit).
        media_type:    "movies" (default) or "gameplay".

    Read-only. Reads: data/annotations/shots/, data/shotlists/, data/metadata/
    """
    result = _ctx()
    if isinstance(result, str):
        return result
    project_path, _ = result

    try:
        from services.search import search_shots as _search

        use_all = not films
        res = _search(
            query=query,
            scopes=films or [],
            field=field or None,
            limit=limit or None,
            limit_per_item=limit_per_film or None,
            use_all=use_all,
            project_path=project_path,
            media_type=media_type,
        )
        return _ok(
            query=query,
            field=field or None,
            films=films,
            result_count=len(res.get("results", [])),
            results=res.get("results", []),
        )

    except Exception as exc:
        return _err(str(exc), traceback.format_exc())


@mcp.tool()
def search_vocabulary(
    field: str,
    top: int = 50,
    sort: str = "count",
    media_type: str = "movies",
) -> str:
    """Query the vocabulary index for a given annotation field.

    Returns canonicalized terms sorted by frequency or alphabetically.
    Useful for exploring what words appear most often in a given field.

    Args:
        field:      Annotation field: "setting", "objects", "animals",
                    "humans", "wearing".
        top:        Number of terms to return (default 50, 0 = all).
        sort:       "count" (most frequent first) or "alphabetical".
        media_type: "movies" (default) or "gameplay".

    Read-only. Reads: data/index/vocabulary_<media_type>.json
    """
    result = _ctx()
    if isinstance(result, str):
        return result
    project_path, _ = result

    try:
        from services.vocabulary_index import get_vocabulary, get_vocabulary_fields

        available_fields = get_vocabulary_fields(project_path, media_type)
        if field not in available_fields:
            return _err(
                f"Field {field!r} not in vocabulary index.",
                f"Available fields: {available_fields}",
            )

        items = get_vocabulary(field, project_path, media_type=media_type, sort=sort)
        if top and top < len(items):
            items = items[:top]

        return _ok(
            field=field,
            sort=sort,
            media_type=media_type,
            total_terms=len(items),
            vocabulary=items,
        )

    except FileNotFoundError as exc:
        return _err(
            str(exc),
            "Build the vocabulary index first: crossing vocabulary build",
        )
    except Exception as exc:
        return _err(str(exc), traceback.format_exc())


# ===========================================================================
# TIER 1 — IMAGE RETRIEVAL TOOLS (read-only, return actual frame pixels)
# ===========================================================================
#
# These tools return a mixed list of [str, Image, Image, …]:
#   • The first element is a JSON string with shot metadata summaries.
#   • Subsequent elements are FastMCP Image objects (JPEG thumbnails).
# Claude Desktop renders Image objects inline in the chat window, enabling
# direct visual inspection of frames without any filesystem access.
#
# Payload budget: default width=400 px → ~40-80 KB per JPEG.
#   limit=4 → ~200 KB total; well under the 1 MB MCP transport cap.
# The service layer enforces a 900 KB hard ceiling across the batch.

from mcp.server.fastmcp import Image as _MCPImage


def _frames_to_mcp(frames: list[dict]) -> list:
    """Convert a list of frame_retrieval result dicts to [metadata_str, Image, …]."""
    summaries = [
        {
            "film":       f["film_title"],
            "shot_id":    f["shot_id"],
            "start_time": f["start_time"],
            "end_time":   f["end_time"],
            "metadata":   f["metadata"],
        }
        for f in frames
    ]
    out: list = [json.dumps({"ok": True, "count": len(frames), "shots": summaries}, indent=2)]
    for f in frames:
        out.append(_MCPImage(data=f["image_data"], format="jpeg"))
    return out


@mcp.tool()
def test_image_return() -> list:
    """Minimal smoke-test: verify Claude Desktop renders an inline image from MCP.

    Creates a synthetic 40×30 gradient JPEG using only PIL — no Crossing data
    required.  Returns [metadata_str, Image] using the identical structure as
    all Crossing image retrieval tools.

    If the coloured rectangle appears below this text, the MCP image pipeline
    works and the issue lies in the Crossing frame-retrieval path.
    If only text appears, the issue is in Claude Desktop's rendering of
    image/jpeg tool results.

    Read-only. No project path required.
    """
    import io as _io
    from PIL import Image as _PIL

    # Build a 40×30 gradient so it's visually obvious if it renders.
    img = _PIL.new("RGB", (40, 30))
    px = img.load()
    for x in range(40):
        for y in range(30):
            px[x, y] = (int(x * 6.3), int(y * 8.5), 120)

    buf = _io.BytesIO()
    img.save(buf, format="JPEG", quality=85)
    jpeg_bytes = buf.getvalue()

    return [
        json.dumps({
            "ok":          True,
            "test":        "test_image_return",
            "description": "40×30 synthetic gradient JPEG — should render inline below",
            "bytes":       len(jpeg_bytes),
            "mime_type":   "image/jpeg",
        }),
        _MCPImage(data=jpeg_bytes, format="jpeg"),
    ]


@mcp.tool()
def test_image_png() -> list:
    """Variant of test_image_return using PNG instead of JPEG.

    If test_image_return (JPEG) fails but this (PNG) works, Claude Desktop
    has a JPEG rendering issue and all Crossing tools need to switch to PNG.

    Read-only. No project path required.
    """
    import io as _io
    from PIL import Image as _PIL

    img = _PIL.new("RGB", (40, 30))
    px = img.load()
    for x in range(40):
        for y in range(30):
            px[x, y] = (int(x * 6.3), int(y * 8.5), 180)

    buf = _io.BytesIO()
    img.save(buf, format="PNG")
    png_bytes = buf.getvalue()

    return [
        json.dumps({
            "ok":          True,
            "test":        "test_image_png",
            "description": "40×30 synthetic gradient PNG — should render inline below",
            "bytes":       len(png_bytes),
            "mime_type":   "image/png",
        }),
        _MCPImage(data=png_bytes, format="png"),
    ]


@mcp.tool()
def debug_frame_bytes(
    film: str,
    shot_id: str,
    media_type: str = "movies",
    width: int = 400,
) -> str:
    """Diagnostic: fetch a frame and report byte-level details WITHOUT returning the image.

    Use this to confirm the retrieval path is working before testing image rendering.
    Returns: source (cache or video), byte count, first/last 8 bytes as hex,
    PIL image size, and whether PIL can re-open the encoded JPEG without error.

    Read-only. Reads: media/frames/best/ and/or media/videos/
    """
    result = _ctx()
    if isinstance(result, str):
        return result
    project_path, _ = result

    try:
        import io as _io
        from PIL import Image as _PIL
        from data.metadata import get_metadata as _get_metadata
        from data.shotlist import read_shotlist
        from services.frame_match import best_frame_path as _bf_path
        from generators.mosaic import _find_video_path, extract_frame_pil, frame_from_pct

        entries = _get_metadata(project_path, query=film, media_type=media_type)
        if not entries:
            return _err(f"No film found matching {film!r}.")
        if len(entries) > 1:
            titles = [e.get("title", e.get("filename", "")) for e in entries]
            return _err(f"Ambiguous: {len(entries)} films match. Matches: {titles}")

        entry    = entries[0]
        filename = entry["filename"]

        shots     = read_shotlist(project_path, filename, media_type)
        shot_data = next((s for s in shots if s.get("shot_id") == shot_id), None)
        if shot_data is None:
            return _err(f"Shot {shot_id!r} not found in shotlist for {filename!r}.")

        source    = "unknown"
        raw_bytes = None

        # --- Priority 1: best-frame PNG cache ---
        bf_png = _bf_path(project_path, media_type, filename, shot_id)
        if bf_png.exists():
            try:
                img_pil = _PIL.open(bf_png).convert("RGB")
                pil_size = img_pil.size

                # Resize + JPEG-encode (same as production)
                from services.frame_retrieval import _resize_pil, _pil_to_jpeg_bytes
                img_resized = _resize_pil(img_pil, width)
                raw_bytes   = _pil_to_jpeg_bytes(img_resized)
                source      = f"best_frame_cache:{bf_png.name}"
                resized_size = img_resized.size
            except Exception as exc:
                source = f"best_frame_cache:ERROR:{exc}"

        # --- Priority 2: video extraction ---
        if raw_bytes is None:
            sf = shot_data.get("start_frame")
            if sf is not None:
                vpath = _find_video_path(project_path, Path(filename).stem)
                if vpath:
                    ef = shot_data.get("end_frame")
                    fidx = frame_from_pct(int(sf), int(ef) if ef else int(sf), 0.5)
                    img_pil = extract_frame_pil(vpath, fidx)
                    if img_pil:
                        pil_size = img_pil.size
                        from services.frame_retrieval import _resize_pil, _pil_to_jpeg_bytes
                        img_resized = _resize_pil(img_pil, width)
                        raw_bytes   = _pil_to_jpeg_bytes(img_resized)
                        source      = f"video:{vpath.name}@frame{fidx}"
                        resized_size = img_resized.size

        if raw_bytes is None:
            return _err("Could not retrieve any frame bytes.", "Check that best-frame cache or video exists.")

        # Verify the JPEG is re-openable by PIL
        reopen_ok = False
        reopen_size = None
        try:
            check_buf = _io.BytesIO(raw_bytes)
            check_img = _PIL.open(check_buf)
            check_img.verify()
            reopen_ok   = True
            reopen_size = check_img.size if hasattr(check_img, 'size') else "unknown"
        except Exception as exc:
            reopen_ok = False
            reopen_size = str(exc)

        return _ok(
            source          = source,
            film            = entry.get("title", filename),
            shot_id         = shot_id,
            pil_size_before = pil_size if 'pil_size' in dir() else "n/a",
            pil_size_after  = resized_size if 'resized_size' in dir() else "n/a",
            jpeg_bytes      = len(raw_bytes),
            first_8_hex     = raw_bytes[:8].hex(),
            last_8_hex      = raw_bytes[-8:].hex(),
            jpeg_reopen_ok  = reopen_ok,
            jpeg_reopen_size = str(reopen_size),
            notes = (
                "If jpeg_reopen_ok=true and jpeg_bytes > 100, bytes are valid. "
                "JPEG magic bytes should start with 'ffd8ffe0' or 'ffd8ffe1'."
            ),
        )

    except Exception as exc:
        return _err(str(exc), traceback.format_exc())


@mcp.tool()
def get_best_frame(
    film: str,
    shot_id: str,
    media_type: str = "movies",
    width: int = 400,
) -> list:
    """Retrieve a single frame thumbnail for a specific shot.

    Returns the actual image content so Claude can see the frame directly.
    Uses the pre-computed best-frame PNG cache when available (fastest path),
    with a fallback to extracting the midpoint frame from the video file.

    Args:
        film:       Title substring, filename, or TMDb ID of the film.
        shot_id:    Canonical shot identifier (e.g. "tmdb_4638@f001234-f001456").
        media_type: "movies" (default) or "gameplay".
        width:      Thumbnail width in pixels (default 400; max recommended 800).

    Read-only. Reads: media/frames/best/ and/or media/videos/
    """
    result = _ctx()
    if isinstance(result, str):
        return [result]
    project_path, _ = result

    try:
        from services.frame_retrieval import retrieve_single_frame
        frame = retrieve_single_frame(
            project_path, film, shot_id, media_type,
            width=width,
        )
        return _frames_to_mcp([frame])

    except ValueError as exc:
        return [_err(str(exc))]
    except Exception as exc:
        return [_err(str(exc), traceback.format_exc())]


@mcp.tool()
def get_best_frames(
    query: str,
    limit: int = 4,
    films: list[str] | None = None,
    field: str = "",
    width: int = 400,
    media_type: str = "movies",
) -> list:
    """Search shots by keyword and return frame thumbnails for the top results.

    Runs the same annotation search as search_shots, then fetches one
    representative frame per result.  Returns actual image content so Claude
    can see the frames directly.

    Args:
        query:      Search string (case-insensitive keyword or phrase).
        limit:      Max frames to return (default 4; max recommended 8).
        films:      Optional list of film titles to restrict search.
        field:      Annotation field to restrict search (empty = all fields).
        width:      Thumbnail width in pixels (default 400).
        media_type: "movies" (default) or "gameplay".

    Read-only. Reads: data/annotations/, media/frames/best/, media/videos/
    """
    result = _ctx()
    if isinstance(result, str):
        return [result]
    project_path, _ = result

    try:
        from services.frame_retrieval import retrieve_frames_for_query
        frames = retrieve_frames_for_query(
            project_path, query,
            films=films, field=field or None,
            limit=max(1, min(limit, 12)),
            media_type=media_type,
            width=width,
        )
        if not frames:
            return [_err(f"No frames found for query {query!r}.", "Try a different query or broaden the film filter.")]
        return _frames_to_mcp(frames)

    except Exception as exc:
        return [_err(str(exc), traceback.format_exc())]


@mcp.tool()
def get_palette_frames(
    warm: bool = False,
    cold: bool = False,
    dark: bool = False,
    bright: bool = False,
    low_chroma: bool = False,
    high_chroma: bool = False,
    foreground_only: bool = False,
    background_only: bool = False,
    luminance_min: float = 0.0,
    luminance_max: float = 1.0,
    chroma_min: float = 0.0,
    chroma_max: float = 1.0,
    films: list[str] | None = None,
    limit: int = 4,
    width: int = 400,
    media_type: str = "movies",
) -> list:
    """Retrieve frame thumbnails for shots matching colour-palette filters.

    Filters the pre-computed palette cache by colour-space characteristics —
    no re-analysis is performed.  At least one filter flag must be True or a
    luminance/chroma range must be non-default.  Returns actual image content.

    Args:
        warm:            Shots with warm hues (positive a*/b* in LAB space).
        cold:            Shots with cool/blue hues.
        dark:            Shots with luminance < 0.3.
        bright:          Shots with luminance > 0.7.
        low_chroma:      Shots with chroma < 0.15 (desaturated/grey).
        high_chroma:     Shots with chroma > 0.30 (vivid colours).
        foreground_only: Only check foreground region of each shot.
        background_only: Only check background region of each shot.
        luminance_min:   Minimum luminance [0.0–1.0] (default 0.0 = off).
        luminance_max:   Maximum luminance [0.0–1.0] (default 1.0 = off).
        chroma_min:      Minimum chroma [0.0–1.0] (default 0.0 = off).
        chroma_max:      Maximum chroma [0.0–1.0] (default 1.0 = off).
        films:           Optional list of film titles to restrict search.
        limit:           Max frames to return (default 4; max recommended 8).
        width:           Thumbnail width in pixels (default 400).
        media_type:      "movies" (default) or "gameplay".

    Read-only. Reads: data/palettes/, media/frames/best/, media/videos/
    """
    result = _ctx()
    if isinstance(result, str):
        return [result]
    project_path, _ = result

    # Pass through only non-default range values.
    lum_min = luminance_min if luminance_min > 0.0 else None
    lum_max = luminance_max if luminance_max < 1.0 else None
    chr_min = chroma_min if chroma_min > 0.0 else None
    chr_max = chroma_max if chroma_max < 1.0 else None

    try:
        from services.frame_retrieval import retrieve_palette_frames
        frames = retrieve_palette_frames(
            project_path,
            films=films, media_type=media_type,
            limit=max(1, min(limit, 12)),
            width=width,
            warm=warm, cold=cold, dark=dark, bright=bright,
            low_chroma=low_chroma, high_chroma=high_chroma,
            foreground_only=foreground_only, background_only=background_only,
            luminance_min=lum_min, luminance_max=lum_max,
            chroma_min=chr_min, chroma_max=chr_max,
        )
        if not frames:
            return [_err("No frames found matching palette filters.", "Check that palette data exists: crossing palette list")]
        return _frames_to_mcp(frames)

    except ValueError as exc:
        return [_err(str(exc))]
    except Exception as exc:
        return [_err(str(exc), traceback.format_exc())]


@mcp.tool()
def get_motif_frames(
    motif: str,
    films: list[str] | None = None,
    limit: int = 4,
    width: int = 400,
    media_type: str = "movies",
) -> list:
    """Retrieve frame thumbnails for shots whose motif word matches *motif*.

    Exact match is tried first; falls back to substring match if needed.
    Returns actual image content so Claude can see the frames directly.

    Args:
        motif:      Motif word to search for (e.g. "riding", "carrying").
        films:      Optional list of film titles to restrict search.
        limit:      Max frames to return (default 4; max recommended 8).
        width:      Thumbnail width in pixels (default 400).
        media_type: "movies" (default) or "gameplay".

    Read-only. Reads: data/motifs/, media/frames/best/, media/videos/
    """
    result = _ctx()
    if isinstance(result, str):
        return [result]
    project_path, _ = result

    try:
        from services.frame_retrieval import retrieve_motif_frames
        frames = retrieve_motif_frames(
            project_path, motif,
            films=films, media_type=media_type,
            limit=max(1, min(limit, 12)),
            width=width,
        )
        if not frames:
            return [_err(f"No frames found for motif {motif!r}.", "Check available motifs: crossing search motifs list")]
        return _frames_to_mcp(frames)

    except Exception as exc:
        return [_err(str(exc), traceback.format_exc())]


@mcp.tool()
def get_context_frames(
    film: str,
    shot_id: str,
    window: int = 3,
    width: int = 400,
    media_type: str = "movies",
) -> list:
    """Retrieve frame thumbnails for a shot and its neighboring shots.

    Shows up to *window* shots before and *window* shots after the central
    shot, providing visual context for a sequence.  The central shot is
    flagged as ``is_center: true`` in the metadata summary.

    Args:
        film:       Title substring, filename, or TMDb ID of the film.
        shot_id:    Central shot identifier.
        window:     Shots to show on each side (default 3; max 6).
        width:      Thumbnail width in pixels (default 400).
        media_type: "movies" (default) or "gameplay".

    Read-only. Reads: data/shotlists/, media/frames/best/, media/videos/
    """
    result = _ctx()
    if isinstance(result, str):
        return [result]
    project_path, _ = result

    try:
        from services.frame_retrieval import retrieve_context_frames
        frames = retrieve_context_frames(
            project_path, film, shot_id,
            window=max(1, min(window, 6)),
            media_type=media_type,
            width=width,
        )
        if not frames:
            return [_err(f"No frames retrieved for context around {shot_id!r}.", "Ensure video or best-frame cache exists for this film.")]
        return _frames_to_mcp(frames)

    except ValueError as exc:
        return [_err(str(exc))]
    except Exception as exc:
        return [_err(str(exc), traceback.format_exc())]


# ===========================================================================
# TIER 2 — GENERATION TOOLS (write to output/ only)
# ===========================================================================

@mcp.tool()
def generate_flipbook(
    film: str,
    media_type: str = "movies",
    force: bool = False,
) -> str:
    """Generate a cinematic flipbook PDF for one film.

    Each page shows one shot's motif word rendered in the shot's palette
    foreground colour on its palette background colour. Front cover shows
    the film semantic title; back cover shows original title and year.

    Requires: motif data (data/motifs/) and palette data (data/palettes/).
    Output: output/flipbooks/<stem>-flipbook.pdf

    Args:
        film:       Title substring, exact filename, or numeric TMDb ID.
        media_type: "movies" (default) or "gameplay".
        force:      Overwrite if PDF already exists (default False).

    Output-writing. Reads: data/motifs/, data/palettes/
                   Writes: output/flipbooks/
    """
    result = _ctx()
    if isinstance(result, str):
        return result
    project_path, _ = result

    try:
        from data.metadata import get_metadata as _get_metadata
        from generators.flipbook import generate_flipbook_for_movie

        entries = _get_metadata(project_path, query=film, media_type=media_type)
        if not entries:
            return _err(f"No film found matching {film!r}.")
        if len(entries) > 1:
            titles = [e.get("title", e.get("filename", "")) for e in entries]
            return _err(f"Ambiguous: {len(entries)} films match {film!r}.", f"Matches: {titles}")

        filename = entries[0]["filename"]
        res = generate_flipbook_for_movie(
            project_path, filename, media_type, force=force
        )
        return _ok(**res)

    except FileExistsError as exc:
        return _err(str(exc), "Pass force=true to overwrite.")
    except FileNotFoundError as exc:
        return _err(str(exc), "Ensure motif and palette data exist for this film.")
    except Exception as exc:
        return _err(str(exc), traceback.format_exc())


@mcp.tool()
def generate_mosaic(
    query: str,
    films: list[str] | None = None,
    field: str = "",
    limit: int = 40,
    layout: str = "landscape",
    media_type: str = "movies",
) -> str:
    """Generate a mosaic contact sheet from shot search results.

    Searches annotations for *query*, then assembles matching frames into a
    grid image. Each tile shows one shot frame with its film title as caption.

    Output: output/mosaics/search-mosaic-<timestamp>.png

    Args:
        query:      Search string — same semantics as search_shots.
        films:      Optional list of film titles to restrict search.
        field:      Annotation field to restrict search (empty = all fields).
        limit:      Max frames in the mosaic (default 40).
        layout:     "landscape" (default) or "portrait".
        media_type: "movies" (default) or "gameplay".

    Output-writing. Reads: data/annotations/, media/videos/
                   Writes: output/mosaics/
    """
    result = _ctx()
    if isinstance(result, str):
        return result
    project_path, _ = result

    try:
        from services.search import search_shots as _search
        from generators.mosaic import mosaic_from_search_results

        use_all = not films
        res = _search(
            query=query,
            scopes=films or [],
            field=field or None,
            limit=limit or None,
            limit_per_item=None,
            use_all=use_all,
            project_path=project_path,
            media_type=media_type,
        )
        results = res.get("results", [])
        if not results:
            return _err(f"No shots matched {query!r}. Cannot build mosaic.")

        out_path = mosaic_from_search_results(
            results, project_path, layout=layout, show_captions=True, verbose=False
        )
        return _ok(
            query=query,
            field=field or None,
            result_count=len(results),
            output_path=str(out_path),
        )

    except ValueError as exc:
        return _err(str(exc))
    except Exception as exc:
        return _err(str(exc), traceback.format_exc())


@mcp.tool()
def generate_cloud(
    film: str = "",
    field: str = "",
    style: str = "default",
    max_words: int = 150,
    min_count: int = 2,
    media_type: str = "movies",
) -> str:
    """Generate a word-cloud PDF from shot annotation text.

    Draws word frequency as visual weight. Font size scales with frequency.
    Stopwords are removed; remaining words reflect the symbolic register of
    the annotations.

    Output: output/clouds/<scope>-<field>-cloud-<timestamp>.pdf

    Args:
        film:       Film title/filename to restrict to one film.
                    Empty = use the full corpus.
        field:      Annotation field ("setting", "objects", "description", …).
                    Empty = all fields.
        style:      Style preset name (default = "default"; "western" available).
        max_words:  Maximum words to render (default 150).
        min_count:  Minimum occurrences to include (default 2).
        media_type: "movies" (default) or "gameplay".

    Output-writing. Reads: data/annotations/
                   Writes: output/clouds/
    """
    result = _ctx()
    if isinstance(result, str):
        return result
    project_path, _ = result

    try:
        from generators.cloud import cloud_from_annotations

        out_path = cloud_from_annotations(
            project_path,
            scope=film or None,
            field=field or None,
            media_type=media_type,
            max_words=max_words,
            min_count=min_count,
            style=style,
        )
        return _ok(
            film=film or None,
            field=field or None,
            style=style,
            output_path=str(out_path),
        )

    except ValueError as exc:
        return _err(str(exc))
    except Exception as exc:
        return _err(str(exc), traceback.format_exc())


@mcp.tool()
def generate_composition(
    query: str,
    films: list[str] | None = None,
    field: str = "",
    orientation: str = "portrait",
    seed: int = 0,
    media_type: str = "movies",
) -> str:
    """Generate a single-frame composition image from a shot search result.

    Searches for *query*, picks one matching shot at random (reproducible with
    *seed*), extracts the frame, fits it to the canvas, and saves a JPEG.

    Output: output/compositions/<query>+<date>+<time>.jpg

    Args:
        query:       Search string — semantics as search_shots.
        films:       Optional list of film titles to restrict search.
        field:       Annotation field to restrict search.
        orientation: "portrait" (1240×1754, poster-like) or
                     "landscape" (1920×1080, cinematic).
        seed:        RNG seed for reproducibility (0 = random).
        media_type:  "movies" (default) or "gameplay".

    Output-writing. Reads: data/annotations/, media/videos/
                   Writes: output/compositions/
    """
    result = _ctx()
    if isinstance(result, str):
        return result
    project_path, _ = result

    try:
        from services.search import search_shots as _search
        from generators.composition import (
            choose_background, build_tableau, save_tableau
        )

        use_all = not films
        res = _search(
            query=query,
            scopes=films or [],
            field=field or None,
            limit=200,
            limit_per_item=None,
            use_all=use_all,
            project_path=project_path,
            media_type=media_type,
        )
        results = res.get("results", [])
        if not results:
            return _err(f"No shots matched {query!r}. Cannot build composition.")

        chosen = choose_background(results, seed=seed or None)
        tableau = build_tableau(chosen, project_path, orientation=orientation)
        out_dir = _output_dir(project_path, "compositions")
        out_path = save_tableau(tableau, query, out_dir)

        return _ok(
            query=query,
            orientation=orientation,
            seed=seed or None,
            chosen_film=chosen.get("movie_title", ""),
            chosen_shot_id=chosen.get("shot_id", ""),
            output_path=str(out_path),
        )

    except ValueError as exc:
        return _err(str(exc))
    except Exception as exc:
        return _err(str(exc), traceback.format_exc())


@mcp.tool()
def generate_catalog(
    films: list[str] | None = None,
    media_type: str = "movies",
    include_annotations: bool = False,
    include_motifs: bool = False,
    inline: bool = False,
) -> str:
    """Generate a JSON catalog of films with metadata and data availability.

    Produces a structured JSON file in output/catalogs/ that Claude can
    reference in subsequent work. The catalog lists every film with its
    metadata, shot counts, and optionally its annotation fields and motifs.

    By default only the output path is returned (the catalog is written to
    disk). Pass inline=True to also receive the full catalog object in the
    response (only advisable for small selections of films).

    Output: output/catalogs/catalog-<media_type>-<timestamp>.json

    Args:
        films:               Optional list of film titles/filenames to include.
                             Omit or pass null to include all films.
        media_type:          "movies" (default) or "gameplay".
        include_annotations: Include per-shot annotation summaries (large output).
        include_motifs:      Include motif word sequences for each film.
        inline:              Return the full catalog object in the response
                             (default False — return only output_path).

    Output-writing. Reads: data/metadata/, data/motifs/, data/shotlists/,
                            data/annotations/
                   Writes: output/catalogs/
    """
    result = _ctx()
    if isinstance(result, str):
        return result
    project_path, _ = result

    try:
        from data.metadata import get_metadata as _get_metadata
        from data.shotlist import get_shotlist_path, read_shotlist
        from data.motif import load_motif_doc

        all_entries = _get_metadata(project_path, media_type=media_type)
        ann_base = Path(project_path) / "data" / "annotations" / "shots" / media_type

        # Filter to requested films if given
        if films:
            from services.search import _resolve_movies_exact_first
            selected, _ = _resolve_movies_exact_first(films, False, all_entries)
        else:
            selected = all_entries

        catalog_entries = []
        for entry in selected:
            filename = entry.get("filename", "")
            stem = Path(filename).stem if filename else ""

            shotlist_path = get_shotlist_path(project_path, filename, media_type) if filename else None
            ann_path = ann_base / f"{stem}.json" if stem else None

            record: dict = {
                "title":      entry.get("title", ""),
                "year":       entry.get("year", ""),
                "tmdb":       entry.get("tmdb") or entry.get("tmdb_id") or "",
                "director":   entry.get("director", ""),
                "runtime":    entry.get("runtime", ""),
                "filename":   filename,
                "media_id":   entry.get("media_id", ""),
                "overview":   entry.get("overview", ""),
                "tagline":    entry.get("tagline", ""),
                "has_shotlist":    shotlist_path.exists() if shotlist_path else False,
                "has_annotations": ann_path.exists() if ann_path else False,
                "shot_count":      0,
            }

            if shotlist_path and shotlist_path.exists():
                try:
                    shots = read_shotlist(project_path, filename, media_type)
                    record["shot_count"] = len(shots)
                    if include_annotations and ann_path and ann_path.exists():
                        record["shots"] = shots
                except Exception:
                    pass

            if include_motifs and filename:
                motif_doc = load_motif_doc(project_path, filename, media_type)
                record["motifs"] = [s.get("value", "") for s in motif_doc.get("shots", [])]
                film_title = motif_doc.get("title")
                if film_title:
                    record["film_motif_title"] = film_title.get("value", "")

            catalog_entries.append(record)

        # Write output
        out_dir = _output_dir(project_path, "catalogs")
        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d-%H%M%S")
        out_path = out_dir / f"catalog-{media_type}-{timestamp}.json"

        catalog = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "media_type":   media_type,
            "film_count":   len(catalog_entries),
            "films":        catalog_entries,
        }
        out_path.write_text(json.dumps(catalog, indent=2, default=str), encoding="utf-8")

        return _ok(
            media_type=media_type,
            film_count=len(catalog_entries),
            output_path=str(out_path),
            **( {"catalog": catalog} if inline else {} ),
        )

    except Exception as exc:
        return _err(str(exc), traceback.format_exc())


# ===========================================================================
# Tier 3 — Analysis tools (read-only archive analysis)
# ===========================================================================

@mcp.tool()
def compare_motifs(
    films: list[str] | None = None,
    mode: str = "overlap",
    limit: int | None = None,
    media_type: str = "movies",
) -> str:
    """Compare motif usage across films.

    Reads cached motif JSON files — no re-inference.

    Args:
        films:      Film title substrings to compare. Omit for all films.
        mode:       "overlap"   — words shared across ≥ 2 films (default)
                    "frequency" — per-film word-count dicts
                    "sequence"  — ordered motif word list per film
                    "rare"      — words appearing in exactly 1 film
        limit:      Cap on returned entries.
        media_type: "movies" (default) or "gameplay".

    Read-only. Reads: data/motifs/, data/metadata/
    """
    result = _ctx()
    if isinstance(result, str):
        return result
    project_path, _ = result

    try:
        from services.analysis import compare_motifs as _compare_motifs
        data = _compare_motifs(
            project_path=project_path,
            media_type=media_type,
            films=films,
            mode=mode,
            limit=limit,
        )
        return _ok(**data)
    except Exception as exc:
        return _err(str(exc), traceback.format_exc())


@mcp.tool()
def get_all_motifs(
    films: list[str] | None = None,
    sort: str = "frequency",
    limit: int | None = None,
    media_type: str = "movies",
) -> str:
    """Return a global motif frequency index across the archive.

    Aggregates motif words across all (or selected) films and returns
    a ranked vocabulary list.

    Args:
        films:      Restrict to these film titles (None → all).
        sort:       "frequency" (most common first, default),
                    "alphabetical", or "rarity" (least common first).
        limit:      Cap on returned vocabulary entries.
        media_type: "movies" (default) or "gameplay".

    Read-only. Reads: data/motifs/, data/metadata/
    """
    result = _ctx()
    if isinstance(result, str):
        return result
    project_path, _ = result

    try:
        from services.analysis import get_all_motifs as _get_all_motifs
        data = _get_all_motifs(
            project_path=project_path,
            media_type=media_type,
            films=films,
            sort=sort,
            limit=limit,
        )
        return _ok(**data)
    except Exception as exc:
        return _err(str(exc), traceback.format_exc())


@mcp.tool()
def search_palette(
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
    films: list[str] | None = None,
    limit: int | None = None,
    media_type: str = "movies",
) -> str:
    """Filter shots by colour-space characteristics from the palette cache.

    All filtering is deterministic (no ML inference). Provide at least one
    filter flag or numeric threshold.

    Colour heuristics (CIE LAB space):
      warm       a* > 5 (reddish) OR b* > 10 (yellowish)
      cold       b* < −10 (bluish) OR (a* < −5 and b* < 5)
      dark       luminance < 0.30
      bright     luminance > 0.70
      low_chroma chroma < 0.15
      high_chroma chroma > 0.30

    Args:
        warm / cold / dark / bright / low_chroma / high_chroma:
                        Colour-space preset flags.
        foreground_only: Apply filters only to foreground region.
        background_only: Apply filters only to background region.
        luminance_min/max: Fine-grained luminance bounds (0–1).
        chroma_min/max:    Fine-grained chroma bounds (0–1).
        films:          Restrict to these film titles (None → all).
        limit:          Cap on returned results.
        media_type:     "movies" (default) or "gameplay".

    Read-only. Reads: data/palettes/, data/shotlists/, data/metadata/
    """
    result = _ctx()
    if isinstance(result, str):
        return result
    project_path, _ = result

    try:
        from services.analysis import search_palette as _search_palette
        data = _search_palette(
            project_path=project_path,
            media_type=media_type,
            films=films,
            warm=warm,
            cold=cold,
            dark=dark,
            bright=bright,
            low_chroma=low_chroma,
            high_chroma=high_chroma,
            foreground_only=foreground_only,
            background_only=background_only,
            luminance_min=luminance_min,
            luminance_max=luminance_max,
            chroma_min=chroma_min,
            chroma_max=chroma_max,
            limit=limit,
        )
        return _ok(**data)
    except ValueError as exc:
        return _err(str(exc))
    except Exception as exc:
        return _err(str(exc), traceback.format_exc())


@mcp.tool()
def search_cooccurrence(
    terms: list[str],
    operator: str = "AND",
    fields: list[str] | None = None,
    films: list[str] | None = None,
    limit: int | None = None,
    media_type: str = "movies",
) -> str:
    """Find shots containing multiple annotation terms simultaneously.

    Runs a search for each term using the existing annotation index, then
    intersects (AND) or unions (OR) the result sets.

    Args:
        terms:      Two or more annotation terms (required).
        operator:   "AND" — shots matching ALL terms (default).
                    "OR"  — shots matching ANY term.
        fields:     Restrict each term search to these annotation fields.
                    None → search all fields.
        films:      Restrict to these film titles (None → all).
        limit:      Cap on returned results.
        media_type: "movies" (default) or "gameplay".

    Read-only. Reads: data/annotations/ (via services/search.py index)
    """
    result = _ctx()
    if isinstance(result, str):
        return result
    project_path, _ = result

    try:
        from services.analysis import search_cooccurrence as _search_cooccurrence
        data = _search_cooccurrence(
            project_path=project_path,
            terms=terms,
            media_type=media_type,
            films=films,
            fields=fields,
            operator=operator,
            limit=limit,
        )
        return _ok(**data)
    except ValueError as exc:
        return _err(str(exc))
    except Exception as exc:
        return _err(str(exc), traceback.format_exc())


@mcp.tool()
def get_shot_context(
    film: str,
    shot_id: str,
    window: int = 3,
    include_subtitles: bool = False,
    include_motif: bool = False,
    include_palette: bool = False,
    media_type: str = "movies",
) -> str:
    """Return neighboring shots around a given shot_id with optional enrichment.

    Useful for understanding narrative context — what comes before and after
    a shot of interest. Optionally enriches each neighbor with subtitle text,
    motif word, and dominant colour.

    Args:
        film:               Film title substring, filename, or TMDb ID.
        shot_id:            Canonical shot identifier, or integer index.
        window:             Shots on each side of the center (default: 3).
        include_subtitles:  Attach overlapping subtitle cues.
        include_motif:      Attach the motif word annotation.
        include_palette:    Attach fg/bg dominant colour.
        media_type:         "movies" (default) or "gameplay".

    Read-only. Reads: data/shotlists/, data/annotations/, data/subtitles/,
                      data/motifs/, data/palettes/
    """
    result = _ctx()
    if isinstance(result, str):
        return result
    project_path, _ = result

    try:
        from services.analysis import get_shot_context as _get_shot_context
        data = _get_shot_context(
            project_path=project_path,
            film=film,
            shot_id=shot_id,
            media_type=media_type,
            window=window,
            include_subtitles=include_subtitles,
            include_motif=include_motif,
            include_palette=include_palette,
        )
        return _ok(**data)
    except ValueError as exc:
        return _err(str(exc))
    except Exception as exc:
        return _err(str(exc), traceback.format_exc())


@mcp.tool()
def align_subtitles_to_shots(
    film: str,
    scene: int | None = None,
    media_type: str = "movies",
) -> str:
    """Align subtitle cues to shotlist entries by time overlap.

    For each shot, finds all subtitle cues whose time range overlaps the
    shot's start_time … end_time. Returns a shot-by-shot list with attached
    dialogue text. Useful for dialogue-driven analysis.

    Args:
        film:       Film title substring, filename, or TMDb ID.
        scene:      Restrict to one scene number. Omit for all scenes.
        media_type: "movies" (default) or "gameplay".

    Read-only. Reads: data/shotlists/, media/subtitles/
    """
    result = _ctx()
    if isinstance(result, str):
        return result
    project_path, _ = result

    try:
        from services.analysis import align_subtitles_to_shots as _align
        data = _align(
            project_path=project_path,
            film=film,
            media_type=media_type,
            scene=scene,
        )
        return _ok(**data)
    except ValueError as exc:
        return _err(str(exc))
    except Exception as exc:
        return _err(str(exc), traceback.format_exc())


@mcp.tool()
def get_archive_stats(
    media_type: str = "movies",
) -> str:
    """Return archive-level coverage statistics.

    Scans data directories to count films, shots, annotations, motifs,
    palettes, subtitles, and silhouette entries. No file contents are fully
    loaded — counts are derived from file presence and header scanning only.

    Args:
        media_type: "movies" (default) or "gameplay".

    Read-only. Reads: data/metadata/, data/shotlists/, data/annotations/,
                      data/motifs/, data/palettes/, media/subtitles/,
                      data/silhouettes/, data/index/
    """
    result = _ctx()
    if isinstance(result, str):
        return result
    project_path, _ = result

    try:
        from services.analysis import get_archive_stats as _get_archive_stats
        data = _get_archive_stats(project_path=project_path, media_type=media_type)
        return _ok(**data)
    except Exception as exc:
        return _err(str(exc), traceback.format_exc())


# ===========================================================================
# Entry point
# ===========================================================================

if __name__ == "__main__":
    mcp.run(transport="stdio")
