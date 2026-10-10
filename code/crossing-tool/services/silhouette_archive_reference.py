"""Explicit historical reference access; never participates in active discovery."""

import hashlib
import json
import re
import sqlite3
from pathlib import Path


class ArchiveReferenceError(ValueError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def original_reference_path(project_path, reference):
    from services.silhouette_archive import archive_path
    from services.silhouette_catalog import _safe_label

    project = Path(project_path).resolve()
    if isinstance(reference, dict):
        keys = ("media_type", "filename_stem", "label", "object_id")
        if not all(isinstance(reference.get(key), str) and reference[key] for key in keys):
            raise ArchiveReferenceError("INVALID_IDENTITY", "Incomplete legacy catalog reference")
        value = f"data/silhouettes/catalog/{reference['media_type']}/{reference['filename_stem']}/{_safe_label(reference['label'])}/{reference['object_id']}.json"
    else:
        candidate = Path(reference)
        if candidate.is_absolute():
            try:
                value = candidate.relative_to(project).as_posix()
            except ValueError as exc:
                raise ArchiveReferenceError("FOREIGN_ROOT", "Absolute path does not belong to this project") from exc
        else:
            value = candidate.as_posix()
    try:
        archive_path(value)
    except ValueError as exc:
        raise ArchiveReferenceError("INVALID_PATH", str(exc)) from exc
    parts = Path(value).parts
    if len(parts) != 7 or parts[:3] != ("data", "silhouettes", "catalog") or parts[3] not in ("movie", "gameplay") or not re.fullmatch(r"object_\d+\.(json|png)", parts[-1]):
        raise ArchiveReferenceError("INVALID_PATH", "Exact legacy catalog JSON/PNG reference required")
    return value


def _safe_file(project, relative):
    path = project / relative
    if any(parent.is_symlink() for parent in (path, *path.parents) if parent.is_relative_to(project) and parent != project):
        raise ArchiveReferenceError("UNSAFE_PATH", "Symlinked silhouette reference")
    return path


def _mapping(lookup, original):
    if lookup is None:
        raise ArchiveReferenceError("MAPPING_REQUIRED", "Explicit historical lookup database is required")
    database_path = Path(lookup)
    if database_path.is_symlink() or not database_path.is_file():
        raise ArchiveReferenceError("INVALID_LOOKUP", "Lookup must be an existing regular database")
    connection = sqlite3.connect(database_path.resolve().as_uri() + "?mode=ro", uri=True)
    try:
        rows = connection.execute("SELECT destination,sha256 FROM assets WHERE path=?", (original,)).fetchall()
    except sqlite3.Error as exc:
        raise ArchiveReferenceError("INVALID_LOOKUP", "Lookup does not contain the archive assets contract") from exc
    finally:
        connection.close()
    if not rows:
        raise ArchiveReferenceError("MAPPING_MISSING", "No preserved mapping for historical reference")
    if len(rows) != 1:
        raise ArchiveReferenceError("MAPPING_AMBIGUOUS", "Multiple preserved mappings for historical reference")
    from services.silhouette_archive import archive_path
    destination, checksum = rows[0]
    if destination != archive_path(original) or not isinstance(checksum, str) or len(checksum) != 64 or any(char not in "0123456789abcdef" for char in checksum):
        raise ArchiveReferenceError("INVALID_MAPPING", "Mapping must preserve hierarchy and include SHA256")
    return destination, checksum


def resolve_silhouette_reference(project_path, reference, *, include_archive=False, lookup=None):
    from services.silhouette_catalog import assignment_state_for_record

    project = Path(project_path).resolve()
    original = original_reference_path(project, reference)
    source = _safe_file(project, original)
    if source.is_file():
        metadata = _safe_file(project, Path(original).with_suffix(".json").as_posix())
        try:
            if not metadata.is_file() and include_archive:
                metadata = resolve_silhouette_reference(project, Path(original).with_suffix(".json"), include_archive=True, lookup=lookup)["path"]
            record = json.loads(metadata.read_text())
            state = assignment_state_for_record(record)
        except (OSError, ValueError, AttributeError) as exc:
            raise ArchiveReferenceError("METADATA_MISSING", "Cannot authorize silhouette lifecycle") from exc
        if not include_archive and state != "active":
            raise ArchiveReferenceError("NOT_ACTIVE", "Historical silhouette requires explicit access")
        return {"status": "original", "path": source, "original_path": original, "assignment_state": state}
    if not include_archive:
        raise ArchiveReferenceError("SOURCE_MISSING", "Working silhouette is missing; archive fallback is disabled")
    destination, checksum = _mapping(lookup, original)
    archived = _safe_file(project, destination)
    if not archived.is_file():
        raise ArchiveReferenceError("ARCHIVE_MISSING", "Mapping exists but archived asset is missing")
    hasher = hashlib.sha256()
    with archived.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            hasher.update(chunk)
    if hasher.hexdigest() != checksum:
        raise ArchiveReferenceError("CHECKSUM_MISMATCH", "Archived asset differs from preserved checksum")
    if archived.suffix == ".json":
        try:
            record = json.loads(archived.read_text())
        except ValueError as exc:
            raise ArchiveReferenceError("INVALID_METADATA", "Archived metadata is not valid JSON") from exc
        if not isinstance(record, dict) or assignment_state_for_record(record) != "superseded":
            raise ArchiveReferenceError("NOT_SUPERSEDED", "Archived metadata must be a retired silhouette")
        from services.silhouette_catalog import _safe_label
        parts = Path(original).parts
        expected = {"media_type": parts[3], "filename_stem": parts[4], "object_id": Path(original).stem}
        if any(record.get(key) not in (None, value) for key, value in expected.items()) or (record.get("label") and _safe_label(record["label"]) != parts[5]):
            raise ArchiveReferenceError("IDENTITY_MISMATCH", "Archived metadata disagrees with its original identity")
    else:
        metadata = resolve_silhouette_reference(project, Path(original).with_suffix(".json"), include_archive=True, lookup=lookup)
        record = json.loads(metadata["path"].read_text())
        if record.get("png", Path(original).name) != Path(original).name:
            raise ArchiveReferenceError("IDENTITY_MISMATCH", "PNG is not owned by its legacy record")
    return {"status": "archived", "path": archived, "original_path": original, "assignment_state": "superseded", "checksum_verified": True}


def inspect_historical_lineage(project_path, reference, *, lookup=None):
    resolved = resolve_silhouette_reference(project_path, reference, include_archive=True, lookup=lookup)
    if resolved["path"].suffix != ".json":
        raise ArchiveReferenceError("METADATA_REQUIRED", "Lineage inspection requires a JSON reference")
    record = json.loads(resolved["path"].read_text())
    assignment = record.get("assignment") or {}
    result = {"record": resolved, "predecessors": [], "replacements": []}
    for field, output in (("supersedes", "predecessors"), ("superseded_by", "replacements")):
        references = assignment.get(field) or []
        references = [references] if isinstance(references, dict) else references
        for item in references:
            try:
                result[output].append(resolve_silhouette_reference(project_path, item, include_archive=True, lookup=lookup))
            except ArchiveReferenceError as exc:
                result[output].append({"status": "error", "code": exc.code, "reference": item})
    return result


def resolve_historical_report_reference(project_path, reference, *, lookup, field=None):
    if field is not None:
        if not isinstance(reference, dict) or not isinstance(reference.get(field), str):
            raise ArchiveReferenceError("INVALID_REPORT_REFERENCE", "Requested report field is not a path")
        reference = reference[field]
    return resolve_silhouette_reference(project_path, reference, include_archive=True, lookup=lookup)