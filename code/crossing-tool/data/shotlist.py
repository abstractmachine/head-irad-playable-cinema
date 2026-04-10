"""Shotlist operations: reading, annotating, and querying shot and scene data."""

import csv
from pathlib import Path
from typing import Any

# Maps legacy CSV column names to the canonical temporal field names.
# Used by normalize_shot_fields() to ensure backward-compatible reads.
_TEMPORAL_FIELD_ALIASES: dict[str, str] = {
    "Start": "start_time",
    "End": "end_time",
    "Start_Frame": "start_frame",
    "End_Frame": "end_frame",
}


def normalize_shot_fields(shot: dict) -> dict:
    """Normalize legacy temporal field names to canonical names.

    Maps old-style CSV column names to the explicit, typed equivalents:
        Start       -> start_time   (HH:MM:SS.mmm)
        End         -> end_time     (HH:MM:SS.mmm)
        Start_Frame -> start_frame  (integer)
        End_Frame   -> end_frame    (integer)

    If the canonical name already exists the legacy name is silently dropped
    so that data is never lost or overwritten.
    """
    result = dict(shot)
    for old_key, new_key in _TEMPORAL_FIELD_ALIASES.items():
        if old_key in result and new_key not in result:
            result[new_key] = result.pop(old_key)
    return result


def resolve_filename(project_path: str, tmdb_id: str | None, filename: str | None, media_type: str = "movies") -> str:
    """Resolve TMDb ID or filename to actual filename.
    
    Args:
        project_path: Path to project
        tmdb_id: TMDb ID (if provided, takes precedence)
        filename: Full filename (used if tmdb_id is None)
        media_type: movies or gameplay
    
    Returns:
        Resolved filename
    
    Raises:
        ValueError: If neither tmdb_id nor filename provided, or if tmdb_id not found
    """
    if tmdb_id is not None:
        # Look up filename by TMDb ID
        from data.metadata import get_metadata
        entries = get_metadata(project_path, media_type=media_type)
        for entry in entries:
            if entry.get('tmdb') == str(tmdb_id):
                return entry['filename']
        raise ValueError(f"No file found with TMDb ID: {tmdb_id}")
    elif filename is not None:
        # If it's already an exact match as a filename, return it directly.
        video_dir = Path(project_path) / "media" / "videos" / media_type
        exact = video_dir / filename
        if exact.exists():
            return filename
        # Otherwise treat it as a case-insensitive substring search against
        # actual files on disk (allows short partial names like "10 000 Dollari").
        query = filename.lower()
        candidates = [p.name for p in video_dir.glob("*") if query in p.name.lower()]
        if len(candidates) == 1:
            return candidates[0]
        if len(candidates) > 1:
            # Prefer the one whose stem starts with the query
            starts = [c for c in candidates if c.lower().startswith(query)]
            if len(starts) == 1:
                return starts[0]
            listed = "\n  ".join(candidates)
            raise ValueError(
                f"Ambiguous filename '{filename}' — {len(candidates)} matches:\n  {listed}"
            )
        raise ValueError(f"No video file found matching '{filename}' in {video_dir}")
    else:
        raise ValueError("Must provide either --tmdb or filename")


def get_shotlist_path(project_path: str, filename: str, media_type: str) -> Path:
    """Get the path to a shotlist CSV file."""
    base_name = Path(filename).stem
    return Path(project_path) / "data" / "shotlists" / media_type / f"{base_name}.csv"


def list_shotlists(project_path: str, media_type: str | None = None) -> list[dict[str, Any]]:
    """List all available shotlists with metadata.
    
    Returns list of dicts with: filename, media_type, shot_count, scene_count
    """
    from data.metadata import _all_metadata
    
    # Get all metadata entries and check for shotlists on disk
    all_entries = _all_metadata(project_path, media_type)
    # Build a lookup by filename for quick access
    meta_by_filename = {e['filename']: e for e in all_entries if e.get('filename')}
    shotlists = []

    types_to_check = [media_type] if media_type else ['movies', 'gameplay']
    for mtype in types_to_check:
        shotlist_dir = Path(project_path) / "data" / "shotlists" / mtype
        if not shotlist_dir.is_dir():
            continue
        for csv_path in sorted(shotlist_dir.glob("*.csv")):
            # Try to match back to a metadata entry by stem
            stem = csv_path.stem
            # Find metadata entry whose filename stem matches
            entry = None
            for fn, e in meta_by_filename.items():
                if Path(fn).stem == stem and e.get('media_type', 'movies') == mtype:
                    entry = e
                    break
            if entry is None:
                continue

            with open(csv_path, 'r', encoding='utf-8') as f:
                reader = csv.DictReader(f)
                rows = list(reader)
                shot_count = len(rows)
                scenes = set(row['Scene'] for row in rows if row.get('Scene'))
                scene_count = len(scenes)

            npy_path = Path(project_path) / "data" / "shotlists" / mtype / (stem + ".npy")
            shotlists.append({
                'filename': entry['filename'],
                'title': entry.get('title', ''),
                'year': entry.get('year', ''),
                'tmdb': entry.get('tmdb', ''),
                'media_type': mtype,
                'shot_count': shot_count,
                'scene_count': scene_count,
                'has_encodings': npy_path.exists()
            })
    
    return shotlists


def read_shotlist(project_path: str, filename: str, media_type: str = "movies") -> list[dict[str, Any]]:
    """Read shotlist CSV and return all shots."""
    shotlist_path = get_shotlist_path(project_path, filename, media_type)
    
    if not shotlist_path.exists():
        raise FileNotFoundError(f"Shotlist not found: {shotlist_path}")
    
    with open(shotlist_path, 'r', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        return [normalize_shot_fields(row) for row in reader]


def write_shotlist(project_path: str, filename: str, media_type: str, shots: list[dict[str, Any]]) -> None:
    """Write shotlist data back to CSV."""
    shotlist_path = get_shotlist_path(project_path, filename, media_type)
    
    # Base fieldnames — canonical temporal naming
    fieldnames = ['Ignore', 'Scene', 'start_time', 'end_time']

    # Add frame columns if present
    if shots and any('start_frame' in shot for shot in shots):
        fieldnames.extend(['start_frame', 'end_frame'])
    
    # Add captions
    fieldnames.extend(['Shot_Caption', 'Scene_Caption'])

    with open(shotlist_path, 'w', encoding='utf-8', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction='ignore')
        writer.writeheader()
        writer.writerows(shots)


def annotate_shot(project_path: str, filename: str, shot_index: int, caption: str, media_type: str = "movies") -> None:
    """Add or update annotation for a specific shot (0-indexed)."""
    shots = read_shotlist(project_path, filename, media_type)
    
    if shot_index < 0 or shot_index >= len(shots):
        raise IndexError(f"Shot index {shot_index} out of range (0-{len(shots)-1})")
    
    shots[shot_index]['Shot_Caption'] = caption
    write_shotlist(project_path, filename, media_type, shots)


def annotate_scene(project_path: str, filename: str, scene_number: int, caption: str, media_type: str = "movies") -> None:
    """Add or update annotation for all shots in a scene."""
    shots = read_shotlist(project_path, filename, media_type)
    
    # Find all shots in this scene
    updated = 0
    for shot in shots:
        if shot.get('Scene') == str(scene_number):
            shot['Scene_Caption'] = caption
            updated += 1
    
    if updated == 0:
        raise ValueError(f"Scene {scene_number} not found in shotlist")
    
    write_shotlist(project_path, filename, media_type, shots)


def get_shot(project_path: str, filename: str, shot_index: int, media_type: str = "movies") -> dict[str, Any]:
    """Get a specific shot by index."""
    shots = read_shotlist(project_path, filename, media_type)
    
    if shot_index < 0 or shot_index >= len(shots):
        raise IndexError(f"Shot index {shot_index} out of range (0-{len(shots)-1})")
    
    return shots[shot_index]


def migrate_shotlist_fields(project_path: str, media_type: str | None = None, dry_run: bool = False) -> list[dict]:
    """Migrate all shotlist CSVs from legacy temporal field names to canonical names.

    For each CSV under data/shotlists/{media_type}/:
    - Checks its raw column headers.
    - If any legacy names are found (Start, End, Start_Frame, End_Frame), reads the
      file (which normalizes field names in memory), then writes it back using the
      canonical names and column order.
    - Files already using canonical names are left untouched.

    Returns a list of result dicts, one per CSV:
        {
            "path": str,
            "status": "migrated" | "already_current" | "skipped",
            "old_headers": list[str],    # only for "migrated"
            "shot_count": int,           # only for "migrated"
        }

    Args:
        project_path: Path to the project root.
        media_type: "movies", "gameplay", or None (both).
        dry_run: If True, report what would change but don't write anything.
    """
    _LEGACY_TEMPORAL = set(_TEMPORAL_FIELD_ALIASES.keys())
    types_to_check = [media_type] if media_type else ["movies", "gameplay"]
    results = []

    for mtype in types_to_check:
        shotlist_dir = Path(project_path) / "data" / "shotlists" / mtype
        if not shotlist_dir.is_dir():
            continue

        for csv_path in sorted(shotlist_dir.glob("*.csv")):
            # Read raw headers without normalizing
            with open(csv_path, "r", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                raw_headers = list(reader.fieldnames or [])

            legacy_found = [h for h in raw_headers if h in _LEGACY_TEMPORAL]
            has_dropped = any(h in ("Shot_Source", "Shot_Confidence") for h in raw_headers)

            if not legacy_found and not has_dropped:
                results.append({"path": str(csv_path), "status": "already_current"})
                continue

            # Read rows through the normalizing reader
            with open(csv_path, "r", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                shots = [normalize_shot_fields(row) for row in reader]

            if not dry_run:
                # Determine fieldnames for the rewritten file
                fieldnames = ["Ignore", "Scene", "start_time", "end_time"]
                if any("start_frame" in shot for shot in shots):
                    fieldnames.extend(["start_frame", "end_frame"])
                fieldnames.extend(["Shot_Caption", "Scene_Caption"])

                with open(csv_path, "w", encoding="utf-8", newline="") as f:
                    writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
                    writer.writeheader()
                    writer.writerows(shots)

            results.append({
                "path": str(csv_path),
                "status": "migrated" if not dry_run else "would_migrate",
                "old_headers": legacy_found,
                "shot_count": len(shots),
                "dropped_columns": [h for h in raw_headers if h in ("Shot_Source", "Shot_Confidence")],
            })

    return results


def get_scene_shots(project_path: str, filename: str, scene_number: int, media_type: str = "movies") -> list[dict[str, Any]]:
    """Get all shots in a specific scene."""
    shots = read_shotlist(project_path, filename, media_type)
    scene_shots = [s for s in shots if s.get('Scene') == str(scene_number)]
    
    if not scene_shots:
        raise ValueError(f"Scene {scene_number} not found in shotlist")
    
    return scene_shots
