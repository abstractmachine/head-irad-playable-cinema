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
def list_movies(media_type: str = "movies") -> str:
    """List all films in the archive with their metadata.

    Returns a summary list of every film: title, year, tmdb id, filename,
    media_id, runtime, director, and annotation/shotlist availability flags.

    Args:
        media_type: "movies" (default) or "gameplay".

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

        return _ok(media_type=media_type, count=len(summary), movies=summary)

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
) -> str:
    """Get the shot list for one film.

    Returns all shots with timecodes, frame numbers, shot_id, and captions.
    Optionally filter to a single scene number.

    Args:
        film:       Title substring, exact filename, or numeric TMDb ID.
        media_type: "movies" (default) or "gameplay".
        scene:      Scene number to filter to (e.g. "3"). Empty = all scenes.

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

        return _ok(
            title=entry.get("title", ""),
            filename=filename,
            media_type=media_type,
            total_shots=len(shots),
            scene_filter=scene or None,
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
) -> str:
    """Get subtitle cues for one film, optionally within a time window.

    Args:
        film:       Title substring, exact filename, or numeric TMDb ID.
        media_type: "movies" (default) or "gameplay".
        start_secs: Window start in seconds (0 = from beginning).
        end_secs:   Window end in seconds (0 = to end of film).

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
        return _ok(
            title=entry.get("title", ""),
            filename=filename,
            subtitle_path=str(srt_path),
            cue_count=len(cue_list),
            window={"start_secs": start_secs, "end_secs": end_secs} if (start_secs or end_secs) else None,
            cues=cue_list,
        )

    except Exception as exc:
        return _err(str(exc), traceback.format_exc())


@mcp.tool()
def list_motifs(
    film: str,
    media_type: str = "movies",
) -> str:
    """Get the motif word sequence for one film.

    Returns the per-shot motif progression plus the film-level semantic title
    if one has been generated.

    Args:
        film:       Title substring, exact filename, or numeric TMDb ID.
        media_type: "movies" (default) or "gameplay".

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
            shots=shots,
        )

    except Exception as exc:
        return _err(str(exc), traceback.format_exc())


@mcp.tool()
def list_palettes(
    film: str,
    media_type: str = "movies",
) -> str:
    """Get the colour palette data for one film's shots.

    Each shot entry contains foreground and background dominant colours in
    RGB, LAB, luminance, and chroma. Returns summary swatches for quick
    inspection plus the full palette if needed.

    Args:
        film:       Title substring, exact filename, or numeric TMDb ID.
        media_type: "movies" (default) or "gameplay".

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
            full=palette_doc,
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
            results, project_path, layout=layout, show_captions=True
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
) -> str:
    """Generate a JSON catalog of films with metadata and data availability.

    Produces a structured JSON file in output/catalogs/ that Claude can
    reference in subsequent work. The catalog lists every film with its
    metadata, shot counts, and optionally its annotation fields and motifs.

    Output: output/catalogs/catalog-<media_type>-<timestamp>.json

    Args:
        films:               Optional list of film titles/filenames to include.
                             Omit or pass null to include all films.
        media_type:          "movies" (default) or "gameplay".
        include_annotations: Include per-shot annotation summaries (large output).
        include_motifs:      Include motif word sequences for each film.

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
            catalog=catalog,
        )

    except Exception as exc:
        return _err(str(exc), traceback.format_exc())


# ===========================================================================
# Entry point
# ===========================================================================

if __name__ == "__main__":
    mcp.run(transport="stdio")
