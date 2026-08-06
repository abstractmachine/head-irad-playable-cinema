"""Migrate per-shot motifs from annotation JSONs into per-movie motif files.

This script moves ``entry["shot"]["motif"]`` data from the annotation JSONs
(under ``<project>/data/annotations/shots/<media_type>/``) into the unified
per-movie motif files (``<project>/data/motifs/<media_type>/<stem>.json``).

Existing title motifs from the old ``<project>/data/film_motifs/`` directory
are also absorbed into the new combined motif files.

Usage
-----
::

    python scripts/archive/migrate_motifs.py --project /path/to/project
    python scripts/archive/migrate_motifs.py --project /path/to/project --media gameplay
    python scripts/archive/migrate_motifs.py --project /path/to/project --dry-run
    python scripts/archive/migrate_motifs.py --project /path/to/project --clean

Flags
-----
--project PATH   Project root directory (required).
--media TYPE     ``movies`` or ``gameplay`` (default: movies).
--dry-run        Print what would happen; write nothing.
--clean          After migrating, strip ``entry["shot"]["motif"]`` from the
                 annotation JSONs (saves space; only do this after verifying
                 the motif files look correct).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def _stem(filename: str) -> str:
    return Path(filename).stem


def migrate(
    project_path: str,
    media_type: str = "movies",
    *,
    dry_run: bool = False,
    clean: bool = False,
) -> None:
    annotations_dir = (
        Path(project_path) / "data" / "annotations" / "shots" / media_type
    )
    film_motifs_dir = (
        Path(project_path) / "data" / "film_motifs" / media_type
    )
    motifs_out_dir = Path(project_path) / "data" / "motifs" / media_type

    if not annotations_dir.exists():
        print(f"  No annotations directory found: {annotations_dir}")
        print("  Nothing to migrate.")
        return

    annotation_files = sorted(
        p for p in annotations_dir.glob("*.json")
        if not p.name.endswith(".manifest.json")
    )
    if not annotation_files:
        print(f"  No annotation JSONs found in {annotations_dir}")
        return

    print(f"  Found {len(annotation_files)} annotation JSON(s) in {annotations_dir}")
    if dry_run:
        print("  [DRY RUN] No files will be written.\n")

    total_files     = 0
    total_shot_motifs = 0
    total_title_motifs = 0
    total_skipped   = 0

    for ann_path in annotation_files:
        stem = ann_path.stem
        filename_stem = stem  # used as display name

        # Load annotation JSON
        try:
            entries: list = json.loads(ann_path.read_text(encoding="utf-8"))
        except Exception as exc:
            print(f"  WARN  {stem}: failed to read annotation JSON: {exc}")
            total_skipped += 1
            continue

        # Resolve the actual video filename from the annotation JSON (preferred)
        # so that the motif doc stores "3 10 To Yuma (1957) {tmdb-14168}.mp4" not just the stem.
        video_filename: str = stem
        for _entry in entries:
            if isinstance(_entry, dict):
                _movie = _entry.get("movie")
                if isinstance(_movie, dict) and _movie.get("filename"):
                    video_filename = str(_movie["filename"])
                    break

        # Extract per-shot motifs ordered by entry position
        shot_motifs: list[dict] = []
        has_any_motif = False
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            shot_data = entry.get("shot")
            if not isinstance(shot_data, dict):
                continue
            motif_obj = shot_data.get("motif")
            shot_id   = str(shot_data.get("shot_id", ""))
            if isinstance(motif_obj, dict) and motif_obj.get("value", "").strip():
                has_any_motif = True
                shot_motifs.append({
                    "shot_id":       shot_id,
                    "value":         motif_obj.get("value", ""),
                    "model":         motif_obj.get("model", ""),
                    "system_prompt": motif_obj.get("system_prompt", ""),
                    "user_prompt":   motif_obj.get("user_prompt", ""),
                    "generated_at":  motif_obj.get("generated_at", ""),
                })
            # Shot entries without a motif simply produce no entry in shot_motifs

        # Load existing title motif from old film_motifs/ directory (if present)
        old_title_path = film_motifs_dir / f"{stem}.json"
        title_motif: dict | None = None
        if old_title_path.exists():
            try:
                candidate = json.loads(old_title_path.read_text(encoding="utf-8"))
                if isinstance(candidate, dict) and candidate.get("value", "").strip():
                    title_motif = candidate
            except Exception as exc:
                print(f"  WARN  {stem}: failed to read old title motif: {exc}")

        if not has_any_motif and title_motif is None:
            print(f"  skip  {filename_stem}: no motifs found, nothing to migrate")
            total_skipped += 1
            continue

        # Load existing motif doc (may already exist from a partial migration or
        # as an old-format title motif left over from a film_motifs/ → motifs/ rename).
        motif_out_path = motifs_out_dir / f"{stem}.json"
        if motif_out_path.exists():
            try:
                existing_raw = json.loads(motif_out_path.read_text(encoding="utf-8"))
                # Detect legacy title-motif file: has "value" key at top level but
                # no "shots" or "filename" key (old film_motifs/<stem>.json format).
                if (
                    isinstance(existing_raw, dict)
                    and "value" in existing_raw
                    and "shots" not in existing_raw
                    and "filename" not in existing_raw
                ):
                    existing_doc: dict = {
                        "filename": video_filename,
                        "media_type": media_type,
                        "title": existing_raw,
                        "shots": [],
                    }
                elif isinstance(existing_raw, dict):
                    existing_doc = existing_raw
                else:
                    existing_doc = {"filename": video_filename, "media_type": media_type, "title": None, "shots": []}
            except Exception:
                existing_doc = {"filename": video_filename, "media_type": media_type, "title": None, "shots": []}
        else:
            existing_doc = {"filename": video_filename, "media_type": media_type, "title": None, "shots": []}

        # Merge: prefer existing doc's shots/title over migrated data (idempotent)
        existing_shot_ids = {
            str(s.get("shot_id", ""))
            for s in existing_doc.get("shots", [])
            if isinstance(s, dict) and s.get("shot_id")
        }
        new_shots = list(existing_doc.get("shots", []))
        added_shots = 0
        for sm in shot_motifs:
            if sm["shot_id"] not in existing_shot_ids:
                new_shots.append(sm)
                added_shots += 1

        new_title = existing_doc.get("title") or title_motif
        migrated_title = title_motif is not None and existing_doc.get("title") is None

        merged_doc = {
            "filename":   video_filename,
            "media_type": media_type,
            "title":      new_title,
            "shots":      new_shots,
        }

        # Report
        n_shot_str = f"{len(shot_motifs)} shot motif(s)"
        n_title_str = "1 title motif" if title_motif else "no title motif"
        status = "migrate" if not dry_run else "would migrate"
        print(
            f"  {status}  {filename_stem}:  {n_shot_str}, {n_title_str}"
            + (f" ({added_shots} new shots merged)" if added_shots < len(shot_motifs) else "")
        )
        total_files += 1
        total_shot_motifs  += len(shot_motifs)
        total_title_motifs += 1 if title_motif else 0

        if dry_run:
            continue

        # Write combined motif doc
        motifs_out_dir.mkdir(parents=True, exist_ok=True)
        motif_out_path.write_text(
            json.dumps(merged_doc, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

        # Optionally strip motif from annotation JSON
        if clean:
            cleaned = False
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                shot_data = entry.get("shot")
                if isinstance(shot_data, dict) and "motif" in shot_data:
                    del shot_data["motif"]
                    cleaned = True
            if cleaned:
                ann_path.write_text(
                    json.dumps(entries, indent=2, ensure_ascii=False),
                    encoding="utf-8",
                )
                print(f"    cleaned  {ann_path.name}")

    print()
    if dry_run:
        print(
            f"  [DRY RUN] Would migrate {total_files} file(s): "
            f"{total_shot_motifs} shot motif(s), {total_title_motifs} title motif(s). "
            f"{total_skipped} skipped."
        )
    else:
        print(
            f"  Done. Migrated {total_files} file(s): "
            f"{total_shot_motifs} shot motif(s), {total_title_motifs} title motif(s). "
            f"{total_skipped} skipped."
        )
        if not clean:
            print(
                "  Tip: run with --clean to strip the motif data from annotation JSONs "
                "once you have verified the motif files look correct."
            )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Migrate per-shot motifs from annotation JSONs to per-movie motif files."
    )
    parser.add_argument(
        "--project",
        required=True,
        help="Project root directory.",
    )
    parser.add_argument(
        "--media",
        default="movies",
        choices=["movies", "gameplay"],
        help="Media type to migrate (default: movies).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would happen; write nothing.",
    )
    parser.add_argument(
        "--clean",
        action="store_true",
        help=(
            "After migrating, strip entry['shot']['motif'] from the annotation JSONs. "
            "Only do this after verifying the motif files look correct."
        ),
    )

    args = parser.parse_args()
    project_path = str(Path(args.project).resolve())

    print(f"Migrating motifs for project: {project_path}")
    print(f"Media type: {args.media}")
    if args.dry_run:
        print("Mode: dry run")
    if args.clean:
        print("Mode: clean (will strip motifs from annotation JSONs)")
    print()

    migrate(
        project_path,
        media_type=args.media,
        dry_run=args.dry_run,
        clean=args.clean,
    )


if __name__ == "__main__":
    main()
