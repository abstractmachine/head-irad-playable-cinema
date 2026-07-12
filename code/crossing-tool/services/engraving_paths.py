"""Canonical path resolution for engraving assets.

Engravings are derivative assets of silhouette catalog objects.  The
canonical database path does **not** include the provider or model name —
that information belongs inside ``engraving.json``, not in the folder path.

Two engraving *modes* are supported: ``"silhouette"`` and ``"full"``.  Each
mode lives in its own sub-folder so both can coexist for the same object:

Canonical layout::

    <project>/data/engravings/catalog/<media_type>/<filename_stem>/<label>/<object_id>/<mode>/
        request.json
        raw.png
        <engraving-filename>.png   — named output (e.g. django_1966-f001275-object_0001-silhouette.png)
        engraving.json

``raw.png`` is the direct model output; the named output is the curated copy
that other pipeline stages should reference.

Output filename format::

    <title_stub>-<frame_id>-<object_id>-<mode>.png

where *title_stub* is a filesystem-safe movie title abbreviation,
*frame_id* is ``f<NNNNNN>`` derived from the ``frame`` field, and
*object_id* is the stem of the source JSON (e.g. ``object_0001``).
"""

from __future__ import annotations

import re
from pathlib import Path

ENGRAVING_SCHEMA_VERSION = "2"
ENGRAVING_MODES = ("silhouette", "full")


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
    mode: str = "silhouette",
) -> Path:
    """Return the canonical engraving directory for a silhouette object JSON.

    Each mode lives in its own sub-directory so silhouette and full engravings
    can coexist for the same object.
    """
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
        / mode
    )


def engraving_output_filename(
    source_json: str | Path,
    meta: dict,
    mode: str,
) -> str:
    """Return the named output filename for a generated engraving.

    Format: ``<title_stub>-<frame_id>-<object_id>-<mode>.png``

    Examples::

        django_1966-f001275-object_0001-silhouette.png
        django_1966-f001275-object_0001-full.png
    """
    source_json = Path(source_json)
    filename_stem = meta.get("filename_stem") or Path(meta.get("filename", "unknown")).stem
    # Build a compact title stub: lowercase, spaces→underscores, strip parens/braces
    title_stub = re.sub(r"[{(][^})]*[})]", "", filename_stem).strip()
    title_stub = re.sub(r"[^a-zA-Z0-9]+", "_", title_stub).strip("_").lower()
    title_stub = title_stub or "unknown"

    frame = meta.get("frame")
    frame_id = f"f{int(frame):06d}" if frame is not None else "f000000"

    object_id = source_json.stem
    return f"{title_stub}-{frame_id}-{object_id}-{mode}.png"


def engraving_paths(
    project_path: str,
    source_json: str | Path,
    meta: dict,
    mode: str = "silhouette",
) -> dict[str, Path]:
    """Return a dict of all canonical paths for a single engraving asset."""
    base = engraving_dir_for_source(project_path, source_json, meta, mode)
    out_name = engraving_output_filename(source_json, meta, mode)
    return {
        "dir": base,
        "request": base / "request.json",
        "raw_png": base / "raw.png",
        "engraving_png": base / out_name,
        "metadata": base / "engraving.json",
    }


def engraving_is_generated(
    project_path: str,
    source_json: str | Path,
    meta: dict,
    mode: str = "silhouette",
) -> bool:
    """Return True when ``raw.png`` already exists for the given mode."""
    paths = engraving_paths(project_path, source_json, meta, mode)
    return paths["raw_png"].exists()
