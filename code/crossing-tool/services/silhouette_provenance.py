"""Historical silhouette search-provenance migration helpers.

This module reads the completed semantic audit for a silhouette catalog,
maps each audited archive JSON back to its canonical record, and adds an
additive ``search_provenance`` object without touching PNG assets or
regenerating segmentation results.

The provenance classification is intentionally shallow here: the audit data is
authoritative, and this module only converts the completed audit rows into the
persisted search-provenance shape used by the catalog and browse index.
"""

from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any

from data.annotate import atomic_write_text

SEARCH_PROVENANCE_AUDIT_VERSION = "semantic-v1"
_AUDIT_DIR_NAME = Path("outputs") / "tests" / "silhouette-semantic-audit"
_AUDIT_REPORT_NAME = "report.json"
_AUDIT_PROVENANCE_NAME = "silhouette_provenance.csv"

_VALID_CLASSIFICATIONS = {
    "VALID_SINGLE": ("valid", "single_word_label"),
    "VALID_EXACT": ("valid", "exact_annotation_value"),
}
_QUESTIONABLE_CLASSIFICATIONS = {
    "QUESTIONABLE_SPLIT",
    "QUESTIONABLE_PARTIAL",
    "QUESTIONABLE_UNSUPPORTED",
}


def _project_path(project_path: str | Path) -> Path:
    return Path(project_path)


def default_audit_dir(project_path: str | Path) -> Path:
    """Return the preferred semantic-audit directory for *project_path*.

    The canonical audit directory for this migration is ``outputs/tests``.
    Callers that need to target a legacy audit snapshot should pass ``audit_dir``
    explicitly instead of relying on a singular-path fallback.
    """
    return _project_path(project_path) / _AUDIT_DIR_NAME


def _load_optional_json(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _parse_json_field(raw: str, default: Any) -> Any:
    text = (raw or "").strip()
    if not text:
        return default
    try:
        return json.loads(text)
    except Exception:
        return default


def _resolve_archive_path(project: Path, raw_path: str) -> Path:
    path = Path(raw_path)
    if path.is_absolute():
        return path.resolve()
    return (project / path).resolve()


def _provenance_from_classification(row: dict[str, Any]) -> dict[str, Any] | None:
    classification = str(row.get("classification") or "")
    if classification in _VALID_CLASSIFICATIONS:
        state, reason = _VALID_CLASSIFICATIONS[classification]
    elif classification in _QUESTIONABLE_CLASSIFICATIONS:
        state, reason = "questionable", "multi_word_not_exact_annotation_value"
    else:
        return None

    provenance: dict[str, Any] = {
        "state": state,
        "reason": reason,
        "audit_version": SEARCH_PROVENANCE_AUDIT_VERSION,
        "audit_classification": classification,
    }

    for key in (
        "annotation_values",
        "matched_words",
        "missing_words",
        "support_values",
        "exact_annotation_match",
        "all_words_present",
        "all_words_present_as_one_value",
        "separate_component_values",
    ):
        if key in row:
            provenance[key] = row[key]

    return provenance


def _load_audit_rows(audit_dir: Path) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    provenance_csv = audit_dir / _AUDIT_PROVENANCE_NAME
    if not provenance_csv.exists():
        raise FileNotFoundError(f"Missing provenance audit CSV: {provenance_csv}")

    report_json = _load_optional_json(audit_dir / _AUDIT_REPORT_NAME)
    rows: list[dict[str, Any]] = []

    with provenance_csv.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        for raw_row in reader:
            row = dict(raw_row)
            row["word_count"] = int(row.get("word_count") or 0)
            row["frame"] = int(row.get("frame") or 0)
            row["annotation_values"] = _parse_json_field(row.get("annotation_values", ""), [])
            row["matched_words"] = _parse_json_field(row.get("matched_words", ""), [])
            row["missing_words"] = _parse_json_field(row.get("missing_words", ""), [])
            row["support_values"] = _parse_json_field(row.get("support_values", ""), {})
            row["exact_annotation_match"] = str(row.get("exact_annotation_match", "")).lower() == "true"
            row["all_words_present"] = str(row.get("all_words_present", "")).lower() == "true"
            row["all_words_present_as_one_value"] = str(
                row.get("all_words_present_as_one_value", "")
            ).lower() == "true"
            row["separate_component_values"] = str(
                row.get("separate_component_values", "")
            ).lower() == "true"
            rows.append(row)

    return rows, report_json


def migrate_search_provenance(
    project_path: str | Path,
    *,
    audit_dir: str | Path | None = None,
    media_type: str = "movie",
    dry_run: bool = True,
) -> dict[str, Any]:
    """Add ``search_provenance`` to catalog JSON files from a completed audit.

    The operation is deliberately non-destructive: PNGs are never touched and
    the JSON payload is only augmented with the new provenance object.

    When *dry_run* is True, the function reports what would change and returns
    without writing. When *dry_run* is False, the helper writes JSON updates
    atomically and rebuilds the silhouette browse index for *media_type*.
    """
    from services.illustration_index import rebuild_index
    from services.silhouette_catalog import scan_catalog

    project = _project_path(project_path).resolve()
    audit_root = Path(audit_dir) if audit_dir is not None else default_audit_dir(project)
    rows, report = _load_audit_rows(audit_root)

    catalog_records = [record for record in scan_catalog(str(project), media_type=media_type) if "error" not in record]
    catalog_by_path: dict[Path, dict[str, Any]] = {}
    catalog_errors = 0
    for record in catalog_records:
        path = record.get("path")
        if not path:
            catalog_errors += 1
            continue
        catalog_by_path[_resolve_archive_path(project, str(path))] = record

    audit_by_path: dict[Path, dict[str, Any]] = {}
    duplicate_paths: list[str] = []
    for row in rows:
        archive_path = _resolve_archive_path(project, str(row.get("archive_json_path") or ""))
        if archive_path in audit_by_path:
            duplicate_paths.append(str(archive_path))
            continue
        audit_by_path[archive_path] = row

    audit_paths = set(audit_by_path)
    catalog_paths = set(catalog_by_path)
    matched_paths = sorted(audit_paths & catalog_paths)
    unmatched_audit_paths = sorted(audit_paths - catalog_paths)
    unmatched_catalog_paths = sorted(catalog_paths - audit_paths)

    derived_total = len(rows)
    derived_valid = 0
    derived_questionable = 0
    unresolved_rows: list[str] = []
    provenance_by_path: dict[Path, dict[str, Any]] = {}

    for archive_path in matched_paths:
        provenance = _provenance_from_classification(audit_by_path[archive_path])
        if provenance is None:
            unresolved_rows.append(str(archive_path))
            continue
        provenance_by_path[archive_path] = provenance
        if provenance["state"] == "valid":
            derived_valid += 1
        elif provenance["state"] == "questionable":
            derived_questionable += 1

    report_summary = report.get("classification", {}) if isinstance(report, dict) else {}
    report_total = int(report_summary.get("total_silhouette_records", derived_total) or derived_total)
    report_valid = int(report_summary.get("valid_single", 0) or 0) + int(report_summary.get("valid_exact", 0) or 0)
    report_questionable = (
        int(report_summary.get("questionable_split", 0) or 0)
        + int(report_summary.get("questionable_partial", 0) or 0)
        + int(report_summary.get("questionable_unsupported", 0) or 0)
    )

    errors = catalog_errors + len(duplicate_paths) + len(unresolved_rows)
    if report and report_total != derived_total:
        errors += 1
    if report and (report_valid != derived_valid or report_questionable != derived_questionable):
        errors += 1
    if unmatched_audit_paths or unmatched_catalog_paths:
        errors += len(unmatched_audit_paths) + len(unmatched_catalog_paths)

    result = {
        "project_path": str(project),
        "audit_dir": str(audit_root),
        "media_type": media_type,
        "dry_run": dry_run,
        "safe_to_apply": errors == 0,
        "total_records_examined": len(matched_paths),
        "valid": derived_valid,
        "questionable": derived_questionable,
        "already_classified": 0,
        "updated": 0,
        "unmatched_audit_records": len(unmatched_audit_paths),
        "unmatched_catalog_records": len(unmatched_catalog_paths),
        "duplicate_audit_records": len(duplicate_paths),
        "unresolved_records": len(unresolved_rows),
        "errors": errors,
        "report_total": report_total,
        "report_valid": report_valid,
        "report_questionable": report_questionable,
        "catalog_records": len(catalog_by_path),
        "audit_rows": len(rows),
    }

    if errors or dry_run:
        result["applied"] = False
        return result

    updated = 0
    already_classified = 0
    for archive_path in matched_paths:
        desired = provenance_by_path.get(archive_path)
        if desired is None:
            continue
        meta = json.loads(archive_path.read_text(encoding="utf-8"))
        if meta.get("search_provenance") == desired:
            already_classified += 1
            continue
        meta["search_provenance"] = desired
        atomic_write_text(archive_path, json.dumps(meta, indent=2, ensure_ascii=False))
        updated += 1

    index_result = rebuild_index(project, "silhouettes", media_type)
    result.update({
        "already_classified": already_classified,
        "updated": updated,
        "applied": True,
        "index_result": index_result,
        "reindexed": index_result.get("status") == "ready",
    })
    return result