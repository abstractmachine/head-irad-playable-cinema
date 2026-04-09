import sys
import os
import argparse
import json
from pathlib import Path

# Ensure the project root is importable so generators/ and visualizers/ are found
# regardless of the cwd when the installed `crossing` entry-point is invoked.
_PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

import prefs

_MEDIA_FOLDER = {"movie": "movies", "gameplay": "gameplay"}
_TOOL_VERSION = "2.0.0"  # Updated for new folder structure (videos/thumbnails/subtitles with media_type subdirs)


def _pick_files_or_folder():
    """Open a GUI file/folder picker dialog. Returns list of selected paths."""
    try:
        from tkinter import Tk, filedialog
        
        root = Tk()
        root.withdraw()  # Hide the main window
        root.attributes('-topmost', True)  # Bring dialog to front
        
        # Ask user what they want to pick
        print("\n📁 Opening file picker...")
        print("   Select: files (Ctrl+Click for multiple) or a folder")
        
        # Try to pick files first (with multiple selection)
        files = filedialog.askopenfilenames(
            title="Select file(s) to import (or Cancel to pick folder)",
            filetypes=[("Video files", "*.mp4 *.mkv *.avi *.mov"), ("All files", "*.*")]
        )
        
        root.destroy()
        
        if files:
            return list(files)
        
        # If no files selected, try folder picker
        root = Tk()
        root.withdraw()
        root.attributes('-topmost', True)
        
        folder = filedialog.askdirectory(
            title="Select folder to import"
        )
        
        root.destroy()
        
        if folder:
            return [folder]
        
        return []
        
    except ImportError:
        print("✗ Error: tkinter not available. Install python3-tk package.", file=sys.stderr)
        return []
    except Exception as e:
        print(f"✗ Error: Failed to open file picker: {e}", file=sys.stderr)
        return []


def _get_data_version(project_path: str) -> str | None:
    """Get the data structure version from project preferences."""
    version_file = Path(project_path) / "preferences" / "version.txt"
    if version_file.exists():
        return version_file.read_text().strip()
    return None


def _set_data_version(project_path: str, version: str) -> None:
    """Set the data structure version in project preferences."""
    version_file = Path(project_path) / "preferences" / "version.txt"
    version_file.parent.mkdir(parents=True, exist_ok=True)
    version_file.write_text(version + "\n")


def cmd_version(args):
    """Show tool and data structure versions."""
    print(f"Tool version: {_TOOL_VERSION}")
    
    project_path = prefs.get("path")
    if project_path:
        data_version = _get_data_version(project_path)
        if data_version:
            print(f"Data version: {data_version}")
            if data_version != _TOOL_VERSION:
                print(f"\n⚠ Warning: Data version ({data_version}) differs from tool version ({_TOOL_VERSION})")
                print("  Consider migrating your data structure to the latest version.")
        else:
            print(f"Data version: (not set)")
            print(f"\n💡 Run 'crossing tool version --init' to initialize data version.")
    else:
        print("\nNo project path set. Use 'crossing tool path <folder>' to set one.")


def cmd_version_init(args):
    """Initialize or update the data version for the current project."""
    _require_path()
    project_path = prefs.get("path")
    _set_data_version(project_path, _TOOL_VERSION)
    print(f"Data version set to {_TOOL_VERSION}")


_MODEL_KEYS = {
    "annotate": "model_annotate",
    "segmentation": "model_segmentation",
    "yolo": "model_yolo",
}
_MODEL_DEFAULTS = {
    "annotate": "gemma4-e4b",
    "segmentation": "sam2.1_b.pt",
    "yolo": "yolov8n.pt",
}

# Persistent defaults for annotate (and other commands)
# key = prefs key, value = built-in fallback
_ANNOTATE_DEFAULT_KEYS = {
    "frames-per-shot": ("annotate_frames_per_shot", 3),
}


def cmd_model(args):
    sub = args.model_subcommand
    if sub == "set":
        key = _MODEL_KEYS[args.role]
        prefs.set(key, args.name)
        print(f"✓ Model for '{args.role}' set to '{args.name}'")
    elif sub == "get" or sub is None:
        role = getattr(args, "role", None)
        if role:
            roles = [role]
        else:
            roles = list(_MODEL_KEYS)
        for r in roles:
            val = prefs.get(_MODEL_KEYS[r], _MODEL_DEFAULTS[r])
            print(f"{r}: {val}")
    elif sub == "list":
        _require_path()
        from services.models import list_models
        list_models(prefs.get("path"))
    elif sub == "download":
        _require_path()
        from services.models import download_model
        try:
            download_model(
                prefs.get("path"),
                args.repo,
                local_name=getattr(args, "name", None) or None,
                ignore_non_safetensors=not getattr(args, "all_formats", False),
            )
        except Exception as e:
            print(f"✗ Download failed: {e}", file=sys.stderr)
            sys.exit(1)
    elif sub == "size":
        _require_path()
        from services.models import model_size_report
        model_size_report(prefs.get("path"), args.model)
    elif sub == "remove":
        _require_path()
        from services.models import remove_model
        try:
            remove_model(prefs.get("path"), args.name, confirm=getattr(args, "confirm", False))
        except FileNotFoundError as e:
            print(f"\u2717 {e}", file=sys.stderr)
            sys.exit(1)


def cmd_path(args):
    if args.folder is None:
        path = prefs.get("path")
        if path:
            print(f"Project path: {path}")
            data_version = _get_data_version(path)
            if data_version:
                print(f"Data version:  {data_version}")
                if data_version != _TOOL_VERSION:
                    print(f"\n⚠ Warning: Data version mismatch (data: {data_version}, tool: {_TOOL_VERSION})")
            else:
                print(f"Data version:  (not set)")
        else:
            print("(not set)")
        return
    from pathlib import Path
    p = Path(args.folder).resolve()
    prefs.set("path", str(p))
    
    # Auto-initialize version for new projects
    if not _get_data_version(str(p)):
        _set_data_version(str(p), _TOOL_VERSION)
        print(f"Project path set to: {p}")
        print(f"Data version initialized to: {_TOOL_VERSION}")
    else:
        print(f"Project path set to: {p}")


def cmd_name(args):
    if args.project_name is None:
        print(prefs.get("name") or "(not set)")
        return
    _require_path()
    prefs.set("name", args.project_name)
    print(f"Project name set to: {args.project_name!r}")


def cmd_media(args):
    sub = args.media_subcommand
    if sub == "import":
        cmd_import(args)
    elif sub == "remove":
        cmd_remove(args)
    elif sub == "subtitle":
        cmd_subtitle(args)


def cmd_import(args):
    _require_path()
    from services.import_media import import_files
    from services.metadata import fetch_metadata, fetch_thumbnail, fetch_subtitle, set_metadata
    
    project_path = prefs.get("path")
    media_type = _MEDIA_FOLDER[args.media]
    
    # Get sources from picker or arguments
    if args.pick:
        sources = _pick_files_or_folder()
        if not sources:
            print("No files or folder selected.")
            return
    else:
        if not args.sources:
            args._parser.error("the following arguments are required: source (or use --pick)")
        sources = args.sources
    
    # Import files
    imported_files = import_files(sources, project_path, dest=media_type, platform=args.platform)
    
    # Auto-update metadata for imported files
    if imported_files and not args.skip_metadata:
        print(f"\nUpdating metadata for {len(imported_files)} imported file{'s' if len(imported_files) > 1 else ''}...")
        for filename in imported_files:
            try:
                candidate = fetch_metadata(filename, project_path)
                candidate["media_type"] = media_type
                set_metadata(project_path, candidate, match_filename=filename)
                # Download thumbnail and subtitle
                if candidate.get("tmdb"):
                    fetch_thumbnail(filename, project_path, media_type, candidate["tmdb"])
                if candidate.get("imdb"):
                    fetch_subtitle(
                        filename,
                        project_path,
                        media_type,
                        candidate["imdb"],
                        candidate.get("title", ""),
                        candidate.get("year")
                    )
                print(f"  ✓  {filename}")
            except (RuntimeError, LookupError) as exc:
                print(f"  ✗  {filename}: {exc}")


def cmd_search(args):
    _require_path()

    # Dispatch: `crossing search vocabulary <field> [scope...]`
    if args.query == "vocabulary":
        from services.search import vocabulary_from_field
        remaining = args.scope or []
        if not remaining:
            print("error: 'vocabulary' requires a field name, e.g. crossing search vocabulary objects", file=sys.stderr)
            sys.exit(1)
        field = remaining[0]
        scopes = (remaining[1:] or []) + (getattr(args, "movie", None) or [])
        scopes = scopes or None
        use_all = getattr(args, "all", False)
        show_count = getattr(args, "show_count", False)
        sort = getattr(args, "sort", "alphabetical")
        media_type = getattr(args, "media", "movies")
        result = vocabulary_from_field(
            field=field,
            scopes=scopes,
            use_all=use_all,
            show_count=show_count,
            project_path=prefs.get("path"),
            media_type=media_type,
            sort=sort,
        )
        print(json.dumps(result, indent=2))
        return

    from services.search import search_shots

    scopes = (args.scope or []) + (getattr(args, "movie", None) or [])
    scopes = scopes or None
    use_all = getattr(args, "all", False)
    field = getattr(args, "field", None)
    limit = getattr(args, "limit", None)
    limit_per_item = getattr(args, "limit_per_item", None)
    media_type = getattr(args, "media", "movies")

    result = search_shots(
        query=args.query,
        scopes=scopes,
        field=field,
        limit=limit,
        limit_per_item=limit_per_item,
        use_all=use_all,
        project_path=prefs.get("path"),
        media_type=media_type,
    )
    print(json.dumps(result, indent=2))


# ---------------------------------------------------------------------------
# metadata subcommand
# ---------------------------------------------------------------------------

def cmd_metadata(args):
    _require_path()
    sub = args.metadata_subcommand
    if sub == "get":
        _meta_get(args)
    elif sub == "set":
        _meta_set(args)
    elif sub == "validate":
        _meta_validate(args)
    elif sub == "update":
        _meta_update(args)
    elif sub == "fixname":
        _meta_fixname(args)
    elif sub == "count":
        _meta_count(args)
    elif sub == "list":
        _meta_list(args)
    elif sub == "prune":
        _meta_prune(args)


def _meta_get(args):
    from services.metadata import get_metadata
    tmdb = getattr(args, "tmdb", None)
    query = getattr(args, "query", None)
    project_path = prefs.get("path")
    media_type = args.media

    if tmdb is not None:
        all_rows = get_metadata(project_path, media_type=media_type)
        result = [r for r in all_rows if r.get("tmdb") == str(tmdb)]
        if not result:
            print(f"✗ No entry found with TMDb ID: {tmdb}", file=sys.stderr)
            sys.exit(1)
    else:
        result = get_metadata(project_path, query, media_type=media_type)

    print(json.dumps(result, indent=2))


def _meta_set(args):
    from services.metadata import set_metadata, validate_metadata
    try:
        data = json.loads(args.json_data)
    except json.JSONDecodeError as exc:
        print(f"✗ Error: invalid JSON — {exc}", file=sys.stderr)
        sys.exit(1)
    ok, errors = validate_metadata(data)
    if not ok:
        print("✗ Error: Validation failed:", file=sys.stderr)
        for e in errors:
            print(f"  {e}", file=sys.stderr)
        sys.exit(1)
    dest = set_metadata(prefs.get("path"), data)
    print(f"Saved: {dest}")


def _meta_validate(args):
    from pathlib import Path
    from services.metadata import get_metadata
    from services.normalize import normalize_filename

    project_path = prefs.get("path")
    media_type = args.media
    media_dir = Path(project_path) / "media" / "videos" / media_type

    rows = get_metadata(project_path, media_type=media_type)
    if not rows:
        print("No metadata entries found.")
        return

    missing = []
    for row in rows:
        filename = row.get("filename", "")
        if not filename:
            continue
        if not (media_dir / filename).exists():
            missing.append(filename)

    if missing:
        print(f"{len(missing)} file(s) missing from {media_dir}:")
        for f in missing:
            print(f"  missing  {f}")
        sys.exit(1)
    else:
        print(f"All {len(rows)} file(s) present.")

    # Check for thumbnails if requested
    if args.check_thumbnails:
        thumbnail_dir = Path(project_path) / "media" / "thumbnails" / media_type
        missing_thumbnails = []
        
        for row in rows:
            filename = row.get("filename", "")
            if not filename:
                continue
            
            # Expected thumbnail name
            thumbnail_name = Path(filename).stem + ".jpg"
            thumbnail_path = thumbnail_dir / thumbnail_name
            
            # Also check old dash-separated format
            old_thumbnail_name = thumbnail_name.replace(" ", "-")
            old_thumbnail_path = thumbnail_dir / old_thumbnail_name
            
            if not thumbnail_path.exists() and not old_thumbnail_path.exists():
                missing_thumbnails.append(filename)
        
        if missing_thumbnails:
            print(f"\n{len(missing_thumbnails)} thumbnail(s) missing:")
            for f in missing_thumbnails:
                print(f"  no thumbnail  {f}")
        else:
            print(f"\nAll {len(rows)} thumbnail(s) present.")

    # Check for subtitles if requested
    if args.check_subtitles:
        subtitle_dir = Path(project_path) / "media" / "subtitles" / media_type
        missing_subtitles = []
        
        for row in rows:
            filename = row.get("filename", "")
            if not filename:
                continue
            
            # Expected subtitle name
            subtitle_name = Path(filename).stem + ".srt"
            subtitle_path = subtitle_dir / subtitle_name
            
            # Also check old dash-separated format
            old_subtitle_name = subtitle_name.replace(" ", "-")
            old_subtitle_path = subtitle_dir / old_subtitle_name
            
            if not subtitle_path.exists() and not old_subtitle_path.exists():
                missing_subtitles.append(filename)
        
        if missing_subtitles:
            print(f"\n{len(missing_subtitles)} subtitle(s) missing:")
            for f in missing_subtitles:
                print(f"  no subtitle  {f}")
        else:
            print(f"\nAll {len(rows)} subtitle(s) present.")


def _meta_update(args):
    from services.metadata import fetch_metadata, fetch_thumbnail, fetch_subtitle, set_metadata, get_metadata
    project_path = prefs.get("path")
    media_type = getattr(args, "media", "movies")
    force = getattr(args, "force", False)
    single_file = getattr(args, "file", None)

    # Single file update
    if single_file:
        try:
            candidate = fetch_metadata(single_file, project_path)
            candidate["media_type"] = media_type
            set_metadata(project_path, candidate, match_filename=single_file)
            # Download thumbnail and subtitle
            if candidate.get("tmdb"):
                thumbnail_path = fetch_thumbnail(single_file, project_path, media_type, candidate["tmdb"])
                if thumbnail_path:
                    print(f"Thumbnail: {thumbnail_path}")
            if candidate.get("imdb"):
                subtitle_path = fetch_subtitle(
                    single_file,
                    project_path,
                    media_type,
                    candidate["imdb"],
                    candidate.get("title", ""),
                    candidate.get("year")
                )
                if subtitle_path:
                    print(f"Subtitle: {subtitle_path}")
            print(f"  ok  {single_file}")
        except RuntimeError as exc:
            print(f"✗ Error: {exc}", file=sys.stderr)
            sys.exit(1)
        except LookupError as exc:
            print(f"✗ Error: {exc}", file=sys.stderr)
            sys.exit(1)
        return

    # Bulk update
    REQUIRED = {"title", "year", "director", "overview"}
    rows = get_metadata(project_path, media_type=media_type)
    known = {r["filename"] for r in rows if r.get("filename")}
    
    if force:
        # Force update all existing entries
        missing = [r for r in rows if r.get("filename")]
    else:
        incomplete = [r for r in rows if any(not r.get(f) for f in REQUIRED)]
        # files on disk not in CSV at all
        media_dir = Path(project_path) / "media" / "videos" / media_type
        unregistered = []
        if media_dir.is_dir():
            for f in sorted(media_dir.iterdir()):
                if f.is_file() and f.name not in known:
                    unregistered.append({"filename": f.name})
        missing = incomplete + unregistered
    
    if not missing:
        print("All entries already have metadata.")
        return
    print(f"Fetching metadata for {len(missing)} entr{'y' if len(missing) == 1 else 'ies'}...")
    ok_count = 0
    for row in missing:
        filename = row.get("filename", "")
        if not filename:
            continue
        try:
            candidate = fetch_metadata(filename, project_path)
            candidate["media_type"] = media_type
            set_metadata(project_path, candidate, match_filename=filename)
            # Download thumbnail and subtitle
            if candidate.get("tmdb"):
                fetch_thumbnail(filename, project_path, media_type, candidate["tmdb"])
            if candidate.get("imdb"):
                fetch_subtitle(
                    filename, 
                    project_path, 
                    media_type,
                    candidate["imdb"],
                    candidate.get("title", ""),
                    candidate.get("year")
                )
            print(f"  ok  {filename}")
            ok_count += 1
        except (RuntimeError, LookupError) as exc:
            print(f"  skip  {filename}: {exc}")
    print(f"Done. {ok_count}/{len(missing)} updated.")


def _meta_count(args):
    from services.metadata import get_metadata
    project_path = prefs.get("path")
    media_type = getattr(args, "media", "movies")
    rows = get_metadata(project_path, media_type=media_type)
    print(f"{len(rows)} {media_type}")


def _meta_list(args):
    from services.metadata import get_metadata
    project_path = prefs.get("path")
    media_type = getattr(args, "media", "movies")

    rows = get_metadata(project_path, media_type=media_type)

    if args.year:
        rows = [r for r in rows if str(r.get("year", "")) == args.year]
    if args.director:
        needle = args.director.lower()
        rows = [r for r in rows if needle in str(r.get("director", "")).lower()]

    # Add subtitle status
    subtitle_dir = Path(project_path) / "media" / "subtitles" / media_type
    if True:
        for row in rows:
            filename = row.get("filename", "")
            if filename:
                subtitle_name = Path(filename).stem + ".srt"
                subtitle_path = subtitle_dir / subtitle_name
                old_subtitle_name = subtitle_name.replace(" ", "-")
                old_subtitle_path = subtitle_dir / old_subtitle_name
                row["has_subtitle"] = subtitle_path.exists() or old_subtitle_path.exists()
            else:
                row["has_subtitle"] = False

    if args.fields:
        fields = [f.strip() for f in args.fields.split(",")]
        rows = [{f: r.get(f, "") for f in fields} for r in rows]

    if args.sort:
        rows.sort(key=lambda r: str(r.get(args.sort, "")).lower(), reverse=args.reverse)

    print(json.dumps(rows, indent=2))
    print(f"\n{len(rows)} result(s)")


def _meta_prune(args):
    from services.metadata import get_metadata, prune_metadata
    project_path = prefs.get("path")
    media_type = getattr(args, "media", "movies")
    media_dir = Path(project_path) / "media" / "videos" / media_type

    rows = get_metadata(project_path, media_type=media_type)
    orphans = [r for r in rows if not r.get("filename") or not (media_dir / r["filename"]).exists()]

    if not orphans:
        print("Nothing to prune — all entries have files on disk.")
        return

    print(f"{len(orphans)} orphan entr{'y' if len(orphans) == 1 else 'ies'} (no matching file on disk):")
    for r in orphans:
        print(f"  {r.get('filename', '(no filename)')}  —  {r.get('title', '')}")

    if not args.confirm:
        print(f"\nDry run. Pass --confirm to remove {'this entry' if len(orphans) == 1 else f'these {len(orphans)} entries'}.")
        return

    pruned = prune_metadata(project_path, media_type=media_type)
    print(f"\nRemoved {len(pruned)} entr{'y' if len(pruned) == 1 else 'ies'}.")


# ---------------------------------------------------------------------------
# remove command
# ---------------------------------------------------------------------------

def cmd_remove(args):
    _require_path()
    from services.metadata import get_metadata, prune_metadata
    from services.shotlist import resolve_filename

    project_path = prefs.get("path")
    media_type = args.media
    tmdb = getattr(args, "tmdb", None)
    query = " ".join(args.query).strip() if args.query else ""

    if not tmdb and not query:
        print("✗ Provide a title query or --tmdb <id>.", file=sys.stderr)
        sys.exit(1)

    rows = get_metadata(project_path, media_type=media_type)

    if tmdb is not None:
        try:
            filename = resolve_filename(project_path, tmdb, None, media_type)
        except ValueError as exc:
            print(f"✗ {exc}", file=sys.stderr)
            sys.exit(1)
        matches = [r for r in rows if r.get("filename") == filename]
    else:
        q = query.lower()
        matches = [
            r for r in rows
            if q in str(r.get("filename", "")).lower()
            or q in str(r.get("title", "")).lower()
        ]

    if not matches:
        hint = f"--tmdb {tmdb}" if tmdb else f"'{query}'"
        print(f"✗ No metadata entry matches {hint}.", file=sys.stderr)
        sys.exit(1)

    if len(matches) > 1:
        print(f"✗ '{query}' matches {len(matches)} entries — be more specific or use --tmdb:", file=sys.stderr)
        for r in matches:
            print(f"  [{r.get('tmdb', '?')}]  {r.get('filename', '')}  —  {r.get('title', '')} ({r.get('year', '')})", file=sys.stderr)
        sys.exit(1)

    row = matches[0]
    filename = row.get("filename", "")
    stem = Path(filename).stem

    video_path     = Path(project_path) / "media" / "videos"     / media_type / filename
    thumbnail_path = Path(project_path) / "media" / "thumbnails" / media_type / (stem + ".jpg")
    subtitle_path  = Path(project_path) / "media" / "subtitles"  / media_type / (stem + ".srt")
    shotlist_path  = Path(project_path) / "data"  / "shotlists"  / media_type / (stem + ".csv")
    npy_path       = Path(project_path) / "data"  / "shotlists"  / media_type / (stem + ".npy")

    candidates = [
        ("video",     video_path),
        ("thumbnail", thumbnail_path),
        ("subtitle",  subtitle_path),
        ("shotlist",  shotlist_path),
        ("embeddings",npy_path),
    ]
    present = [(label, p) for label, p in candidates if p.exists()]

    print(f"Will remove: {row.get('title', filename)} ({row.get('year', '?')})")
    print(f"  metadata row in {media_type}.csv")
    for label, p in present:
        print(f"  {label}: {p.relative_to(project_path)}")
    absent = [(label, p) for label, p in candidates if not p.exists()]
    for label, _ in absent:
        print(f"  {label}: (not present)")

    if not args.confirm:
        print(f"\nDry run. Pass --confirm to delete.")
        return

    # Delete files
    for _, p in present:
        p.unlink()

    # Remove metadata row by rewriting the CSV without this filename
    import csv as _csv
    from services.metadata import _csv_path
    dest = _csv_path(project_path, media_type)
    if dest.exists():
        with dest.open(newline="", encoding="utf-8") as f:
            reader = _csv.DictReader(f)
            fieldnames = list(reader.fieldnames or [])
            kept = [r for r in reader if r.get("filename") != filename]
        with dest.open("w", newline="", encoding="utf-8") as f:
            writer = _csv.DictWriter(f, fieldnames=fieldnames, restval="")
            writer.writeheader()
            writer.writerows(kept)

    removed_files = len(present)
    print(f"\nRemoved: metadata row + {removed_files} file(s).")


def _meta_fixname(args):
    from pathlib import Path
    from services.normalize import normalize_filename
    from services.metadata import get_metadata, set_metadata

    project_path = prefs.get("path")
    media_type = args.media
    media_dir = Path(project_path) / "media" / "videos" / media_type

    # Fix media files and CSV
    rows = get_metadata(project_path, media_type=media_type)
    if not rows:
        print("No metadata entries found.")
        return

    renamed = 0
    for row in rows:
        old_name = row.get("filename", "")
        if not old_name:
            continue
        normalized = normalize_filename(old_name)
        if normalized == old_name:
            continue

        # Rename physical file if it exists
        old_path = media_dir / old_name
        new_path = media_dir / normalized
        if old_path.exists():
            if new_path.exists():
                print(f"  warn  {old_name} → {normalized}  (skipped, target exists)")
                continue
            old_path.rename(new_path)

        # Update CSV — pass old name so set_metadata can find and replace the row
        set_metadata(project_path, {**row, "filename": normalized, "media_type": media_type}, match_filename=old_name)
        print(f"  {old_name} → {normalized}")
        renamed += 1

    # Fix thumbnail files
    art_dir = Path(project_path) / "media" / "thumbnails" / media_type
    
    art_renamed = 0
    if art_dir.is_dir():
        for old_path in sorted(art_dir.iterdir()):
            if not old_path.is_file():
                continue
            old_name = old_path.name
            normalized = normalize_filename(old_name)
            if normalized == old_name:
                continue
            
            new_path = art_dir / normalized
            if new_path.exists():
                print(f"  warn  thumbnails/{old_name} → {normalized}  (skipped, target exists)")
                continue
            
            old_path.rename(new_path)
            print(f"  thumbnails/{old_name} → {normalized}")
            art_renamed += 1
        
        if art_renamed > 0:
            print(f"Renamed {art_renamed} thumbnails.")

    # Fix subtitle files
    subtitle_renamed = 0
    if True:
        subtitle_dir = Path(project_path) / "media" / "subtitles" / media_type
        if subtitle_dir.is_dir():
            for old_path in sorted(subtitle_dir.iterdir()):
                if not old_path.is_file() or old_path.suffix != ".srt":
                    continue
                old_name = old_path.name
                normalized = normalize_filename(old_name)
                if normalized == old_name:
                    continue
                
                new_path = subtitle_dir / normalized
                if new_path.exists():
                    print(f"  warn  subtitles/{old_name} → {normalized}  (skipped, target exists)")
                    continue
                
                old_path.rename(new_path)
                print(f"  subtitles/{old_name} → {normalized}")
                subtitle_renamed += 1
            
            if subtitle_renamed > 0:
                print(f"Renamed {subtitle_renamed} subtitle(s).")

    if renamed == 0 and art_renamed == 0 and subtitle_renamed == 0:
        print("Nothing to rename.")


# ---------------------------------------------------------------------------
# shotlist command
# ---------------------------------------------------------------------------

def cmd_shotlist(args):
    _require_path()
    sub = args.shotlist_subcommand
    if sub == "list":
        _shotlist_list(args)
    elif sub == "get":
        _shotlist_get(args)
    elif sub == "show":
        _shotlist_show(args)
    elif sub == "shot":
        sub2 = args.shot_subcommand
        if sub2 == "detect":
            _shot_detect(args)
    elif sub == "visualizer":
        _shot_visualizer(args)
    elif sub == "migrate":
        _shotlist_migrate(args)


def _shotlist_migrate(args):
    """Rewrite all shotlist CSVs that still use legacy temporal field names."""
    from services.shotlist import migrate_shotlist_fields
    project_path = prefs.get("path")
    media_type = getattr(args, "media", None)
    dry_run = getattr(args, "dry_run", False)

    results = migrate_shotlist_fields(project_path, media_type=media_type, dry_run=dry_run)

    if not results:
        print("No shotlist CSVs found.")
        return

    migrated = [r for r in results if r["status"] in ("migrated", "would_migrate")]
    current  = [r for r in results if r["status"] == "already_current"]

    if dry_run:
        print("Dry run — no files written.")

    for r in migrated:
        label = "Would migrate" if dry_run else "Migrated"
        old = ", ".join(r["old_headers"]) if r["old_headers"] else "none"
        dropped = ", ".join(r.get("dropped_columns", []))
        print(f"  {label}: {r['path']}")
        if r["old_headers"]:
            print(f"    Renamed:  {old}")
        if dropped:
            print(f"    Dropped:  {dropped}")
        print(f"    Shots: {r['shot_count']}")

    for r in current:
        print(f"  Already current: {r['path']}")

    print()
    action = "to migrate" if dry_run else "migrated"
    print(f"Total: {len(migrated)} {action}, {len(current)} already current.")


def _shotlist_list(args):
    from services.shotlist import list_shotlists
    project_path = prefs.get("path")
    media_type = getattr(args, "media", None)
    
    shotlists = list_shotlists(project_path, media_type)
    
    if args.json:
        print(json.dumps(shotlists, indent=2))
    else:
        if not shotlists:
            print("No shotlists found.")
            return
        
        print(f"Found {len(shotlists)} shotlist(s):\n")
        for sl in shotlists:
            enc_marker = "📊" if sl['has_encodings'] else "  "
            print(f"  {enc_marker} {sl['title']} ({sl['year']})  [tmdb: {sl['tmdb']}]")
            print(f"     {sl['shot_count']} shots, {sl['scene_count']} scenes")
            print()


def _shotlist_get(args):
    from services.shotlist import read_shotlist, resolve_filename
    project_path = prefs.get("path")
    
    try:
        filename = resolve_filename(project_path, args.tmdb, args.filename, args.media)
        shots = read_shotlist(project_path, filename, args.media)
        
        if args.scene is not None:
            shots = [s for s in shots if s.get('Scene') == str(args.scene)]
            if not shots:
                print(f"No shots found in scene {args.scene}")
                return
        
        print(json.dumps(shots, indent=2))
    except ValueError as e:
        print(f"✗ Error: {e}", file=sys.stderr)
        sys.exit(1)
    except FileNotFoundError as e:
        print(f"✗ Error: {e}", file=sys.stderr)
        sys.exit(1)
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        print(f"✗ Error: Invalid data format - {e}", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"✗ Error: {e}", file=sys.stderr)
        sys.exit(1)


def _shotlist_annotate(args):
    from services.shotlist import annotate_shot, annotate_scene, resolve_filename
    project_path = prefs.get("path")

    try:
        # Determine manual vs automatic mode. Manual mode is triggered by
        # presence of `--manual` text or a positional caption.
        manual_text = getattr(args, "manual", None) or getattr(args, "caption", None)

        if args.annotate_type == "shot":
            # Manual mode: update a specific shot with provided caption text
            if manual_text is not None:
                if args.index is None:
                    raise ValueError("Provide shot index when annotating manually")
                filename = resolve_filename(project_path, args.tmdb, args.filename, args.media)
                annotate_shot(project_path, filename, args.index, manual_text, args.media)
                print(f"✓ Annotated shot {args.index}")
                if getattr(args, "notify", False):
                    try:
                        from services.notify import discord_notify
                        discord_notify(f"✓ Manual annotation: {filename} shot {args.index}", project_path)
                    except Exception:
                        pass
                return

            # Automatic (default) mode
            from generators.annotate import annotate_file_shots, annotate_all_files

            if getattr(args, "all", False):
                results = annotate_all_files(
                    project_path,
                    media_type=args.media,
                    model_name=args.model,
                    prompt_file=args.prompt_file,
                    prompt_text=args.prompt_text,
                    user_prompt_file=getattr(args, "user_prompt_file", None),
                    frames_per_shot=args.frames_per_shot,
                    sample_mode=args.sample_mode,
                    force=args.force,
                    skip_existing=args.skip_existing,
                    limit=getattr(args, "limit", None),
                    verbose=getattr(args, "verbose", False),
                    write_log=getattr(args, "log", False),
                    reload_every_n_shots=getattr(args, "reload_every_n_shots", 25),
                )
                for r in results:
                    failed_count = len(r.get("failed", [])) if r.get("failed") else 0
                    print(f"  {r.get('filename')}: updated={r.get('updated')} skipped={r.get('skipped')} failed={failed_count}")
                if getattr(args, "notify", False):
                    try:
                        from services.notify import discord_notify
                        lines = [f"Annotation batch complete: {len(results)} file(s)"]
                        for r in results:
                            failed_count = len(r.get("failed", [])) if r.get("failed") else 0
                            lines.append(f"{r.get('filename')}: updated={r.get('updated')} skipped={r.get('skipped')} failed={failed_count}")
                        discord_notify("\n".join(lines), project_path)
                    except Exception:
                        pass
                return

            filename = resolve_filename(project_path, args.tmdb, args.filename, args.media)
            summary = annotate_file_shots(
                project_path,
                filename,
                media_type=args.media,
                model_name=args.model,
                prompt_file=args.prompt_file,
                prompt_text=args.prompt_text,
                user_prompt_file=getattr(args, "user_prompt_file", None),
                frames_per_shot=args.frames_per_shot,
                sample_mode=args.sample_mode,
                force=args.force,
                skip_existing=args.skip_existing,
                export_csv=getattr(args, "export_csv", None),
                export_md=getattr(args, "export_md", None),
                shot_index=getattr(args, "index", None),
                limit=getattr(args, "limit", None),
                verbose=getattr(args, "verbose", False),
                write_log=getattr(args, "log", False),
                reload_every_n_shots=getattr(args, "reload_every_n_shots", 25),
            )
            failed_count = len(summary.get("failed", [])) if summary.get("failed") else 0
            print(f"✓ Annotated: {filename} — updated={summary['updated']} skipped={summary['skipped']} failed={failed_count}")
            if getattr(args, "notify", False):
                try:
                    from services.notify import discord_notify
                    discord_notify(f"✓ Annotated: {filename} — updated={summary['updated']} skipped={summary['skipped']} failed={failed_count}", project_path)
                except Exception:
                    pass
            return

        elif args.annotate_type == "scene":
            # Manual scene annotation
            if manual_text is not None:
                filename = resolve_filename(project_path, args.tmdb, args.filename, args.media)
                annotate_scene(project_path, filename, args.scene_number, manual_text, args.media)
                print(f"✓ Annotated scene {args.scene_number}")
                if getattr(args, "notify", False):
                    try:
                        from services.notify import discord_notify
                        discord_notify(f"✓ Manual scene annotation: {filename} scene {args.scene_number}", project_path)
                    except Exception:
                        pass
                return

            # Automatic scene annotation (default)
            from generators.annotate import annotate_file_shots

            filename = resolve_filename(project_path, args.tmdb, args.filename, args.media)
            summary = annotate_file_shots(
                project_path,
                filename,
                media_type=args.media,
                model_name=args.model,
                prompt_file=args.prompt_file,
                prompt_text=args.prompt_text,
                user_prompt_file=getattr(args, "user_prompt_file", None),
                frames_per_shot=args.frames_per_shot,
                sample_mode=args.sample_mode,
                force=args.force,
                skip_existing=args.skip_existing,
                scene_number=args.scene_number,
                limit=getattr(args, "limit", None),
                export_csv=getattr(args, "export_csv", None),
                export_md=getattr(args, "export_md", None),
                verbose=getattr(args, "verbose", False),
                write_log=getattr(args, "log", False),
                reload_every_n_shots=getattr(args, "reload_every_n_shots", 25),
            )
            failed_count = len(summary.get("failed", [])) if summary.get("failed") else 0
            print(f"✓ Annotated scene {args.scene_number} in {filename} — updated={summary['updated']} skipped={summary['skipped']} failed={failed_count}")
            if getattr(args, "notify", False):
                try:
                    from services.notify import discord_notify
                    discord_notify(f"✓ Annotated scene {args.scene_number} in {filename} — updated={summary['updated']} skipped={summary['skipped']} failed={failed_count}", project_path)
                except Exception:
                    pass
            return

    except ValueError as e:
        print(f"✗ Error: {e}", file=sys.stderr)
        sys.exit(1)
    except IndexError as e:
        print(f"✗ Error: {e}", file=sys.stderr)
        sys.exit(1)
    except FileNotFoundError as e:
        print(f"✗ Error: {e}", file=sys.stderr)
        sys.exit(1)
    except (PermissionError, UnicodeDecodeError) as e:
        print(f"✗ Error: {e}", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"✗ Error: {e}", file=sys.stderr)
        sys.exit(1)


def _extract_shot_fields(shot: dict, fields: list) -> dict:
    """Extract specific fields from shot caption JSON and return as dict."""
    result = {
        "start_time": shot.get("start_time", "N/A"),
        "end_time": shot.get("end_time", "N/A"),
        "Scene": shot.get("Scene", "N/A")
    }
    
    # Try parsing Shot_Caption first, then Scene_Caption
    caption_data = {}
    for caption_key in ["Shot_Caption", "Scene_Caption"]:
        caption = shot.get(caption_key, "")
        if caption and caption.strip():
            try:
                caption_data = json.loads(caption)
                break
            except json.JSONDecodeError:
                continue
    
    if not caption_data:
        result["error"] = "No parseable caption data"
        return result
    
    # Normalize field names to handle case-insensitive matching
    field_map = {k.lower(): k for k in caption_data.keys()}
    
    # Extract requested fields
    for field in fields:
        field_lower = field.lower()
        actual_key = field_map.get(field_lower)
        
        if actual_key:
            result[actual_key] = caption_data[actual_key]
        else:
            result[field] = None
    
    return result


def _display_shot_fields(shot: dict, fields: list, indent: int = 0):
    """Display specific fields from shot caption JSON in table format."""
    indent_str = " " * indent
    
    # Try parsing Shot_Caption first, then Scene_Caption
    caption_data = {}
    for caption_key in ["Shot_Caption", "Scene_Caption"]:
        caption = shot.get(caption_key, "")
        if caption and caption.strip():
            try:
                caption_data = json.loads(caption)
                break
            except json.JSONDecodeError:
                continue
    
    if not caption_data:
        print(f"{indent_str}No parseable caption data")
        return
    
    # Normalize field names to handle case-insensitive matching
    field_map = {k.lower(): k for k in caption_data.keys()}
    
    # Display basic shot info
    print(f"{indent_str}Start: {shot.get('start_time', 'N/A')} → End: {shot.get('end_time', 'N/A')}")
    print(f"{indent_str}{'-' * 60}")
    
    # Display requested fields in table format
    for field in fields:
        field_lower = field.lower()
        actual_key = field_map.get(field_lower)
        
        if actual_key:
            value = caption_data[actual_key]
            # Format the value nicely
            if isinstance(value, list):
                value_str = ", ".join(str(v) for v in value)
            else:
                value_str = str(value)
            
            # Display with proper alignment
            print(f"{indent_str}{actual_key:20s} {value_str}")
        else:
            print(f"{indent_str}{field:20s} (not found)")



def _shotlist_show(args):
    from services.shotlist import get_shot, get_scene_shots, resolve_filename
    project_path = prefs.get("path")
    
    try:
        filename = resolve_filename(project_path, args.tmdb, args.filename, args.media)
        
        if args.show_type == "shot":
            shot = get_shot(project_path, filename, args.index, args.media)
            if args.field:
                if args.json:
                    # Extract fields and output as JSON
                    extracted = _extract_shot_fields(shot, args.field)
                    print(json.dumps(extracted, indent=2))
                else:
                    # Display fields in table format
                    _display_shot_fields(shot, args.field)
            else:
                print(json.dumps(shot, indent=2))
        elif args.show_type == "scene":
            shots = get_scene_shots(project_path, filename, args.scene_number, args.media)
            if args.field:
                if args.json:
                    # Extract fields from all shots and output as JSON array
                    extracted_shots = [_extract_shot_fields(shot, args.field) for shot in shots]
                    print(json.dumps(extracted_shots, indent=2))
                else:
                    # Display fields in table format for each shot
                    for i, shot in enumerate(shots):
                        if i > 0:
                            print()  # blank line between shots
                        print(f"Shot {shot.get('Scene', '?')}.{i}:")
                        _display_shot_fields(shot, args.field, indent=2)
            else:
                print(json.dumps(shots, indent=2))
    except ValueError as e:
        print(f"✗ Error: {e}", file=sys.stderr)
        sys.exit(1)
    except IndexError as e:
        print(f"✗ Error: {e}", file=sys.stderr)
        sys.exit(1)
    except FileNotFoundError as e:
        print(f"✗ Error: {e}", file=sys.stderr)
        sys.exit(1)
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        print(f"✗ Error: Invalid data format - {e}", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"✗ Error: {e}", file=sys.stderr)
        sys.exit(1)


# ---------------------------------------------------------------------------
# shot command
# ---------------------------------------------------------------------------

def cmd_shot(args):
    _require_path()
    sub = args.shot_subcommand
    if sub == "detect":
        _shot_detect(args)
    elif sub == "visualizer":
        _shot_visualizer(args)


def _shot_detect(args):
    """Detect shot boundaries using TransNetV2."""
    from generators.shot_detection import detect_shots_transnet, write_shotlist_csv
    from services.shotlist import resolve_filename, get_shotlist_path
    from services.metadata import get_metadata

    project_path = prefs.get("path")
    media_type = args.media
    notify = getattr(args, "notify", False)
    notify_items = getattr(args, "notify_items", False)

    try:
        if getattr(args, 'all', False):
            _shot_detect_all(project_path, media_type, args.force,
                             notify=notify, notify_items=notify_items)
            return

        # Resolve filename from query or tmdb
        if args.tmdb is not None:
            filename = resolve_filename(project_path, args.tmdb, None, media_type)
        elif args.query:
            # Try to find matching file by substring
            matches = get_metadata(project_path, args.query, media_type)
            if not matches:
                print(f"✗ Error: No file found matching '{args.query}'", file=sys.stderr)
                sys.exit(1)
            elif len(matches) > 1:
                print(f"✗ Error: Multiple files match '{args.query}':", file=sys.stderr)
                for m in matches:
                    print(f"  - {m['filename']}", file=sys.stderr)
                print("\nUse --tmdb <id> or a more specific query", file=sys.stderr)
                sys.exit(1)
            filename = matches[0]['filename']
        else:
            print("✗ Error: Must provide query or --tmdb", file=sys.stderr)
            sys.exit(1)
        
        # Get full video path
        video_path = Path(project_path) / "media" / "videos" / media_type / filename
        if not video_path.exists():
            print(f"✗ Error: Video file not found: {video_path}", file=sys.stderr)
            sys.exit(1)
        
        print(f"Detecting shots for: {filename}")
        print(f"Using TransNetV2...")
        
        # Detect shots
        import time
        start_time = time.time()
        shots = detect_shots_transnet(str(video_path))
        elapsed = time.time() - start_time
        
        print(f"Found {len(shots)} shots in {elapsed:.1f} seconds")
        
        # Write CSV
        csv_path = write_shotlist_csv(
            project_path,
            filename,
            shots,
            media_type,
            force=args.force
        )
        
        print(f"✓ Saved to: {csv_path}")

        if notify:
            from services.notify import discord_notify
            discord_notify(
                f"✓ Shot detection complete: {filename}\n"
                f"{len(shots)} shots in {elapsed:.1f}s",
                project_path,
            )

    except ImportError as e:
        print(f"✗ {e}", file=sys.stderr)
        sys.exit(1)
    except FileExistsError as e:
        print(f"✗ {e}", file=sys.stderr)
        sys.exit(1)
    except ValueError as e:
        print(f"✗ Error: {e}", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"✗ Error: {e}", file=sys.stderr)
        import traceback
        traceback.print_exc()
        sys.exit(1)


def _shot_detect_all(
    project_path: str,
    media_type: str,
    force: bool,
    *,
    notify: bool = False,
    notify_items: bool = False,
):
    """Detect shots for all metadata entries that don't yet have a shotlist."""
    from generators.shot_detection import detect_shots_transnet, write_shotlist_csv
    from services.shotlist import get_shotlist_path
    from services.metadata import get_metadata
    import time

    entries = get_metadata(project_path, media_type=media_type)
    if not entries:
        print("No metadata entries found.")
        return

    pending = []
    skipped = []
    for entry in entries:
        filename = entry.get('filename')
        if not filename:
            continue
        shotlist_path = get_shotlist_path(project_path, filename, media_type)
        if shotlist_path.exists() and not force:
            skipped.append(filename)
        else:
            video_path = Path(project_path) / "media" / "videos" / media_type / filename
            if video_path.exists():
                pending.append(filename)
            else:
                print(f"  ⚠ Skipping (video not found): {filename}")

    print(f"Found {len(entries)} entries: {len(pending)} to process, {len(skipped)} already have shotlists.")
    if skipped:
        print("  (use --force to reprocess existing shotlists)")
    print()

    failed = []
    for i, filename in enumerate(pending, 1):
        video_path = Path(project_path) / "media" / "videos" / media_type / filename
        print(f"[{i}/{len(pending)}] {filename}")
        try:
            start_time = time.time()
            shots = detect_shots_transnet(str(video_path))
            elapsed = time.time() - start_time
            csv_path = write_shotlist_csv(project_path, filename, shots, media_type, force=force)
            print(f"  ✓ {len(shots)} shots in {elapsed:.1f}s → {csv_path.name}")
            if notify_items:
                from services.notify import discord_notify
                discord_notify(
                    f"[{i}/{len(pending)}] ✓ {filename}\n"
                    f"{len(shots)} shots in {elapsed:.1f}s",
                    project_path,
                )
        except Exception as e:
            print(f"  ✗ Failed: {e}", file=sys.stderr)
            failed.append(filename)

    print()
    ok = len(pending) - len(failed)
    print(f"Done. {ok}/{len(pending)} processed successfully.")
    if failed:
        print("Failed:")
        for f in failed:
            print(f"  - {f}")

    if notify:
        from services.notify import discord_notify
        summary = f"Shot detection batch complete: {ok}/{len(pending)} succeeded"
        if failed:
            summary += "\nFailed:\n" + "\n".join(f"  {f}" for f in failed)
        discord_notify(summary, project_path)


def _shot_visualizer(args):
    """Launch shot visualizer GUI."""
    import subprocess
    from pathlib import Path
    from services.shotlist import get_shotlist_path
    from services.metadata import get_metadata

    cli_dir = Path(__file__).parent
    validator_path = cli_dir / "visualizers" / "shot_visualizer.py"

    if not validator_path.exists():
        print(f"✗ Error: {validator_path.name} not found at {validator_path}", file=sys.stderr)
        sys.exit(1)

    _require_path()
    project_path = prefs.get("path")
    media_type = args.media

    # Resolve the list of filenames to validate
    if getattr(args, 'all', False):
        entries = get_metadata(project_path, media_type=media_type)
        filenames = [
            e['filename'] for e in entries
            if e.get('filename') and get_shotlist_path(project_path, e['filename'], media_type).exists()
        ]
        if not filenames:
            print("✗ Error: No shotlists found.", file=sys.stderr)
            sys.exit(1)
    elif args.tmdb is not None:
        entries = get_metadata(project_path, media_type=media_type)
        filenames = [e['filename'] for e in entries if e.get('tmdb') == str(args.tmdb)]
        if not filenames:
            print(f"✗ Error: No file found with TMDb ID: {args.tmdb}", file=sys.stderr)
            sys.exit(1)
    elif args.query:
        entries = get_metadata(project_path, query=args.query, media_type=media_type)
        if not entries:
            print(f"✗ Error: No file found matching '{args.query}'", file=sys.stderr)
            sys.exit(1)
        if len(entries) > 1:
            print(f"✗ Error: Multiple files match '{args.query}':", file=sys.stderr)
            for e in entries:
                print(f"  - {e['filename']}", file=sys.stderr)
            sys.exit(1)
        filenames = [entries[0]['filename']]
    else:
        print("✗ Error: Must provide query, --tmdb, or --all", file=sys.stderr)
        sys.exit(1)

    cmd = [
        sys.executable, str(validator_path),
        "--media", media_type,
        "--project", project_path,
        "--filenames",
    ] + filenames

    try:
        subprocess.run(cmd, check=True)
    except subprocess.CalledProcessError as e:
        sys.exit(e.returncode)
    except KeyboardInterrupt:
        sys.exit(0)


# ---------------------------------------------------------------------------
# api_key command
# ---------------------------------------------------------------------------

_API_KEY_SERVICES = ("discord", "opensubtitles", "tmdb")


def cmd_api_key(args):
    from pathlib import Path
    _require_path()
    service = args.service
    key_file = Path(prefs.get("path")) / "preferences" / "keys" / f"{service}_api_key.txt"
    if args.api_key_subcommand == "set":
        key_file.parent.mkdir(parents=True, exist_ok=True)
        key_file.write_text(args.value.strip())
        print(f"{service} API key saved.")
    elif args.api_key_subcommand == "get":
        if not key_file.exists():
            print(f"(not set)")
        else:
            print(key_file.read_text().strip())



def cmd_tool(args):
    sub = args.tool_subcommand
    if sub == "version":
        cmd_version_init(args) if getattr(args, "init", False) else cmd_version(args)
    elif sub == "path":
        cmd_path(args)
    elif sub == "name":
        cmd_name(args)
    elif sub == "api_key":
        cmd_api_key(args)
    elif sub == "notify":
        cmd_notify(args)
    elif sub == "model":
        cmd_model(args)
    elif sub == "default":
        cmd_tool_default(args)


def cmd_tool_default(args):
    """Get or set persistent defaults for annotate and other commands."""
    sub = args.default_subcommand
    if sub == "set":
        key = args.key
        if key not in _ANNOTATE_DEFAULT_KEYS:
            print(f"✗ Unknown default key '{key}'. Available keys: {', '.join(_ANNOTATE_DEFAULT_KEYS)}", file=sys.stderr)
            sys.exit(1)
        pref_key, _ = _ANNOTATE_DEFAULT_KEYS[key]
        prefs.set(pref_key, args.value)
        print(f"✓ Default '{key}' set to '{args.value}'")
    elif sub == "get" or sub is None:
        key = getattr(args, "key", None)
        keys = [key] if key else list(_ANNOTATE_DEFAULT_KEYS)
        for k in keys:
            pref_key, fallback = _ANNOTATE_DEFAULT_KEYS[k]
            val = prefs.get(pref_key, fallback)
            print(f"{k}: {val}")


def cmd_notify(args):
    """Send a test notification to verify a service is configured correctly."""
    _require_path()
    service = args.notify_service
    if service == "discord":
        import uuid
        from services.notify import discord_notify
        project_path = prefs.get("path")
        test_id = uuid.uuid4().hex[:8].upper()
        print(f"Sending test Discord notification...  (id: {test_id})")
        ok = discord_notify(f"✓ crossing-tool notification test — id: {test_id}", project_path)
        if ok:
            print(f"✓ Notification sent. Check Discord for message with id: {test_id}")
    else:
        print(f"✗ Unknown notification service: {service}", file=sys.stderr)
        sys.exit(1)


def cmd_audit(args):
    """Report missing metadata, shotlists, and subtitles."""
    from pathlib import Path
    from services.metadata import get_metadata

    _require_path()
    project_path = prefs.get("path")
    media_type = args.media

    video_dir  = Path(project_path) / "media" / "videos" / media_type
    subtitle_dir = Path(project_path) / "media" / "subtitles" / media_type
    shotlist_dir = Path(project_path) / "data" / "shotlists" / media_type

    video_files = sorted(f.name for f in video_dir.glob("*") if f.is_file()) if video_dir.exists() else []
    entries = get_metadata(project_path, media_type=media_type)
    meta_filenames = {e["filename"] for e in entries if e.get("filename")}

    # Videos on disk with no metadata row
    no_metadata = [f for f in video_files if f not in meta_filenames]

    # Metadata entries with no shotlist CSV
    no_shotlist = []
    for entry in entries:
        fn = entry.get("filename", "")
        if fn and not (shotlist_dir / (Path(fn).stem + ".csv")).exists():
            no_shotlist.append(fn)

    # Metadata entries with no subtitle file
    no_subtitle = []
    for entry in entries:
        fn = entry.get("filename", "")
        if not fn:
            continue
        stem = Path(fn).stem
        if not (subtitle_dir / (stem + ".srt")).exists() and \
           not (subtitle_dir / (stem.replace(" ", "-") + ".srt")).exists():
            no_subtitle.append(fn)

    n = len(entries)
    print(f"Audit · {media_type}  ({len(video_files)} video file(s) · {n} metadata entry(ies))")

    print()
    if no_metadata:
        print(f"  No metadata   ({len(no_metadata)}):")
        for f in no_metadata:
            print(f"    {f}")
    else:
        print(f"  Metadata:   ✓ all {n}")

    print()
    if no_shotlist:
        print(f"  No shotlist   ({len(no_shotlist)} / {n}):")
        for f in no_shotlist:
            print(f"    {f}")
    else:
        print(f"  Shotlists:  ✓ all {n}")

    print()
    if no_subtitle:
        print(f"  No subtitles  ({len(no_subtitle)} / {n}):")
        for f in no_subtitle:
            print(f"    {f}")
    else:
        print(f"  Subtitles:  ✓ all {n}")


# ---------------------------------------------------------------------------
# subtitle command
# ---------------------------------------------------------------------------

def cmd_subtitle(args):
    _require_path()
    sub = args.subtitle_subcommand
    if sub == "fetch":
        _subtitle_fetch(args)
    elif sub == "list":
        _subtitle_list(args)


def _subtitle_fetch(args):
    from services.metadata import fetch_subtitle, get_metadata
    from services.shotlist import resolve_filename

    project_path = prefs.get("path")
    media_type = args.media
    force = getattr(args, "force", False)
    tmdb = getattr(args, "tmdb", None)
    query_words = getattr(args, "query", None) or []
    query = " ".join(query_words).strip() if query_words else None
    fetch_all = getattr(args, "all", False)

    rows = get_metadata(project_path, media_type=media_type)
    if not rows:
        print("No metadata entries found.")
        return

    if tmdb is not None:
        try:
            filename = resolve_filename(project_path, tmdb, None, media_type)
        except ValueError as exc:
            print(f"✗ {exc}", file=sys.stderr)
            sys.exit(1)
        targets = [r for r in rows if r.get("filename") == filename]
    elif fetch_all:
        targets = rows
    elif query:
        q = query.lower()
        targets = [
            r for r in rows
            if q in str(r.get("filename", "")).lower()
            or q in str(r.get("title", "")).lower()
        ]
        if not targets:
            print(f"✗ No entries match '{query}'", file=sys.stderr)
            sys.exit(1)
    else:
        print("✗ Provide a search query, --tmdb, or --all", file=sys.stderr)
        sys.exit(1)

    subtitle_dir = Path(project_path) / "media" / "subtitles" / media_type
    ok = skip_exists = skip_no_imdb = failed = 0

    for row in targets:
        filename = row.get("filename", "")
        if not filename:
            continue
        imdb_id = row.get("imdb", "")
        if not imdb_id:
            print(f"  skip (no imdb)  {filename}")
            skip_no_imdb += 1
            continue

        stem = Path(filename).stem
        existing = (subtitle_dir / (stem + ".srt")).exists() or \
                   (subtitle_dir / (stem.replace(" ", "-") + ".srt")).exists()
        if existing and not force:
            print(f"  skip (exists)   {filename}")
            skip_exists += 1
            continue

        try:
            path = fetch_subtitle(
                filename,
                project_path,
                media_type,
                imdb_id,
                row.get("title", ""),
                row.get("year"),
                force=force,
            )
            if path:
                print(f"  ✓ fetched       {filename}")
                ok += 1
            else:
                print(f"  ✗ no results    {filename}  [{imdb_id}]")
                failed += 1
        except RuntimeError as exc:
            print(f"  ✗ error         {filename}: {exc}")
            failed += 1

    total = ok + skip_exists + skip_no_imdb + failed
    print(
        f"\n{total} processed — "
        f"{ok} fetched, {skip_exists} already present, "
        f"{skip_no_imdb} no IMDb ID, {failed} failed"
    )


def _subtitle_list(args):
    from services.metadata import get_metadata

    project_path = prefs.get("path")
    media_type = args.media
    subtitle_dir = Path(project_path) / "media" / "subtitles" / media_type

    rows = get_metadata(project_path, media_type=media_type)
    if not rows:
        print("No metadata entries found.")
        return

    present = []
    missing_fetchable = []
    missing_no_imdb = []

    for row in rows:
        filename = row.get("filename", "")
        if not filename:
            continue
        stem = Path(filename).stem
        has = (subtitle_dir / (stem + ".srt")).exists() or \
              (subtitle_dir / (stem.replace(" ", "-") + ".srt")).exists()
        label = f"{row.get('title', filename)} ({row.get('year', '?')})"
        if has:
            present.append(label)
        elif row.get("imdb"):
            missing_fetchable.append(label)
        else:
            missing_no_imdb.append(label)

    for label in present:
        print(f"  ✓  {label}")
    for label in missing_fetchable:
        print(f"  ✗  {label}")
    for label in missing_no_imdb:
        print(f"  ✗  {label}  [no imdb id]")

    total = len(present) + len(missing_fetchable) + len(missing_no_imdb)
    print(
        f"\n{total} total — "
        f"{len(present)} present, "
        f"{len(missing_fetchable)} missing (fetchable), "
        f"{len(missing_no_imdb)} missing (no IMDb ID)"
    )


# ---------------------------------------------------------------------------
# text command family
# ---------------------------------------------------------------------------

def _text_calibrate(args):
    """Sweep confidence thresholds using known ground-truth strings."""
    from generators.text_extraction import calibrate_text_detection
    from services.shotlist import resolve_filename

    project_path = prefs.get("path")
    media_type = args.media
    lang = getattr(args, "lang", "en")
    window = getattr(args, "window", 180.0)
    expected = args.expect

    try:
        tmdb = getattr(args, "tmdb", None)
        filename_arg = getattr(args, "filename", None)
        filename = resolve_filename(project_path, tmdb, filename_arg, media_type)
    except ValueError as exc:
        print(f"✗ {exc}", file=sys.stderr)
        sys.exit(1)

    video_path = Path(project_path) / "media" / "videos" / media_type / filename
    if not video_path.exists():
        print(f"✗ Video not found: {video_path}", file=sys.stderr)
        sys.exit(1)

    print(f"Calibrating: {filename}")
    print(f"  window: first {window:.0f}s  |  lang: {lang}")
    print(f"  expected strings ({len(expected)}):")
    for e in expected:
        print(f"    • {e!r}")
    print()

    result = calibrate_text_detection(
        str(video_path),
        expected,
        window_seconds=window,
        lang=lang,
    )

    print(f"Sampled {result['frames_sampled']} frames, {result['raw_detection_count']} raw detections before filtering")
    print()
    print(f"  {'Threshold':>9}  {'Hits':>6}  {'Found':>9}  Missed")
    print("  " + "-" * 56)

    best_threshold = None
    for r in result["thresholds"]:
        found_count = len(r["found"])
        total_expected = len(expected)
        missed_str = ", ".join(f"'{m}'" for m in r["missed"]) if r["missed"] else "—"
        marker = ""
        if found_count == total_expected and best_threshold is None:
            best_threshold = r["threshold"]
            marker = "  ◄ recommended"
        print(f"  {r['threshold']:>9.2f}  {r['total_hits']:>6}  {found_count:>3}/{total_expected:<3}   {missed_str}{marker}")

    print()
    if best_threshold is not None:
        print(f"Recommendation: --min-confidence {best_threshold:.2f}")
        print(f"  (highest threshold that recovers all {len(expected)} expected string(s))")
    else:
        print(f"⚠ No threshold recovered all expected strings.")
        print(f"  Check the strings appear within the first {window:.0f}s, or use --window <seconds>.")


def cmd_text(args):
    _require_path()
    sub = args.text_subcommand
    if sub == "detect":
        _text_detect(args)
    elif sub == "list":
        _text_list(args)
    elif sub == "visualizer":
        _text_visualizer(args)
    elif sub == "calibrate":
        _text_calibrate(args)


def _text_detect(args):
    """Detect on-screen text for one film or all films (--all)."""
    from generators.text_extraction import extract_text_events, write_text_csv, get_text_csv_path
    import time

    project_path = prefs.get("path")
    media_type = args.media
    force = getattr(args, "force", False)
    sample_fps = getattr(args, "sample_fps", 1.0)
    lang = getattr(args, "lang", "en")
    min_confidence = getattr(args, "min_confidence", 0.75)
    verbose = getattr(args, "verbose", False)
    cards_only = not getattr(args, "include_diegetic", False)
    do_all = getattr(args, "all", False)
    notify = getattr(args, "notify", False)
    notify_items = getattr(args, "notify_items", False)

    # ------------------------------------------------------------------ batch
    if do_all:
        from services.metadata import get_metadata
        entries = get_metadata(project_path, media_type=media_type)
        filenames = [
            e["filename"] for e in entries
            if e.get("filename")
            and (Path(project_path) / "media" / "videos" / media_type / e["filename"]).exists()
        ]

        if not filenames:
            print("No films to process.")
            return

        pending, skipped = [], []
        for fn in filenames:
            if get_text_csv_path(project_path, fn, media_type).exists() and not force:
                skipped.append(fn)
            else:
                pending.append(fn)

        print(f"{len(filenames)} film(s): {len(pending)} to process, {len(skipped)} already done.")
        if skipped:
            print("  (use --force to reprocess)")

        failed = []
        for i, filename in enumerate(pending, 1):
            video_path = Path(project_path) / "media" / "videos" / media_type / filename
            if not video_path.exists():
                print(f"  [{i}/{len(pending)}] ⚠ video not found, skipping: {filename}")
                failed.append(filename)
                continue
            print(f"  [{i}/{len(pending)}] {filename}")
            t0 = time.time()
            try:
                events = extract_text_events(str(video_path), sample_fps=sample_fps, lang=lang, min_confidence=min_confidence, verbose=verbose, project_path=project_path, filename=filename, media_type=media_type, cards_only=cards_only)
                rows = [{"filename": filename, **e} for e in events]
                dest = write_text_csv(project_path, filename, rows, media_type, force=force)
                elapsed = time.time() - t0
                print(f"    ✓ {len(events)} event(s) in {elapsed:.1f}s → {dest.name}")
                if notify_items:
                    from services.notify import discord_notify
                    discord_notify(
                        f"[{i}/{len(pending)}] ✓ {filename}\n"
                        f"{len(events)} text event(s) in {elapsed:.1f}s",
                        project_path,
                    )
            except FileExistsError as exc:
                print(f"    ⚠ {exc}")
            except Exception as exc:
                print(f"    ✗ failed: {exc}", file=sys.stderr)
                import traceback
                traceback.print_exc()
                failed.append(filename)

        print()
        ok = len(pending) - len(failed)
        print(f"Done. {ok}/{len(pending)} processed successfully.")
        if failed:
            print("Failed:")
            for f in failed:
                print(f"  {f}")

        if notify:
            from services.notify import discord_notify
            summary = f"Text detection batch complete: {ok}/{len(pending)} succeeded"
            if failed:
                summary += "\nFailed:\n" + "\n".join(f"  {f}" for f in failed)
            discord_notify(summary, project_path)
        return

    # ------------------------------------------------------------------ single
    from services.shotlist import resolve_filename

    try:
        tmdb = getattr(args, "tmdb", None)
        filename_arg = getattr(args, "filename", None)
        filename = resolve_filename(project_path, tmdb, filename_arg, media_type)
    except ValueError as exc:
        print(f"✗ {exc}", file=sys.stderr)
        sys.exit(1)

    video_path = Path(project_path) / "media" / "videos" / media_type / filename
    if not video_path.exists():
        print(f"✗ Video not found: {video_path}", file=sys.stderr)
        sys.exit(1)

    print(f"Detecting text: {filename}")
    print(f"  sample rate: {sample_fps} fps  |  lang: {lang}")

    t0 = time.time()
    try:
        events = extract_text_events(str(video_path), sample_fps=sample_fps, lang=lang, min_confidence=min_confidence, verbose=verbose, project_path=project_path, filename=filename, media_type=media_type, cards_only=cards_only)
    except ImportError as exc:
        print(f"✗ {exc}", file=sys.stderr)
        sys.exit(1)
    except Exception as exc:
        print(f"✗ Detection failed: {exc}", file=sys.stderr)
        import traceback
        traceback.print_exc()
        sys.exit(1)

    elapsed = time.time() - t0
    print(f"  found {len(events)} text event(s) in {elapsed:.1f}s")

    rows = [{"filename": filename, **e} for e in events]

    try:
        dest = write_text_csv(project_path, filename, rows, media_type, force=force)
    except FileExistsError as exc:
        print(f"✗ {exc}", file=sys.stderr)
        sys.exit(1)

    print(f"✓ Saved: {dest}")

    from collections import Counter
    counts = Counter(r["type"] for r in rows)
    for t, n in sorted(counts.items()):
        print(f"    {n:3d}  {t}")

    if notify:
        from services.notify import discord_notify
        discord_notify(
            f"✓ Text detection complete: {filename}\n"
            f"{len(events)} event(s) in {elapsed:.1f}s",
            project_path,
        )


def _text_list(args):
    """List text CSVs."""
    from generators.text_extraction import list_text_csvs

    project_path = prefs.get("path")
    media_type = getattr(args, "media", None)
    as_json = getattr(args, "json", False)

    results = list_text_csvs(project_path, media_type)

    if as_json:
        print(json.dumps(results, indent=2))
        return

    if not results:
        print("No text CSVs found.")
        return

    print(f"Found {len(results)} text CSV(s):\n")
    for r in results:
        counts_str = "  ".join(f"{v} {k}" for k, v in sorted(r["type_counts"].items()))
        print(f"  {r['filename']}  ({r['media_type']})")
        print(f"    {r['row_count']} event(s)  ·  {counts_str or 'none'}")
        print()


def _annotate_remove(args):
    """Remove shot-annotation JSON for one or all films."""
    from generators.annotate import remove_file_annotations
    from services.shotlist import resolve_filename

    _require_path()
    project_path = prefs.get("path")
    media_type = getattr(args, "media", "movies")

    if getattr(args, "all", False):
        from services.metadata import get_metadata
        entries = get_metadata(project_path, media_type=media_type)
        filenames = [e["filename"] for e in entries if e.get("filename")]
    else:
        if not args.filename and not getattr(args, "tmdb", None):
            print("✗ Provide a filename or --tmdb (or use --all)", file=sys.stderr)
            sys.exit(1)
        filenames = [resolve_filename(project_path, getattr(args, "tmdb", None), args.filename, media_type)]

    removed = 0
    skipped = 0
    for fn in filenames:
        if remove_file_annotations(project_path, fn, media_type):
            print(f"✓ Removed annotations: {fn}")
            removed += 1
        else:
            print(f"  (no annotation file)  {fn}")
            skipped += 1

    print(f"\nRemoved {removed}  |  already absent {skipped}")


def _annotate_visualizer(args):
    """Launch the annotation visualizer GUI."""
    from generators.annotate import get_annotation_json_path as _ann_json_path

    _require_path()
    project_path = prefs.get("path")
    media_type = getattr(args, "media", "movies")

    import subprocess
    validator_path = Path(__file__).parent / "visualizers" / "annotation_visualizer.py"
    if not validator_path.exists():
        print(f"\u2717 Error: annotation_visualizer.py not found at {validator_path}", file=sys.stderr)
        sys.exit(1)

    if getattr(args, "all", False):
        from services.metadata import get_metadata
        entries = get_metadata(project_path, media_type=media_type)
        filenames = [e["filename"] for e in entries if e.get("filename")]
        if not filenames:
            print("\u2717 No films found in metadata.", file=sys.stderr)
            sys.exit(1)
    elif getattr(args, "tmdb", None) is not None:
        from services.metadata import get_metadata
        entries = get_metadata(project_path, media_type=media_type)
        filenames = [e["filename"] for e in entries if e.get("tmdb") == str(args.tmdb)]
        if not filenames:
            print(f"\u2717 No file found with TMDb ID: {args.tmdb}", file=sys.stderr)
            sys.exit(1)
    elif getattr(args, "query", None):
        from services.metadata import get_metadata
        entries = get_metadata(project_path, query=args.query, media_type=media_type)
        if not entries:
            print(f"\u2717 No file found matching '{args.query}'", file=sys.stderr)
            sys.exit(1)
        if len(entries) > 1:
            print(f"\u2717 Multiple files match '{args.query}':", file=sys.stderr)
            for e in entries:
                print(f"  - {e['filename']}", file=sys.stderr)
            sys.exit(1)
        filenames = [entries[0]["filename"]]
    else:
        print("\u2717 Must provide a query, --tmdb, or --all", file=sys.stderr)
        sys.exit(1)

    valid = filenames

    cmd = [
        sys.executable, str(validator_path),
        "--media", media_type,
        "--project", project_path,
        "--filenames",
    ] + valid

    try:
        subprocess.run(cmd, check=True)
    except subprocess.CalledProcessError as e:
        sys.exit(e.returncode)
    except KeyboardInterrupt:
        sys.exit(0)


def _text_visualizer(args):
    """Launch the text visualizer GUI."""
    from generators.text_extraction import get_text_csv_path

    _require_path()
    project_path = prefs.get("path")
    media_type = getattr(args, "media", "movies")

    # GUI mode — resolve the list of filenames to visualize
    import subprocess
    validator_path = Path(__file__).parent / "visualizers" / "text_visualizer.py"
    if not validator_path.exists():
        print(f"\u2717 Error: text_visualizer.py not found at {validator_path}", file=sys.stderr)
        sys.exit(1)

    if getattr(args, "all", False):
        from services.metadata import get_metadata
        entries = get_metadata(project_path, media_type=media_type)
        filenames = [
            e["filename"]
            for e in entries
            if e.get("filename")
            and get_text_csv_path(project_path, e["filename"], media_type).exists()
        ]
        if not filenames:
            print("\u2717 No text CSVs found.", file=sys.stderr)
            sys.exit(1)
    elif getattr(args, "tmdb", None) is not None:
        from services.metadata import get_metadata
        entries = get_metadata(project_path, media_type=media_type)
        filenames = [e["filename"] for e in entries if e.get("tmdb") == str(args.tmdb)]
        if not filenames:
            print(f"\u2717 No file found with TMDb ID: {args.tmdb}", file=sys.stderr)
            sys.exit(1)
    elif getattr(args, "query", None):
        from services.metadata import get_metadata
        entries = get_metadata(project_path, query=args.query, media_type=media_type)
        if not entries:
            print(f"\u2717 No file found matching '{args.query}'", file=sys.stderr)
            sys.exit(1)
        if len(entries) > 1:
            print(f"\u2717 Multiple files match '{args.query}':", file=sys.stderr)
            for e in entries:
                print(f"  - {e['filename']}", file=sys.stderr)
            sys.exit(1)
        filenames = [entries[0]["filename"]]
    else:
        print(
            "\u2717 Must provide a query, --tmdb, or --all",
            file=sys.stderr,
        )
        sys.exit(1)

    # Warn about any entry without a text CSV
    valid = []
    for fn in filenames:
        csv_path = get_text_csv_path(project_path, fn, media_type)
        if csv_path.exists():
            valid.append(fn)
        else:
            print(f"  \u26a0 No text CSV for: {fn}  (skipping)", file=sys.stderr)

    if not valid:
        print("\u2717 No text CSVs found for the selected film(s).", file=sys.stderr)
        sys.exit(1)

    cmd = [
        sys.executable, str(validator_path),
        "--media", media_type,
        "--project", project_path,
        "--filenames",
    ] + valid

    try:
        subprocess.run(cmd, check=True)
    except subprocess.CalledProcessError as e:
        sys.exit(e.returncode)
    except KeyboardInterrupt:
        sys.exit(0)


def cmd_generate(args):
    sub = args.generate_subcommand
    if sub == "compose":
        cmd_compose(args)
    elif sub == "mosaic":
        cmd_mosaic(args)


def cmd_compose(args):
    """Dispatch compose subcommands."""
    _require_path()
    sub = args.compose_subcommand
    if sub == "search":
        _compose_search(args)


def _compose_search(args):
    """compose search <query> [scope...] — compose a poster from search results."""
    from services.search import search_shots
    from generators.compose import compose_from_search_results

    project_path = prefs.get("path")
    media_type   = getattr(args, "media", "movies")
    scopes       = (args.scope or []) + (getattr(args, "movie", None) or [])
    scopes       = scopes or None
    use_all      = getattr(args, "all", False)
    field        = getattr(args, "field", None)
    limit        = getattr(args, "limit", None)
    orientation  = getattr(args, "orientation", "portrait")
    seed         = getattr(args, "seed", None)
    output_path  = getattr(args, "output", None)
    fmt          = getattr(args, "format", "jpg")
    open_result  = not getattr(args, "no_open", False)

    search_result = search_shots(
        query=args.query,
        scopes=scopes,
        field=field,
        limit=limit,
        limit_per_item=None,
        use_all=use_all,
        project_path=project_path,
        media_type=media_type,
    )
    results = search_result["results"]

    if not results:
        print(f"✗ No results for query '{args.query}'.", file=sys.stderr)
        sys.exit(1)

    print(f"Composing from {len(results)} result(s) (query: {args.query!r})…")

    try:
        out_path = compose_from_search_results(
            results,
            project_path,
            orientation=orientation,
            seed=seed,
            output_path=output_path,
            fmt=fmt,
            open_result=open_result,
        )
        print(f"✓ Saved: {out_path}")
        if getattr(args, "notify", False):
            from services.notify import discord_notify
            discord_notify(
                f"✓ Compose complete: '{args.query}' → {out_path.name}",
                project_path,
            )
    except (ValueError, FileNotFoundError) as exc:
        print(f"✗ compose failed: {exc}", file=sys.stderr)
        sys.exit(1)
    except Exception as exc:
        print(f"✗ compose failed: {exc}", file=sys.stderr)
        import traceback
        traceback.print_exc()
        sys.exit(1)


# ---------------------------------------------------------------------------
# mosaic command family
# ---------------------------------------------------------------------------

def cmd_mosaic(args):
    """Generate a mosaic contact sheet from thumbnails, personas, or search results."""
    _require_path()
    sub = args.mosaic_subcommand
    if sub == "thumbnails":
        _mosaic_thumbnails(args)
    elif sub == "personas":
        _persona_mosaic(args)
    elif sub == "search":
        _mosaic_search(args)
    elif sub == "export":
        _mosaic_export(args)
    elif sub == "visualizer":
        _mosaic_visualizer(args)


def _mosaic_thumbnails(args):
    """Collect thumbnails for a media type and render a mosaic grid."""
    from services.metadata import get_metadata
    from generators.mosaic import MosaicItem, render_mosaic

    project_path = prefs.get("path")
    media_type   = args.media
    caption_mode = getattr(args, "caption", "short")
    layout       = getattr(args, "layout", "landscape")

    rows = get_metadata(project_path, media_type=media_type)
    if not rows:
        print(f"✗ No metadata entries found for '{media_type}'.", file=sys.stderr)
        sys.exit(1)

    thumbnail_dir = Path(project_path) / "media" / "thumbnails" / media_type

    items = []
    missing = 0
    for row in rows:
        filename = row.get("filename", "")
        if not filename:
            continue
        stem = Path(filename).stem

        # Canonical path first, then legacy dash-separated fallback
        thumb = thumbnail_dir / (stem + ".jpg")
        if not thumb.exists():
            thumb = thumbnail_dir / (stem.replace(" ", "-") + ".jpg")
        if not thumb.exists():
            print(f"  ⚠ no thumbnail: {filename}")
            missing += 1
            continue

        if caption_mode == "none":
            caption = None
        else:
            title = row.get("title", stem)
            year  = row.get("year", "")
            caption = f"{title} ({year})" if year else title

        items.append(MosaicItem(
            image_path=thumb,
            caption=caption,
            metadata=dict(row),
        ))

    if not items:
        print(f"✗ No thumbnails found in {thumbnail_dir}.", file=sys.stderr)
        print(f"  Fetch them with: crossing metadata update --media {media_type}", file=sys.stderr)
        sys.exit(1)

    found_str = f"{len(items)} thumbnail(s)"
    missing_str = f", {missing} missing" if missing else ""
    print(f"Building thumbnails mosaic for: {media_type}")
    print(f"  {found_str}{missing_str}")

    output_path = getattr(args, "output", None)
    if output_path:
        output_path = Path(output_path)
    else:
        import datetime
        stamp = datetime.datetime.now().strftime("%Y-%m-%d-%H-%M-%S")
        output_path = (
            Path(project_path) / "output" / "mosaics"
            / f"{media_type}-thumbnails-mosaic-{stamp}.png"
        )

    try:
        out = render_mosaic(
            items,
            output_path,
            layout=layout,
            show_captions=(caption_mode != "none"),
        )
        print(f"✓ Saved: {out}")
        import subprocess
        subprocess.Popen(["xdg-open", str(out)])
    except ValueError as exc:
        print(f"✗ {exc}", file=sys.stderr)
        sys.exit(1)
    except Exception as exc:
        print(f"✗ Render failed: {exc}", file=sys.stderr)
        import traceback
        traceback.print_exc()
        sys.exit(1)


def _mosaic_search(args):
    """mosaic search <query> [scope...] — mosaic grid from shot annotation search."""
    import subprocess
    from services.search import search_shots
    from generators.mosaic import mosaic_from_search_results

    project_path = prefs.get("path")
    media_type   = getattr(args, "media", "movies")
    scopes       = (args.scope or []) + (getattr(args, "movie", None) or [])
    scopes       = scopes or None
    use_all      = getattr(args, "all", False)
    field        = getattr(args, "field", None)
    limit        = getattr(args, "limit", None)
    layout       = getattr(args, "layout", "landscape")
    frame_pct    = getattr(args, "frame_pct", 0.5)
    output_path  = getattr(args, "output", None)
    open_result  = not getattr(args, "no_open", False)

    search_result = search_shots(
        query=args.query,
        scopes=scopes,
        field=field,
        limit=limit,
        limit_per_item=None,
        use_all=use_all,
        project_path=project_path,
        media_type=media_type,
    )
    results = search_result["results"]

    if not results:
        print(f"✗ No results for query '{args.query}'.", file=sys.stderr)
        sys.exit(1)

    print(f"Building search mosaic: {len(results)} result(s) (query: {args.query!r})…")

    try:
        out = mosaic_from_search_results(
            results,
            project_path,
            output_path=output_path,
            layout=layout,
            frame_pct=frame_pct,
        )
        print(f"✓ Saved: {out}")
        if open_result:
            subprocess.Popen(["xdg-open", str(out)])
        if getattr(args, "notify", False):
            from services.notify import discord_notify
            discord_notify(
                f"✓ Mosaic search complete: '{args.query}' → {out.name}",
                project_path,
            )
    except ValueError as exc:
        print(f"✗ {exc}", file=sys.stderr)
        sys.exit(1)
    except Exception as exc:
        print(f"✗ Render failed: {exc}", file=sys.stderr)
        import traceback
        traceback.print_exc()
        sys.exit(1)


def _mosaic_visualizer(args):
    """mosaic visualizer — launch the interactive live mosaic explorer GUI."""
    from visualizers.mosaic_visualizer import run_visualizer
    run_visualizer(prefs.get("path"))


def _mosaic_export(args):
    """mosaic export <query> [scope...] — export individual JPEGs for each search result."""
    import subprocess
    from services.search import search_shots
    from generators.mosaic import export_frames_from_search_results

    project_path = prefs.get("path")
    media_type   = getattr(args, "media", "movies")
    scopes       = (args.scope or []) + (getattr(args, "movie", None) or [])
    scopes       = scopes or None
    use_all      = getattr(args, "all", False)
    field        = getattr(args, "field", None)
    limit        = getattr(args, "limit", None)
    frame_pct    = getattr(args, "frame_pct", 0.5)
    open_result  = not getattr(args, "no_open", False)

    search_result = search_shots(
        query=args.query,
        scopes=scopes,
        field=field,
        limit=limit,
        limit_per_item=None,
        use_all=use_all,
        project_path=project_path,
        media_type=media_type,
    )
    results = search_result["results"]

    if not results:
        print(f"✗ No results for query '{args.query}'.", file=sys.stderr)
        sys.exit(1)

    print(f"Exporting {len(results)} frame(s) for query: {args.query!r}…")

    try:
        out_dir = export_frames_from_search_results(
            results,
            project_path,
            query=args.query,
            field=field,
            frame_pct=frame_pct,
        )
        print(f"✓ Exported to: {out_dir}")
        if open_result:
            subprocess.Popen(["xdg-open", str(out_dir)])
        if getattr(args, "notify", False):
            from services.notify import discord_notify
            discord_notify(
                f"✓ Mosaic export complete: '{args.query}' → {out_dir.name}/",
                project_path,
            )
    except ValueError as exc:
        print(f"✗ {exc}", file=sys.stderr)
        sys.exit(1)
    except Exception as exc:
        print(f"✗ Export failed: {exc}", file=sys.stderr)
        import traceback
        traceback.print_exc()
        sys.exit(1)


# ---------------------------------------------------------------------------
# persona command family
# ---------------------------------------------------------------------------

def cmd_persona(args):
    """Dispatch persona subcommands."""
    _require_path()
    sub = args.persona_subcommand
    if sub == "detect":
        _persona_detect(args)
    elif sub == "get":
        _persona_get(args)


def _persona_get(args):
    """Print the persona JSON for a single film."""
    from services.persona.io import read_persona_json, get_persona_json_path
    from services.shotlist import resolve_filename

    project_path = prefs.get("path")
    media_type = args.media

    query_words = getattr(args, "query", None) or []
    query_str = " ".join(query_words).strip() if query_words else None

    if args.tmdb is None and not query_str:
        print("✗ Provide a title query or --tmdb <id>.", file=sys.stderr)
        sys.exit(1)

    try:
        filename = resolve_filename(project_path, args.tmdb, query_str, media_type)
    except ValueError as exc:
        print(f"✗ {exc}", file=sys.stderr)
        sys.exit(1)

    try:
        doc = read_persona_json(project_path, filename, media_type)
    except FileNotFoundError:
        path = get_persona_json_path(project_path, filename, media_type)
        print(f"✗ No persona JSON found: {path}", file=sys.stderr)
        print(f"  Run: crossing persona detect {query_str or f'--tmdb {args.tmdb}'}", file=sys.stderr)
        sys.exit(1)

    from services.persona.io import document_to_dict
    print(json.dumps(document_to_dict(doc), indent=2))


def _persona_mosaic(args):
    """Generate one mosaic image per persona in a subfolder."""
    from services.persona.mosaic import persona_mosaic
    from services.persona.io import get_persona_json_path
    from services.shotlist import resolve_filename

    project_path = prefs.get("path")
    media_type = args.media
    layout = getattr(args, "layout", "landscape")
    max_per = getattr(args, "max_per_persona", 6)
    output_path = Path(args.output) if getattr(args, "output", None) else None
    open_result = not getattr(args, "no_open", False)
    notify = getattr(args, "notify", False)

    query_words = getattr(args, "query", None) or []
    query_str = " ".join(query_words).strip() if query_words else None

    if args.tmdb is None and not query_str:
        print("✗ Provide a title query or --tmdb <id>.", file=sys.stderr)
        sys.exit(1)

    try:
        filename = resolve_filename(project_path, args.tmdb, query_str, media_type)
    except ValueError as exc:
        print(f"✗ {exc}", file=sys.stderr)
        sys.exit(1)

    json_path = get_persona_json_path(project_path, filename, media_type)
    if not json_path.exists():
        print(f"✗ No persona JSON found: {json_path}", file=sys.stderr)
        print(f"  Run: crossing persona detect {query_str or f'--tmdb {args.tmdb}'}", file=sys.stderr)
        sys.exit(1)

    print(f"Building persona mosaics: {filename}")

    try:
        out = persona_mosaic(
            project_path,
            filename,
            media_type=media_type,
            max_per_persona=max_per,
            output_path=output_path,
            layout=layout,
            open_result=open_result,
        )
        n_images = len(list(out.glob("*.png")))
        print(f"✓ Saved {n_images} persona images → {out}/")
        if notify:
            from services.notify import discord_notify
            discord_notify(
                f"✓ Persona mosaics complete: {filename}\n"
                f"  {n_images} images → {out.name}/",
                project_path,
            )
    except ValueError as exc:
        print(f"✗ {exc}", file=sys.stderr)
        sys.exit(1)
    except Exception as exc:
        print(f"✗ Render failed: {exc}", file=sys.stderr)
        import traceback
        traceback.print_exc()
        sys.exit(1)



def _persona_detect(args):
    """Detect recurring anonymous personas in one film or all films."""
    from services.shotlist import resolve_filename
    from services.persona.detect import (
        detect_personas,
        detect_personas_for_all,
        _DEFAULT_FRAMES_PER_SHOT,
        _DEFAULT_MIN_PERSON_CONF,
        _DEFAULT_CLUSTER_THRESHOLD,
        _DEFAULT_MIN_SHOTS,
    )
    from services.persona.io import write_persona_json, get_persona_json_path

    project_path = prefs.get("path")
    media_type = args.media
    force = getattr(args, "force", False)
    do_all = getattr(args, "all", False)
    frames_per_shot = getattr(args, "frames_per_shot", _DEFAULT_FRAMES_PER_SHOT)
    cluster_threshold = getattr(args, "cluster_threshold", _DEFAULT_CLUSTER_THRESHOLD)
    min_confidence = getattr(args, "min_confidence", _DEFAULT_MIN_PERSON_CONF)
    min_shots = getattr(args, "min_shots", _DEFAULT_MIN_SHOTS)
    linkage_method = getattr(args, "linkage", "average")
    include_background = getattr(args, "include_background", False)
    require_face = not getattr(args, "no_face_filter", False)
    verbose = getattr(args, "verbose", False)

    # ── batch mode ───────────────────────────────────────────────────────────
    if do_all:
        import time as _time

        print(f"Running persona detection for all {media_type} with shotlists...")
        print(f"  frames/shot: {frames_per_shot}  |  cluster threshold: {cluster_threshold}  |  linkage: {linkage_method}")
        print(f"  min confidence: {min_confidence}  |  min shots: {min_shots}  |  face filter: {require_face}  |  include background: {include_background}")
        print()

        t0 = _time.time()
        results = detect_personas_for_all(
            project_path,
            media_type=media_type,
            force=force,
            frames_per_shot=frames_per_shot,
            min_person_confidence=min_confidence,
            cluster_threshold=cluster_threshold,
            min_shots_to_be_persona=min_shots,
            linkage=linkage_method,
            include_background=include_background,
            require_face=require_face,
            verbose=verbose,
        )
        elapsed = _time.time() - t0

        ok = [(fn, p) for fn, p, e in results if p is not None]
        skipped = [(fn, e) for fn, p, e in results if p is None and e and "skipped" in e]
        failed  = [(fn, e) for fn, p, e in results if p is None and not (e and "skipped" in e)]

        print()
        for fn, out_path in ok:
            print(f"  ✓  {fn}  →  {out_path.name}")
        for fn, err in skipped:
            print(f"  –  {fn}  ({err})")
        for fn, err in failed:
            print(f"  ✗  {fn}  {err}", file=sys.stderr)

        print(f"\nDone in {elapsed:.1f}s — {len(ok)} processed, {len(skipped)} skipped, {len(failed)} failed.")

        if getattr(args, "notify", False):
            from services.notify import discord_notify
            summary = (
                f"Persona detection batch complete: {len(ok)}/{len(ok)+len(failed)} succeeded "
                f"in {elapsed:.1f}s"
            )
            if failed:
                summary += "\nFailed:\n" + "\n".join(f"  {fn}" for fn, _ in failed)
            discord_notify(summary, project_path)

        if failed:
            sys.exit(1)
        return

    # ── single film mode ─────────────────────────────────────────────────────
    query_words = getattr(args, "query", None) or []
    query_str = " ".join(query_words).strip() if query_words else None

    if args.tmdb is None and not query_str:
        print("✗ Provide a title query or --tmdb <id> (or --all for batch).", file=sys.stderr)
        sys.exit(1)

    try:
        filename = resolve_filename(project_path, args.tmdb, query_str, media_type)
    except ValueError as exc:
        print(f"✗ {exc}", file=sys.stderr)
        sys.exit(1)

    out_path = get_persona_json_path(project_path, filename, media_type)
    if out_path.exists() and not force:
        print(f"✗ Output already exists: {out_path}", file=sys.stderr)
        print("  Pass --force to overwrite.", file=sys.stderr)
        sys.exit(1)

    print(f"Persona detection: {filename}")
    print(f"  frames/shot: {frames_per_shot}  |  cluster threshold: {cluster_threshold}  |  linkage: {linkage_method}")
    print(f"  min confidence: {min_confidence}  |  min shots: {min_shots}  |  face filter: {require_face}  |  include background: {include_background}")
    print()

    import time as _time
    t0 = _time.time()

    try:
        doc = detect_personas(
            project_path,
            filename,
            media_type=media_type,
            frames_per_shot=frames_per_shot,
            min_person_confidence=min_confidence,
            cluster_threshold=cluster_threshold,
            min_shots_to_be_persona=min_shots,
            linkage=linkage_method,
            include_background=include_background,
            require_face=require_face,
            verbose=verbose,
        )
    except FileNotFoundError as exc:
        print(f"✗ {exc}", file=sys.stderr)
        sys.exit(1)
    except RuntimeError as exc:
        print(f"✗ {exc}", file=sys.stderr)
        sys.exit(1)
    except ImportError as exc:
        print(f"✗ Missing dependency: {exc}", file=sys.stderr)
        sys.exit(1)
    except Exception as exc:
        print(f"✗ Unexpected error: {exc}", file=sys.stderr)
        import traceback
        traceback.print_exc()
        sys.exit(1)

    elapsed = _time.time() - t0

    try:
        saved = write_persona_json(doc, project_path, filename, media_type, force=force)
    except FileExistsError as exc:
        print(f"✗ {exc}", file=sys.stderr)
        sys.exit(1)

    n_personas = len(doc.personas)
    print(f"\n✓ {n_personas} persona(s) found in {elapsed:.1f}s")
    print(f"  Saved: {saved}")

    if n_personas and getattr(args, "verbose", False):
        print()
        for p in doc.personas:
            ambig_str = f"  ambiguous with {p.ambiguous_with}" if p.ambiguous_with else ""
            print(
                f"  {p.persona_id}  shots {p.first_shot}–{p.last_shot} "
                f"({p.shot_count} shots)  conf={p.cluster_confidence:.2f}{ambig_str}"
            )

    if getattr(args, "notify", False):
        from services.notify import discord_notify
        discord_notify(
            f"✓ Persona detection complete: {filename}\n"
            f"{n_personas} persona(s) in {elapsed:.1f}s",
            project_path,
        )


def _require_path():
    if not prefs.get("path"):
        print("✗ Error: no project path set. Run: crossing tool path <folder>", file=sys.stderr)
        sys.exit(1)


class _HelpfulParser(argparse.ArgumentParser):
    def error(self, message):
        sys.stderr.write(f"missing: {message}\n")
        self.print_usage(sys.stderr)
        if "the following arguments are required:" in message:
            sys.stderr.write(f"help: {self.prog} -h\n")
        self.exit(2)


def build_parser():
    parser = _HelpfulParser(
        prog="crossing",
        description="Relate moving images across media.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # annotate command: annotate shots or scenes (LLM)
    p_annotate = sub.add_parser("annotate", help="Annotate shots or scenes (LLM)")
    p_annotate.set_defaults(func=_shotlist_annotate)
    annotate_sub = p_annotate.add_subparsers(dest="annotate_type", required=True)

    p_annotate_shot = annotate_sub.add_parser("shot", help="Annotate shot(s)")
    p_annotate_shot.add_argument("filename", nargs="?", default=None, help="Video filename (or use --tmdb)")
    p_annotate_shot.add_argument("index", type=int, nargs="?", default=None, help="Shot index (0-based). Omit to annotate all shots in file.")
    p_annotate_shot.add_argument("caption", nargs="?", default=None, help="Annotation text (manual mode only)")
    p_annotate_shot.add_argument("--tmdb", type=int, default=None, help="TMDb ID")
    p_annotate_shot.add_argument("--media", choices=["movies", "gameplay"], default="movies")
    p_annotate_shot.add_argument("--manual", dest="manual", metavar="TEXT", default=None, help="Manual caption text; when provided operate in manual mode")
    p_annotate_shot.add_argument("--all", action="store_true", help="Annotate all files in metadata (automatic mode by default)")
    p_annotate_shot.add_argument(
        "--model",
        default=prefs.get(_MODEL_KEYS["annotate"], _MODEL_DEFAULTS["annotate"]),
        help=(
            "Model name to use for annotation (default from 'crossing tool model annotate'). "
            "By default this refers to a local folder under <project>/models/<name>. "
            "You may also pass an absolute/relative local path or a valid Hugging Face repo id."
        ),
    )
    p_annotate_shot.add_argument("--prompt-file", default=None, help="System prompt file (system-*.txt under prompts/<media>/shots/)")
    p_annotate_shot.add_argument("--prompt-text", default=None, help="Inline system prompt text (overrides prompt file)")
    p_annotate_shot.add_argument("--user-prompt-file", default=None, dest="user_prompt_file", help="User prompt file (user-*.txt under prompts/<media>/shots/)")
    p_annotate_shot.add_argument("--frames-per-shot", type=int, default=prefs.get(_ANNOTATE_DEFAULT_KEYS["frames-per-shot"][0], _ANNOTATE_DEFAULT_KEYS["frames-per-shot"][1]), help="Frames to sample per shot")
    p_annotate_shot.add_argument("--limit", type=int, default=None, help="Limit to first N shots (process shots with index < N)")
    p_annotate_shot.add_argument("--sample-mode", choices=["center", "start", "end"], default="center", help="Frame sampling mode")
    p_annotate_shot.add_argument("--force", action="store_true", help="Overwrite existing annotations")
    p_annotate_shot.add_argument("--skip-existing", dest="skip_existing", action="store_true", default=True, help="Skip shots that already have annotations (default)")
    p_annotate_shot.add_argument("--no-skip-existing", dest="skip_existing", action="store_false", help="Do not skip shots that already have annotations")
    p_annotate_shot.add_argument("--export-csv", default=None, help="Export annotations CSV path")
    p_annotate_shot.add_argument("--export-md", default=None, help="Export annotations Markdown path")
    p_annotate_shot.add_argument("--verbose", action="store_true", help="Print per-shot progress to stdout")
    p_annotate_shot.add_argument("--log", action="store_true", help="Write a debug log file alongside the annotation JSON")
    p_annotate_shot.add_argument("--notify", action="store_true", help="Send a Discord notification when the run finishes")
    p_annotate_shot.add_argument(
        "--reload-every", type=int, default=25, dest="reload_every_n_shots", metavar="N",
        help="Reload the model pipeline every N processed shots to prevent output drift (default: 25; set 0 to disable)",
    )

    p_annotate_scene = annotate_sub.add_parser("scene", help="Annotate scene(s)")
    p_annotate_scene.add_argument("filename", nargs="?", default=None, help="Video filename (or use --tmdb)")
    p_annotate_scene.add_argument("scene_number", type=int, help="Scene number")
    p_annotate_scene.add_argument("caption", nargs="?", default=None, help="Annotation text (manual mode only)")
    p_annotate_scene.add_argument("--tmdb", type=int, default=None, help="TMDb ID")
    p_annotate_scene.add_argument("--media", choices=["movies", "gameplay"], default="movies")
    p_annotate_scene.add_argument("--manual", dest="manual", metavar="TEXT", default=None, help="Manual caption text; when provided operate in manual mode")
    p_annotate_scene.add_argument(
        "--model",
        default=prefs.get(_MODEL_KEYS["annotate"], _MODEL_DEFAULTS["annotate"]),
        help=(
            "Model name to use for annotation (default from 'crossing model set annotate'). "
            "By default this refers to a local folder under <project>/models/<name>. "
            "You may also pass an absolute/relative local path or a valid Hugging Face repo id."
        ),
    )
    p_annotate_scene.add_argument("--prompt-file", default=None, help="System prompt file (system-*.txt under prompts/<media>/shots/)")
    p_annotate_scene.add_argument("--prompt-text", default=None, help="Inline system prompt text (overrides prompt file)")
    p_annotate_scene.add_argument("--user-prompt-file", default=None, dest="user_prompt_file", help="User prompt file (user-*.txt under prompts/<media>/shots/)")
    p_annotate_scene.add_argument("--frames-per-shot", type=int, default=prefs.get(_ANNOTATE_DEFAULT_KEYS["frames-per-shot"][0], _ANNOTATE_DEFAULT_KEYS["frames-per-shot"][1]), help="Frames to sample per shot")
    p_annotate_scene.add_argument("--limit", type=int, default=None, help="Limit to first N shots (process shots with index < N)")
    p_annotate_scene.add_argument("--sample-mode", choices=["center", "start", "end"], default="center", help="Frame sampling mode")
    p_annotate_scene.add_argument("--force", action="store_true", help="Overwrite existing annotations")
    p_annotate_scene.add_argument("--skip-existing", dest="skip_existing", action="store_true", default=True, help="Skip shots that already have annotations (default)")
    p_annotate_scene.add_argument("--no-skip-existing", dest="skip_existing", action="store_false", help="Do not skip shots that already have annotations")
    p_annotate_scene.add_argument("--export-csv", default=None, help="Export annotations CSV path")
    p_annotate_scene.add_argument("--export-md", default=None, help="Export annotations Markdown path")
    p_annotate_scene.add_argument("--verbose", action="store_true", help="Print per-shot progress to stdout")
    p_annotate_scene.add_argument("--log", action="store_true", help="Write a debug log file alongside the annotation JSON")
    p_annotate_scene.add_argument("--notify", action="store_true", help="Send a Discord notification when the run finishes")
    p_annotate_scene.add_argument(
        "--reload-every", type=int, default=25, dest="reload_every_n_shots", metavar="N",
        help="Reload the model pipeline every N processed shots to prevent output drift (default: 25; set 0 to disable)",
    )

    p_annotate_visualizer = annotate_sub.add_parser("visualizer", help="Review shot annotations in GUI")
    p_annotate_visualizer.set_defaults(func=_annotate_visualizer)
    p_annotate_visualizer.add_argument("query", nargs="?", default=None, help="Filename substring to match")
    p_annotate_visualizer.add_argument("--tmdb", type=int, default=None, help="TMDb ID")
    p_annotate_visualizer.add_argument("--media", choices=["movies", "gameplay"], default="movies")
    p_annotate_visualizer.add_argument("--all", action="store_true", help="Open visualizer for all films with annotation JSON files")

    p_annotate_remove = annotate_sub.add_parser("remove", help="Remove shot annotations for a film")
    p_annotate_remove.set_defaults(func=_annotate_remove)
    p_annotate_remove.add_argument("filename", nargs="?", default=None, help="Video filename (or use --tmdb / --all)")
    p_annotate_remove.add_argument("--tmdb", type=int, default=None, help="TMDb ID")
    p_annotate_remove.add_argument("--media", choices=["movies", "gameplay"], default="movies")
    p_annotate_remove.add_argument("--all", action="store_true", help="Remove annotations for all films in metadata")


    # (moved under 'crossing tool model' — see tool_sub below)

    # audit command
    p_audit = sub.add_parser("audit", help="Report missing metadata, shotlists, and subtitles")
    p_audit.set_defaults(func=cmd_audit)
    p_audit.add_argument("--media", choices=["movies", "gameplay"], default="movies")

    # generate command group — compose, mosaic
    p_generate = sub.add_parser("generate", help="Generate content from project data (compose, mosaic)")
    p_generate.set_defaults(func=cmd_generate)
    generate_sub = p_generate.add_subparsers(dest="generate_subcommand", required=True)

    # generate compose
    p_compose = generate_sub.add_parser(
        "compose",
        help="Compose a poster from shot annotation search results",
    )
    p_compose.set_defaults(func=cmd_compose)
    compose_sub = p_compose.add_subparsers(dest="compose_subcommand", required=True)

    p_compose_search = compose_sub.add_parser(
        "search",
        help="Compose a poster from shots matching a query",
    )
    p_compose_search.add_argument("query", help="Search query (e.g. \"gun\" or \"sunset\")")
    p_compose_search.add_argument("scope", nargs="*", help="Fuzzy movie-title filter(s); omit to search all movies")
    p_compose_search.add_argument("--field", default=None, help="Restrict search to one annotation field")
    p_compose_search.add_argument("--limit", type=int, default=None, help="Max search results to draw from")
    p_compose_search.add_argument("--all", action="store_true", help="Search all movies (overrides positional scopes)")
    p_compose_search.add_argument("--media", choices=["movies", "gameplay"], default="movies")
    p_compose_search.add_argument(
        "--orientation", choices=["portrait", "landscape"], default="portrait",
        help="Canvas orientation: portrait 1240×1754 or landscape 1920×1080 (default: portrait)",
    )
    p_compose_search.add_argument("--seed", type=int, default=None, help="RNG seed for reproducibility")
    p_compose_search.add_argument("--output", default=None, help="Output file path (default: auto-generated)")
    p_compose_search.add_argument("--format", choices=["jpg", "pdf"], default="jpg", dest="format")
    p_compose_search.add_argument("--no-open", action="store_true", dest="no_open", help="Do not open result in desktop viewer")
    p_compose_search.add_argument("--notify", action="store_true", help="Send a Discord notification when done")

    # generate mosaic
    p_mosaic = generate_sub.add_parser(
        "mosaic",
        help="Generate a mosaic grid image from thumbnails or text frames",
    )
    p_mosaic.set_defaults(func=cmd_mosaic)
    mosaic_sub = p_mosaic.add_subparsers(dest="mosaic_subcommand", required=True)

    p_mosaic_thumbnails = mosaic_sub.add_parser(
        "thumbnails",
        help="Mosaic of thumbnails for a media type (e.g. all movies)",
    )
    p_mosaic_thumbnails.add_argument(
        "--media", choices=["movies", "gameplay"], default="movies",
        help="Media type whose thumbnails to collect (default: movies)",
    )
    p_mosaic_thumbnails.add_argument(
        "--all", action="store_true", dest="all",
        help="Include all entries (the only supported mode for now)",
    )
    p_mosaic_thumbnails.add_argument(
        "--output", default=None, metavar="PATH",
        help="Override output file path (default: output/mosaics/<media>-thumbnails-mosaic.png)",
    )
    p_mosaic_thumbnails.add_argument(
        "--layout", choices=["portrait", "landscape"], default="landscape",
        help="Grid orientation: landscape (wider) or portrait (taller) (default: landscape)",
    )
    p_mosaic_thumbnails.add_argument(
        "--caption", choices=["none", "short"], default="short",
        help="Caption style: short (title + year) or none (default: short)",
    )
    p_mosaic_thumbnails.add_argument(
        "--notify", action="store_true",
        help="Send a Discord notification when the run finishes",
    )

    p_mosaic_search = mosaic_sub.add_parser(
        "search",
        help="Mosaic grid of frames matching a shot annotation query",
    )
    p_mosaic_search.add_argument("query", help="Search query (e.g. \"gun\" or \"sunset\")")
    p_mosaic_search.add_argument("scope", nargs="*", help="Fuzzy movie-title filter(s); omit to search all movies")
    p_mosaic_search.add_argument("--movie", nargs="+", default=None, metavar="TITLE", help="Fuzzy movie-title filter(s) (named alternative to positional scope)")
    p_mosaic_search.add_argument("--field", default=None, help="Restrict search to one annotation field")
    p_mosaic_search.add_argument("--limit", type=int, default=None, help="Max search results / mosaic tiles")
    p_mosaic_search.add_argument("--all", action="store_true", help="Search all movies (overrides positional scopes)")
    p_mosaic_search.add_argument("--media", choices=["movies", "gameplay"], default="movies")
    p_mosaic_search.add_argument(
        "--layout", choices=["portrait", "landscape"], default="landscape",
        help="Grid orientation (default: landscape)",
    )
    p_mosaic_search.add_argument(
        "--frame_pct", type=float, default=0.5, metavar="PCT",
        help="Frame position within each shot: 0.0=start  0.5=middle (default)  1.0=end",
    )
    p_mosaic_search.add_argument("--output", default=None, metavar="PATH", help="Override output file path")
    p_mosaic_search.add_argument("--no-open", action="store_true", dest="no_open", help="Do not open result")
    p_mosaic_search.add_argument("--notify", action="store_true", help="Send a Discord notification when done")

    p_mosaic_personas = mosaic_sub.add_parser(
        "personas",
        help="Contact-sheet mosaic of persona appearances for a film",
    )
    p_mosaic_personas.add_argument(
        "query", nargs="*",
        help="Title words to match (e.g. young guns), or omit and use --tmdb",
    )
    p_mosaic_personas.add_argument("--tmdb", type=int, default=None, help="TMDb ID of the film")
    p_mosaic_personas.add_argument("--media", choices=["movies", "gameplay"], default="movies")
    p_mosaic_personas.add_argument(
        "--max-per-persona", type=int, default=6, dest="max_per_persona", metavar="N",
        help="Maximum appearance frames shown per persona row (default: 6)",
    )
    p_mosaic_personas.add_argument(
        "--layout", choices=["portrait", "landscape"], default="landscape",
        help="Grid orientation (default: landscape)",
    )
    p_mosaic_personas.add_argument(
        "--output", default=None, metavar="PATH",
        help="Override output file path",
    )
    p_mosaic_personas.add_argument(
        "--no-open", action="store_true", dest="no_open",
        help="Do not open the result in the desktop viewer after saving",
    )
    p_mosaic_personas.add_argument(
        "--notify", action="store_true",
        help="Send a Discord notification when the run finishes",
    )

    # generate mosaic export
    p_mosaic_export = mosaic_sub.add_parser(
        "export",
        help="Export individual JPEG frames for each search result into a timestamped folder",
    )
    p_mosaic_export.add_argument("query", help="Search query (e.g. \"gun\" or \"sunset\")")
    p_mosaic_export.add_argument("scope", nargs="*", help="Fuzzy movie-title filter(s); omit to search all movies")
    p_mosaic_export.add_argument("--movie", nargs="+", default=None, metavar="TITLE", help="Fuzzy movie-title filter(s)")
    p_mosaic_export.add_argument("--field", default=None, help="Restrict search to one annotation field")
    p_mosaic_export.add_argument("--limit", type=int, default=None, help="Max results to export")
    p_mosaic_export.add_argument("--all", action="store_true", help="Search all movies (overrides positional scopes)")
    p_mosaic_export.add_argument("--media", choices=["movies", "gameplay"], default="movies")
    p_mosaic_export.add_argument(
        "--frame_pct", type=float, default=0.5, metavar="PCT",
        help="Frame position within each shot: 0.0=start  0.5=middle (default)  1.0=end",
    )
    p_mosaic_export.add_argument("--no-open", action="store_true", dest="no_open", help="Do not open result folder")
    p_mosaic_export.add_argument("--notify", action="store_true", help="Send a Discord notification when done")

    # generate mosaic visualizer
    p_mosaic_visualizer = mosaic_sub.add_parser(
        "visualizer",
        help="Launch interactive live mosaic explorer GUI (no arguments needed)",
    )
    p_mosaic_visualizer.set_defaults(func=cmd_mosaic)

    # media command group
    p_media = sub.add_parser(
        "media",
        help="Manage content: videos, subtitles, posters, and thumbnails",
    )
    p_media.set_defaults(func=cmd_media)
    media_sub = p_media.add_subparsers(dest="media_subcommand", required=True)

    # media import
    p_import = media_sub.add_parser("import", help="Import media files into the project")
    p_import.add_argument("sources", nargs="*", metavar="source", help="File(s) or folder to import")
    p_import.add_argument("--pick", action="store_true", help="Open GUI file/folder picker")
    p_import.add_argument("--media", choices=["movie", "gameplay"], default="movie")
    p_import.add_argument("--platform", choices=["universal", "pi5"], default="universal")
    p_import.add_argument("--skip-metadata", action="store_true", help="Skip automatic metadata fetch")
    p_import.set_defaults(func=cmd_import, _parser=p_import)

    # metadata command group
    p_meta = sub.add_parser("metadata", help="Manage media metadata")
    p_meta.set_defaults(func=cmd_metadata)
    meta_sub = p_meta.add_subparsers(dest="metadata_subcommand", required=True)

    p_meta_get = meta_sub.add_parser("get", help="Get metadata (all, by index, or by filename)")
    p_meta_get.add_argument("query", nargs="?", default=None,
                            help="index (int) or title/filename substring")
    p_meta_get.add_argument("--tmdb", type=int, default=None, help="TMDb ID")
    p_meta_get.add_argument("--media", choices=["movies", "gameplay"], default="movies")

    p_meta_set = meta_sub.add_parser("set", help="Set/update metadata from a JSON string")
    p_meta_set.add_argument("json_data", metavar="json")

    p_meta_validate = meta_sub.add_parser("validate", help="Check that all metadata files exist on disk")
    p_meta_validate.add_argument("--media", choices=["movies", "gameplay"], default="movies")
    p_meta_validate.add_argument("--check-thumbnails", action="store_true", help="Also verify thumbnails exist")
    p_meta_validate.add_argument("--check-subtitles", action="store_true", help="Also verify subtitles exist")

    p_meta_update = meta_sub.add_parser("update", help="Fetch and save metadata for entries missing key fields")
    p_meta_update.add_argument("--file", default=None, help="Update a single file by filename")
    p_meta_update.add_argument("--media", choices=["movies", "gameplay"], default="movies")
    p_meta_update.add_argument("--force", action="store_true", help="Force re-fetch metadata for all entries (including duration)")

    p_meta_fixname = meta_sub.add_parser("fixname", help="Normalize filenames and update metadata CSV")
    p_meta_fixname.add_argument("--media", choices=["movies", "gameplay"], default="movies")

    p_meta_count = meta_sub.add_parser("count", help="Print the number of metadata entries")
    p_meta_count.add_argument("--media", choices=["movies", "gameplay"], default="movies")

    p_meta_list = meta_sub.add_parser("list", help="List entries, optionally filtered by year or director")
    p_meta_list.add_argument("--year", default=None, help="Filter by exact year (e.g. 1956)")
    p_meta_list.add_argument("--director", default=None, help="Filter by director name (case-insensitive substring)")
    p_meta_list.add_argument("--fields", default=None, help="Comma-separated fields to include (e.g. title,year,director)")
    p_meta_list.add_argument("--sort", default=None, help="Field to sort by (e.g. year, director, title)")
    p_meta_list.add_argument("--reverse", action="store_true", help="Reverse the sort order (descending)")
    p_meta_list.add_argument("--media", choices=["movies", "gameplay"], default="movies")

    p_meta_prune = meta_sub.add_parser("prune", help="Remove metadata entries with no matching file on disk")
    p_meta_prune.add_argument("--media", choices=["movies", "gameplay"], default="movies")
    p_meta_prune.add_argument("--confirm", action="store_true",
                              help="Actually remove the entries (default is a dry run)")

    # persona command group
    p_persona = sub.add_parser(
        "persona",
        help="Detect and manage recurring anonymous personas (characters) in films",
    )
    p_persona.set_defaults(func=cmd_persona)
    persona_sub = p_persona.add_subparsers(dest="persona_subcommand", required=True)

    p_persona_detect = persona_sub.add_parser(
        "detect",
        help=(
            "Detect recurring anonymous personas in a film by clustering "
            "person re-ID embeddings across shots"
        ),
    )
    p_persona_detect.add_argument(
        "query", nargs="*",
        help="Title words to match (e.g. young guns), or omit and use --tmdb",
    )
    p_persona_detect.add_argument(
        "--tmdb", type=int, default=None,
        help="TMDb ID of the film to process",
    )
    p_persona_detect.add_argument(
        "--all", action="store_true",
        help="Process all films that have a shotlist (skips already-processed films unless --force)",
    )
    p_persona_detect.add_argument(
        "--media", choices=["movies", "gameplay"], default="movies",
        help="Media type (default: movies)",
    )
    p_persona_detect.add_argument(
        "--force", action="store_true",
        help="Overwrite existing persona JSON if it already exists",
    )
    p_persona_detect.add_argument(
        "--frames-per-shot", type=int, default=2, dest="frames_per_shot",
        metavar="N",
        help="Number of frames to sample per shot, 1–3 (default: 2)",
    )
    p_persona_detect.add_argument(
        "--cluster-threshold", type=float, default=0.45, dest="cluster_threshold",
        metavar="T",
        help=(
            "Maximum cosine distance to group detections into one persona. "
            "Lower = stricter (default: 0.45)"
        ),
    )
    p_persona_detect.add_argument(
        "--linkage", choices=["complete", "average"], default="average",
        dest="linkage",
        help=(
            "Hierarchical clustering linkage method.  'average' (default) chains "
            "appearances across lighting/angle changes.  'complete' is stricter "
            "and requires every pair within a cluster to be within threshold."
        ),
    )
    p_persona_detect.add_argument(
        "--include-background", action="store_true", dest="include_background",
        help=(
            "Include background-visibility detections in clustering.  "
            "By default, only foreground and mid-ground persons are used."
        ),
    )
    p_persona_detect.add_argument(
        "--no-face-filter", action="store_true", dest="no_face_filter",
        help=(
            "Disable the face-presence filter.  By default, crops with no "
            "detectable face (backs-of-heads, silhouettes, animals) are "
            "discarded before embedding."
        ),
    )
    p_persona_detect.add_argument(
        "--min-confidence", type=float, default=0.25, dest="min_confidence",
        metavar="C",
        help="Minimum YOLO person detection confidence (default: 0.40)",
    )
    p_persona_detect.add_argument(
        "--min-shots", type=int, default=3, dest="min_shots",
        metavar="N",
        help=(
            "Minimum number of shots a persona must appear in to be recorded "
            "(default: 3)"
        ),
    )
    p_persona_detect.add_argument(
        "--verbose", action="store_true",
        help="Print per-shot progress during detection",
    )
    p_persona_detect.add_argument(
        "--notify", action="store_true",
        help="Send a Discord notification when the run finishes",
    )

    p_persona_get = persona_sub.add_parser(
        "get",
        help="Print the persona JSON for a film",
    )
    p_persona_get.add_argument(
        "query", nargs="*",
        help="Title words to match (e.g. young guns), or omit and use --tmdb",
    )
    p_persona_get.add_argument("--tmdb", type=int, default=None, help="TMDb ID of the film")
    p_persona_get.add_argument("--media", choices=["movies", "gameplay"], default="movies")

    # media remove
    p_remove = media_sub.add_parser("remove", help="Remove a film and all its associated files")
    p_remove.set_defaults(func=cmd_remove)
    p_remove.add_argument("query", nargs="*", help="Filename or title words to match")
    p_remove.add_argument("--tmdb", type=int, default=None, help="TMDb ID (unambiguous)")
    p_remove.add_argument("--media", choices=["movies", "gameplay"], default="movies")
    p_remove.add_argument("--confirm", action="store_true", help="Actually delete (default is a dry run)")

    # search command
    p_search = sub.add_parser("search", help="Search shot annotations")
    p_search.add_argument("query", help="Search string (e.g. \"sunset\" or \"man with gun\")")
    p_search.add_argument("scope", nargs="*", help="Fuzzy movie-title filter(s); omit to search all movies")
    p_search.add_argument("--movie", nargs="+", default=None, metavar="TITLE", help="Fuzzy movie-title filter(s) (named alternative to positional scope)")
    p_search.add_argument("--field", default=None, help="Restrict search to one annotation field (e.g. objects)")
    p_search.add_argument("--limit", type=int, default=None, help="Max results to return overall")
    p_search.add_argument("--limit-per-item", dest="limit_per_item", type=int, default=None, help="Max results per movie")
    p_search.add_argument("--all", action="store_true", help="Search all movies (overrides positional scopes)")
    p_search.add_argument("--show_count", action="store_true", help="(vocabulary mode) include occurrence counts in output")
    p_search.add_argument("--sort", choices=["alphabetical", "count"], default="alphabetical", help="(vocabulary mode) sort order: alphabetical (default) or count")
    p_search.add_argument("--media", choices=["movies", "gameplay"], default="movies")
    p_search.set_defaults(func=cmd_search)

    # shotlist command group
    p_shotlist = sub.add_parser("shotlist", help="Manage shot and scene cuts and annotations")
    p_shotlist.set_defaults(func=cmd_shotlist)
    shotlist_sub = p_shotlist.add_subparsers(dest="shotlist_subcommand", required=True)

    p_shotlist_list = shotlist_sub.add_parser("list", help="List all available shotlists")
    p_shotlist_list.add_argument("--media", choices=["movies", "gameplay"], default=None, help="Filter by media type")
    p_shotlist_list.add_argument("--json", action="store_true", help="Output as JSON")

    p_shotlist_get = shotlist_sub.add_parser("get", help="Get shotlist data for a file")
    p_shotlist_get.add_argument("filename", nargs="?", default=None, help="Video filename (or use --tmdb)")
    p_shotlist_get.add_argument("--tmdb", type=int, default=None, help="TMDb ID")
    p_shotlist_get.add_argument("--media", choices=["movies", "gameplay"], default="movies")
    p_shotlist_get.add_argument("--scene", type=int, default=None, help="Filter by scene number")
    
    p_shotlist_show = shotlist_sub.add_parser("show", help="Show shot or scene data")
    show_sub = p_shotlist_show.add_subparsers(dest="show_type", required=True)
    
    p_show_shot = show_sub.add_parser("shot", help="Show a specific shot")
    p_show_shot.add_argument("filename", nargs="?", default=None, help="Video filename (or use --tmdb)")
    p_show_shot.add_argument("index", type=int, help="Shot index (0-based)")
    p_show_shot.add_argument("--tmdb", type=int, default=None, help="TMDb ID")
    p_show_shot.add_argument("--media", choices=["movies", "gameplay"], default="movies")
    p_show_shot.add_argument("--field", nargs="+", default=None, help="Extract specific fields from caption JSON (e.g. protagonists place actions)")
    p_show_shot.add_argument("--json", action="store_true", help="Output as JSON (raw or filtered by --field)")
    
    p_show_scene = show_sub.add_parser("scene", help="Show all shots in a scene")
    p_show_scene.add_argument("filename", nargs="?", default=None, help="Video filename (or use --tmdb)")
    p_show_scene.add_argument("scene_number", type=int, help="Scene number")
    p_show_scene.add_argument("--tmdb", type=int, default=None, help="TMDb ID")
    p_show_scene.add_argument("--media", choices=["movies", "gameplay"], default="movies")
    p_show_scene.add_argument("--field", nargs="+", default=None, help="Extract specific fields from caption JSON (e.g. protagonists place actions)")
    p_show_scene.add_argument("--json", action="store_true", help="Output as JSON (raw or filtered by --field)")

    p_sl_shot = shotlist_sub.add_parser("shot", help="Shot boundary detection")
    sl_shot_sub = p_sl_shot.add_subparsers(dest="shot_subcommand", required=True)

    p_sl_shot_detect = sl_shot_sub.add_parser("detect", help="Detect shot boundaries using TransNetV2")
    p_sl_shot_detect.add_argument("query", nargs="?", default=None, help="Filename substring to match")
    p_sl_shot_detect.add_argument("--tmdb", type=int, default=None, help="TMDb ID")
    p_sl_shot_detect.add_argument("--media", choices=["movies", "gameplay"], default="movies")
    p_sl_shot_detect.add_argument("--force", action="store_true", help="Overwrite existing shotlist if it exists")
    p_sl_shot_detect.add_argument("--all", action="store_true", help="Process all metadata entries without a shotlist")
    p_sl_shot_detect.add_argument("--notify", action="store_true", help="Send a Discord notification when the process finishes")
    p_sl_shot_detect.add_argument("--notify-items", action="store_true", dest="notify_items", help="Send a Discord notification after each item in a batch")

    p_sl_visualizer = shotlist_sub.add_parser("visualizer", help="Review and correct shot/scene data (GUI)")
    p_sl_visualizer.add_argument("query", nargs="?", default=None, help="Filename substring to match")
    p_sl_visualizer.add_argument("--tmdb", type=int, default=None, help="TMDb ID")
    p_sl_visualizer.add_argument("--all", action="store_true", help="Open visualizer for all movies that have a shotlist")
    p_sl_visualizer.add_argument("--media", choices=["movies", "gameplay"], default="movies")

    p_sl_migrate = shotlist_sub.add_parser(
        "migrate",
        help="Rewrite shotlist CSVs with legacy column names to the canonical naming scheme",
    )
    p_sl_migrate.add_argument(
        "--media", choices=["movies", "gameplay"], default=None,
        help="Limit to one media type (default: both movies and gameplay)",
    )
    p_sl_migrate.add_argument(
        "--dry-run", dest="dry_run", action="store_true",
        help="Report what would change without writing any files",
    )
    # media subtitle
    p_subtitle = media_sub.add_parser("subtitle", help="Download and list subtitles")
    p_subtitle.set_defaults(func=cmd_subtitle)
    subtitle_sub = p_subtitle.add_subparsers(dest="subtitle_subcommand", required=True)

    p_sub_fetch = subtitle_sub.add_parser("fetch", help="Download missing subtitles from OpenSubtitles")
    p_sub_fetch.add_argument("query", nargs="*", default=None, help="Filename or title words (e.g. pals saddle)")
    p_sub_fetch.add_argument("--tmdb", type=int, default=None, help="TMDb ID (unambiguous)")
    p_sub_fetch.add_argument("--all", action="store_true", help="Fetch for all entries without a subtitle")
    p_sub_fetch.add_argument("--force", action="store_true", help="Re-download even if a subtitle already exists")
    p_sub_fetch.add_argument("--media", choices=["movies", "gameplay"], default="movies")

    p_sub_list = subtitle_sub.add_parser("list", help="Show subtitle status for all entries")
    p_sub_list.add_argument("--media", choices=["movies", "gameplay"], default="movies")

    # text command group
    p_text = sub.add_parser("text", help="Extract and manage on-screen text (intertitles, credits, signs)")
    p_text.set_defaults(func=cmd_text)
    text_sub = p_text.add_subparsers(dest="text_subcommand", required=True)

    p_text_detect = text_sub.add_parser("detect", help="Detect on-screen text events for one or all films")
    p_text_detect.add_argument("filename", nargs="?", default=None, help="Video filename (or use --tmdb)")
    p_text_detect.add_argument("--tmdb", type=int, default=None, help="TMDb ID")
    p_text_detect.add_argument("--all", action="store_true", help="Process all films with video files")
    p_text_detect.add_argument("--media", choices=["movies", "gameplay"], default="movies")
    p_text_detect.add_argument("--force", action="store_true", help="Overwrite existing CSV(s)")
    p_text_detect.add_argument("--sample-fps", type=float, default=1.0, dest="sample_fps",
                               help="Frames per second to sample (default: 1.0)")
    p_text_detect.add_argument("--lang", default="en", help="PaddleOCR language code (default: en)")
    p_text_detect.add_argument("--min-confidence", type=float, default=0.75, dest="min_confidence",
                               help="Minimum OCR confidence score to accept (default: 0.75)")
    p_text_detect.add_argument("--verbose", action="store_true", help="Print per-frame OCR output")
    p_text_detect.add_argument("--include-diegetic", action="store_true", default=False, dest="include_diegetic",
                               help="Also capture diegetic text (signs, props, scene text). Default: cards only.")
    p_text_detect.add_argument("--notify", action="store_true", help="Send a Discord notification when the process finishes")
    p_text_detect.add_argument("--notify-items", action="store_true", dest="notify_items", help="Send a Discord notification after each item in a batch")

    p_text_list = text_sub.add_parser("list", help="List all text CSVs")
    p_text_list.add_argument("--media", choices=["movies", "gameplay"], default=None,
                             help="Filter by media type")
    p_text_list.add_argument("--json", action="store_true", help="Output as JSON")

    p_text_visualizer = text_sub.add_parser("visualizer", help="Review and edit text events (GUI)")
    p_text_visualizer.add_argument("query", nargs="?", default=None, help="Filename substring to match")
    p_text_visualizer.add_argument("--tmdb", type=int, default=None, help="TMDb ID")
    p_text_visualizer.add_argument("--all", action="store_true", help="Open visualizer for all films with text CSVs")
    p_text_visualizer.add_argument("--media", choices=["movies", "gameplay"], default="movies")

    p_text_calibrate = text_sub.add_parser("calibrate", help="Sweep confidence thresholds using known ground-truth text")
    p_text_calibrate.add_argument("filename", nargs="?", default=None, help="Video filename substring (or use --tmdb)")
    p_text_calibrate.add_argument("--tmdb", type=int, default=None, help="TMDb ID")
    p_text_calibrate.add_argument("--media", choices=["movies", "gameplay"], default="movies")
    p_text_calibrate.add_argument("--lang", default="en", help="PaddleOCR language code (default: en)")
    p_text_calibrate.add_argument("--expect", nargs="+", required=True, metavar="TEXT",
                                  help="One or more known strings that appear in the first window of the film")
    p_text_calibrate.add_argument("--window", type=float, default=180.0,
                                  help="Seconds from start to analyse (default: 180)")

    # tool command group (version, path, name, api_key)
    p_tool = sub.add_parser("tool", help="Tool settings: version, path, name, models, API keys")
    p_tool.set_defaults(func=cmd_tool)
    tool_sub = p_tool.add_subparsers(dest="tool_subcommand", required=True)

    p_tool_version = tool_sub.add_parser("version", help="Show tool and data structure versions")
    p_tool_version.add_argument("--init", action="store_true", help="Initialize/update data version for current project")

    p_tool_path = tool_sub.add_parser("path", help="Get or set the active project folder")
    p_tool_path.add_argument("folder", nargs="?")

    p_tool_name = tool_sub.add_parser("name", help="Get or set the project name")
    p_tool_name.add_argument("project_name", nargs="?")

    p_tool_api_key = tool_sub.add_parser("api_key", help="Get or set API keys")
    tool_api_key_sub = p_tool_api_key.add_subparsers(dest="api_key_subcommand", required=True)

    p_tool_api_key_get = tool_api_key_sub.add_parser("get", help="Print a stored API key")
    p_tool_api_key_get.add_argument("service", choices=_API_KEY_SERVICES)

    p_tool_api_key_set = tool_api_key_sub.add_parser("set", help="Save an API key")
    p_tool_api_key_set.add_argument("service", choices=_API_KEY_SERVICES)
    p_tool_api_key_set.add_argument("value", metavar="key")

    p_tool_notify = tool_sub.add_parser("notify", help="Send a test notification to verify a service is configured")
    p_tool_notify.add_argument("notify_service", choices=["discord"], metavar="service",
                               help="Notification service to test (discord)")

    p_tool_default = tool_sub.add_parser("default", help="Get or set persistent defaults for annotate and other commands")
    default_sub = p_tool_default.add_subparsers(dest="default_subcommand")

    _default_key_choices = list(_ANNOTATE_DEFAULT_KEYS)

    p_tool_default_set = default_sub.add_parser("set", help="Set a default value")
    p_tool_default_set.add_argument("key", choices=_default_key_choices, help="Setting name")
    p_tool_default_set.add_argument("value", help="New value")

    p_tool_default_get = default_sub.add_parser("get", help="Show a default value (omit key to show all)")
    p_tool_default_get.add_argument("key", nargs="?", choices=_default_key_choices, default=None, help="Setting name")

    p_tool_model = tool_sub.add_parser("model", help="Get or set the model used for each subcommand")
    tool_model_sub = p_tool_model.add_subparsers(dest="model_subcommand")

    p_tool_model_set = tool_model_sub.add_parser("set", help="Set the default model for a role")
    p_tool_model_set.add_argument(
        "role",
        choices=list(_MODEL_KEYS),
        help="Which role to configure: annotate, segmentation, or yolo",
    )
    p_tool_model_set.add_argument("name", help="Model name or path")

    p_tool_model_get = tool_model_sub.add_parser("get", help="Show the configured model(s)")
    p_tool_model_get.add_argument(
        "role",
        nargs="?",
        choices=list(_MODEL_KEYS),
        default=None,
        help="Role to show (omit to show all)",
    )

    p_tool_model_list = tool_model_sub.add_parser(
        "list", help="List all models downloaded to the project"
    )
    _ = p_tool_model_list  # no extra arguments needed

    p_tool_model_download = tool_model_sub.add_parser(
        "download", help="Download a model from Hugging Face into <project>/models/"
    )
    p_tool_model_download.add_argument(
        "repo",
        metavar="repo-or-url",
        help="HF repo-id (owner/model) or huggingface.co URL",
    )
    p_tool_model_download.add_argument(
        "--name",
        default=None,
        metavar="NAME",
        help="Override the local folder name (default: model part of repo-id)",
    )
    p_tool_model_download.add_argument(
        "--all-formats",
        dest="all_formats",
        action="store_true",
        help="Also download legacy weight formats (bin, h5, msgpack, tf, flax)",
    )

    p_tool_model_size = tool_model_sub.add_parser(
        "size",
        help="Estimate model VRAM and check whether it fits on the GPU",
    )
    p_tool_model_size.add_argument(
        "model",
        metavar="name-or-repo",
        help="Local model folder name, HF repo-id (owner/model), or HF URL",
    )

    p_tool_model_remove = tool_model_sub.add_parser(
        "remove", help="Delete a downloaded model from <project>/models/"
    )
    p_tool_model_remove.add_argument(
        "name",
        metavar="name",
        help="Local model folder or file name (as shown by 'crossing tool model list')",
    )
    p_tool_model_remove.add_argument(
        "--confirm",
        action="store_true",
        help="Actually delete the model (default is a dry run)",
    )

    return parser


def main():
    parser = build_parser()
    if len(sys.argv) == 1:
        parser.print_help()
        return
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
