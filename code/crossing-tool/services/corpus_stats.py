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

    path = get_shotlist_path(project_path, filename, "movie")
    if path.exists():
        return True
    legacy = get_shotlist_path(project_path, filename, "movies")
    return legacy.exists()


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


def get_corpus_stats(project_path: str) -> dict[str, Any]:
    """Return corpus-level project statistics."""
    movie_metadata = load_json_metadata(project_path, "movie")
    gameplay_metadata = load_json_metadata(project_path, "gameplay")

    vocabulary_index = load_vocabulary_index(project_path, "movie")
    vocabulary_terms = int(vocabulary_index.get("meta", {}).get("total_tokens", 0))

    annotated_shots = _count_annotated_shots(project_path, "movie")
    detected_scenes = _count_detected_scenes(project_path, movie_metadata)

    silhouette_objects, label_counts = _combined_silhouette_report(project_path)

    subtitle_files = _count_present_assets(project_path, movie_metadata, _subtitle_path_exists)
    shotlists = _count_present_assets(project_path, movie_metadata, _shotlist_path_exists)

    return {
        "movies": len(movie_metadata),
        "gameplay_videos": len(gameplay_metadata),
        "vocabulary_terms": vocabulary_terms,
        "annotated_shots": annotated_shots,
        "detected_scenes": detected_scenes,
        "silhouette_objects": silhouette_objects,
        "silhouette_labels": len(label_counts),
        "subtitle_files": subtitle_files,
        "shotlists": shotlists,
    }