"""Engraving preparation stage (no API calls).

Creates the canonical engraving asset folder and provenance files from an
existing silhouette catalog object, ready for a later generation step.

Usage::

    from services.engraving_smoke import prepare_engraving_from_source

    result = prepare_engraving_from_source(
        project_path="/path/to/project",
        source_json="/path/to/data/silhouettes/catalog/movie/.../horse/object_0007.json",
        mode="silhouette",  # or "full"
    )
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

from services.engraving_paths import (
    ENGRAVING_SCHEMA_VERSION,
    engraving_paths,
    resolve_silhouette_png,
)
from services.engraving_prompt import EngravingPromptError, load_engraving_prompt


def _project_rel(project_path: str, path_value: str | Path | None) -> str | None:
    """Return *path_value* relative to *project_path* when possible, else as-is."""
    if not path_value:
        return None
    p = Path(path_value)
    if not p.is_absolute():
        return str(p)
    try:
        return str(p.resolve().relative_to(Path(project_path).resolve()))
    except ValueError:
        return str(p)


def prepare_engraving_from_source(
    project_path: str,
    source_json: str | Path,
    *,
    mode: str = "silhouette",
    force: bool = False,
) -> dict:
    """Prepare the canonical engraving folder from a silhouette object JSON.

    Parameters
    ----------
    project_path:
        Absolute path to the crossing project directory.
    source_json:
        Path to an ``object_NNNN.json`` file inside the silhouette catalog.
    mode:
        ``"silhouette"`` or ``"full"``.  Selects the engraving mode and the
        corresponding prompt file set.
    force:
        Overwrite existing ``engraving.json`` if present.

    Returns
    -------
    dict
        Summary with keys ``source_json``, ``silhouette_png``, ``dir``,
        ``metadata``, ``project``, ``mode``.

    Raises
    ------
    FileNotFoundError
        If the sibling silhouette PNG cannot be resolved.
    FileExistsError
        If ``engraving.json`` already exists and *force* is ``False``.
    EngravingPromptError
        If no engraving prompt is found in the project.
    """
    source_json = Path(source_json).resolve()
    project = Path(project_path).resolve()

    meta = json.loads(source_json.read_text(encoding="utf-8"))
    sil_png = resolve_silhouette_png(source_json, meta)

    paths = engraving_paths(str(project), source_json, meta, mode)
    eng_json_path = paths["metadata"]

    if eng_json_path.exists() and not force:
        from services.engraving_paths import read_engraving_meta
        existing = read_engraving_meta(eng_json_path)
        existing_status = (existing or {}).get("status", "pending")
        # Only block re-preparation when engraving is already done or queued.
        # Failed engravings can be retried without --force.
        if existing_status not in ("failed",):
            raise FileExistsError(
                f"Engraving already {existing_status}:\n  {eng_json_path}\n"
                "Pass force=True (or --force on the CLI) to overwrite."
            )

    prompt_filename, prompt_text = load_engraving_prompt(str(project), mode)
    prompt_sha256 = hashlib.sha256(prompt_text.encode("utf-8")).hexdigest()
    prompt_path_rel = _project_rel(
        str(project),
        project / "prompts" / "engravings" / prompt_filename,
    )

    paths["dir"].mkdir(parents=True, exist_ok=True)

    # ── request.json  (stub — no API call yet) ────────────────────────────────
    request_stub = {
        "status": "pending",
        "mode": mode,
        "service": None,
        "model": None,
        "prompt_file": prompt_filename,
        "prompt_path": prompt_path_rel,
        "prompt_sha256": prompt_sha256,
        "object_id": source_json.stem,
        "silhouette_png": _project_rel(str(project), sil_png),
    }
    paths["request"].write_text(
        json.dumps(request_stub, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    # ── engraving.json  (provenance record) ───────────────────────────────────
    created = datetime.now(timezone.utc).isoformat()
    engraving_meta = {
        "schema_version": ENGRAVING_SCHEMA_VERSION,
        "status": "pending",
        "mode": mode,
        "source": {
            "silhouette_json": _project_rel(str(project), source_json),
            "silhouette_png": _project_rel(str(project), sil_png),
            "source_frame": _project_rel(str(project), meta.get("source_frame")) or "",
        },
        "silhouette": {
            "label": meta.get("label"),
            "field": meta.get("field"),
            "media_type": meta.get("media_type"),
            "filename": meta.get("filename"),
            "filename_stem": meta.get("filename_stem"),
            "media_id": meta.get("media_id"),
            "shot_id": meta.get("shot_id"),
            "frame": meta.get("frame"),
            "confidence": meta.get("confidence"),
            "bbox": meta.get("bbox"),
            "mask_area": meta.get("mask_area"),
            "frame_size": meta.get("frame_size"),
            "human_best": meta.get("human_best", False),
            "motif": meta.get("motif"),
        },
        "generation": {
            "service": None,
            "model": None,
            "api": None,
            "created": created,
        },
        "prompt": {
            "prompt_file": prompt_filename,
            "prompt_path": prompt_path_rel,
            "prompt_sha256": prompt_sha256,
        },
        "outputs": {
            "raw_png": "raw.png",
            "engraving_png": paths["engraving_png"].name,
        },
    }
    paths["metadata"].write_text(
        json.dumps(engraving_meta, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    return {
        "source_json": source_json,
        "silhouette_png": sil_png,
        "dir": paths["dir"],
        "metadata": paths["metadata"],
        "project": project,
        "mode": mode,
    }
