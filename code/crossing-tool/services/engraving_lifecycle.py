"""Lifecycle tooling for canonical engraving Illustrations.

Covers the operations that were missing once engravings became a real
Illustration source: enumerate them, plan a reference-aware removal, execute
that plan, and audit integrity.

Three rules shape this module:

- **Dry-run is the default.**  :func:`plan_removal` never touches the
  filesystem; only :func:`apply_removal` does, and only from a plan.
- **Unknown state is conservative.**  Anything that cannot be classified is
  reported as ``blocking`` and suppresses deletion rather than being assumed
  safe.
- **Removing assets and rebuilding the index are different operations.**
  Nothing here rebuilds or edits the SQLite index; it only invalidates it, so
  ``crossing index illustration`` stays the single rebuild mechanism.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from services.illustration_index import MEDIA_TYPES

# Files inside a mode directory that are canonical rather than provenance.
_CANONICAL_NAMES = {"engraving.json", "request.json"}

# Directory names beside the canonical mode directories that hold local
# generation artifacts rather than published assets.
PROVENANCE_DIR_NAMES = ("local",)

OK = "ok"
WARNING = "warning"
REPAIRABLE = "repairable"
BLOCKING = "blocking"


def _project(project_path: str | Path) -> Path:
    return Path(project_path)


def catalog_root(project_path: str | Path, media_type: str) -> Path:
    return _project(project_path) / "data" / "engravings" / "catalog" / media_type


# ---------------------------------------------------------------------------
# Enumeration
# ---------------------------------------------------------------------------

def _read_json(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def _resolve_output(project: Path, mode_dir: Path, metadata: dict) -> Path | None:
    raw = metadata.get("output_png")
    if raw:
        candidate = Path(str(raw))
        if not candidate.is_absolute():
            candidate = project / candidate
        if candidate.exists():
            return candidate
    named = [p for p in sorted(mode_dir.glob("*.png")) if p.name != "raw.png"]
    return named[0] if named else None


def scan_engravings(
    project_path: str | Path,
    *,
    media_type: str | None = None,
    backend: str | None = None,
    review_state: str | None = None,
    label: str | None = None,
    title: str | None = None,
    status: str | None = None,
) -> list[dict]:
    """Enumerate canonical engravings from the filesystem, newest path order.

    Reads the same ``engraving.json`` contract the index reads, so this stays
    correct whether or not the index is currently built.
    """
    project = _project(project_path)
    media_types = (media_type,) if media_type else MEDIA_TYPES

    found: list[dict] = []
    for current in media_types:
        base = catalog_root(project, current)
        if not base.is_dir():
            continue
        for metadata_path in sorted(base.rglob("engraving.json")):
            metadata = _read_json(metadata_path)
            if metadata is None:
                found.append({
                    "media_type": current,
                    "engraving_json": metadata_path,
                    "unreadable": True,
                    "mode_dir": metadata_path.parent,
                })
                continue

            mode_dir = metadata_path.parent
            silhouette = metadata.get("silhouette") or {}
            entry = {
                "media_type": current,
                "engraving_json": metadata_path,
                "mode_dir": mode_dir,
                "mode": metadata.get("mode") or mode_dir.name,
                "object_id": mode_dir.parent.name,
                "label": mode_dir.parent.parent.name,
                "title": mode_dir.parent.parent.parent.name,
                "status": metadata.get("status"),
                "review_state": metadata.get("review_state"),
                "backend": (metadata.get("generation") or {}).get("service"),
                "output_png": _resolve_output(project, mode_dir, metadata),
                "silhouette_json": (metadata.get("source") or {}).get("silhouette_json"),
                "field": silhouette.get("field"),
                "shot_id": silhouette.get("shot_id"),
                "metadata": metadata,
                "unreadable": False,
            }
            if backend and entry["backend"] != backend:
                continue
            if review_state and entry["review_state"] != review_state:
                continue
            if label and entry["label"] != label:
                continue
            if title and title.lower() not in entry["title"].lower():
                continue
            if status and entry["status"] != status:
                continue
            found.append(entry)
    return found


def provenance_dirs_for(entry: dict) -> list[Path]:
    """Return local run directories that sit beside a canonical mode dir."""
    object_dir = entry["mode_dir"].parent
    return [
        object_dir / name
        for name in PROVENANCE_DIR_NAMES
        if (object_dir / name).is_dir()
    ]


def orphan_provenance_dirs(project_path: str | Path, media_type: str | None = None) -> list[Path]:
    """Return local run directories that have no published engraving beside them."""
    project = _project(project_path)
    media_types = (media_type,) if media_type else MEDIA_TYPES

    orphans: list[Path] = []
    for current in media_types:
        base = catalog_root(project, current)
        if not base.is_dir():
            continue
        for name in PROVENANCE_DIR_NAMES:
            for run_dir in sorted(base.glob(f"*/*/*/{name}")):
                if not run_dir.is_dir():
                    continue
                object_dir = run_dir.parent
                published = [
                    sibling / "engraving.json"
                    for sibling in object_dir.iterdir()
                    if sibling.is_dir() and sibling.name not in PROVENANCE_DIR_NAMES
                ]
                if not any(path.is_file() for path in published):
                    orphans.append(run_dir)
    return orphans


# ---------------------------------------------------------------------------
# Book reference analysis
# ---------------------------------------------------------------------------

_LAYER_REF_KEYS = ("source", "output_png", "preprocessing_path")


def book_references(project_path: str | Path) -> dict:
    """Classify every image reference held by every book on disk.

    Books normally copy images into their own directory and store a relative
    path, which makes them self-contained and immune to catalog deletion.  An
    absolute reference that points back into the project is a genuine
    dependency and is reported separately.
    """
    project = _project(project_path)
    books_root = project / "outputs" / "books"

    result = {
        "books": [],
        "self_contained": [],
        "external_references": [],
        "missing_references": [],
    }
    if not books_root.is_dir():
        return result

    for book_dir in sorted(p for p in books_root.iterdir() if p.is_dir()):
        layers_path = book_dir / "layers.json"
        layers = _read_json(layers_path) if layers_path.is_file() else None
        slug = book_dir.name
        result["books"].append(slug)
        if not isinstance(layers, list):
            continue

        for layer in layers:
            if not isinstance(layer, dict):
                continue
            for key in _LAYER_REF_KEYS:
                raw = layer.get(key)
                if not isinstance(raw, str) or not raw:
                    continue
                reference = {
                    "book": slug,
                    "layer_id": layer.get("id"),
                    "key": key,
                    "value": raw,
                }
                candidate = Path(raw)
                if candidate.is_absolute() or ".." in Path(raw).parts:
                    reference["resolved"] = str(candidate)
                    reference["exists"] = candidate.exists()
                    result["external_references"].append(reference)
                    continue

                resolved = book_dir / raw
                reference["resolved"] = str(resolved)
                reference["exists"] = resolved.exists()
                if resolved.exists():
                    result["self_contained"].append(reference)
                else:
                    result["missing_references"].append(reference)
    return result


def book_dependencies_for(project_path: str | Path, targets: list[Path]) -> list[dict]:
    """Return book references that resolve to any of *targets*."""
    wanted = {str(Path(t).resolve()) for t in targets if t}
    refs = book_references(project_path)
    hits = []
    for reference in refs["external_references"] + refs["self_contained"]:
        resolved = reference.get("resolved")
        if not resolved:
            continue
        try:
            if str(Path(resolved).resolve()) in wanted:
                hits.append(reference)
        except OSError:
            continue
    return hits


# ---------------------------------------------------------------------------
# Removal planning
# ---------------------------------------------------------------------------

def plan_removal(
    project_path: str | Path,
    entries: list[dict],
    *,
    include_provenance: bool = False,
) -> dict:
    """Return a removal plan without touching the filesystem.

    The plan lists every file that would be deleted, every index that would be
    invalidated, and every reason the operation is considered unsafe.
    """
    project = _project(project_path)

    canonical_assets: list[Path] = []
    metadata_files: list[Path] = []
    provenance_files: list[Path] = []
    provenance_roots: list[Path] = []
    blocking: list[str] = []
    warnings: list[str] = []
    media_types: set[str] = set()

    for entry in entries:
        if entry.get("unreadable"):
            blocking.append(
                f"unreadable engraving.json, cannot classify: {entry['engraving_json']}"
            )
            continue

        media_types.add(entry["media_type"])
        metadata_files.append(entry["engraving_json"])

        output = entry.get("output_png")
        if output and Path(output).is_file():
            canonical_assets.append(Path(output))
        else:
            warnings.append(
                f"canonical asset already missing for {entry['label']}/{entry['object_id']}"
                f"/{entry['mode']}"
            )

        request = entry["mode_dir"] / "request.json"
        if request.is_file():
            metadata_files.append(request)
        raw = entry["mode_dir"] / "raw.png"
        if raw.is_file():
            canonical_assets.append(raw)

        if include_provenance:
            for run_dir in provenance_dirs_for(entry):
                provenance_roots.append(run_dir)
                provenance_files.extend(
                    sorted(p for p in run_dir.rglob("*") if p.is_file())
                )

    targets = canonical_assets + metadata_files + provenance_files
    book_hits = book_dependencies_for(project, targets)
    for hit in book_hits:
        blocking.append(
            f"book {hit['book']!r} layer {hit['layer_id']} references "
            f"{hit['key']}={hit['value']!r}"
        )

    index_records = _index_record_counts(project, entries)

    return {
        "selected": len([e for e in entries if not e.get("unreadable")]),
        "media_types": sorted(media_types),
        "canonical_assets": canonical_assets,
        "metadata_files": metadata_files,
        "provenance_roots": provenance_roots,
        "provenance_files": provenance_files,
        "index_records": index_records,
        "book_dependencies": book_hits,
        "warnings": warnings,
        "blocking": blocking,
        "safe": not blocking,
        "include_provenance": include_provenance,
    }


def _index_record_counts(project: Path, entries: list[dict]) -> dict:
    """Count index rows that currently represent *entries*.

    Matching uses the record's own ``engraving.json`` path, stored
    project-relative in the payload.  The indexed ``title`` column holds a
    display title with the ``{tmdb-…}`` suffix stripped, so it cannot be
    matched against the catalog directory name.
    """
    from services.illustration_index import load_index, query_page

    counts: dict = {"matched": 0, "by_media_type": {}, "index_status": {}}
    by_media: dict[str, list[dict]] = {}
    for entry in entries:
        if entry.get("unreadable"):
            continue
        by_media.setdefault(entry["media_type"], []).append(entry)

    for media_type, group in by_media.items():
        status = load_index(project, "engravings", media_type)
        counts["index_status"][media_type] = status.get("status")
        if not status.get("usable"):
            continue
        matched = 0
        for entry in group:
            try:
                record_path = str(Path(entry["engraving_json"]).relative_to(project))
            except ValueError:
                record_path = str(entry["engraving_json"])
            page = query_page(
                project,
                "engravings",
                media_type,
                record_path=record_path,
                limit=1,
            )
            matched += int(page.get("total") or 0)
        counts["by_media_type"][media_type] = matched
        counts["matched"] += matched
    return counts


def apply_removal(project_path: str | Path, plan: dict, *, force: bool = False) -> dict:
    """Execute a plan produced by :func:`plan_removal`.

    Refuses to run an unsafe plan unless *force* is set.  Never rebuilds the
    index: it only invalidates it, so the index is regenerated by the normal
    ``crossing index illustration`` rebuild.
    """
    from services.illustration_index import invalidate_index

    project = _project(project_path)
    if not plan.get("safe") and not force:
        raise RuntimeError(
            "Refusing to apply an unsafe removal plan:\n  "
            + "\n  ".join(plan.get("blocking", []))
        )

    removed_files = 0
    removed_dirs = 0
    errors: list[str] = []

    for path in plan.get("canonical_assets", []) + plan.get("metadata_files", []):
        try:
            Path(path).unlink(missing_ok=True)
            removed_files += 1
        except OSError as exc:
            errors.append(f"{path}: {exc}")

    for run_dir in plan.get("provenance_roots", []):
        try:
            shutil.rmtree(run_dir, ignore_errors=False)
            removed_dirs += 1
        except OSError as exc:
            errors.append(f"{run_dir}: {exc}")

    # Drop mode directories that are now empty; never touch anything above them.
    for path in plan.get("metadata_files", []):
        parent = Path(path).parent
        try:
            if parent.is_dir() and not any(parent.iterdir()):
                parent.rmdir()
                removed_dirs += 1
        except OSError:
            pass

    for media_type in plan.get("media_types", []):
        invalidate_index(project, "engravings", media_type)

    return {
        "removed_files": removed_files,
        "removed_dirs": removed_dirs,
        "errors": errors,
        "invalidated": list(plan.get("media_types", [])),
    }


# ---------------------------------------------------------------------------
# Doctor
# ---------------------------------------------------------------------------

def _finding(severity: str, check: str, message: str, **extra) -> dict:
    return {"severity": severity, "check": check, "message": message, **extra}


def doctor(project_path: str | Path, *, media_type: str | None = None) -> dict:
    """Audit engraving/Illustration integrity and return structured findings."""
    from services.illustration_index import load_index, query_records

    project = _project(project_path)
    media_types = (media_type,) if media_type else MEDIA_TYPES
    findings: list[dict] = []

    entries = scan_engravings(project, media_type=media_type)
    readable = [e for e in entries if not e.get("unreadable")]

    for entry in entries:
        if entry.get("unreadable"):
            findings.append(_finding(
                BLOCKING, "unreadable_metadata",
                f"engraving.json cannot be parsed: {entry['engraving_json']}",
            ))

    # Duplicate canonical identities.
    seen: dict[tuple, Path] = {}
    for entry in readable:
        identity = (entry["media_type"], entry["title"], entry["label"],
                    entry["object_id"], entry["mode"])
        if identity in seen:
            findings.append(_finding(
                BLOCKING, "duplicate_illustration",
                f"two engraving.json files claim identity {identity}",
                first=str(seen[identity]), second=str(entry["engraving_json"]),
            ))
        else:
            seen[identity] = entry["engraving_json"]

    # Canonical asset present?  Provenance coherent?
    for entry in readable:
        if entry["status"] != "generated":
            continue
        output = entry.get("output_png")
        if not output or not Path(output).is_file():
            findings.append(_finding(
                REPAIRABLE, "missing_canonical_asset",
                f"indexed engraving has no canonical PNG: {entry['engraving_json']}",
            ))

        silhouette_json = entry.get("silhouette_json")
        if silhouette_json:
            candidate = Path(silhouette_json)
            if not candidate.is_absolute():
                candidate = project / candidate
            if not candidate.is_file():
                findings.append(_finding(
                    WARNING, "provenance_missing_source",
                    f"source silhouette missing for {entry['label']}/{entry['object_id']}: "
                    f"{silhouette_json}",
                ))

        provenance = entry["metadata"].get("provenance") or {}
        run_json = provenance.get("run_json")
        if entry["backend"] and entry["backend"] != "openai":
            if not run_json:
                findings.append(_finding(
                    WARNING, "missing_provenance",
                    f"locally generated engraving has no provenance run_json: "
                    f"{entry['engraving_json']}",
                ))
            else:
                candidate = Path(run_json)
                if not candidate.is_absolute():
                    candidate = project / candidate
                if not candidate.is_file():
                    findings.append(_finding(
                        WARNING, "provenance_missing_run",
                        f"provenance run_json is missing: {run_json}",
                    ))

    # Index agreement.
    for current in media_types:
        status = load_index(project, "engravings", current)
        if status.get("status") == "stale":
            findings.append(_finding(
                REPAIRABLE, "stale_index",
                f"{current} engraving index is stale; run: crossing index illustration",
            ))
            continue
        if not status.get("usable"):
            findings.append(_finding(
                REPAIRABLE, "missing_index",
                f"{current} engraving index is {status.get('status')}; "
                f"run: crossing index illustration",
            ))
            continue

        indexed = query_records(project, "engravings", current)
        indexed_ids = {
            (r.get("filename_stem"), r.get("label"), r.get("object_id"), r.get("mode"))
            for r in indexed
        }
        disk_ids = {
            (e["title"], e["label"], e["object_id"], e["mode"])
            for e in readable
            if e["media_type"] == current and e["status"] == "generated"
        }

        for identity in sorted(disk_ids - indexed_ids):
            findings.append(_finding(
                REPAIRABLE, "unindexed_asset",
                f"generated engraving is not in the {current} index: {identity}",
            ))
        for identity in sorted(indexed_ids - disk_ids):
            findings.append(_finding(
                REPAIRABLE, "stale_index_record",
                f"{current} index has a record with no generated engraving on disk: {identity}",
            ))

        for record in indexed:
            output = record.get("output_png")
            if output and not Path(output).is_file():
                findings.append(_finding(
                    REPAIRABLE, "indexed_missing_file",
                    f"{current} index record points at a missing file: {output}",
                ))

    # Local runs that were generated but never published.
    for run_dir in orphan_provenance_dirs(project, media_type):
        findings.append(_finding(
            WARNING, "unpublished_run",
            f"local run exists but was never published as an Illustration: {run_dir}",
        ))

    # Book integrity.
    refs = book_references(project)
    for reference in refs["missing_references"]:
        findings.append(_finding(
            BLOCKING, "dangling_book_reference",
            f"book {reference['book']!r} layer {reference['layer_id']} references "
            f"missing {reference['value']!r}",
        ))
    for reference in refs["external_references"]:
        severity = BLOCKING if not reference.get("exists") else WARNING
        findings.append(_finding(
            severity, "external_book_reference",
            f"book {reference['book']!r} layer {reference['layer_id']} stores an "
            f"absolute {reference['key']} outside the book: {reference['value']!r}",
        ))

    counts = {level: 0 for level in (OK, WARNING, REPAIRABLE, BLOCKING)}
    for finding in findings:
        counts[finding["severity"]] += 1

    return {
        "engravings_on_disk": len(readable),
        "generated_on_disk": len([e for e in readable if e["status"] == "generated"]),
        "books": refs["books"],
        "findings": findings,
        "counts": counts,
        "status": OK if not findings else (
            BLOCKING if counts[BLOCKING] else
            (REPAIRABLE if counts[REPAIRABLE] else WARNING)
        ),
    }
