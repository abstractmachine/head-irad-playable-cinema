"""Canonical path resolution for engraving assets.

Engravings are derivative assets of silhouette catalog objects.  The
canonical database path does **not** include the provider or model name —
that information belongs inside ``engraving.json``, not in the folder path.

Canonical layout::

    <project>/data/engravings/catalog/<media_type>/<filename_stem>/<label>/<object_id>/
        prompt.txt
        request.json
        raw.png
        engraving.png
        engraving.json
"""

from __future__ import annotations

import re
from pathlib import Path

ENGRAVING_SCHEMA_VERSION = "1"


def _safe_part(value: str) -> str:
    value = str(value or "").strip()
    value = re.sub(r"[^a-zA-Z0-9._(){} -]+", "_", value)
    value = re.sub(r"\s+", " ", value)
    value = re.sub(r"_+", "_", value)
    return value.strip(" ._") or "unknown"


def _safe_label(label: str) -> str:
    """Canonical silhouette-style label folder name (lowercase, underscores only)."""
    return re.sub(r"[^a-z0-9_]", "_", str(label or "").lower().strip()) or "unknown"


def label_folder_for_source(source_json: str | Path, meta: dict) -> str:
    """Return the label folder name that matches the silhouette catalog layout.

    In the silhouette catalog an object JSON lives at::

        <filename_stem>/<label_folder>/object_NNNN.json

    so the parent directory name **is** the canonical label folder.  This is
    preferred over re-deriving the name from ``meta["label"]`` to guarantee
    the engraving path mirrors the silhouette path exactly.

    Falls back to ``_safe_label(meta["label"])`` when the parent name is
    missing (e.g. synthetic paths in tests).
    """
    parent_name = Path(source_json).parent.name
    if parent_name:
        return parent_name
    return _safe_label(meta.get("label", "unknown"))


def resolve_silhouette_png(source_json: str | Path, meta: dict) -> Path:
    """Return the sibling silhouette PNG path, raising ``FileNotFoundError`` if absent."""
    source_json = Path(source_json)
    parent = source_json.parent

    png_name = meta.get("png")
    if png_name:
        candidate = parent / png_name
        if candidate.exists():
            return candidate

    candidate = source_json.with_suffix(".png")
    if candidate.exists():
        return candidate

    raise FileNotFoundError(
        f"Could not resolve silhouette PNG for:\n  {source_json}\n"
        f"Tried meta['png']={png_name!r} and sibling PNG:\n  {candidate}"
    )


def engraving_dir_for_source(
    project_path: str,
    source_json: str | Path,
    meta: dict,
) -> Path:
    """Return the canonical engraving directory for a silhouette object JSON."""
    source_json = Path(source_json)

    media_type = meta.get("media_type", "movie")
    filename_stem = meta.get("filename_stem") or Path(meta.get("filename", "unknown")).stem
    label_folder = label_folder_for_source(source_json, meta)
    object_id = source_json.stem

    return (
        Path(project_path)
        / "data"
        / "engravings"
        / "catalog"
        / _safe_part(media_type)
        / _safe_part(filename_stem)
        / label_folder
        / object_id
    )


def engraving_paths(
    project_path: str,
    source_json: str | Path,
    meta: dict,
) -> dict[str, Path]:
    """Return a dict of all canonical paths for a single engraving asset."""
    base = engraving_dir_for_source(project_path, source_json, meta)
    return {
        "dir": base,
        "request": base / "request.json",
        "raw_png": base / "raw.png",
        "engraving_png": base / "engraving.png",
        "metadata": base / "engraving.json",
    }
