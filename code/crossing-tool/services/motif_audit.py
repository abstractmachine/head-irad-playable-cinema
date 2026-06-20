"""Read-only audit of canonical shot.motif coverage in annotation JSON files."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def _is_present(motif: Any) -> bool:
    """Return True only for a non-empty string motif (canonical form)."""
    return isinstance(motif, str) and bool(motif.strip())


def audit_motifs_for_file(
    project_path: str,
    filename: str,
    media_type: str,
    *,
    max_missing_ids: int = 10,
) -> dict[str, Any]:
    """Return motif coverage for a single annotation JSON file.

    Returns a dict with keys:
        filename, total, present, missing, missing_shot_ids
    """
    from data.annotate import get_annotation_json_path

    json_path = get_annotation_json_path(project_path, filename, media_type)
    if not json_path.exists():
        raise FileNotFoundError(f"Annotation JSON not found: {json_path}")

    try:
        entries = json.loads(json_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise OSError(f"Failed to read {json_path}: {exc}") from exc

    total = 0
    present = 0
    missing_shot_ids: list[str] = []

    for entry in entries:
        if not isinstance(entry, dict):
            continue
        shot = entry.get("shot")
        if not isinstance(shot, dict):
            continue
        total += 1
        motif = shot.get("motif")
        if _is_present(motif):
            present += 1
        elif len(missing_shot_ids) < max_missing_ids:
            sid = shot.get("shot_id", "")
            if sid:
                missing_shot_ids.append(sid)

    return {
        "filename": filename,
        "total": total,
        "present": present,
        "missing": total - present,
        "missing_shot_ids": missing_shot_ids,
    }


def audit_motifs_for_all(
    project_path: str,
    media_type: str,
    *,
    max_missing_ids: int = 10,
) -> dict[str, Any]:
    """Return corpus-wide motif coverage for a media type.

    Returns a dict with keys:
        media_type, files_checked, shots_checked,
        motifs_present, motifs_missing, coverage, files
    where ``files`` is a list of per-file dicts from audit_motifs_for_file,
    sorted by missing count descending.
    """
    base = Path(project_path) / "data" / "annotations" / "shots" / media_type
    if not base.exists():
        return {
            "media_type": media_type,
            "files_checked": 0,
            "shots_checked": 0,
            "motifs_present": 0,
            "motifs_missing": 0,
            "coverage": 1.0,
            "files": [],
        }

    file_results: list[dict[str, Any]] = []
    for json_path in sorted(base.glob("*.json")):
        if json_path.name.endswith(".manifest.json"):
            continue
        try:
            entries = json.loads(json_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue

        total = 0
        present = 0
        missing_shot_ids: list[str] = []

        for entry in entries:
            if not isinstance(entry, dict):
                continue
            shot = entry.get("shot")
            if not isinstance(shot, dict):
                continue
            total += 1
            motif = shot.get("motif")
            if _is_present(motif):
                present += 1
            elif len(missing_shot_ids) < max_missing_ids:
                sid = shot.get("shot_id", "")
                if sid:
                    missing_shot_ids.append(sid)

        file_stem = json_path.stem
        # Strip canonical .annotations suffix so the filename matches the video stem
        if file_stem.endswith(".annotations"):
            file_stem = file_stem[: -len(".annotations")]
        file_results.append({
            "filename": file_stem,   # stem matches the display convention
            "total": total,
            "present": present,
            "missing": total - present,
            "missing_shot_ids": missing_shot_ids,
        })

    file_results.sort(key=lambda r: (-r["missing"], r["filename"]))

    shots_checked = sum(r["total"] for r in file_results)
    motifs_present = sum(r["present"] for r in file_results)
    motifs_missing = sum(r["missing"] for r in file_results)
    coverage = motifs_present / shots_checked if shots_checked else 1.0

    return {
        "media_type": media_type,
        "files_checked": len(file_results),
        "shots_checked": shots_checked,
        "motifs_present": motifs_present,
        "motifs_missing": motifs_missing,
        "coverage": coverage,
        "files": file_results,
    }
