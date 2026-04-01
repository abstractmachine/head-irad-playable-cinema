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
    
    # Get all metadata entries that have shotlists
    all_entries = _all_metadata(project_path, media_type)
    shotlists = []
    
    for entry in all_entries:
        if entry.get('shotlist') == 'true':
            shotlist_path = get_shotlist_path(
                project_path, 
                entry['filename'], 
                entry.get('media_type', 'movies')
            )
            
            if shotlist_path.exists():
                # Count shots and scenes
                with open(shotlist_path, 'r', encoding='utf-8') as f:
                    reader = csv.DictReader(f)
                    rows = list(reader)
                    shot_count = len(rows)
                    scenes = set(row['Scene'] for row in rows if row.get('Scene'))
                    scene_count = len(scenes)
                
                shotlists.append({
                    'filename': entry['filename'],
                    'title': entry.get('title', ''),
                    'year': entry.get('year', ''),
                    'tmdb': entry.get('tmdb', ''),
                    'media_type': entry.get('media_type', 'movies'),
                    'shot_count': shot_count,
                    'scene_count': scene_count,
                    'has_encodings': entry.get('encodings') == 'true'
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
    
    fieldnames = ['Ignore', 'Scene', 'Start', 'End', 'Shot_Caption', 'Scene_Caption']
    
    with open(shotlist_path, 'w', encoding='utf-8', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
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
