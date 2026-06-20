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

from tool import prefs
from tool.helpers import _add_media_arg, _add_tmdb_arg, _add_verbose_arg, _add_dry_run_arg, normalize_media_type

_MEDIA_FOLDER = {"movie": "movie", "movies": "movie", "gameplay": "gameplay"}
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
    "frame_match": "model_frame_match",
}
_MODEL_DEFAULTS = {
    "annotate": "gemma4-e4b",
    "segmentation": "sam3.pt",
    "embed": "BAAI/bge-small-en-v1.5",
    "frame_match": "clip-vit-base-patch32",
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
    selected_media_type = normalize_media_type(selector) if selector else None

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
        selected_media_type = "movie"

    if not do_all and (not selected_media_type or not query):
        print("✗ Provide either --all, or: crossing media channels {movie|gameplay} <query>", file=sys.stderr)
        sys.exit(1)

    media_types = [selected_media_type] if selected_media_type else ["movie", "gameplay"]

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
    selected_media_type = normalize_media_type(selector) if selector else None

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

    media_types = [selected_media_type] if selected_media_type else ["movie", "gameplay"]

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
        from services.vocabulary_format import format_vocabulary_items, format_vocabulary_map
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
        media_type = normalize_media_type(getattr(args, "media", "movie"))
        project_path = prefs.get("path")
        output_format = getattr(args, "output_format", "auto")
        top = getattr(args, "top", None)

        exclude_fields = set(getattr(args, "exclude", None) or [])

        if all_fields:
            # Use the vocabulary allowlist from fields.yaml, falling back to a
            # live scan of annotation keys when the config is absent.
            try:
                from data.index import load_vocabulary_fields as _load_vf
                vocab_fields_list = _load_vf(project_path)
            except (FileNotFoundError, Exception):
                vocab_fields_list = []

            if not vocab_fields_list:
                # Fallback: discover fields from annotation files directly.
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
                vocab_fields_list = sorted(fields_seen)

            if not vocab_fields_list:
                print(json.dumps({}))
                return
            scopes_arg = scopes or None
            output: dict = {}
            for f in vocab_fields_list:
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
                print(format_vocabulary_map(output, fmt=output_format, top=top))
            return

        if not remaining:
            print("error: 'vocabulary' requires a field name, 'fields', or --all-fields", file=sys.stderr)
            print("  crossing search vocabulary fields          # list configured vocabulary fields", file=sys.stderr)
            print("  crossing search vocabulary objects         # list values for a field", file=sys.stderr)
            print("  crossing search vocabulary --all-fields    # all fields as JSON", file=sys.stderr)
            sys.exit(1)

        # `crossing search vocabulary fields` — list the configured vocabulary field names.
        if remaining[0] == "fields":
            from services.vocabulary_index import get_vocabulary_fields
            try:
                fields_list = get_vocabulary_fields(project_path, media_type)
            except FileNotFoundError as exc:
                print(f"error: {exc}", file=sys.stderr)
                sys.exit(1)
            # For fields listing, table/list/auto just print one per line; json stays JSON.
            fmt = output_format if output_format == "json" else "list"
            if fmt == "json":
                print(json.dumps(fields_list, indent=2))
            else:
                print("\n".join(fields_list))
            return

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
            print(format_vocabulary_items(result, fmt=output_format, field_name=field, top=top))
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
        media_type = normalize_media_type(getattr(args, "media", "movie"))
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

    # Dispatch: `crossing search frame <clip-query> [scope...]`
    # Runs text search then enriches each result with a CLIP-based best frame.
    if args.query == "frame":
        from services.search import search_shots
        from services.frame_match import _load_clip_model, find_query_best_frame_for_shot
        remaining = args.scope or []
        if not remaining:
            print(
                "error: 'search frame' requires a query, "
                "e.g. crossing search frame \"horse\"",
                file=sys.stderr,
            )
            sys.exit(1)
        clip_query = remaining[0]
        scopes = (remaining[1:] or []) + (getattr(args, "movie", None) or [])
        scopes = scopes or None
        use_all = getattr(args, "all", False)
        limit = getattr(args, "limit", None)
        limit_per_item = getattr(args, "limit_per_item", None)
        media_type = normalize_media_type(getattr(args, "media", "movie"))
        project_path = prefs.get("path")
        model_name = (
            getattr(args, "model", None)
            or prefs.get(_MODEL_KEYS["frame_match"], _MODEL_DEFAULTS["frame_match"])
        )

        search_result = search_shots(
            query=clip_query,
            scopes=scopes,
            field=None,
            limit=limit,
            limit_per_item=limit_per_item,
            use_all=use_all,
            project_path=project_path,
            media_type=media_type,
        )

        results = search_result.get("results", [])
        if results:
            model, processor, device = _load_clip_model(project_path, model_name)
            for r in results:
                try:
                    frame, score = find_query_best_frame_for_shot(
                        project_path=project_path,
                        filename=r["filename"],
                        shot_id=r["shot_id"],
                        query=clip_query,
                        media_type=media_type,
                        model=model,
                        processor=processor,
                        device=device,
                    )
                    r["best_frame"] = frame
                    r["best_score"] = round(score, 6)
                except Exception:
                    pass

        print(json.dumps(search_result, indent=2))
        return

    # Dispatch: `crossing search motifs compare|list [options]`
    if args.query == "motifs":
        _search_motifs(args)
        return

    # Dispatch: `crossing search palette [options]`
    if args.query == "palette":
        _search_palette_cmd(args)
        return

    # Dispatch: `crossing search cooccurrence --terms A B [options]`
    if args.query == "cooccurrence":
        _search_cooccurrence_cmd(args)
        return

    # Dispatch: `crossing search frames <query>` — retrieve frame thumbnails
    if args.query == "frames":
        _search_frames_cmd(args)
        return

    # Dispatch: `crossing search palette-frames` — palette-filtered thumbnails
    if args.query == "palette-frames":
        _search_palette_frames_cmd(args)
        return

    # Dispatch: `crossing search motif-frames <motif>` — motif-matched thumbnails
    if args.query == "motif-frames":
        _search_motif_frames_cmd(args)
        return

    from services.search import search_shots

    scopes = (args.scope or []) + (getattr(args, "movie", None) or [])
    scopes = scopes or None
    use_all = getattr(args, "all", False)
    field = getattr(args, "field", None)
    limit = getattr(args, "limit", None)
    limit_per_item = getattr(args, "limit_per_item", None)
    media_type = normalize_media_type(getattr(args, "media", "movie"))

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
    elif sub == "stats":
        _meta_stats(args)


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
    media_type = normalize_media_type(getattr(args, "media", "movie"))
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
    media_type = normalize_media_type(getattr(args, "media", "movie"))
    rows = get_metadata(project_path, media_type=media_type)
    print(f"{len(rows)} {media_type}")


def _meta_list(args):
    from data.metadata import get_metadata
    project_path = prefs.get("path")
    media_type = normalize_media_type(getattr(args, "media", "movie"))

    rows = get_metadata(project_path, media_type=media_type)

    if args.year:
        rows = [r for r in rows if str(r.get("year", "")) == args.year]
    if args.director:
        needle = args.director.lower()
        rows = [r for r in rows if needle in str(r.get("director", "")).lower()]

    # Add subtitle status
    from data.subtitles import subtitle_exists as _subtitle_exists
    for row in rows:
        filename = row.get("filename", "")
        if filename:
            row["has_subtitle"] = _subtitle_exists(project_path, media_type, filename)
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
    media_type = normalize_media_type(getattr(args, "media", "movie"))
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
    from data.subtitles import subtitle_exists as _subtitle_exists

    project_path = prefs.get("path")
    media_type = normalize_media_type(getattr(args, "media", "movie"))

    video_dir     = Path(project_path) / "media" / "videos"     / media_type
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
        if not _subtitle_exists(project_path, media_type, fn):
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
        print("\u2717 --media is required: choose 'movie' or 'gameplay'", file=sys.stderr)
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

def _shotlist_create(args):
    """Create a hybrid draft shotlist (classical detection + gameplay refinement).

    Phase 1: OpenCV frame-diff (default) or TransNetV2 (--transnet).
    Phase 2: boundary nudging, long-segment splitting, short-segment merging.
    The result is written to the canonical shotlist path and optionally opened
    in the Shotlist Visualizer for review.
    """
    from services.draft_shotlist import create_draft_shotlist

    project_path = prefs.get("path")
    media_type = normalize_media_type(getattr(args, "media", "gameplay"))
    media_id = getattr(args, "media_id", None)
    force = getattr(args, "force", False)
    no_open = getattr(args, "no_open", False)
    verbose = getattr(args, "verbose", False)
    min_shot_sec = getattr(args, "min_shot_sec", 2.0)
    max_shot_sec = getattr(args, "max_shot_sec", 30.0)
    motion_threshold = getattr(args, "motion_threshold", 8.0)
    nudge_sec = getattr(args, "nudge_sec", 0.5)
    use_transnet = getattr(args, "transnet", False)

    if not media_id:
        print("✗ --media-id is required for shotlist create.", file=sys.stderr)
        sys.exit(1)

    print(f"Creating draft shotlist for {media_id} [{media_type}] …")

    try:
        csv_path = create_draft_shotlist(
            project_path,
            media_id,
            media_type,
            force=force,
            min_shot_sec=min_shot_sec,
            max_shot_sec=max_shot_sec,
            motion_threshold=motion_threshold,
            nudge_sec=nudge_sec,
            use_transnet=use_transnet,
            verbose=verbose,
        )
    except (ValueError, FileNotFoundError, FileExistsError, RuntimeError) as exc:
        print(f"✗ {exc}", file=sys.stderr)
        sys.exit(1)
    except Exception as exc:
        print(f"✗ {exc}", file=sys.stderr)
        import traceback
        traceback.print_exc()
        sys.exit(1)

    print(f"✓ Draft shotlist saved: {csv_path}")

    if getattr(args, "notify", False):
        from services.notify import discord_notify
        discord_notify(
            f"✓ Draft shotlist created: {media_id} [{media_type}]\n"
            f"Saved to: {csv_path}",
            project_path,
        )

    if no_open:
        return

    # Open the result in the Shotlist Visualizer
    print("  Opening Shotlist Visualizer …")
    from data.metadata import get_metadata
    entries = get_metadata(project_path, media_type=media_type)
    record = next((e for e in entries if e.get("media_id") == media_id), None)
    if record is None:
        print("  (Could not resolve filename for visualizer — skipping.)", file=sys.stderr)
        return

    _require_visualizer_deps()

    import subprocess as _sp
    cli_dir = Path(__file__).parent
    visualizer_path = cli_dir / "visualizers" / "shot_visualizer.py"
    cmd = [
        sys.executable, str(visualizer_path),
        "--media",     media_type,
        "--project",   project_path,
        "--filenames", record["filename"],
    ]
    if verbose:
        cmd.append("--verbose")
    _sp.Popen(cmd)


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
    if sub == "create":
        _shotlist_create(args)
    elif sub == "list":
        _shotlist_list(args)
    elif sub == "get":
        _shotlist_get(args)
    elif sub == "show":
        _shotlist_show(args)
    elif sub == "shot":
        sub2 = args.shot_subcommand
        if sub2 == "detect":
            _shot_detect(args)
    elif sub == "scene":
        sub2 = args.scene_subcommand
        if sub2 == "detect":
            _scene_detect(args)
    elif sub == "migrate":
        _shotlist_migrate(args)
    elif sub == "context":
        _shotlist_context(args)
    elif sub == "context-frames":
        _shotlist_context_frames(args)


def _print_dry_run_result(result: dict) -> None:
    """Print proposed scene boundaries for a single movie (dry-run mode)."""
    title = Path(result["filename"]).stem
    scenes = result.get("scenes", 0)
    assignments = result.get("scene_assignments", [])

    print(f"{title}")
    print(f"  Proposed scenes: {scenes}")
    print()

    if assignments:
        # Group consecutive shots into (scene_num, first_shot, last_shot)
        scene_groups: dict[int, tuple[int, int]] = {}
        for i, sn in enumerate(assignments):
            if sn not in scene_groups:
                scene_groups[sn] = (i, i)
            else:
                start, _ = scene_groups[sn]
                scene_groups[sn] = (start, i)
        for sn in sorted(scene_groups):
            start, end = scene_groups[sn]
            print(f"  Scene {sn}: shots {start}–{end}")
    print()


def _scene_detect(args):
    """Detect scene boundaries using shot embeddings."""
    from services.scene_detection import detect_scenes_for_movie, detect_scenes_for_all_movies
    from data.shotlist import read_shotlist, write_shotlist, resolve_filename

    project_path = prefs.get("path")
    media_type = normalize_media_type(getattr(args, "media", "movie"))
    force = getattr(args, "force", False)
    dry_run = getattr(args, "dry_run", False)
    verbose = getattr(args, "verbose", False)

    try:
        if getattr(args, "all", False):
            summary = detect_scenes_for_all_movies(
                project_path,
                media_type,
                force=force,
            )

            for result in summary["results"]:
                fn = result["filename"]
                if result.get("error"):
                    print(f"\n{Path(fn).stem}")
                    print(f"  ERROR: {result['error']}")
                elif result.get("skipped"):
                    reason = result.get("reason", "skipped")
                    print(f"\n{Path(fn).stem}")
                    print(f"  skipped ({reason})")
                else:
                    if dry_run:
                        _print_dry_run_result(result)
                    else:
                        print(f"\n{Path(fn).stem}")
                        print(f"  shots: {result['shots']}")
                        print(f"  scenes: {result['scenes']}")
                        print(f"  boundaries: {result['boundaries']}")
                        if verbose:
                            for pos in result.get("boundary_positions", []):
                                print(f"    scene boundary at shot {pos}")

            print()
            if dry_run:
                print(f"Dry run — {summary['processed']} movies")
            else:
                # Write scene numbers for non-skipped results
                for result in summary["results"]:
                    if not result.get("skipped") and not result.get("error"):
                        fn = result["filename"]
                        shots = read_shotlist(project_path, fn, media_type)
                        assignments = result["scene_assignments"]
                        for i, shot in enumerate(shots):
                            if i < len(assignments):
                                shot["Scene"] = str(assignments[i])
                        write_shotlist(project_path, fn, media_type, shots)

                print(f"Processed: {summary['processed']} movies")
                print(f"Updated:   {summary['updated']}")
                print(f"Skipped:   {summary['skipped']}")
                print(f"Failed:    {summary['failed']}")

            if summary["failed"]:
                sys.exit(1)
            return

        # Single movie
        tmdb = getattr(args, "tmdb", None)
        query_parts = getattr(args, "query", None)
        if isinstance(query_parts, list):
            query = " ".join(query_parts).strip() if query_parts else None
        else:
            query = query_parts

        if not tmdb and not query:
            print("✗ Provide a film title or --tmdb (or use --all)", file=sys.stderr)
            sys.exit(1)

        try:
            filename = resolve_filename(project_path, tmdb, query, media_type)
        except ValueError as exc:
            print(f"✗ {exc}", file=sys.stderr)
            sys.exit(1)

        title = Path(filename).stem

        result = detect_scenes_for_movie(
            project_path,
            filename,
            media_type,
            force=force,
        )

        if result.get("skipped"):
            reason = result.get("reason", "skipped")
            print(f"{title}")
            print(f"  skipped ({reason})")
            return

        if dry_run:
            _print_dry_run_result(result)
            return

        # Write scene numbers back to the shotlist
        shots = read_shotlist(project_path, filename, media_type)
        assignments = result["scene_assignments"]
        for i, shot in enumerate(shots):
            if i < len(assignments):
                shot["Scene"] = str(assignments[i])
        write_shotlist(project_path, filename, media_type, shots)

        print(f"{title}")
        print(f"  shots: {result['shots']}")
        print(f"  scenes: {result['scenes']}")
        print(f"  boundaries: {result['boundaries']}")
        if verbose:
            for pos in result.get("boundary_positions", []):
                print(f"    scene boundary at shot {pos}")

    except FileNotFoundError as exc:
        print(f"✗ {exc}", file=sys.stderr)
        sys.exit(1)
    except Exception as exc:
        print(f"✗ {exc}", file=sys.stderr)
        import traceback
        traceback.print_exc()
        sys.exit(1)


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
        if r.get("added_shot_id"):
            print(f"    Added:    shot_id")
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


# Fields whose values must always be lists, never a plain string.
_ANNOTATION_ARRAY_FIELDS = {"type", "humans", "action", "wearing", "animals", "objects", "text"}


def _repair_annotation_file(project_path, filename, media_type, label_fields, *, dry_run=False):
    """Normalize a single annotation JSON file in-place.

    Returns ``(fixes, invalid)`` where *fixes* is the number of fields
    corrected and *invalid* is ``True`` when the file could not be parsed.
    """
    from data.annotate import normalize_label_list

    ann_path = Path(project_path) / "data" / "annotations" / "shots" / media_type / f"{Path(filename).stem}.json"
    if not ann_path.exists():
        return 0, False

    raw = ann_path.read_text(encoding="utf-8")
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return 0, True

    if not isinstance(data, list):
        return 0, True

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
        for field in _ANNOTATION_ARRAY_FIELDS:
            val = ann.get(field)
            if isinstance(val, str) and "," in val:
                parts = [p.strip() for p in val.split(",") if p.strip()]
                ann[field] = parts
                file_fixes += 1
            elif isinstance(val, list) and field in label_fields:
                normalized = normalize_label_list(val, field, label_fields=label_fields)
                if normalized != val:
                    ann[field] = normalized
                    file_fixes += 1

    if file_fixes > 0 and not dry_run:
        ann_path.write_text(
            json.dumps(data, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
    return file_fixes, False


def _auto_index_and_motif(
    project_path: str,
    filename: str,
    media_type: str,
    *,
    verbose: bool = False,
) -> None:
    """Run motif generation then annotation-embedding index for one file after annotation."""
    from pathlib import Path

    stem = Path(filename).stem
    embed_model = prefs.get(_MODEL_KEYS["embed"], _MODEL_DEFAULTS["embed"])
    motif_model = prefs.get(_MODEL_KEYS["annotate"], _MODEL_DEFAULTS["annotate"])

    # --- Motif generation (must run before annotation-embeddings so motif is included) ---
    print(f"  [{stem}] Running motif generation…")
    motif_ok = False
    try:
        from data.motif import generate_motifs_for_movie
        motif_summary = generate_motifs_for_movie(
            project_path,
            filename,
            media_type,
            model_name=motif_model,
            force=False,
            verbose=verbose,
        )
        n_gen = motif_summary.get("processed", 0)
        n_skip = motif_summary.get("skipped", 0)
        n_fail = motif_summary.get("failed", 0)
        print(f"  [{stem}] Motifs: generated={n_gen} skipped={n_skip} failed={n_fail}")
        motif_ok = True
    except FileNotFoundError as _exc:
        print(f"  ✗ [{stem}] Motif generation skipped: {_exc}", file=sys.stderr)
    except Exception as _exc:
        print(f"  ✗ [{stem}] Motif generation error: {_exc}", file=sys.stderr)

    # --- Annotation-embedding index (force=True so motif field is always included) ---
    print(f"  [{stem}] Running annotation-embedding index…")
    try:
        result = _update_one_film(
            project_path, filename, media_type, embed_model,
            force=motif_ok, verbose=verbose,
        )
        if result == "ok":
            print(f"  [{stem}] Index updated.")
        elif result == "skip":
            print(f"  [{stem}] Index up to date.")
        else:
            print(f"  ✗ [{stem}] Index processing failed.", file=sys.stderr)
    except Exception as _exc:
        print(f"  ✗ [{stem}] Index processing error: {_exc}", file=sys.stderr)


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
                    # --- frame matching after shot annotation (--with-frame or auto) ---
                    if getattr(args, "with_frame", False) or not getattr(args, "no_best", False):
                        fn = summary.get("filename")
                        if fn:
                            import gc
                            gc.collect()
                            try:
                                import torch
                                if torch.cuda.is_available():
                                    torch.cuda.empty_cache()
                            except Exception:
                                pass
                            _frame_model = getattr(args, "frame_model", None) or prefs.get(_MODEL_KEYS["frame_match"], _MODEL_DEFAULTS["frame_match"])
                            if getattr(args, "verbose", False):
                                print(f"\n  [frame matching: {fn}]")
                            try:
                                from services.frame_match import annotate_best_frames
                                frame_summary = annotate_best_frames(
                                    project_path,
                                    fn,
                                    media_type=args.media,
                                    model_name=_frame_model,
                                    force=getattr(args, "force", False),
                                    verbose=getattr(args, "verbose", False),
                                )
                                low_conf = frame_summary.get("low_confidence", [])
                                low_conf_str = f" low_confidence={len(low_conf)}" if low_conf else ""
                                print(f"  {fn} (frames): updated={frame_summary.get('updated', 0)} skipped={frame_summary.get('skipped', 0)}{low_conf_str}")
                            except FileNotFoundError as _exc:
                                print(f"  ✗ Frame matching skipped for {fn}: {_exc}", file=sys.stderr)
                            except Exception as _exc:
                                print(f"  ✗ Frame matching failed for {fn}: {_exc}", file=sys.stderr)
                            gc.collect()
                            try:
                                import torch
                                if torch.cuda.is_available():
                                    torch.cuda.empty_cache()
                            except Exception:
                                pass

                    # Post-annotation normalisation
                    _fn = summary.get("filename")
                    if _fn:
                        from data.annotate import load_label_list_fields
                        _label_fields = load_label_list_fields(project_path)
                        _val_fixes, _ = _repair_annotation_file(project_path, _fn, args.media, _label_fields)
                        if _val_fixes and getattr(args, "verbose", False):
                            print(f"  {_fn}: normalised {_val_fixes} annotation field(s)")
                        # Auto index processing + motif generation
                        _auto_index_and_motif(project_path, _fn, args.media, verbose=getattr(args, "verbose", False))

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

            def _notify_annotation_failure(shot_index, reason):
                try:
                    from services.notify import discord_notify
                    discord_notify(f"✗ Annotate failed: {filename} shot {shot_index} — {reason}", project_path)
                except Exception:
                    pass

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
                on_failure=_notify_annotation_failure if getattr(args, "notify", False) else None,
            )
            failed_count = len(summary.get("failed", [])) if summary.get("failed") else 0
            print(f"✓ Annotated: {filename} — updated={summary['updated']} skipped={summary['skipped']} failed={failed_count}")
            if getattr(args, "notify", False) and (summary.get("updated", 0) > 0 or failed_count > 0):
                try:
                    from services.notify import discord_notify
                    discord_notify(f"✓ Annotated: {filename} — updated={summary['updated']} skipped={summary['skipped']} failed={failed_count}", project_path)
                except Exception:
                    pass

            # Automatic best-frame detection (skip with --no-best)
            if not getattr(args, "no_best", False):
                _frame_model = getattr(args, "frame_model", None) or prefs.get(_MODEL_KEYS["frame_match"], _MODEL_DEFAULTS["frame_match"])
                print("Running best-frame detection...")
                try:
                    from services.frame_match import annotate_best_frames
                    frame_summary = annotate_best_frames(
                        project_path,
                        filename,
                        media_type=args.media,
                        model_name=_frame_model,
                        force=False,
                        verbose=getattr(args, "verbose", False),
                    )
                    skipped_bf = frame_summary.get("skipped", 0)
                    updated_bf = frame_summary.get("updated", 0)
                    low_conf = frame_summary.get("low_confidence", [])
                    if skipped_bf:
                        print(f"Skipping existing best frames: {skipped_bf}")
                    if updated_bf:
                        print(f"Updated best frames: {updated_bf}")
                    if low_conf:
                        print(f"Low-confidence best frames: {len(low_conf)}")
                except FileNotFoundError as _exc:
                    print(f"  ✗ Best-frame detection skipped: {_exc}", file=sys.stderr)
                except Exception as _exc:
                    print(f"  ✗ Best-frame detection failed: {_exc}", file=sys.stderr)
            # Post-annotation normalisation (always runs, no --no-best flag needed)
            from data.annotate import load_label_list_fields
            _label_fields = load_label_list_fields(project_path)
            _val_fixes, _val_invalid = _repair_annotation_file(project_path, filename, args.media, _label_fields)
            if _val_fixes:
                print(f"Normalised {_val_fixes} annotation field(s)")
            # Auto index processing + motif generation
            _auto_index_and_motif(project_path, filename, args.media, verbose=getattr(args, "verbose", False))
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
            # Auto index processing + motif generation
            _auto_index_and_motif(project_path, filename, args.media, verbose=getattr(args, "verbose", False))
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
        if media_type == "gameplay":
            # Gameplay shotlists may not exist yet — show all gameplay entries;
            # the visualizer itself will display a graceful "no shotlist yet" message.
            filenames = [e['filename'] for e in entries if e.get('filename')]
            if not filenames:
                print("✗ Error: No gameplay metadata found.", file=sys.stderr)
                sys.exit(1)
        else:
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
    if getattr(args, "play", False):
        cmd.append("--play")
    if getattr(args, "loop", False):
        cmd.append("--loop")
    if getattr(args, "no_continue", False):
        cmd.append("--no-continue")

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
        elif key == "atomic-fields":
            project_path = prefs.get("path")
            if not project_path:
                print("✗ No project path set. Run: crossing tool path <path>", file=sys.stderr)
                sys.exit(1)
            from data.index import save_atomic_fields
            field_list = [f.strip() for f in args.value.split(",") if f.strip()]
            save_atomic_fields(project_path, field_list)
            print(f"✓ Atomic label fields set to: {', '.join(field_list)}")
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
        elif key == "atomic-fields":
            project_path = prefs.get("path")
            if not project_path:
                print("atomic-fields: (no project path set)", file=sys.stderr)
                sys.exit(1)
            try:
                from data.index import load_atomic_fields
                fields = load_atomic_fields(project_path)
                print(f"atomic-fields: {', '.join(fields)}")
            except (ValueError, ImportError) as exc:
                print(f"atomic-fields: (error loading — {exc})")
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
                # Show atomic label fields
                if project_path:
                    try:
                        from data.index import load_atomic_fields
                        atomic = load_atomic_fields(project_path)
                        print(f"atomic-fields: {', '.join(atomic)}")
                    except (ValueError, ImportError) as exc:
                        print(f"atomic-fields: (error loading — {exc})")
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

    from data.subtitles import subtitle_exists as _subtitle_exists
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

        if _subtitle_exists(project_path, media_type, filename) and not force:
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
    from data.subtitles import subtitle_exists as _subtitle_exists

    project_path = prefs.get("path")
    media_type = args.media

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
        has = _subtitle_exists(project_path, media_type, filename)
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


def _annotate_frame(args):
    """Dispatch: ``crossing annotate frame <query>``

    For each shot in the film's annotation JSON, use a CLIP model to find the
    frame that best matches the shot's ``description`` field, save it as a PNG,
    and store ``best_frame`` metadata in the annotation JSON.
    """
    _require_path()
    project_path = prefs.get("path")
    media_type = normalize_media_type(getattr(args, "media", "movie"))

    model_name = (
        getattr(args, "model", None)
        or prefs.get(_MODEL_KEYS["frame_match"], _MODEL_DEFAULTS["frame_match"])
    )

    # --all: run frame matching for every registered movie
    if getattr(args, "all", False):
        _notify_items = getattr(args, "notify_items", False)
        if _notify_items:
            args.notify = True  # --notify-each implies --notify

        from data.metadata import get_metadata
        try:
            from services.frame_match import annotate_best_frames
        except ImportError as exc:
            print(f"✗ {exc}", file=sys.stderr)
            sys.exit(1)

        entries = get_metadata(project_path, media_type=media_type)
        total_updated = 0
        total_skipped = 0
        all_summaries = []
        for e in entries:
            fn = e.get("filename")
            if not fn:
                continue
            try:
                summary = annotate_best_frames(
                    project_path,
                    fn,
                    media_type=media_type,
                    model_name=model_name,
                    force=getattr(args, "force", False),
                    verbose=getattr(args, "verbose", False),
                )
            except FileNotFoundError as exc:
                print(f"  ✗ Skipping {fn}: {exc}", file=sys.stderr)
                continue
            except Exception as exc:
                print(f"  ✗ Error on {fn}: {exc}", file=sys.stderr)
                continue
            total_updated += summary.get("updated", 0)
            total_skipped += summary.get("skipped", 0)
            low_conf = summary.get("low_confidence", [])
            low_conf_str = f" low_confidence={len(low_conf)}" if low_conf else ""
            print(f"  {fn}: updated={summary.get('updated', 0)} skipped={summary.get('skipped', 0)}{low_conf_str}")
            all_summaries.append({"filename": fn, **summary})
            if _notify_items and summary.get("updated", 0) > 0:
                try:
                    from services.notify import discord_notify
                    discord_notify(
                        f"✓ Frame match: {fn}\nupdated={summary.get('updated', 0)} skipped={summary.get('skipped', 0)}{low_conf_str}",
                        project_path,
                    )
                except Exception:
                    pass
        print(f"✓ Batch complete: updated={total_updated} skipped={total_skipped}")
        if getattr(args, "notify", False):
            try:
                from services.notify import discord_notify
                lines = [f"Frame match batch complete: {len(all_summaries)} file(s)"]
                for s in all_summaries:
                    low_conf = s.get("low_confidence", [])
                    low_conf_str = f" low_confidence={len(low_conf)}" if low_conf else ""
                    lines.append(f"{s.get('filename')}: updated={s.get('updated', 0)} skipped={s.get('skipped', 0)}{low_conf_str}")
                discord_notify("\n".join(lines), project_path)
            except Exception:
                pass
        return

    from data.shotlist import resolve_filename
    try:
        filename = resolve_filename(
            project_path,
            getattr(args, "tmdb", None),
            getattr(args, "filename", None),
            media_type,
        )
    except Exception as exc:
        print(f"✗ Could not resolve film: {exc}", file=sys.stderr)
        sys.exit(1)

    try:
        from services.frame_match import annotate_best_frames
        summary = annotate_best_frames(
            project_path,
            filename,
            media_type=media_type,
            model_name=model_name,
            force=getattr(args, "force", False),
            verbose=getattr(args, "verbose", False),
        )
    except ImportError as exc:
        print(f"✗ {exc}", file=sys.stderr)
        sys.exit(1)
    except FileNotFoundError as exc:
        print(f"✗ {exc}", file=sys.stderr)
        sys.exit(1)
    except Exception as exc:
        print(f"✗ {exc}", file=sys.stderr)
        sys.exit(1)

    low_conf = summary.get("low_confidence", [])
    if low_conf and getattr(args, "verbose", False):
        sample = ", ".join(low_conf[:5])
        tail = f" (and {len(low_conf) - 5} more)" if len(low_conf) > 5 else ""
        print(f"  Low confidence ({len(low_conf)}): {sample}{tail}")

    print(f"✓ Processed: {filename}")
    print(f"  updated: {summary.get('updated', 0)}")
    print(f"  skipped: {summary.get('skipped', 0)}")


def _annotate_remove(args):
    """Remove shot-annotation JSON for one or all films."""
    from data.annotate import remove_file_annotations
    from data.shotlist import resolve_filename

    _require_path()
    project_path = prefs.get("path")
    media_type = normalize_media_type(getattr(args, "media", "movie"))

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


def _annotate_best(args):
    """Dispatch: ``crossing annotate best <subcommand>``."""
    best_action = getattr(args, "best_action", None)
    if best_action == "migrate":
        _require_path()
        project_path = prefs.get("path")
        media_type = normalize_media_type(getattr(args, "media", "movie"))
        try:
            from services.frame_match import migrate_best_frame_sources
            summary = migrate_best_frame_sources(project_path, media_type)
        except Exception as exc:
            print(f"✗ {exc}", file=sys.stderr)
            sys.exit(1)
        print(
            f"✓ Migration complete: "
            f"{summary['shots_updated']} shots updated "
            f"across {summary['files_updated']} file(s)"
        )
        return
    print("✗ annotate best: specify a subcommand (e.g. migrate)", file=sys.stderr)
    sys.exit(1)


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
    media_type   = normalize_media_type(getattr(args, "media", "movie"))

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
    - For atomic label fields (configured via ``atomic-fields`` in fields.yaml),
      also splits comma-joined items within lists
      (e.g. ``["pronghorn, pronghorns"]`` → ``["pronghorn", "pronghorns"]``)
      and deduplicates.
    """
    _require_path()
    from data.shotlist import resolve_filename
    from data.annotate import load_label_list_fields

    project_path = prefs.get("path")
    media_type   = normalize_media_type(getattr(args, "media", "movie"))
    dry_run      = getattr(args, "dry_run", False)

    label_fields = load_label_list_fields(project_path)

    if getattr(args, "all", False):
        from data.metadata import get_metadata
        entries   = get_metadata(project_path, media_type=media_type)
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
        ann_path = Path(project_path) / "data" / "annotations" / "shots" / media_type / f"{Path(fn).stem}.json"
        if not ann_path.exists():
            print(f"  ?  {fn}  (no annotation file)")
            continue
        total_files += 1
        fixes, invalid = _repair_annotation_file(project_path, fn, media_type, label_fields, dry_run=dry_run)
        if invalid:
            print(f"  ✗  {fn}: invalid JSON or unexpected structure")
            invalid_files += 1
        elif fixes > 0:
            verb = "would fix" if dry_run else "fixed"
            print(f"  ✓  {fn}: {verb} {fixes} field(s)")
            total_fixes += fixes
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
    media_type   = normalize_media_type(getattr(args, "media", "movie"))

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
    elif sub == "cloud":
        cmd_cloud(args)
    elif sub == "flipbook":
        cmd_flipbook(args)
    elif sub == "film-title":
        cmd_film_title(args)


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
    elif sub == "video":
        _mosaic_video(args)
    elif sub == "shots":
        _mosaic_shots(args)
    elif sub == "scenes":
        _mosaic_scenes(args)


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


def _mosaic_shots(args):
    """mosaic shots [--title TITLE | --all] — PDF contact sheet of every shot."""
    import subprocess
    from data.metadata import get_metadata
    from generators.mosaic import mosaic_pdf_from_shots

    project_path  = prefs.get("path")
    media_type    = normalize_media_type(getattr(args, "media", "movie"))
    best_mode     = getattr(args, "best", False)
    output_path   = getattr(args, "output", None)
    open_result   = not getattr(args, "no_open", False)
    verbose       = getattr(args, "verbose", False)
    notify        = getattr(args, "notify", False)
    notify_items  = getattr(args, "notify_items", False)
    movie_query   = getattr(args, "movie", None)
    use_all       = getattr(args, "all", False)

    # Resolve target filenames
    if use_all:
        rows = get_metadata(project_path, media_type=media_type)
        filenames = [r["filename"] for r in rows if r.get("filename")]
        if not filenames:
            print(f"✗ No {media_type} metadata found.", file=sys.stderr)
            sys.exit(1)
    elif movie_query:
        rows = get_metadata(project_path, movie_query, media_type=media_type)
        if not rows:
            print(f"✗ No match for {movie_query!r}.", file=sys.stderr)
            sys.exit(1)
        filenames = [rows[0]["filename"]]
    else:
        print("✗ mosaic shots: specify --title TITLE or --all.", file=sys.stderr)
        sys.exit(1)

    mode_label = " (best)" if best_mode else ""
    print(f"Building shots PDF{mode_label} for {len(filenames)} movie(s)…")

    for filename in filenames:
        stem = Path(filename).stem
        print(f"  {stem}…")
        try:
            out = mosaic_pdf_from_shots(
                project_path, filename,
                best_mode=best_mode,
                output_path=output_path if len(filenames) == 1 else None,
                verbose=verbose,
            )
            print(f"  ✓ Saved: {out}")
            if open_result and len(filenames) == 1:
                subprocess.Popen(["xdg-open", str(out)])
            if notify_items:
                from services.notify import discord_notify
                stem_label = Path(filename).stem
                discord_notify(f"✓ Mosaic shots: {stem_label}\nSaved: {out.name}", project_path)
        except ValueError as exc:
            print(f"  ✗ {exc}", file=sys.stderr)
            if notify_items:
                from services.notify import discord_notify
                discord_notify(f"✗ Mosaic shots failed: {Path(filename).stem}\n{exc}", project_path)
        except Exception as exc:
            import traceback
            print(f"  ✗ Render failed: {exc}", file=sys.stderr)
            traceback.print_exc()
            if notify_items:
                from services.notify import discord_notify
                discord_notify(f"✗ Mosaic shots failed: {Path(filename).stem}\n{exc}", project_path)

    if open_result and len(filenames) > 1:
        subprocess.Popen(["xdg-open", str(Path(project_path) / "output" / "mosaics")])

    if notify:
        from services.notify import discord_notify
        mode_label = " (best)" if best_mode else ""
        discord_notify(
            f"✓ Mosaic shots{mode_label} complete — {len(filenames)} movie(s)",
            project_path,
        )


def _mosaic_scenes(args):
    """mosaic scenes [--title TITLE | --all] — PDF with scene intertitles."""
    import subprocess
    from data.metadata import get_metadata
    from generators.mosaic import mosaic_pdf_from_scenes

    project_path  = prefs.get("path")
    media_type    = normalize_media_type(getattr(args, "media", "movie"))
    best_mode     = getattr(args, "best", False)
    output_path   = getattr(args, "output", None)
    open_result   = not getattr(args, "no_open", False)
    verbose       = getattr(args, "verbose", False)
    notify        = getattr(args, "notify", False)
    notify_items  = getattr(args, "notify_items", False)
    movie_query   = getattr(args, "movie", None)
    use_all       = getattr(args, "all", False)

    # Resolve target filenames
    if use_all:
        rows = get_metadata(project_path, media_type=media_type)
        filenames = [r["filename"] for r in rows if r.get("filename")]
        if not filenames:
            print(f"✗ No {media_type} metadata found.", file=sys.stderr)
            sys.exit(1)
    elif movie_query:
        rows = get_metadata(project_path, movie_query, media_type=media_type)
        if not rows:
            print(f"✗ No match for {movie_query!r}.", file=sys.stderr)
            sys.exit(1)
        filenames = [rows[0]["filename"]]
    else:
        print("✗ mosaic scenes: specify --title TITLE or --all.", file=sys.stderr)
        sys.exit(1)

    mode_label = " (best)" if best_mode else ""
    print(f"Building scenes PDF{mode_label} for {len(filenames)} movie(s)…")

    for filename in filenames:
        stem = Path(filename).stem
        print(f"  {stem}…")
        try:
            out = mosaic_pdf_from_scenes(
                project_path, filename,
                best_mode=best_mode,
                output_path=output_path if len(filenames) == 1 else None,
                verbose=verbose,
            )
            print(f"  ✓ Saved: {out}")
            if open_result and len(filenames) == 1:
                subprocess.Popen(["xdg-open", str(out)])
            if notify_items:
                from services.notify import discord_notify
                stem_label = Path(filename).stem
                discord_notify(f"✓ Mosaic scenes: {stem_label}\nSaved: {out.name}", project_path)
        except ValueError as exc:
            print(f"  ✗ {exc}", file=sys.stderr)
            if notify_items:
                from services.notify import discord_notify
                discord_notify(f"✗ Mosaic scenes failed: {Path(filename).stem}\n{exc}", project_path)
        except Exception as exc:
            import traceback
            print(f"  ✗ Render failed: {exc}", file=sys.stderr)
            traceback.print_exc()
            if notify_items:
                from services.notify import discord_notify
                discord_notify(f"✗ Mosaic scenes failed: {Path(filename).stem}\n{exc}", project_path)

    if open_result and len(filenames) > 1:
        subprocess.Popen(["xdg-open", str(Path(project_path) / "output" / "mosaics")])

    if notify:
        from services.notify import discord_notify
        mode_label = " (best)" if best_mode else ""
        discord_notify(
            f"✓ Mosaic scenes{mode_label} complete — {len(filenames)} movie(s)",
            project_path,
        )


def _mosaic_search(args):
    """mosaic search <query> [scope...] — mosaic grid from shot annotation search."""
    import subprocess
    from services.search import search_shots
    from generators.mosaic import mosaic_from_search_results

    project_path = prefs.get("path")
    media_type   = normalize_media_type(getattr(args, "media", "movie"))
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


def _mosaic_video(args):
    """mosaic video <query> — generate a looping video mosaic from search results."""
    import subprocess
    from services.search import search_shots
    from generators.mosaic import mosaic_video_from_search_results

    project_path = prefs.get("path")
    query    = args.query
    limit    = getattr(args, "limit", 50)
    fps      = getattr(args, "fps", 8)
    duration = getattr(args, "duration", 2)
    layout   = getattr(args, "layout", "landscape")
    open_result = not getattr(args, "no_open", False)

    search_result = search_shots(
        query=query,
        scopes=None,
        field=None,
        limit=limit,
        limit_per_item=None,
        use_all=True,
        project_path=project_path,
    )
    results = search_result["results"]

    if not results:
        print(f"✗ No results for query '{query}'.", file=sys.stderr)
        sys.exit(1)

    print(f"Building video mosaic: {min(len(results), limit)} tile(s) (query: {query!r})…")

    try:
        out = mosaic_video_from_search_results(
            results,
            project_path,
            layout=layout,
            fps=fps,
            duration=duration,
            limit=limit,
            query=query,
        )
        print(f"✓ Saved: {out}")
        if open_result:
            subprocess.Popen(["xdg-open", str(out)])
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
    run_visualizer(
        prefs.get("path"),
        media_type=normalize_media_type(getattr(args, "media", "movie")) or "movie",
    )


def _mosaic_export(args):
    """mosaic export <query> [scope...] — export individual JPEGs for each search result."""
    import subprocess
    from services.search import search_shots
    from generators.mosaic import export_frames_from_search_results

    project_path = prefs.get("path")
    media_type   = normalize_media_type(getattr(args, "media", "movie"))
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
# cloud command family
# ---------------------------------------------------------------------------

def cmd_cloud(args):
    """Generate a word-cloud PDF from annotation text."""
    _require_path()
    if getattr(args, "visualizer", False):
        _cloud_visualizer(args)
        return

    import subprocess
    from generators.cloud import cloud_from_annotations, STYLE_NAMES, PREFS_KEY_STYLE, DEFAULT_STYLE

    project_path = prefs.get("path")
    scope        = getattr(args, "scope", None) or None
    field        = getattr(args, "field", None) or None
    media_type   = normalize_media_type(getattr(args, "media", "movie"))
    max_words    = getattr(args, "max_words", 150)
    min_count    = getattr(args, "min_count", 2)
    output_path  = getattr(args, "output", None)
    open_result  = not getattr(args, "no_open", False)
    style        = getattr(args, "style", None) or prefs.get(PREFS_KEY_STYLE) or DEFAULT_STYLE

    if getattr(args, "save_style", False):
        prefs.set(PREFS_KEY_STYLE, style)
        print(f"✓ Default cloud style saved: {style}")

    scope_label = scope or f"{media_type} (all)"
    field_label = field or "all fields"
    print(f"Building cloud: {scope_label} · {field_label} [{style}]")

    try:
        out, _ = cloud_from_annotations(
            project_path,
            scope=scope,
            field=field,
            media_type=media_type,
            output_path=output_path,
            max_words=max_words,
            min_count=min_count,
            style=style,
        )
        print(f"✓ Saved: {out}")
        if open_result:
            try:
                subprocess.Popen(["xdg-open", str(out)])
            except Exception:
                pass
        if getattr(args, "notify", False):
            from services.notify import discord_notify
            discord_notify(
                f"✓ Cloud complete: {scope_label} · {field_label} → {out.name}",
                project_path,
            )
    except FileNotFoundError as exc:
        print(f"✗ {exc}", file=sys.stderr)
        sys.exit(1)
    except ValueError as exc:
        print(f"✗ {exc}", file=sys.stderr)
        sys.exit(1)
    except Exception as exc:
        print(f"✗ Cloud failed: {exc}", file=sys.stderr)
        import traceback
        traceback.print_exc()
        sys.exit(1)


def _cloud_visualizer(args):
    """cloud --visualizer — launch the interactive word-cloud explorer GUI."""
    _require_visualizer_deps()
    from visualizers.cloud_visualizer import run_visualizer
    run_visualizer(prefs.get("path"))


# ---------------------------------------------------------------------------
# generate flipbook
# ---------------------------------------------------------------------------

def cmd_film_title(args):
    """Generate a semantic condensation title for one or all movies."""
    _require_path()
    project_path = prefs.get("path")
    media_type   = normalize_media_type(getattr(args, "media", "movie")) or "movie"
    force        = getattr(args, "force", False)
    verbose      = getattr(args, "verbose", False)
    model_name   = getattr(args, "model", "Qwen3-VL-8B-Instruct") or "Qwen3-VL-8B-Instruct"
    movie        = getattr(args, "movie", None)
    do_all       = getattr(args, "all", False)
    notify_each  = getattr(args, "notify_items", False)
    notify       = getattr(args, "notify", False) or notify_each

    from data.film_motif import (
        generate_film_title,
        generate_film_titles_for_all_movies,
        set_film_title,
    )
    from data.shotlist import resolve_filename

    # Manual override: --set VALUE --title <title>
    set_value = getattr(args, "set", None)
    if set_value is not None:
        if not movie:
            print("✗ film-title --set: specify --title", file=sys.stderr)
            sys.exit(1)
        try:
            filename = resolve_filename(project_path, None, movie, media_type)
        except Exception as exc:
            print(f"✗ Could not resolve movie: {exc}", file=sys.stderr)
            sys.exit(1)
        result = set_film_title(project_path, filename, media_type, set_value)
        print(f"✓ {result['value']}  ({filename})")
        return

    if do_all:
        print(f"Generating film titles for all {media_type}…")

        def _title_on_item(title, item_result, exc):
            if not notify_each:
                return
            from services.notify import discord_notify
            if exc is None:
                value = item_result.get("value", "?")
                discord_notify(f"✓ Film title: {title}  → {value!r}", project_path)
            else:
                discord_notify(f"✗ Film title failed: {title}  — {exc}", project_path)

        summary = generate_film_titles_for_all_movies(
            project_path,
            media_type=media_type,
            model_name=model_name,
            force=force,
            verbose=verbose,
            on_item_done=_title_on_item if notify_each else None,
        )
        print()
        print(f"  processed: {summary['processed']}")
        print(f"  skipped:   {summary['skipped']}")
        print(f"  failed:    {summary['failed']}")
        if notify:
            from services.notify import discord_notify
            discord_notify(
                f"✓ Film titles complete: "
                f"processed={summary['processed']}  "
                f"skipped={summary['skipped']}  "
                f"failed={summary['failed']}",
                project_path,
            )
        return

    if not movie:
        print("✗ film-title: specify --title or --all.", file=sys.stderr)
        sys.exit(1)

    try:
        filename = resolve_filename(project_path, None, movie, media_type)
    except Exception as exc:
        print(f"✗ Could not resolve movie: {exc}", file=sys.stderr)
        sys.exit(1)

    try:
        result = generate_film_title(
            project_path, filename, media_type,
            model_name=model_name,
            force=force,
            verbose=verbose,
        )
        print(f"✓ {result['value']}  ({filename})")
        if notify:
            from services.notify import discord_notify
            discord_notify(
                f"✓ Film title: {filename}  → {result['value']!r}",
                project_path,
            )
    except FileNotFoundError as exc:
        print(f"✗ {exc}", file=sys.stderr)
        sys.exit(1)
    except ValueError as exc:
        print(f"✗ {exc}", file=sys.stderr)
        sys.exit(1)
    except Exception as exc:
        print(f"✗ film-title failed: {exc}", file=sys.stderr)
        import traceback
        traceback.print_exc()
        sys.exit(1)


# generate flipbook
# ---------------------------------------------------------------------------

def cmd_flipbook(args):
    """Generate a cinematic motif flipbook PDF (one page per shot)."""
    _require_path()
    project_path = prefs.get("path")
    media_type   = normalize_media_type(getattr(args, "media", "movie")) or "movie"
    force        = getattr(args, "force", False)
    verbose      = getattr(args, "verbose", False)
    open_result  = not getattr(args, "no_open", False)
    notify_each  = getattr(args, "notify_items", False)
    notify       = getattr(args, "notify", False) or notify_each

    if getattr(args, "visualizer", False):
        _flipbook_visualizer(args)
        return

    import subprocess
    from generators.flipbook import (
        generate_flipbook_for_movie,
        generate_flipbook_for_all_movies,
    )
    from data.shotlist import resolve_filename

    # Determine scope
    do_all   = getattr(args, "all", False)
    movie    = getattr(args, "movie", None)
    tmdb_id  = getattr(args, "tmdb", None)

    if do_all:
        print(f"Generating flipbooks for all {media_type}…")

        def _flipbook_on_item(title, item_summary, exc):
            if not notify_each:
                return
            from services.notify import discord_notify
            if exc is None:
                pages = item_summary.get("pages", "?")
                discord_notify(f"✓ Flipbook: {title}  ({pages} pages)", project_path)
            else:
                discord_notify(f"✗ Flipbook failed: {title}  — {exc}", project_path)

        summary = generate_flipbook_for_all_movies(
            project_path, media_type=media_type,
            force=force, verbose=verbose,
            on_item_done=_flipbook_on_item if notify_each else None,
        )
        print()
        print(f"  processed: {summary['total_processed']}")
        print(f"  skipped:   {summary['total_skipped']}")
        print(f"  failed:    {summary['total_failed']}")
        if summary["errors"]:
            for fn, err in summary["errors"]:
                print(f"  ✗ {fn}: {err}", file=sys.stderr)
        if notify:
            from services.notify import discord_notify
            discord_notify(
                f"✓ Flipbook batch complete: "
                f"processed={summary['total_processed']}  "
                f"skipped={summary['total_skipped']}  "
                f"failed={summary['total_failed']}",
                project_path,
            )
        return

    # Single movie
    if not movie and not tmdb_id:
        print("✗ flipbook: specify --title, --tmdb, or --all.", file=sys.stderr)
        sys.exit(1)

    try:
        filename = resolve_filename(project_path, tmdb_id, movie, media_type)
    except Exception as exc:
        print(f"✗ Could not resolve movie: {exc}", file=sys.stderr)
        sys.exit(1)

    try:
        result = generate_flipbook_for_movie(
            project_path, filename, media_type,
            force=force, verbose=verbose,
        )
        out = result["output_path"]
        print(f"✓ Saved: {out}  ({result['pages']} pages)")
        if open_result:
            try:
                subprocess.Popen(["xdg-open", str(out)])
            except Exception:
                pass
        if notify:
            from services.notify import discord_notify
            discord_notify(
                f"✓ Flipbook: {out.name}  ({result['pages']} pages)",
                project_path,
            )
    except FileExistsError as exc:
        print(f"✗ {exc}", file=sys.stderr)
        sys.exit(1)
    except FileNotFoundError as exc:
        print(f"✗ {exc}", file=sys.stderr)
        sys.exit(1)
    except Exception as exc:
        print(f"✗ Flipbook failed: {exc}", file=sys.stderr)
        import traceback
        traceback.print_exc()
        sys.exit(1)


def _flipbook_visualizer(args):
    """flipbook --visualizer — launch the interactive flipbook GUI."""
    _require_visualizer_deps()
    from visualizers.flipbook_visualizer import run_visualizer
    run_visualizer(
        prefs.get("path"),
        media_type=normalize_media_type(getattr(args, "media", "movie")) or "movie",
    )


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
    elif sub in ("process", "annotation-embeddings"):
        _index_update(args)
    elif sub == "audit":
        _index_audit(args)
    elif sub == "vocabulary":
        _index_vocabulary(args)
    elif sub == "stats":
        _index_stats(args)
    elif sub == "silhouette":
        _index_silhouette(args)
    elif sub == "palette":
        _index_palette(args)
    elif sub == "motif":
        _index_motif(args)
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
    media_type = normalize_media_type(getattr(args, "media", "movie"))

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
    media_type = normalize_media_type(getattr(args, "media", "movie"))

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
    limit: int | None = None,
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
        if limit is not None:
            items = items[:limit]
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
    media_type = normalize_media_type(getattr(args, "media", "movie"))
    query_words = getattr(args, "query", None) or []
    query_str = (
        getattr(args, "movie", None)
        or (" ".join(query_words).strip() if query_words else None)
    )
    tmdb = getattr(args, "tmdb", None)
    do_all = getattr(args, "all", False)
    model_name = (
        getattr(args, "model", None)
        or prefs.get(_MODEL_KEYS["embed"], _MODEL_DEFAULTS["embed"])
    )
    force = getattr(args, "force", False)
    verbose = getattr(args, "verbose", False)
    limit = getattr(args, "limit", None)
    notify_each = getattr(args, "notify_items", False)
    notify = getattr(args, "notify", False) or notify_each

    if do_all:
        filenames = _resolve_all_annotation_filenames(project_path, media_type)
        if not filenames:
            print(f"No annotation JSON files found under {media_type}.", file=sys.stderr)
            sys.exit(1)
        counts = {"ok": 0, "skip": 0, "error": 0}
        for fn in filenames:
            result = _update_one_film(
                project_path, fn, media_type, model_name,
                force=force, verbose=verbose, limit=limit,
            )
            counts[result] = counts.get(result, 0) + 1
            if notify_each:
                from services.notify import discord_notify
                stem = Path(fn).stem
                if result == "ok":
                    discord_notify(f"✓ index process {media_type}/{stem} — updated", project_path)
                elif result == "error":
                    discord_notify(f"✗ index process {media_type}/{stem} — error", project_path)
        total = sum(counts.values())
        parts = []
        if counts["ok"]:
            parts.append(f"{counts['ok']} updated")
        if counts["skip"]:
            parts.append(f"{counts['skip']} current")
        if counts["error"]:
            parts.append(f"{counts['error']} error(s)")
        summary = f"{', '.join(parts)}  —  {total} total"
        print(f"\n{summary}")
        if notify:
            from services.notify import discord_notify
            discord_notify(f"index process complete ({media_type})\n{summary}", project_path)
    else:
        if tmdb is None and not query_str:
            print("✗ Provide a title query, --title <value>, --tmdb <id>, or --all.", file=sys.stderr)
            sys.exit(1)
        try:
            filename = resolve_filename(project_path, tmdb, query_str, media_type)
        except ValueError as exc:
            print(f"✗ {exc}", file=sys.stderr)
            sys.exit(1)
        result = _update_one_film(
            project_path, filename, media_type, model_name,
            force=force, verbose=verbose, limit=limit,
        )
        if notify:
            from services.notify import discord_notify
            stem = Path(filename).stem
            status = "updated" if result == "ok" else ("up to date" if result == "skip" else "error")
            discord_notify(f"index process {media_type}/{stem} — {status}", project_path)


def _index_audit(args):
    """Inspect and report index status for one film or all films. Never writes files."""
    from data.shotlist import resolve_filename

    project_path = prefs.get("path")
    media_type = normalize_media_type(getattr(args, "media", "movie"))
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


def _index_vocabulary(args):
    """Build a per-field vocabulary index from annotation JSON files."""
    from services.vocabulary_index import build_vocabulary_index

    project_path = prefs.get("path")
    media_type   = normalize_media_type(getattr(args, "media", "movie"))
    force        = getattr(args, "force", False)
    do_all       = getattr(args, "all", False)

    media_types = ["movie", "gameplay"] if do_all else [media_type]

    for mt in media_types:
        print(f"Building vocabulary index ({mt})...")
        try:
            index = build_vocabulary_index(project_path, mt, force=force)
        except FileNotFoundError as exc:
            print(f"  ✗ {exc}", file=sys.stderr)
            continue
        meta      = index.get("meta", {})
        n_files   = meta.get("files_processed", "?")
        n_tokens  = meta.get("total_tokens", 0)
        voc_flds  = meta.get("vocabulary_fields") or []
        out_rel   = Path("data") / "vocabulary" / f"vocabulary_{mt}.json"
        if n_files != "?":
            print(f"Processed {n_files} files")
        if voc_flds:
            print(f"Fields: {', '.join(voc_flds)}")
        print(f"Found {n_tokens} unique tokens")
        print(f"Saved: {out_rel}")


def _index_stats(args):
    """Print a corpus-wide statistical summary."""
    from services.corpus_stats import get_corpus_stats, get_top_silhouette_labels

    project_path = prefs.get("path")
    output_json = getattr(args, "json", False)
    verbose = getattr(args, "verbose", False)

    try:
        stats = get_corpus_stats(project_path)
    except FileNotFoundError as exc:
        print(f"✗ {exc}", file=sys.stderr)
        sys.exit(1)

    if output_json:
        print(json.dumps(stats, indent=2, ensure_ascii=False))
        return

    project_name = prefs.get("name") or Path(project_path).name
    movie_count  = stats["movies"]
    gp_count     = stats["gameplay_videos"]

    W = 31  # right-align column width

    def row(label, value, suffix=""):
        print(f"{label}:{value:>{W - len(label)}}{suffix}")

    def blank():
        print()

    print("Corpus Statistics")
    blank()

    # ── Media ────────────────────────────────────────────────────────────────
    row("Movies", movie_count)
    row("Gameplay Videos", gp_count)
    blank()

    # ── Annotations ──────────────────────────────────────────────────────────
    row("Annotated Shots", stats["annotated_shots"])
    if stats.get("annotated_shots_movie") or stats.get("annotated_shots_gameplay"):
        if movie_count:
            row("  ↳ Movie Shots", stats["annotated_shots_movie"])
        if gp_count:
            row("  ↳ Gameplay Shots", stats["annotated_shots_gameplay"])
    row("Detected Scenes", stats["detected_scenes"])
    blank()

    # ── Best Frames ───────────────────────────────────────────────────────────
    row("Best Frames (PNG)", stats["best_frames"])
    by_type = stats.get("best_frames_by_type", {})
    for mt, n in sorted(by_type.items()):
        row(f"  ↳ {mt.capitalize()}", n)
    row("Shots w/ Best Frame", stats["shots_with_best_frame"])
    blank()

    # ── Motifs ───────────────────────────────────────────────────────────────
    row("Motifs", stats["motifs"])
    for mt, n in sorted(stats.get("motifs_by_type", {}).items()):
        row(f"  ↳ {mt.capitalize()}", n)
    blank()

    # ── Palettes ─────────────────────────────────────────────────────────────
    row("Palettes", stats["palettes"])
    blank()

    # ── Embeddings ───────────────────────────────────────────────────────────
    row("Embedding Indexes (.npy)", stats["embeddings"])
    for mt, n in sorted(stats.get("embeddings_by_type", {}).items()):
        row(f"  ↳ {mt.capitalize()}", n)
    blank()

    # ── Vocabulary ───────────────────────────────────────────────────────────
    row("Vocabulary Terms", stats["vocabulary_terms"])
    blank()

    # ── Silhouettes ──────────────────────────────────────────────────────────
    row("Silhouette Objects", stats["silhouette_objects"])
    row("Silhouette Labels",  stats["silhouette_labels"])
    blank()

    # ── Assets ───────────────────────────────────────────────────────────────
    print(f"Subtitles:{stats['subtitle_files']:>{W - 9}} / {movie_count}")
    print(f"Shotlists:{stats['shotlists']:>{W - 9}} / {movie_count}")
    blank()

    print(f"Project: {project_name}")

    if verbose:
        labels = get_top_silhouette_labels(project_path)
        if labels:
            print()
            print("Top silhouette labels:")
            for label, count in labels:
                print(f"{label} ({count})")


def _index_silhouette(args):
    """Dispatch ``crossing index silhouette <extract|audit|clear>``."""
    silhouette_action = getattr(args, "silhouette_action", None)
    if silhouette_action == "extract":
        _silhouette_catalog_extract(args)
    elif silhouette_action == "audit":
        _silhouette_catalog_audit(args)
    elif silhouette_action == "clear":
        _silhouette_catalog_clear(args)
    elif silhouette_action == "score":
        # Compute and persist silhouette quality scores
        from services.silhouette_scoring import compute_scores_for_catalog

        project_path = prefs.get("path")
        media_type = normalize_media_type(getattr(args, "media", "movie"))
        label = getattr(args, "label", None)
        field = getattr(args, "field", None)
        rebuild = getattr(args, "rebuild", False)
        verbose = getattr(args, "verbose", False)

        summary = compute_scores_for_catalog(
            project_path=project_path,
            media_type=media_type,
            label=label,
            field=field,
            rebuild=rebuild,
            verbose=verbose,
        )
        print(f"Scoring summary: processed={summary.get('processed', 0)} skipped={summary.get('skipped',0)} errors={summary.get('errors',0)}")
    elif silhouette_action == "backfill-scanned":
        _silhouette_backfill_scanned(args)
    else:
        print("✗ index silhouette: specify a subcommand (extract, audit, clear, backfill-scanned)", file=sys.stderr)
        sys.exit(1)


def _silhouette_catalog_extract(args):
    """Extract transparent PNG objects for a label and save to the catalog."""
    from data.metadata import get_metadata
    from data.media_id import compute_media_id
    from data.shotlist import resolve_filename
    from services.silhouette_catalog import (
        extract_objects_for_shot,
        extract_catalog_for_movie,
        extract_catalog_for_all,
        _find_shot_candidates,
    )

    project_path = prefs.get("path")
    label        = args.label
    field        = getattr(args, "field",  None)  # None → search across all annotation fields
    fields_multi = getattr(args, "fields", None)  # --fields: multi-field expansion mode
    media_type   = normalize_media_type(getattr(args, "media", "movie"))
    force        = getattr(args, "force", False)
    verbose      = getattr(args, "verbose", False)
    dry_run      = getattr(args, "dry_run", False)
    notify_items = getattr(args, "notify_items", False)  # --notify-each
    notify       = getattr(args, "notify", False) or notify_items  # --notify-each implies --notify

    sam_model   = (
        getattr(args, "model", None)
        or prefs.get(_MODEL_KEYS["segmentation"], _MODEL_DEFAULTS["segmentation"])
    )
    frame_model = (
        getattr(args, "frame_model", None)
        or prefs.get(_MODEL_KEYS["frame_match"], _MODEL_DEFAULTS["frame_match"])
    )

    # Validate: exactly one of label or --fields must be given
    if not label and not fields_multi:
        print(
            "✗ Specify a positional LABEL or --fields FIELD [FIELD …]",
            file=sys.stderr,
        )
        sys.exit(1)
    if label and fields_multi:
        print(
            "✗ Cannot combine a positional LABEL with --fields; use one or the other.",
            file=sys.stderr,
        )
        sys.exit(1)

    # Resolve scope
    shot_id  = getattr(args, "shot",  None)
    movie    = getattr(args, "movie", None)
    tmdb     = getattr(args, "tmdb",  None)
    do_all   = getattr(args, "all",   False)

    if not shot_id and not movie and tmdb is None and not do_all:
        print(
            "✗ Specify a scope: --shot <shot_id>, --title <title>, --tmdb <id>, or --all",
            file=sys.stderr,
        )
        sys.exit(1)

    # ── multi-field expansion mode ─────────────────────────────────────────
    if fields_multi:
        from services.vocabulary_index import get_vocabulary
        grand = {"files": 0, "shots": 0, "saved": 0, "skipped": 0, "failed": 0}
        errors: list[str] = []

        start_from_field = getattr(args, "start_from_field", None)
        start_from_label = getattr(args, "start_from_label", None)

        # Validate --start-from-field
        if start_from_field and start_from_field not in fields_multi:
            print(
                f"✗ --start-from-field '{start_from_field}' is not in the --fields list "
                f"({', '.join(fields_multi)})",
                file=sys.stderr,
            )
            sys.exit(1)

        # Determine which fields to actually process
        active_fields = fields_multi
        if start_from_field:
            idx = fields_multi.index(start_from_field)
            skipped_fields = fields_multi[:idx]
            active_fields = fields_multi[idx:]
            if skipped_fields:
                print(f"  (skipping fields: {', '.join(skipped_fields)})")

        # first_field is the one where --start-from-label applies (if set)
        first_field = active_fields[0] if active_fields else None

        for fld in active_fields:
            try:
                vocab = get_vocabulary(fld, project_path, media_type)
            except (FileNotFoundError, KeyError) as exc:
                msg = f"field '{fld}': {exc}"
                errors.append(msg)
                print(f"✗ {msg}", file=sys.stderr)
                continue

            labels = [e["value"] for e in vocab]

            # Apply --start-from-label only for the first active field
            if start_from_label and fld == first_field:
                if start_from_label not in labels:
                    print(
                        f"  ✗ --start-from-label '{start_from_label}' not found in "
                        f"field '{fld}'; starting from the beginning of this field.",
                        file=sys.stderr,
                    )
                else:
                    skip_idx = labels.index(start_from_label)
                    if skip_idx:
                        print(f"  (skipping {skip_idx} label(s) before '{start_from_label}')")
                    labels = labels[skip_idx:]

            print(f"\nField '{fld}': {len(labels)} label(s) — "
                  f"{', '.join(labels[:8])}"
                  f"{' …' if len(labels) > 8 else ''}")

            if dry_run:
                for lbl in labels:
                    print(f"  ── {fld}/{lbl}  (dry-run)")
                continue

            fld_totals = {"files": 0, "shots": 0, "saved": 0, "skipped": 0, "failed": 0}
            fld_skipped_labels = 0

            for lbl in labels:
                # Auto-skip labels already fully scanned (--all scope only; single-movie
                # runs don't write the corpus-wide sentinel so we never skip them).
                if not force and not movie and tmdb is None:
                    from services.silhouette_catalog import is_label_scanned
                    if is_label_scanned(project_path, media_type, fld, lbl):
                        fld_skipped_labels += 1
                        continue

                print(f"  ── {fld}/{lbl}")
                try:
                    if movie or tmdb is not None:
                        # Single-movie scope
                        from data.shotlist import resolve_filename as _rfn
                        fn = _rfn(project_path, tmdb, movie, media_type)
                        mid = None
                        for entry in get_metadata(project_path, media_type=media_type):
                            if entry.get("filename") == fn:
                                mid = compute_media_id(entry, media_type)
                                break
                        if not mid:
                            raise RuntimeError(f"Could not resolve media_id for '{fn}'")
                        s = extract_catalog_for_movie(
                            project_path=project_path,
                            filename=fn,
                            media_id=mid,
                            label=lbl,
                            field=fld,
                            media_type=media_type,
                            sam_model_name=sam_model,
                            frame_model_name=frame_model,
                            force=force,
                            verbose=verbose,
                        )
                        fld_totals["shots"]   += s.get("total_shots",   0)
                        fld_totals["saved"]   += s.get("total_saved",   0)
                        fld_totals["skipped"] += s.get("total_skipped", 0)
                        fld_totals["failed"]  += s.get("failed",        0)
                    else:
                        # All-movies scope
                        s = extract_catalog_for_all(
                            project_path=project_path,
                            label=lbl,
                            field=fld,
                            media_type=media_type,
                            sam_model_name=sam_model,
                            frame_model_name=frame_model,
                            force=force,
                            verbose=verbose,
                        )
                        fld_totals["files"]   += s.get("total_files",   0)
                        fld_totals["shots"]   += s.get("total_shots",   0)
                        fld_totals["saved"]   += s.get("total_saved",   0)
                        fld_totals["skipped"] += s.get("total_skipped", 0)
                        fld_totals["failed"]  += s.get("total_failed",  0)
                    print(
                        f"     saved={s.get('total_saved', s.get('saved', 0))}  "
                        f"skipped={s.get('total_skipped', s.get('skipped', 0))}  "
                        f"failed={s.get('total_failed', s.get('failed', 0))}"
                    )
                except RuntimeError as exc:
                    msg = f"{fld}/{lbl}: {exc}"
                    errors.append(msg)
                    fld_totals["failed"] += 1
                    print(f"     ✗ {exc}", file=sys.stderr)

            # Accumulate into grand totals
            for k in grand:
                grand[k] += fld_totals[k]

            if fld_skipped_labels:
                print(f"  ({fld_skipped_labels} label(s) skipped — already scanned)")

            # Per-field notification (--notify-each)
            if notify_items:
                from services.notify import discord_notify
                discord_notify(
                    f"✓ Silhouette field '{fld}' complete: "
                    f"{len(labels)} label(s)  "
                    f"saved={fld_totals['saved']}  "
                    f"skipped={fld_totals['skipped']}  "
                    f"failed={fld_totals['failed']}",
                    project_path,
                )

        fields_str = ", ".join(fields_multi)
        print(
            f"\n✓ Multi-field extract complete ({fields_str}):\n"
            f"  files={grand['files']}  shots={grand['shots']}  "
            f"saved={grand['saved']}  skipped={grand['skipped']}  "
            f"failed={grand['failed']}"
        )
        if errors:
            print(f"  {len(errors)} error(s):", file=sys.stderr)
            for e in errors:
                print(f"    ✗ {e}", file=sys.stderr)
        if notify:
            from services.notify import discord_notify
            discord_notify(
                f"✓ Silhouette multi-field extract ({fields_str}): "
                f"saved={grand['saved']}  failed={grand['failed']}",
                project_path,
            )
        if grand["failed"] or errors:
            sys.exit(1)
        return

    if verbose or dry_run:
        scope_str = (
            f"shot:{shot_id}" if shot_id
            else f"tmdb:{tmdb}" if tmdb is not None
            else f"movie:{movie}" if movie
            else "all"
        )
        print(
            f"Silhouette catalog extract: label='{label}'  field='{field}'  "
            f"scope={scope_str}  media={media_type}"
        )
        if dry_run:
            print("  (dry-run — listing candidates only)")

    # ── dry-run: list candidates and exit ──────────────────────────────────
    if dry_run:
        scope_type  = "shot" if shot_id else ("movie" if (movie or tmdb is not None) else "all")
        scope_value = shot_id or (str(tmdb) if tmdb is not None else movie)

        if scope_type == "shot":
            print(f"  Single shot: {shot_id}")
            return

        candidates = _find_shot_candidates(
            project_path=project_path,
            label=label,
            field=field,
            media_type=media_type,
            scope_type=scope_type,
            scope_value=scope_value,
        )
        if not candidates:
            print(f"  No candidates found for '{label}' in field '{field}'.")
        else:
            print(f"  {len(candidates)} candidate shot(s):")
            for c in candidates[:20]:
                sid = c.get("shot_id", "?")
                sc  = c.get("score", 0)
                fn  = c.get("filename", "?")
                print(f"    [{sc:.3f}] {sid}  ({fn})")
            if len(candidates) > 20:
                print(f"    … and {len(candidates) - 20} more")
        return

    # ── single-shot mode ───────────────────────────────────────────────────
    if shot_id:
        from data.media_id import parse_shot_id
        from services.silhouette import _resolve_shot_details

        filename, media_id = _resolve_shot_details(project_path, shot_id, media_type)
        if not filename or not media_id:
            print(f"✗ Could not resolve filename/media_id for shot '{shot_id}'.", file=sys.stderr)
            sys.exit(1)

        result = extract_objects_for_shot(
            project_path=project_path,
            label=label,
            field=field,
            shot_id=shot_id,
            filename=filename,
            media_id=media_id,
            media_type=media_type,
            sam_model_name=sam_model,
            frame_model_name=frame_model,
            force=force,
            verbose=verbose,
        )
        saved = result.get("saved", [])
        reason = result.get("reason", "?")
        if saved:
            print(f"✓ {len(saved)} object(s) extracted from shot {shot_id}")
            for p in saved:
                print(f"  {p}")
        elif reason == "cached":
            print(f"  (cached) objects for {shot_id} already in catalog")
        else:
            print(f"✗ No objects extracted: {reason}", file=sys.stderr)
            sys.exit(1)
        return

    # ── single-movie mode ──────────────────────────────────────────────────
    if movie or tmdb is not None:
        query_str = movie
        try:
            filename = resolve_filename(project_path, tmdb, query_str, media_type)
        except ValueError as exc:
            print(f"✗ {exc}", file=sys.stderr)
            sys.exit(1)

        media_id = None
        for entry in get_metadata(project_path, media_type=media_type):
            if entry.get("filename") == filename:
                media_id = compute_media_id(entry, media_type)
                break
        if not media_id:
            print(f"✗ Could not resolve media_id for '{filename}'.", file=sys.stderr)
            sys.exit(1)

        summary = extract_catalog_for_movie(
            project_path=project_path,
            filename=filename,
            media_id=media_id,
            label=label,
            field=field,
            media_type=media_type,
            sam_model_name=sam_model,
            frame_model_name=frame_model,
            force=force,
            verbose=verbose,
        )
        _print_silhouette_extract_summary(summary, label, filename)
        if notify:
            from services.notify import discord_notify
            discord_notify(
                f"✓ Silhouette extract: '{label}' in {filename}  "
                f"saved={summary.get('total_saved', 0)}  "
                f"failed={summary.get('failed', 0)}",
                project_path,
            )
        if summary.get("failed", 0):
            sys.exit(1)
        return

    # ── all-movies mode ────────────────────────────────────────────────────
    def _on_item(fn, item_summary, exc):
        if not notify:
            return
        from services.notify import discord_notify
        if exc is None:
            n = item_summary.get("total_saved", 0)
            if n == 0:
                return  # nothing new, skip notification
            discord_notify(
                f"✓ Silhouette extract '{label}': {fn}  saved={n}", project_path
            )
        else:
            discord_notify(
                f"✗ Silhouette extract '{label}': {fn}  — {exc}", project_path
            )

    try:
        summary = extract_catalog_for_all(
            project_path=project_path,
            label=label,
            field=field,
            media_type=media_type,
            sam_model_name=sam_model,
            frame_model_name=frame_model,
            force=force,
            verbose=verbose,
            on_item_done=_on_item if notify else None,
        )
    except RuntimeError as exc:
        print(f"✗ {exc}", file=sys.stderr)
        sys.exit(1)

    print(
        f"✓ Silhouette catalog extract complete for '{label}':\n"
        f"  files={summary['total_files']}  "
        f"shots={summary['total_shots']}  "
        f"saved={summary['total_saved']}  "
        f"skipped={summary['total_skipped']}  "
        f"failed={summary['total_failed']}"
    )
    if notify:
        from services.notify import discord_notify
        discord_notify(
            f"✓ Silhouette extract batch '{label}': "
            f"files={summary['total_files']}  "
            f"saved={summary['total_saved']}  "
            f"failed={summary['total_failed']}",
            project_path,
        )
    if summary.get("total_failed", 0):
        sys.exit(1)


def _print_silhouette_extract_summary(summary: dict, label: str, filename: str) -> None:
    saved   = summary.get("total_saved", 0)
    skipped = summary.get("total_skipped", 0)
    failed  = summary.get("failed", 0)
    shots   = summary.get("total_shots", 0)
    if saved == 0 and failed == 0:
        print(f"  (nothing new) '{label}' in {filename}  shots={shots}  skipped={skipped}")
    else:
        print(f"✓ '{label}' in {filename}")
        print(f"  shots={shots}  saved={saved}  skipped={skipped}  failed={failed}")


def _silhouette_backfill_scanned(args):
    """Write scanned sentinels for (field, label) pairs that have existing catalog entries."""
    from services.silhouette_catalog import backfill_scanned_from_catalog

    project_path = prefs.get("path")
    media_type   = normalize_media_type(getattr(args, "media", "movie"))

    print(f"Scanning catalog for existing (field, label) pairs…")
    result = backfill_scanned_from_catalog(project_path, media_type)

    written = result["written"]
    already = result["already"]
    pairs   = result["pairs"]

    if not pairs:
        print("  No catalog entries found — nothing to backfill.")
        return

    for fld, lbl in pairs:
        status = "already" if already and not written else "✓"
        print(f"  {status}  {fld}/{lbl}")

    print(
        f"\n✓ Backfill complete: {written} sentinel(s) written, "
        f"{already} already existed."
    )
    if written:
        print(
            "  Labels with no catalog entries (scanned but found nothing) will be\n"
            "  re-run on the next extract pass — this is fast (text search only, no GPU)."
        )


def _silhouette_catalog_audit(args):
    """Print a summary of the silhouette catalog."""
    from services.silhouette_catalog import audit_catalog, catalog_base_dir

    project_path   = prefs.get("path")
    media_type     = normalize_media_type(getattr(args, "media", "movie"))
    label          = getattr(args, "label", None)
    movie          = getattr(args, "movie", None)
    tmdb           = getattr(args, "tmdb",  None)
    do_all         = getattr(args, "all",   False)
    output_json    = getattr(args, "json",  False)

    filename_stem = None
    if movie or tmdb is not None:
        from data.shotlist import resolve_filename
        from pathlib import Path as _Path
        try:
            filename = resolve_filename(project_path, tmdb, movie, media_type)
            filename_stem = _Path(filename).stem
        except ValueError as exc:
            print(f"✗ {exc}", file=sys.stderr)
            sys.exit(1)

    report = audit_catalog(
        project_path=project_path,
        media_type=media_type,
        label=label,
        filename_stem=filename_stem,
    )

    if output_json:
        print(json.dumps(report, indent=2, ensure_ascii=False))
        return

    total   = report["total_objects"]
    errors  = report["errors"]
    by_lbl  = report["by_label"]
    items   = report["media_items"]

    base = catalog_base_dir(project_path, media_type)
    print(f"Silhouette catalog  ({media_type})  — {base}")
    print(f"  Total objects : {total}")
    print(f"  Media items   : {len(items)}")
    print(f"  Labels        : {len(by_lbl)}")
    if errors:
        print(f"  Errors        : {errors}")

    if by_lbl:
        print()
        print("  By label:")
        for lbl, count in by_lbl[:30]:
            bar = "█" * min(count, 40)
            print(f"    {lbl:<25s} {count:>5d}  {bar}")
        if len(by_lbl) > 30:
            print(f"    … and {len(by_lbl) - 30} more labels")


def _silhouette_catalog_clear(args):
    """Delete silhouette catalog entries matching the given filters."""
    from services.silhouette_catalog import clear_catalog

    project_path = prefs.get("path")
    media_type   = normalize_media_type(getattr(args, "media", "movie"))
    label        = getattr(args, "label", None)
    movie        = getattr(args, "movie", None)
    tmdb         = getattr(args, "tmdb",  None)
    do_all       = getattr(args, "all",   False)
    dry_run      = getattr(args, "dry_run", False)

    filename_stem = None
    if movie or tmdb is not None:
        from data.shotlist import resolve_filename
        from pathlib import Path as _Path
        try:
            filename = resolve_filename(project_path, tmdb, movie, media_type)
            filename_stem = _Path(filename).stem
        except ValueError as exc:
            print(f"✗ {exc}", file=sys.stderr)
            sys.exit(1)

    if not do_all and label is None and filename_stem is None:
        print(
            "✗ Specify --all, --label <label>, --title <title>, or --tmdb <id>.\n"
            "  Use --all to clear the entire catalog (irreversible without --dry-run).",
            file=sys.stderr,
        )
        sys.exit(1)

    result = clear_catalog(
        project_path=project_path,
        media_type=media_type,
        label=label,
        filename_stem=filename_stem,
        dry_run=dry_run,
    )

    n_files = result["deleted_files"]
    n_dirs  = result["deleted_dirs"]
    prefix  = "(dry-run) would delete" if dry_run else "Deleted"
    print(f"✓ {prefix} {n_files} file(s) in {n_dirs} director{'y' if n_dirs == 1 else 'ies'}.")


def _index_palette(args):
    """Dispatch ``crossing index palette <create|get>``."""
    palette_action = getattr(args, "palette_action", None)
    if palette_action == "create":
        _index_palette_create(args)
    elif palette_action == "get":
        _index_palette_get(args)
    else:
        print("✗ index palette: specify create or get", file=sys.stderr)
        sys.exit(1)


def _index_palette_create(args):
    """Build and cache colour palettes from best-frame PNGs."""
    from data.palette import create_palette_for_movie, create_palette_for_all_movies
    from data.shotlist import resolve_filename

    project_path = prefs.get("path")
    media_type = normalize_media_type(getattr(args, "media", "movie"))
    force = getattr(args, "force", False)
    verbose = getattr(args, "verbose", False)
    notify_each = getattr(args, "notify_items", False)
    notify = getattr(args, "notify", False) or notify_each

    do_all = getattr(args, "all", False)
    movie_query = getattr(args, "movie", None)
    tmdb = getattr(args, "tmdb", None)

    if do_all:

        def _palette_on_item(filename, item_summary, exc):
            if not notify_each:
                return
            from services.notify import discord_notify
            if exc is None:
                if item_summary.get("processed", 0) == 0 and item_summary.get("failed", 0) == 0:
                    return  # already cached / nothing changed
                processed = item_summary.get("processed", 0)
                discord_notify(f"✓ Palette: {filename}  ({processed} shots)", project_path)
            else:
                discord_notify(f"✗ Palette failed: {filename}  — {exc}", project_path)

        summary = create_palette_for_all_movies(
            project_path,
            media_type,
            force=force,
            verbose=verbose,
            on_item_done=_palette_on_item if notify_each else None,
        )
        cached = summary.get("total_cached", 0)
        cached_note = f"  ({cached} already cached)" if cached else ""
        print(
            f"✓ Palette create complete: "
            f"{summary['total_files']} file(s)  "
            f"processed={summary['total_processed']}  "
            f"skipped={summary['total_skipped']}  "
            f"failed={summary['total_failed']}"
            f"{cached_note}"
        )
        if summary.get("total_failed", 0):
            sys.exit(1)
        if notify:
            from services.notify import discord_notify
            discord_notify(
                f"✓ Palette batch complete: "
                f"processed={summary['total_processed']}  "
                f"skipped={summary['total_skipped']}  "
                f"failed={summary['total_failed']}",
                project_path,
            )
        return

    if tmdb is None and movie_query is None:
        print(
            "✗ Specify a target: --all, --title <title>, or --tmdb <id>",
            file=sys.stderr,
        )
        sys.exit(1)

    try:
        filename = resolve_filename(project_path, tmdb, movie_query, media_type)
    except ValueError as exc:
        print(f"✗ {exc}", file=sys.stderr)
        sys.exit(1)

    try:
        summary = create_palette_for_movie(
            project_path,
            filename,
            media_type,
            force=force,
            verbose=verbose,
        )
    except FileNotFoundError as exc:
        print(f"✗ {exc}", file=sys.stderr)
        sys.exit(1)
    except FileExistsError as exc:
        print(f"✗ {exc}", file=sys.stderr)
        sys.exit(1)
    except Exception as exc:
        print(f"✗ {exc}", file=sys.stderr)
        sys.exit(1)

    if summary.get("cached"):
        print(f"  (cached)  {filename}  — use --force to recompute")
        return

    print(f"✓ {filename}")
    print(f"  shots:     {summary.get('shot_count', 0)}")
    print(f"  processed: {summary.get('processed', 0)}")
    print(f"  skipped:   {summary.get('skipped', 0)}")
    print(f"  failed:    {summary.get('failed', 0)}")
    if notify:
        from services.notify import discord_notify
        discord_notify(
            f"✓ Palette: {filename}  "
            f"processed={summary.get('processed', 0)}  "
            f"failed={summary.get('failed', 0)}",
            project_path,
        )

    if summary.get("failed", 0):
        sys.exit(1)


def _index_palette_get(args):
    """Print the cached colour palette for a movie."""
    from data.palette import get_palette, get_palette_path
    from data.shotlist import resolve_filename

    project_path = prefs.get("path")
    media_type = normalize_media_type(getattr(args, "media", "movie"))

    do_all = getattr(args, "all", False)
    movie_query = getattr(args, "movie", None)
    tmdb = getattr(args, "tmdb", None)
    shot_index = getattr(args, "shot", None)

    if do_all:
        from data.metadata import get_metadata
        entries = get_metadata(project_path, media_type=media_type)
        results = []
        for e in entries:
            fn = e.get("filename")
            if not fn:
                continue
            data = get_palette(project_path, fn, media_type)
            if data is None:
                results.append({"filename": fn, "status": "missing"})
            else:
                summary = data.get("summary", {})
                results.append({
                    "filename": fn,
                    "status": "ok",
                    "processed": summary.get("processed", 0),
                    "skipped": summary.get("skipped", 0),
                    "created_at": data.get("created_at"),
                })
        print(json.dumps(results, indent=2, ensure_ascii=False))
        return

    if tmdb is None and movie_query is None:
        print(
            "✗ Specify a target: --all, --title <title>, or --tmdb <id>",
            file=sys.stderr,
        )
        sys.exit(1)

    try:
        filename = resolve_filename(project_path, tmdb, movie_query, media_type)
    except ValueError as exc:
        print(f"✗ {exc}", file=sys.stderr)
        sys.exit(1)

    data = get_palette(project_path, filename, media_type)
    if data is None:
        cache_path = get_palette_path(project_path, filename, media_type)
        print(
            f"✗ No palette cache found for '{filename}'.\n"
            f"  Expected: {cache_path}\n"
            f"  Run: crossing index palette create --title {movie_query or filename}",
            file=sys.stderr,
        )
        sys.exit(1)

    if shot_index is not None:
        shots = data.get("shots", [])
        matches = [s for s in shots if s.get("shot_index") == shot_index]
        if not matches:
            print(
                f"✗ Shot index {shot_index} not found in palette cache for '{filename}'.",
                file=sys.stderr,
            )
            sys.exit(1)
        print(json.dumps(matches[0], indent=2, ensure_ascii=False))
        return

    print(json.dumps(data, indent=2, ensure_ascii=False))


def _index_motif(args):
    """Dispatch ``crossing index motif <generate|attach|audit>``."""
    motif_action = getattr(args, "motif_action", None)
    if motif_action == "generate":
        _index_motif_generate(args)
    elif motif_action == "attach":
        _index_motif_attach(args)
    elif motif_action == "audit":
        _index_motif_audit(args)
    else:
        print("✗ index motif: specify a subcommand (generate, attach, audit)", file=sys.stderr)
        sys.exit(1)


def _index_motif_generate(args):
    """Generate cinematic motifs for all shots in one movie or all movies."""
    from data.motif import generate_motifs_for_movie, generate_motifs_for_all_movies
    from data.shotlist import resolve_filename

    project_path = prefs.get("path")
    media_type   = normalize_media_type(getattr(args, "media", "movie"))
    force        = getattr(args, "force",    False)
    verbose      = getattr(args, "verbose",  False)
    do_all       = getattr(args, "all",      False)
    query_words  = getattr(args, "query", None) or []
    movie_query  = getattr(args, "movie", None) or getattr(args, "video", None) or (" ".join(query_words).strip() or None)
    tmdb         = getattr(args, "tmdb",     None)
    model_name   = getattr(args, "model",    None) or prefs.get(_MODEL_KEYS["annotate"], _MODEL_DEFAULTS["annotate"])
    notify_each  = getattr(args, "notify_items", False)
    notify       = getattr(args, "notify", False) or notify_each

    if do_all:

        def _motif_on_item(title, item_summary, exc):
            if not notify_each:
                return
            from services.notify import discord_notify
            if exc is None:
                if item_summary.get("processed", 0) == 0 and item_summary.get("failed", 0) == 0:
                    return  # all shots already had motifs / nothing changed
                n_gen = item_summary.get("processed", 0)
                discord_notify(f"✓ Motifs: {title}  ({n_gen} generated)", project_path)
            else:
                discord_notify(f"✗ Motifs failed: {title}  — {exc}", project_path)

        summary = generate_motifs_for_all_movies(
            project_path,
            media_type,
            model_name=model_name,
            force=force,
            verbose=verbose,
            on_item_done=_motif_on_item if notify_each else None,
        )
        print(
            f"\n✓ Motif generate complete: "
            f"{summary['total_files']} file(s)  "
            f"generated={summary['total_processed']}  "
            f"skipped={summary['total_skipped']}  "
            f"failed={summary['total_failed']}"
        )
        if summary.get("errors"):
            for fn, msg in summary["errors"]:
                print(f"  ✗ {fn}: {msg}", file=sys.stderr)
        if summary.get("total_failed", 0):
            sys.exit(1)
        if notify:
            from services.notify import discord_notify
            discord_notify(
                f"✓ Motif batch complete: "
                f"generated={summary['total_processed']}  "
                f"skipped={summary['total_skipped']}  "
                f"failed={summary['total_failed']}",
                project_path,
            )
        return

    if tmdb is None and movie_query is None:
        print(
            "✗ Specify a target: --all, --title <title>, or --tmdb <id>",
            file=sys.stderr,
        )
        sys.exit(1)

    try:
        filename = resolve_filename(project_path, tmdb, movie_query, media_type)
    except ValueError as exc:
        print(f"✗ {exc}", file=sys.stderr)
        sys.exit(1)

    if verbose:
        from data.metadata import get_metadata
        meta_entries = get_metadata(project_path, media_type=media_type)
        meta = next((e for e in meta_entries if e.get("filename") == filename), {})
        title = meta.get("title") or filename
        print(f"{title}")

    try:
        summary = generate_motifs_for_movie(
            project_path,
            filename,
            media_type,
            model_name=model_name,
            force=force,
            verbose=verbose,
        )
    except FileNotFoundError as exc:
        print(f"✗ {exc}", file=sys.stderr)
        sys.exit(1)
    except Exception as exc:
        print(f"✗ {exc}", file=sys.stderr)
        sys.exit(1)

    print(f"✓ {filename}")
    print(f"  total:     {summary.get('total', 0)}")
    print(f"  generated: {summary.get('processed', 0)}")
    print(f"  skipped:   {summary.get('skipped', 0)}")
    print(f"  failed:    {summary.get('failed', 0)}")
    if notify:
        from services.notify import discord_notify
        discord_notify(
            f"✓ Motifs: {filename}  "
            f"generated={summary.get('processed', 0)}  "
            f"failed={summary.get('failed', 0)}",
            project_path,
        )

    if summary.get("failed", 0):
        sys.exit(1)


def _index_motif_attach(args):
    """Attach motif values from data/motifs/ sidecars into annotation JSON as shot.motif."""
    from data.motif import attach_motifs_to_annotation
    from data.shotlist import resolve_filename
    from data.annotate import get_annotation_json_path

    project_path = prefs.get("path")
    media_type   = normalize_media_type(getattr(args, "media", "movie"))
    force        = getattr(args, "force",   False)
    dry_run      = getattr(args, "dry_run", False)
    verbose      = getattr(args, "verbose", False)
    do_all       = getattr(args, "all",     False)
    query_words  = getattr(args, "query", None) or []
    movie_query  = getattr(args, "movie", None) or (" ".join(query_words).strip() or None)
    tmdb         = getattr(args, "tmdb", None)

    if dry_run:
        print("(dry run — no files will be modified)")

    if do_all:
        # Resolve all files that have annotation JSON
        from data.annotate import get_annotation_json_path
        from data.metadata import get_metadata
        meta_entries = get_metadata(project_path, media_type=media_type)
        filenames = [
            e["filename"] for e in meta_entries
            if e.get("filename")
            and get_annotation_json_path(project_path, e["filename"], media_type).exists()
        ]
        if not filenames:
            print(f"No annotation JSON files found under {media_type}.", file=sys.stderr)
            sys.exit(1)

        total_files = 0
        total_updated = 0
        total_skipped = 0
        total_conflicts = 0
        total_missing_sidecars = 0
        total_missing_shots = 0

        for fn in filenames:
            stem = Path(fn).stem
            try:
                r = attach_motifs_to_annotation(
                    project_path, fn, media_type,
                    force=force, dry_run=dry_run, verbose=False,
                )
            except FileNotFoundError as exc:
                print(f"  ✗ {stem}: {exc}", file=sys.stderr)
                continue

            total_files += 1
            # A file counts as "updated" when anything was added or force-overwritten
            n_updated = r["added"] + (r["conflicts"] if force else 0)
            if n_updated:
                total_updated += 1
            else:
                total_skipped += 1

            if r["missing"] and r["found"] == 0:
                total_missing_sidecars += 1
            total_conflicts  += (r["conflicts"] if not force else 0)
            total_missing_shots += r["missing"]

            if verbose:
                tag = "(dry run) " if dry_run else ""
                print(
                    f"  {tag}{stem}: "
                    f"shots={r['shots']}  found={r['found']}  "
                    f"added={r['added']}  unchanged={r['unchanged']}  "
                    f"conflicts={r['conflicts']}  missing={r['missing']}"
                )

        print(f"\nFiles processed:         {total_files}")
        print(f"Updated:                 {total_updated}")
        print(f"Skipped (up to date):    {total_skipped}")
        print(f"Missing motif sidecars:  {total_missing_sidecars}")
        print(f"Conflicts (not written): {total_conflicts}")
        print(f"Missing shot motifs:     {total_missing_shots}")
        return

    if tmdb is None and not movie_query:
        print("✗ Specify a target: --all, --title <value>, or --tmdb <id>.", file=sys.stderr)
        sys.exit(1)

    try:
        filename = resolve_filename(project_path, tmdb, movie_query, media_type)
    except ValueError as exc:
        print(f"✗ {exc}", file=sys.stderr)
        sys.exit(1)

    try:
        r = attach_motifs_to_annotation(
            project_path, filename, media_type,
            force=force, dry_run=dry_run, verbose=verbose,
        )
    except FileNotFoundError as exc:
        print(f"✗ {exc}", file=sys.stderr)
        sys.exit(1)

    from data.metadata import get_metadata
    meta_entries = get_metadata(project_path, media_type=media_type)
    meta = next((e for e in meta_entries if e.get("filename") == filename), {})
    title = meta.get("title") or Path(filename).stem

    prefix = "(dry run) " if dry_run else ""
    print(f"{title}")
    print(f"  {prefix}shots:       {r['shots']}")
    print(f"  {prefix}motifs found: {r['found']}")
    print(f"  {prefix}added:        {r['added']}")
    print(f"  {prefix}unchanged:    {r['unchanged']}")
    print(f"  {prefix}conflicts:    {r['conflicts']}")
    print(f"  {prefix}missing:      {r['missing']}")
    if r["conflicts"] and not force:
        print(
            f"  ⚠  {r['conflicts']} conflict(s) — existing shot.motif differs from sidecar. "
            f"Use --force to overwrite.",
            file=sys.stderr,
        )


def _index_motif_audit(args):
    """Audit canonical shot.motif coverage across annotation JSON files."""
    from services.motif_audit import audit_motifs_for_all, audit_motifs_for_file
    from data.shotlist import resolve_filename

    project_path = prefs.get("path")
    media_type   = normalize_media_type(getattr(args, "media", "movie"))
    do_all       = getattr(args, "all",          False)
    missing_only = getattr(args, "missing_only", False)
    zero_only    = getattr(args, "zero_only",    False)
    limit        = getattr(args, "limit",        None)
    verbose      = getattr(args, "verbose",      False)
    query_words  = getattr(args, "query", None) or []
    movie_query  = getattr(args, "movie", None) or (" ".join(query_words).strip() or None)
    tmdb         = getattr(args, "tmdb", None)

    if do_all:
        report = audit_motifs_for_all(project_path, media_type)
        pct = report["coverage"] * 100
        print("Motif Audit\n")
        print(f"Media: {media_type}")
        print(f"Files checked:       {report['files_checked']}")
        print(f"Shots checked:       {report['shots_checked']}")
        print(f"Shots with motif:    {report['motifs_present']}")
        print(f"Shots missing motif: {report['motifs_missing']}")
        print(f"Coverage:            {pct:.2f}%")

        files = report["files"]
        if missing_only:
            files = [f for f in files if f["missing"] > 0]
        if zero_only:
            files = [f for f in files if f["present"] == 0]
        if limit is not None:
            files = files[:limit]

        if files:
            print("\nFiles with missing motifs:")
            for f in files:
                label = f"{f['missing']} missing / {f['total']} total"
                print(f"  {label:<30}  {f['filename']}")
                if verbose and f["missing_shot_ids"]:
                    print(f"    missing shot_ids:")
                    for sid in f["missing_shot_ids"]:
                        print(f"      {sid}")
        return

    if tmdb is None and not movie_query:
        print("✗ Specify a target: --all, --title <value>, or --tmdb <id>.", file=sys.stderr)
        sys.exit(1)

    try:
        filename = resolve_filename(project_path, tmdb, movie_query, media_type)
    except ValueError as exc:
        print(f"✗ {exc}", file=sys.stderr)
        sys.exit(1)

    try:
        f = audit_motifs_for_file(project_path, filename, media_type)
    except (FileNotFoundError, OSError) as exc:
        print(f"✗ {exc}", file=sys.stderr)
        sys.exit(1)

    pct = (f["present"] / f["total"] * 100) if f["total"] else 100.0
    print(f"Motif Audit: {Path(filename).stem}\n")
    print(f"Shots checked:       {f['total']}")
    print(f"Shots with motif:    {f['present']}")
    print(f"Shots missing motif: {f['missing']}")
    print(f"Coverage:            {pct:.2f}%")
    if verbose and f["missing_shot_ids"]:
        print("\nMissing shot_ids (first 10):")
        for sid in f["missing_shot_ids"]:
            print(f"  {sid}")


# ---------------------------------------------------------------------------
# book command family
# ---------------------------------------------------------------------------

def cmd_engraving(args):
    sub = args.engraving_subcommand
    if sub == "smoke-test":
        _engraving_smoke_test(args)
    else:
        print("✗ engraving: specify a subcommand.", file=sys.stderr)
        sys.exit(1)


def _engraving_smoke_test(args):
    """Standalone engraving smoke test: preprocessing PNG → FLUX raw output PNG."""
    import traceback
    from pathlib import Path as _Path

    preprocessing_path = _Path(args.preprocessing_png).resolve()
    if not preprocessing_path.exists():
        print(f"✗ preprocessing PNG not found: {preprocessing_path}", file=sys.stderr)
        sys.exit(1)

    project_path = getattr(args, "project_path", None) or prefs.get("path")
    if not project_path:
        print("✗ No project path set.  Run: crossing tool path /path/to/project", file=sys.stderr)
        sys.exit(1)

    if args.out_dir:
        out_dir = _Path(args.out_dir).resolve()
    else:
        out_dir = preprocessing_path.parent
    out_dir.mkdir(parents=True, exist_ok=True)

    engraving_id = preprocessing_path.stem.removesuffix("_preprocess_v1")
    if not engraving_id:
        engraving_id = "smoke"

    # Parse optional silhouette context for prompt variable substitution
    import json as _json
    context = None
    if getattr(args, "context_json", None):
        try:
            context = _json.loads(args.context_json)
        except (ValueError, TypeError) as _ctx_exc:
            print(f"✗ Invalid --context JSON: {_ctx_exc}", file=sys.stderr)
            sys.exit(1)

    try:
        from PIL import Image as _PilImage
        with _PilImage.open(preprocessing_path) as _im:
            w, h = _im.size
    except Exception as exc:
        print(f"✗ Cannot read preprocessing PNG: {exc}", file=sys.stderr)
        sys.exit(1)

    print("[engraving smoke-test]")
    print(f"  preprocessing : {preprocessing_path}")
    print(f"  size          : {w} x {h} px")
    print(f"  output dir    : {out_dir}")
    print(f"  engraving id  : {engraving_id}")
    print()

    try:
        from services.engraving_generate import validate_models, generate_engraving
        from services.engraving_prompt import load_engraving_prompt, EngravingPromptError

        try:
            prompt_filename, _ = load_engraving_prompt(project_path)
            print(f"  prompt        : {prompt_filename}")
            print()
        except EngravingPromptError as exc:
            print(f"✗ {exc}", file=sys.stderr)
            sys.exit(1)

        validate_models(project_path)
        result = generate_engraving(
            project_path=project_path,
            preprocessing_path=str(preprocessing_path),
            engraving_id=engraving_id,
            cache_dir=out_dir,
            context=context,
        )
    except Exception:
        print("[engraving smoke-test] FAILED:", file=sys.stderr)
        traceback.print_exc()
        sys.exit(1)

    print("[engraving smoke-test] SUCCESS")
    print(f"  raw    : {result['raw_png']}")
    print(f"  output : {result['output_png']}")


def cmd_book(args):
    sub = args.book_subcommand
    if sub == "new":
        _book_new(args)
    elif sub == "delete":
        _book_delete(args)
    elif sub == "list":
        _book_list(args)
    elif sub == "use":
        _book_use(args)
    elif sub == "current":
        _book_current(args)
    elif sub == "import":
        _book_import(args)
    else:
        print("✗ book: specify a subcommand.", file=sys.stderr)
        sys.exit(1)


def _book_new(args):
    _require_path()
    project_path = prefs.get("path")
    slug = args.slug
    from data.book import create_book
    try:
        create_book(project_path, slug)
    except FileExistsError as exc:
        print(f"✗ {exc}", file=sys.stderr)
        sys.exit(1)
    prefs.set("current_book", slug)
    print(f"✓ Created book: {slug}")
    print(f"  Path: {project_path}/output/books/{slug}")
    print(f"  Set as current book.")


def _book_delete(args):
    _require_path()
    project_path = prefs.get("path")
    slug = args.slug
    force = getattr(args, "force", False)

    from data.book import book_dir, delete_book
    folder = book_dir(project_path, slug)
    if not folder.exists():
        print(f"✗ Book not found: {slug}", file=sys.stderr)
        sys.exit(1)

    if not force:
        answer = input(f"Delete book '{slug}' and all its contents? [y/N] ").strip().lower()
        if answer not in ("y", "yes"):
            print("Aborted.")
            return

    try:
        delete_book(project_path, slug)
    except FileNotFoundError as exc:
        print(f"✗ {exc}", file=sys.stderr)
        sys.exit(1)

    # Clear current book preference if it was this book
    if prefs.get("current_book") == slug:
        prefs.set("current_book", None)

    print(f"✓ Deleted book: {slug}")


def _book_list(args):
    _require_path()
    project_path = prefs.get("path")
    from data.book import list_books
    books = list_books(project_path)
    current = prefs.get("current_book")
    if not books:
        print("No books found.")
        return
    for book in books:
        slug = book.get("slug", "")
        marker = "* " if slug == current else "  "
        page_info = f"  ({book.get('page_count', 0)} pages)" if book.get("pdf") else ""
        print(f"{marker}{slug}{page_info}")


def _book_use(args):
    _require_path()
    project_path = prefs.get("path")
    slug = args.slug
    from data.book import book_dir
    if not book_dir(project_path, slug).exists():
        print(f"✗ Book not found: {slug}", file=sys.stderr)
        sys.exit(1)
    prefs.set("current_book", slug)
    print(f"✓ Current book set to: {slug}")


def _book_current(args):
    _require_path()
    project_path = prefs.get("path")
    slug = prefs.get("current_book")
    if not slug:
        print("No current book set. Use: crossing book use <slug>")
        return
    from data.book import book_dir
    folder = book_dir(project_path, slug)
    print(f"Current book: {slug}")
    print(f"Path: {folder}")


def _book_import(args):
    _require_path()
    project_path = prefs.get("path")
    slug = prefs.get("current_book")
    if not slug:
        print("✗ No current book set. Use: crossing book use <slug>", file=sys.stderr)
        sys.exit(1)
    force = getattr(args, "force", False)
    from data.book import import_pdf
    try:
        data = import_pdf(project_path, slug, args.pdf, force=force)
    except FileNotFoundError as exc:
        print(f"✗ {exc}", file=sys.stderr)
        sys.exit(1)
    except FileExistsError as exc:
        print(f"✗ {exc}", file=sys.stderr)
        sys.exit(1)
    except ImportError:
        print(
            "✗ PyMuPDF (fitz) is required to import PDFs.\n"
            "  Install it: pip install pymupdf",
            file=sys.stderr,
        )
        sys.exit(1)
    print(f"✓ Imported PDF into book '{slug}': {data['page_count']} pages")


def _book_visualizer(args):
    """book visualizer — launch the interactive book GUI."""
    _require_visualizer_deps()
    from visualizers.book_visualizer import run_visualizer
    run_visualizer(prefs.get("path"))


def cmd_visualizer(args):
    sub = args.visualizer_subcommand
    if sub in (None, "project"):
        _project_visualizer(args)
    elif sub == "shotlist":
        _require_path()
        filename = getattr(args, "filename", None)
        if filename:
            _require_visualizer_deps()
            from visualizers.shot_visualizer import open_at_shot
            open_at_shot(
                prefs.get("path"),
                filename,
                normalize_media_type(getattr(args, "media", "movie")) or "movie",
                shot_id=getattr(args, "shot_id", "") or "",
                play=getattr(args, "play", False),
                loop=getattr(args, "loop", False),
                no_continue=getattr(args, "no_continue", False),
            )
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
    elif sub == "cloud":
        _require_path()
        _cloud_visualizer(args)
    elif sub == "metadata":
        _require_path()
        _metadata_visualizer(args)
    elif sub == "silhouette":
        _require_path()
        _silhouette_visualizer(args)
    elif sub == "palette":
        _require_path()
        _palette_visualizer(args)
    elif sub == "flipbook":
        _require_path()
        _flipbook_visualizer(args)
    elif sub == "book":
        _require_path()
        _book_visualizer(args)


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


def _silhouette_visualizer(args):
    """Launch the silhouette polygon browser GUI."""
    _require_visualizer_deps()
    from visualizers.silhouette_visualizer import run_visualizer
    run_visualizer(
        prefs.get("path"),
        media_type=normalize_media_type(getattr(args, "media", "movie")) or "movie",
        field=getattr(args, "field", None),
    )


def _palette_visualizer(args):
    """Launch the palette colour browser GUI."""
    _require_visualizer_deps()
    from visualizers.palette_visualizer import run_visualizer
    run_visualizer(
        prefs.get("path"),
        media_type=normalize_media_type(getattr(args, "media", "movie")) or "movie",
    )


def _require_path():
    if not prefs.get("path"):
        print("✗ Error: no project path set. Run: crossing tool path <folder>", file=sys.stderr)
        sys.exit(1)


# ---------------------------------------------------------------------------
# Backup
# ---------------------------------------------------------------------------

def _validate_backup_paths() -> tuple[str, str]:
    """Validate project and backup paths. Returns (project_path, backup_path) or exits."""
    project_path = prefs.get("path")
    backup_path = prefs.get("backup_path")

    if not project_path:
        print("✗ No project path set. Run: crossing tool path <folder>", file=sys.stderr)
        sys.exit(1)
    if not backup_path:
        print("✗ No backup path set. Run: crossing backup path <folder>", file=sys.stderr)
        sys.exit(1)

    project = Path(project_path)
    backup = Path(backup_path)

    if not project.exists():
        print(f"✗ Project folder does not exist: {project}", file=sys.stderr)
        sys.exit(1)
    if not backup.exists():
        print(f"✗ Backup folder does not exist: {backup}", file=sys.stderr)
        sys.exit(1)
    if not os.access(str(backup), os.W_OK):
        print(f"✗ Backup folder is not writable: {backup}", file=sys.stderr)
        sys.exit(1)

    return str(project), str(backup_path)


def _backup_update(project_path: str, backup_path: str, dry_run: bool = False) -> None:
    """Synchronize project folder contents into backup folder."""
    import shutil
    import subprocess as _sp

    project = Path(project_path)
    backup = Path(backup_path)

    # Estimate available disk space (non-fatal if it fails)
    try:
        usage = shutil.disk_usage(str(backup))
        free_gb = usage.free / (1024 ** 3)
        print(f"  Free space on backup volume: {free_gb:.1f} GB")
    except Exception:
        pass

    # Ensure source path ends with / so rsync syncs contents, not the folder itself
    src = str(project).rstrip("/") + "/"
    dst = str(backup).rstrip("/") + "/"

    # Try rsync first
    rsync_available = shutil.which("rsync") is not None
    if rsync_available:
        cmd = ["rsync", "-a", "--info=progress2", src, dst]
        if dry_run:
            cmd.insert(1, "--dry-run")
            print("  (dry run — no files will be written)")
        print(f"  rsync {src!r} → {dst!r}")
        try:
            _sp.run(cmd, check=True)
            return
        except _sp.CalledProcessError as exc:
            print(f"✗ rsync failed (exit {exc.returncode})", file=sys.stderr)
            sys.exit(exc.returncode)

    # Fallback: shutil-based incremental copy
    if dry_run:
        print("  (dry run — no files will be written)")
        print(f"  [shutil fallback] would sync {src!r} → {dst!r}")
        return

    print(f"  rsync not found — using shutil fallback")
    print(f"  Copying {src!r} → {dst!r}")
    copied = 0
    skipped = 0
    for root, dirs, files in os.walk(str(project)):
        rel_root = os.path.relpath(root, str(project))
        dst_root = backup / rel_root
        dst_root.mkdir(parents=True, exist_ok=True)
        for filename in files:
            src_file = Path(root) / filename
            dst_file = dst_root / filename
            try:
                src_stat = src_file.stat()
                if dst_file.exists():
                    dst_stat = dst_file.stat()
                    # Skip if same size and dst is not older
                    if (dst_stat.st_size == src_stat.st_size and
                            dst_stat.st_mtime >= src_stat.st_mtime):
                        skipped += 1
                        continue
                shutil.copy2(str(src_file), str(dst_file))
                copied += 1
            except Exception as exc:
                print(f"  ✗ Failed to copy {src_file}: {exc}", file=sys.stderr)
    print(f"  Done — {copied} file(s) copied, {skipped} skipped (up to date)")


def cmd_backup(args):
    sub = args.backup_subcommand
    if sub == "update":
        project_path, backup_path = _validate_backup_paths()
        dry_run = getattr(args, "dry_run", False)
        print(f"Backing up project to: {backup_path}")
        _backup_update(project_path, backup_path, dry_run=dry_run)
    elif sub == "status":
        cmd_backup_status(args)
    elif sub == "path":
        if args.folder is None:
            val = prefs.get("backup_path")
            print(val if val else "(not set)")
        else:
            p = Path(args.folder).resolve()
            prefs.set("backup_path", str(p))
            print(f"Backup path set to: {p}")


def cmd_backup_status(args):
    import shutil

    project_path = prefs.get("path") or "(not set)"
    backup_path = prefs.get("backup_path") or "(not set)"

    print(f"Project path : {project_path}")
    print(f"Backup path  : {backup_path}")

    if backup_path == "(not set)":
        print("Backup folder: not configured")
        return

    backup = Path(backup_path)
    if not backup.exists():
        print("Backup folder: does not exist")
        return

    writable = os.access(str(backup), os.W_OK)
    print(f"Backup folder: exists")
    print(f"Writable     : {'yes' if writable else 'no'}")

    try:
        usage = shutil.disk_usage(str(backup))
        free_gb = usage.free / (1024 ** 3)
        total_gb = usage.total / (1024 ** 3)
        print(f"Free space   : {free_gb:.1f} GB / {total_gb:.1f} GB")
    except Exception:
        print("Free space   : (unavailable)")


class _HelpfulParser(argparse.ArgumentParser):
    def error(self, message):
        sys.stderr.write(f"missing: {message}\n")
        self.print_usage(sys.stderr)
        if "the following arguments are required:" in message:
            sys.stderr.write(f"help: {self.prog} -h\n")
        self.exit(2)


def _add_notify_args(p, *, batch: bool = True) -> None:
    """Add ``--notify`` and, for batch commands, ``--notify-each`` to a parser.

    All batch commands (those that accept ``--all``) should call this with the
    default ``batch=True``.  Single-output commands should pass ``batch=False``
    to omit ``--notify-each``.

    Uses ``dest="notify_items"`` for ``--notify-each`` to match the pattern used
    throughout this CLI.
    """
    p.add_argument(
        "--notify", action="store_true",
        help="Send a Discord notification when the run finishes",
    )
    if batch:
        p.add_argument(
            "--notify-each", action="store_true", dest="notify_items",
            help="Send a Discord notification after each item in a --all batch",
        )


# ---------------------------------------------------------------------------
# Analysis handler functions (new — wired to services/analysis.py)
# ---------------------------------------------------------------------------

def _search_motifs(args):
    """Handle: crossing search motifs compare|list [options]."""
    from services.analysis import compare_motifs, get_all_motifs
    project_path = prefs.get("path")
    media_type = normalize_media_type(getattr(args, "media", "movie"))
    remaining = args.scope or []
    sub_cmd = remaining[0] if remaining else "list"
    films = (remaining[1:] or []) + (getattr(args, "movie", None) or []) or None
    limit = getattr(args, "limit", None)

    if sub_cmd == "compare":
        mode = getattr(args, "mode", None) or "overlap"
        result = compare_motifs(
            project_path=project_path,
            media_type=media_type,
            films=films,
            mode=mode,
            limit=limit,
        )
    else:  # "list" or no sub_cmd
        sort = getattr(args, "sort", None) or "frequency"
        result = get_all_motifs(
            project_path=project_path,
            media_type=media_type,
            films=films,
            sort=sort,
            limit=limit,
        )
    print(json.dumps(result, indent=2))


def _search_palette_cmd(args):
    """Handle: crossing search palette [filter flags]."""
    from services.analysis import search_palette
    project_path = prefs.get("path")
    media_type = normalize_media_type(getattr(args, "media", "movie"))
    remaining = args.scope or []
    films = list(remaining) + (getattr(args, "movie", None) or []) or None

    result = search_palette(
        project_path=project_path,
        media_type=media_type,
        films=films,
        warm=getattr(args, "warm", False),
        cold=getattr(args, "cold", False),
        dark=getattr(args, "dark", False),
        bright=getattr(args, "bright", False),
        low_chroma=getattr(args, "low_chroma", False),
        high_chroma=getattr(args, "high_chroma", False),
        foreground_only=getattr(args, "foreground_only", False),
        background_only=getattr(args, "background_only", False),
        luminance_min=getattr(args, "luminance_min", None),
        luminance_max=getattr(args, "luminance_max", None),
        chroma_min=getattr(args, "chroma_min", None),
        chroma_max=getattr(args, "chroma_max", None),
        limit=getattr(args, "limit", None),
    )
    print(json.dumps(result, indent=2))


def _save_frames_to_dir(frames: list, save_dir: str, label: str = "frame") -> list[str]:
    """Save frame dicts to *save_dir* as JPEG files.  Returns list of written paths."""
    import os
    os.makedirs(save_dir, exist_ok=True)
    paths = []
    for i, f in enumerate(frames):
        stem = f.get("shot_id", f"{label}-{i:04d}").replace("/", "_").replace(":", "_")
        out = Path(save_dir) / f"{stem}.jpg"
        out.write_bytes(f["image_data"])
        paths.append(str(out))
    return paths


def _search_frames_cmd(args):
    """Handle: crossing search frames <query> [options]."""
    from services.frame_retrieval import retrieve_frames_for_query
    project_path = prefs.get("path")
    media_type = normalize_media_type(getattr(args, "media", "movie"))
    remaining = args.scope or []
    # First token after 'frames' is the query; remaining are film filters.
    if not remaining:
        print("error: 'search frames' requires a query string", file=sys.stderr)
        print("  crossing search frames <query> [--title FILM] [--limit 4] [--width 400] [--save-dir DIR]", file=sys.stderr)
        sys.exit(1)
    query = remaining[0]
    films = remaining[1:] + (getattr(args, "movie", None) or []) or None
    limit = getattr(args, "limit", None) or 4
    width = getattr(args, "width", 400) or 400
    save_dir = getattr(args, "save_dir", None)

    frames = retrieve_frames_for_query(
        project_path, query,
        films=films,
        field=getattr(args, "field", None),
        limit=limit,
        media_type=media_type,
        width=width,
    )
    if not frames:
        print(f"No frames found for query {query!r}.", file=sys.stderr)
        sys.exit(1)

    if save_dir:
        paths = _save_frames_to_dir(frames, save_dir, label="frame")
        summaries = [
            {**{k: v for k, v in f.items() if k != "image_data"}, "saved_path": p}
            for f, p in zip(frames, paths)
        ]
    else:
        summaries = [{k: v for k, v in f.items() if k != "image_data"} for f in frames]

    if getattr(args, "json", False):
        print(json.dumps(summaries, indent=2, default=str))
    else:
        for s in summaries:
            path_label = f"  → {s.get('saved_path')}" if save_dir else ""
            print(f"  {s['film_title']}  {s['shot_id']}  {s['start_time']} → {s['end_time']}{path_label}")
        if not save_dir:
            print(f"\n({len(frames)} frame(s) retrieved — use --save-dir DIR to write JPEG files)")


def _search_palette_frames_cmd(args):
    """Handle: crossing search palette-frames [filter flags]."""
    from services.frame_retrieval import retrieve_palette_frames
    project_path = prefs.get("path")
    media_type = normalize_media_type(getattr(args, "media", "movie"))
    remaining = args.scope or []
    films = list(remaining) + (getattr(args, "movie", None) or []) or None
    limit = getattr(args, "limit", None) or 4
    width = getattr(args, "width", 400) or 400
    save_dir = getattr(args, "save_dir", None)

    lum_min = getattr(args, "luminance_min", None)
    lum_max = getattr(args, "luminance_max", None)
    chr_min = getattr(args, "chroma_min", None)
    chr_max = getattr(args, "chroma_max", None)

    frames = retrieve_palette_frames(
        project_path,
        films=films, media_type=media_type, limit=limit, width=width,
        warm=getattr(args, "warm", False),
        cold=getattr(args, "cold", False),
        dark=getattr(args, "dark", False),
        bright=getattr(args, "bright", False),
        low_chroma=getattr(args, "low_chroma", False),
        high_chroma=getattr(args, "high_chroma", False),
        foreground_only=getattr(args, "foreground_only", False),
        background_only=getattr(args, "background_only", False),
        luminance_min=lum_min,
        luminance_max=lum_max,
        chroma_min=chr_min,
        chroma_max=chr_max,
    )
    if not frames:
        print("No frames found matching palette filters.", file=sys.stderr)
        sys.exit(1)

    if save_dir:
        paths = _save_frames_to_dir(frames, save_dir, label="palette-frame")
        summaries = [
            {**{k: v for k, v in f.items() if k != "image_data"}, "saved_path": p}
            for f, p in zip(frames, paths)
        ]
    else:
        summaries = [{k: v for k, v in f.items() if k != "image_data"} for f in frames]

    if getattr(args, "json", False):
        print(json.dumps(summaries, indent=2, default=str))
    else:
        for s in summaries:
            meta = s.get("metadata", {})
            path_label = f"  → {s.get('saved_path')}" if save_dir else ""
            print(f"  {s['film_title']}  {s['shot_id']}  lum={meta.get('luminance', '?')}  chroma={meta.get('chroma', '?')}{path_label}")
        if not save_dir:
            print(f"\n({len(frames)} frame(s) retrieved — use --save-dir DIR to write JPEG files)")


def _search_motif_frames_cmd(args):
    """Handle: crossing search motif-frames <motif> [options]."""
    from services.frame_retrieval import retrieve_motif_frames
    project_path = prefs.get("path")
    media_type = normalize_media_type(getattr(args, "media", "movie"))
    remaining = args.scope or []
    if not remaining:
        print("error: 'search motif-frames' requires a motif word", file=sys.stderr)
        print("  crossing search motif-frames <motif> [--title FILM] [--limit 4] [--save-dir DIR]", file=sys.stderr)
        sys.exit(1)
    motif = remaining[0]
    films = remaining[1:] + (getattr(args, "movie", None) or []) or None
    limit = getattr(args, "limit", None) or 4
    width = getattr(args, "width", 400) or 400
    save_dir = getattr(args, "save_dir", None)

    frames = retrieve_motif_frames(
        project_path, motif,
        films=films, media_type=media_type, limit=limit, width=width,
    )
    if not frames:
        print(f"No frames found for motif {motif!r}.", file=sys.stderr)
        sys.exit(1)

    if save_dir:
        paths = _save_frames_to_dir(frames, save_dir, label="motif-frame")
        summaries = [
            {**{k: v for k, v in f.items() if k != "image_data"}, "saved_path": p}
            for f, p in zip(frames, paths)
        ]
    else:
        summaries = [{k: v for k, v in f.items() if k != "image_data"} for f in frames]

    if getattr(args, "json", False):
        print(json.dumps(summaries, indent=2, default=str))
    else:
        for s in summaries:
            meta = s.get("metadata", {})
            path_label = f"  → {s.get('saved_path')}" if save_dir else ""
            print(f"  {s['film_title']}  {s['shot_id']}  motif={meta.get('motif', '?')}{path_label}")
        if not save_dir:
            print(f"\n({len(frames)} frame(s) retrieved — use --save-dir DIR to write JPEG files)")


def _shotlist_context_frames(args):
    """Handle: crossing shotlist context-frames <film> <shot_id> [options]."""
    from services.frame_retrieval import retrieve_context_frames
    project_path = prefs.get("path")
    media_type = normalize_media_type(getattr(args, "media", "movie"))
    film = getattr(args, "film", None) or getattr(args, "filename", None)
    shot_id = getattr(args, "shot_id", None)
    if not film or not shot_id:
        print("error: shotlist context-frames requires film and shot_id", file=sys.stderr)
        sys.exit(1)
    window = getattr(args, "window", 3)
    width = getattr(args, "width", 400) or 400
    save_dir = getattr(args, "save_dir", None)

    try:
        frames = retrieve_context_frames(
            project_path, film, shot_id,
            window=window, media_type=media_type, width=width,
        )
    except ValueError as exc:
        print(f"✗ {exc}", file=sys.stderr)
        sys.exit(1)

    if not frames:
        print("No frames could be retrieved for this context window.", file=sys.stderr)
        sys.exit(1)

    if save_dir:
        paths = _save_frames_to_dir(frames, save_dir, label="context-frame")
        summaries = [
            {**{k: v for k, v in f.items() if k != "image_data"}, "saved_path": p}
            for f, p in zip(frames, paths)
        ]
    else:
        summaries = [{k: v for k, v in f.items() if k != "image_data"} for f in frames]

    if getattr(args, "json", False):
        print(json.dumps(summaries, indent=2, default=str))
    else:
        for s in summaries:
            meta = s.get("metadata", {})
            center_marker = "→" if meta.get("is_center") else " "
            path_label = f"  → {s.get('saved_path')}" if save_dir else ""
            print(f"{center_marker} {s['shot_id']}  {s['start_time']} → {s['end_time']}{path_label}")
        if not save_dir:
            print(f"\n({len(frames)} frame(s) retrieved — use --save-dir DIR to write JPEG files)")


def _search_cooccurrence_cmd(args):
    """Handle: crossing search cooccurrence --terms A B [options]."""
    from services.analysis import search_cooccurrence
    project_path = prefs.get("path")
    media_type = normalize_media_type(getattr(args, "media", "movie"))
    terms = getattr(args, "terms", None) or []
    if not terms:
        print("error: 'search cooccurrence' requires --terms TERM [TERM ...]", file=sys.stderr)
        sys.exit(1)
    films = (getattr(args, "scope", None) or []) + (getattr(args, "movie", None) or []) or None
    result = search_cooccurrence(
        project_path=project_path,
        terms=terms,
        media_type=media_type,
        films=films,
        fields=getattr(args, "cooccurrence_fields", None),
        operator=getattr(args, "operator", "AND"),
        limit=getattr(args, "limit", None),
    )
    print(json.dumps(result, indent=2))


def _shotlist_context(args):
    """Handle: crossing shotlist context <film> <shot_id> [options]."""
    from services.analysis import get_shot_context
    project_path = prefs.get("path")
    media_type = normalize_media_type(getattr(args, "media", "movie"))
    film = getattr(args, "film", None) or getattr(args, "filename", None)
    shot_id = getattr(args, "shot_id", None)
    if not film or not shot_id:
        print("error: shotlist context requires film and shot_id", file=sys.stderr)
        sys.exit(1)
    try:
        result = get_shot_context(
            project_path=project_path,
            film=film,
            shot_id=shot_id,
            media_type=media_type,
            window=getattr(args, "window", 3),
            include_subtitles=getattr(args, "include_subtitles", False),
            include_motif=getattr(args, "include_motif", False),
            include_palette=getattr(args, "include_palette", False),
        )
    except ValueError as exc:
        print(f"✗ {exc}", file=sys.stderr)
        sys.exit(1)
    if getattr(args, "json", False):
        print(json.dumps(result, indent=2))
    else:
        film_label = result["film"]
        print(f"Film: {film_label}  |  Center shot: {result['shot_id']} (index {result['center_index']})")
        print(f"Window ±{result['window']}  |  Total shots: {result['total_shots']}\n")
        for shot in result["shots"]:
            marker = "→" if shot.get("is_center") else " "
            st = shot.get("start_time", "")
            et = shot.get("end_time", "")
            sid = shot.get("shot_id", f"index {shot['index']}")
            line = f"{marker} [{shot['index']:4d}] {sid}  {st} → {et}"
            if "motif" in shot and shot["motif"]:
                line += f"  [motif: {shot['motif']}]"
            if "palette" in shot:
                pal = shot["palette"]
                fg_rgb = pal.get("fg_rgb", "")
                line += f"  [fg: {fg_rgb}]"
            print(line)
            if "subtitles" in shot and shot["subtitles"]:
                for cue in shot["subtitles"]:
                    print(f"        \" {cue}\"")


def _meta_stats(args):
    """Handle: crossing metadata stats."""
    from services.analysis import get_archive_stats
    project_path = prefs.get("path")
    media_type = normalize_media_type(getattr(args, "media", "movie"))
    result = get_archive_stats(project_path=project_path, media_type=media_type)
    if getattr(args, "json", False):
        print(json.dumps(result, indent=2))
    else:
        print(f"Archive statistics ({result['media_type']})")
        print(f"  Films:               {result['film_count']}")
        print(f"    with shotlists:    {result['films_with_shotlist']}")
        print(f"    with annotations:  {result['films_with_annotations']}")
        print(f"    with motifs:       {result['films_with_motifs']}")
        print(f"    with palettes:     {result['films_with_palettes']}")
        print(f"    with subtitles:    {result['films_with_subtitles']}")
        print(f"  Total shots:         {result['total_shots']}")
        print(f"  Annotated shots:     {result['annotated_shots']}")
        print(f"  Silhouette entries:  {result['silhouette_entries']}")
        vocab = result.get("vocabulary", {})
        if vocab:
            print(f"  Vocabulary built:    {vocab.get('built_at', 'n/a')}")
            print(f"  Vocabulary tokens:   {vocab.get('total_tokens', 'n/a')}")
            fields = vocab.get("vocabulary_fields", [])
            if fields:
                print(f"  Vocabulary fields:   {', '.join(fields)}")


def cmd_subtitles(args):
    """Handle: crossing subtitles align <film> [options]."""
    _require_path()
    sub = args.subtitles_subcommand
    if sub == "align":
        _subtitles_align(args)
    else:
        print("✗ subtitles: unknown subcommand.", file=sys.stderr)
        sys.exit(1)


def _subtitles_align(args):
    from services.analysis import align_subtitles_to_shots
    project_path = prefs.get("path")
    media_type = normalize_media_type(getattr(args, "media", "movie"))
    film = getattr(args, "film", None) or getattr(args, "filename", None)
    if not film:
        print("error: subtitles align requires a film argument", file=sys.stderr)
        sys.exit(1)
    scene = getattr(args, "scene", None)
    try:
        result = align_subtitles_to_shots(
            project_path=project_path,
            film=film,
            media_type=media_type,
            scene=scene,
        )
    except ValueError as exc:
        print(f"✗ {exc}", file=sys.stderr)
        sys.exit(1)
    if getattr(args, "json", False):
        print(json.dumps(result, indent=2))
    else:
        title = result["film"]
        has_subs = result.get("has_subtitles", False)
        if not has_subs:
            print(f"✗ No subtitle file found for {title!r}.")
            return
        scene_label = f" (scene {scene})" if scene is not None else ""
        print(f"{title}{scene_label}  —  {result['total_shots']} shots\n")
        for shot in result["aligned_shots"]:
            sid = shot.get("shot_id", "")
            st = shot.get("start_time", "")
            et = shot.get("end_time", "")
            cues = shot.get("subtitle_cues", [])
            if cues:
                cue_texts = " / ".join(c["text"] for c in cues)
                print(f"  {sid}  {st}→{et}  \"{cue_texts}\"")
            else:
                print(f"  {sid}  {st}→{et}  [silent]")


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
    _add_tmdb_arg(p_annotate_shot)
    _add_media_arg(p_annotate_shot)
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
    _add_verbose_arg(p_annotate_shot, help="Print per-shot progress to stdout")
    p_annotate_shot.add_argument("--log", action="store_true", help="Write a debug log file alongside the annotation JSON")
    _add_notify_args(p_annotate_shot)
    p_annotate_shot.add_argument(
        "--reload-every", type=int, default=25, dest="reload_every_n_shots", metavar="N",
        help="Reload the model pipeline every N processed shots to prevent output drift (default: 25; set 0 to disable)",
    )
    p_annotate_shot.add_argument(
        "--with-frame", action="store_true", dest="with_frame",
        help="After annotating each movie's shots (in --all mode), also run 'annotate frame' for that movie",
    )
    p_annotate_shot.add_argument(
        "--frame-model",
        default=prefs.get(_MODEL_KEYS["frame_match"], _MODEL_DEFAULTS["frame_match"]),
        dest="frame_model",
        help="CLIP model to use for best-frame detection (default: %(default)s)",
    )
    p_annotate_shot.add_argument(
        "--no-best", action="store_true", dest="no_best",
        help="Skip automatic best-frame detection after shot annotation",
    )

    p_annotate_scene = annotate_sub.add_parser("scene", help="Annotate scene(s)")
    p_annotate_scene.add_argument("filename", nargs="?", default=None, help="Video filename (or use --tmdb)")
    p_annotate_scene.add_argument("scene_number", type=int, help="Scene number")
    p_annotate_scene.add_argument("caption", nargs="?", default=None, help="Annotation text (manual mode only)")
    _add_tmdb_arg(p_annotate_scene)
    _add_media_arg(p_annotate_scene)
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
    _add_verbose_arg(p_annotate_scene, help="Print per-shot progress to stdout")
    p_annotate_scene.add_argument("--log", action="store_true", help="Write a debug log file alongside the annotation JSON")
    _add_notify_args(p_annotate_scene, batch=False)
    p_annotate_scene.add_argument(
        "--reload-every", type=int, default=25, dest="reload_every_n_shots", metavar="N",
        help="Reload the model pipeline every N processed shots to prevent output drift (default: 25; set 0 to disable)",
    )

    p_annotate_frame = annotate_sub.add_parser(
        "frame",
        help="Find best matching frame per shot using CLIP (requires prior 'annotate shot' pass)",
    )
    p_annotate_frame.set_defaults(func=_annotate_frame)
    p_annotate_frame.add_argument(
        "filename", nargs="?", default=None,
        help="Film title keyword or filename (or use --tmdb)",
    )
    _add_tmdb_arg(p_annotate_frame)
    _add_media_arg(p_annotate_frame)
    p_annotate_frame.add_argument(
        "--model",
        default=prefs.get(_MODEL_KEYS["frame_match"], _MODEL_DEFAULTS["frame_match"]),
        help=(
            "CLIP model for frame matching "
            "(default: %(default)s). "
            "Use a local folder under <project>/models/<name> or a HuggingFace repo id."
        ),
    )
    p_annotate_frame.add_argument(
        "--force", action="store_true",
        help="Re-process shots even when best_frame already exists and the description hasn't changed",
    )
    _add_verbose_arg(p_annotate_frame, help="Print per-shot progress to stdout")
    p_annotate_frame.add_argument(
        "--all", action="store_true",
        help="Run frame matching for all registered movies (ignores positional filename / --tmdb)",
    )
    _add_notify_args(p_annotate_frame)

    p_annotate_best = annotate_sub.add_parser(
        "best",
        help="Best-frame utilities (e.g. migrate source/fallback fields into existing annotations)",
    )
    p_annotate_best.set_defaults(func=_annotate_best)
    best_sub = p_annotate_best.add_subparsers(dest="best_action", required=True)

    p_annotate_best_migrate = best_sub.add_parser(
        "migrate",
        help="Backfill 'source' and 'fallback_reason' fields on existing best_frame entries",
    )
    _add_media_arg(p_annotate_best_migrate)

    p_annotate_remove = annotate_sub.add_parser("remove", help="Remove shot annotations for a film")
    p_annotate_remove.set_defaults(func=_annotate_remove)
    p_annotate_remove.add_argument("filename", nargs="?", default=None, help="Video filename (or use --tmdb / --all)")
    _add_tmdb_arg(p_annotate_remove)
    _add_media_arg(p_annotate_remove)
    p_annotate_remove.add_argument("--all", action="store_true", help="Remove annotations for all films in metadata")

    p_annotate_audit = annotate_sub.add_parser("audit", help="Report annotation status per film (complete, incomplete, missing)")
    p_annotate_audit.set_defaults(func=_shotlist_annotate)
    _add_media_arg(p_annotate_audit)

    p_annotate_validate = annotate_sub.add_parser(
        "validate",
        help="Validate annotation JSON and fix comma-separated values in array fields",
    )
    p_annotate_validate.set_defaults(func=_annotate_validate)
    p_annotate_validate.add_argument(
        "filename", nargs="?", default=None,
        help="Fuzzy keyword to match a movie or game title (or use --tmdb / --all)",
    )
    _add_tmdb_arg(p_annotate_validate)
    _add_media_arg(p_annotate_validate)
    p_annotate_validate.add_argument(
        "--all", action="store_true",
        help="Validate annotations for all films/games in metadata",
    )
    _add_dry_run_arg(p_annotate_validate, help="Report issues without writing any changes")

    p_annotate_migrate = annotate_sub.add_parser(
        "migrate",
        help="Migrate annotation JSON files from legacy integer shot_ids to stable <media_id>@fSTART-fEND IDs",
    )
    p_annotate_migrate.set_defaults(func=_annotate_migrate)
    p_annotate_migrate.add_argument(
        "filename", nargs="?", default=None,
        help="Fuzzy keyword to match a movie or game title (or use --tmdb / --all)",
    )
    _add_tmdb_arg(p_annotate_migrate)
    _add_media_arg(p_annotate_migrate)
    p_annotate_migrate.add_argument(
        "--all", action="store_true",
        help="Migrate all annotation files in the project",
    )

    # (moved under 'crossing tool model' — see tool_sub below)

    # generate command group — composition, mosaic, cloud
    p_generate = sub.add_parser("generate", help="Generate content from project data (composition, mosaic, cloud)")
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
    _add_notify_args(p_composition, batch=False)
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
    _add_media_arg(p_mosaic_thumbnails)
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
    _add_notify_args(p_mosaic_thumbnails, batch=False)

    p_mosaic_search = mosaic_sub.add_parser(
        "search",
        help="Mosaic grid of frames matching a shot annotation query",
    )
    p_mosaic_search.add_argument("query", help="Search query (e.g. \"gun\" or \"sunset\")")
    p_mosaic_search.add_argument("scope", nargs="*", help="Fuzzy movie-title filter(s); omit to search all movies")
    p_mosaic_search.add_argument("--title", dest="movie", nargs="+", default=None, metavar="TITLE", help="Fuzzy title filter(s) (named alternative to positional scope)")
    p_mosaic_search.add_argument("--field", default=None, help="Restrict search to one annotation field")
    p_mosaic_search.add_argument("--limit", type=int, default=None, help="Max search results / mosaic tiles")
    p_mosaic_search.add_argument("--all", action="store_true", help="Search all movies (overrides positional scopes)")
    _add_media_arg(p_mosaic_search)
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
    _add_notify_args(p_mosaic_search, batch=False)

    # generate mosaic export
    p_mosaic_export = mosaic_sub.add_parser(
        "export",
        help="Export individual JPEG frames for each search result into a timestamped folder",
    )
    p_mosaic_export.add_argument("query", help="Search query (e.g. \"gun\" or \"sunset\")")
    p_mosaic_export.add_argument("scope", nargs="*", help="Fuzzy movie-title filter(s); omit to search all movies")
    p_mosaic_export.add_argument("--title", dest="movie", nargs="+", default=None, metavar="TITLE", help="Fuzzy title filter(s)")
    p_mosaic_export.add_argument("--field", default=None, help="Restrict search to one annotation field")
    p_mosaic_export.add_argument("--limit", type=int, default=None, help="Max results to export")
    p_mosaic_export.add_argument("--all", action="store_true", help="Search all movies (overrides positional scopes)")
    _add_media_arg(p_mosaic_export)
    p_mosaic_export.add_argument(
        "--frame_pct", type=float, default=0.5, metavar="PCT",
        help="Frame position within each shot: 0.0=start  0.5=middle (default)  1.0=end",
    )
    p_mosaic_export.add_argument("--no-open", action="store_true", dest="no_open", help="Do not open result folder")
    _add_notify_args(p_mosaic_export, batch=False)

    # generate mosaic video
    p_mosaic_video = mosaic_sub.add_parser(
        "video",
        help="Generate a looping video mosaic from shot search results",
    )
    p_mosaic_video.add_argument("query", help="Search query (e.g. \"horse\" or \"sunset\")")
    p_mosaic_video.add_argument(
        "--limit", type=int, default=50,
        help="Maximum number of tiles (default: 50)",
    )
    p_mosaic_video.add_argument(
        "--fps", type=int, default=8,
        help="Output frame rate (default: 8)",
    )
    p_mosaic_video.add_argument(
        "--duration", type=int, default=2,
        help="Loop length in seconds per tile (default: 2)",
    )
    p_mosaic_video.add_argument(
        "--layout", choices=["portrait", "landscape"], default="landscape",
        help="Grid orientation (default: landscape)",
    )
    p_mosaic_video.add_argument("--no-open", action="store_true", dest="no_open", help="Do not open result in desktop viewer")

    # generate mosaic shots
    p_mosaic_shots = mosaic_sub.add_parser(
        "shots",
        help="PDF contact sheet of every shot in a movie (first or best frame per shot)",
    )
    p_mosaic_shots.add_argument(
        "--title", dest="movie", default=None, metavar="TITLE",
        help="Fuzzy title or filename slug fragment (required unless --all is used)",
    )
    p_mosaic_shots.add_argument(
        "--all", action="store_true",
        help="Generate a PDF for every movie in the project",
    )
    p_mosaic_shots.add_argument(
        "--best", action="store_true",
        help="Use precomputed CLIP best-frame PNGs instead of raw first frames",
    )
    _add_media_arg(p_mosaic_shots)
    p_mosaic_shots.add_argument("--output", default=None, metavar="PATH", help="Override output file path (single-movie only)")
    p_mosaic_shots.add_argument("--no-open", action="store_true", dest="no_open", help="Do not open result in desktop viewer")
    p_mosaic_shots.add_argument("--verbose", action="store_true", help="Print progress details while building the PDF")
    _add_notify_args(p_mosaic_shots, batch=True)

    # generate mosaic scenes
    p_mosaic_scenes = mosaic_sub.add_parser(
        "scenes",
        help="PDF contact sheet of shots grouped by scene, with title and scene-number intertitles",
    )
    p_mosaic_scenes.add_argument(
        "--title", dest="movie", default=None, metavar="TITLE",
        help="Fuzzy title or filename slug fragment (required unless --all is used)",
    )
    p_mosaic_scenes.add_argument(
        "--all", action="store_true",
        help="Generate a PDF for every movie in the project",
    )
    p_mosaic_scenes.add_argument(
        "--best", action="store_true",
        help="Use precomputed CLIP best-frame PNGs instead of raw first frames",
    )
    _add_media_arg(p_mosaic_scenes)
    p_mosaic_scenes.add_argument("--output", default=None, metavar="PATH", help="Override output file path (single-movie only)")
    p_mosaic_scenes.add_argument("--no-open", action="store_true", dest="no_open", help="Do not open result in desktop viewer")
    p_mosaic_scenes.add_argument("--verbose", action="store_true", help="Print progress details while building the PDF")
    _add_notify_args(p_mosaic_scenes, batch=True)

    # generate cloud
    p_cloud = generate_sub.add_parser(
        "cloud",
        help="Generate a word-cloud PDF from annotation text",
    )
    p_cloud.set_defaults(func=cmd_cloud)
    p_cloud.add_argument(
        "--scope", default=None, metavar="TITLE",
        help="Movie title / id substring to restrict to (default: all movies)",
    )
    p_cloud.add_argument(
        "--field", default=None, metavar="FIELD",
        help=(
            "Annotation field to read "
            "(e.g. description, objects, action, setting); "
            "omit to aggregate all text fields"
        ),
    )
    _add_media_arg(p_cloud)
    p_cloud.add_argument(
        "--max-words", type=int, default=150, dest="max_words", metavar="N",
        help="Maximum number of words to include (default: 150)",
    )
    p_cloud.add_argument(
        "--min-count", type=int, default=2, dest="min_count", metavar="N",
        help="Minimum word occurrence count to include (default: 2)",
    )
    p_cloud.add_argument(
        "--output", default=None, metavar="PATH",
        help="Override output file path (default: output/clouds/<scope>-<field>-cloud-<stamp>.pdf)",
    )
    p_cloud.add_argument(
        "--no-open", action="store_true", dest="no_open",
        help="Do not open the PDF after saving",
    )
    _add_notify_args(p_cloud, batch=False)
    p_cloud.add_argument(
        "--style", default=None, metavar="STYLE",
        help=(
            "Visual style preset (default: read from saved prefs, else 'default'). "
            "Available: default, western"
        ),
    )
    p_cloud.add_argument(
        "--save-style", action="store_true", dest="save_style",
        help="Save --style as the project default for future runs",
    )
    p_cloud.add_argument(
        "--visualizer", action="store_true",
        help="Open the interactive cloud visualizer instead of saving",
    )

    # generate flipbook
    p_flipbook = generate_sub.add_parser(
        "flipbook",
        help="Generate a cinematic motif flipbook PDF (one page per shot)",
    )
    p_flipbook.set_defaults(func=cmd_flipbook)
    p_flipbook.add_argument(
        "--title", dest="movie", default=None, metavar="TITLE",
        help="Title or filename slug fragment to generate flipbook for",
    )
    p_flipbook.add_argument(
        "--all", action="store_true", dest="all",
        help="Generate flipbooks for all movies",
    )
    _add_tmdb_arg(p_flipbook, help="TMDB ID to identify the movie")
    _add_media_arg(p_flipbook)
    p_flipbook.add_argument(
        "--force", action="store_true",
        help="Overwrite existing flipbook PDF",
    )
    _add_verbose_arg(p_flipbook, help="Print per-page progress")
    p_flipbook.add_argument(
        "--no-open", action="store_true", dest="no_open",
        help="Do not open the PDF after saving",
    )
    p_flipbook.add_argument(
        "--visualizer", action="store_true",
        help="Open the interactive flipbook visualizer instead of saving",
    )
    _add_notify_args(p_flipbook)

    # generate film-title
    p_film_title = generate_sub.add_parser(
        "film-title",
        help="Generate a semantic condensation title for a film (from its motif progression)",
    )
    p_film_title.set_defaults(func=cmd_film_title)
    p_film_title.add_argument(
        "--title", dest="movie", default=None, metavar="TITLE",
        help="Title or filename slug fragment to generate a film title for",
    )
    p_film_title.add_argument(
        "--all", action="store_true", dest="all",
        help="Generate film titles for all movies",
    )
    _add_media_arg(p_film_title)
    p_film_title.add_argument(
        "--model", default="Qwen3-VL-8B-Instruct", metavar="MODEL",
        help="Model name or path (default: Qwen3-VL-8B-Instruct)",
    )
    p_film_title.add_argument(
        "--force", action="store_true",
        help="Regenerate even if a cached title already exists",
    )
    p_film_title.add_argument(
        "--set", default=None, metavar="VALUE",
        help="Manually set the film title motif to VALUE (skips AI generation; requires --title)",
    )
    _add_verbose_arg(p_film_title, help="Print per-movie progress")
    _add_notify_args(p_film_title)

    # index command group
    p_index = sub.add_parser(
        "index",
        help="Build and inspect annotation-embedding indexes and related indexes",
    )
    p_index.set_defaults(func=cmd_index)
    index_sub = p_index.add_subparsers(dest="index_subcommand", required=True)

    p_index_embed = index_sub.add_parser(
        "embed",
        help="Read serialized annotation text lines and generate annotation embeddings",
    )
    p_index_embed.set_defaults(func=cmd_index)
    p_index_embed.add_argument(
        "query",
        nargs="*",
        help="Title keywords to identify the film (e.g. 7th Cavalry)",
    )
    _add_tmdb_arg(p_index_embed, help="TMDb ID of the film (unambiguous alternative to title keywords)")
    _add_media_arg(p_index_embed)
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
    _add_verbose_arg(p_index_embed, help="Print model, input path, output shape, and save confirmation")

    p_index_update = index_sub.add_parser(
        "process",
        help=(
            "Serialize shot annotations and build annotation-embedding indexes.  "
            "Only rebuilds what is missing or stale."
        ),
    )
    p_index_update.set_defaults(func=cmd_index)
    p_index_update.add_argument(
        "query",
        nargs="*",
        help="Title keywords to identify the film (e.g. 7th Cavalry)",
    )
    _add_tmdb_arg(p_index_update, help="TMDb ID of the film (unambiguous alternative to title keywords)")
    _add_media_arg(p_index_update)
    p_index_update.add_argument(
        "--title", dest="movie", default=None, metavar="TITLE",
        help="Title, slug, or partial ID to identify a single film (alternative to positional query)",
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
        "--limit", type=int, default=None, metavar="N",
        help="Limit serialization and embedding to the first N annotation items (useful for testing)",
    )
    _add_verbose_arg(p_index_update, help="Print per-file actions (txt written, npy written, unchanged)")
    _add_notify_args(p_index_update)

    # Alias: `crossing index annotation-embeddings` → same logic as `crossing index process`
    p_index_annot_embed = index_sub.add_parser(
        "annotation-embeddings",
        help=(
            "Alias for 'process'.  "
            "Serialize shot annotations and build annotation-embedding indexes."
        ),
    )
    p_index_annot_embed.set_defaults(func=cmd_index)
    p_index_annot_embed.add_argument(
        "query",
        nargs="*",
        help="Title keywords to identify the film (e.g. 7th Cavalry)",
    )
    _add_tmdb_arg(p_index_annot_embed, help="TMDb ID of the film (unambiguous alternative to title keywords)")
    _add_media_arg(p_index_annot_embed)
    p_index_annot_embed.add_argument(
        "--title", dest="movie", default=None, metavar="TITLE",
        help="Title, slug, or partial ID to identify a single film (alternative to positional query)",
    )
    p_index_annot_embed.add_argument(
        "--model", default=None, metavar="NAME",
        help=(
            "Embedding model name or path.  "
            "Defaults to the 'embed' model role (crossing tool model set embed <name>)."
        ),
    )
    p_index_annot_embed.add_argument(
        "--all", action="store_true",
        help="Process all films that have an annotation JSON",
    )
    p_index_annot_embed.add_argument(
        "--force", action="store_true",
        help="Force a full rebuild even if files appear current",
    )
    p_index_annot_embed.add_argument(
        "--limit", type=int, default=None, metavar="N",
        help="Limit serialization and embedding to the first N annotation items (useful for testing)",
    )
    _add_verbose_arg(p_index_annot_embed, help="Print per-file actions (txt written, npy written, unchanged)")
    _add_notify_args(p_index_annot_embed)

    p_index_vocabulary = index_sub.add_parser(
        "vocabulary",
        help="Build a vocabulary index (per-field token counts) from annotation JSON",
    )
    p_index_vocabulary.set_defaults(func=cmd_index)
    _add_media_arg(p_index_vocabulary)
    p_index_vocabulary.add_argument(
        "--all", action="store_true",
        help="Build index for both movies and gameplay",
    )
    p_index_vocabulary.add_argument(
        "--force", action="store_true",
        help="Rebuild even if a cached index already exists",
    )

    p_index_stats = index_sub.add_parser(
        "stats",
        help="Print corpus-wide project statistics",
        epilog=(
            "Examples:\n"
            "  crossing index stats\n"
            "  crossing index stats --json\n"
            "  crossing index stats --verbose"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p_index_stats.set_defaults(func=cmd_index)
    p_index_stats.add_argument(
        "--json", action="store_true", dest="json",
        help="Output raw JSON instead of formatted text",
    )
    _add_verbose_arg(p_index_stats, help="Print top silhouette labels after the summary")

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
    _add_tmdb_arg(p_index_audit, help="TMDb ID of the film (unambiguous alternative to title keywords)")
    _add_media_arg(p_index_audit)
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
    _add_verbose_arg(p_index_audit, help="Show per-field detail (json, mapping, txt, npy, manifest) for each film")

    p_index_silhouette = index_sub.add_parser(
        "silhouette",
        help="Extract, audit, and manage the silhouette object catalog (transparent PNGs)",
        epilog=(
            "Subcommands:\n"
            "  extract   Extract transparent PNG objects for a label\n"
            "  audit     Show catalog statistics\n"
            "  clear     Delete catalog entries\n\n"
            "Examples:\n"
            "  crossing index silhouette extract horse --field animals --all\n"
            "  crossing index silhouette extract horse --field animals --title Django\n"
            "  crossing index silhouette extract saddle --field objects --tmdb 11969\n"
            "  crossing index silhouette extract cowboy --field characters --shot tmdb_281957@f001240-f001310\n"
            "  crossing index silhouette audit\n"
            "  crossing index silhouette audit --label horse\n"
            "  crossing index silhouette clear --label horse --dry-run"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p_index_silhouette.set_defaults(func=cmd_index)
    silhouette_sub = p_index_silhouette.add_subparsers(dest="silhouette_action", required=True)

    # ── extract ────────────────────────────────────────────────────────────
    p_sil_extract = silhouette_sub.add_parser(
        "extract",
        help=(
            "Run CLIP + SAM3 concept segmentation and save all valid objects as transparent PNGs "
            "in the silhouette catalog"
        ),
        epilog=(
            "Examples:\n"
            "  crossing index silhouette extract horse --field animals --all\n"
            "  crossing index silhouette extract horse --field animals --title Django\n"
            "  crossing index silhouette extract saddle --field objects --tmdb 11969\n"
            "  crossing index silhouette extract cowboy --field characters --shot tmdb_281957@f001240-f001310"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p_sil_extract.set_defaults(func=cmd_index)
    p_sil_extract.add_argument(
        "label",
        nargs="?",
        default=None,
        help=(
            "Object label / vocabulary word to segment (e.g. horse, saddle, cowboy). "
            "Omit when using --fields to auto-expand all vocabulary labels for each listed field."
        ),
    )
    p_sil_extract.add_argument(
        "--field", default=None, metavar="FIELD",
        help=(
            "Annotation field / category to search in (e.g. animals, objects, characters). "
            "Omit to search across all annotation fields (recommended)."
        ),
    )
    p_sil_extract.add_argument(
        "--fields", nargs="+", default=None, metavar="FIELD", dest="fields",
        help=(
            "Expand all vocabulary labels for each listed field and extract them in one pass. "
            "Cannot be combined with a positional LABEL. Requires --all or --title/--tmdb scope. "
            "Example: crossing index silhouette extract "
            "--fields setting objects wearing action humans animals --all"
        ),
    )
    p_sil_extract.add_argument(
        "--title", dest="movie", default=None, metavar="TITLE",
        help="Restrict extraction to this film (title or slug substring)",
    )
    _add_tmdb_arg(p_sil_extract, help="Restrict extraction to the movie with this TMDb ID")
    p_sil_extract.add_argument(
        "--shot", default=None, metavar="SHOT_ID",
        help=(
            "Single-shot mode: process exactly this shot_id "
            "(format: <media_id>@fSTART-fEND)"
        ),
    )
    p_sil_extract.add_argument(
        "--all", action="store_true",
        help="Run extraction across the entire corpus",
    )
    _add_media_arg(p_sil_extract)
    p_sil_extract.add_argument(
        "--model", default=None, metavar="NAME",
        help=(
            "SAM3 model bundle name inside <project>/models/ "
            "(default: segmentation model role, e.g. \"sam3\")"
        ),
    )
    p_sil_extract.add_argument(
        "--frame-model", default=None, dest="frame_model", metavar="NAME",
        help="CLIP model name for best-frame selection (default: frame_match model role)",
    )
    p_sil_extract.add_argument(
        "--start-from-field", default=None, dest="start_from_field", metavar="FIELD",
        help=(
            "When using --fields, skip all fields listed before FIELD and begin processing "
            "from FIELD onwards. Useful for resuming an interrupted --fields run."
        ),
    )
    p_sil_extract.add_argument(
        "--start-from-label", default=None, dest="start_from_label", metavar="LABEL",
        help=(
            "When using --fields, skip all labels (alphabetically) before LABEL within the "
            "first active field. Combined with --start-from-field to resume mid-field. "
            "Has no effect for subsequent fields (they run in full)."
        ),
    )
    p_sil_extract.add_argument(
        "--force", action="store_true",
        help="Re-extract even if catalog entries for this shot already exist",
    )
    _add_dry_run_arg(p_sil_extract, help="List candidate shots without running segmentation or writing files")
    _add_verbose_arg(p_sil_extract, help="Print progress (model loading, frame selection, mask counts, saved paths)")
    _add_notify_args(p_sil_extract)

    # ── backfill-scanned ───────────────────────────────────────────────────
    p_sil_backfill = silhouette_sub.add_parser(
        "backfill-scanned",
        help="Retroactively write scanned sentinels from existing catalog data (one-time recovery)",
        epilog=(
            "Use this once after upgrading from a version without sentinel tracking.\n"
            "It marks every (field, label) pair that already has catalog entries as\n"
            "fully scanned so future --fields runs skip them automatically.\n\n"
            "Labels that were scanned but produced no results will be re-run on the\n"
            "next extract pass — this is fast (annotation text search only, no GPU).\n\n"
            "Example:\n"
            "  crossing index silhouette backfill-scanned"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p_sil_backfill.set_defaults(func=cmd_index)
    _add_media_arg(p_sil_backfill)

    # ── audit ──────────────────────────────────────────────────────────────
    p_sil_audit = silhouette_sub.add_parser(
        "audit",
        help="Print a summary of the silhouette object catalog",
        epilog=(
            "Examples:\n"
            "  crossing index silhouette audit\n"
            "  crossing index silhouette audit --label horse\n"
            "  crossing index silhouette audit --title Django\n"
            "  crossing index silhouette audit --json"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p_sil_audit.set_defaults(func=cmd_index)
    p_sil_audit.add_argument(
        "--label", default=None, metavar="LABEL",
        help="Filter report to a single label",
    )
    p_sil_audit.add_argument(
        "--title", dest="movie", default=None, metavar="TITLE",
        help="Filter report to a single film (title or slug substring)",
    )
    _add_tmdb_arg(p_sil_audit, help="Filter report to the movie with this TMDb ID")
    p_sil_audit.add_argument(
        "--all", action="store_true",
        help="Report across all media types (movies + gameplay)",
    )
    _add_media_arg(p_sil_audit)
    p_sil_audit.add_argument(
        "--json", action="store_true", dest="json",
        help="Output raw JSON instead of formatted text",
    )

    # ── clear ──────────────────────────────────────────────────────────────
    p_sil_clear = silhouette_sub.add_parser(
        "clear",
        help="Delete silhouette catalog entries (use --dry-run first!)",
        epilog=(
            "Examples:\n"
            "  crossing index silhouette clear --label horse --dry-run\n"
            "  crossing index silhouette clear --label horse\n"
            "  crossing index silhouette clear --title Django\n"
            "  crossing index silhouette clear --all"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p_sil_clear.set_defaults(func=cmd_index)
    p_sil_clear.add_argument(
        "--label", default=None, metavar="LABEL",
        help="Delete only objects with this label",
    )
    p_sil_clear.add_argument(
        "--title", dest="movie", default=None, metavar="TITLE",
        help="Delete only objects from this film (title or slug substring)",
    )
    _add_tmdb_arg(p_sil_clear, help="Delete only objects from the movie with this TMDb ID")
    p_sil_clear.add_argument(
        "--all", action="store_true",
        help="Delete the entire catalog for the selected media type",
    )
    _add_media_arg(p_sil_clear)
    _add_dry_run_arg(p_sil_clear, help="Show what would be deleted without removing any files")

    # ── score ─────────────────────────────────────────────────────────────
    p_sil_score = silhouette_sub.add_parser(
        "score",
        help="Compute and persist silhouette quality scores for catalog objects",
        epilog=(
            "Examples:\n"
            "  crossing index silhouette score --all\n"
            "  crossing index silhouette score --label horse\n"
            "  crossing index silhouette score --field animals --rebuild\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p_sil_score.set_defaults(func=cmd_index)
    p_sil_score.add_argument(
        "--label", default=None, metavar="LABEL",
        help="Restrict scoring to a specific label",
    )
    p_sil_score.add_argument(
        "--field", default=None, metavar="FIELD",
        help="Restrict scoring to a specific annotation field (e.g. animals)",
    )
    p_sil_score.add_argument(
        "--rebuild", action="store_true",
        help="Recompute scores even when they already exist on disk",
    )
    _add_media_arg(p_sil_score)
    _add_verbose_arg(p_sil_score, help="Print per-object scoring progress")

    # index palette
    p_index_palette = index_sub.add_parser(
        "palette",
        help="Build and retrieve per-shot colour palettes (foreground / background) from best-frame PNGs",
        epilog=(
            "Examples:\n"
            "  crossing index palette create --all\n"
            "  crossing index palette create --title 'The Searchers'\n"
            "  crossing index palette create --tmdb 12345 --force\n"
            "  crossing index palette get --title 'The Searchers'\n"
            "  crossing index palette get --title 'The Searchers' --shot 4\n"
            "  crossing index palette get --all"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p_index_palette.set_defaults(func=cmd_index)
    palette_sub = p_index_palette.add_subparsers(dest="palette_action", required=True)

    p_index_palette_create = palette_sub.add_parser(
        "create",
        help=(
            "Extract and cache foreground/background colours for every shot "
            "that has a best-frame PNG"
        ),
    )
    p_index_palette_create.set_defaults(func=cmd_index)
    p_index_palette_create.add_argument(
        "--all", action="store_true",
        help="Process every movie in the metadata index",
    )
    p_index_palette_create.add_argument(
        "--title", dest="movie", default=None, metavar="TITLE",
        help="Title or slug substring to identify a single film",
    )
    _add_tmdb_arg(p_index_palette_create, help="TMDb ID of the movie (unambiguous alternative to --title)")
    _add_media_arg(p_index_palette_create)
    p_index_palette_create.add_argument(
        "--force", action="store_true",
        help="Overwrite an existing palette cache instead of skipping",
    )
    _add_verbose_arg(p_index_palette_create, help="Print per-shot colour results and progress")
    _add_notify_args(p_index_palette_create)

    p_index_palette_get = palette_sub.add_parser(
        "get",
        help="Print the cached colour palette JSON for a movie",
    )
    p_index_palette_get.set_defaults(func=cmd_index)
    p_index_palette_get.add_argument(
        "--all", action="store_true",
        help="List palette status for every movie",
    )
    p_index_palette_get.add_argument(
        "--title", dest="movie", default=None, metavar="TITLE",
        help="Title or slug substring to identify a single film",
    )
    _add_tmdb_arg(p_index_palette_get, help="TMDb ID of the movie (unambiguous alternative to --title)")
    _add_media_arg(p_index_palette_get)
    p_index_palette_get.add_argument(
        "--shot", type=int, default=None, metavar="INDEX",
        help="Return palette for only this shot (0-based index)",
    )

    # index motif
    p_index_motif = index_sub.add_parser(
        "motif",
        help="Generate and manage cinematic motifs for shots",
        epilog=(
            "Examples:\n"
            "  crossing index motif generate --all\n"
            "  crossing index motif generate --title 'The Searchers'\n"
            "  crossing index motif attach --all\n"
            "  crossing index motif attach --media gameplay --title 'ce5e0bba'\n"
            "  crossing index motif attach --all --dry-run --verbose\n"
            "  crossing index motif audit --media movie --all\n"
            "  crossing index motif audit --media movie --all --missing-only\n"
            "  crossing index motif audit --media movie --title 'High Noon'"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p_index_motif.set_defaults(func=cmd_index)
    motif_sub = p_index_motif.add_subparsers(dest="motif_action", required=True)

    p_index_motif_generate = motif_sub.add_parser(
        "generate",
        help=(
            "Generate one cinematic motif per shot using local LLM and editable prompts"
        ),
    )
    p_index_motif_generate.set_defaults(func=cmd_index)
    p_index_motif_generate.add_argument(
        "query",
        nargs="*",
        help="Title keywords to identify the film (e.g. 7th Cavalry)",
    )
    p_index_motif_generate.add_argument(
        "--all", action="store_true",
        help="Generate motifs for every movie in the metadata index",
    )
    p_index_motif_generate.add_argument(
        "--title", dest="movie", default=None, metavar="TITLE",
        help="Title or slug substring to identify a single film or gameplay video",
    )
    p_index_motif_generate.add_argument(
        "--video", default=None, metavar="TITLE",
        help="Title/filename substring to identify a single gameplay video (alias for --title)",
    )
    _add_tmdb_arg(p_index_motif_generate, help="TMDb ID of the movie (unambiguous alternative to --title)")
    _add_media_arg(p_index_motif_generate)
    p_index_motif_generate.add_argument(
        "--model", default=None, metavar="NAME",
        help=(
            "Model name for motif generation. "
            "Defaults to the 'annotate' model role "
            "(crossing tool model set annotate <name>)."
        ),
    )
    p_index_motif_generate.add_argument(
        "--force", action="store_true",
        help="Regenerate motifs even if they already exist",
    )
    _add_verbose_arg(p_index_motif_generate, help="Print [001] motif lines while generating")
    _add_notify_args(p_index_motif_generate)

    p_index_motif_attach = motif_sub.add_parser(
        "attach",
        help=(
            "Copy motif values from data/motifs/ sidecars into annotation JSON "
            "as shot.motif (required before annotation-embedding rebuild includes motif)"
        ),
        epilog=(
            "Examples:\n"
            "  crossing index motif attach --all\n"
            "  crossing index motif attach --media gameplay --title ce5e0bba\n"
            "  crossing index motif attach --all --force\n"
            "  crossing index motif attach --all --dry-run --verbose"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p_index_motif_attach.set_defaults(func=cmd_index)
    p_index_motif_attach.add_argument(
        "query",
        nargs="*",
        help="Title keywords to identify the film (e.g. 7th Cavalry)",
    )
    p_index_motif_attach.add_argument(
        "--all", action="store_true",
        help="Attach motifs for every film/video that has an annotation JSON",
    )
    p_index_motif_attach.add_argument(
        "--title", dest="movie", default=None, metavar="TITLE",
        help="Title, slug, or partial ID to identify a single film",
    )
    _add_tmdb_arg(p_index_motif_attach, help="TMDb ID of the film (unambiguous alternative to --title)")
    _add_media_arg(p_index_motif_attach)
    p_index_motif_attach.add_argument(
        "--force", action="store_true",
        help="Overwrite shot.motif when it conflicts with the sidecar value",
    )
    _add_dry_run_arg(p_index_motif_attach, help="Report what would change without modifying any files")
    _add_verbose_arg(p_index_motif_attach, help="Print per-shot detail (added / unchanged / conflict)")

    p_index_motif_audit = motif_sub.add_parser(
        "audit",
        help="Report canonical shot.motif coverage (read-only)",
        epilog=(
            "Examples:\n"
            "  crossing index motif audit --media movie --all\n"
            "  crossing index motif audit --media movie --all --missing-only\n"
            "  crossing index motif audit --media movie --all --zero-only\n"
            "  crossing index motif audit --media movie --all --verbose\n"
            "  crossing index motif audit --media movie --title 'High Noon'"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p_index_motif_audit.set_defaults(func=cmd_index)
    p_index_motif_audit.add_argument(
        "query",
        nargs="*",
        help="Title keywords to identify a single film",
    )
    p_index_motif_audit.add_argument(
        "--all", action="store_true",
        help="Audit every file that has an annotation JSON",
    )
    p_index_motif_audit.add_argument(
        "--title", dest="movie", default=None, metavar="TITLE",
        help="Title or slug substring to identify a single film",
    )
    _add_tmdb_arg(p_index_motif_audit, help="TMDb ID of the film (unambiguous alternative to --title)")
    _add_media_arg(p_index_motif_audit)
    p_index_motif_audit.add_argument(
        "--missing-only", action="store_true",
        help="List only files where at least one shot is missing a motif",
    )
    p_index_motif_audit.add_argument(
        "--zero-only", action="store_true",
        help="List only files where no shots have a motif",
    )
    p_index_motif_audit.add_argument(
        "--limit", type=int, default=None, metavar="N",
        help="Limit the number of files listed",
    )
    _add_verbose_arg(p_index_motif_audit, help="Print the first missing shot_ids per file")

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
    _add_verbose_arg(p_import, help="Print a message as each file import begins")
    p_import.set_defaults(func=cmd_import, _parser=p_import)

    # metadata command group
    p_meta = sub.add_parser("metadata", help="Manage media metadata")
    p_meta.set_defaults(func=cmd_metadata)
    meta_sub = p_meta.add_subparsers(dest="metadata_subcommand", required=True)

    p_meta_get = meta_sub.add_parser("get", help="Get metadata (all, by index, or by filename)")
    p_meta_get.add_argument("query", nargs="?", default=None,
                            help="index (int) or title/filename substring")
    _add_tmdb_arg(p_meta_get)
    _add_media_arg(p_meta_get)
    p_meta_get.add_argument("--markdown", action="store_true", help="Save output as Markdown to <project>/data/markdown/")
    p_meta_get.add_argument("--open", action="store_true", help="Open the saved Markdown file after writing (implies --markdown)")

    p_meta_set = meta_sub.add_parser("set", help="Set/update metadata from a JSON string")
    p_meta_set.add_argument("json_data", metavar="json")

    p_meta_update = meta_sub.add_parser("update", help="Fetch and save metadata for entries missing key fields")
    p_meta_update.add_argument("--file", default=None, help="Update a single file by filename")
    _add_media_arg(p_meta_update)
    p_meta_update.add_argument("--force", action="store_true", help="Force re-fetch metadata for all entries (including duration)")

    p_meta_count = meta_sub.add_parser("count", help="Print the number of metadata entries")
    _add_media_arg(p_meta_count)

    p_meta_list = meta_sub.add_parser("list", help="List entries, optionally filtered by year or director")
    p_meta_list.add_argument("--year", default=None, help="Filter by exact year (e.g. 1956)")
    p_meta_list.add_argument("--director", default=None, help="Filter by director name (case-insensitive substring)")
    p_meta_list.add_argument("--fields", default=None, help="Comma-separated fields to include (e.g. title,year,director)")
    p_meta_list.add_argument("--sort", default=None, help="Field to sort by (e.g. year, director, title)")
    p_meta_list.add_argument("--reverse", action="store_true", help="Reverse the sort order (descending)")
    _add_media_arg(p_meta_list)

    p_meta_prune = meta_sub.add_parser("prune", help="Remove metadata entries with no matching file on disk")
    _add_media_arg(p_meta_prune)
    p_meta_prune.add_argument("--confirm", action="store_true",
                              help="Actually remove the entries (default is a dry run)")

    p_meta_audit = meta_sub.add_parser("audit", help="Report missing metadata, shotlists, and subtitles")
    _add_media_arg(p_meta_audit)

    p_meta_stats = meta_sub.add_parser("stats", help="Archive-level coverage statistics")
    _add_media_arg(p_meta_stats)
    p_meta_stats.add_argument("--json", action="store_true", help="Output as JSON")


    # media audit (alias for metadata audit)
    p_media_audit = media_sub.add_parser("audit", help="Report missing metadata, thumbnails, shotlists, and subtitles")
    _add_media_arg(p_media_audit)
    p_media_audit.set_defaults(func=cmd_media)

    # media update (alias for metadata update)
    p_media_update = media_sub.add_parser("update", help="Fetch and save metadata/thumbnails for entries missing key fields")
    p_media_update.add_argument("--file", default=None, help="Update a single file by filename")
    _add_media_arg(p_media_update)
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
    _add_verbose_arg(p_media_normalize, help="Print per-asset loudness and gain details during normalization")
    _add_notify_args(p_media_normalize)
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
    _add_verbose_arg(p_media_channels, help="Print per-asset channel details during scanning")
    _add_notify_args(p_media_channels)
    p_media_channels.set_defaults(func=cmd_media)

    # media remove
    p_remove = media_sub.add_parser("remove", help="Remove a film and all its associated files")
    p_remove.set_defaults(func=cmd_remove)
    p_remove.add_argument("query", nargs="*", help="Filename or title words to match")
    _add_tmdb_arg(p_remove, help="TMDb ID (unambiguous)")
    _add_media_arg(p_remove, required=True)
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
    p_search.add_argument("--title", dest="movie", nargs="+", default=None, metavar="TITLE", help="Fuzzy title filter(s) (named alternative to positional scope)")
    p_search.add_argument("--field", default=None, help="Restrict search to one annotation field (e.g. objects)")
    p_search.add_argument("--limit", type=int, default=None, help="Max results to return overall")
    p_search.add_argument("--limit-per-item", dest="limit_per_item", type=int, default=None, help="Max results per movie")
    p_search.add_argument("--all", action="store_true", help="Search all movies (overrides positional scopes)")
    p_search.add_argument("--show_count", action="store_true", help="(vocabulary mode) include occurrence counts in output")
    p_search.add_argument("--sort", choices=["alphabetical", "count", "frequency", "rarity"], default="alphabetical", help="(vocabulary mode) sort order; (motifs mode) frequency, alphabetical, or rarity")
    p_search.add_argument("--all-fields", dest="all_fields", action="store_true", help="(vocabulary mode) emit vocabulary for every annotation field as a single JSON object")
    p_search.add_argument("--exclude", nargs="+", default=None, metavar="FIELD", help="(vocabulary mode) exclude one or more fields from output (e.g. --exclude description humans)")
    p_search.add_argument("--format", dest="output_format", choices=["json", "table", "list", "markdown", "bar", "auto"], default="auto", metavar="FORMAT", help="(vocabulary mode) output format: auto (default), json, table, list, markdown, bar")
    p_search.add_argument("--top", type=int, default=None, metavar="N", help="(vocabulary mode) show only the top N results")
    p_search.add_argument("--markdown", action="store_true", help="Save output as Markdown to <project>/data/markdown/")
    p_search.add_argument("--open", action="store_true", help="Open the saved Markdown file after writing (implies --markdown)")
    # Analysis-mode args (motifs, palette, cooccurrence)
    p_search.add_argument("--mode", default=None, metavar="MODE",
                          help="(motifs compare) overlap, frequency, sequence, rare")
    p_search.add_argument("--terms", nargs="+", default=None, metavar="TERM",
                          help="(cooccurrence) two or more annotation terms to intersect/union")
    p_search.add_argument("--cooccurrence-fields", dest="cooccurrence_fields", nargs="+", default=None,
                          metavar="FIELD", help="(cooccurrence) restrict search to these annotation fields")
    p_search.add_argument("--operator", choices=["AND", "OR"], default="AND",
                          help="(cooccurrence) AND = shots matching all terms; OR = any term")
    p_search.add_argument("--warm", action="store_true", help="(palette) shots with warm fg/bg colour (a*>5 or b*>10)")
    p_search.add_argument("--cold", action="store_true", help="(palette) shots with cold fg/bg colour (blue/cyan)")
    p_search.add_argument("--dark", action="store_true", help="(palette) shots with low luminance (< 0.30)")
    p_search.add_argument("--bright", action="store_true", help="(palette) shots with high luminance (> 0.70)")
    p_search.add_argument("--low-chroma", dest="low_chroma", action="store_true",
                          help="(palette) shots with low colour saturation (chroma < 0.15)")
    p_search.add_argument("--high-chroma", dest="high_chroma", action="store_true",
                          help="(palette) shots with high colour saturation (chroma > 0.30)")
    p_search.add_argument("--foreground-only", dest="foreground_only", action="store_true",
                          help="(palette) apply colour filters to foreground region only")
    p_search.add_argument("--background-only", dest="background_only", action="store_true",
                          help="(palette) apply colour filters to background region only")
    p_search.add_argument("--luminance-min", dest="luminance_min", type=float, default=None,
                          help="(palette) minimum luminance threshold (0–1)")
    p_search.add_argument("--luminance-max", dest="luminance_max", type=float, default=None,
                          help="(palette) maximum luminance threshold (0–1)")
    p_search.add_argument("--chroma-min", dest="chroma_min", type=float, default=None,
                          help="(palette) minimum chroma threshold (0–1)")
    p_search.add_argument("--chroma-max", dest="chroma_max", type=float, default=None,
                          help="(palette) maximum chroma threshold (0–1)")
    p_search.add_argument("--width", type=int, default=400, metavar="PX",
                          help="(frames mode) thumbnail width in pixels (default: 400)")
    p_search.add_argument("--save-dir", dest="save_dir", default=None, metavar="DIR",
                          help="(frames mode) directory to save retrieved JPEG thumbnails")
    p_search.add_argument("--json", action="store_true",
                          help="(frames mode) output metadata as JSON instead of human-readable text")
    _add_media_arg(p_search)
    p_search.set_defaults(func=cmd_search)

    # shotlist command group
    p_shotlist = sub.add_parser("shotlist", help="Manage shot and scene cuts and annotations")
    p_shotlist.set_defaults(func=cmd_shotlist)
    p_shotlist.add_argument("--visualizer", action="store_true", help="Open the shot visualizer GUI (all films)")
    _add_media_arg(p_shotlist)
    shotlist_sub = p_shotlist.add_subparsers(dest="shotlist_subcommand", required=False)

    p_shotlist_create = shotlist_sub.add_parser(
        "create",
        help="Create a provisional draft shotlist using motion + temporal sampling (no TransNetV2 required)",
    )
    p_shotlist_create.add_argument(
        "--media-id",
        dest="media_id",
        required=True,
        metavar="MEDIA_ID",
        help="Stable media_id of the source (e.g. game_rdr2_ce5e0bba)",
    )
    _add_media_arg(p_shotlist_create, default="gameplay")
    p_shotlist_create.add_argument("--force", action="store_true", help="Overwrite existing draft shotlist")
    p_shotlist_create.add_argument("--no-open", dest="no_open", action="store_true",
                                   help="Skip opening the Shotlist Visualizer after creation")
    p_shotlist_create.add_argument(
        "--min-shot-sec", dest="min_shot_sec", type=float, default=2.0, metavar="SEC",
        help="Minimum shot duration in seconds (default: 2.0)",
    )
    p_shotlist_create.add_argument(
        "--max-shot-sec", dest="max_shot_sec", type=float, default=30.0, metavar="SEC",
        help="Maximum shot duration before a forced boundary (default: 30.0)",
    )
    p_shotlist_create.add_argument(
        "--motion-threshold", dest="motion_threshold", type=float, default=8.0, metavar="DIFF",
        help="Mean pixel-diff threshold for motion boundary detection (default: 8.0)",
    )
    p_shotlist_create.add_argument(
        "--nudge-sec", dest="nudge_sec", type=float, default=0.5, metavar="SEC",
        help="Half-width of fine-scan window for boundary nudging (default: 0.5; 0 disables)",
    )
    p_shotlist_create.add_argument(
        "--transnet", action="store_true",
        help="Use TransNetV2 as the Phase-1 detector (requires TensorFlow; not recommended for >1h videos)",
    )
    _add_verbose_arg(p_shotlist_create, help="Print per-phase progress during detection")
    _add_notify_args(p_shotlist_create, batch=False)

    p_shotlist_list = shotlist_sub.add_parser("list", help="List all available shotlists")
    _add_media_arg(p_shotlist_list, default=None)
    p_shotlist_list.add_argument("--json", action="store_true", help="Output as JSON")

    p_shotlist_get = shotlist_sub.add_parser("get", help="Get shotlist data for a file")
    p_shotlist_get.add_argument("filename", nargs="?", default=None, help="Video filename (or use --tmdb)")
    _add_tmdb_arg(p_shotlist_get)
    _add_media_arg(p_shotlist_get)
    p_shotlist_get.add_argument("--scene", type=int, default=None, help="Filter by scene number")
    
    p_shotlist_show = shotlist_sub.add_parser("show", help="Show shot or scene data")
    show_sub = p_shotlist_show.add_subparsers(dest="show_type", required=True)
    
    p_show_shot = show_sub.add_parser("shot", help="Show a specific shot")
    p_show_shot.add_argument("filename", nargs="?", default=None, help="Video filename (or use --tmdb)")
    p_show_shot.add_argument("index", type=int, help="Shot index (0-based)")
    _add_tmdb_arg(p_show_shot)
    _add_media_arg(p_show_shot)
    p_show_shot.add_argument("--field", nargs="+", default=None, help="Extract specific fields from caption JSON (e.g. protagonists place actions)")
    p_show_shot.add_argument("--json", action="store_true", help="Output as JSON (raw or filtered by --field)")
    
    p_show_scene = show_sub.add_parser("scene", help="Show all shots in a scene")
    p_show_scene.add_argument("filename", nargs="?", default=None, help="Video filename (or use --tmdb)")
    p_show_scene.add_argument("scene_number", type=int, help="Scene number")
    _add_tmdb_arg(p_show_scene)
    _add_media_arg(p_show_scene)
    p_show_scene.add_argument("--field", nargs="+", default=None, help="Extract specific fields from caption JSON (e.g. protagonists place actions)")
    p_show_scene.add_argument("--json", action="store_true", help="Output as JSON (raw or filtered by --field)")

    p_sl_shot = shotlist_sub.add_parser("shot", help="Shot boundary detection")
    sl_shot_sub = p_sl_shot.add_subparsers(dest="shot_subcommand", required=True)

    p_sl_shot_detect = sl_shot_sub.add_parser("detect", help="Detect shot boundaries using TransNetV2")
    p_sl_shot_detect.add_argument("query", nargs="?", default=None, help="Filename substring to match")
    _add_tmdb_arg(p_sl_shot_detect)
    _add_media_arg(p_sl_shot_detect)
    p_sl_shot_detect.add_argument("--force", action="store_true", help="Overwrite existing shotlist if it exists")
    p_sl_shot_detect.add_argument("--all", action="store_true", help="Process all metadata entries without a shotlist")
    _add_notify_args(p_sl_shot_detect)

    p_sl_scene = shotlist_sub.add_parser("scene", help="Scene-level operations")
    scene_sub = p_sl_scene.add_subparsers(dest="scene_subcommand", required=True)

    p_sl_scene_detect = scene_sub.add_parser(
        "detect",
        help="Detect scene boundaries from shot embeddings",
    )
    p_sl_scene_detect.add_argument(
        "query",
        nargs="?",
        default=None,
        help="Film title substring to match (or use --tmdb / --all)",
    )
    _add_tmdb_arg(p_sl_scene_detect)
    _add_media_arg(p_sl_scene_detect)
    p_sl_scene_detect.add_argument(
        "--force",
        action="store_true",
        help="Overwrite existing Scene values",
    )
    _add_dry_run_arg(p_sl_scene_detect, help="Show proposed scene boundaries without writing")
    _add_verbose_arg(p_sl_scene_detect, help="Print boundary positions (shot indices) for each film")
    p_sl_scene_detect.add_argument(
        "--all",
        action="store_true",
        help="Process all available shotlists",
    )

    p_sl_migrate = shotlist_sub.add_parser(
        "migrate",
        help="Rewrite shotlist CSVs with legacy column names to the canonical naming scheme",
    )
    _add_media_arg(p_sl_migrate, default=None)
    _add_dry_run_arg(p_sl_migrate, help="Report what would change without writing any files")

    p_sl_context = shotlist_sub.add_parser("context", help="Show neighboring shots around a given shot_id")
    p_sl_context.add_argument("film", help="Film title substring, filename, or TMDb ID")
    p_sl_context.add_argument("shot_id", help="Canonical shot identifier (or integer index)")
    p_sl_context.add_argument("--window", type=int, default=3, help="Shots to show on each side (default: 3)")
    p_sl_context.add_argument("--include-subtitles", dest="include_subtitles", action="store_true",
                              help="Attach overlapping subtitle cues to each shot")
    p_sl_context.add_argument("--include-motif", dest="include_motif", action="store_true",
                              help="Attach the motif word to each shot")
    p_sl_context.add_argument("--include-palette", dest="include_palette", action="store_true",
                              help="Attach dominant fg/bg colour to each shot")
    p_sl_context.add_argument("--json", action="store_true", help="Output raw JSON")
    _add_media_arg(p_sl_context)

    p_sl_context_frames = shotlist_sub.add_parser("context-frames", help="Retrieve frame thumbnails for a shot and its neighbors")
    p_sl_context_frames.add_argument("film", help="Film title substring, filename, or TMDb ID")
    p_sl_context_frames.add_argument("shot_id", help="Canonical shot identifier (or integer index)")
    p_sl_context_frames.add_argument("--window", type=int, default=3, help="Shots to show on each side (default: 3)")
    p_sl_context_frames.add_argument("--width", type=int, default=400, help="Thumbnail width in pixels (default: 400)")
    p_sl_context_frames.add_argument("--save-dir", dest="save_dir", default=None, metavar="DIR", help="Directory to save JPEG thumbnails")
    p_sl_context_frames.add_argument("--json", action="store_true", help="Output metadata as JSON")
    _add_media_arg(p_sl_context_frames)

    # media subtitle
    p_subtitle = media_sub.add_parser("subtitle", help="Download and list subtitles")
    p_subtitle.set_defaults(func=cmd_subtitle)
    subtitle_sub = p_subtitle.add_subparsers(dest="subtitle_subcommand", required=True)

    p_sub_fetch = subtitle_sub.add_parser("fetch", help="Download missing subtitles from OpenSubtitles")
    p_sub_fetch.add_argument("query", nargs="*", default=None, help="Filename or title words (e.g. pals saddle)")
    _add_tmdb_arg(p_sub_fetch, help="TMDb ID (unambiguous)")
    p_sub_fetch.add_argument("--all", action="store_true", help="Fetch for all entries without a subtitle")
    p_sub_fetch.add_argument("--force", action="store_true", help="Re-download even if a subtitle already exists")
    _add_media_arg(p_sub_fetch)

    p_sub_list = subtitle_sub.add_parser("list", help="Show subtitle status for all entries")
    _add_media_arg(p_sub_list)

    # subtitles command group (analysis: align to shots)
    p_subtitles = sub.add_parser("subtitles", help="Subtitle analysis tools")
    p_subtitles.set_defaults(func=cmd_subtitles)
    subtitles_sub = p_subtitles.add_subparsers(dest="subtitles_subcommand", required=True)

    p_subtitles_align = subtitles_sub.add_parser(
        "align", help="Align subtitle cues to shotlist entries by time overlap"
    )
    p_subtitles_align.add_argument("film", help="Film title substring, filename, or TMDb ID")
    p_subtitles_align.add_argument("--scene", type=int, default=None,
                                   help="Restrict to one scene number")
    p_subtitles_align.add_argument("--json", action="store_true", help="Output raw JSON")
    _add_media_arg(p_subtitles_align)

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

    _default_key_choices = list(_TOOL_DEFAULT_KEYS) + ["fields", "atomic-fields"]

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

    # backup command group
    p_backup = sub.add_parser("backup", help="Synchronize project folder to an external backup drive")
    p_backup.set_defaults(func=cmd_backup)
    backup_sub = p_backup.add_subparsers(dest="backup_subcommand", required=True)

    p_backup_update = backup_sub.add_parser(
        "update",
        help="Sync project folder contents into backup folder (uses rsync when available)",
    )
    p_backup_update.set_defaults(func=cmd_backup)
    _add_dry_run_arg(p_backup_update, help="Show what would be transferred without copying any files")
    p_backup_update.add_argument(
        "--mirror", dest="mirror", action="store_true",
        help="(reserved — not yet implemented)",
    )

    p_backup_status = backup_sub.add_parser(
        "status",
        help="Print project path, backup path, and disk space",
    )
    p_backup_status.set_defaults(func=cmd_backup)

    p_backup_path = backup_sub.add_parser(
        "path",
        help="Get or set the backup destination folder",
    )
    p_backup_path.set_defaults(func=cmd_backup)
    p_backup_path.add_argument(
        "folder", nargs="?", default=None,
        help="Absolute path to the backup folder (omit to print current value)",
    )

    # visualizer command group — shortcut to all visualizer GUIs
    p_visualizer = sub.add_parser(
        "visualizer",
        help="Open a visualizer GUI (project, shotlist, composition, mosaic, cloud, silhouette)",
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
    _add_media_arg(p_vis_shot)
    p_vis_shot.add_argument(
        "--filename",
        help="Open (or jump to) a specific film by filename",
    )
    p_vis_shot.add_argument(
        "--shot-id",
        dest="shot_id",
        default="",
        help="Jump to a specific shot_id on open",
    )
    p_vis_shot.add_argument(
        "--play",
        action="store_true",
        help="Start playback immediately on open",
    )
    p_vis_shot.add_argument(
        "--loop",
        action="store_true",
        help="Enable loop mode on open",
    )
    p_vis_shot.add_argument(
        "--no-continue",
        dest="no_continue",
        action="store_true",
        help="Disable continue mode on open",
    )
    _add_verbose_arg(p_vis_shot, help="Enable verbose logging in the shotlist visualizer")

    p_vis_composition = visualizer_sub.add_parser(
        "composition",
        help="Open the interactive composition visualizer",
    )
    p_vis_composition.add_argument(
        "query", nargs="?", default="",
        help="Optional initial search query",
    )
    _add_media_arg(p_vis_composition)
    p_vis_composition.set_defaults(visualizer=True, no_open=False, orientation="portrait", output=None, notify=False)

    p_vis_mosaic = visualizer_sub.add_parser(
        "mosaic",
        help="Open the interactive mosaic explorer GUI",
    )
    _add_media_arg(p_vis_mosaic)

    visualizer_sub.add_parser(
        "cloud",
        help="Open the interactive cloud visualizer GUI",
    )

    visualizer_sub.add_parser(
        "metadata",
        help="Open the metadata browser GUI (movies and gameplay cards)",
    )

    p_vis_silhouette = visualizer_sub.add_parser(
        "silhouette",
        help="Browse cached silhouette polygons (from crossing index silhouette)",
    )
    _add_media_arg(p_vis_silhouette)
    p_vis_silhouette.add_argument(
        "--field",
        metavar="NAME",
        default=None,
        help="Show only silhouettes for this annotation field",
    )

    p_vis_palette = visualizer_sub.add_parser(
        "palette",
        help="Browse per-shot foreground/background colour palettes",
    )
    _add_media_arg(p_vis_palette)

    p_vis_flipbook = visualizer_sub.add_parser(
        "flipbook",
        help="Browse cinematic motif flipbook pages (bg color + motif word per shot)",
    )
    _add_media_arg(p_vis_flipbook)

    visualizer_sub.add_parser(
        "book",
        help="Browse imported books as page spreads",
    )

    # ── engraving command ─────────────────────────────────────────────────────
    p_engraving = sub.add_parser("engraving", help="Engraving generation utilities")
    p_engraving.set_defaults(func=cmd_engraving, engraving_subcommand=None)
    engraving_sub = p_engraving.add_subparsers(dest="engraving_subcommand", required=True)

    p_eng_smoke = engraving_sub.add_parser(
        "smoke-test",
        help="Run FLUX.1-Kontext-dev on a preprocessing PNG and write a raw output PNG",
    )
    p_eng_smoke.add_argument(
        "preprocessing_png",
        help="Path to the RGBA preprocessing PNG (passed directly to FLUX as the reference image)",
    )
    p_eng_smoke.add_argument(
        "--out-dir",
        dest="out_dir",
        default=None,
        metavar="DIR",
        help="Directory to write output PNG (default: same directory as input)",
    )
    p_eng_smoke.add_argument(
        "--project-path",
        dest="project_path",
        default=None,
        metavar="PATH",
        help="Project directory (overrides saved prefs)",
    )
    p_eng_smoke.add_argument(
        "--context",
        dest="context_json",
        default=None,
        metavar="JSON",
        help='Silhouette context as a JSON object (e.g. \'{"label":"cowboy","field":"character"}\')'
             " — used for {variable} substitution in the engraving prompt",
    )
    p_eng_smoke.set_defaults(func=cmd_engraving)

    # ── book command ─────────────────────────────────────────────────────────
    p_book = sub.add_parser("book", help="Manage books (create, delete, list, import PDF)")
    p_book.set_defaults(func=cmd_book, book_subcommand=None)
    book_sub = p_book.add_subparsers(dest="book_subcommand", required=False)

    p_book_new = book_sub.add_parser("new", help="Create a new book")
    p_book_new.add_argument("slug", help="Book slug (e.g. cowboy-metaphysics)")
    p_book_new.set_defaults(func=cmd_book)

    p_book_delete = book_sub.add_parser("delete", help="Delete a book and all its contents")
    p_book_delete.add_argument("slug", help="Book slug to delete")
    p_book_delete.add_argument("--force", action="store_true", help="Skip confirmation prompt")
    p_book_delete.set_defaults(func=cmd_book)

    p_book_list = book_sub.add_parser("list", help="List all books in the project")
    p_book_list.set_defaults(func=cmd_book)

    p_book_use = book_sub.add_parser("use", help="Set the current book")
    p_book_use.add_argument("slug", help="Book slug to activate")
    p_book_use.set_defaults(func=cmd_book)

    p_book_current = book_sub.add_parser("current", help="Show the current book")
    p_book_current.set_defaults(func=cmd_book)

    p_book_import = book_sub.add_parser("import", help="Import a PDF into the current book")
    p_book_import.add_argument("pdf", help="Path to the PDF file to import")
    p_book_import.add_argument("--force", action="store_true", help="Overwrite existing PDF")
    p_book_import.set_defaults(func=cmd_book)

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
