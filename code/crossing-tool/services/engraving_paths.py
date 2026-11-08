"""Transitional production-layout engraving API.

Pure legacy constructors delegate to data.resource_locations_legacy. Approved
media-first destinations are owned by data.resource_locations; exact registered
lookup is owned by services.resource_resolution. Status repair below is an
existing write workflow and is never invoked by the new read-only resolver.

Engravings are derivative assets of silhouette catalog objects.  The
canonical database path does **not** include the provider or model name —
that information belongs inside ``engraving.json``, not in the folder path.

Two engraving *modes* are supported: ``"isolated"`` and ``"frame"``.  Each
mode lives in its own sub-folder so both can coexist for the same object:

Canonical layout::

    <project>/data/engravings/catalog/<media_type>/<filename_stem>/<label>/<object_id>/<mode>/
        request.json
        raw.png
        <engraving-filename>.png   — named output (e.g. django_1966-f001275-object_0001-isolated.png)
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

import json
import re
from pathlib import Path

ENGRAVING_SCHEMA_VERSION = "2"
ENGRAVING_MODES = ("isolated", "frame")

# Explicit lifecycle states stored in engraving.json["status"].
ENGRAVING_STATUSES = ("pending", "generating", "generated", "failed")


def _safe_part(value: str) -> str:
    from data.resource_locations_legacy import _safe_part as legacy_part
    return legacy_part(value)


def _safe_label(label: str) -> str:
    """Canonical silhouette-style label folder name (lowercase, underscores only)."""
    from data.resource_locations_legacy import _safe_label as legacy_label
    return legacy_label(label)


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
    from data.resource_locations_legacy import label_folder_for_source as legacy_folder
    return legacy_folder(source_json, meta)


def resolve_silhouette_png(source_json: str | Path, meta: dict) -> Path:
    """Return the sibling silhouette PNG path, raising ``FileNotFoundError`` if absent."""
    from data.resource_locations_legacy import silhouette_png
    return silhouette_png(source_json, meta)


def engraving_dir_for_source(
    project_path: str,
    source_json: str | Path,
    meta: dict,
    mode: str = "isolated",
) -> Path:
    """Return the canonical engraving directory for a silhouette object JSON.

    Each mode lives in its own sub-directory so isolated and frame engravings
    can coexist for the same object.
    """
    from data.resource_locations_legacy import engraving_dir_for_source as legacy_directory
    return legacy_directory(project_path, source_json, meta, mode)


def engraving_output_filename(
    source_json: str | Path,
    meta: dict,
    mode: str,
) -> str:
    """Return the named output filename for a generated engraving.

    Format: ``<title_stub>-<frame_id>-<object_id>-<mode>.png``

    Examples::

        django_1966-f001275-object_0001-isolated.png
        django_1966-f001275-object_0001-frame.png
    """
    from data.resource_locations_legacy import engraving_output_filename as legacy_filename
    return legacy_filename(source_json, meta, mode)


def engraving_paths(
    project_path: str,
    source_json: str | Path,
    meta: dict,
    mode: str = "isolated",
) -> dict[str, Path]:
    """Return a dict of all canonical paths for a single engraving asset."""
    from data.resource_locations_legacy import engraving_paths as legacy_paths
    return legacy_paths(project_path, source_json, meta, mode)


def read_engraving_meta(eng_json_path: Path) -> dict | None:
    """Pure JSON lookup: never infer status, write metadata, or repair a run."""
    try:
        metadata = json.loads(Path(eng_json_path).read_text(encoding="utf-8"))
        return metadata if isinstance(metadata, dict) else None
    except (OSError, json.JSONDecodeError):
        return None


def repair_engraving_meta(eng_json_path: Path, *, approved: bool = False) -> dict | None:
    """Explicitly repair legacy / interrupted status in-place.

    Migration rules
    ---------------
    - No ``status`` field, or ``status == "prepared"`` (old name):
        * ``raw.png`` present  → ``"generated"``
        * ``raw.png`` absent   → ``"pending"``
    - ``status == "generating"``:
        The process that was generating did not finish; treat as ``"failed"``.

    Returns ``None`` when the file does not exist.
    """
    if not approved:
        raise ValueError("Explicit engraving repair approval required")
    eng_json_path = Path(eng_json_path)
    meta = read_engraving_meta(eng_json_path)
    if meta is None:
        return None

    changed = False
    status = meta.get("status")

    if status not in ENGRAVING_STATUSES:
        # Legacy "prepared" or missing status — infer from filesystem.
        raw_png = eng_json_path.parent / "raw.png"
        meta["status"] = "generated" if raw_png.exists() else "pending"
        changed = True
    elif status == "generating":
        # Process died mid-generation.
        meta["status"] = "failed"
        meta.setdefault("last_error", "interrupted (generation process did not complete)")
        changed = True

    if changed:
        from data.annotate import atomic_write_text
        atomic_write_text(eng_json_path, json.dumps(meta, indent=2, ensure_ascii=False))

    return meta


def read_engraving_meta_legacy(eng_json_path: Path) -> dict | None:
    """Transitional old behavior; explicit repair boundary for unmigrated callers."""
    return repair_engraving_meta(eng_json_path, approved=True)


def engraving_status(
    project_path: str,
    source_json: str | Path,
    meta: dict,
    mode: str = "isolated",
) -> str | None:
    """Return the explicit lifecycle status from ``engraving.json``.

    Returns ``None`` when no engraving exists for the given mode.
    Does not repair missing, legacy or interrupted status.
    """
    paths = engraving_paths(project_path, source_json, meta, mode)
    eng_meta = read_engraving_meta(paths["metadata"])
    return eng_meta.get("status") if eng_meta is not None else None


def engraving_is_generated(
    project_path: str,
    source_json: str | Path,
    meta: dict,
    mode: str = "isolated",
) -> bool:
    """Return ``True`` when the engraving has status ``"generated"``."""
    return engraving_status(project_path, source_json, meta, mode) == "generated"
