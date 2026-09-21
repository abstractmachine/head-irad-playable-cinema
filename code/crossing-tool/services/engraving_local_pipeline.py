"""Orchestrator for the local (offline) engraving converter.

Runs the five logical stages — evidence, repair, engrave, postprocess,
validate — and writes a complete, inspectable run record.

Two guarantees this module is responsible for:

- **Inputs are never written to.**  Everything is produced under the object's
  own ``local`` engraving directory; the silhouette catalog is read-only here.
- **One model per run.**  :class:`LocalEngravingModel` is constructed once by
  :func:`convert_batch` and passed to every object, so a batch never reloads
  the 20B transformer per item.
"""

from __future__ import annotations

import hashlib
import json
import platform
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from services.engraving_local_config import CONFIG_SCHEMA_VERSION, resolve_config
from services.engraving_local_paths import (
    LOCAL_ENGRAVING_SCHEMA_VERSION,
    engraving_paths,
    is_complete,
)

_HASH_CHUNK = 1 << 20


class EngravingRunError(RuntimeError):
    """Raised when a run cannot be completed."""


def _sha256(path: str | Path) -> str | None:
    path = Path(path)
    if not path.is_file():
        return None
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(_HASH_CHUNK), b""):
            digest.update(block)
    return digest.hexdigest()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _environment() -> dict:
    from services.engraving_local_model import describe_device

    return {
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "device": describe_device(),
    }


def _write_json(path: Path, payload: dict) -> None:
    from data.annotate import atomic_write_text

    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(path, json.dumps(payload, indent=2, ensure_ascii=False) + "\n")


def convert_one(
    project_path: str | Path,
    source_json: str | Path,
    *,
    config: dict | None = None,
    model=None,
    seed: int | None = None,
    force: bool = False,
    output_png: str | Path | None = None,
) -> dict:
    """Convert one silhouette into an isolated engraving and return the run record.

    When *model* is ``None`` a pipeline is loaded for this single conversion and
    released afterwards.  Batch callers should pass a shared model instead.
    """
    from services.engraving_local_alpha import postprocess_file, white_to_alpha
    from services.engraving_local_evidence import assemble_evidence
    from services.engraving_local_model import LocalEngravingModel
    from services.engraving_local_stages import run_engrave, run_repair
    from services.engraving_local_validate import validate_result

    project_path = Path(project_path)
    source_json = Path(source_json)
    config = config or resolve_config()
    seed = int(config["run"]["seed"] if seed is None else seed)

    evidence = assemble_evidence(source_json, config)
    record = evidence["record"]
    paths = engraving_paths(project_path, source_json, record)

    if is_complete(paths) and not force:
        existing = json.loads(paths["run_json"].read_text(encoding="utf-8"))
        existing["reused_cache"] = True
        # A run completed before publication existed (or with publishing off)
        # still owns a canonical asset; publish it without re-running the model.
        if config["run"].get("publish", True) and not (existing.get("publication") or {}).get("published"):
            from services.engraving_local_publish import publish_run

            existing["publication"] = publish_run(project_path, existing, config)
            _write_json(paths["run_json"], existing)
        return existing

    paths["dir"].mkdir(parents=True, exist_ok=True)
    paths["evidence_dir"].mkdir(parents=True, exist_ok=True)

    owns_model = model is None
    if owns_model:
        model = LocalEngravingModel(project_path, config)

    run: dict = {
        "schema_version": LOCAL_ENGRAVING_SCHEMA_VERSION,
        "config_schema_version": CONFIG_SCHEMA_VERSION,
        "status": "running",
        "created_at": _now(),
        "seed": seed,
        "provenance": evidence["provenance"],
        "source_json": str(source_json),
        "run_json": str(paths["run_json"]),
        "inputs": {
            "silhouette_png": evidence["silhouette_png"],
            "silhouette_sha256": _sha256(evidence["silhouette_png"]),
            "source_frame": evidence["source_frame"],
            "source_frame_sha256": _sha256(evidence["source_frame"]) if evidence["source_frame"] else None,
            "context_frames": evidence["context_frames"],
        },
        "coordinate_transform": evidence["transform"],
        "evidence_review": evidence["review"],
        "environment": _environment(),
        "config": config,
        "stages": [],
    }

    started = time.time()
    peak_vram = 0
    try:
        evidence["subject"].save(paths["evidence_subject"])
        evidence["mask"].save(paths["evidence_mask"])

        # -- stage 2: structural repair ---------------------------------
        current = evidence["subject"]
        if config["repair"]["enabled"]:
            repair = run_repair(
                model=model,
                evidence=evidence,
                config=config,
                project_path=project_path,
                seed=seed,
            )
            repair["image"].save(paths["repair_png"])
            current = repair["image"]
            peak_vram = max(peak_vram, repair["peak_vram_bytes"] or 0)
            run["stages"].append(
                {
                    "stage": "repair",
                    "output": str(paths["repair_png"]),
                    "output_sha256": _sha256(paths["repair_png"]),
                    "output_size": list(repair["image"].size),
                    "seconds": repair["seconds"],
                    "peak_vram_bytes": repair["peak_vram_bytes"],
                    "reference_count": repair["reference_count"],
                    "references": repair["references"],
                    "settings": repair["settings"],
                    "prompt": repair["prompt"],
                }
            )

        # -- stage 3: engraving ------------------------------------------
        if config["engrave"]["enabled"]:
            engrave = run_engrave(
                model=model,
                evidence=evidence,
                source_image=current,
                config=config,
                project_path=project_path,
                seed=seed,
            )
            engrave["image"].save(paths["raw_engraving_png"])
            current = engrave["image"]
            peak_vram = max(peak_vram, engrave["peak_vram_bytes"] or 0)
            run["stages"].append(
                {
                    "stage": "engrave",
                    "output": str(paths["raw_engraving_png"]),
                    "output_sha256": _sha256(paths["raw_engraving_png"]),
                    "output_size": list(engrave["image"].size),
                    "seconds": engrave["seconds"],
                    "peak_vram_bytes": engrave["peak_vram_bytes"],
                    "settings": engrave["settings"],
                    "prompt": engrave["prompt"],
                }
            )
        else:
            current.save(paths["raw_engraving_png"])

        # -- stage 4: deterministic alpha ---------------------------------
        alpha_cfg = config["alpha"]
        alpha_stats = postprocess_file(
            paths["raw_engraving_png"],
            paths["final_png"],
            white_point=alpha_cfg["white_point"],
            padding_fraction=alpha_cfg["padding_fraction"],
            seal_size=alpha_cfg["seal_size"],
        )
        run["stages"].append(
            {
                "stage": "postprocess",
                "output": str(paths["final_png"]),
                "output_sha256": _sha256(paths["final_png"]),
                "output_size": alpha_stats["size"],
                "settings": dict(alpha_cfg),
                "alpha": {k: v for k, v in alpha_stats.items() if k not in {"path", "size"}},
            }
        )

        # -- stage 5: validation ------------------------------------------
        from PIL import Image

        with Image.open(paths["final_png"]) as opened:
            final_rgba = opened.convert("RGBA")
            verdict = validate_result(final_rgba, evidence, alpha_stats, config)

        run["validation"] = verdict
        run["status"] = verdict["status"]

        # Named copy other pipeline stages reference.
        import shutil

        shutil.copy2(paths["final_png"], paths["named_png"])
        run["outputs"] = {
            "repair_png": str(paths["repair_png"]) if paths["repair_png"].is_file() else None,
            "raw_engraving_png": str(paths["raw_engraving_png"]),
            "final_png": str(paths["final_png"]),
            "named_png": str(paths["named_png"]),
        }

        if not config["run"].get("keep_intermediates", True):
            for key in ("repair_png", "raw_engraving_png"):
                paths[key].unlink(missing_ok=True)

        if output_png:
            output_png = Path(output_png)
            output_png.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(paths["final_png"], output_png)
            run["outputs"]["requested_png"] = str(output_png)

        # -- publication: the one canonical asset becomes an Illustration ---
        if config["run"].get("publish", True):
            from services.engraving_local_publish import publish_run

            run["publication"] = publish_run(project_path, run, config, force=force)
        else:
            run["publication"] = {"published": False, "reason": "publishing disabled"}

    except Exception as exc:
        run["status"] = "failed"
        run["error"] = f"{type(exc).__name__}: {exc}"
        run["total_seconds"] = time.time() - started
        run["peak_vram_bytes"] = peak_vram or None
        run["model"] = model.provenance() if model else None
        _write_json(paths["run_json"], run)
        if owns_model and model:
            model.release()
        raise EngravingRunError(f"{source_json}: {exc}") from exc

    run["total_seconds"] = time.time() - started
    run["peak_vram_bytes"] = peak_vram or None
    run["model"] = model.provenance()
    run["completed_at"] = _now()
    _write_json(paths["run_json"], run)

    if owns_model:
        model.release()
    return run


def convert_batch(
    project_path: str | Path,
    sources: list[str | Path],
    *,
    config: dict | None = None,
    force: bool = False,
    on_result=None,
) -> dict:
    """Convert many silhouettes with one shared model instance.

    *on_result* is called as ``on_result(index, total, source, run, error)``
    after each item so callers can report progress without this module owning
    any presentation.
    """
    from services.engraving_local_model import LocalEngravingModel

    project_path = Path(project_path)
    config = config or resolve_config()
    total = len(sources)

    summary = {
        "total": total,
        "converted": 0,
        "reused": 0,
        "rejected": 0,
        "needs_review": 0,
        "failed": 0,
        "runs": [],
        "started_at": _now(),
    }

    model = LocalEngravingModel(project_path, config)
    try:
        for index, source in enumerate(sources, start=1):
            error = None
            run = None
            try:
                run = convert_one(
                    project_path,
                    source,
                    config=config,
                    model=model,
                    force=force,
                )
                if run.get("reused_cache"):
                    summary["reused"] += 1
                else:
                    summary["converted"] += 1
                status = run.get("status")
                if status in summary:
                    summary[status] += 1
                summary["runs"].append(
                    {
                        "source_json": str(source),
                        "status": status,
                        "run_json": run.get("run_json"),
                        "reused_cache": bool(run.get("reused_cache")),
                    }
                )
            except Exception as exc:
                error = exc
                summary["failed"] += 1
                summary["runs"].append(
                    {"source_json": str(source), "status": "failed", "error": str(exc)}
                )
            if on_result:
                on_result(index, total, source, run, error)
    finally:
        model.release()

    summary["completed_at"] = _now()
    return summary
