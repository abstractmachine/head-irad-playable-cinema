"""Bounded automated rechecks for canonically-questionable silhouettes.

This service reruns the existing per-shot catalog extractor using only current
atomic values from the source annotation. Historical catalog labels are kept
as evidence in the old object's extraction identity and are never re-used as
new semantic queries.
"""

from __future__ import annotations

import csv
import io
import json
import os
import sqlite3
import subprocess
import tempfile
import time
from contextlib import nullcontext
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, NamedTuple

from services.silhouette_catalog import (
    ASSIGNMENT_ACTIVE,
    ASSIGNMENT_FIELD,
    ASSIGNMENT_INACTIVE,
    ASSIGNMENT_SUPERSEDED,
    RECHECK_COMPLETED,
    RECHECK_ERROR,
    RECHECK_FIELD,
    RECHECK_PENDING,
    assignment_is_active,
    assignment_state_for_record,
    catalog_object_reference,
    exclusive_catalog_extraction_lock,
    extract_objects_for_shot,
    iter_catalog,
)
from services.silhouette_curation import (
    complete_recheck_source,
    mark_recheck_error,
    queue_recheck_pending,
    SourceJobCommitInterrupted,
    stage_recheck_replacements,
    staged_recheck_replacement_matches,
)

_MEDIA_TYPES = ("movie", "gameplay")
_ZERO_RESULT_PREFIXES = (
    "SAM3 produced no masks",
    "all ",
    "no masks after CLIP ranking",
    "no candidates after re-ranking",
    "no masks above confidence floor",
    "PNG extraction failed",
)
_PROGRESS_EVERY_JOBS = 25
_ETA_MIN_JOBS = 25


class RecheckInputError(ValueError):
    """A source annotation cannot safely reconstruct an extraction request."""


class RecheckInfrastructureError(RuntimeError):
    """An archive-wide condition that requires the current run to stop."""


class RecheckLogError(RuntimeError):
    """The requested durable operational log can no longer be written."""


class _RecheckLogger:
    """Append-only UTF-8 operational logger, flushed for long-running monitoring."""

    def __init__(self, log_path: str | Path | None) -> None:
        self.path: Path | None = None
        self._handle = None
        if log_path is None:
            return
        self.path = Path(log_path).expanduser()
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._handle = self.path.open("a", encoding="utf-8")
        except OSError as exc:
            raise ValueError(f"Could not open recheck log '{self.path}': {exc}") from exc

    def write(self, message: str) -> None:
        if self._handle is None:
            return
        try:
            self._handle.write(f"{_now()} {message}\n")
            self._handle.flush()
            os.fsync(self._handle.fileno())
        except OSError as exc:
            raise RecheckLogError(f"Could not write recheck log '{self.path}': {exc}") from exc

    def close(self) -> None:
        if self._handle is None:
            return
        try:
            self._handle.close()
        finally:
            self._handle = None


class RecheckSourceKey(NamedTuple):
    """Canonical extraction-unit identity for one source recheck job."""

    media_type: str
    media_id: str
    shot_id: str
    field: str

    @classmethod
    def from_record(cls, record: dict[str, Any]) -> "RecheckSourceKey":
        return cls(
            str(record.get("media_type") or ""),
            str(record.get("media_id") or ""),
            str(record.get("shot_id") or ""),
            str(record.get("field") or ""),
        )


class _RecheckModels(NamedTuple):
    """Run-scoped production model instances shared by all fresh extractions."""

    clip_model: Any
    clip_processor: Any
    clip_device: Any
    mask_generator: Any
    sam_model_name: str
    sam_device: Any


def _load_recheck_models(
    project_path: str,
    sam_model_name: str,
    frame_model_name: str,
) -> _RecheckModels:
    """Load the production CLIP and SAM3 models once for one recheck run."""
    from services.frame_match import _load_clip_model
    from services.silhouette import load_sam_model

    clip_model, clip_processor, clip_device = _load_clip_model(
        project_path, frame_model_name,
    )
    mask_generator, effective_sam_model_name, sam_device = load_sam_model(
        project_path, sam_model_name,
    )
    return _RecheckModels(
        clip_model,
        clip_processor,
        clip_device,
        mask_generator,
        effective_sam_model_name,
        sam_device,
    )


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _emit(logger: _RecheckLogger, verbose: bool, message: str) -> None:
    """Send an operational line to its requested durable/terminal destinations."""
    logger.write(message)
    if verbose:
        print(message, flush=True)


def _progress_line(report: dict[str, Any], elapsed_seconds: float) -> str:
    examined = int(report["source_jobs_examined"])
    available = int(report["source_jobs_available"])
    rate = examined * 60 / elapsed_seconds if elapsed_seconds > 0 else 0.0
    eta = ""
    if examined >= _ETA_MIN_JOBS and rate > 0 and available >= examined:
        eta_minutes = max(0.0, (available - examined) / rate)
        eta = f" eta={eta_minutes:.1f}m"
    return (
        f"progress source_jobs={examined}/{available} "
        f"historical_superseded={report['historical_records_superseded']} "
        f"produced={report['produced_objects']} "
        f"zero={report['source_jobs_zero_result']} "
        f"failed={report['source_jobs_failed']} "
        f"elapsed={elapsed_seconds:.1f}s rate={rate:.2f} jobs/min{eta}"
    )


def _uncoordinated_extraction_process_running() -> bool:
    """Best-effort detection for extract jobs started before lock coordination."""
    if os.name != "posix":
        return False
    try:
        result = subprocess.run(
            ["ps", "-eo", "pid=,args="],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return False
    for line in result.stdout.splitlines():
        fields = line.strip().split(None, 1)
        if len(fields) != 2:
            continue
        try:
            pid = int(fields[0])
        except ValueError:
            continue
        command = fields[1]
        if pid != os.getpid() and (
            "cli.py index silhouette extract" in command
            or "crossing index silhouette extract" in command
        ):
            return True
    return False


def _source_annotation_values(project_path: Path, record: dict[str, Any]) -> list[str]:
    """Read ordered atomic values from the current source-shot field."""
    from data.annotate import get_annotation_json_path

    filename = str(record.get("filename") or "")
    media_type = str(record.get("media_type") or "")
    shot_id = str(record.get("shot_id") or "")
    field = str(record.get("field") or "")
    if not all((filename, media_type, shot_id, field)):
        raise RecheckInputError("catalog record lacks source filename, media, shot, or field")
    annotation_path = get_annotation_json_path(str(project_path), filename, media_type)
    try:
        entries = json.loads(annotation_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RecheckInputError(f"source annotation is unavailable: {annotation_path}") from exc
    if not isinstance(entries, list):
        raise RecheckInputError(f"source annotation is malformed: {annotation_path}")

    annotation: dict[str, Any] | None = None
    for entry in entries:
        shot = entry.get("shot") if isinstance(entry, dict) else None
        if isinstance(shot, dict) and str(shot.get("shot_id") or "") == shot_id:
            candidate = shot.get("annotation")
            annotation = candidate if isinstance(candidate, dict) else None
            break
    if annotation is None:
        raise RecheckInputError(f"source shot is not annotated: {shot_id}")
    if field not in annotation:
        raise RecheckInputError(f"source shot has no '{field}' annotation")
    raw_values = annotation[field]
    values = raw_values if isinstance(raw_values, list) else [raw_values]
    result: list[str] = []
    for value in values:
        if not isinstance(value, str) or not value.strip():
            continue
        value = value.strip()
        if value not in result:
            result.append(value)
    if not result:
        raise RecheckInputError(f"source shot has no usable '{field}' annotation values")
    return result


def _pending_recheck(record: dict[str, Any]) -> dict[str, Any] | None:
    assignment = record.get(ASSIGNMENT_FIELD)
    if not isinstance(assignment, dict):
        return None
    recheck = assignment.get(RECHECK_FIELD)
    return recheck if isinstance(recheck, dict) else None


def _candidate_kind(record: dict[str, Any], *, retry_errors: bool) -> str | None:
    assignment_state = assignment_state_for_record(record)
    recheck = _pending_recheck(record)
    if assignment_state == ASSIGNMENT_INACTIVE and recheck:
        if recheck.get("state") == RECHECK_PENDING:
            identity = {
                "media_type": str(record.get("media_type") or ""),
                "media_id": str(record.get("media_id") or ""),
                "shot_id": str(record.get("shot_id") or ""),
                "field": str(record.get("field") or ""),
                "search_label": str(record.get("label") or ""),
            }
            if all(identity.values()) and all(
                str(recheck.get(key) or "") == value for key, value in identity.items()
            ):
                return "pending"
            return "malformed_pending"
        if retry_errors and recheck.get("state") == RECHECK_ERROR:
            return "retry_error"
    provenance = record.get("search_provenance")
    if (
        assignment_state == ASSIGNMENT_ACTIVE
        and isinstance(provenance, dict)
        and provenance.get("state") == "questionable"
    ):
        return "questionable"
    return None


def iter_recheck_candidates(
    project_path: str | Path,
    *,
    media_type: str | None = None,
    field: str | None = None,
    retry_errors: bool = False,
) -> Iterator[tuple[str, dict[str, Any]]]:
    """Yield queued and active-questionable records in deterministic catalog order."""
    media_types = (media_type,) if media_type else _MEDIA_TYPES

    # Resume existing requests before growing the queue with new questionable
    # historical records, while retaining deterministic catalog traversal.
    for pending_only in (True, False):
        for current_media_type in media_types:
            for record in iter_catalog(str(project_path), media_type=current_media_type):
                if record.get("error") or (field is not None and record.get("field") != field):
                    continue
                kind = _candidate_kind(record, retry_errors=retry_errors)
                is_pending = kind in {"pending", "malformed_pending", "retry_error"}
                if kind is not None and is_pending == pending_only:
                    yield kind, record


def _candidate_priority(kind: str) -> int:
    return 0 if kind in {"pending", "malformed_pending", "retry_error"} else 1


def _create_source_job_store(connection: sqlite3.Connection) -> None:
    connection.executescript("""
        CREATE TABLE candidates (
            media_type TEXT NOT NULL,
            media_id TEXT NOT NULL,
            shot_id TEXT NOT NULL,
            field TEXT NOT NULL,
            filename TEXT NOT NULL,
            filename_stem TEXT NOT NULL,
            json_path TEXT PRIMARY KEY,
            queue_kind TEXT NOT NULL,
            queue_priority INTEGER NOT NULL
        );
        CREATE INDEX candidates_source_job ON candidates (
            queue_priority, media_type, media_id, shot_id, field, filename_stem
        );
    """)


def _populate_source_job_store(
    connection: sqlite3.Connection,
    project_path: Path,
    *,
    media_type: str | None,
    field: str | None,
    retry_errors: bool,
) -> None:
    rows = []
    for kind, record in iter_recheck_candidates(
        project_path,
        media_type=media_type,
        field=field,
        retry_errors=retry_errors,
    ):
        key = RecheckSourceKey.from_record(record)
        rows.append((
            *key,
            str(record.get("filename") or ""),
            str(record.get("filename_stem") or ""),
            str(record["path"]),
            kind,
            _candidate_priority(kind),
        ))
        if len(rows) >= 1_000:
            connection.executemany(
                "INSERT OR REPLACE INTO candidates VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                rows,
            )
            rows.clear()
    if rows:
        connection.executemany(
            "INSERT OR REPLACE INTO candidates VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            rows,
        )
    connection.commit()


def _source_job_totals(connection: sqlite3.Connection) -> tuple[int, int]:
    historical_records = int(connection.execute("SELECT COUNT(*) FROM candidates").fetchone()[0])
    source_jobs = int(connection.execute("""
        SELECT COUNT(*) FROM (
            SELECT 1 FROM candidates
            GROUP BY media_type, media_id, shot_id, field
        )
    """).fetchone()[0])
    return historical_records, source_jobs


def _iter_source_jobs(
    connection: sqlite3.Connection,
    *,
    limit_sources: int | None,
    limit_records: int | None,
) -> Iterator[tuple[RecheckSourceKey, int]]:
    rows = connection.execute("""
        SELECT media_type, media_id, shot_id, field, COUNT(*) AS historical_records
        FROM candidates
        GROUP BY media_type, media_id, shot_id, field
        ORDER BY MIN(queue_priority), media_type, MIN(filename_stem), shot_id, field
    """)
    selected_sources = 0
    selected_records = 0
    for row in rows:
        if limit_sources is not None and selected_sources >= limit_sources:
            break
        historical_records = int(row[4])
        # Never split a source job to satisfy a record cap. The first job can
        # therefore be larger than the requested cap and is left untouched.
        if limit_records is not None and selected_records + historical_records > limit_records:
            break
        yield RecheckSourceKey(*row[:4]), historical_records
        selected_sources += 1
        selected_records += historical_records


def _job_records(
    connection: sqlite3.Connection,
    source_key: RecheckSourceKey,
) -> list[tuple[str, dict[str, Any]]]:
    rows = connection.execute("""
        SELECT queue_kind, json_path FROM candidates
        WHERE media_type = ? AND media_id = ? AND shot_id = ? AND field = ?
        ORDER BY json_path
    """, source_key).fetchall()
    records: list[tuple[str, dict[str, Any]]] = []
    for queue_kind, raw_path in rows:
        path = Path(raw_path)
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RecheckInputError(f"historical catalog JSON is unreadable: {path}") from exc
        if not isinstance(record, dict):
            raise RecheckInputError(f"historical catalog JSON is malformed: {path}")
        record["path"] = path
        if RecheckSourceKey.from_record(record) != source_key:
            raise RecheckInputError("historical catalog record changed during source-job preparation")
        records.append((str(queue_kind), record))
    return records


def _atomic_values(values: Any) -> tuple[str, ...]:
    if not isinstance(values, list):
        return ()
    result: list[str] = []
    for value in values:
        if isinstance(value, str) and value.strip() and value.strip() not in result:
            result.append(value.strip())
    return tuple(result)


def _completed_sources_for_media_item(
    project_path: Path,
    *,
    media_type: str,
    filename_stem: str,
) -> dict[RecheckSourceKey, list[tuple[tuple[str, ...], list[dict[str, str]]]]]:
    """Read completed job evidence once for one media item, not the archive."""
    completed: dict[RecheckSourceKey, list[tuple[tuple[str, ...], list[dict[str, str]]]]] = {}
    for record in iter_catalog(
        str(project_path), media_type=media_type, filename_stem=filename_stem,
    ):
        if record.get("error") or assignment_state_for_record(record) != ASSIGNMENT_SUPERSEDED:
            continue
        recheck = _pending_recheck(record)
        if not recheck or recheck.get("state") != RECHECK_COMPLETED:
            continue
        values = _atomic_values(recheck.get("annotation_values"))
        references = recheck.get("replacement_objects")
        if not values or not isinstance(references, list):
            continue
        valid_refs = [reference for reference in references if isinstance(reference, dict)]
        completed.setdefault(RecheckSourceKey.from_record(record), []).append((values, valid_refs))
    return completed


def _reference_paths(
    project_path: Path,
    references: list[dict[str, str]],
) -> list[Path]:
    """Resolve completed lifecycle references back to active catalog sidecars."""
    paths: list[Path] = []
    for reference in references:
        media_type = str(reference.get("media_type") or "")
        filename_stem = str(reference.get("filename_stem") or "")
        label = str(reference.get("label") or "")
        object_id = str(reference.get("object_id") or "")
        if not all((media_type, filename_stem, label, object_id)):
            raise RecheckInputError("completed source job has an incomplete replacement reference")
        matches = [
            candidate for candidate in iter_catalog(
                str(project_path),
                media_type=media_type,
                filename_stem=filename_stem,
                label=label,
            )
            if not candidate.get("error")
            and catalog_object_reference(candidate, candidate.get("path")) == reference
        ]
        if len(matches) != 1 or not assignment_is_active(matches[0]):
            raise RecheckInputError("completed source job replacement is missing or inactive")
        paths.append(Path(matches[0]["path"]))
    return list(dict.fromkeys(paths))


def _completed_source_job_paths(
    project_path: Path,
    source_key: RecheckSourceKey,
    records: list[dict[str, Any]],
    source_values: list[str],
    cache: dict[
        tuple[str, str],
        dict[RecheckSourceKey, list[tuple[tuple[str, ...], list[dict[str, str]]]]],
    ],
) -> list[Path] | None:
    """Return a verified historical result set, including a valid empty set."""
    filename_stems = {str(record.get("filename_stem") or "") for record in records}
    if len(filename_stems) != 1 or not next(iter(filename_stems)):
        raise RecheckInputError("source job does not have one consistent source media item")
    cache_key = (source_key.media_type, next(iter(filename_stems)))
    if cache_key not in cache:
        cache[cache_key] = _completed_sources_for_media_item(
            project_path, media_type=cache_key[0], filename_stem=cache_key[1],
        )
    entries = [
        references
        for values, references in cache[cache_key].get(source_key, [])
        if values == tuple(source_values)
    ]
    if not entries:
        return None
    signatures = {
        tuple((
            str(reference.get("media_type") or ""),
            str(reference.get("filename_stem") or ""),
            str(reference.get("label") or ""),
            str(reference.get("object_id") or ""),
        ) for reference in references)
        for references in entries
    }
    if len(signatures) != 1:
        raise RecheckInputError("completed source job has conflicting replacement evidence")
    return _reference_paths(project_path, entries[0])


def _matching_active_replacements(
    project_path: Path,
    record: dict[str, Any],
    annotation_value: str,
    source_key: RecheckSourceKey,
) -> list[Path]:
    """Find reusable active or staged objects for one exact current source value."""
    matches: list[Path] = []
    source_job = dict(zip(("media_type", "media_id", "shot_id", "field"), source_key))
    for candidate in iter_catalog(
        str(project_path),
        media_type=str(record["media_type"]),
        filename_stem=str(record["filename_stem"]),
        label=annotation_value,
    ):
        if (
            candidate.get("error")
            or str(candidate.get("media_id") or "") != str(record.get("media_id") or "")
            or str(candidate.get("shot_id") or "") != str(record.get("shot_id") or "")
            or str(candidate.get("field") or "") != str(record.get("field") or "")
        ):
            continue
        if not (
            assignment_is_active(candidate)
            or staged_recheck_replacement_matches(
                candidate, source_job=source_job, annotation_value=annotation_value,
            )
        ):
            continue
        path = Path(candidate.get("path") or "")
        if path and path != Path(record.get("path") or ""):
            matches.append(path)
    return matches


def _result_paths(result: dict[str, Any], record: dict[str, Any], annotation_value: str) -> list[Path]:
    """Validate that extractor-reported PNGs have matching canonical JSON sidecars."""
    paths: list[Path] = []
    for raw_png_path in result.get("saved", []):
        path = Path(raw_png_path).with_suffix(".json")
        try:
            replacement = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"extractor reported an unreadable replacement JSON: {path}") from exc
        if (
            str(replacement.get("media_type") or "") != str(record.get("media_type") or "")
            or str(replacement.get("media_id") or "") != str(record.get("media_id") or "")
            or str(replacement.get("shot_id") or "") != str(record.get("shot_id") or "")
            or str(replacement.get("field") or "") != str(record.get("field") or "")
            or str(replacement.get("label") or "") != annotation_value
        ):
            raise RuntimeError(f"extractor replacement does not match recheck request: {path}")
        paths.append(path)
    return paths


def _is_zero_result(result: dict[str, Any]) -> bool:
    if result.get("saved"):
        return False
    reason = str(result.get("reason") or "")
    return reason.startswith(_ZERO_RESULT_PREFIXES)


def _report_paths(project_path: Path, generated_at: str) -> tuple[Path, Path]:
    directory = project_path / "outputs" / "tests" / "silhouette-recheck"
    directory.mkdir(parents=True, exist_ok=True)
    stem = f"recheck-{generated_at.replace(':', '').replace('+00:00', 'Z').replace('-', '')}"
    return directory / f"{stem}.json", directory / f"{stem}.csv"


def _write_report(project_path: Path, report: dict[str, Any]) -> dict[str, str]:
    from data.annotate import atomic_write_text

    json_path, csv_path = _report_paths(project_path, report["generated_at"])
    artifacts = {"report_json": str(json_path), "outcomes_csv": str(csv_path)}
    report["artifacts"] = artifacts
    output = io.StringIO(newline="")
    columns = (
        "outcome", "source_job", "queue_kinds", "historical_record_count",
        "historical_objects", "historical_labels", "media_type", "media_id",
        "shot_id", "field", "source_annotation_values", "recheck_values",
        "produced_objects", "replacement_object_ids", "error",
    )
    writer = csv.DictWriter(output, fieldnames=columns)
    writer.writeheader()
    for row in report["outcomes"]:
        writer.writerow({key: row.get(key, "") for key in columns})
    atomic_write_text(csv_path, output.getvalue())
    atomic_write_text(
        json_path,
        json.dumps(report, indent=2, ensure_ascii=False, default=str) + "\n",
    )
    return artifacts


def _source_job_label(source_key: RecheckSourceKey) -> str:
    return "/".join(source_key)


def _outcome_base(
    source_key: RecheckSourceKey,
    job_records: list[tuple[str, dict[str, Any]]],
) -> dict[str, Any]:
    references = [
        catalog_object_reference(record, record.get("path"))
        for _kind, record in job_records
    ]
    return {
        "source_job": _source_job_label(source_key),
        "source_key": dict(zip(("media_type", "media_id", "shot_id", "field"), source_key)),
        "queue_kinds": sorted({kind for kind, _record in job_records}),
        "historical_record_count": len(job_records),
        "historical_objects": ["/".join((
            reference.get("filename_stem", ""), reference.get("label", ""), reference.get("object_id", ""),
        )) for reference in references],
        "historical_labels": [str(record.get("label") or "") for _kind, record in job_records],
        "media_type": source_key.media_type,
        "media_id": source_key.media_id,
        "shot_id": source_key.shot_id,
        "field": source_key.field,
        "source_annotation_values": [],
        "recheck_values": [],
        "expected_extraction_identities": [],
        "produced_objects": 0,
        "replacement_object_ids": [],
        "error": "",
    }


def _increment(
    breakdown: dict[str, dict[str, int]],
    key: str,
    outcome: str,
    historical_records: int,
    produced: int,
) -> None:
    values = breakdown.setdefault(key or "<missing>", {
        "source_jobs_examined": 0, "source_jobs_completed": 0,
        "source_jobs_zero_result": 0, "source_jobs_failed": 0,
        "historical_records_examined": 0, "historical_records_superseded": 0,
        "historical_records_skipped": 0, "produced_objects": 0,
    })
    values["source_jobs_examined"] += 1
    values["historical_records_examined"] += historical_records
    if outcome == "completed":
        values["source_jobs_completed"] += 1
        values["historical_records_superseded"] += historical_records
    elif outcome == "zero_results":
        values["source_jobs_completed"] += 1
        values["source_jobs_zero_result"] += 1
        values["historical_records_superseded"] += historical_records
    elif outcome == "failed":
        values["source_jobs_failed"] += 1
    elif outcome == "skipped":
        values["historical_records_skipped"] += historical_records
    values["produced_objects"] += produced


def _mark_source_job_error(
    records: list[dict[str, Any]],
    *,
    error: str,
    annotation_values: list[str] | None,
) -> None:
    for record in records:
        mark_recheck_error(
            Path(record["path"]),
            error=error,
            annotation_values=annotation_values,
        )


def _sync_legacy_counts(report: dict[str, Any]) -> None:
    """Keep the initial record-oriented report fields available to callers."""
    report["examined"] = report["historical_records_examined"]
    report["eligible"] = report["historical_records_eligible"]
    report["attempted"] = report["historical_records_attempted"]
    report["completed"] = report["historical_records_superseded"]
    report["zero_results"] = report["source_jobs_zero_result"]
    report["skipped"] = report["historical_records_skipped"]
    report["failed"] = report["historical_records_failed"]


def run_silhouette_rechecks(
    project_path: str | Path,
    *,
    media_type: str | None = None,
    field: str | None = None,
    limit: int | None = None,
    limit_records: int | None = None,
    limit_sources: int | None = None,
    all_sources: bool = False,
    dry_run: bool = False,
    retry_errors: bool = False,
    sam_model_name: str = "sam3.pt",
    frame_model_name: str = "clip-vit-base-patch32",
    verbose: bool = False,
    log_path: str | Path | None = None,
) -> dict[str, Any]:
    """Process complete source jobs of queued or active-questionable records.

    The job unit is ``(media_type, media_id, shot_id, field)``. Every current
    atomic source value is extracted once per job, then the shared replacement
    set is attached to every linked historical object. Callers must explicitly
    select ``all_sources`` or one bounded limit. ``limit`` remains a
    backward-compatible alias for ``limit_records``.
    """
    project = Path(project_path)
    if media_type is not None and media_type not in _MEDIA_TYPES:
        raise ValueError("media_type must be 'movie', 'gameplay', or None")
    if limit is not None and limit_records is not None:
        raise ValueError("Specify only one of limit and limit_records")
    limit_records = limit if limit_records is None else limit_records
    for name, value in (("limit_records", limit_records), ("limit_sources", limit_sources)):
        if value is not None and value <= 0:
            raise ValueError(f"{name} must be positive")
    if sum(value is not None for value in (limit_records, limit_sources)) > 1:
        raise ValueError("Specify only one of limit_sources and limit_records")
    if all_sources and (limit_records is not None or limit_sources is not None):
        raise ValueError("all_sources cannot be combined with a bounded recheck limit")
    if not all_sources and limit_records is None and limit_sources is None:
        raise ValueError("Rechecks require all_sources, limit_sources, or limit_records")
    if not dry_run and _uncoordinated_extraction_process_running():
        raise RuntimeError(
            "A silhouette extraction process is already active without the catalog lock; "
            "wait for it to finish before starting a recheck"
        )

    started_at = _now()
    started_monotonic = time.monotonic()
    mode = "all" if all_sources else (
        "limit-sources" if limit_sources is not None else "limit-records"
    )
    logger = _RecheckLogger(log_path)
    report: dict[str, Any] = {
        "generated_at": started_at,
        "started_at": started_at,
        "finished_at": None,
        "elapsed_seconds": None,
        "interrupted": False,
        "fatal_error": None,
        "command": {
            "media_type": media_type or "both",
            "field": field,
            "mode": mode,
            "all": all_sources,
            "limit_records": limit_records,
            "limit_sources": limit_sources,
            "dry_run": dry_run,
            "retry_errors": retry_errors,
            "sam_model_name": sam_model_name,
            "frame_model_name": frame_model_name,
            "log_path": str(logger.path) if logger.path is not None else None,
        },
        "historical_records_available": 0,
        "historical_records_examined": 0,
        "historical_records_eligible": 0,
        "historical_records_attempted": 0,
        "historical_records_superseded": 0,
        "historical_records_skipped": 0,
        "historical_records_failed": 0,
        "source_jobs_available": 0,
        "source_jobs_examined": 0,
        "source_jobs_eligible": 0,
        "source_jobs_attempted": 0,
        "source_jobs_completed": 0,
        "source_jobs_zero_result": 0,
        "source_jobs_failed": 0,
        "duplicate_source_jobs_avoided": 0,
        "deduplication_ratio": 1.0,
        "model_loads": 0,
        "effective_sam_model_name": None,
        "produced_objects": 0,
        "errors": 0,
        "media_breakdown": {},
        "field_breakdown": {},
        "index_results": {},
        "outcomes": [],
    }
    mutated_media_types: set[str] = set()
    lock = nullcontext() if dry_run else exclusive_catalog_extraction_lock(project)
    try:
        _emit(logger, False, "=" * 60)
        _emit(logger, False, "SILHOUETTE RECHECK RUN")
        _emit(logger, False, f"started_at={started_at}")
        _emit(logger, False, f"project={project}")
        _emit(logger, False, f"media_filter={media_type or 'both'} field_filter={field or 'all'}")
        _emit(logger, False, f"mode={mode} all={all_sources} limit_sources={limit_sources} limit_records={limit_records}")
        _emit(logger, False, f"retry_errors={retry_errors} dry_run={dry_run}")
        _emit(logger, False, f"sam_model={sam_model_name} frame_model={frame_model_name}")
        _emit(logger, False, "=" * 60)
        with lock:
            with tempfile.TemporaryDirectory(prefix="crossing-silhouette-recheck-") as temporary_dir:
                connection = sqlite3.connect(Path(temporary_dir) / "source-jobs.sqlite3")
                try:
                    _create_source_job_store(connection)
                    _populate_source_job_store(
                        connection, project, media_type=media_type, field=field,
                        retry_errors=retry_errors,
                    )
                    historical_available, source_available = _source_job_totals(connection)
                    report["historical_records_available"] = historical_available
                    report["source_jobs_available"] = source_available
                    report["deduplication_ratio"] = round(
                        historical_available / source_available, 6
                    ) if source_available else 1.0
                    _emit(
                        logger,
                        verbose,
                        "queue "
                        f"source_jobs_available={source_available} "
                        f"historical_records_available={historical_available} "
                        f"deduplication_ratio={report['deduplication_ratio']:.6f}",
                    )
                    completed_source_cache = {}
                    recheck_models: _RecheckModels | None = None

                    for source_key, _stored_count in _iter_source_jobs(
                        connection,
                        limit_sources=None if all_sources else limit_sources,
                        limit_records=None if all_sources else limit_records,
                    ):
                        job_records = _job_records(connection, source_key)
                        if not job_records:
                            continue
                        records = [record for _kind, record in job_records]
                        outcome = _outcome_base(source_key, job_records)
                        historical_count = len(records)
                        commit_interrupted = False
                        report["source_jobs_examined"] += 1
                        report["historical_records_examined"] += historical_count
                        source_values: list[str] | None = None
                        try:
                            if "malformed_pending" in outcome["queue_kinds"]:
                                raise RecheckInputError(
                                    "pending recheck identity does not match its catalog object"
                                )
                            filenames = {str(record.get("filename") or "") for record in records}
                            if len(filenames) != 1 or not next(iter(filenames)):
                                raise RecheckInputError("source job does not have one consistent source media file")
                            source_values = _source_annotation_values(project, records[0])
                            outcome["source_annotation_values"] = list(source_values)
                            outcome["recheck_values"] = list(source_values)
                            outcome["expected_extraction_identities"] = [{
                                "media_type": source_key.media_type,
                                "media_id": source_key.media_id,
                                "shot_id": source_key.shot_id,
                                "field": source_key.field,
                                "search_label": value,
                            } for value in source_values]
                        except RecheckInputError as exc:
                            outcome.update({"outcome": "failed", "error": str(exc)})
                            report["source_jobs_failed"] += 1
                            report["historical_records_failed"] += historical_count
                            report["errors"] += 1
                            if not dry_run:
                                _mark_source_job_error(records, error=str(exc), annotation_values=source_values)
                                mutated_media_types.add(source_key.media_type)
                        else:
                            report["source_jobs_eligible"] += 1
                            report["historical_records_eligible"] += historical_count
                            if dry_run:
                                outcome["outcome"] = "eligible"
                            else:
                                report["source_jobs_attempted"] += 1
                                report["historical_records_attempted"] += historical_count
                                try:
                                    for record in records:
                                        queue_recheck_pending(
                                            record["path"], annotation_values=source_values,
                                        )
                                    replacement_paths = _completed_source_job_paths(
                                        project, source_key, records, source_values, completed_source_cache,
                                    )
                                    if replacement_paths is None:
                                        replacement_paths = []
                                        for annotation_value in source_values:
                                            existing = _matching_active_replacements(
                                                project, records[0], annotation_value, source_key,
                                            )
                                            if existing:
                                                replacement_paths.extend(existing)
                                                continue
                                            if recheck_models is None:
                                                try:
                                                    recheck_models = _load_recheck_models(
                                                        str(project), sam_model_name, frame_model_name,
                                                    )
                                                except Exception as exc:
                                                    raise RecheckInfrastructureError(
                                                        f"Could not load run-scoped recheck models: {exc}"
                                                    ) from exc
                                                report["model_loads"] += 1
                                                report["effective_sam_model_name"] = (
                                                    recheck_models.sam_model_name
                                                )
                                                _emit(
                                                    logger,
                                                    verbose,
                                                    "models "
                                                    f"model_loads={report['model_loads']} "
                                                    f"sam_model={recheck_models.sam_model_name} "
                                                    f"sam_device={recheck_models.sam_device} "
                                                    f"clip_device={recheck_models.clip_device}",
                                                )
                                            result = extract_objects_for_shot(
                                                project_path=str(project),
                                                label=annotation_value,
                                                field=source_key.field,
                                                shot_id=source_key.shot_id,
                                                filename=str(records[0]["filename"]),
                                                media_id=source_key.media_id,
                                                media_type=source_key.media_type,
                                                sam_model_name=recheck_models.sam_model_name,
                                                frame_model_name=frame_model_name,
                                                clip_model=recheck_models.clip_model,
                                                clip_processor=recheck_models.clip_processor,
                                                clip_device=recheck_models.clip_device,
                                                mask_generator=recheck_models.mask_generator,
                                                verbose=False,
                                                recheck_source_json_path=records[0]["path"],
                                            )
                                            if result.get("saved"):
                                                fresh_paths = _result_paths(
                                                    result, records[0], annotation_value,
                                                )
                                                stage_recheck_replacements(
                                                    source_job=dict(zip(
                                                        ("media_type", "media_id", "shot_id", "field"), source_key,
                                                    )),
                                                    replacement_json_paths=fresh_paths,
                                                )
                                                replacement_paths.extend(fresh_paths)
                                            elif not _is_zero_result(result):
                                                raise RuntimeError(
                                                    str(result.get("reason") or "recheck extraction failed")
                                                )
                                    replacement_paths = list(dict.fromkeys(replacement_paths))
                                    try:
                                        historical, replacements = complete_recheck_source(
                                            project,
                                            old_json_paths=[record["path"] for record in records],
                                            replacement_json_paths=replacement_paths,
                                            annotation_values=source_values,
                                        )
                                    except SourceJobCommitInterrupted as exc:
                                        historical = exc.historical
                                        replacements = exc.replacements
                                        commit_interrupted = True
                                    outcome["produced_objects"] = len(replacements)
                                    outcome["replacement_object_ids"] = [
                                        catalog_object_reference(replacement, replacement.get("path")).get("object_id", "")
                                        for replacement in replacements
                                    ]
                                    outcome["outcome"] = "completed" if replacements else "zero_results"
                                    report["source_jobs_completed"] += 1
                                    report["historical_records_superseded"] += len(historical)
                                    if not replacements:
                                        report["source_jobs_zero_result"] += 1
                                    report["produced_objects"] += len(replacements)
                                    mutated_media_types.add(source_key.media_type)
                                except (RecheckInfrastructureError, RecheckLogError):
                                    raise
                                except Exception as exc:
                                    outcome.update({"outcome": "failed", "error": str(exc)[:500]})
                                    report["source_jobs_failed"] += 1
                                    report["historical_records_failed"] += historical_count
                                    report["errors"] += 1
                                    try:
                                        _mark_source_job_error(
                                            records, error=str(exc), annotation_values=source_values,
                                        )
                                        mutated_media_types.add(source_key.media_type)
                                    except Exception as persist_exc:
                                        outcome["error"] = (
                                            f"{outcome['error']}; could not persist failure: {persist_exc}"
                                        )[:500]

                        report["outcomes"].append(outcome)
                        _increment(
                            report["media_breakdown"], source_key.media_type, outcome["outcome"],
                            historical_count, outcome["produced_objects"],
                        )
                        _increment(
                            report["field_breakdown"], source_key.field, outcome["outcome"],
                            historical_count, outcome["produced_objects"],
                        )
                        _emit(
                            logger,
                            verbose,
                            f"outcome={outcome['outcome']} source_job={outcome['source_job']} "
                            f"historical_records={historical_count} "
                            f"values={', '.join(outcome['recheck_values'])} "
                            f"replacements={outcome['produced_objects']} "
                            f"error={outcome['error'][:500] if outcome['error'] else '-'}",
                        )
                        if report["source_jobs_examined"] % _PROGRESS_EVERY_JOBS == 0:
                            _emit(
                                logger,
                                verbose,
                                _progress_line(report, time.monotonic() - started_monotonic),
                            )
                        if commit_interrupted:
                            raise KeyboardInterrupt
                finally:
                    connection.close()

            report["duplicate_source_jobs_avoided"] = max(
                0, report["historical_records_examined"] - report["source_jobs_examined"],
            )
            if not dry_run and not report["interrupted"] and not report["fatal_error"]:
                from services.illustration_index import rebuild_index

                for current_media_type in sorted(mutated_media_types):
                    result = rebuild_index(project, "silhouettes", current_media_type)
                    report["index_results"][current_media_type] = result
                    if result.get("status") != "ready":
                        report["errors"] += 1
    except KeyboardInterrupt:
        report["interrupted"] = True
        try:
            _emit(logger, verbose, "interrupted=true; current source job remains incomplete")
        except RecheckLogError:
            pass
    except (RecheckInfrastructureError, RecheckLogError) as exc:
        report["fatal_error"] = str(exc)
        report["errors"] += 1
    except Exception as exc:
        report["fatal_error"] = f"Unexpected recheck infrastructure failure: {exc}"
        report["errors"] += 1
    finally:
        report["finished_at"] = _now()
        report["elapsed_seconds"] = round(time.monotonic() - started_monotonic, 6)
        _sync_legacy_counts(report)
        try:
            report["artifacts"] = _write_report(project, report)
            try:
                _emit(
                    logger,
                    False,
                    "finished "
                    f"finished_at={report['finished_at']} elapsed_seconds={report['elapsed_seconds']:.3f} "
                    f"source_jobs_completed={report['source_jobs_completed']} "
                    f"source_jobs_failed={report['source_jobs_failed']} "
                    f"interrupted={report['interrupted']} fatal_error={report['fatal_error'] or '-'}",
                )
            except RecheckLogError:
                pass
        finally:
            logger.close()
    return report