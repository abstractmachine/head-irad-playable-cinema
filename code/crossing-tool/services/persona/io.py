"""JSON serialisation helpers for PersonaDocument.

All JSON I/O for the persona pipeline goes through here so that the
format can evolve in one place.
"""

from __future__ import annotations

import json
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
import warnings

from .models import (
    DetectorInfo,
    MovieInfo,
    PersonaAppearance,
    PersonaCluster,
    PersonaDocument,
    SourceInfo,
)

_PERSONA_VERSION = "0.1.0"


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------

def get_persona_json_path(project_path: str, filename: str, media_type: str = "movies") -> Path:
    """Return the canonical output path for a persona JSON file.

    New convention (preferred):
        data/annotations/personas/<media_type>/<stem>.json

    Falls back to the legacy location `data/personas/...` when reading existing files.
    """
    stem = Path(filename).stem
    return Path(project_path) / "data" / "annotations" / "personas" / media_type / f"{stem}.json"


# ---------------------------------------------------------------------------
# Serialisation
# ---------------------------------------------------------------------------

def _appearance_to_dict(a: PersonaAppearance) -> dict:
    return {
        "shot_id": a.shot_id,
        "confidence": round(a.confidence, 4),
        "visibility": a.visibility,
        "bbox": a.bbox,
        "frame_index": a.frame_index,
        "embedding_norm": round(a.embedding_norm, 4),
    }


def _cluster_to_dict(c: PersonaCluster) -> dict:
    return {
        "persona_id": c.persona_id,
        "label": c.label,
        "shots": c.shots,
        "shot_count": c.shot_count,
        "first_shot": c.first_shot,
        "last_shot": c.last_shot,
        "cluster_confidence": round(c.cluster_confidence, 4),
        "ambiguous_with": c.ambiguous_with,
        "appearances": [_appearance_to_dict(a) for a in c.appearances],
        "notes": c.notes,
    }


def document_to_dict(doc: PersonaDocument) -> dict:
    return {
        "movie": {
            "tmdb_id": doc.movie.tmdb_id,
            "title": doc.movie.title,
            "year": doc.movie.year,
            "filename": doc.movie.filename,
        },
        "source": {
            "video_path": doc.source.video_path,
            "shotlist_path": doc.source.shotlist_path,
        },
        "detector": {
            "version": doc.detector.version,
            "method": doc.detector.method,
            "generated_at": doc.detector.generated_at,
            "frames_per_shot": doc.detector.frames_per_shot,
            "cluster_threshold": doc.detector.cluster_threshold,
            "min_shots_to_be_persona": doc.detector.min_shots_to_be_persona,
        },
        "personas": [_cluster_to_dict(c) for c in doc.personas],
        "stats": doc.stats,
    }


def write_persona_json(
    doc: PersonaDocument,
    project_path: str,
    filename: str,
    media_type: str = "movies",
    force: bool = False,
) -> Path:
    """Serialise a PersonaDocument and write it to the canonical path.

    Args:
        doc:          The document to write.
        project_path: Project root directory.
        filename:     Video filename (used to derive the output stem).
        media_type:   'movies' or 'gameplay'.
        force:        If False (default), raise FileExistsError when the
                      output file already exists.

    Returns:
        Path to the written JSON file.
    """
    dest = get_persona_json_path(project_path, filename, media_type)

    if dest.exists() and not force:
        raise FileExistsError(
            f"Persona JSON already exists: {dest}\n"
            "  Pass --force to overwrite."
        )

    dest.parent.mkdir(parents=True, exist_ok=True)
    payload = document_to_dict(doc)

    with dest.open("w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)
        f.write("\n")

    return dest


# ---------------------------------------------------------------------------
# Deserialisation
# ---------------------------------------------------------------------------

def read_persona_json(
    project_path: str,
    filename: str,
    media_type: str = "movies",
) -> PersonaDocument:
    """Load a persona JSON file and return a PersonaDocument.

    Raises:
        FileNotFoundError: if the file does not exist.
    """
    # Prefer new annotations location, but fall back to legacy data/personas
    path_new = Path(project_path) / "data" / "annotations" / "personas" / media_type / f"{Path(filename).stem}.json"
    path_old = Path(project_path) / "data" / "personas" / media_type / f"{Path(filename).stem}.json"

    if path_new.exists():
        path = path_new
    elif path_old.exists():
        warnings.warn(
            f"Persona JSON found in legacy location: {path_old}. This location is deprecated; move files to {path_new}.",
            UserWarning,
        )
        raise FileNotFoundError(
            f"Persona JSON not found at canonical location: {path_new} (legacy found at {path_old})"
        )
    else:
        raise FileNotFoundError(f"Persona JSON not found: {path_new} (or legacy: {path_old})")

    with path.open(encoding="utf-8") as f:
        data = json.load(f)

    movie = MovieInfo(
        tmdb_id=data["movie"]["tmdb_id"],
        title=data["movie"]["title"],
        year=data["movie"].get("year"),
        filename=data["movie"]["filename"],
    )
    source = SourceInfo(
        video_path=data["source"]["video_path"],
        shotlist_path=data["source"]["shotlist_path"],
    )
    det = data["detector"]
    detector = DetectorInfo(
        version=det["version"],
        method=det["method"],
        generated_at=det["generated_at"],
        frames_per_shot=det.get("frames_per_shot", 2),
        cluster_threshold=det.get("cluster_threshold", 0.35),
        min_shots_to_be_persona=det.get("min_shots_to_be_persona", 2),
    )
    personas = []
    for p in data.get("personas", []):
        appearances = [
            PersonaAppearance(
                shot_id=a["shot_id"],
                confidence=a["confidence"],
                visibility=a["visibility"],
                bbox=a["bbox"],
                frame_index=a["frame_index"],
                embedding_norm=a.get("embedding_norm", 0.0),
            )
            for a in p.get("appearances", [])
        ]
        personas.append(PersonaCluster(
            persona_id=p["persona_id"],
            label=p.get("label"),
            shots=p["shots"],
            shot_count=p["shot_count"],
            first_shot=p["first_shot"],
            last_shot=p["last_shot"],
            cluster_confidence=p["cluster_confidence"],
            ambiguous_with=p.get("ambiguous_with", []),
            appearances=appearances,
            notes=p.get("notes"),
        ))

    return PersonaDocument(
        movie=movie,
        source=source,
        detector=detector,
        personas=personas,
        stats=data.get("stats", {}),
    )


def now_iso() -> str:
    """Return current UTC time as ISO 8601 string."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
