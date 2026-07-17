"""Migrate on-disk engraving data from legacy mode folder names to the
canonical names introduced in July 2026.

Renames
-------
- ``silhouette/`` → ``isolated/``   (single-object isolated engraving)
- ``full/``        → ``frame/``     (full-frame recreation)

Also updates the ``mode`` field inside every affected ``engraving.json``
so stored metadata stays consistent with the folder name.

Usage
-----
    python scripts/migrate_engraving_modes.py              # dry-run (safe)
    python scripts/migrate_engraving_modes.py --apply      # apply changes
    python scripts/migrate_engraving_modes.py --project /path/to/project

The script is idempotent: already-migrated directories are skipped.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

LEGACY_TO_CANONICAL = {
    "silhouette": "isolated",
    "full": "frame",
}

ENGRAVINGS_CATALOG_REL = Path("data") / "engravings" / "catalog"


# ---------------------------------------------------------------------------
# Core logic
# ---------------------------------------------------------------------------

def find_legacy_mode_dirs(catalog_root: Path) -> list[tuple[Path, str, str]]:
    """Return list of (dir, old_mode, new_mode) for every legacy mode folder."""
    results = []
    for old_name, new_name in LEGACY_TO_CANONICAL.items():
        for d in sorted(catalog_root.rglob(old_name)):
            if d.is_dir() and d.name == old_name:
                results.append((d, old_name, new_name))
    return results


def migrate(project_path: str, *, apply: bool = False) -> None:
    catalog_root = Path(project_path) / ENGRAVINGS_CATALOG_REL
    if not catalog_root.is_dir():
        print(f"No engravings catalog found at: {catalog_root}")
        return

    entries = find_legacy_mode_dirs(catalog_root)
    if not entries:
        print("Nothing to migrate — no legacy mode directories found.")
        return

    label = "DRY RUN" if not apply else "APPLYING"
    print(f"[{label}]  {len(entries)} director(ies) to rename\n")

    renamed = 0
    json_updated = 0
    skipped = 0
    errors = 0

    for old_dir, old_mode, new_mode in entries:
        new_dir = old_dir.parent / new_mode

        if new_dir.exists():
            print(f"  SKIP (target exists)  {old_dir}")
            skipped += 1
            continue

        print(f"  {old_dir.relative_to(catalog_root)}  →  …/{new_mode}/")

        if apply:
            try:
                old_dir.rename(new_dir)
                renamed += 1
            except OSError as exc:
                print(f"    ERROR renaming: {exc}", file=sys.stderr)
                errors += 1
                continue

            # Update engraving.json mode field
            eng_json = new_dir / "engraving.json"
            if eng_json.exists():
                try:
                    data = json.loads(eng_json.read_text(encoding="utf-8"))
                    if data.get("mode") == old_mode:
                        data["mode"] = new_mode
                        eng_json.write_text(
                            json.dumps(data, indent=2, ensure_ascii=False),
                            encoding="utf-8",
                        )
                        json_updated += 1
                except Exception as exc:
                    print(f"    WARNING: could not update engraving.json: {exc}", file=sys.stderr)
        else:
            renamed += 1  # would-be rename

    print()
    if apply:
        print(f"Done — {renamed} renamed, {json_updated} JSON updated, "
              f"{skipped} skipped, {errors} error(s).")
    else:
        print(f"Dry-run summary — {renamed} would be renamed, "
              f"{skipped} skipped (target already exists).")
        print("Run with --apply to apply changes.")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _find_project_path() -> str | None:
    """Try to read the saved project path from crossing tool preferences."""
    prefs_file = Path.home() / ".crossing" / "prefs.json"
    try:
        data = json.loads(prefs_file.read_text(encoding="utf-8"))
        return data.get("path")
    except Exception:
        return None


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Rename legacy engraving mode folders (silhouette→isolated, full→frame).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--project",
        default=None,
        metavar="PATH",
        help="Project folder path (default: read from saved crossing tool prefs)",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Apply changes (default is a dry run)",
    )
    args = parser.parse_args()

    project_path = args.project or _find_project_path()
    if not project_path:
        print(
            "No project path provided.  "
            "Use --project /path/to/project or run 'crossing tool path <folder>' first.",
            file=sys.stderr,
        )
        sys.exit(1)

    project_path = str(Path(project_path).resolve())
    print(f"Project: {project_path}\n")
    migrate(project_path, apply=args.apply)


if __name__ == "__main__":
    main()
