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

from services.engraving_local_config import CONFIG_SCHEMA_VERSION, canonical_mode, resolve_config
from services.engraving_local_paths import (
    LOCAL_ENGRAVING_SCHEMA_VERSION,
    is_complete,
    resolve_run_paths,
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
    registry=None,
    instance_id=None,
    approved: bool = False,
    publish_selected: bool = False,
    expected_revision: int = 0,
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
    from services.resource_resolution import resolve_source_silhouette
    source = resolve_source_silhouette(project_path, source_json, registry=registry, instance_id=instance_id)
    source_json = source["metadata"]
    if registry is None and source["layout"] != "legacy-v1":
        raise ValueError("Canonical generation requires explicit provider output cutover; legacy writer is disabled for canonical sources")
    if registry is not None:
        from data.resource_storage import ResourceStore
        store = ResourceStore(project_path, registry, approved=approved)
        if source["layout"] != "media-first-v1" or output_png is not None:
            raise ValueError("Registered conversion requires a canonical silhouette and run-owned output")
    config = config or resolve_config()
    seed = int(config["run"]["seed"] if seed is None else seed)

    record = source["record"]
    mode = canonical_mode(config)
    # Each (object, mode) owns an independent provenance namespace, so an
    # isolated run can never be served from a frame cache or vice versa.
    reservation = None
    if registry is not None:
        from data.resource_storage import create_storage_directory
        from services.engraving_local_paths import _paths_for_dir
        reservation = store.reserve_generation_run(source["instance_id"], record["field"], record["label"], "local", mode)
        work = store.registry.parent / ".resource-attempts" / reservation["token"]
        paths, legacy_layout = _paths_for_dir(work, source_json, record), False
    else:
        paths, legacy_layout = resolve_run_paths(project_path, source_json, record, mode)

    if is_complete(paths) and not force:
        existing = json.loads(paths["run_json"].read_text(encoding="utf-8"))
        existing["reused_cache"] = True
        # A run completed before publication existed (or with publishing off)
        # still owns a canonical asset; publish it without re-running the model.
        if config["run"].get("publish", True) and not (existing.get("publication") or {}).get("published"):
            from services.engraving_local_publish import publish_run

            existing["publication"] = publish_run(project_path, existing, config, registry=registry)
            _write_json(paths["run_json"], existing)
        return existing

    try:
        if registry is not None:
            create_storage_directory(project_path, work, exclusive=True)
        evidence = assemble_evidence(source_json, config, resolved_source=source, project=project_path, workdir=work) if registry is not None else assemble_evidence(source_json, config)
    except Exception:
        if reservation is not None:
            store.finish_generation_attempt(reservation, "failed")
        raise

    paths["dir"].mkdir(parents=True, exist_ok=True)
    paths["evidence_dir"].mkdir(parents=True, exist_ok=True)

    owns_model = model is None
    if owns_model:
        try:
            model = LocalEngravingModel(project_path, config)
        except Exception:
            if reservation is not None:
                store.finish_generation_attempt(reservation, "failed")
            raise

    run: dict = {
        "schema_version": LOCAL_ENGRAVING_SCHEMA_VERSION,
        "config_schema_version": CONFIG_SCHEMA_VERSION,
        "status": "running",
        "created_at": _now(),
        "seed": seed,
        "generation_run": reservation,
        "mode": mode,
        "provenance_layout": "legacy" if legacy_layout else "mode-aware",
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

        if registry is None and not config["run"].get("keep_intermediates", True):
            for key in ("repair_png", "raw_engraving_png"):
                paths[key].unlink(missing_ok=True)

        if output_png:
            output_png = Path(output_png)
            output_png.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(paths["final_png"], output_png)
            run["outputs"]["requested_png"] = str(output_png)

        # -- publication: the one canonical asset becomes an Illustration ---
        if registry is None and config["run"].get("publish", True):
            from services.engraving_local_publish import publish_run

            run["publication"] = publish_run(project_path, run, config, force=force, registry=registry)
        else:
            run["publication"] = {"published": False, "reason": "publishing disabled"}

    except Exception as exc:
        if reservation is not None:
            store.finish_generation_attempt(reservation, "failed")
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
    if registry is not None:
        from services.resource_publication import complete_registered_engraving_run
        model_info = run["model"]
        generation = {"service": "qwen-local", "model": model_info["pipeline_repo"],
                      "model_version": model_info.get("model_version") or model_info["pipeline_repo"],
                      "seed": seed, "prompt": json.dumps({stage["stage"]: stage["prompt"] for stage in run["stages"] if "prompt" in stage}, sort_keys=True),
                      "parameters": {"config": config, "stages": [{key: stage[key] for key in ("stage", "settings") if key in stage} for stage in run["stages"]], "model": model_info},
                      "software": {"python": run["environment"]["python"]}, "created": run["created_at"]}
        source_info = {"image_sha256": run["inputs"]["silhouette_sha256"], "mask_association": "silhouette-alpha", "evidence": []}
        artifacts = {"result.png": paths["final_png"], "input.png": Path(evidence["silhouette_png"]), "provider-run.json": paths["run_json"],
                     "subject.png": paths["evidence_subject"], "mask.png": paths["evidence_mask"]}
        for key in ("repair_png", "raw_engraving_png"):
            if paths[key].is_file():
                artifacts[paths[key].name] = paths[key]
        if evidence["source_frame"]:
            frame = Path(evidence["source_frame"])
            source_info["evidence"].append({"media_id": record["media_id"], "frame": record["frame"], "image_sha256": _sha256(frame)})
            artifacts["frame.png"] = frame
        run["registered_run"] = complete_registered_engraving_run(project_path, registry, reservation, generation, source_info, artifacts, approved=approved,
                                                                 publish_selected=publish_selected, expected_revision=expected_revision,
                                                                 review_state="rejected" if run["status"] == "rejected" else "needs_review")
    return run


def convert_batch(
    project_path: str | Path,
    sources: list[str | Path],
    *,
    config: dict | None = None,
    force: bool = False,
    on_result=None,
    registry=None,
    approved: bool = False,
    publish_selected: bool = False,
    expected_revision: int = 0,
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
    if registry is not None:
        from data.resource_storage import ResourceStore
        ResourceStore(project_path, registry, approved=approved)
        from services.resource_resolution import resolve_source_silhouette
        for source_path in sources:
            if resolve_source_silhouette(project_path, source_path, registry=registry)["layout"] != "media-first-v1":
                raise ValueError("Registered batch requires explicitly published canonical silhouettes")

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
                    **({"registry": registry, "approved": approved, "publish_selected": publish_selected, "expected_revision": expected_revision} if registry is not None else {}),
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
