"""Provenance path resolution for locally generated engravings.

These paths hold *generation artifacts*, never canonical Illustration assets.
The canonical asset is published separately by
``services/engraving_local_publish.py`` into the provider layout
(``…/<object_id>/<mode>/engraving.json`` plus its named PNG); nothing here ever
gains an Illustration identity because no ``engraving.json`` is written beside
it.

Canonical Illustration identity already treats ``isolated`` and ``frame`` as
distinct variants, so provenance is namespaced the same way::

    <project>/data/engravings/catalog/<media>/<stem>/<label>/<object_id>/local/
        isolated/
            evidence/subject.png
            evidence/mask.png
            01-repair.png
            02-engraving-raw.png
            03-engraving-alpha.png
            run.json
        frame/
            …

Runs written before this layout existed live directly in ``local/``.  They stay
readable via :func:`resolve_run_paths` and are converted in place by
:func:`migrate_legacy_run`, which renames rather than copies so bytes,
timestamps and every ``*_sha256`` recorded in ``run.json`` remain valid.
"""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path

LOCAL_ENGRAVING_SCHEMA_VERSION = "1"
LOCAL_MODE = "local"

# Explicit lifecycle states stored in run.json["status"].
RUN_STATUSES = ("pending", "running", "rejected", "needs_review", "failed")


def _safe_mode(mode: str) -> str:
    """Return a filesystem-safe canonical mode segment."""
    cleaned = re.sub(r"[^a-z0-9_]", "_", str(mode or "").lower().strip())
    return cleaned or "isolated"


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


def engraving_dir(
    project_path: str | Path,
    source_json: str | Path,
    record: dict,
    mode: str,
) -> Path:
    """Return the provenance directory for one ``(object, mode)`` local run."""
    return _object_local_root(project_path, source_json, record) / _safe_mode(mode)


def legacy_engraving_dir(
    project_path: str | Path,
    source_json: str | Path,
    record: dict,
) -> Path:
    """Return the pre-mode-aware provenance directory (``…/<object_id>/local``)."""
    return _object_local_root(project_path, source_json, record)


def _object_local_root(
    project_path: str | Path,
    source_json: str | Path,
    record: dict,
) -> Path:
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


def _paths_for_dir(base: Path, source_json: str | Path, record: dict) -> dict:
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


def engraving_paths(
    project_path: str | Path,
    source_json: str | Path,
    record: dict,
    mode: str,
) -> dict:
    """Return every provenance path for one ``(object, mode)`` local run."""
    return _paths_for_dir(
        engraving_dir(project_path, source_json, record, mode), source_json, record
    )


def legacy_engraving_paths(
    project_path: str | Path,
    source_json: str | Path,
    record: dict,
) -> dict:
    """Return provenance paths in the pre-mode-aware layout."""
    return _paths_for_dir(
        legacy_engraving_dir(project_path, source_json, record), source_json, record
    )


def resolve_run_paths(
    project_path: str | Path,
    source_json: str | Path,
    record: dict,
    mode: str,
) -> tuple[dict, bool]:
    """Return ``(paths, is_legacy)`` for an existing run of this object+mode.

    New runs always write the mode-aware layout.  A legacy ``…/local/run.json``
    is only adopted when no mode-aware run exists *and* it belongs to this
    mode, so an unmigrated isolated run can never be mistaken for a frame run.
    """
    paths = engraving_paths(project_path, source_json, record, mode)
    if is_complete(paths):
        return paths, False

    legacy = legacy_engraving_paths(project_path, source_json, record)
    if is_complete(legacy) and legacy_run_mode(legacy["run_json"]) == mode:
        return legacy, True
    return paths, False


def legacy_run_mode(run_json: str | Path) -> str | None:
    """Return the canonical mode a legacy run belongs to, or ``None``.

    Prefers the mode already recorded by publication; otherwise derives it from
    the run's own stored configuration."""
    from services.engraving_local_config import canonical_mode

    try:
        run = json.loads(Path(run_json).read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None

    published = (run.get("publication") or {}).get("mode")
    if published:
        return str(published)
    config = run.get("config")
    return canonical_mode(config) if isinstance(config, dict) else None


def find_legacy_runs(project_path: str | Path, media_type: str | None = None) -> list[Path]:
    """Return every legacy ``…/<object_id>/local/run.json`` still on disk."""
    from services.illustration_index import MEDIA_TYPES

    project = Path(project_path)
    media_types = (media_type,) if media_type else MEDIA_TYPES

    found: list[Path] = []
    for current in media_types:
        base = project / "data" / "engravings" / "catalog" / current
        if not base.is_dir():
            continue
        found.extend(sorted(base.glob(f"*/*/*/{LOCAL_MODE}/run.json")))
    return found


def migrate_legacy_run(run_json: str | Path, *, apply: bool = False) -> dict:
    """Move a legacy run into its mode-aware subdirectory.

    The move is a rename, so bytes and timestamps survive and every
    ``*_sha256`` already recorded in ``run.json`` stays valid.  Only the stored
    paths are rewritten; nothing is regenerated and no file is overwritten.
    """
    run_json = Path(run_json)
    legacy_dir = run_json.parent
    mode = legacy_run_mode(run_json)

    result = {
        "run_json": str(run_json),
        "legacy_dir": str(legacy_dir),
        "mode": mode,
        "migrated": False,
    }
    if not mode:
        result["reason"] = "cannot determine the mode this run belongs to"
        return result

    target = legacy_dir / _safe_mode(mode)
    result["target_dir"] = str(target)
    if target.exists():
        result["reason"] = f"target already exists: {target}"
        return result
    if not apply:
        result["reason"] = "dry-run"
        return result

    target.mkdir(parents=True)
    for child in sorted(legacy_dir.iterdir()):
        if child == target:
            continue
        shutil.move(str(child), str(target / child.name))

    _rewrite_run_paths(run_json=target / "run.json", legacy_dir=legacy_dir, target=target)
    result["migrated"] = True
    result["run_json"] = str(target / "run.json")
    return result


def _rewrite_run_paths(*, run_json: Path, legacy_dir: Path, target: Path) -> None:
    """Repoint a migrated run record, and any publication that cites it."""
    from data.annotate import atomic_write_text

    old = str(legacy_dir)
    new = str(target)

    def _fix(value):
        if isinstance(value, str) and value.startswith(old + "/"):
            return new + value[len(old):]
        if isinstance(value, list):
            return [_fix(item) for item in value]
        if isinstance(value, dict):
            return {key: _fix(item) for key, item in value.items()}
        return value

    run = json.loads(run_json.read_text(encoding="utf-8"))
    run = _fix(run)
    run["provenance_layout"] = "mode-aware"
    run["migrated_from"] = old
    atomic_write_text(run_json, json.dumps(run, indent=2, ensure_ascii=False) + "\n")

    # A published engraving.json cites the run by path; keep it resolvable.
    object_dir = legacy_dir.parent
    for metadata_path in sorted(object_dir.glob("*/engraving.json")):
        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        provenance = metadata.get("provenance") or {}
        stored = provenance.get("run_json")
        if not isinstance(stored, str):
            continue
        legacy_rel = Path(old).name
        if f"/{legacy_rel}/run.json" not in stored and not stored.endswith(f"{legacy_rel}/run.json"):
            continue
        provenance["run_json"] = stored.replace(
            f"{legacy_rel}/run.json", f"{legacy_rel}/{target.name}/run.json"
        )
        metadata["provenance"] = provenance
        atomic_write_text(
            metadata_path, json.dumps(metadata, indent=2, ensure_ascii=False) + "\n"
        )


def is_complete(paths: dict) -> bool:
    """Return True when a finished run already exists at *paths*."""
    return paths["run_json"].is_file() and paths["final_png"].is_file()
