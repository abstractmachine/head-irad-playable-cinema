import sys
import os
import argparse
import json
from pathlib import Path

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
        print("ERROR: tkinter not available. Install python3-tk package.")
        return []
    except Exception as e:
        print(f"ERROR: Failed to open file picker: {e}")
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
            print(f"\n💡 Run 'passage version --init' to initialize data version.")
    else:
        print("\nNo project path set. Use 'passage path <folder>' to set one.")


def cmd_version_init(args):
    """Initialize or update the data version for the current project."""
    _require_path()
    project_path = prefs.get("path")
    _set_data_version(project_path, _TOOL_VERSION)
    print(f"Data version set to {_TOOL_VERSION}")


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
            print("ERROR: No sources provided. Use --pick flag or provide file/folder paths.")
            return
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
    from services.search import search
    result = search(args.query)
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
    query = args.query if hasattr(args, "query") else None
    result = get_metadata(prefs.get("path"), query, media_type=args.media)
    print(json.dumps(result, indent=2))


def _meta_set(args):
    from services.metadata import set_metadata, validate_metadata
    try:
        data = json.loads(args.json_data)
    except json.JSONDecodeError as exc:
        print(f"Error: invalid JSON — {exc}")
        sys.exit(1)
    ok, errors = validate_metadata(data)
    if not ok:
        print("Validation failed:")
        for e in errors:
            print(f"  {e}")
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
        subtitle_dir = Path(project_path) / "media" / "subtitles" / media_type / media_type
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
            print(f"Error: {exc}")
            sys.exit(1)
        except LookupError as exc:
            print(f"Not found: {exc}")
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
    subtitle_dir = Path(project_path) / "media" / "subtitles" / media_type / media_type
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
        subtitle_dir = Path(project_path) / "media" / "subtitles" / media_type / media_type
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
    elif sub == "annotate":
        _shotlist_annotate(args)
    elif sub == "show":
        _shotlist_show(args)


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
        print(f"✗ Unexpected error: {e}", file=sys.stderr)
        sys.exit(1)


def _shotlist_annotate(args):
    from services.shotlist import annotate_shot, annotate_scene, resolve_filename
    project_path = prefs.get("path")
    
    try:
        filename = resolve_filename(project_path, args.tmdb, args.filename, args.media)
        
        if args.annotate_type == "shot":
            annotate_shot(project_path, filename, args.index, args.caption, args.media)
            print(f"✓ Annotated shot {args.index}")
        elif args.annotate_type == "scene":
            annotate_scene(project_path, filename, args.scene_number, args.caption, args.media)
            print(f"✓ Annotated scene {args.scene_number}")
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
        print(f"✗ Unexpected error: {e}", file=sys.stderr)
        sys.exit(1)


def _extract_shot_fields(shot: dict, fields: list) -> dict:
    """Extract specific fields from shot caption JSON and return as dict."""
    result = {
        "Start": shot.get("Start", "N/A"),
        "End": shot.get("End", "N/A"),
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
    print(f"{indent_str}Start: {shot.get('Start', 'N/A')} → End: {shot.get('End', 'N/A')}")
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
        print(f"✗ Unexpected error: {e}", file=sys.stderr)
        sys.exit(1)


# ---------------------------------------------------------------------------
# shot command
# ---------------------------------------------------------------------------

def cmd_shot(args):
    _require_path()
    sub = args.shot_subcommand
    if sub == "detect":
        _shot_detect(args)
    elif sub == "validate":
        _shot_validate(args)


def _shot_detect(args):
    """Detect shot boundaries using TransNetV2."""
    from services.shot_detection import detect_shots_transnet, write_shotlist_csv
    from services.shotlist import resolve_filename, get_shotlist_path
    from services.metadata import get_metadata

    project_path = prefs.get("path")
    media_type = args.media

    try:
        if getattr(args, 'all', False):
            _shot_detect_all(project_path, media_type, args.force)
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
        print(f"✗ Unexpected error: {e}", file=sys.stderr)
        import traceback
        traceback.print_exc()
        sys.exit(1)


def _shot_detect_all(project_path: str, media_type: str, force: bool):
    """Detect shots for all metadata entries that don't yet have a shotlist."""
    from services.shot_detection import detect_shots_transnet, write_shotlist_csv
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
        except Exception as e:
            print(f"  ✗ Failed: {e}", file=sys.stderr)
            failed.append(filename)

    print()
    print(f"Done. {len(pending) - len(failed)}/{len(pending)} processed successfully.")
    if failed:
        print("Failed:")
        for f in failed:
            print(f"  - {f}")


def _shot_validate(args):
    """Launch shot validation GUI."""
    import subprocess
    from pathlib import Path
    
    # Get path to validator (in same directory as cli.py)
    cli_dir = Path(__file__).parent
    
    validator_path = cli_dir / "services" / "shot_validator.py"
    
    if not validator_path.exists():
        print(f"✗ Error: {validator_path.name} not found at {validator_path}", file=sys.stderr)
        sys.exit(1)
    
    # Build command
    cmd = [sys.executable, str(validator_path)]
    
    if args.query:
        cmd.append(args.query)
    if args.tmdb:
        cmd.extend(["--tmdb", str(args.tmdb)])
    if args.media:
        cmd.extend(["--media", args.media])
    
    # Add project path from preferences
    project_path = prefs.get("path")
    if project_path:
        cmd.extend(["--project", project_path])
    
    # Launch validator
    try:
        subprocess.run(cmd, check=True)
    except subprocess.CalledProcessError as e:
        sys.exit(e.returncode)
    except KeyboardInterrupt:
        sys.exit(0)


# ---------------------------------------------------------------------------
# api_key command
# ---------------------------------------------------------------------------

_API_KEY_SERVICES = ("openai", "opensubtitles", "tmdb")


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


def _require_path():
    if not prefs.get("path"):
        print("Error: no project path set. Run: passage path <folder>")
        sys.exit(1)


def build_parser():
    parser = argparse.ArgumentParser(
        prog="passage",
        description="Relate moving images across media.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_version = sub.add_parser("version", help="Show tool and data structure versions")
    p_version.add_argument("--init", action="store_true", help="Initialize/update data version for current project")
    p_version.set_defaults(func=lambda args: cmd_version_init(args) if args.init else cmd_version(args))

    p_path = sub.add_parser("path", help="Set the active project folder")
    p_path.add_argument("folder", nargs="?")
    p_path.set_defaults(func=cmd_path)

    p_name = sub.add_parser("name", help="Set the project name")
    p_name.add_argument("project_name", nargs="?")
    p_name.set_defaults(func=cmd_name)

    p_import = sub.add_parser("import", help="Import media files into the project")
    p_import.add_argument("sources", nargs="*", metavar="source", help="File(s) or folder to import")
    p_import.add_argument("--pick", action="store_true", help="Open GUI file/folder picker")
    p_import.add_argument("--media", choices=["movie", "gameplay"], default="movie")
    p_import.add_argument("--platform", choices=["universal", "pi5"], default="universal")
    p_import.add_argument("--skip-metadata", action="store_true", help="Skip automatic metadata fetch")
    p_import.set_defaults(func=cmd_import)

    p_search = sub.add_parser("search", help="Search for passages")
    p_search.add_argument("query")
    p_search.set_defaults(func=cmd_search)

    # metadata command group
    p_meta = sub.add_parser("metadata", help="Manage media metadata")
    p_meta.set_defaults(func=cmd_metadata)
    meta_sub = p_meta.add_subparsers(dest="metadata_subcommand", required=True)

    p_meta_get = meta_sub.add_parser("get", help="Get metadata (all, by index, or by filename)")
    p_meta_get.add_argument("query", nargs="?", default=None,
                            help="index (int) or filename substring")
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

    # shotlist command group
    p_shotlist = sub.add_parser("shotlist", help="Manage shot and scene annotations")
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

    p_shotlist_annotate = shotlist_sub.add_parser("annotate", help="Add annotation to shot or scene")
    annotate_sub = p_shotlist_annotate.add_subparsers(dest="annotate_type", required=True)
    
    p_annotate_shot = annotate_sub.add_parser("shot", help="Annotate a specific shot")
    p_annotate_shot.add_argument("filename", nargs="?", default=None, help="Video filename (or use --tmdb)")
    p_annotate_shot.add_argument("index", type=int, help="Shot index (0-based)")
    p_annotate_shot.add_argument("caption", help="Annotation text")
    p_annotate_shot.add_argument("--tmdb", type=int, default=None, help="TMDb ID")
    p_annotate_shot.add_argument("--media", choices=["movies", "gameplay"], default="movies")
    
    p_annotate_scene = annotate_sub.add_parser("scene", help="Annotate all shots in a scene")
    p_annotate_scene.add_argument("filename", nargs="?", default=None, help="Video filename (or use --tmdb)")
    p_annotate_scene.add_argument("scene_number", type=int, help="Scene number")
    p_annotate_scene.add_argument("caption", help="Annotation text")
    p_annotate_scene.add_argument("--tmdb", type=int, default=None, help="TMDb ID")
    p_annotate_scene.add_argument("--media", choices=["movies", "gameplay"], default="movies")

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

    # shot command group
    p_shot = sub.add_parser("shot", help="Shot boundary detection")
    p_shot.set_defaults(func=cmd_shot)
    shot_sub = p_shot.add_subparsers(dest="shot_subcommand", required=True)
    
    p_shot_detect = shot_sub.add_parser("detect", help="Detect shot boundaries using TransNetV2")
    p_shot_detect.add_argument("query", nargs="?", default=None, help="Filename substring to match")
    p_shot_detect.add_argument("--tmdb", type=int, default=None, help="TMDb ID")
    p_shot_detect.add_argument("--media", choices=["movies", "gameplay"], default="movies")
    p_shot_detect.add_argument("--force", action="store_true", help="Overwrite existing shotlist if it exists")
    p_shot_detect.add_argument("--all", action="store_true", help="Process all metadata entries without a shotlist")
    
    p_shot_validate = shot_sub.add_parser("validate", help="Validate and correct shot boundaries (GUI)")
    p_shot_validate.add_argument("query", nargs="?", default=None, help="Filename substring to match")
    p_shot_validate.add_argument("--tmdb", type=int, default=None, help="TMDb ID")
    p_shot_validate.add_argument("--media", choices=["movies", "gameplay"], default="movies")

    # audit command
    p_audit = sub.add_parser("audit", help="Report missing metadata, shotlists, and subtitles")
    p_audit.set_defaults(func=cmd_audit)
    p_audit.add_argument("--media", choices=["movies", "gameplay"], default="movies")

    # api_key command group
    p_api_key = sub.add_parser("api_key", help="Get or set API keys")
    p_api_key.set_defaults(func=cmd_api_key)
    api_key_sub = p_api_key.add_subparsers(dest="api_key_subcommand", required=True)

    p_api_key_get = api_key_sub.add_parser("get", help="Print a stored API key")
    p_api_key_get.add_argument("service", choices=_API_KEY_SERVICES)

    p_api_key_set = api_key_sub.add_parser("set", help="Save an API key")
    p_api_key_set.add_argument("service", choices=_API_KEY_SERVICES)
    p_api_key_set.add_argument("value", metavar="key")

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
