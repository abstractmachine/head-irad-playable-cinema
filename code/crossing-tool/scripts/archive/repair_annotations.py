#!/usr/bin/env python3
"""One-time repair script: clean malformed label items in existing annotation JSON files.

Malformed patterns fixed:
- "dog, horse"  →  "dog", "horse"   (comma-joined labels as a single string)
- '"cat"'       →  "cat"            (quote-wrapped labels)

Only the atomic-label fields are touched: objects, humans, animals.
Free-text fields (setting, text) are left unchanged.

Usage
-----
    python scripts/archive/repair_annotations.py                       # project = cwd
    python scripts/archive/repair_annotations.py --project /path/to/project
    python scripts/archive/repair_annotations.py --dry-run             # preview only
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Allow running from the repo root without installing the package.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from data.annotate import normalize_label_list, _LABEL_LIST_FIELDS
from data.index import load_atomic_fields


# ---------------------------------------------------------------------------
# Core repair logic
# ---------------------------------------------------------------------------

def _repair_annotation(ann: dict, label_fields: frozenset) -> tuple[dict, list[str]]:
    """Return (repaired_dict, list_of_change_descriptions).

    The dict is a new copy; the original is not mutated.
    If nothing changed the list of changes is empty.

    Two repairs are applied:
    1. Case-key mismatch: if a schema field (e.g. ``setting``) is empty/missing
       but an uppercase variant (e.g. ``SETTING``) has data, the value is
       copied down to the lowercase key.  This fixes annotations produced when
       the model returned UPPERCASE keys.
    2. Label normalisation: comma-joined / quote-wrapped values in atomic-label
       fields are split and cleaned.
    """
    from data.annotate import _ANNOTATION_SCHEMA

    repaired = dict(ann)
    changes: list[str] = []

    # --- Repair 1: uppercase-key mismatch ---
    # Fold all keys to lowercase, preferring the non-empty value when both
    # cases appear (e.g. SETTING="forest" wins over setting="").
    # This covers both schema fields and extra model fields (DESCRIPTION, TYPE…).
    folded: dict = {}
    for k, v in ann.items():
        lk = k.lower()
        if lk not in folded or (not folded[lk] and v):
            folded[lk] = v

    if folded != ann:
        # Record which keys changed (uppercase removed / values promoted).
        for k in ann:
            if k != k.lower() and k.lower() in folded:
                changes.append(f"  lowercased key '{k}' → '{k.lower()}'")
        repaired = folded
    else:
        repaired = dict(ann)

    # --- Repair 2: label normalisation ---
    for field in label_fields:
        current = repaired.get(field)
        if not isinstance(current, list):
            continue
        current_strs = [str(v) for v in current]
        fixed = normalize_label_list(current_strs, field, label_fields=label_fields)
        if fixed != current_strs:
            repaired[field] = fixed
            changes.append(
                f"  {field}: {current_strs!r} → {fixed!r}"
            )
    return repaired, changes


def _repair_file(path: Path, dry_run: bool, label_fields: frozenset) -> tuple[int, int]:
    """Process one annotation JSON file.

    Returns (shots_changed, shots_total).
    """
    try:
        raw = path.read_text(encoding="utf-8")
        entries = json.loads(raw)
    except Exception as exc:
        print(f"  [SKIP] {path}: could not read/parse — {exc}", file=sys.stderr)
        return 0, 0

    if not isinstance(entries, list):
        print(f"  [SKIP] {path}: top-level value is not a list", file=sys.stderr)
        return 0, 0

    new_entries = []
    shots_changed = 0
    for entry in entries:
        if not isinstance(entry, dict):
            new_entries.append(entry)
            continue
        shot_block = entry.get("shot")
        if not isinstance(shot_block, dict):
            new_entries.append(entry)
            continue
        annotation = shot_block.get("annotation")
        if not isinstance(annotation, dict):
            new_entries.append(entry)
            continue

        repaired_ann, changes = _repair_annotation(annotation, label_fields)
        if changes:
            shot_id = shot_block.get("shot_id", "<unknown>")
            print(f"  shot {shot_id}:")
            for c in changes:
                print(c)
            shots_changed += 1
            new_entry = dict(entry)
            new_entry["shot"] = dict(shot_block)
            new_entry["shot"]["annotation"] = repaired_ann
            new_entries.append(new_entry)
        else:
            new_entries.append(entry)

    if shots_changed and not dry_run:
        path.write_text(
            json.dumps(new_entries, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

    return shots_changed, len(entries)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description="Repair malformed label items in annotation JSON files."
    )
    parser.add_argument(
        "--project",
        default=".",
        help="Path to the project root (default: current directory).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would change without writing any files.",
    )
    args = parser.parse_args(argv)

    base = Path(args.project).resolve()
    ann_root = base / "data" / "annotations" / "shots"

    # Load the active atomic-label fields for this project (falls back to built-in).
    try:
        label_fields = frozenset(load_atomic_fields(str(base)))
    except Exception:
        label_fields = _LABEL_LIST_FIELDS
    print(f"Atomic label fields: {', '.join(sorted(label_fields))}\n")

    if not ann_root.exists():
        print(f"No annotations directory found at {ann_root}", file=sys.stderr)
        sys.exit(0)

    json_files = sorted(ann_root.rglob("*.json"))
    if not json_files:
        print(f"No annotation JSON files found under {ann_root}")
        sys.exit(0)

    if args.dry_run:
        print("[DRY RUN] No files will be written.\n")

    total_files_changed = 0
    total_shots_changed = 0
    total_shots = 0

    for path in json_files:
        rel = path.relative_to(base)
        shots_changed, shots_total = _repair_file(path, dry_run=args.dry_run, label_fields=label_fields)
        total_shots += shots_total
        if shots_changed:
            total_shots_changed += shots_changed
            total_files_changed += 1
            status = "(dry run — not written)" if args.dry_run else "(written)"
            print(f"  → {rel}: {shots_changed}/{shots_total} shots updated {status}\n")

    print(
        f"\nDone. {total_shots_changed} shot(s) changed across "
        f"{total_files_changed} file(s) "
        f"({total_shots} shots scanned)."
    )


if __name__ == "__main__":
    main()
