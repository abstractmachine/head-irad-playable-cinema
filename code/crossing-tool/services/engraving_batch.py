"""Batch engraving generation service.

Scans the silhouette catalog for objects marked ``human_best=true`` and
generates missing engravings for them.  Supports three modes:

- ``"silhouette"`` — generate the silhouette engraving only.
- ``"full"``       — generate the full-frame engraving only.
- ``"both"``       — generate silhouette first, then full, for each target.

Usage::

    from services.engraving_batch import (
        scan_best_silhouettes,
        batch_generate_engravings,
    )

    targets = scan_best_silhouettes(project_path, media_type="movie")
    summary = batch_generate_engravings(
        project_path,
        targets,
        mode="both",
        force=False,
    )
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Callable

from services.engraving_paths import engraving_is_generated
from services.silhouette_catalog import catalog_base_dir


# ---------------------------------------------------------------------------
# Scanning for best silhouettes
# ---------------------------------------------------------------------------

def scan_best_silhouettes(
    project_path: str,
    media_type: str = "movie",
    label: str | None = None,
) -> list[dict]:
    """Return catalog objects that are marked ``human_best=true``.

    Parameters
    ----------
    project_path:
        Absolute path to the crossing project directory.
    media_type:
        ``"movie"`` or ``"gameplay"``.
    label:
        Optional label filter (e.g. ``"horse"``).  When ``None`` all labels
        are scanned.

    Returns
    -------
    list[dict]
        Each entry has keys:
        ``path`` (Path), ``meta`` (dict), ``media_type``, ``label``.
    """
    base = catalog_base_dir(project_path, media_type)
    if not base.is_dir():
        return []

    results: list[dict] = []
    pattern = "**/*.json"
    for json_path in sorted(base.glob(pattern)):
        # Skip non-object JSON files (e.g. .scanned sentinels have no extension mismatch)
        if not json_path.name.startswith("object_"):
            continue
        try:
            meta = json.loads(json_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue

        if not meta.get("human_best"):
            continue

        obj_label = meta.get("label", "")
        if label is not None and obj_label != label:
            continue

        results.append({
            "path": json_path,
            "meta": meta,
            "media_type": meta.get("media_type", media_type),
            "label": obj_label,
        })

    return results


# ---------------------------------------------------------------------------
# Batch generation
# ---------------------------------------------------------------------------

def batch_generate_engravings(
    project_path: str,
    targets: list[dict],
    *,
    mode: str = "silhouette",
    model: str = "gpt-image-2",
    size: str = "1024x1024",
    force: bool = False,
    verbose: bool = False,
    on_item_done: Callable[[dict, dict | list, Exception | None], None] | None = None,
) -> dict:
    """Generate engravings for a list of catalog targets.

    Parameters
    ----------
    project_path:
        Absolute path to the crossing project directory.
    targets:
        Output of :func:`scan_best_silhouettes` — list of dicts with ``path``
        and ``meta`` keys.
    mode:
        ``"silhouette"``, ``"full"``, or ``"both"``.
    model:
        OpenAI model name.
    size:
        Output image size string.
    force:
        Regenerate even if already done.
    verbose:
        Print progress to stdout.
    on_item_done:
        Optional callback called after each target with
        ``(target, result, exc)``.  ``result`` is a dict (single mode) or
        list (both); ``exc`` is ``None`` on success.

    Returns
    -------
    dict
        ``generated``, ``skipped``, ``failed``, ``total`` counts.
    """
    from services.engraving_generate_openai import generate_engraving_openai

    generated = 0
    skipped = 0
    failed = 0

    modes_to_check = ("silhouette", "full") if mode == "both" else (mode,)

    for target in targets:
        source_json = target["path"]
        meta = target["meta"]
        label = target.get("label", meta.get("label", "?"))

        # Skip check: all required modes already generated
        if not force:
            all_done = all(
                engraving_is_generated(project_path, source_json, meta, m)
                for m in modes_to_check
            )
            if all_done:
                if verbose:
                    print(f"  skip  {source_json.stem}  [{label}]  (already generated)")
                skipped += 1
                if on_item_done:
                    on_item_done(target, {}, None)
                continue

        if verbose:
            print(f"  gen   {source_json.stem}  [{label}]  mode={mode}")

        try:
            result = generate_engraving_openai(
                project_path,
                source_json,
                mode=mode,
                model=model,
                size=size,
                force=force,
            )
            generated += 1
            if on_item_done:
                on_item_done(target, result, None)
        except Exception as exc:
            failed += 1
            if verbose:
                print(f"  fail  {source_json.stem}  [{label}]: {exc}")
            if on_item_done:
                on_item_done(target, {}, exc)

    return {
        "generated": generated,
        "skipped": skipped,
        "failed": failed,
        "total": len(targets),
    }
