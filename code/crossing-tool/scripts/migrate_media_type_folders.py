"""Migrate a project from the legacy ``movies/`` folder layout to the canonical
``movie/`` layout introduced in Crossing 2.0.

What this script does
---------------------
1.  **Renames directories** — every ``<anything>/movies/`` segment inside the
    project root is renamed to ``<anything>/movie/``.  The rename is done with
    an atomic ``os.rename`` so no data is copied.  Existing ``movie/`` siblings
    are left untouched (their contents are merged by moving the individual files
    across).

2.  **Renames the metadata file** — ``data/metadata/movies.json`` →
    ``data/metadata/movie.json`` (if the canonical file does not already exist).

3.  **Patches JSON files** — every ``*.json`` inside the project is scanned for
    the string ``/movies/`` (as it appears inside path values) and the
    occurrence is replaced with ``/movie/``.  Only files that actually contain
    the substring are rewritten.

4.  **Updates ``media_type`` fields** — JSON objects whose ``"media_type"``
    value is ``"movies"`` have that value updated to ``"movie"``.

Usage
-----
::

    # Preview — print what would change, write nothing
    python scripts/migrate_media_type_folders.py --project /path/to/project --dry-run

    # Apply
    python scripts/migrate_media_type_folders.py --project /path/to/project

Flags
-----
--project PATH   Project root directory (required).
--dry-run        Print what would happen; write nothing.
--quiet          Suppress per-file output; only print the summary.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from pathlib import Path


# ---------------------------------------------------------------------------
# Directory segments that use media_type and need renaming
# ---------------------------------------------------------------------------

# These are all the known subdirectory trees that use media_type as a path
# component.  Expressed as relative paths from the project root, with
# ``{mt}`` as the placeholder for the media_type segment.
_LEGACY_MT = "movies"
_CANONICAL_MT = "movie"


def _find_movies_dirs(root: Path) -> list[Path]:
    """Walk *root* and return every directory whose name is ``movies``."""
    found = []
    for dirpath, dirnames, _ in os.walk(root):
        # Avoid descending into hidden dirs (e.g. .git)
        dirnames[:] = [d for d in sorted(dirnames) if not d.startswith(".")]
        for d in dirnames:
            if d == _LEGACY_MT:
                found.append(Path(dirpath) / d)
    return found


def _rename_or_merge_dir(src: Path, dst: Path, *, dry_run: bool, quiet: bool) -> int:
    """Rename *src* → *dst*.

    If *dst* already exists the contents of *src* are moved into *dst*
    individually (files only; sub-dirs are renamed recursively first via the
    parent walk).  Returns the number of items moved/renamed.
    """
    if not dst.exists():
        if not dry_run:
            src.rename(dst)
        _log(f"  rename  {src}  →  {dst.name}/", quiet)
        return 1

    # dst already exists — move individual files across
    moved = 0
    for item in sorted(src.iterdir()):
        target = dst / item.name
        if item.is_file():
            if not target.exists():
                if not dry_run:
                    item.rename(target)
                _log(f"  move    {item}  →  {dst.name}/{item.name}", quiet)
                moved += 1
            else:
                _log(f"  SKIP    {item}  (target already exists: {target})", quiet)
        elif item.is_dir():
            # recurse
            moved += _rename_or_merge_dir(item, dst / item.name, dry_run=dry_run, quiet=quiet)
    if not dry_run and src.exists() and not any(src.iterdir()):
        src.rmdir()
    return moved


def _log(msg: str, quiet: bool) -> None:
    if not quiet:
        print(msg)


# ---------------------------------------------------------------------------
# JSON patching
# ---------------------------------------------------------------------------

def _patch_json_bytes(raw: bytes) -> tuple[bytes, int]:
    """Replace ``/movies/`` path segments and ``"movies"`` media_type values.

    Returns ``(new_bytes, change_count)``.  If nothing changed, returns the
    original bytes object unchanged.
    """
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw, 0

    # Fast path — skip files with no relevant content
    if "/movies/" not in text and '"movies"' not in text:
        return raw, 0

    data = json.loads(text)
    changes = _patch_value(data)
    if changes == 0:
        return raw, 0

    new_bytes = json.dumps(data, indent=2, ensure_ascii=False).encode("utf-8") + b"\n"
    return new_bytes, changes


def _patch_value(obj: object) -> int:
    """Recursively patch *obj* in-place.  Returns number of changes."""
    changes = 0
    if isinstance(obj, dict):
        for key, val in obj.items():
            if key == "media_type" and val == _LEGACY_MT:
                obj[key] = _CANONICAL_MT
                changes += 1
            elif isinstance(val, str) and f"/{_LEGACY_MT}/" in val:
                obj[key] = val.replace(f"/{_LEGACY_MT}/", f"/{_CANONICAL_MT}/")
                changes += 1
            elif isinstance(val, (dict, list)):
                changes += _patch_value(val)
    elif isinstance(obj, list):
        for i, item in enumerate(obj):
            if isinstance(item, str) and f"/{_LEGACY_MT}/" in item:
                obj[i] = item.replace(f"/{_LEGACY_MT}/", f"/{_CANONICAL_MT}/")
                changes += 1
            elif isinstance(item, (dict, list)):
                changes += _patch_value(item)
    return changes


def _patch_json_files(root: Path, *, dry_run: bool, quiet: bool) -> tuple[int, int]:
    """Patch all *.json files under *root*.  Returns (files_changed, total_changes)."""
    files_changed = 0
    total_changes = 0
    for json_path in sorted(root.rglob("*.json")):
        if any(part.startswith(".") for part in json_path.parts):
            continue
        try:
            raw = json_path.read_bytes()
        except OSError as exc:
            print(f"  WARN  {json_path}: read error: {exc}", file=sys.stderr)
            continue
        new_raw, n = _patch_json_bytes(raw)
        if n > 0:
            _log(f"  patch   {json_path.relative_to(root)}  ({n} change{'s' if n != 1 else ''})", quiet)
            files_changed += 1
            total_changes += n
            if not dry_run:
                json_path.write_bytes(new_raw)
    return files_changed, total_changes


# ---------------------------------------------------------------------------
# Metadata file rename
# ---------------------------------------------------------------------------

def _rename_metadata_file(root: Path, *, dry_run: bool, quiet: bool) -> int:
    """Rename ``data/metadata/movies.json`` → ``data/metadata/movie.json``."""
    src = root / "data" / "metadata" / f"{_LEGACY_MT}.json"
    dst = root / "data" / "metadata" / f"{_CANONICAL_MT}.json"
    if not src.exists():
        return 0
    if dst.exists():
        _log(f"  SKIP    {src.relative_to(root)}  (canonical file already exists)", quiet)
        return 0
    _log(f"  rename  {src.relative_to(root)}  →  {dst.name}", quiet)
    if not dry_run:
        src.rename(dst)
    return 1


# ---------------------------------------------------------------------------
# Main migration
# ---------------------------------------------------------------------------

def migrate(project_path: str, *, dry_run: bool, quiet: bool) -> None:
    root = Path(project_path).resolve()
    if not root.is_dir():
        print(f"✗  Project path does not exist: {root}", file=sys.stderr)
        sys.exit(1)

    print(f"Project: {root}")
    if dry_run:
        print("[DRY RUN] — no files will be written or renamed\n")

    # 1. Rename / merge directories
    print("── Step 1: rename movies/ directories ──────────────────────────")
    movies_dirs = _find_movies_dirs(root)
    if movies_dirs:
        # Process deepest paths first so parent renames don't invalidate children
        for d in sorted(movies_dirs, key=lambda p: len(p.parts), reverse=True):
            dst = d.parent / _CANONICAL_MT
            _rename_or_merge_dir(d, dst, dry_run=dry_run, quiet=quiet)
        dirs_found = len(movies_dirs)
    else:
        _log("  (none found)", quiet)
        dirs_found = 0

    # 2. Rename metadata file
    print("\n── Step 2: rename data/metadata/movies.json ─────────────────────")
    meta_renamed = _rename_metadata_file(root, dry_run=dry_run, quiet=quiet)
    if meta_renamed == 0 and not quiet:
        print("  (nothing to do)")

    # 3. Patch JSON files
    print("\n── Step 3: patch /movies/ paths and media_type fields in JSONs ──")
    files_changed, total_changes = _patch_json_files(root, dry_run=dry_run, quiet=quiet)
    if files_changed == 0 and not quiet:
        print("  (nothing to patch)")

    # Summary
    print()
    if dry_run:
        print(
            f"[DRY RUN] Would rename {dirs_found} director{'y' if dirs_found == 1 else 'ies'}, "
            f"{meta_renamed} metadata file, "
            f"patch {files_changed} JSON file(s) ({total_changes} value(s))."
        )
    else:
        print(
            f"Done.  Renamed {dirs_found} director{'y' if dirs_found == 1 else 'ies'}, "
            f"{meta_renamed} metadata file, "
            f"patched {files_changed} JSON file(s) ({total_changes} value(s))."
        )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Migrate a Crossing project from the legacy movies/ layout to movie/."
    )
    parser.add_argument(
        "--project",
        required=True,
        help="Project root directory.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would happen; write nothing.",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="Suppress per-file output; only print the summary.",
    )
    args = parser.parse_args()
    migrate(args.project, dry_run=args.dry_run, quiet=args.quiet)


if __name__ == "__main__":
    main()
