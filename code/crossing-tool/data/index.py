"""Index services: serialization of annotation items for embedding pipelines.

This module provides utilities for converting annotation JSON items into
plain-text representations suitable for downstream indexing and embedding.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def load_mapping(project_path: str) -> dict:
    """Load the serialization mapping from <project>/preferences/data/mapping.yaml.

    Returns the contents of the top-level ``mapping`` key in the YAML file.

    Raises:
        FileNotFoundError: If the YAML file does not exist at the expected path.
        ValueError: If the YAML structure is missing the required ``mapping`` key.
    """
    mapping_path = Path(project_path) / "preferences" / "data" / "mapping.yaml"
    if not mapping_path.exists():
        raise FileNotFoundError(f"Mapping file not found: {mapping_path}")

    try:
        import yaml
    except ImportError as exc:
        raise ImportError(
            "PyYAML is required for mapping support. Install with: pip install pyyaml"
        ) from exc

    with mapping_path.open("r", encoding="utf-8") as f:
        raw = yaml.safe_load(f)

    if not isinstance(raw, dict) or "mapping" not in raw:
        raise ValueError(
            f"Invalid mapping YAML at {mapping_path}: "
            "expected a top-level 'mapping' key"
        )

    return raw["mapping"]


def serialize_annotation_item(item: dict, mapping: dict) -> str:
    """Serialize one annotation item to a single line of text.

    Each field value is stringified according to its type:
    - str  → used as-is (stripped)
    - list → joined with ", "
    - None / missing → skipped when skip_empty is True, otherwise ""

    Args:
        item:    One entry from the annotation JSON list.  Expected shape:
                 ``{ movie: {...}, annotation: {...},
                     shot: { shot_id: int, annotation: { field: value, ... } } }``
        mapping: Parsed mapping config dict with keys:
                 ``fields``, ``include_labels``, ``separator``, ``skip_empty``.

    Returns:
        A single-line string with the configured fields joined by the separator.
        Returns an empty string if all fields are empty/missing and skip_empty is True.
    """
    fields: list[str] = mapping.get("fields", [])
    include_labels: bool = mapping.get("include_labels", True)
    separator: str = mapping.get("separator", " | ")
    skip_empty: bool = mapping.get("skip_empty", True)

    shot_annotation: dict = item.get("shot", {}).get("annotation", {})

    parts: list[str] = []
    for field in fields:
        value: Any = shot_annotation.get(field)

        if value is None:
            if skip_empty:
                continue
            value_str = ""
        elif isinstance(value, list):
            if not value and skip_empty:
                continue
            value_str = ", ".join(str(v) for v in value)
        else:
            value_str = str(value).strip()
            if not value_str and skip_empty:
                continue

        if include_labels:
            parts.append(f"{field}: {value_str}")
        else:
            parts.append(value_str)

    return separator.join(parts)


def get_text_path(project_path: str, filename: str, media_type: str) -> Path:
    """Return the canonical path for the serialized `.txt` file.

    Sits alongside the annotation JSON:
    ``<project>/data/annotations/shots/<media_type>/<stem>.txt``
    """
    stem = Path(filename).stem
    return Path(project_path) / "data" / "annotations" / "shots" / media_type / f"{stem}.txt"


def write_text_file(
    project_path: str,
    filename: str,
    media_type: str,
    lines: list[str],
    *,
    force: bool = False,
) -> Path:
    """Write serialized text lines to ``<project>/data/index/text/<media_type>/<stem>.txt``.

    Args:
        project_path: Project root path.
        filename:     Source video filename (used to derive the stem).
        media_type:   ``"movies"`` or ``"gameplay"``.
        lines:        Pure serialized payload lines — no display indices.
        force:        Overwrite the file if it already exists.

    Returns:
        The Path where the file was written.

    Raises:
        FileExistsError: If the file already exists and ``force`` is False.
    """
    dest = get_text_path(project_path, filename, media_type)
    if dest.exists() and not force:
        raise FileExistsError(
            f"Output already exists: {dest}\n  Pass --force to overwrite."
        )
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return dest


def load_annotation_items(project_path: str, filename: str, media_type: str) -> list[dict]:
    """Load annotation JSON items for a given film.

    Args:
        project_path: Project root path.
        filename:     Video filename (e.g. ``"7th Cavalry (1956) {tmdb-5678}.mp4"``).
        media_type:   ``"movies"`` or ``"gameplay"``.

    Returns:
        List of annotation item dicts as stored in the shot annotation JSON.

    Raises:
        FileNotFoundError: If the annotation JSON does not exist.
    """
    from generators.annotate import get_annotation_json_path

    json_path = get_annotation_json_path(project_path, filename, media_type)
    if not json_path.exists():
        raise FileNotFoundError(
            f"No annotation JSON found: {json_path}\n"
            f"  Run: crossing annotate shot {filename}"
        )

    with json_path.open("r", encoding="utf-8") as f:
        return json.load(f)
