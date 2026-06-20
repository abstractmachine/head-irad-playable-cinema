"""Corpus-wide statistics for Crossing projects."""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Any

from data.metadata import load_json_metadata
from data.shotlist import read_shotlist
from data.subtitles import subtitle_path_for
from services.silhouette_catalog import audit_catalog
from services.vocabulary_index import load_vocabulary_index


def _count_annotated_shots(project_path: str, media_type: str = "movie") -> int:
    ann_dir = Path(project_path) / "data" / "annotations" / "shots" / media_type
    if not ann_dir.exists():
        return 0

    total = 0
    for json_file in sorted(ann_dir.glob("*.json")):
        if json_file.name.endswith(".manifest.json"):
            continue
        try:
            payload = json.loads(json_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(payload, list):
            total += sum(1 for entry in payload if isinstance(entry, dict))
    return total


def _count_detected_scenes(project_path: str, metadata: list[dict]) -> int:
    total = 0
    for entry in metadata:
        filename = entry.get("filename")
        if not filename:
            continue
        try:
            shots = read_shotlist(project_path, filename, "movie")
        except FileNotFoundError:
            continue

        scenes = {
            str(row.get("Scene", "")).strip()
            for row in shots
            if str(row.get("Scene", "")).strip()
        }
        total += len(scenes)
    return total


def _count_present_assets(project_path: str, metadata: list[dict], exists_fn) -> int:
    count = 0
    for entry in metadata:
        filename = entry.get("filename")
        if filename and exists_fn(project_path, str(filename)):
            count += 1
    return count


def _shotlist_path_exists(project_path: str, filename: str) -> bool:
    from data.shotlist import get_shotlist_path

    return get_shotlist_path(project_path, filename, "movie").exists()


def _subtitle_path_exists(project_path: str, filename: str) -> bool:
    return subtitle_path_for(project_path, "movie", filename) is not None


def _combined_silhouette_report(project_path: str) -> tuple[int, Counter[str]]:
    total_objects = 0
    label_counts: Counter[str] = Counter()
    for media_type in ("movie", "gameplay"):
        report = audit_catalog(project_path, media_type=media_type)
        total_objects += int(report.get("total_objects", 0))
        label_counts.update(report.get("labels", {}))
    return total_objects, label_counts


def get_top_silhouette_labels(project_path: str, limit: int = 10) -> list[tuple[str, int]]:
    """Return the most common silhouette labels across the corpus."""
    _, label_counts = _combined_silhouette_report(project_path)
    return sorted(label_counts.items(), key=lambda item: (-item[1], item[0]))[:limit]


# ---------------------------------------------------------------------------
# New extended counters
# ---------------------------------------------------------------------------

def _count_best_frames(project_path: str) -> dict[str, int]:
    """Count PNG best-frame images under media/frames/best/<media_type>/."""
    result: dict[str, int] = {}
    base = Path(project_path) / "media" / "frames" / "best"
    if not base.exists():
        return result
    for mt_dir in sorted(base.iterdir()):
        if mt_dir.is_dir():
            result[mt_dir.name] = sum(1 for _ in mt_dir.rglob("*.png"))
    return result


def _count_motifs(project_path: str) -> dict[str, int]:
    """Count shots with a canonical shot.motif string in annotation JSON, by media type."""
    result: dict[str, int] = {}
    base = Path(project_path) / "data" / "annotations" / "shots"
    if not base.exists():
        return result
    for mt_dir in sorted(base.iterdir()):
        if not mt_dir.is_dir():
            continue
        total = 0
        for json_file in mt_dir.glob("*.json"):
            if json_file.name.endswith(".manifest.json"):
                continue
            try:
                entries = json.loads(json_file.read_text(encoding="utf-8"))
                for entry in entries:
                    if not isinstance(entry, dict):
                        continue
                    shot = entry.get("shot")
                    if not isinstance(shot, dict):
                        continue
                    motif = shot.get("motif")
                    if isinstance(motif, str) and motif.strip():
                        total += 1
            except (OSError, json.JSONDecodeError):
                continue
        result[mt_dir.name] = total
    return result


def _count_palettes(project_path: str) -> dict[str, int]:
    """Count palette JSON files that exist, broken down by media type."""
    result: dict[str, int] = {}
    base = Path(project_path) / "data" / "palettes"
    if not base.exists():
        return result
    # palettes live directly under data/palettes/ (one file per media item,
    # named after the video stem, not nested by media type).
    count = sum(1 for f in base.rglob("*.json"))
    if count:
        result["total"] = count
    return result


def _count_embeddings(project_path: str) -> dict[str, int]:
    """Count annotation-embedding ``.annotations.npy`` files under data/annotations/shots/.

    Counts only ``*.annotations.npy`` — the canonical new name.
    Old ambiguous ``*.npy`` files (pre-migration) are intentionally not counted
    here so that the stats reflect the new canonical naming convention.
    """
    result: dict[str, int] = {}
    base = Path(project_path) / "data" / "annotations" / "shots"
    if not base.exists():
        return result
    for mt_dir in sorted(base.iterdir()):
        if mt_dir.is_dir():
            count = sum(1 for f in mt_dir.glob("*.annotations.npy"))
            result[mt_dir.name] = count
    return result


def _count_frame_embeddings(project_path: str) -> dict[str, int]:
    """Count frame-embedding .frames.npy files under data/annotations/shots/<media_type>/.

    Counts only ``*.frames.npy`` — does not count ``*.frames.valid.npy``.
    """
    result: dict[str, int] = {}
    base = Path(project_path) / "data" / "annotations" / "shots"
    if not base.exists():
        return result
    for mt_dir in sorted(base.iterdir()):
        if mt_dir.is_dir():
            count = sum(
                1
                for f in mt_dir.glob("*.frames.npy")
                if not f.name.endswith(".frames.valid.npy")
            )
            result[mt_dir.name] = count
    return result


def _count_shots_with_best_frame(project_path: str) -> dict[str, int]:
    """Count annotation entries that have a best_frame.frame value, per media type."""
    result: dict[str, int] = {}
    base = Path(project_path) / "data" / "annotations" / "shots"
    if not base.exists():
        return result
    for mt_dir in sorted(base.iterdir()):
        if not mt_dir.is_dir():
            continue
        total = 0
        for json_file in mt_dir.glob("*.json"):
            if json_file.name.endswith(".manifest.json"):
                continue
            try:
                entries = json.loads(json_file.read_text(encoding="utf-8"))
                for entry in entries:
                    if not isinstance(entry, dict):
                        continue
                    shot = entry.get("shot")
                    if not isinstance(shot, dict):
                        continue
                    bf = shot.get("best_frame")
                    if bf and isinstance(bf, dict) and bf.get("frame") is not None:
                        total += 1
            except (OSError, json.JSONDecodeError):
                continue
        result[mt_dir.name] = total
    return result


def _count_annotated_shots_by_type(project_path: str) -> dict[str, int]:
    """Count annotation entries per media type."""
    result: dict[str, int] = {}
    base = Path(project_path) / "data" / "annotations" / "shots"
    if not base.exists():
        return result
    for mt_dir in sorted(base.iterdir()):
        if not mt_dir.is_dir():
            continue
        total = 0
        for json_file in mt_dir.glob("*.json"):
            if json_file.name.endswith(".manifest.json"):
                continue
            try:
                payload = json.loads(json_file.read_text(encoding="utf-8"))
                if isinstance(payload, list):
                    total += sum(1 for e in payload if isinstance(e, dict))
            except (OSError, json.JSONDecodeError):
                continue
        result[mt_dir.name] = total
    return result


def get_corpus_stats(project_path: str) -> dict[str, Any]:
    """Return corpus-level project statistics."""
    movie_metadata = load_json_metadata(project_path, "movie")
    gameplay_metadata = load_json_metadata(project_path, "gameplay")

    vocabulary_index = load_vocabulary_index(project_path, "movie")
    vocabulary_terms = int(vocabulary_index.get("meta", {}).get("total_tokens", 0))

    annotated_shots_by_type = _count_annotated_shots_by_type(project_path)
    annotated_shots_movie = annotated_shots_by_type.get("movie", 0)
    annotated_shots_gameplay = annotated_shots_by_type.get("gameplay", 0)
    annotated_shots = annotated_shots_movie + annotated_shots_gameplay

    detected_scenes = _count_detected_scenes(project_path, movie_metadata)

    silhouette_objects, label_counts = _combined_silhouette_report(project_path)

    subtitle_files = _count_present_assets(project_path, movie_metadata, _subtitle_path_exists)
    shotlists = _count_present_assets(project_path, movie_metadata, _shotlist_path_exists)

    best_frames_by_type = _count_best_frames(project_path)
    motifs_by_type = _count_motifs(project_path)
    palettes = _count_palettes(project_path)
    embeddings_by_type = _count_embeddings(project_path)
    frame_embeddings_by_type = _count_frame_embeddings(project_path)
    shots_with_best_frame_by_type = _count_shots_with_best_frame(project_path)

    return {
        # Media
        "movie_videos": len(movie_metadata),
        "gameplay_videos": len(gameplay_metadata),
        # Annotations
        "annotated_shots": annotated_shots,
        "annotated_shots_movie": annotated_shots_movie,
        "annotated_shots_gameplay": annotated_shots_gameplay,
        "detected_scenes": detected_scenes,
        # Best frames (CLIP-selected representative frames)
        "best_frames": sum(best_frames_by_type.values()),
        "best_frames_by_type": best_frames_by_type,
        "shots_with_best_frame": sum(shots_with_best_frame_by_type.values()),
        "shots_with_best_frame_by_type": shots_with_best_frame_by_type,
        # Motifs
        "motifs": sum(motifs_by_type.values()),
        "motifs_by_type": motifs_by_type,
        # Palettes
        "palettes": palettes.get("total", 0),
        # Annotation embeddings (.npy, excludes .frames.npy)
        "embeddings": sum(embeddings_by_type.values()),
        "embeddings_by_type": embeddings_by_type,
        # Frame embeddings (.frames.npy)
        "frame_embeddings": sum(frame_embeddings_by_type.values()),
        "frame_embeddings_by_type": frame_embeddings_by_type,
        # Vocabulary
        "vocabulary_terms": vocabulary_terms,
        # Silhouettes
        "silhouette_objects": silhouette_objects,
        "silhouette_labels": len(label_counts),
        # Assets
        "subtitle_files": subtitle_files,
        "shotlists": shotlists,
    }