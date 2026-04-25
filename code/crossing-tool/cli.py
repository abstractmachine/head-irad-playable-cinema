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
    """Open a native file/folder picker dialog. Returns list of selected paths.

    Tries in order:
      1. PyQt5 QFileDialog  (available when [visualizer] extra is installed)
      2. Platform subprocess — zenity (Linux/GNOME), kdialog (Linux/KDE),
                               osascript (macOS), PowerShell (Windows)
      3. tkinter fallback    (Python built-in on macOS/Windows; needs python3-tk on Linux)
    """
    import platform as _platform

    print("\n📁 Opening file picker...")
    print("   Select files (multi-select supported) or cancel to choose a folder.")

    # ------------------------------------------------------------------
    # 1. PyQt5 (already present if [visualizer] extra is installed)
    # ------------------------------------------------------------------
    try:
        from PyQt5.QtWidgets import QApplication, QFileDialog
        _app = QApplication.instance() or QApplication(sys.argv)
        files, _ = QFileDialog.getOpenFileNames(
            None,
            "Select file(s) to import — cancel to pick a folder instead",
            "",
            "Video files (*.mp4 *.mkv *.avi *.mov);;All files (*.*)",
        )
        if files:
            return list(files)
        folder = QFileDialog.getExistingDirectory(None, "Select folder to import")
        return [folder] if folder else []
    except ImportError:
        pass

    # ------------------------------------------------------------------
    # 2. Platform-native subprocess (no Python packages needed)
    # ------------------------------------------------------------------
    import subprocess as _sp

    _sys = _platform.system()

    if _sys == "Linux":
        # zenity (GNOME) ------------------------------------------------
        try:
            r = _sp.run(
                ["zenity", "--file-selection", "--multiple", "--separator=\n",
                 "--file-filter=Video files|*.mp4 *.mkv *.avi *.mov",
                 "--title=Select file(s) to import — cancel to pick a folder"],
                capture_output=True, text=True, timeout=120,
            )
            if r.returncode == 0 and r.stdout.strip():
                return [p for p in r.stdout.strip().split("\n") if p]
            # User cancelled files picker → try folder
            r2 = _sp.run(
                ["zenity", "--file-selection", "--directory",
                 "--title=Select folder to import"],
                capture_output=True, text=True, timeout=120,
            )
            if r2.returncode == 0 and r2.stdout.strip():
                return [r2.stdout.strip()]
            return []
        except FileNotFoundError:
            pass

        # kdialog (KDE) -------------------------------------------------
        try:
            r = _sp.run(
                ["kdialog", "--getopenfilename", ".",
                 "*.mp4 *.mkv *.avi *.mov|Video files",
                 "--multiple", "--separate-output"],
                capture_output=True, text=True, timeout=120,
            )
            if r.returncode == 0 and r.stdout.strip():
                return [p for p in r.stdout.strip().split("\n") if p]
            r2 = _sp.run(
                ["kdialog", "--getexistingdirectory", "."],
                capture_output=True, text=True, timeout=120,
            )
            if r2.returncode == 0 and r2.stdout.strip():
                return [r2.stdout.strip()]
            return []
        except FileNotFoundError:
            pass

    elif _sys == "Darwin":
        # osascript (built-in on macOS) ---------------------------------
        try:
            r = _sp.run(
                ["osascript", "-e",
                 'set fs to choose file of type {"mp4","mkv","avi","mov"}'
                 ' with prompt "Select file(s) to import"'
                 ' with multiple selections allowed\n'
                 'set out to ""\n'
                 'repeat with f in fs\n'
                 '  set out to out & POSIX path of f & "\n"\n'
                 'end repeat\n'
                 'out'],
                capture_output=True, text=True, timeout=120,
            )
            if r.returncode == 0 and r.stdout.strip():
                return [p for p in r.stdout.strip().split("\n") if p]
            # Folder fallback
            r2 = _sp.run(
                ["osascript", "-e",
                 'set f to choose folder with prompt "Select folder to import"\n'
                 'POSIX path of f'],
                capture_output=True, text=True, timeout=120,
            )
            if r2.returncode == 0 and r2.stdout.strip():
                return [r2.stdout.strip()]
            return []
        except FileNotFoundError:
            pass

    elif _sys == "Windows":
        # PowerShell with Windows.Forms (built-in on Windows) -----------
        try:
            ps_files = (
                'Add-Type -AssemblyName System.Windows.Forms;'
                '$d=New-Object System.Windows.Forms.OpenFileDialog;'
                '$d.Multiselect=$true;'
                '$d.Filter="Video files|*.mp4;*.mkv;*.avi;*.mov|All files|*.*";'
                'if($d.ShowDialog()-eq"OK"){$d.FileNames -join "`n"}'
            )
            r = _sp.run(["powershell", "-Command", ps_files],
                        capture_output=True, text=True, timeout=120)
            if r.returncode == 0 and r.stdout.strip():
                return [p for p in r.stdout.strip().split("\n") if p]
            ps_folder = (
                'Add-Type -AssemblyName System.Windows.Forms;'
                '$d=New-Object System.Windows.Forms.FolderBrowserDialog;'
                'if($d.ShowDialog()-eq"OK"){$d.SelectedPath}'
            )
            r2 = _sp.run(["powershell", "-Command", ps_folder],
                         capture_output=True, text=True, timeout=120)
            if r2.returncode == 0 and r2.stdout.strip():
                return [r2.stdout.strip()]
            return []
        except FileNotFoundError:
            pass

    # ------------------------------------------------------------------
    # 3. tkinter last-resort fallback
    # ------------------------------------------------------------------
    try:
        from tkinter import Tk, filedialog
        root = Tk()
        root.withdraw()
        root.attributes('-topmost', True)
        files = filedialog.askopenfilenames(
            title="Select file(s) to import (or Cancel to pick folder)",
            filetypes=[("Video files", "*.mp4 *.mkv *.avi *.mov"), ("All files", "*.*")]
        )
        root.destroy()
        if files:
            return list(files)
        root = Tk()
        root.withdraw()
        root.attributes('-topmost', True)
        folder = filedialog.askdirectory(title="Select folder to import")
        root.destroy()
        return [folder] if folder else []
    except ImportError:
        pass

    print(
        "✗ No file picker available.\n"
        "  Install the visualizer extra:  uv tool install --reinstall \"crossing[visualizer] @ ...\"\n"
        "  Or pass file paths directly:   crossing media import /path/to/film.mkv",
        file=sys.stderr,
    )
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
    "embed": "model_embed",
}
_MODEL_DEFAULTS = {
    "annotate": "gemma4-e4b",
    "segmentation": "sam2.1_b.pt",
    "embed": "BAAI/bge-small-en-v1.5",
}

# Persistent defaults for annotate (and other commands)
# key = prefs key, value = built-in fallback
_ANNOTATE_DEFAULT_KEYS = {
    "frames-per-shot": ("annotate_frames_per_shot", 3),
    "min-frame-interval": ("annotate_min_frame_interval", 4.0),
    "max-frames-per-shot": ("annotate_max_frames_per_shot", 16),
}

_TOOL_DEFAULT_KEYS = {
    **_ANNOTATE_DEFAULT_KEYS,
    "audio-target-lufs": ("audio_target_lufs", -23.0),
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
    elif sub == "audit":
        _require_path()
        _meta_audit(args)
    elif sub == "update":
        _require_path()
        _meta_update(args)
    elif sub == "normalize":
        _require_path()
        cmd_media_normalize(args)
    elif sub == "channels":
        _require_path()
        cmd_media_channels(args)


def _resolve_normalize_matches(project_path: str, media_type: str, query: str) -> list[dict]:
    from data.metadata import get_metadata

    rows = get_metadata(project_path, media_type=media_type)
    q = query.lower()
    exact_media_id = [
        r for r in rows
        if str(r.get("media_id", "")).lower() == q
    ]
    if exact_media_id:
        return exact_media_id

    return [
        r for r in rows
        if q in str(r.get("filename", "")).lower()
        or q in str(r.get("title", "")).lower()
        or q in str(r.get("original_filename", "")).lower()
    ]


def cmd_media_channels(args):
    from data.metadata import get_metadata, set_metadata
    from services.audio_channels import inspect_audio_channel_count, suggest_audio_channels_mapping
    from services.notify import discord_notify
    from collections import Counter

    project_path = prefs.get("path")

    selector = getattr(args, "target", None)
    type_map = {
        "movie": "movies",
        "movies": "movies",
        "gameplay": "gameplay",
    }
    selected_media_type = type_map.get(selector) if selector else None

    do_all = getattr(args, "all", False)
    count_only = getattr(args, "count", False)
    output_format = getattr(args, "format", "text")
    verbose = getattr(args, "verbose", False)
    notify_each = getattr(args, "notify_items", False)
    notify = getattr(args, "notify", False) or notify_each  # --notify-each implies --notify
    force = getattr(args, "force", False)
    query_words = getattr(args, "query", None) or []
    query = " ".join(query_words).strip()

    # Convenience mode: `crossing media channels --count` defaults to all movies.
    if count_only and not do_all and not selected_media_type and not query:
        do_all = True
        selected_media_type = "movies"

    if not do_all and (not selected_media_type or not query):
        print("✗ Provide either --all, or: crossing media channels {movie|gameplay} <query>", file=sys.stderr)
        sys.exit(1)

    media_types = [selected_media_type] if selected_media_type else ["movies", "gameplay"]

    total = 0
    ok = 0
    skipped = 0
    failed = 0
    distribution: Counter[int] = Counter()

    for media_type in media_types:
        if do_all:
            targets = [r for r in get_metadata(project_path, media_type=media_type) if r.get("filename")]
        else:
            targets = _resolve_normalize_matches(project_path, media_type, query)
            if not targets:
                print(f"✗ No {media_type} entries match '{query}'.", file=sys.stderr)
                sys.exit(1)
            if len(targets) > 1:
                print(f"✗ '{query}' matches {len(targets)} {media_type} entries — be more specific:", file=sys.stderr)
                for row in targets:
                    ident = row.get("media_id") or row.get("tmdb") or "?"
                    print(f"  [{ident}]  {row.get('filename', '')}  —  {row.get('title', '')}", file=sys.stderr)
                sys.exit(1)

        for row in targets:
            filename = row.get("filename", "")
            if not filename:
                skipped += 1
                continue

            if count_only:
                total += 1
                video_path = Path(project_path) / "media" / "videos" / media_type / filename
                if not video_path.exists():
                    if verbose:
                        print(f"  fail  {media_type}/{filename}: file not found", file=sys.stderr)
                    failed += 1
                    continue
                try:
                    channel_count = inspect_audio_channel_count(video_path)
                except RuntimeError as exc:
                    if "No audio stream found" in str(exc):
                        channel_count = 0
                    else:
                        if verbose:
                            print(f"  fail  {media_type}/{filename}: {exc}", file=sys.stderr)
                        failed += 1
                        continue

                distribution[channel_count] += 1
                if verbose:
                    print(f"  ok    {media_type}/{filename}  channels={channel_count}")
                continue

            if row.get("audio_channels") is not None and not force:
                if verbose:
                    print(f"  skip  {media_type}/{filename}: audio_channels already set")
                skipped += 1
                continue

            total += 1
            video_path = Path(project_path) / "media" / "videos" / media_type / filename
            if not video_path.exists():
                if verbose:
                    print(f"  skip  {media_type}/{filename}: file not found")
                skipped += 1
                continue

            try:
                channel_count = inspect_audio_channel_count(video_path)
                mapping = suggest_audio_channels_mapping(channel_count)
                updated = dict(row)
                updated["audio_channels"] = mapping
                updated["media_type"] = media_type
                set_metadata(project_path, updated, match_filename=filename)
                if verbose:
                    print(
                        f"  ok    {media_type}/{filename}  "
                        f"channels={channel_count}  map={json.dumps(mapping, ensure_ascii=False)}"
                    )
                if notify_each:
                    discord_notify(
                        f"✓ Channels {media_type}/{filename} — channels={channel_count}, map={mapping}",
                        project_path,
                    )
                ok += 1
            except RuntimeError as exc:
                if verbose:
                    print(f"  fail  {media_type}/{filename}: {exc}", file=sys.stderr)
                if notify_each:
                    discord_notify(
                        f"✗ Channel scan failed for {media_type}/{filename}: {exc}",
                        project_path,
                    )
                failed += 1

    if count_only:
        if total == 0 and failed == 0:
            print("No eligible media entries found.")
            return

        ordered = {k: distribution[k] for k in sorted(distribution)}
        if output_format == "json":
            print(json.dumps({"total": total, "distribution": ordered}, indent=2))
        elif output_format == "markdown":
            print("| Channels | Movies |")
            print("|---------:|-------:|")
            for channels, count in ordered.items():
                print(f"| {channels} | {count} |")
            print(f"\nTotal scanned: {total}")
        else:
            print("Audio channel distribution:")
            for channels, count in ordered.items():
                print(f"  {channels}: {count}")
            print(f"\nTotal scanned: {total}")

        if failed:
            sys.exit(1)
        return

    if total == 0 and failed == 0:
        print("No eligible media entries found.")
        return

    print(
        f"\nAudio channel scan complete: "
        f"{ok} updated, {skipped} skipped, {failed} failed"
    )

    if notify:
        lines = [
            "Audio channel scan complete",
            f"Updated: {ok}",
            f"Skipped: {skipped}",
            f"Failed: {failed}",
        ]
        discord_notify("\n".join(lines), project_path)

    if failed:
        sys.exit(1)


def cmd_media_normalize(args):
    from data.metadata import get_metadata, set_metadata
    from services.audio_normalize import measure_audio_gain_db
    from services.notify import discord_notify

    project_path = prefs.get("path")
    raw_target_lufs = prefs.get("audio_target_lufs", -23.0)
    try:
        target_lufs = float(raw_target_lufs)
    except (TypeError, ValueError):
        print(
            "✗ Invalid global default for audio target LUFS. "
            "Set it with: crossing tool default set audio-target-lufs -23.0",
            file=sys.stderr,
        )
        sys.exit(1)

    # Accept both singular and plural names from CLI selectors/options.
    selector = getattr(args, "target", None)
    type_map = {
        "movie": "movies",
        "movies": "movies",
        "gameplay": "gameplay",
    }
    selected_media_type = type_map.get(selector) if selector else None

    normalize_all = getattr(args, "all", False)
    verbose = getattr(args, "verbose", False)
    notify_each = getattr(args, "notify_items", False)
    notify = getattr(args, "notify", False) or notify_each  # --notify-each implies --notify
    force = getattr(args, "force", False)
    query_words = getattr(args, "query", None) or []
    query = " ".join(query_words).strip()

    if not normalize_all and (not selected_media_type or not query):
        print("✗ Provide either --all, or: crossing media normalize {movie|gameplay} <query>", file=sys.stderr)
        sys.exit(1)

    media_types = [selected_media_type] if selected_media_type else ["movies", "gameplay"]

    total = 0
    ok = 0
    skipped = 0
    failed = 0

    for media_type in media_types:
        if normalize_all:
            targets = [r for r in get_metadata(project_path, media_type=media_type) if r.get("filename")]
        else:
            targets = _resolve_normalize_matches(project_path, media_type, query)
            if not targets:
                print(f"✗ No {media_type} entries match '{query}'.", file=sys.stderr)
                sys.exit(1)
            if len(targets) > 1:
                print(f"✗ '{query}' matches {len(targets)} {media_type} entries — be more specific:", file=sys.stderr)
                for row in targets:
                    ident = row.get("media_id") or row.get("tmdb") or "?"
                    print(f"  [{ident}]  {row.get('filename', '')}  —  {row.get('title', '')}", file=sys.stderr)
                sys.exit(1)

        for row in targets:
            filename = row.get("filename", "")
            if not filename:
                skipped += 1
                continue

            if row.get("audio_gain_db") is not None and not force:
                if verbose:
                    print(f"  skip  {media_type}/{filename}: audio_gain_db already set")
                skipped += 1
                continue

            total += 1
            video_path = Path(project_path) / "media" / "videos" / media_type / filename
            if not video_path.exists():
                if verbose:
                    print(f"  skip  {media_type}/{filename}: file not found")
                skipped += 1
                continue

            try:
                gain_db, integrated_lufs = measure_audio_gain_db(video_path, target_lufs)
                updated = dict(row)
                updated["audio_gain_db"] = gain_db
                updated["media_type"] = media_type
                set_metadata(project_path, updated, match_filename=filename)
                if verbose:
                    print(
                        f"  ok    {media_type}/{filename}  "
                        f"integrated={integrated_lufs:.2f} LUFS  gain={gain_db:+.3f} dB"
                    )
                if notify_each:
                    discord_notify(
                        f"✓ Normalized {media_type}/{filename} — integrated={integrated_lufs:.2f} LUFS, gain={gain_db:+.3f} dB",
                        project_path,
                    )
                ok += 1
            except RuntimeError as exc:
                if verbose:
                    print(f"  fail  {media_type}/{filename}: {exc}", file=sys.stderr)
                if notify_each:
                    discord_notify(
                        f"✗ Normalize failed for {media_type}/{filename}: {exc}",
                        project_path,
                    )
                failed += 1

    if total == 0 and failed == 0:
        print("No eligible media entries found.")
        return

    print(
        f"\nNormalization complete — target {target_lufs:.1f} LUFS: "
        f"{ok} updated, {skipped} skipped, {failed} failed"
    )

    if notify:
        lines = [
            f"Normalization complete — target {target_lufs:.1f} LUFS",
            f"Updated: {ok}",
            f"Skipped: {skipped}",
            f"Failed: {failed}",
        ]
        discord_notify("\n".join(lines), project_path)

    if failed:
        sys.exit(1)


def cmd_import(args):
    _require_path()

    if not args.media:
        args._parser.error("--media is required: choose 'movie' or 'gameplay'")

    media_type = _MEDIA_FOLDER[args.media]

    if media_type == "gameplay":
        # Gameplay path: assign media_id, copy with readable name, write to gameplay.json
        import re as _re
        from data.metadata import ingest_gameplay

        if not args.game:
            args._parser.error("--game is required for gameplay imports (e.g. --game rdr2)")

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

        project_path = prefs.get("path")
        for src in sources:
            # Default title: filename stem with separators replaced by spaces
            if args.title:
                title = args.title
            else:
                from pathlib import Path as _Path
                stem = _Path(src).stem
                title = _re.sub(r"[-_]+", " ", stem).strip()
            try:
                record = ingest_gameplay(src, project_path, title=title, game=args.game)
                print(f"  \u2713  {record['media_id']}  {record['filename']}")
            except FileNotFoundError as exc:
                print(f"  \u2717  {exc}", file=sys.stderr)
        return

    # Movie path: transcode + TMDB metadata fetch
    from services.import_media import import_files
    from data.metadata import fetch_metadata, fetch_thumbnail, fetch_subtitle, set_metadata

    project_path = prefs.get("path")

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
    if getattr(args, "verbose", False):
        for src in sources:
            from pathlib import Path as _Path
            print(f"  importing  {_Path(src).name} ...", flush=True)
    imported_files = import_files(sources, project_path, dest=media_type, platform=args.optimize)

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
                else:
                    from services.transcode import extract_video_thumbnail
                    from pathlib import Path as _Path
                    video_path = _Path(project_path) / "media" / "videos" / media_type / filename
                    thumb_dir = _Path(project_path) / "media" / "thumbnails" / media_type
                    thumb_path = thumb_dir / (_Path(filename).stem + ".jpg")
                    extract_video_thumbnail(video_path, thumb_path)
                if candidate.get("imdb"):
                    fetch_subtitle(
                        filename,
                        project_path,
                        media_type,
                        candidate["imdb"],
                        candidate.get("title", ""),
                        candidate.get("year")
                    )
                print(f"  \u2713  {filename}")
            except (RuntimeError, LookupError) as exc:
                print(f"  \u2717  {filename}: {exc}")


# ---------------------------------------------------------------------------
# Markdown export helpers
# ---------------------------------------------------------------------------

def _markdown_output_path(project_path: str, stem: str, kind: str) -> Path:
    """Return <project>/data/markdown/<stem> [<kind>].md, creating the dir if needed."""
    out_dir = Path(project_path) / "data" / "markdown"
    out_dir.mkdir(parents=True, exist_ok=True)
    return out_dir / f"{stem} [{kind}].md"


def _resolve_movie_stem(project_path: str, query: str, media_type: str) -> str:
    """Resolve a fuzzy movie query to the actual filename stem. Falls back to the raw query."""
    try:
        from data.metadata import get_metadata
        rows = get_metadata(project_path, query, media_type=media_type)
        if rows:
            filename = rows[0].get("filename", "")
            if filename:
                return Path(filename).stem
    except Exception:
        pass
    return query


def _vocabulary_to_markdown(vocab: dict, title: str) -> str:
    """Render a {field: [{value, count}]} vocabulary dict as Markdown."""
    lines = [f"# {title}", ""]
    for field, items in sorted(vocab.items()):
        lines.append(f"## {field}")
        lines.append("")
        if not items:
            lines.append("_(no values)_")
        else:
            lines.append("| Value | Count |")
            lines.append("|-------|------:|")
            for item in items:
                val = str(item["value"]).replace("|", "\\|")
                lines.append(f"| {val} | {item['count']} |")
        lines.append("")
    return "\n".join(lines)


def _metadata_to_markdown(entries: list, title: str) -> str:
    """Render a list of metadata dicts as a Markdown document."""
    lines = [f"# {title}", ""]
    for entry in entries:
        name = entry.get("title") or entry.get("filename", "Unknown")
        year = entry.get("year", "")
        lines.append(f"## {name}{f' ({year})' if year else ''}")
        lines.append("")
        field_order = ["title", "year", "director", "duration", "tmdb", "imdb",
                       "tagline", "overview", "shotlist", "encodings", "filename"]
        shown = set()
        for key in field_order:
            if key in entry:
                val = entry[key]
                if val not in (None, "", "null"):
                    lines.append(f"**{key}**: {val}  ")
                shown.add(key)
        for key, val in entry.items():
            if key not in shown and val not in (None, "", "null"):
                lines.append(f"**{key}**: {val}  ")
        lines.append("")
    return "\n".join(lines)


def cmd_search(args):
    _require_path()

    # Dispatch: `crossing search vocabulary <field> [scope...]`
    if args.query == "vocabulary":
        from services.search import vocabulary_from_field
        remaining = args.scope or []
        all_fields = getattr(args, "all_fields", False)
        # For --all-fields there is no field name at remaining[0]; scopes start at [0].
        # For a named field, remaining[0] is the field and scopes start at [1].
        if all_fields:
            scopes = list(remaining) + (getattr(args, "movie", None) or [])
        else:
            scopes = (remaining[1:] or []) + (getattr(args, "movie", None) or [])
        use_all = getattr(args, "all", False)
        show_count = getattr(args, "show_count", False)
        sort = getattr(args, "sort", "alphabetical")
        media_type = getattr(args, "media", "movies")
        project_path = prefs.get("path")

        exclude_fields = set(getattr(args, "exclude", None) or [])

        if all_fields:
            # Discover every annotation field used across all files, then
            # emit a single JSON object {field: [{value, count}]} sorted by count.
            import re as _re
            ann_base = Path(project_path) / "data" / "annotations" / "shots" / media_type
            fields_seen: set[str] = set()
            for ann_file in ann_base.glob("*.json"):
                try:
                    entries = json.loads(ann_file.read_text(encoding="utf-8"))
                except Exception:
                    continue
                for entry in entries:
                    if not isinstance(entry, dict):
                        continue
                    ann = entry.get("shot", {}).get("annotation") if isinstance(entry.get("shot"), dict) else None
                    if isinstance(ann, dict):
                        fields_seen.update(ann.keys())
            if not fields_seen:
                print(json.dumps({}))
                return
            scopes_arg = scopes or None
            output: dict = {}
            for f in sorted(fields_seen):
                if f in exclude_fields:
                    continue
                output[f] = vocabulary_from_field(
                    field=f,
                    scopes=scopes_arg,
                    use_all=use_all,
                    show_count=True,
                    project_path=project_path,
                    media_type=media_type,
                    sort="count",
                )
            if getattr(args, "markdown", False) or getattr(args, "open", False):
                scope_stem = _resolve_movie_stem(project_path, scopes[0], media_type) if scopes else f"{media_type}-all"
                md_path = _markdown_output_path(project_path, scope_stem, "vocabulary-all")
                title = f"Vocabulary (all fields) — {scopes[0] if scopes else media_type + ' (all)'}"
                md_path.write_text(_vocabulary_to_markdown(output, title), encoding="utf-8")
                print(f"✓ Saved: {md_path}")
                if getattr(args, "open", False):
                    import subprocess; subprocess.Popen(["xdg-open", str(md_path)])
            else:
                print(json.dumps(output, indent=2))
            return

        if not remaining:
            print("error: 'vocabulary' requires a field name or --all-fields, e.g. crossing search vocabulary objects", file=sys.stderr)
            sys.exit(1)
        field = remaining[0]
        if field in exclude_fields:
            return
        scopes = scopes or None
        result = vocabulary_from_field(
            field=field,
            scopes=scopes,
            use_all=use_all,
            show_count=True,
            project_path=project_path,
            media_type=media_type,
            sort=sort,
        )
        if getattr(args, "markdown", False) or getattr(args, "open", False):
            scope_stem = _resolve_movie_stem(project_path, scopes[0], media_type) if scopes else f"{media_type}-all"
            md_path = _markdown_output_path(project_path, scope_stem, f"vocabulary-{field}")
            title = f"Vocabulary ({field}) — {scopes[0] if scopes else media_type + ' (all)'}"
            md_path.write_text(_vocabulary_to_markdown({field: result}, title), encoding="utf-8")
            print(f"✓ Saved: {md_path}")
            if getattr(args, "open", False):
                import subprocess; subprocess.Popen(["xdg-open", str(md_path)])
        else:
            print(json.dumps(result, indent=2))
        return

    # Dispatch: `crossing search text <query> [scope...]`
    # Searches the annotation `text` field — the canonical semantic text layer
    # produced by `crossing annotate`. Downstream spatial localization (bbox)
    # will build on these results but is not implemented here.
    if args.query == "text":
        from services.search import search_shots
        remaining = args.scope or []
        if not remaining:
            print("error: 'search text' requires a query, e.g. crossing search text \"WANTED\"", file=sys.stderr)
            sys.exit(1)
        text_query = remaining[0]
        scopes = (remaining[1:] or []) + (getattr(args, "movie", None) or [])
        scopes = scopes or None
        use_all = getattr(args, "all", False)
        limit = getattr(args, "limit", None)
        limit_per_item = getattr(args, "limit_per_item", None)
        media_type = getattr(args, "media", "movies")
        result = search_shots(
            query=text_query,
            scopes=scopes,
            field="text",
            limit=limit,
            limit_per_item=limit_per_item,
            use_all=use_all,
            project_path=prefs.get("path"),
            media_type=media_type,
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
    elif sub == "update":
        _meta_update(args)
    elif sub == "count":
        _meta_count(args)
    elif sub == "list":
        _meta_list(args)
    elif sub == "prune":
        _meta_prune(args)
    elif sub == "audit":
        _meta_audit(args)


def _meta_get(args):
    from data.metadata import get_metadata
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

    if getattr(args, "markdown", False) or getattr(args, "open", False):
        if query:
            stem = _resolve_movie_stem(project_path, query, media_type)
        elif tmdb:
            stem = _resolve_movie_stem(project_path, None, media_type) if result else f"tmdb-{tmdb}"
            # use the resolved filename from the result itself
            stem = Path(result[0]["filename"]).stem if result and result[0].get("filename") else f"tmdb-{tmdb}"
        else:
            stem = f"{media_type}-all"
        title_label = query or (f"TMDb {tmdb}" if tmdb else f"{media_type} (all)")
        md_path = _markdown_output_path(project_path, stem, "metadata")
        md_path.write_text(_metadata_to_markdown(result, f"Metadata — {title_label}"), encoding="utf-8")
        print(f"✓ Saved: {md_path}")
        if getattr(args, "open", False):
            import subprocess; subprocess.Popen(["xdg-open", str(md_path)])
    else:
        print(json.dumps(result, indent=2))


def _meta_set(args):
    from data.metadata import set_metadata, validate_metadata
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


def _meta_update(args):
    from data.metadata import fetch_metadata, fetch_thumbnail, fetch_subtitle, set_metadata, get_metadata, load_json_metadata
    project_path = prefs.get("path")
    media_type = getattr(args, "media", "movies")
    force = getattr(args, "force", False)
    single_file = getattr(args, "file", None)

    def _ensure_thumbnail(filename, candidate):
        """Fetch TMDB thumbnail or extract a frame from the video if missing."""
        from services.transcode import extract_video_thumbnail
        thumb_dir = Path(project_path) / "media" / "thumbnails" / media_type
        thumb_path = thumb_dir / (Path(filename).stem + ".jpg")
        old_thumb  = thumb_dir / (Path(filename).stem.replace(" ", "-") + ".jpg")
        if thumb_path.exists() or old_thumb.exists():
            return  # already present
        if candidate.get("tmdb"):
            fetch_thumbnail(filename, project_path, media_type, candidate["tmdb"])
        else:
            video_path = Path(project_path) / "media" / "videos" / media_type / filename
            extract_video_thumbnail(video_path, thumb_path)

    # ------------------------------------------------------------------ #
    # Gameplay: no TMDb lookup — only ensure thumbnails exist             #
    # ------------------------------------------------------------------ #
    if media_type == "gameplay":
        # Collect all known gameplay filenames from JSON metadata + disk
        json_records = load_json_metadata(project_path, "gameplay")
        known = {r["filename"] for r in json_records if r.get("filename")}
        media_dir = Path(project_path) / "media" / "videos" / "gameplay"
        disk_files = sorted(f.name for f in media_dir.iterdir() if f.is_file()) if media_dir.is_dir() else []
        targets = list(known | set(disk_files)) if force else list(known | set(disk_files))

        if single_file:
            targets = [single_file]

        ok_count = 0
        for filename in sorted(targets):
            # Build a minimal candidate just to satisfy _ensure_thumbnail signature
            rec = next((r for r in json_records if r.get("filename") == filename), {})
            _ensure_thumbnail(filename, rec)
            print(f"  ok  {filename}")
            ok_count += 1
        print(f"Done. {ok_count} gameplay file(s) checked.")
        return

    # ------------------------------------------------------------------ #
    # Movies (and other TMDb-backed types)                                #
    # ------------------------------------------------------------------ #
    # Single file update
    if single_file:
        try:
            candidate = fetch_metadata(single_file, project_path)
            candidate["media_type"] = media_type
            set_metadata(project_path, candidate, match_filename=single_file)
            _ensure_thumbnail(single_file, candidate)
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
            print(f"\u2717 Error: {exc}", file=sys.stderr)
            sys.exit(1)
        except LookupError as exc:
            print(f"\u2717 Error: {exc}", file=sys.stderr)
            sys.exit(1)
        return

    # Bulk update
    REQUIRED = {"title", "year", "director", "overview"}
    rows = get_metadata(project_path, media_type=media_type)
    known = {r["filename"] for r in rows if r.get("filename")}

    if force:
        missing = [r for r in rows if r.get("filename")]
    else:
        incomplete = [r for r in rows if any(not r.get(f) for f in REQUIRED)]
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
            _ensure_thumbnail(filename, candidate)
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
    from data.metadata import get_metadata
    project_path = prefs.get("path")
    media_type = getattr(args, "media", "movies")
    rows = get_metadata(project_path, media_type=media_type)
    print(f"{len(rows)} {media_type}")


def _meta_list(args):
    from data.metadata import get_metadata
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
    from data.metadata import get_metadata, prune_metadata
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


def _meta_audit(args):
    """Report missing metadata, shotlists, subtitles, and thumbnails."""
    from data.metadata import get_metadata

    project_path = prefs.get("path")
    media_type = getattr(args, "media", "movies")

    video_dir     = Path(project_path) / "media" / "videos"     / media_type
    subtitle_dir  = Path(project_path) / "media" / "subtitles"  / media_type
    shotlist_dir  = Path(project_path) / "data"  / "shotlists"  / media_type
    thumbnail_dir = Path(project_path) / "media" / "thumbnails" / media_type

    video_files    = sorted(f.name for f in video_dir.glob("*") if f.is_file()) if video_dir.exists() else []
    entries        = get_metadata(project_path, media_type=media_type)
    meta_filenames = {e["filename"] for e in entries if e.get("filename")}

    no_metadata = [f for f in video_files if f not in meta_filenames]

    no_shotlist = []
    for entry in entries:
        fn = entry.get("filename", "")
        if fn and not (shotlist_dir / (Path(fn).stem + ".csv")).exists():
            no_shotlist.append(fn)

    no_subtitle = []
    for entry in entries:
        fn = entry.get("filename", "")
        if not fn:
            continue
        stem = Path(fn).stem
        if not (subtitle_dir / (stem + ".srt")).exists() and \
           not (subtitle_dir / (stem.replace(" ", "-") + ".srt")).exists():
            no_subtitle.append(fn)

    no_thumbnail = []
    for entry in entries:
        fn = entry.get("filename", "")
        if not fn:
            continue
        stem = Path(fn).stem
        if not (thumbnail_dir / (stem + ".jpg")).exists() and \
           not (thumbnail_dir / (stem.replace(" ", "-") + ".jpg")).exists():
            no_thumbnail.append(fn)

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
    if no_thumbnail:
        print(f"  No thumbnail  ({len(no_thumbnail)} / {n}):")
        for f in no_thumbnail:
            print(f"    {f}")
    else:
        print(f"  Thumbnails: ✓ all {n}")

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
# remove command
# ---------------------------------------------------------------------------

def cmd_remove(args):
    _require_path()
    from data.metadata import get_metadata, load_json_metadata, save_json_metadata
    from data.shotlist import resolve_filename

    project_path = prefs.get("path")

    if not args.media:
        print("\u2717 --media is required: choose 'movies' or 'gameplay'", file=sys.stderr)
        sys.exit(1)

    media_type = args.media
    tmdb = getattr(args, "tmdb", None)
    query = " ".join(args.query).strip() if args.query else ""

    if not tmdb and not query:
        print("✗ Provide a title query or --tmdb <id>.", file=sys.stderr)
        sys.exit(1)

    all_rows = get_metadata(project_path, media_type=media_type)

    if tmdb is not None:
        try:
            filename = resolve_filename(project_path, tmdb, None, media_type)
        except ValueError as exc:
            print(f"✗ {exc}", file=sys.stderr)
            sys.exit(1)
        matches = [r for r in all_rows if r.get("filename") == filename]
    else:
        q = query.lower()
        matches = [
            r for r in all_rows
            if q in str(r.get("filename", "")).lower()
            or q in str(r.get("title", "")).lower()
            or q in str(r.get("original_filename", "")).lower()
        ]

    if not matches:
        hint = f"--tmdb {tmdb}" if tmdb else f"'{query}'"
        print(f"✗ No metadata entry matches {hint}.", file=sys.stderr)
        sys.exit(1)

    if len(matches) > 1:
        print(f"✗ '{query}' matches {len(matches)} entries — be more specific or use --tmdb:", file=sys.stderr)
        for r in matches:
            mid = r.get('tmdb') or r.get('media_id', '?')
            print(f"  [{mid}]  {r.get('filename', '')}  —  {r.get('title', '')} ({r.get('year', '')})", file=sys.stderr)
        sys.exit(1)

    row = matches[0]
    filename = row.get("filename", "")
    stem = Path(filename).stem
    media_id = row.get("media_id", "")

    video_path     = Path(project_path) / "media" / "videos"     / media_type / filename
    thumbnail_path = Path(project_path) / "media" / "thumbnails" / media_type / (stem + ".jpg")
    subtitle_path  = Path(project_path) / "media" / "subtitles"  / media_type / (stem + ".srt")
    shotlist_path  = Path(project_path) / "data"  / "shotlists"  / media_type / (stem + ".csv")
    npy_path       = Path(project_path) / "data"  / "shotlists"  / media_type / (stem + ".npy")

    candidates = [
        ("video",      video_path),
        ("thumbnail",  thumbnail_path),
        ("subtitle",   subtitle_path),
        ("shotlist",   shotlist_path),
        ("embeddings", npy_path),
    ]
    present = [(label, p) for label, p in candidates if p.exists()]

    print(f"Will remove: {row.get('title', filename)} ({row.get('year', '?')})")
    print(f"  metadata record in {media_type}.json  (media_id: {media_id or 'n/a'})")
    for label, p in present:
        print(f"  {label}: {p.relative_to(project_path)}")
    for label, _ in [(l, p) for l, p in candidates if not p.exists()]:
        print(f"  {label}: (not present)")

    if not args.confirm:
        print(f"\nDry run. Pass --confirm to delete.")
        return

    # Delete files
    for _, p in present:
        p.unlink()

    # Remove from JSON
    updated = [
        r for r in all_rows
        if not (
            r.get("filename") == filename
            or (media_id and r.get("media_id") == media_id)
        )
    ]
    save_json_metadata(project_path, media_type, updated)

    removed_files = len(present)
    print(f"\nRemoved: metadata + {removed_files} file(s).")


# ---------------------------------------------------------------------------
# shotlist command
# ---------------------------------------------------------------------------

def cmd_shotlist(args):
    _require_path()
    if getattr(args, "visualizer", False):
        args.all = True
        args.query = None
        args.tmdb = None
        _shot_visualizer(args)
        return
    sub = args.shotlist_subcommand
    if sub is None:
        print("✗ shotlist: specify a subcommand or use --visualizer.", file=sys.stderr)
        sys.exit(1)
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
    elif sub == "migrate":
        _shotlist_migrate(args)


def _shotlist_migrate(args):
    """Rewrite all shotlist CSVs that still use legacy temporal field names."""
    from data.shotlist import migrate_shotlist_fields
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
    from data.shotlist import list_shotlists
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
    from data.shotlist import read_shotlist, resolve_filename
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
    if getattr(args, "annotate_type", None) is None:
        print("✗ annotate: specify a subcommand.", file=sys.stderr)
        sys.exit(1)
    if args.annotate_type == "audit":
        _annotate_audit(args)
        return
    if args.annotate_type == "validate":
        _annotate_validate(args)
        return
    from data.shotlist import annotate_shot, annotate_scene, resolve_filename
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
            from data.annotate import annotate_file_shots, annotate_all_files

            if getattr(args, "all", False):
                _notify_items = getattr(args, "notify_items", False)
                if _notify_items:
                    args.notify = True  # --notify-each implies --notify

                def _on_file_done(summary, elapsed):
                    if not _notify_items:
                        return
                    from services.notify import discord_notify
                    failed_count = len(summary.get("failed", [])) if summary.get("failed") else 0
                    if summary.get("updated", 0) == 0 and failed_count == 0:
                        return  # nothing changed — skip notification
                    h, rem = divmod(int(elapsed), 3600)
                    m, s = divmod(rem, 60)
                    elapsed_str = f"{h}h{m:02d}m{s:02d}s" if h else (f"{m}m{s:02d}s" if m else f"{s}s")
                    msg = (
                        f"\u2713 Annotated: {summary.get('filename')}\n"
                        f"updated={summary.get('updated')} "
                        f"skipped={summary.get('skipped')} "
                        f"failed={failed_count} "
                        f"({elapsed_str})"
                    )
                    discord_notify(msg, project_path)

                results = annotate_all_files(
                    project_path,
                    media_type=args.media,
                    model_name=args.model,
                    prompt_file=args.prompt_file,
                    prompt_text=args.prompt_text,
                    user_prompt_file=getattr(args, "user_prompt_file", None),
                    frames_per_shot=args.frames_per_shot,
                    min_frame_interval_s=getattr(args, "min_frame_interval", 4.0),
                    max_frames_per_shot=getattr(args, "max_frames_per_shot", 16),
                    sample_mode=args.sample_mode,
                    force=args.force,
                    skip_existing=args.skip_existing,
                    limit=getattr(args, "limit", None),
                    verbose=getattr(args, "verbose", False),
                    write_log=getattr(args, "log", False),
                    reload_every_n_shots=getattr(args, "reload_every_n_shots", 25),
                    on_file_done=_on_file_done,
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
                min_frame_interval_s=getattr(args, "min_frame_interval", 4.0),
                max_frames_per_shot=getattr(args, "max_frames_per_shot", 16),
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
            if getattr(args, "notify", False) and (summary.get("updated", 0) > 0 or failed_count > 0):
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
            from data.annotate import annotate_file_shots

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
                min_frame_interval_s=getattr(args, "min_frame_interval", 4.0),
                max_frames_per_shot=getattr(args, "max_frames_per_shot", 16),
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

    except ImportError as e:
        print(f"✗ {e}", file=sys.stderr)
        print("  Annotation requires the 'annotate' extra.", file=sys.stderr)
        print("  Installed as a tool? Re-install with the extra:", file=sys.stderr)
        print("    uv tool install --reinstall \"crossing[annotate] @ git+https://github.com/abstractmachine/head-irad-playable-cinema.git#subdirectory=code/crossing-tool\"", file=sys.stderr)
        print("  Working from source? Sync the extra into your dev environment:", file=sys.stderr)
        print("    uv sync --extra annotate", file=sys.stderr)
        sys.exit(1)
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
    from data.shotlist import get_shot, get_scene_shots, resolve_filename
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
    from data.shot_detection import detect_shots_transnet, write_shotlist_csv
    from data.shotlist import resolve_filename, get_shotlist_path
    from data.metadata import get_metadata

    project_path = prefs.get("path")
    media_type = args.media
    notify_items = getattr(args, "notify_items", False)
    notify = getattr(args, "notify", False) or notify_items  # --notify-each implies --notify

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
    from data.shot_detection import detect_shots_transnet, write_shotlist_csv
    from data.shotlist import get_shotlist_path
    from data.metadata import get_metadata
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


def _require_visualizer_deps():
    """Exit with a helpful message if the visualizer optional deps are missing."""
    import importlib.util
    missing = [pkg for pkg in ("PyQt5", "cv2") if importlib.util.find_spec(pkg) is None]
    if missing:
        print(
            "✗ Visualizer dependencies not installed (PyQt5, opencv).",
            file=sys.stderr,
        )
        print(
            "  Installed as a tool? Re-install with the extra:",
            file=sys.stderr,
        )
        print(
            "    uv tool install --reinstall \"crossing[visualizer] @ git+https://github.com/abstractmachine/head-irad-playable-cinema.git#subdirectory=code/crossing-tool\"",
            file=sys.stderr,
        )
        print(
            "  Working from source? Sync the extra into your dev environment:",
            file=sys.stderr,
        )
        print(
            "    uv sync --extra visualizer",
            file=sys.stderr,
        )
        sys.exit(1)


def _shot_visualizer(args):
    """Launch shot visualizer GUI."""
    import subprocess
    from pathlib import Path
    from data.shotlist import get_shotlist_path
    from data.metadata import get_metadata

    _require_visualizer_deps()

    cli_dir = Path(__file__).parent
    visualizer_path = cli_dir / "visualizers" / "shot_visualizer.py"

    if not visualizer_path.exists():
        print(f"✗ Error: {visualizer_path.name} not found at {visualizer_path}", file=sys.stderr)
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
        sys.executable, str(visualizer_path),
        "--media", media_type,
        "--project", project_path,
        "--filenames",
    ] + filenames

    if getattr(args, "verbose", False):
        cmd.append("--verbose")

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
        if key == "fields":
            project_path = prefs.get("path")
            if not project_path:
                print("✗ No project path set. Run: crossing tool path <path>", file=sys.stderr)
                sys.exit(1)
            from data.index import save_fields
            field_list = [f.strip() for f in args.value.split(",") if f.strip()]
            save_fields(project_path, field_list)
            print(f"✓ Display fields set to: {', '.join(field_list)}")
        elif key not in _TOOL_DEFAULT_KEYS:
            print(f"✗ Unknown default key '{key}'. Available keys: fields, {', '.join(_TOOL_DEFAULT_KEYS)}", file=sys.stderr)
            sys.exit(1)
        else:
            pref_key, fallback = _TOOL_DEFAULT_KEYS[key]
            value = args.value
            if isinstance(fallback, float):
                try:
                    value = float(args.value)
                except ValueError:
                    print(f"✗ Invalid value for '{key}': expected a number.", file=sys.stderr)
                    sys.exit(1)
            elif isinstance(fallback, int):
                try:
                    value = int(args.value)
                except ValueError:
                    print(f"✗ Invalid value for '{key}': expected an integer.", file=sys.stderr)
                    sys.exit(1)
            prefs.set(pref_key, value)
            print(f"✓ Default '{key}' set to '{value}'")
    elif sub == "get" or sub is None:
        key = getattr(args, "key", None)
        if key == "fields":
            project_path = prefs.get("path")
            if not project_path:
                print("fields: (no project path set)", file=sys.stderr)
                sys.exit(1)
            try:
                from data.index import load_fields
                fields = load_fields(project_path)
                print(f"fields: {', '.join(fields)}")
            except FileNotFoundError:
                print("fields: (no fields.yaml found in project)")
            except (ValueError, ImportError) as exc:
                print(f"fields: (error loading — {exc})")
        else:
            keys = [key] if key else list(_TOOL_DEFAULT_KEYS)
            for k in keys:
                if k not in _TOOL_DEFAULT_KEYS:
                    print(f"✗ Unknown default key '{k}'. Available keys: fields, {', '.join(_TOOL_DEFAULT_KEYS)}", file=sys.stderr)
                    sys.exit(1)
                pref_key, fallback = _TOOL_DEFAULT_KEYS[k]
                val = prefs.get(pref_key, fallback)
                print(f"{k}: {val}")
            if not key:
                # Show fields when displaying all defaults
                project_path = prefs.get("path")
                if project_path:
                    try:
                        from data.index import load_fields
                        fields = load_fields(project_path)
                        print(f"fields: {', '.join(fields)}")
                    except FileNotFoundError:
                        print("fields: (no fields.yaml found in project)")
                    except (ValueError, ImportError) as exc:
                        print(f"fields: (error loading — {exc})")
                # Show the index mapping when displaying all defaults
                if project_path:
                    try:
                        from data.index import load_mapping
                        mapping = load_mapping(project_path)
                        print()
                        print("mapping:")
                        print(f"  fields:         {', '.join(mapping.get('fields', []))}")
                        print(f"  include_labels: {mapping.get('include_labels', True)}")
                        print(f"  separator:      {mapping.get('separator', ' | ')!r}")
                        print(f"  skip_empty:     {mapping.get('skip_empty', True)}")
                    except FileNotFoundError:
                        print("\nmapping: (no mapping.yaml found in project)")
                    except (ValueError, ImportError) as exc:
                        print(f"\nmapping: (error loading — {exc})")


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
    from data.metadata import fetch_subtitle, get_metadata
    from data.shotlist import resolve_filename

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
    from data.metadata import get_metadata

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
# (text command family removed — text retrieval now lives under
#  `crossing search text <query>`; see cmd_search)
# ---------------------------------------------------------------------------


def _annotate_remove(args):
    """Remove shot-annotation JSON for one or all films."""
    from data.annotate import remove_file_annotations
    from data.shotlist import resolve_filename

    _require_path()
    project_path = prefs.get("path")
    media_type = getattr(args, "media", "movies")

    if getattr(args, "all", False):
        from data.metadata import get_metadata
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


def _annotate_migrate(args):
    """Migrate annotation JSON files from legacy integer shot_ids to stable IDs.

    The stable format is ``<media_id>@fSTART-fEND`` (e.g.
    ``tmdb_39435@f000234-f000398``).  Files that already use stable IDs are
    left untouched.
    """
    from data.annotate import migrate_annotations_to_stable_ids
    from data.shotlist import resolve_filename

    _require_path()
    project_path = prefs.get("path")
    media_type   = getattr(args, "media", "movies")

    if getattr(args, "all", False):
        from data.metadata import get_metadata
        entries   = get_metadata(project_path, media_type=media_type)
        filenames = [e["filename"] for e in entries if e.get("filename")]
    else:
        if not args.filename and not getattr(args, "tmdb", None):
            print("✗ Provide a filename, --tmdb, or --all", file=sys.stderr)
            sys.exit(1)
        try:
            filenames = [resolve_filename(project_path, getattr(args, "tmdb", None), args.filename, media_type)]
        except ValueError as exc:
            print(f"✗ {exc}", file=sys.stderr)
            sys.exit(1)

    migrated = 0
    skipped  = 0
    errors   = 0
    for fn in filenames:
        try:
            changed = migrate_annotations_to_stable_ids(project_path, fn, media_type)
            if changed:
                print(f"✓ Migrated  {fn}")
                migrated += 1
            else:
                print(f"  (already stable or no annotations)  {fn}")
                skipped += 1
        except Exception as exc:
            print(f"✗ Error migrating {fn}: {exc}", file=sys.stderr)
            errors += 1

    print(f"\nMigrated {migrated}  |  already stable/absent {skipped}  |  errors {errors}")
    if errors:
        sys.exit(1)


def _annotate_validate(args):
    """Validate and repair annotation JSON files.

    For each annotation file:
    - Checks that the file is valid JSON.
    - Fixes fields that should be arrays but were stored as a comma-separated
      string (e.g. ``"diegetic, graphics"`` → ``["diegetic", "graphics"]``).
    """
    _require_path()
    from data.shotlist import resolve_filename

    project_path = prefs.get("path")
    media_type   = getattr(args, "media", "movies")
    dry_run      = getattr(args, "dry_run", False)

    # Fields whose values must always be lists, never a plain string.
    _ARRAY_FIELDS = {"type", "humans", "action", "wearing", "animals", "objects", "text"}

    ann_dir = Path(project_path) / "data" / "annotations" / "shots" / media_type

    if getattr(args, "all", False):
        from data.metadata import get_metadata
        entries  = get_metadata(project_path, media_type=media_type)
        filenames = [e["filename"] for e in entries if e.get("filename")]
    else:
        query = getattr(args, "filename", None)
        tmdb  = getattr(args, "tmdb", None)
        if not query and not tmdb:
            print("✗ Provide a keyword, --tmdb, or --all", file=sys.stderr)
            sys.exit(1)
        try:
            filenames = [resolve_filename(project_path, tmdb, query, media_type)]
        except ValueError as exc:
            print(f"✗ {exc}", file=sys.stderr)
            sys.exit(1)

    total_files   = 0
    total_fixes   = 0
    invalid_files = 0

    for fn in filenames:
        stem     = Path(fn).stem
        ann_path = ann_dir / f"{stem}.json"

        if not ann_path.exists():
            print(f"  ?  {fn}  (no annotation file)")
            continue

        total_files += 1

        raw = ann_path.read_text(encoding="utf-8")
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            print(f"  ✗  {fn}: invalid JSON — {exc}")
            invalid_files += 1
            continue

        if not isinstance(data, list):
            print(f"  ✗  {fn}: expected a JSON array at the top level")
            invalid_files += 1
            continue

        file_fixes = 0
        for entry in data:
            if not isinstance(entry, dict):
                continue
            shot_block = entry.get("shot")
            if not isinstance(shot_block, dict):
                continue
            ann = shot_block.get("annotation")
            if not isinstance(ann, dict):
                continue
            for field in _ARRAY_FIELDS:
                val = ann.get(field)
                if isinstance(val, str) and "," in val:
                    parts = [p.strip() for p in val.split(",") if p.strip()]
                    ann[field] = parts
                    file_fixes += 1

        if file_fixes > 0:
            if not dry_run:
                ann_path.write_text(
                    json.dumps(data, indent=2, ensure_ascii=False) + "\n",
                    encoding="utf-8",
                )
            verb = "would fix" if dry_run else "fixed"
            print(f"  ✓  {fn}: {verb} {file_fixes} field(s)")
            total_fixes += file_fixes
        else:
            print(f"  ✓  {fn}: ok")

    action_label = "would be fixed" if dry_run else "fixed"
    print(
        f"\n{total_files} file(s) checked  ·  "
        f"{total_fixes} value(s) {action_label}  ·  "
        f"{invalid_files} invalid JSON file(s)"
    )


def _annotate_audit(args):
    """Report annotation status per film: missing, empty, incomplete, or complete."""
    import json as _json
    from data.metadata import get_metadata
    from data.shotlist import read_shotlist

    project_path = prefs.get("path")
    media_type   = getattr(args, "media", "movies")

    entries = get_metadata(project_path, media_type=media_type)
    if not entries:
        print("No metadata entries found.")
        return

    ann_dir = Path(project_path) / "data" / "annotations" / "shots" / media_type

    missing    = []  # no annotation JSON
    empty      = []  # JSON exists but 0 valid annotations
    incomplete = []  # some shots annotated, some not
    complete   = []  # all shots annotated

    for entry in entries:
        fn = entry.get("filename", "")
        if not fn:
            continue
        stem = Path(fn).stem
        ann_path = ann_dir / f"{stem}.json"

        if not ann_path.exists():
            missing.append(fn)
            continue

        try:
            ann_entries = _json.loads(ann_path.read_text(encoding="utf-8"))
        except Exception:
            missing.append(fn)
            continue

        if not ann_entries:
            empty.append(fn)
            continue

        # Count valid annotations
        valid = sum(
            1 for e in ann_entries
            if isinstance(e.get("shot"), dict)
            and isinstance(e["shot"].get("annotation"), dict)
            and "setting" in e["shot"]["annotation"]
        )

        # Compare against shotlist if available
        try:
            shots = read_shotlist(project_path, fn, media_type)
            total_shots = len(shots)
        except Exception:
            total_shots = len(ann_entries)

        if valid == 0:
            empty.append(fn)
        elif valid < total_shots:
            incomplete.append((fn, valid, total_shots))
        else:
            complete.append(fn)

    n = len(entries)
    print(f"Annotation audit · {media_type}  ({n} film(s))")

    print()
    if complete:
        print(f"  Complete ({len(complete)}):")
        for f in complete:
            print(f"    ✓  {f}")
    else:
        print(f"  Complete:  (none)")

    print()
    if incomplete:
        print(f"  Incomplete ({len(incomplete)}):")
        for f, done, total in incomplete:
            print(f"    ~  {f}  ({done}/{total} shots)")
    else:
        print(f"  Incomplete:  (none)")

    print()
    if empty:
        print(f"  No valid annotations ({len(empty)}):")
        for f in empty:
            print(f"    ✗  {f}")
    else:
        print(f"  No valid annotations:  (none)")

    print()
    if missing:
        print(f"  Missing annotation file ({len(missing)}):")
        for f in missing:
            print(f"    ?  {f}")
    else:
        print(f"  Missing annotation file:  (none)")

    print()
    print(f"  {len(complete)} complete  {len(incomplete)} incomplete  {len(empty)} empty  {len(missing)} missing  —  {n} total")


def cmd_generate(args):
    sub = args.generate_subcommand
    if sub == "composition":
        cmd_composition(args)
    elif sub == "mosaic":
        cmd_mosaic(args)


def cmd_composition(args):
    """composition [query] — build a single tableau from one random search result."""
    _require_path()
    project_path = prefs.get("path")
    query        = args.query or ""
    orientation  = getattr(args, "orientation", "portrait")
    output_path  = getattr(args, "output", None)
    open_result  = not getattr(args, "no_open", False)

    if getattr(args, "visualizer", False):
        from visualizers.composition_visualizer import run_visualizer
        run_visualizer(project_path, initial_query=query)
        return

    if not query:
        print("✗ composition: a search query is required outside of --visualizer mode.", file=sys.stderr)
        sys.exit(1)

    from services.search import search_shots
    from generators.composition import choose_background, build_tableau, save_tableau

    search_result = search_shots(
        query          = query,
        scopes         = None,
        field          = None,
        limit          = None,
        limit_per_item = None,
        use_all        = True,
        project_path   = project_path,
    )
    results = search_result["results"]

    if not results:
        print(f"✗ No results for query {query!r}.", file=sys.stderr)
        sys.exit(1)

    print(f"  {len(results)} result(s) for {query!r} — picking background…")

    try:
        result  = choose_background(results)
        tableau = build_tableau(result, project_path, orientation=orientation)

        if output_path:
            from pathlib import Path as _Path
            out = _Path(output_path)
            out.parent.mkdir(parents=True, exist_ok=True)
            tableau.convert("RGB").save(str(out), "JPEG", quality=93)
        else:
            from pathlib import Path as _Path
            out = save_tableau(
                tableau, query,
                _Path(project_path) / "output" / "compositions",
            )

        movie = result.get("movie_title", result.get("movie_id", ""))
        print(f"✓ Saved: {out}  [{movie}]")

        if open_result:
            try:
                import subprocess as _sp
                _sp.Popen(["xdg-open", str(out)])
            except Exception:
                pass

        if getattr(args, "notify", False):
            from services.notify import discord_notify
            discord_notify(
                f"✓ Composition: {query!r} → {out.name}  [{movie}]",
                project_path,
            )

    except (ValueError, FileNotFoundError) as exc:
        print(f"✗ composition failed: {exc}", file=sys.stderr)
        sys.exit(1)
    except Exception as exc:
        print(f"✗ composition failed: {exc}", file=sys.stderr)
        import traceback
        traceback.print_exc()
        sys.exit(1)


# ---------------------------------------------------------------------------
# mosaic command family
# ---------------------------------------------------------------------------

def cmd_mosaic(args):
    """Generate a mosaic contact sheet from thumbnails or search results."""
    _require_path()
    if getattr(args, "visualizer", False):
        _mosaic_visualizer(args)
        return
    sub = args.mosaic_subcommand
    if sub is None:
        print("✗ mosaic: specify a subcommand or use --visualizer.", file=sys.stderr)
        sys.exit(1)
    if sub == "thumbnails":
        _mosaic_thumbnails(args)
    elif sub == "search":
        _mosaic_search(args)
    elif sub == "export":
        _mosaic_export(args)


def _mosaic_thumbnails(args):
    """Collect thumbnails for a media type and render a mosaic grid."""
    from data.metadata import get_metadata
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
    use_all      = getattr(args, "all", False)
    field        = getattr(args, "field", None)
    limit        = getattr(args, "limit", None)
    layout       = getattr(args, "layout", "landscape")
    frame_pct    = getattr(args, "frame_pct", 0.5)
    output_path  = getattr(args, "output", None)
    open_result  = not getattr(args, "no_open", False)

    # `crossing generate mosaic search text "WANTED"` — same shorthand as
    # `crossing search text "WANTED"`: restrict to the annotation text field.
    query = args.query
    if query == "text":
        if not scopes:
            print("✗ mosaic search text: provide a query phrase, e.g. crossing generate mosaic search text \"WANTED\"", file=sys.stderr)
            sys.exit(1)
        query = scopes[0]
        scopes = scopes[1:] or None
        field = "text"
    else:
        scopes = scopes or None

    search_result = search_shots(
        query=query,
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
        print(f"✗ No results for query '{query}'.", file=sys.stderr)
        sys.exit(1)

    print(f"Building search mosaic: {len(results)} result(s) (query: {query!r})…")

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
                f"✓ Mosaic search complete: '{query}' → {out.name}",
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
# index command family
# ---------------------------------------------------------------------------

def cmd_index(args):
    _require_path()
    sub = args.index_subcommand
    if sub == "serialize":
        _index_serialize(args)
    elif sub == "embed":
        _index_embed(args)
    elif sub == "update":
        _index_update(args)
    elif sub == "audit":
        _index_audit(args)
    else:
        print("✗ index: specify a subcommand.", file=sys.stderr)
        sys.exit(1)


def _index_serialize(args):
    """Serialize annotation items to text and print to stdout and/or save to a .txt file."""
    from data.shotlist import resolve_filename
    from data.index import (
        load_mapping,
        load_annotation_items,
        serialize_annotation_item,
        get_text_path,
        write_text_file,
    )

    project_path = prefs.get("path")
    media_type = getattr(args, "media", "movies")

    query_words = getattr(args, "query", None) or []
    query_str = " ".join(query_words).strip() if query_words else None
    tmdb = getattr(args, "tmdb", None)
    shot_index = getattr(args, "shot", None)
    do_save = getattr(args, "save", False)
    do_print = getattr(args, "print", False)
    force = getattr(args, "force", False)
    verbose = getattr(args, "verbose", False)

    if tmdb is None and not query_str:
        print("✗ Provide a title query or --tmdb <id>.", file=sys.stderr)
        sys.exit(1)

    try:
        filename = resolve_filename(project_path, tmdb, query_str, media_type)
    except ValueError as exc:
        print(f"✗ {exc}", file=sys.stderr)
        sys.exit(1)

    try:
        mapping = load_mapping(project_path)
    except (FileNotFoundError, ValueError, ImportError) as exc:
        print(f"✗ {exc}", file=sys.stderr)
        sys.exit(1)

    try:
        items = load_annotation_items(project_path, filename, media_type)
    except FileNotFoundError as exc:
        print(f"✗ {exc}", file=sys.stderr)
        sys.exit(1)

    # Resolve the slice of items to process
    if shot_index is not None:
        if shot_index < 0 or shot_index >= len(items):
            print(
                f"✗ Shot index {shot_index} out of range "
                f"(0–{len(items) - 1} for {len(items)} shots).",
                file=sys.stderr,
            )
            sys.exit(1)
        indexed_items = [(shot_index, items[shot_index])]
    else:
        indexed_items = list(enumerate(items))

    # Serialize all selected items
    lines: list[str] = []
    for i, item in indexed_items:
        line = serialize_annotation_item(item, mapping)
        lines.append(line)

    # --- stdout-only mode (default) ---
    if not do_save:
        for i, line in zip([idx for idx, _ in indexed_items], lines):
            print(f"{i}: {line}")
        return

    # --- save mode ---
    if shot_index is not None:
        # Single-shot: saving a slice of the full file would be surprising;
        # notify the user and write normally.
        print(
            f"  note: saving a single-shot slice — "
            f"only shot {shot_index} will be written to the .txt file.",
            file=sys.stderr,
        )

    try:
        dest = write_text_file(
            project_path, filename, media_type, lines, force=force
        )
    except FileExistsError as exc:
        print(f"✗ {exc}", file=sys.stderr)
        sys.exit(1)

    if verbose:
        print(f"✓ Saved: {dest}  ({len(lines)} line(s))")

    if do_print:
        for i, line in zip([idx for idx, _ in indexed_items], lines):
            print(f"{i}: {line}")


def _index_embed(args):
    """Embed serialized text lines and save the embedding array to a .npy file."""
    from data.shotlist import resolve_filename
    from data.index import (
        get_text_path,
        get_embeddings_path,
        embed_texts,
        write_embeddings,
    )

    project_path = prefs.get("path")
    media_type = getattr(args, "media", "movies")

    query_words = getattr(args, "query", None) or []
    query_str = " ".join(query_words).strip() if query_words else None
    tmdb = getattr(args, "tmdb", None)
    model_name = getattr(args, "model", None) or prefs.get(_MODEL_KEYS["embed"], _MODEL_DEFAULTS["embed"])
    force = getattr(args, "force", False)
    verbose = getattr(args, "verbose", False)

    if tmdb is None and not query_str:
        print("✗ Provide a title query or --tmdb <id>.", file=sys.stderr)
        sys.exit(1)

    try:
        filename = resolve_filename(project_path, tmdb, query_str, media_type)
    except ValueError as exc:
        print(f"✗ {exc}", file=sys.stderr)
        sys.exit(1)

    text_path = get_text_path(project_path, filename, media_type)
    if not text_path.exists():
        print(
            f"✗ No serialized text found: {text_path}\n"
            f"  Run first: crossing index serialize {query_str or ''} --save",
            file=sys.stderr,
        )
        sys.exit(1)

    lines = [ln for ln in text_path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    if not lines:
        print(f"✗ Serialized text file is empty: {text_path}", file=sys.stderr)
        sys.exit(1)

    if verbose:
        print(f"  Model:  {model_name}")
        print(f"  Input:  {text_path}  ({len(lines)} line(s))")

    try:
        embeddings = embed_texts(lines, model_name, project_path)
    except (ImportError, RuntimeError) as exc:
        print(f"✗ {exc}", file=sys.stderr)
        sys.exit(1)

    try:
        dest = write_embeddings(project_path, filename, media_type, embeddings, force=force)
    except FileExistsError as exc:
        print(f"✗ {exc}", file=sys.stderr)
        sys.exit(1)

    if verbose:
        print(f"✓ Saved: {dest}  (shape {embeddings.shape}  dtype {embeddings.dtype})")
    else:
        print(f"✓ {dest.name}  shape={embeddings.shape}")


def _resolve_all_annotation_filenames(project_path: str, media_type: str) -> list:
    """Return sorted video filenames that have an annotation JSON on disk.

    The filename is read from the first item's ``movie.filename`` field inside
    each JSON; falls back to ``<stem>.mp4`` when that is unavailable.
    """
    from pathlib import Path

    ann_dir = Path(project_path) / "data" / "annotations" / "shots" / media_type
    if not ann_dir.exists():
        return []
    filenames = []
    for json_file in sorted(ann_dir.glob("*.json")):
        if json_file.name.endswith(".manifest.json"):
            continue
        try:
            import json as _json
            with json_file.open("r", encoding="utf-8") as f:
                items = _json.load(f)
            if items and isinstance(items[0], dict) and "movie" in items[0]:
                fn = items[0]["movie"].get("filename")
                if fn:
                    filenames.append(fn)
                    continue
        except Exception:
            pass
        filenames.append(json_file.stem + ".mp4")
    return filenames


def _update_one_film(
    project_path: str,
    filename: str,
    media_type: str,
    model_name: str,
    *,
    force: bool = False,
    verbose: bool = False,
) -> str:
    """Reconcile .txt, .npy, and manifest for one film.

    Returns:
        ``"ok"``    — files were rebuilt and manifest refreshed.
        ``"skip"``  — everything was already up to date.
        ``"error"`` — a fatal error occurred; message already printed.
    """
    from pathlib import Path
    from data.annotate import get_annotation_json_path
    from data.index import (
        load_mapping,
        load_annotation_items,
        serialize_annotation_item,
        get_text_path,
        write_text_file,
        get_embeddings_path,
        embed_texts,
        write_embeddings,
        load_manifest,
        write_manifest,
        build_manifest,
        hash_file,
    )

    stem = Path(filename).stem
    json_path = get_annotation_json_path(project_path, filename, media_type)
    mapping_path = Path(project_path) / "preferences" / "data" / "mapping.yaml"
    txt_path = get_text_path(project_path, filename, media_type)
    npy_path = get_embeddings_path(project_path, filename, media_type)

    if not json_path.exists():
        print(f"  ✗ {stem}  no annotation JSON", file=sys.stderr)
        return "error"

    live_json_hash = hash_file(json_path)
    live_mapping_hash = hash_file(mapping_path) if mapping_path.exists() else None

    manifest = None if force else load_manifest(project_path, filename, media_type)

    # ---- Determine what needs rebuilding ----
    if manifest and not force:
        m_json = manifest.get("json", {})
        m_map = manifest.get("mapping", {})
        m_txt = manifest.get("txt", {})
        m_npy = manifest.get("npy", {})

        need_txt = (
            not txt_path.exists()
            or m_json.get("hash") != live_json_hash
            or m_map.get("hash") != live_mapping_hash
            or m_txt.get("hash") != hash_file(txt_path)
        )
        if need_txt:
            need_npy = True
        else:
            live_txt_hash = hash_file(txt_path)
            need_npy = (
                not npy_path.exists()
                or m_npy.get("embed_model") != model_name
                or m_txt.get("hash") != live_txt_hash
                or m_npy.get("hash") != hash_file(npy_path)
            )
    else:
        need_txt = True
        need_npy = True

    if not need_txt and not need_npy:
        print(f"✓ {stem}  (up to date)")
        return "skip"

    # ---- Rebuild .txt ----
    if need_txt:
        try:
            mapping = load_mapping(project_path)
        except (FileNotFoundError, ValueError, ImportError) as exc:
            print(f"  ✗ {stem}  mapping: {exc}", file=sys.stderr)
            return "error"
        try:
            items = load_annotation_items(project_path, filename, media_type)
        except FileNotFoundError as exc:
            print(f"  ✗ {stem}  {exc}", file=sys.stderr)
            return "error"
        lines = [serialize_annotation_item(item, mapping) for item in items]
        write_text_file(project_path, filename, media_type, lines, force=True)
        if verbose:
            print(f"  ✓ txt    {txt_path.name}  ({len(lines)} lines)")
    elif verbose:
        print(f"  — txt    (unchanged)")

    # ---- Rebuild .npy ----
    if need_npy:
        raw_lines = txt_path.read_text(encoding="utf-8").splitlines()
        lines = [ln for ln in raw_lines if ln.strip()]
        try:
            embeddings = embed_texts(lines, model_name, project_path)
        except (ImportError, RuntimeError) as exc:
            print(f"  ✗ {stem}  embed: {exc}", file=sys.stderr)
            return "error"
        write_embeddings(project_path, filename, media_type, embeddings, force=True)
        if verbose:
            print(f"  ✓ npy    {npy_path.name}  (shape {embeddings.shape})")
    elif verbose:
        print(f"  — npy    (unchanged)")

    # ---- Write manifest ----
    try:
        m = build_manifest(project_path, filename, media_type, embed_model=model_name)
        write_manifest(project_path, filename, media_type, m)
    except Exception as exc:
        print(f"  ✗ {stem}  manifest: {exc}", file=sys.stderr)
        return "error"

    if verbose:
        print(f"  ✓ manifest updated")
    print(f"✓ {stem}  updated")
    return "ok"


def _audit_one_film(
    project_path: str,
    filename: str,
    media_type: str,
    model_name: str,
    *,
    verbose: bool = False,
) -> str:
    """Inspect and print index status for one film. Never modifies files.

    Returns one of: ``"current"``, ``"stale"``, ``"missing"``, ``"no manifest"``.
    """
    from pathlib import Path
    from data.annotate import get_annotation_json_path
    from data.index import (
        get_text_path,
        get_embeddings_path,
        load_manifest,
        hash_file,
    )

    stem = Path(filename).stem
    json_path = get_annotation_json_path(project_path, filename, media_type)
    mapping_path = Path(project_path) / "preferences" / "data" / "mapping.yaml"
    txt_path = get_text_path(project_path, filename, media_type)
    npy_path = get_embeddings_path(project_path, filename, media_type)

    json_exists = json_path.exists()
    txt_exists = txt_path.exists()
    npy_exists = npy_path.exists()

    live_json_hash = hash_file(json_path) if json_exists else None
    live_mapping_hash = hash_file(mapping_path) if mapping_path.exists() else None
    live_txt_hash = hash_file(txt_path) if txt_exists else None
    live_npy_hash = hash_file(npy_path) if npy_exists else None

    manifest = load_manifest(project_path, filename, media_type)

    txt_issues: list = []
    npy_issues: list = []

    if manifest:
        m_json = manifest.get("json", {})
        m_map = manifest.get("mapping", {})
        m_txt = manifest.get("txt", {})
        m_npy = manifest.get("npy", {})

        # txt staleness checks (at most one reason reported)
        if not txt_exists:
            txt_issues.append("missing")
        elif m_json.get("hash") != live_json_hash:
            txt_issues.append("json changed")
        elif m_map.get("hash") != live_mapping_hash:
            txt_issues.append("mapping changed")
        elif m_txt.get("hash") != live_txt_hash:
            txt_issues.append("externally modified")

        # npy staleness checks
        # When txt is stale npy will need a rebuild too; we report it separately
        # only if txt itself is fine so the output stays focused.
        if not npy_exists:
            npy_issues.append("missing")
        elif txt_issues:
            npy_issues.append("will need rebuild")
        elif m_npy.get("embed_model") != model_name:
            prev_model = m_npy.get("embed_model", "?")
            npy_issues.append(f"model changed ({prev_model!r} → {model_name!r})")
        elif m_txt.get("hash") != live_txt_hash:
            npy_issues.append("txt modified externally")
        elif m_npy.get("hash") != live_npy_hash:
            npy_issues.append("externally modified")

        # alignment check (cheap — uses manifest counts, not file reads)
        if json_exists and txt_exists and npy_exists and not txt_issues and not npy_issues:
            item_count = m_json.get("item_count")
            line_count = m_txt.get("line_count")
            npy_rows = (m_npy.get("shape") or [None])[0]
            if None not in (item_count, line_count, npy_rows):
                if not (item_count == line_count == npy_rows):
                    npy_issues.append(
                        f"row mismatch (json={item_count}, txt={line_count}, npy={npy_rows})"
                    )

        all_issues = (
            [f"stale txt ({i})" for i in txt_issues]
            + [f"stale npy ({i})" for i in npy_issues]
        )
        if all_issues:
            status = "stale"
            icon = "✗"
        else:
            status = "current"
            icon = "✓"
    else:
        # No manifest — report what's present
        if not txt_exists:
            txt_issues.append("missing")
        if not npy_exists:
            npy_issues.append("missing")
        all_issues = (
            (["txt missing"] if not txt_exists else [])
            + (["npy missing"] if not npy_exists else [])
        )
        status = "no manifest"
        icon = "!"

    # ---- Output ----
    if not verbose:
        issue_str = f"  [{'; '.join(all_issues)}]" if all_issues else ""
        print(f"{icon}  {stem}  {status}{issue_str}")
    else:
        print(f"\n{stem}")

        # json row
        if json_exists:
            h = live_json_hash[:22] + "…" if live_json_hash else ""
            count = (manifest.get("json", {}).get("item_count", "?")
                     if manifest else "?")
            print(f"  json      ✓  {count} items    {h}")
        else:
            print(f"  json      ✗  missing")

        # mapping row
        if mapping_path.exists():
            h = live_mapping_hash[:22] + "…" if live_mapping_hash else ""
            print(f"  mapping   ✓  {h}")
        else:
            print(f"  mapping   ✗  missing")

        # txt row
        if txt_exists:
            lc = (manifest.get("txt", {}).get("line_count", "?")
                  if manifest else "?")
            lbl = "stale" if txt_issues else "current"
            reason = f"  ({'; '.join(txt_issues)})" if txt_issues else ""
            sym = "✗" if txt_issues else "✓"
            print(f"  txt       {sym}  {lc} lines    [{lbl}]{reason}")
        else:
            print(f"  txt       ✗  missing")

        # npy row
        if npy_exists:
            shape = (manifest.get("npy", {}).get("shape") if manifest else None)
            dtype = (manifest.get("npy", {}).get("dtype", "?") if manifest else "?")
            stored_model = (manifest.get("npy", {}).get("embed_model", "?")
                            if manifest else "?")
            shape_str = f"({', '.join(str(d) for d in shape)})" if shape else "?"
            lbl = "stale" if npy_issues else "current"
            reason = f"  ({'; '.join(npy_issues)})" if npy_issues else ""
            sym = "✗" if npy_issues else "✓"
            model_note = ""
            if stored_model != model_name:
                model_note = f"  stored: {stored_model!r}"
            print(f"  npy       {sym}  {shape_str} {dtype}    [{lbl}]{reason}{model_note}")
        else:
            print(f"  npy       ✗  missing")

        # manifest row
        if manifest:
            updated = manifest.get("updated_at", "?")
            print(f"  manifest  ✓  {updated}")
        else:
            print(f"  manifest  ✗  missing")

        # active model
        print(f"  model        {model_name}")

        # status summary
        issue_desc = f"  ({'; '.join(all_issues)})" if all_issues else ""
        print(f"  status    {icon}  {status}{issue_desc}")

    return status


def _index_update(args):
    """Reconcile .txt, .npy, and manifest for one film or all films."""
    from data.shotlist import resolve_filename

    project_path = prefs.get("path")
    media_type = getattr(args, "media", "movies")
    query_words = getattr(args, "query", None) or []
    query_str = " ".join(query_words).strip() if query_words else None
    tmdb = getattr(args, "tmdb", None)
    do_all = getattr(args, "all", False)
    model_name = (
        getattr(args, "model", None)
        or prefs.get(_MODEL_KEYS["embed"], _MODEL_DEFAULTS["embed"])
    )
    force = getattr(args, "force", False)
    verbose = getattr(args, "verbose", False)

    if do_all:
        filenames = _resolve_all_annotation_filenames(project_path, media_type)
        if not filenames:
            print(f"No annotation JSON files found under {media_type}.", file=sys.stderr)
            sys.exit(1)
        counts = {"ok": 0, "skip": 0, "error": 0}
        for fn in filenames:
            result = _update_one_film(
                project_path, fn, media_type, model_name,
                force=force, verbose=verbose,
            )
            counts[result] = counts.get(result, 0) + 1
        total = sum(counts.values())
        parts = []
        if counts["ok"]:
            parts.append(f"{counts['ok']} updated")
        if counts["skip"]:
            parts.append(f"{counts['skip']} current")
        if counts["error"]:
            parts.append(f"{counts['error']} error(s)")
        print(f"\n{', '.join(parts)}  —  {total} total")
    else:
        if tmdb is None and not query_str:
            print("✗ Provide a title query, --tmdb <id>, or --all.", file=sys.stderr)
            sys.exit(1)
        try:
            filename = resolve_filename(project_path, tmdb, query_str, media_type)
        except ValueError as exc:
            print(f"✗ {exc}", file=sys.stderr)
            sys.exit(1)
        _update_one_film(
            project_path, filename, media_type, model_name,
            force=force, verbose=verbose,
        )


def _index_audit(args):
    """Inspect and report index status for one film or all films. Never writes files."""
    from data.shotlist import resolve_filename

    project_path = prefs.get("path")
    media_type = getattr(args, "media", "movies")
    query_words = getattr(args, "query", None) or []
    query_str = " ".join(query_words).strip() if query_words else None
    tmdb = getattr(args, "tmdb", None)
    do_all = getattr(args, "all", False)
    model_name = (
        getattr(args, "model", None)
        or prefs.get(_MODEL_KEYS["embed"], _MODEL_DEFAULTS["embed"])
    )
    verbose = getattr(args, "verbose", False)

    if do_all or (tmdb is None and not query_str):
        filenames = _resolve_all_annotation_filenames(project_path, media_type)
        if not filenames:
            print(f"No annotation JSON files found under {media_type}.", file=sys.stderr)
            sys.exit(1)
        tally: dict = {}
        for fn in filenames:
            s = _audit_one_film(
                project_path, fn, media_type, model_name, verbose=verbose
            )
            tally[s] = tally.get(s, 0) + 1
        total = sum(tally.values())
        parts = []
        for key in ("current", "stale", "missing", "no manifest"):
            if tally.get(key):
                parts.append(f"{tally[key]} {key}")
        remaining = {k: v for k, v in tally.items() if k not in parts}
        for k, v in remaining.items():
            parts.append(f"{v} {k}")
        print(f"\n{', '.join(parts)}  —  {total} total")
    else:
        if tmdb is None and not query_str:
            print("✗ Provide a title query, --tmdb <id>, or --all.", file=sys.stderr)
            sys.exit(1)
        try:
            filename = resolve_filename(project_path, tmdb, query_str, media_type)
        except ValueError as exc:
            print(f"✗ {exc}", file=sys.stderr)
            sys.exit(1)
        _audit_one_film(
            project_path, filename, media_type, model_name, verbose=verbose
        )


def cmd_visualizer(args):
    sub = args.visualizer_subcommand
    if sub in (None, "project"):
        _project_visualizer(args)
    elif sub == "shotlist":
        _require_path()
        filename = getattr(args, "filename", None)
        if filename:
            # Try to send to a running Shotlist Visualizer first.
            from visualizers.shot_visualizer import ipc_send_load
            media_type = getattr(args, "media", "movies")
            project_path = prefs.get("path")
            if ipc_send_load(project_path, filename, media_type):
                return  # delivered to running instance
            # No running instance — launch a new one with this film.
            _require_visualizer_deps()
            from visualizers.shot_visualizer import ipc_send_load  # noqa: already imported
            cli_dir = Path(__file__).parent
            visualizer_path = cli_dir / "visualizers" / "shot_visualizer.py"
            import subprocess as _sp
            _sp.Popen([
                sys.executable, str(visualizer_path),
                "--media", media_type,
                "--project", project_path,
                "--filenames", filename,
                *( ["--verbose"] if getattr(args, "verbose", False) else [] ),
            ])
        else:
            args.all = True
            args.query = None
            args.tmdb = None
            _shot_visualizer(args)
    elif sub == "composition":
        _require_path()
        args.query = getattr(args, "query", "") or ""
        cmd_composition(args)
    elif sub == "mosaic":
        _require_path()
        _mosaic_visualizer(args)
    elif sub == "metadata":
        _require_path()
        _metadata_visualizer(args)


def _project_visualizer(args):
    """Launch the project launcher and configuration GUI."""
    _require_visualizer_deps()
    from visualizers.project_visualizer import run_visualizer
    run_visualizer()


def _metadata_visualizer(args):
    """Launch the metadata browser GUI."""
    _require_visualizer_deps()
    from visualizers.metadata_visualizer import run_visualizer
    run_visualizer(prefs.get("path"))


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
    annotate_sub = p_annotate.add_subparsers(dest="annotate_type", required=False)

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
    p_annotate_shot.add_argument(
        "--min-frame-interval",
        type=float,
        default=prefs.get(_ANNOTATE_DEFAULT_KEYS["min-frame-interval"][0], _ANNOTATE_DEFAULT_KEYS["min-frame-interval"][1]),
        help="For long shots, ensure at least one sampled frame every N seconds (default: 4.0)",
    )
    p_annotate_shot.add_argument(
        "--max-frames-per-shot",
        type=int,
        default=prefs.get(_ANNOTATE_DEFAULT_KEYS["max-frames-per-shot"][0], _ANNOTATE_DEFAULT_KEYS["max-frames-per-shot"][1]),
        help="Hard cap for adaptive frame sampling on long shots (default: 16)",
    )
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
    p_annotate_shot.add_argument("--notify-each", action="store_true", dest="notify_items", help="Send a Discord notification after each movie is annotated in a --all batch")
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
    p_annotate_scene.add_argument(
        "--min-frame-interval",
        type=float,
        default=prefs.get(_ANNOTATE_DEFAULT_KEYS["min-frame-interval"][0], _ANNOTATE_DEFAULT_KEYS["min-frame-interval"][1]),
        help="For long shots, ensure at least one sampled frame every N seconds (default: 4.0)",
    )
    p_annotate_scene.add_argument(
        "--max-frames-per-shot",
        type=int,
        default=prefs.get(_ANNOTATE_DEFAULT_KEYS["max-frames-per-shot"][0], _ANNOTATE_DEFAULT_KEYS["max-frames-per-shot"][1]),
        help="Hard cap for adaptive frame sampling on long shots (default: 16)",
    )
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

    p_annotate_remove = annotate_sub.add_parser("remove", help="Remove shot annotations for a film")
    p_annotate_remove.set_defaults(func=_annotate_remove)
    p_annotate_remove.add_argument("filename", nargs="?", default=None, help="Video filename (or use --tmdb / --all)")
    p_annotate_remove.add_argument("--tmdb", type=int, default=None, help="TMDb ID")
    p_annotate_remove.add_argument("--media", choices=["movies", "gameplay"], default="movies")
    p_annotate_remove.add_argument("--all", action="store_true", help="Remove annotations for all films in metadata")

    p_annotate_audit = annotate_sub.add_parser("audit", help="Report annotation status per film (complete, incomplete, missing)")
    p_annotate_audit.set_defaults(func=_shotlist_annotate)
    p_annotate_audit.add_argument("--media", choices=["movies", "gameplay"], default="movies")

    p_annotate_validate = annotate_sub.add_parser(
        "validate",
        help="Validate annotation JSON and fix comma-separated values in array fields",
    )
    p_annotate_validate.set_defaults(func=_shotlist_annotate)
    p_annotate_validate.add_argument(
        "filename", nargs="?", default=None,
        help="Fuzzy keyword to match a movie or game title (or use --tmdb / --all)",
    )
    p_annotate_validate.add_argument("--tmdb", type=int, default=None, help="TMDb ID")
    p_annotate_validate.add_argument(
        "--media", choices=["movies", "gameplay"], default="movies",
    )
    p_annotate_validate.add_argument(
        "--all", action="store_true",
        help="Validate annotations for all films/games in metadata",
    )
    p_annotate_validate.add_argument(
        "--dry-run", dest="dry_run", action="store_true",
        help="Report issues without writing any changes",
    )

    p_annotate_migrate = annotate_sub.add_parser(
        "migrate",
        help="Migrate annotation JSON files from legacy integer shot_ids to stable <media_id>@fSTART-fEND IDs",
    )
    p_annotate_migrate.set_defaults(func=_annotate_migrate)
    p_annotate_migrate.add_argument(
        "filename", nargs="?", default=None,
        help="Fuzzy keyword to match a movie or game title (or use --tmdb / --all)",
    )
    p_annotate_migrate.add_argument("--tmdb", type=int, default=None, help="TMDb ID")
    p_annotate_migrate.add_argument(
        "--media", choices=["movies", "gameplay"], default="movies",
    )
    p_annotate_migrate.add_argument(
        "--all", action="store_true",
        help="Migrate all annotation files in the project",
    )

    # (moved under 'crossing tool model' — see tool_sub below)

    # generate command group — composition, mosaic
    p_generate = sub.add_parser("generate", help="Generate content from project data (composition, mosaic)")
    p_generate.set_defaults(func=cmd_generate)
    generate_sub = p_generate.add_subparsers(dest="generate_subcommand", required=True)

    # generate composition
    p_composition = generate_sub.add_parser(
        "composition",
        help="Build a single tableau image from a search criteria string",
    )
    p_composition.set_defaults(func=cmd_composition)
    p_composition.add_argument("query", nargs="?", default="", help="Background search criteria (e.g. \"gun\" or \"sunset\"); optional when --visualizer is used")
    p_composition.add_argument(
        "--orientation", choices=["portrait", "landscape"], default="portrait",
        help="Canvas preset: portrait 1240×1754 or landscape 1920×1080 (default: portrait)",
    )
    p_composition.add_argument("--output", default=None, metavar="PATH", help="Override output file path")
    p_composition.add_argument("--no-open", action="store_true", dest="no_open", help="Do not open result in desktop viewer")
    p_composition.add_argument("--notify", action="store_true", help="Send a Discord notification when done")
    p_composition.add_argument("--visualizer", action="store_true", help="Open the interactive composition visualizer instead of saving")

    # generate mosaic
    p_mosaic = generate_sub.add_parser(
        "mosaic",
        help="Generate a mosaic grid image from thumbnails or text frames",
    )
    p_mosaic.set_defaults(func=cmd_mosaic)
    p_mosaic.add_argument("--visualizer", action="store_true", help="Open the interactive mosaic explorer GUI")
    mosaic_sub = p_mosaic.add_subparsers(dest="mosaic_subcommand", required=False)

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

    # index command group
    p_index = sub.add_parser(
        "index",
        help="Build and inspect text indices for annotation data",
    )
    p_index.set_defaults(func=cmd_index)
    index_sub = p_index.add_subparsers(dest="index_subcommand", required=True)

    p_index_serialize = index_sub.add_parser(
        "serialize",
        help=(
            "Serialize annotation items to text lines using the project mapping "
            "and print them to stdout"
        ),
    )
    p_index_serialize.set_defaults(func=cmd_index)
    p_index_serialize.add_argument(
        "query",
        nargs="*",
        help="Title keywords to identify the film (e.g. 7th Cavalry)",
    )
    p_index_serialize.add_argument(
        "--tmdb", type=int, default=None,
        help="TMDb ID of the film (unambiguous alternative to title keywords)",
    )
    p_index_serialize.add_argument(
        "--media", choices=["movies", "gameplay"], default="movies",
        help="Media type (default: movies)",
    )
    p_index_serialize.add_argument(
        "--shot", type=int, default=None, metavar="INDEX",
        help=(
            "Serialize only this one shot (0-based list index). "
            "Omit to serialize all shots."
        ),
    )
    p_index_serialize.add_argument(
        "--save", action="store_true",
        help=(
            "Write serialized lines to "
            "<project>/data/index/text/<media>/<stem>.txt "
            "instead of printing to stdout"
        ),
    )
    p_index_serialize.add_argument(
        "--print", action="store_true", dest="print",
        help="When --save is active, also print serialized lines to stdout",
    )
    p_index_serialize.add_argument(
        "--force", action="store_true",
        help="Overwrite an existing .txt file (only relevant with --save)",
    )
    p_index_serialize.add_argument(
        "--verbose", action="store_true",
        help="Print each serialized line as it is produced (during --save runs)",
    )

    p_index_embed = index_sub.add_parser(
        "embed",
        help=(
            "Generate embeddings from serialized text lines"
        ),
    )
    p_index_embed.set_defaults(func=cmd_index)
    p_index_embed.add_argument(
        "query",
        nargs="*",
        help="Title keywords to identify the film (e.g. 7th Cavalry)",
    )
    p_index_embed.add_argument(
        "--tmdb", type=int, default=None,
        help="TMDb ID of the film (unambiguous alternative to title keywords)",
    )
    p_index_embed.add_argument(
        "--media", choices=["movies", "gameplay"], default="movies",
        help="Media type (default: movies)",
    )
    p_index_embed.add_argument(
        "--model", default=None, metavar="NAME",
        help=(
            "Embedding model name or path.  Resolved against <project>/models/, "
            "then as an explicit path, then as a HuggingFace repo-id.  "
            "Defaults to the 'embed' model role (crossing tool model set embed <name>)."
        ),
    )
    p_index_embed.add_argument(
        "--force", action="store_true",
        help="Overwrite an existing .npy embeddings file",
    )
    p_index_embed.add_argument(
        "--verbose", action="store_true",
        help="Print model, input path, output shape, and save confirmation",
    )

    p_index_update = index_sub.add_parser(
        "update",
        help=(
            "Reconcile .txt, .npy, and manifest for a film.  "
            "Only rebuilds what is missing or stale."
        ),
    )
    p_index_update.set_defaults(func=cmd_index)
    p_index_update.add_argument(
        "query",
        nargs="*",
        help="Title keywords to identify the film (e.g. 7th Cavalry)",
    )
    p_index_update.add_argument(
        "--tmdb", type=int, default=None,
        help="TMDb ID of the film (unambiguous alternative to title keywords)",
    )
    p_index_update.add_argument(
        "--media", choices=["movies", "gameplay"], default="movies",
        help="Media type (default: movies)",
    )
    p_index_update.add_argument(
        "--model", default=None, metavar="NAME",
        help=(
            "Embedding model name or path.  "
            "Defaults to the 'embed' model role (crossing tool model set embed <name>)."
        ),
    )
    p_index_update.add_argument(
        "--all", action="store_true",
        help="Process all films that have an annotation JSON",
    )
    p_index_update.add_argument(
        "--force", action="store_true",
        help="Force a full rebuild even if files appear current",
    )
    p_index_update.add_argument(
        "--verbose", action="store_true",
        help="Print per-file actions (txt written, npy written, unchanged)",
    )

    p_index_audit = index_sub.add_parser(
        "audit",
        help="Inspect index status for a film without modifying any files",
    )
    p_index_audit.set_defaults(func=cmd_index)
    p_index_audit.add_argument(
        "query",
        nargs="*",
        help="Title keywords to identify the film (e.g. 7th Cavalry)",
    )
    p_index_audit.add_argument(
        "--tmdb", type=int, default=None,
        help="TMDb ID of the film (unambiguous alternative to title keywords)",
    )
    p_index_audit.add_argument(
        "--media", choices=["movies", "gameplay"], default="movies",
        help="Media type (default: movies)",
    )
    p_index_audit.add_argument(
        "--model", default=None, metavar="NAME",
        help=(
            "Embedding model to check against.  "
            "Defaults to the 'embed' model role (crossing tool model set embed <name>)."
        ),
    )
    p_index_audit.add_argument(
        "--all", action="store_true",
        help="Audit all films that have an annotation JSON",
    )
    p_index_audit.add_argument(
        "--verbose", action="store_true",
        help="Show per-field detail (json, mapping, txt, npy, manifest) for each film",
    )

    # media command group
    p_media = sub.add_parser(
        "media",
        help="Manage content: videos, subtitles, posters, and thumbnails",
    )
    p_media.set_defaults(func=cmd_media)
    media_sub = p_media.add_subparsers(dest="media_subcommand", required=True)

    # media import
    p_import = media_sub.add_parser(
        "import",
        help="Import media files into the project",
        epilog=(
            "movie:    crossing media import --media movie film.mkv\n"
            "gameplay: crossing media import --media gameplay --game rdr2 clip.mp4"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p_import.add_argument("sources", nargs="*", metavar="source", help="File(s) or folder to import")
    p_import.add_argument("--pick", action="store_true", help="Open GUI file/folder picker")
    p_import.add_argument("--media", choices=["movie", "gameplay"], default=None, required=True,
                          help="Media type to import")
    p_import.add_argument("--optimize", choices=["universal", "pi5"], default=None,
                          help="Re-encode for a target platform (movie only; omit to copy as-is)")
    p_import.add_argument("--skip-metadata", action="store_true", help="Skip automatic metadata fetch (movie only)")
    p_import.add_argument("--title", default=None, help="Display title (gameplay only; default: derived from filename)")
    p_import.add_argument("--game", default=None, help="Game slug for media_id prefix (gameplay only, required; e.g. rdr2)")
    p_import.add_argument("--verbose", action="store_true", help="Print a message as each file import begins")
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
    p_meta_get.add_argument("--markdown", action="store_true", help="Save output as Markdown to <project>/data/markdown/")
    p_meta_get.add_argument("--open", action="store_true", help="Open the saved Markdown file after writing (implies --markdown)")

    p_meta_set = meta_sub.add_parser("set", help="Set/update metadata from a JSON string")
    p_meta_set.add_argument("json_data", metavar="json")

    p_meta_update = meta_sub.add_parser("update", help="Fetch and save metadata for entries missing key fields")
    p_meta_update.add_argument("--file", default=None, help="Update a single file by filename")
    p_meta_update.add_argument("--media", choices=["movies", "gameplay"], default="movies")
    p_meta_update.add_argument("--force", action="store_true", help="Force re-fetch metadata for all entries (including duration)")

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

    p_meta_audit = meta_sub.add_parser("audit", help="Report missing metadata, shotlists, and subtitles")
    p_meta_audit.add_argument("--media", choices=["movies", "gameplay"], default="movies")



    # media audit (alias for metadata audit)
    p_media_audit = media_sub.add_parser("audit", help="Report missing metadata, thumbnails, shotlists, and subtitles")
    p_media_audit.add_argument("--media", choices=["movies", "gameplay"], default="movies")
    p_media_audit.set_defaults(func=cmd_media)

    # media update (alias for metadata update)
    p_media_update = media_sub.add_parser("update", help="Fetch and save metadata/thumbnails for entries missing key fields")
    p_media_update.add_argument("--file", default=None, help="Update a single file by filename")
    p_media_update.add_argument("--media", choices=["movies", "gameplay"], default="movies")
    p_media_update.add_argument("--force", action="store_true", help="Force re-fetch metadata for all entries")
    p_media_update.set_defaults(func=cmd_media)

    # media normalize
    p_media_normalize = media_sub.add_parser(
        "normalize",
        help="Measure loudness and save one playback gain value (audio_gain_db) per asset",
    )
    p_media_normalize.add_argument(
        "target",
        nargs="?",
        choices=["movie", "gameplay"],
        help="Normalize one media type: movie or gameplay",
    )
    p_media_normalize.add_argument(
        "query",
        nargs="*",
        help="Title/filename/media_id query for a single asset (omit with --all)",
    )
    p_media_normalize.add_argument(
        "--all",
        action="store_true",
        help="Normalize all eligible metadata entries (both movies and gameplay unless a target is provided)",
    )
    p_media_normalize.add_argument(
        "--force",
        action="store_true",
        help="Overwrite existing audio_gain_db values instead of skipping already-normalized assets",
    )
    p_media_normalize.add_argument(
        "--verbose",
        action="store_true",
        help="Print per-asset loudness and gain details during normalization",
    )
    p_media_normalize.add_argument(
        "--notify",
        action="store_true",
        help="Send a Discord notification when normalization finishes",
    )
    p_media_normalize.add_argument(
        "--notify-each",
        action="store_true",
        dest="notify_items",
        help="Send a Discord notification after each processed asset",
    )
    p_media_normalize.set_defaults(func=cmd_media)

    # media channels
    p_media_channels = media_sub.add_parser(
        "channels",
        help="Inspect audio stream channels and save a playback mapping (audio_channels) per asset",
    )
    p_media_channels.add_argument(
        "target",
        nargs="?",
        choices=["movie", "gameplay"],
        help="Scan one media type: movie or gameplay",
    )
    p_media_channels.add_argument(
        "query",
        nargs="*",
        help="Title/filename/media_id query for a single asset (omit with --all)",
    )
    p_media_channels.add_argument(
        "--all",
        action="store_true",
        help="Scan all eligible metadata entries (both movies and gameplay unless a target is provided)",
    )
    p_media_channels.add_argument(
        "--count",
        action="store_true",
        help="Read-only mode: do not save metadata, print a channel-count distribution",
    )
    p_media_channels.add_argument(
        "--format",
        choices=["text", "json", "markdown"],
        default="text",
        help="Output format for --count mode (default: text)",
    )
    p_media_channels.add_argument(
        "--force",
        action="store_true",
        help="Overwrite existing audio_channels values instead of skipping already-scanned assets",
    )
    p_media_channels.add_argument(
        "--verbose",
        action="store_true",
        help="Print per-asset channel details during scanning",
    )
    p_media_channels.add_argument(
        "--notify",
        action="store_true",
        help="Send a Discord notification when channel scanning finishes",
    )
    p_media_channels.add_argument(
        "--notify-each",
        action="store_true",
        dest="notify_items",
        help="Send a Discord notification after each processed asset",
    )
    p_media_channels.set_defaults(func=cmd_media)

    # media remove
    p_remove = media_sub.add_parser("remove", help="Remove a film and all its associated files")
    p_remove.set_defaults(func=cmd_remove)
    p_remove.add_argument("query", nargs="*", help="Filename or title words to match")
    p_remove.add_argument("--tmdb", type=int, default=None, help="TMDb ID (unambiguous)")
    p_remove.add_argument("--media", choices=["movies", "gameplay"], default=None, required=True,
                          help="Media type to remove from")
    p_remove.add_argument("--confirm", action="store_true", help="Actually delete (default is a dry run)")

    # search command
    p_search = sub.add_parser(
        "search",
        help="Search shot annotations",
        epilog=(
            "query modes:\n"
            "  crossing search \"<phrase>\"          semantic search across all fields\n"
            "  crossing search text \"<phrase>\"     search the annotation text field\n"
            "  crossing search vocabulary <field>  list distinct values in a field\n"
            "  crossing search vocabulary --all-fields  vocabulary for every field (JSON)\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p_search.add_argument(
        "query",
        help=(
            "Search phrase, or a special mode keyword: "
            "\"text\" (search annotation text field) or "
            "\"vocabulary\" (list distinct field values). "
            "See query modes below."
        ),
    )
    p_search.add_argument("scope", nargs="*", help="Fuzzy movie-title filter(s); omit to search all movies")
    p_search.add_argument("--movie", nargs="+", default=None, metavar="TITLE", help="Fuzzy movie-title filter(s) (named alternative to positional scope)")
    p_search.add_argument("--field", default=None, help="Restrict search to one annotation field (e.g. objects)")
    p_search.add_argument("--limit", type=int, default=None, help="Max results to return overall")
    p_search.add_argument("--limit-per-item", dest="limit_per_item", type=int, default=None, help="Max results per movie")
    p_search.add_argument("--all", action="store_true", help="Search all movies (overrides positional scopes)")
    p_search.add_argument("--show_count", action="store_true", help="(vocabulary mode) include occurrence counts in output")
    p_search.add_argument("--sort", choices=["alphabetical", "count"], default="alphabetical", help="(vocabulary mode) sort order: alphabetical (default) or count")
    p_search.add_argument("--all-fields", dest="all_fields", action="store_true", help="(vocabulary mode) emit vocabulary for every annotation field as a single JSON object")
    p_search.add_argument("--exclude", nargs="+", default=None, metavar="FIELD", help="(vocabulary mode) exclude one or more fields from output (e.g. --exclude description humans)")
    p_search.add_argument("--markdown", action="store_true", help="Save output as Markdown to <project>/data/markdown/")
    p_search.add_argument("--open", action="store_true", help="Open the saved Markdown file after writing (implies --markdown)")
    p_search.add_argument("--media", choices=["movies", "gameplay"], default="movies")
    p_search.set_defaults(func=cmd_search)

    # shotlist command group
    p_shotlist = sub.add_parser("shotlist", help="Manage shot and scene cuts and annotations")
    p_shotlist.set_defaults(func=cmd_shotlist)
    p_shotlist.add_argument("--visualizer", action="store_true", help="Open the shot visualizer GUI (all films)")
    p_shotlist.add_argument("--media", choices=["movies", "gameplay"], default="movies", help="Media type for --visualizer (default: movies)")
    shotlist_sub = p_shotlist.add_subparsers(dest="shotlist_subcommand", required=False)

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
    p_sl_shot_detect.add_argument("--notify-each", action="store_true", dest="notify_items", help="Send a Discord notification after each item in a batch")

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

    _default_key_choices = list(_TOOL_DEFAULT_KEYS)

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
        help="Which role to configure: annotate or segmentation",
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

    # visualizer command group — shortcut to all visualizer GUIs
    p_visualizer = sub.add_parser(
        "visualizer",
        help="Open a visualizer GUI (project, shotlist, composition, mosaic)",
    )
    p_visualizer.set_defaults(func=cmd_visualizer, visualizer_subcommand="project")
    visualizer_sub = p_visualizer.add_subparsers(dest="visualizer_subcommand", required=False)

    visualizer_sub.add_parser(
        "project",
        help="Open the project launcher and configuration window (default)",
    )

    p_vis_shot = visualizer_sub.add_parser(
        "shotlist",
        help="Open the shotlist visualizer GUI (all films)",
    )
    p_vis_shot.add_argument(
        "--media", choices=["movies", "gameplay"], default="movies",
        help="Media type (default: movies)",
    )
    p_vis_shot.add_argument(
        "--filename",
        help="Open (or jump to) a specific film by filename",
    )
    p_vis_shot.add_argument(
        "--verbose",
        action="store_true",
        help="Enable verbose logging in the shotlist visualizer",
    )

    p_vis_composition = visualizer_sub.add_parser(
        "composition",
        help="Open the interactive composition visualizer",
    )
    p_vis_composition.add_argument(
        "query", nargs="?", default="",
        help="Optional initial search query",
    )
    p_vis_composition.add_argument(
        "--media", choices=["movies", "gameplay"], default="movies",
        help="Media type (default: movies)",
    )
    p_vis_composition.set_defaults(visualizer=True, no_open=False, orientation="portrait", output=None, notify=False)

    p_vis_mosaic = visualizer_sub.add_parser(
        "mosaic",
        help="Open the interactive mosaic explorer GUI",
    )
    p_vis_mosaic.add_argument(
        "--media", choices=["movies", "gameplay"], default="movies",
        help="Media type (default: movies)",
    )

    visualizer_sub.add_parser(
        "metadata",
        help="Open the metadata browser GUI (movies and gameplay cards)",
    )

    return parser


def main():
    parser = build_parser()
    if len(sys.argv) == 1:
        parser.print_help()
        return
    args, unknown = parser.parse_known_args()
    # argparse nargs="*" positionals don't consume tokens that appear after flags;
    # append any unrecognized *positional* tokens to the scope list when present,
    # and error-out on any unrecognized options.
    if unknown:
        unrecognized_opts = [u for u in unknown if u.startswith("-")]
        if unrecognized_opts:
            parser.error(f"unrecognized arguments: {' '.join(unrecognized_opts)}")
        if hasattr(args, "scope"):
            args.scope = list(args.scope or []) + [u for u in unknown if not u.startswith("-")]
    args.func(args)


if __name__ == "__main__":
    main()
