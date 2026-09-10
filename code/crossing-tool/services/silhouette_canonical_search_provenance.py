"""Persist canonical-search audit results as silhouette search provenance.

This migration consumes an already-completed canonical-search audit.  It does
not rerun canonical search or reinterpret the audit through morphology,
heuristics, or historical provenance.  Every target JSON is updated atomically
only after the audit and selected live catalog exactly agree.
"""

from __future__ import annotations

import csv
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import tempfile
from typing import Any, Iterator

from data.annotate import atomic_write_text
from data.media_id import parse_shot_id
from services.silhouette_catalog import iter_catalog


CANONICAL_SEARCH_PROVENANCE_AUDIT_VERSION = "canonical-search-v1"
CANONICAL_AUDIT_DIR = Path("outputs") / "tests" / "silhouette-canonical-search-audit"
MIGRATION_REPORT_DIR = (
    Path("outputs") / "tests" / "silhouette-canonical-search-provenance-migration"
)
RECORDS_CSV_NAME = "canonical_search_records.csv"
REPORT_JSON_NAME = "report.json"
MIGRATION_REPORT_NAME = "report.json"

_CANONICAL_SEARCH_SERVICE = "services.search.search_shots"
_CLASSIFICATION_TO_STATE = {
    "VALID": "valid",
    "QUESTIONABLE": "questionable",
    "UNVERIFIABLE": "unverifiable",
}
_MAX_REPORTED_ERRORS = 50
_LEGACY_SEARCH_SHOT_ID = re.compile(r"shot_\d{5}\Z")


def default_audit_dir(project_path: str | Path) -> Path:
    """Return the canonical source-audit location for this migration."""
    return Path(project_path) / CANONICAL_AUDIT_DIR


def default_migration_report_dir(project_path: str | Path) -> Path:
    """Return the generated-artifact directory for migration reports."""
    return Path(project_path) / MIGRATION_REPORT_DIR


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _normalise_shot_id(value: Any) -> str | None:
    """Match the legacy shot-ID display form used by canonical search."""
    raw = _text(value)
    if not raw:
        return None
    if _LEGACY_SEARCH_SHOT_ID.fullmatch(raw):
        return raw
    try:
        parse_shot_id(raw)
    except ValueError:
        try:
            return f"shot_{int(raw):05d}"
        except (TypeError, ValueError):
            return None
    return raw


def _parse_json(raw: str, *, field: str, row_number: int, expected: type) -> Any:
    try:
        value = json.loads(raw)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ValueError(f"row {row_number}: {field} is not valid JSON") from exc
    if not isinstance(value, expected):
        raise ValueError(f"row {row_number}: {field} must be a {expected.__name__}")
    return value


def _parse_bool(raw: str, *, field: str, row_number: int) -> bool | None:
    value = _text(raw).casefold()
    if not value:
        return None
    if value == "true":
        return True
    if value == "false":
        return False
    raise ValueError(f"row {row_number}: {field} must be true, false, or empty")


def _parse_nonnegative_int(raw: str, *, field: str, row_number: int) -> int | None:
    value = _text(raw)
    if not value:
        return None
    try:
        result = int(value)
    except ValueError as exc:
        raise ValueError(f"row {row_number}: {field} must be an integer") from exc
    if result < 0:
        raise ValueError(f"row {row_number}: {field} must not be negative")
    return result


def _within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _resolve_catalog_path(project: Path, raw_path: str, *, row_number: int) -> Path:
    raw = _text(raw_path)
    if not raw:
        raise ValueError(f"row {row_number}: historical_json_path is missing")
    path = Path(raw)
    resolved = path.resolve() if path.is_absolute() else (project / path).resolve()
    catalog_root = (project / "data" / "silhouettes" / "catalog").resolve()
    if resolved.suffix != ".json" or not _within(resolved, catalog_root):
        raise ValueError(
            f"row {row_number}: historical_json_path is outside the live silhouette catalog"
        )
    return resolved


def _require_fields(reader: csv.DictReader) -> None:
    required = {
        "media_type",
        "media_id",
        "filename",
        "shot_id",
        "canonical_source_shot_id",
        "source_shot_identity",
        "field",
        "historical_label",
        "historical_search_label",
        "historical_json_path",
        "original_annotation_values",
        "canonical_search_match",
        "source_shot_returned",
        "canonical_search_result_count",
        "canonical_matching_shot_ids",
        "canonical_matching_shot_refs",
        "canonical_matching_shot_ids_truncated",
        "classification",
        "classification_reason",
    }
    present = set(reader.fieldnames or [])
    missing = sorted(required - present)
    if missing:
        raise ValueError("canonical audit CSV is missing columns: " + ", ".join(missing))


def _parse_audit_row(project: Path, raw_row: dict[str, str], row_number: int) -> dict[str, Any]:
    """Parse one completed-audit row without deriving a new verdict."""
    classification = _text(raw_row.get("classification"))
    state = _CLASSIFICATION_TO_STATE.get(classification)
    if state is None:
        raise ValueError(f"row {row_number}: unsupported classification {classification!r}")

    media_type = _text(raw_row.get("media_type"))
    if media_type not in {"movie", "gameplay"}:
        raise ValueError(f"row {row_number}: unsupported media_type {media_type!r}")
    raw_source_identity = _parse_json(
        raw_row.get("source_shot_identity", ""),
        field="source_shot_identity",
        row_number=row_number,
        expected=dict,
    )
    source_identity = {
        "media_type": _text(raw_source_identity.get("media_type")),
        "filename": _text(raw_source_identity.get("filename")),
        "shot_id": raw_source_identity.get("shot_id"),
    }
    if source_identity["shot_id"] is not None and not isinstance(source_identity["shot_id"], str):
        raise ValueError(f"row {row_number}: source_shot_identity.shot_id must be a string or null")
    if isinstance(source_identity["shot_id"], str):
        source_identity["shot_id"] = source_identity["shot_id"].strip() or None
    row = {
        "path": _resolve_catalog_path(
            project, raw_row.get("historical_json_path", ""), row_number=row_number
        ),
        "media_type": media_type,
        "media_id": _text(raw_row.get("media_id")),
        "filename": _text(raw_row.get("filename")),
        "shot_id": _text(raw_row.get("shot_id")),
        "canonical_source_shot_id": _text(raw_row.get("canonical_source_shot_id")),
        "source_shot_identity": source_identity,
        "field": _text(raw_row.get("field")),
        "historical_label": _text(raw_row.get("historical_label")),
        "historical_search_label": _text(raw_row.get("historical_search_label")),
        "original_annotation_values": _parse_json(
            raw_row.get("original_annotation_values", ""),
            field="original_annotation_values",
            row_number=row_number,
            expected=list,
        ),
        "canonical_search_match": _parse_bool(
            raw_row.get("canonical_search_match", ""),
            field="canonical_search_match",
            row_number=row_number,
        ),
        "source_shot_returned": _parse_bool(
            raw_row.get("source_shot_returned", ""),
            field="source_shot_returned",
            row_number=row_number,
        ),
        "canonical_search_result_count": _parse_nonnegative_int(
            raw_row.get("canonical_search_result_count", ""),
            field="canonical_search_result_count",
            row_number=row_number,
        ),
        "canonical_matching_shot_ids": _parse_json(
            raw_row.get("canonical_matching_shot_ids", ""),
            field="canonical_matching_shot_ids",
            row_number=row_number,
            expected=list,
        ),
        "canonical_matching_shot_refs": _parse_json(
            raw_row.get("canonical_matching_shot_refs", ""),
            field="canonical_matching_shot_refs",
            row_number=row_number,
            expected=list,
        ),
        "canonical_matching_shot_ids_truncated": _parse_bool(
            raw_row.get("canonical_matching_shot_ids_truncated", ""),
            field="canonical_matching_shot_ids_truncated",
            row_number=row_number,
        ),
        "classification": classification,
        "classification_reason": _text(raw_row.get("classification_reason")),
    }
    if row["historical_label"] != row["historical_search_label"]:
        raise ValueError(f"row {row_number}: historical label disagrees with historical search label")
    expected_source = {
        "media_type": row["media_type"],
        "filename": row["filename"],
        "shot_id": row["canonical_source_shot_id"] or None,
    }
    if source_identity != expected_source:
        raise ValueError(f"row {row_number}: source_shot_identity disagrees with audit columns")

    expected_match = {"VALID": True, "QUESTIONABLE": False, "UNVERIFIABLE": None}[classification]
    if (
        row["canonical_search_match"] is not expected_match
        or row["source_shot_returned"] is not expected_match
    ):
        raise ValueError(f"row {row_number}: classification disagrees with canonical search result")
    if classification == "UNVERIFIABLE":
        if row["canonical_search_result_count"] is not None:
            raise ValueError(f"row {row_number}: unverifiable result count must be empty")
    else:
        if row["canonical_search_result_count"] is None:
            raise ValueError(f"row {row_number}: searchable result count is missing")
        if not all((row["filename"], row["canonical_source_shot_id"], row["field"], row["historical_search_label"])):
            raise ValueError(f"row {row_number}: searchable row is missing source identity or request")
    return row


def _desired_provenance(row: dict[str, Any], audit_report: dict[str, Any]) -> dict[str, Any]:
    """Copy canonical audit evidence into the authoritative production shape."""
    return {
        "state": _CLASSIFICATION_TO_STATE[row["classification"]],
        "method": "canonical_search",
        "audit_version": CANONICAL_SEARCH_PROVENANCE_AUDIT_VERSION,
        "audit_generated_at": audit_report.get("generated_at"),
        "audit_classification": row["classification"],
        "reason": row["classification_reason"],
        "historical_search_label": row["historical_search_label"],
        "field": row["field"],
        "source_shot_identity": row["source_shot_identity"],
        "annotation_values": row["original_annotation_values"],
        "canonical_search_match": row["canonical_search_match"],
        "source_shot_returned": row["source_shot_returned"],
        "canonical_search_result_count": row["canonical_search_result_count"],
        "canonical_matching_shot_ids": row["canonical_matching_shot_ids"],
        "canonical_matching_shot_refs": row["canonical_matching_shot_refs"],
        "canonical_matching_shot_ids_truncated": row[
            "canonical_matching_shot_ids_truncated"
        ],
    }


def _record_identity_error(record: dict[str, Any], row: dict[str, Any]) -> str | None:
    expected = {
        "media_type": row["media_type"],
        "media_id": row["media_id"],
        "filename": row["filename"],
        "field": row["field"],
        "label": row["historical_search_label"],
    }
    for key, value in expected.items():
        if _text(record.get(key)) != value:
            return f"{key} differs from the completed audit"
    canonical_shot_id = _normalise_shot_id(record.get("shot_id"))
    if canonical_shot_id != (row["canonical_source_shot_id"] or None):
        return "canonical source shot ID differs from the completed audit"
    return None


def _load_live_record(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("catalog JSON must contain an object")
    return value


def _audit_fingerprint(path: Path) -> dict[str, Any]:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    stat = path.stat()
    return {
        "path": str(path),
        "sha256": digest.hexdigest(),
        "size_bytes": stat.st_size,
        "modified_ns": stat.st_mtime_ns,
    }


def _validate_audit_report(
    report: dict[str, Any],
    project: Path,
    audit_root: Path,
    media_types: tuple[str, ...],
) -> list[str]:
    errors: list[str] = []
    try:
        reported_project = Path(str(report.get("project_path") or "")).resolve()
        reported_output = Path(str(report.get("output_dir") or "")).resolve()
    except (OSError, ValueError):
        errors.append("source audit has an invalid project or output directory")
    else:
        if reported_project != project:
            errors.append("source audit belongs to a different project")
        if reported_output != audit_root:
            errors.append("source audit output directory does not match the selected audit directory")
    if report.get("probe") is not False:
        errors.append("source audit is a probe, not a completed full audit")
    canonical = report.get("canonical_search")
    if not isinstance(canonical, dict) or canonical.get("service") != _CANONICAL_SEARCH_SERVICE:
        errors.append("source audit did not record the canonical search service")
    if isinstance(canonical, dict) and int(canonical.get("failed_queries", 0) or 0) != 0:
        errors.append("source audit contains failed canonical-search queries")
    reported_media_types = report.get("media_types")
    if not isinstance(reported_media_types, list) or not set(media_types).issubset(reported_media_types):
        errors.append("source audit does not cover every selected media type")
    summary = report.get("summary")
    if not isinstance(summary, dict):
        errors.append("source audit is missing its summary")
    else:
        for key in ("total_records", "valid", "questionable", "unverifiable"):
            if not isinstance(summary.get(key), int):
                errors.append(f"source audit summary is missing integer {key}")
    return errors


def _add_error(result: dict[str, Any], message: str) -> None:
    result["errors"] += 1
    examples = result["error_examples"]
    if len(examples) < _MAX_REPORTED_ERRORS:
        examples.append(message)


def _iter_rows(project: Path, csv_path: Path) -> Iterator[tuple[int, dict[str, Any]]]:
    try:
        with csv_path.open("r", encoding="utf-8", newline="") as handle:
            reader = csv.DictReader(handle)
            _require_fields(reader)
            for row_number, raw_row in enumerate(reader, 2):
                yield row_number, _parse_audit_row(project, dict(raw_row), row_number)
    except (csv.Error, OSError, UnicodeError, ValueError) as exc:
        yield 0, {"audit_error": str(exc)}


def _json_safe(value: Any) -> Any:
    """Convert report values to JSON-compatible data without hiding unknown types."""
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise TypeError(f"Migration report contains unsupported value: {type(value).__name__}")


def _write_migration_report(project: Path, result: dict[str, Any]) -> Path:
    report_dir = default_migration_report_dir(project)
    report_dir.mkdir(parents=True, exist_ok=True)
    report_path = report_dir / MIGRATION_REPORT_NAME
    atomic_write_text(
        report_path,
        json.dumps(_json_safe(result), indent=2, ensure_ascii=False) + "\n",
    )
    return report_path


def migrate_canonical_search_provenance(
    project_path: str | Path,
    *,
    audit_dir: str | Path | None = None,
    media_type: str = "both",
    dry_run: bool = True,
) -> dict[str, Any]:
    """Transfer the completed canonical-search audit into catalog JSON records.

    The source CSV is streamed twice only after a full preflight.  The first
    pass verifies source-audit integrity and exact live archive coverage; the
    second pass writes only the required provenance object with
    ``atomic_write_text``.  No search service or previous provenance verdict is
    used to determine a state.
    """
    if media_type not in {"movie", "gameplay", "both"}:
        raise ValueError("media_type must be 'movie', 'gameplay', or 'both'")

    project = Path(project_path).resolve()
    media_types = ("movie", "gameplay") if media_type == "both" else (media_type,)
    audit_candidate = Path(audit_dir) if audit_dir is not None else default_audit_dir(project)
    audit_root = (
        audit_candidate.resolve()
        if audit_candidate.is_absolute()
        else (project / audit_candidate).resolve()
    )
    report_path = audit_root / REPORT_JSON_NAME
    csv_path = audit_root / RECORDS_CSV_NAME
    if not report_path.is_file():
        raise FileNotFoundError(f"Missing canonical audit report: {report_path}")
    if not csv_path.is_file():
        raise FileNotFoundError(f"Missing canonical audit records CSV: {csv_path}")
    try:
        audit_report = json.loads(report_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"Canonical audit report is not valid JSON: {report_path}") from exc
    if not isinstance(audit_report, dict):
        raise ValueError(f"Canonical audit report must contain an object: {report_path}")

    source_fingerprint = _audit_fingerprint(csv_path)
    result: dict[str, Any] = {
        "migration": "silhouette-canonical-search-provenance-v1",
        "generated_at": _now(),
        "project_path": str(project),
        "audit_dir": str(audit_root),
        "audit_report": str(report_path),
        "audit_records_csv": source_fingerprint,
        "media_type": media_type,
        "dry_run": dry_run,
        "safe_to_apply": False,
        "applied": False,
        "audit_records_complete": False,
        "total_audit_rows": 0,
        "selected_audit_records": 0,
        "selected_catalog_records": 0,
        "valid": 0,
        "questionable": 0,
        "unverifiable": 0,
        "would_update": 0,
        "already_authoritative": 0,
        "duplicate_audit_records": 0,
        "unreadable_catalog_records": 0,
        "identity_mismatches": 0,
        "unmatched_audit_records": 0,
        "unmatched_catalog_records": 0,
        "errors": 0,
        "error_examples": [],
    }
    for message in _validate_audit_report(audit_report, project, audit_root, media_types):
        _add_error(result, message)

    report_counts = {key: 0 for key in _CLASSIFICATION_TO_STATE}
    with tempfile.TemporaryDirectory(prefix="crossing-canonical-provenance-") as temp_dir:
        registry_path = Path(temp_dir) / "audit-paths.sqlite3"
        with sqlite3.connect(registry_path) as registry:
            registry.execute("PRAGMA synchronous=OFF")
            registry.execute("CREATE TABLE paths (path TEXT PRIMARY KEY)")
            for row_number, row in _iter_rows(project, csv_path):
                if "audit_error" in row:
                    _add_error(result, f"cannot read canonical audit records: {row['audit_error']}")
                    break
                result["total_audit_rows"] += 1
                report_counts[row["classification"]] += 1
                if row["media_type"] not in media_types:
                    continue
                result["selected_audit_records"] += 1
                result[_CLASSIFICATION_TO_STATE[row["classification"]]] += 1
                try:
                    registry.execute("INSERT INTO paths (path) VALUES (?)", (str(row["path"]),))
                except sqlite3.IntegrityError:
                    result["duplicate_audit_records"] += 1
                    _add_error(result, f"row {row_number}: duplicate historical_json_path")
                    continue
                if not row["path"].is_file():
                    result["unmatched_audit_records"] += 1
                    _add_error(result, f"row {row_number}: audited catalog JSON is missing")
                    continue
                try:
                    record = _load_live_record(row["path"])
                except (OSError, ValueError, json.JSONDecodeError) as exc:
                    result["unreadable_catalog_records"] += 1
                    _add_error(result, f"row {row_number}: cannot read audited catalog JSON ({exc})")
                    continue
                identity_error = _record_identity_error(record, row)
                if identity_error:
                    result["identity_mismatches"] += 1
                    _add_error(result, f"row {row_number}: {identity_error}")
                    continue
                desired = _desired_provenance(row, audit_report)
                if record.get("search_provenance") == desired:
                    result["already_authoritative"] += 1
                else:
                    result["would_update"] += 1
            else:
                result["audit_records_complete"] = True

            summary = audit_report.get("summary") if isinstance(audit_report.get("summary"), dict) else {}
            expected_counts = {
                "VALID": summary.get("valid"),
                "QUESTIONABLE": summary.get("questionable"),
                "UNVERIFIABLE": summary.get("unverifiable"),
            }
            if (
                summary.get("total_records") != result["total_audit_rows"]
                or any(expected_counts[key] != report_counts[key] for key in report_counts)
            ):
                _add_error(result, "source audit report summary does not match its records CSV")

            for current_media_type in media_types:
                for record in iter_catalog(str(project), media_type=current_media_type):
                    result["selected_catalog_records"] += 1
                    record_path = Path(record.get("path") or "").resolve()
                    if record.get("error"):
                        result["unreadable_catalog_records"] += 1
                        _add_error(result, f"live catalog JSON is unreadable: {record_path}")
                        continue
                    seen = registry.execute(
                        "SELECT 1 FROM paths WHERE path = ?", (str(record_path),)
                    ).fetchone()
                    if seen is None:
                        result["unmatched_catalog_records"] += 1
                        _add_error(result, f"live catalog JSON is absent from the completed audit: {record_path}")

    if result["selected_catalog_records"] != result["selected_audit_records"]:
        _add_error(result, "selected audit and live catalog record counts differ")
    result["safe_to_apply"] = result["errors"] == 0
    result["report_path"] = str(_write_migration_report(project, result))
    if dry_run or not result["safe_to_apply"]:
        return result

    if _audit_fingerprint(csv_path) != source_fingerprint:
        _add_error(result, "source audit CSV changed during migration preflight")
        result["safe_to_apply"] = False
        result["report_path"] = str(_write_migration_report(project, result))
        return result

    updated = 0
    already_authoritative = 0
    write_error = None
    try:
        for row_number, row in _iter_rows(project, csv_path):
            if row["media_type"] not in media_types:
                continue
            record = _load_live_record(row["path"])
            identity_error = _record_identity_error(record, row)
            if identity_error:
                raise RuntimeError(f"row {row_number}: {identity_error} after preflight")
            desired = _desired_provenance(row, audit_report)
            if record.get("search_provenance") == desired:
                already_authoritative += 1
                continue
            record["search_provenance"] = desired
            atomic_write_text(row["path"], json.dumps(record, indent=2, ensure_ascii=False))
            updated += 1
    except (OSError, ValueError, json.JSONDecodeError, RuntimeError) as exc:
        write_error = str(exc)

    result["updated"] = updated
    result["already_authoritative"] = already_authoritative
    if write_error:
        _add_error(result, f"migration stopped after {updated} atomic writes: {write_error}")
        result["safe_to_apply"] = False
        result["partial_apply"] = updated > 0
        result["report_path"] = str(_write_migration_report(project, result))
        return result

    from services.illustration_index import rebuild_index

    index_results = {
        current_media_type: rebuild_index(project, "silhouettes", current_media_type)
        for current_media_type in media_types
    }
    result.update({
        "applied": True,
        "index_results": index_results,
        "reindexed": all(item.get("status") == "ready" for item in index_results.values()),
    })
    result["report_path"] = str(_write_migration_report(project, result))
    return result