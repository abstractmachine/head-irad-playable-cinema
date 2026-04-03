"""Shotlist operations: reading, annotating, and querying shot and scene data."""

import csv
from pathlib import Path
from typing import Any


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
        from services.metadata import get_metadata
        entries = get_metadata(project_path, media_type=media_type)
        for entry in entries:
            if entry.get('tmdb') == str(tmdb_id):
                return entry['filename']
        raise ValueError(f"No file found with TMDb ID: {tmdb_id}")
    elif filename is not None:
        return filename
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
    from services.metadata import _all_metadata
    
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
        return list(reader)


def write_shotlist(project_path: str, filename: str, media_type: str, shots: list[dict[str, Any]]) -> None:
    """Write shotlist data back to CSV."""
    shotlist_path = get_shotlist_path(project_path, filename, media_type)
    
    # Base fieldnames
    fieldnames = ['Ignore', 'Scene', 'Start', 'End']
    
    # Add frame columns if present
    if shots and any('Start_Frame' in shot for shot in shots):
        fieldnames.extend(['Start_Frame', 'End_Frame'])
    
    # Add captions
    fieldnames.extend(['Shot_Caption', 'Scene_Caption'])
    
    # Add Shot_Source and Shot_Confidence if present in any shot (for auto-detected shots)
    if shots and any('Shot_Source' in shot for shot in shots):
        fieldnames.extend(['Shot_Source', 'Shot_Confidence'])
    
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


def get_scene_shots(project_path: str, filename: str, scene_number: int, media_type: str = "movies") -> list[dict[str, Any]]:
    """Get all shots in a specific scene."""
    shots = read_shotlist(project_path, filename, media_type)
    scene_shots = [s for s in shots if s.get('Scene') == str(scene_number)]
    
    if not scene_shots:
        raise ValueError(f"Scene {scene_number} not found in shotlist")
    
    return scene_shots
