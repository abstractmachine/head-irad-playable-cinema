"""Publish a completed local engraving run as a canonical Illustration.

The local converter produces a staged run directory full of provenance
(evidence, repair, raw engraving, validation, timings).  None of that is an
Illustration.  This module takes the one *canonical asset* out of a run — the
final cropped RGBA engraving — and publishes it through the same contract the
provider pipeline already uses:

    data/engravings/catalog/<media>/<stem>/<label>/<object_id>/<mode>/
        engraving.json                                  <- index contract
        <title_stub>-<frame_id>-<object_id>-<mode>.png  <- canonical asset

``services.illustration_index._scan_engravings`` indexes any directory
containing an ``engraving.json`` whose ``status`` is ``"generated"``, so
publishing is exactly "write that file".  The backend is recorded inside
``generation.service``; it is deliberately **not** part of the Illustration
identity, which stays ``(media_type, filename_stem, label, object_id, mode)``.

Run artifacts stay where the run wrote them and are referenced from
``provenance``; they never gain an Illustration identity of their own because
they have no ``engraving.json`` beside them.
"""

from __future__ import annotations

import json
import shutil
from datetime import datetime, timezone
from pathlib import Path

GENERATION_SERVICE = "qwen-local"

# Review states carried as a top-level scalar in engraving.json.  They land in
# the index payload automatically, so no index schema change is needed.
REVIEW_STATES = ("needs_review", "accepted", "rejected")

# A run whose automatic gates rejected it is never publishable.
_UNPUBLISHABLE = "rejected"


class PublicationError(RuntimeError):
    """Raised when a run cannot be published as a canonical Illustration."""


def canonical_mode(config: dict) -> str:
    """Return the canonical engraving mode this run's evidence corresponds to.

    The provider pipeline's two modes describe *what evidence the model saw*:
    ``isolated`` is the silhouette alone, ``frame`` adds the source frame as a
    second reference.  The local converter's ``use_source_frame`` switch means
    the same thing, so local runs reuse the existing axis rather than
    inventing a backend-named pseudo-mode.
    """
    return "frame" if config.get("evidence", {}).get("use_source_frame") else "isolated"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _relative_to_project(project_path: Path, path: Path) -> str:
    try:
        return str(Path(path).relative_to(project_path))
    except ValueError:
        return str(path)


def existing_publication(engraving_json: Path) -> dict | None:
    """Return an already-published ``engraving.json`` payload, if any."""
    if not engraving_json.is_file():
        return None
    try:
        return json.loads(engraving_json.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def build_engraving_metadata(
    project_path: str | Path,
    run: dict,
    *,
    mode: str,
    output_png: Path,
    review_state: str,
) -> dict:
    """Return the canonical ``engraving.json`` payload for a local run.

    Mirrors the provider pipeline's structure so a consumer cannot tell the
    backends apart except by reading ``generation.service``.
    """
    project_path = Path(project_path)
    provenance = run.get("provenance", {})
    model = run.get("model", {})
    inputs = run.get("inputs", {})

    evidence_frames = [inputs["source_frame"]] if inputs.get("source_frame") else []
    evidence_frames.extend(inputs.get("context_frames") or [])

    return {
        "schema_version": "2",
        "status": "generated",
        "mode": mode,
        # Scalars, so services.illustration_index copies them into the payload.
        # The nested "generation" dict is authoritative but, being a dict, is
        # not carried into the index record.
        "review_state": review_state,
        "generation_service": GENERATION_SERVICE,
        "silhouette": {
            "label": provenance.get("label"),
            "field": provenance.get("field"),
            "media_type": provenance.get("media_type"),
            "filename": provenance.get("filename"),
            "filename_stem": provenance.get("filename_stem"),
            "media_id": provenance.get("media_id"),
            "shot_id": provenance.get("shot_id"),
            "frame": provenance.get("frame"),
        },
        "source": {
            "silhouette_json": _relative_to_project(project_path, Path(run["source_json"])),
            "silhouette_png": _relative_to_project(project_path, Path(inputs["silhouette_png"])),
            "source_frame": (
                _relative_to_project(project_path, Path(inputs["source_frame"]))
                if inputs.get("source_frame") else None
            ),
            # Forward-compatible: a future evidence stage may select several
            # frames from the same shot; the list already carries them.
            "evidence_frames": [
                _relative_to_project(project_path, Path(p)) for p in evidence_frames
            ],
        },
        "generation": {
            "service": GENERATION_SERVICE,
            "model": model.get("pipeline_repo"),
            "model_license": model.get("pipeline_license"),
            "transformer_backend": model.get("transformer_backend"),
            "seed": run.get("seed"),
            "created": _now(),
        },
        "output_png": _relative_to_project(project_path, output_png),
        "provenance": {
            "run_json": _relative_to_project(project_path, Path(run["run_json"])),
            "total_seconds": run.get("total_seconds"),
            "peak_vram_bytes": run.get("peak_vram_bytes"),
            "stages": [stage.get("stage") for stage in run.get("stages", [])],
            "validation_status": (run.get("validation") or {}).get("status"),
        },
    }


def publish_run(
    project_path: str | Path,
    run: dict,
    config: dict,
    *,
    force: bool = False,
) -> dict:
    """Publish a completed local run as a canonical Illustration.

    Returns a dict describing what happened, with ``published`` False and a
    ``reason`` when publication was intentionally skipped.
    """
    from services.engraving_paths import engraving_paths
    from services.illustration_index import invalidate_index

    project_path = Path(project_path)

    validation = run.get("validation") or {}
    review_state = validation.get("status") or _UNPUBLISHABLE
    if review_state == _UNPUBLISHABLE:
        return {
            "published": False,
            "reason": "automatic gates rejected this run; it is not publishable",
            "review_state": review_state,
        }

    final_png = Path((run.get("outputs") or {}).get("final_png") or "")
    if not final_png.is_file():
        raise PublicationError(f"Run has no final engraving to publish: {final_png}")

    source_json = Path(run["source_json"])
    record = run.get("silhouette_record") or {}
    if not record:
        record = json.loads(source_json.read_text(encoding="utf-8"))

    mode = canonical_mode(config)
    paths = engraving_paths(project_path, source_json, record, mode=mode)

    previous = existing_publication(paths["metadata"])
    if previous and not force:
        prior_service = (previous.get("generation") or {}).get("service")
        if prior_service and prior_service != GENERATION_SERVICE:
            return {
                "published": False,
                "reason": (
                    f"canonical {mode} engraving already published by "
                    f"{prior_service!r}; re-run with --force to replace it"
                ),
                "mode": mode,
                "engraving_json": str(paths["metadata"]),
                "existing_service": prior_service,
            }

    paths["dir"].mkdir(parents=True, exist_ok=True)
    shutil.copy2(final_png, paths["engraving_png"])

    metadata = build_engraving_metadata(
        project_path,
        run,
        mode=mode,
        output_png=paths["engraving_png"],
        review_state=review_state,
    )

    from data.annotate import atomic_write_text

    atomic_write_text(
        paths["metadata"], json.dumps(metadata, indent=2, ensure_ascii=False) + "\n"
    )

    media_type = record.get("media_type") or "movie"
    invalidate_index(project_path, "engravings", media_type)

    return {
        "published": True,
        "mode": mode,
        "review_state": review_state,
        "replaced": bool(previous),
        "engraving_json": str(paths["metadata"]),
        "engraving_png": str(paths["engraving_png"]),
        "media_type": media_type,
    }


def set_review_state(engraving_json: str | Path, review_state: str) -> dict:
    """Record a human review decision on a published engraving.

    ``rejected`` also clears ``status`` so the record leaves the browse index
    on the next rebuild without deleting any asset.
    """
    from data.annotate import atomic_write_text

    if review_state not in REVIEW_STATES:
        raise PublicationError(
            f"Unknown review state {review_state!r}. Valid: {', '.join(REVIEW_STATES)}"
        )

    engraving_json = Path(engraving_json)
    metadata = existing_publication(engraving_json)
    if metadata is None:
        raise PublicationError(f"Not a published engraving: {engraving_json}")

    metadata["review_state"] = review_state
    metadata["status"] = "failed" if review_state == "rejected" else "generated"
    metadata["reviewed_at"] = _now()

    atomic_write_text(
        engraving_json, json.dumps(metadata, indent=2, ensure_ascii=False) + "\n"
    )
    return metadata
