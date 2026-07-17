"""Canonical path resolution for engraving assets.

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
    mode: str = "isolated",
) -> Path:
    """Return the canonical engraving directory for a silhouette object JSON.

    Each mode lives in its own sub-directory so isolated and frame engravings
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

        django_1966-f001275-object_0001-isolated.png
        django_1966-f001275-object_0001-frame.png
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
    mode: str = "isolated",
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


def read_engraving_meta(eng_json_path: Path) -> dict | None:
    """Read ``engraving.json`` and migrate legacy / interrupted files in-place.

    Migration rules
    ---------------
    - No ``status`` field, or ``status == "prepared"`` (old name):
        * ``raw.png`` present  → ``"generated"``
        * ``raw.png`` absent   → ``"pending"``
    - ``status == "generating"``:
        The process that was generating did not finish; treat as ``"failed"``.

    Returns ``None`` when the file does not exist.
    """
    if not eng_json_path.exists():
        return None
    try:
        meta = json.loads(eng_json_path.read_text(encoding="utf-8"))
    except Exception:
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
        try:
            eng_json_path.write_text(
                json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8"
            )
        except Exception:
            pass

    return meta


def engraving_status(
    project_path: str,
    source_json: str | Path,
    meta: dict,
    mode: str = "isolated",
) -> str | None:
    """Return the explicit lifecycle status from ``engraving.json``.

    Returns ``None`` when no engraving exists for the given mode.
    Migrates legacy files transparently.
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
