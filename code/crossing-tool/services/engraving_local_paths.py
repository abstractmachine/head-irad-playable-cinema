"""Canonical path resolution for locally generated engravings.

Mirrors ``services/engraving_paths.py`` so local output sits beside the
existing catalog rather than in a parallel tree, but always under a ``local``
mode folder so it can never collide with provider-generated assets::

    <project>/data/engravings/catalog/<media_type>/<filename_stem>/<label>/<object_id>/local/
        evidence/subject.png          — silhouette composited on near-white
        evidence/mask.png             — binary silhouette mask
        01-repair.png                 — reconstructed object on near-white
        02-engraving-raw.png          — engraving on near-white
        03-engraving-alpha.png        — final cropped RGBA engraving
        <title_stub>-<frame_id>-<object_id>-local.png
        run.json                      — full provenance record

As in the provider pipeline, the model name is deliberately absent from the
path: that belongs inside ``run.json``, not in the folder structure.
"""

from __future__ import annotations

import re
from pathlib import Path

LOCAL_ENGRAVING_SCHEMA_VERSION = "1"
LOCAL_MODE = "local"

# Explicit lifecycle states stored in run.json["status"].
RUN_STATUSES = ("pending", "running", "rejected", "needs_review", "failed")


def _safe_part(value: str) -> str:
    value = str(value or "").strip()
    value = re.sub(r"[^a-zA-Z0-9._(){} -]+", "_", value)
    value = re.sub(r"\s+", " ", value)
    value = re.sub(r"_+", "_", value)
    return value.strip(" ._") or "unknown"


def _safe_label(label: str) -> str:
    return re.sub(r"[^a-z0-9_]", "_", str(label or "").lower().strip()) or "unknown"


def label_folder_for_source(source_json: str | Path, record: dict) -> str:
    """Return the label folder name, preferring the silhouette's own parent dir."""
    parent_name = Path(source_json).parent.name
    if parent_name:
        return parent_name
    return _safe_label(record.get("label", "unknown"))


def output_filename(source_json: str | Path, record: dict) -> str:
    """Return ``<title_stub>-<frame_id>-<object_id>-local.png``."""
    source_json = Path(source_json)
    filename_stem = record.get("filename_stem") or Path(record.get("filename", "unknown")).stem

    title_stub = re.sub(r"[{(][^})]*[})]", "", filename_stem).strip()
    title_stub = re.sub(r"[^a-zA-Z0-9]+", "_", title_stub).strip("_").lower() or "unknown"

    frame = record.get("frame")
    frame_id = f"f{int(frame):06d}" if frame is not None else "f000000"

    return f"{title_stub}-{frame_id}-{source_json.stem}-{LOCAL_MODE}.png"


def engraving_dir(project_path: str | Path, source_json: str | Path, record: dict) -> Path:
    """Return the canonical local-engraving directory for a silhouette object."""
    source_json = Path(source_json)
    media_type = record.get("media_type", "movie")
    filename_stem = record.get("filename_stem") or Path(record.get("filename", "unknown")).stem

    return (
        Path(project_path)
        / "data"
        / "engravings"
        / "catalog"
        / _safe_part(media_type)
        / _safe_part(filename_stem)
        / label_folder_for_source(source_json, record)
        / source_json.stem
        / LOCAL_MODE
    )


def engraving_paths(project_path: str | Path, source_json: str | Path, record: dict) -> dict:
    """Return every canonical path for one local engraving run."""
    base = engraving_dir(project_path, source_json, record)
    return {
        "dir": base,
        "evidence_dir": base / "evidence",
        "evidence_subject": base / "evidence" / "subject.png",
        "evidence_mask": base / "evidence" / "mask.png",
        "repair_png": base / "01-repair.png",
        "raw_engraving_png": base / "02-engraving-raw.png",
        "final_png": base / "03-engraving-alpha.png",
        "named_png": base / output_filename(source_json, record),
        "run_json": base / "run.json",
    }


def is_complete(paths: dict) -> bool:
    """Return True when a finished run already exists at *paths*."""
    return paths["run_json"].is_file() and paths["final_png"].is_file()
