"""OpenAI image generation provider for the engraving pipeline.

Workflow (per mode)
-------------------
**isolated mode** (default)
1. Check raw.png — bail early if already generated and force=False.
2. (Re-)prepare the canonical folder via ``prepare_engraving_from_source``.
3. Load the silhouette PNG and the isolated prompt template.
4. Expand the template with silhouette + annotation context.
5. Call ``openai.images.edit()`` with the silhouette PNG as the reference.
6. Write ``raw.png``; copy to ``<named-output>.png``.
7. Update ``engraving.json`` and ``request.json``.

**frame mode**
Same as above but uses the full-source-frame as the primary reference and
the silhouette PNG as a secondary guidance image.  Both are passed to the
API.  Requires ``meta["source_frame"]`` to resolve to an existing PNG.

**both mode** (dispatch helper)
Generates isolated first, then frame, in sequence.

The actual OpenAI network call is isolated in ``_call_openai_api()`` so tests
can patch it without needing the ``openai`` package installed.
"""

from __future__ import annotations

import base64
import json
import shutil
import string
from datetime import datetime, timezone
from pathlib import Path

from services.engraving_paths import engraving_paths, resolve_silhouette_png
from services.engraving_prompt import load_engraving_prompt
from services.engraving_smoke import _project_rel, prepare_engraving_from_source
from services.keys import MissingKeyError, get_key  # noqa: F401 — re-exported for callers

OPENAI_API_NAME = "openai"
DEFAULT_MODEL = "gpt-image-2"
DEFAULT_SIZE = "1024x1024"
VALID_SIZES = ("1024x1024", "1024x1792", "1792x1024")


def _set_status(metadata_path: Path, status: str, **extra) -> None:
    """Update ``engraving.json`` status field in-place (best-effort)."""
    try:
        meta = json.loads(metadata_path.read_text(encoding="utf-8"))
        meta["status"] = status
        meta.update(extra)
        metadata_path.write_text(
            json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8"
        )
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Prompt expansion
# ---------------------------------------------------------------------------

def _expand_prompt(template: str, meta: dict) -> str:
    """Expand ``$variable`` placeholders in *template* from silhouette *meta*.

    Variables expanded:
        $label, $field, $movie, $shot_id, $description, $media_id,
        $filename_stem, $filename, $frame, $motif, $human_best

    ``$movie`` and ``$description`` are part of the canonical variable set
    documented in ``services/engraving_prompt.py`` (shared by both the FLUX
    and OpenAI backends). *meta* — the raw silhouette catalog record — does
    not currently carry either value, so they default to an empty string
    here just like the FLUX backend's ``_expand_prompt`` does. This keeps
    the two backends consistent: a template using ``$movie``/``$description``
    renders blank instead of leaking the literal placeholder text into the
    compiled prompt sent to the API.
    """
    context = {
        "label":         meta.get("label", ""),
        "field":         meta.get("field", ""),
        "movie":         meta.get("movie", ""),
        "shot_id":       meta.get("shot_id", ""),
        "description":   meta.get("description", ""),
        "media_id":      meta.get("media_id", ""),
        "filename_stem": meta.get("filename_stem", ""),
        "filename":      meta.get("filename", ""),
        "frame":         str(meta.get("frame", "")),
        "motif":         meta.get("motif", ""),
        "human_best":    str(meta.get("human_best", "")),
    }
    return string.Template(template).safe_substitute(context)


# ---------------------------------------------------------------------------
# Network call — isolated for easy mocking in tests
# ---------------------------------------------------------------------------

def _call_openai_api(
    api_key: str,
    prompt: str,
    image_path: Path,
    model: str,
    size: str,
) -> bytes:
    """Call ``openai.images.edit()`` with a single image and return raw PNG bytes."""
    try:
        import openai
    except ImportError as exc:
        raise ImportError(
            "openai package not installed.\n"
            "Install with: pip install 'openai>=1.0.0'"
        ) from exc

    client = openai.OpenAI(api_key=api_key)
    with open(image_path, "rb") as img_file:
        response = client.images.edit(
            model=model,
            image=img_file,
            prompt=prompt,
            n=1,
            size=size,
        )
    return base64.b64decode(response.data[0].b64_json)


def _call_openai_api_dual(
    api_key: str,
    prompt: str,
    primary_image_path: Path,
    secondary_image_path: Path,
    model: str,
    size: str,
) -> bytes:
    """Call ``openai.images.edit()`` with two images (frame mode) and return raw PNG bytes.

    The primary image is the full source frame; the silhouette is passed as a
    second reference image when the API supports it.  Falls back gracefully to
    the single-image path when the API variant is unavailable.
    """
    try:
        import openai
    except ImportError as exc:
        raise ImportError(
            "openai package not installed.\n"
            "Install with: pip install 'openai>=1.0.0'"
        ) from exc

    client = openai.OpenAI(api_key=api_key)
    # The images.edit endpoint accepts a list of images when the model supports it.
    try:
        with open(primary_image_path, "rb") as primary_f, \
             open(secondary_image_path, "rb") as secondary_f:
            response = client.images.edit(
                model=model,
                image=[primary_f, secondary_f],
                prompt=prompt,
                n=1,
                size=size,
            )
    except Exception:
        # Fallback: single primary image only
        with open(primary_image_path, "rb") as primary_f:
            response = client.images.edit(
                model=model,
                image=primary_f,
                prompt=prompt,
                n=1,
                size=size,
            )
    return base64.b64decode(response.data[0].b64_json)


# ---------------------------------------------------------------------------
# Source frame resolution (full mode)
# ---------------------------------------------------------------------------

def _resolve_source_frame_png(
    source_json: str | Path,
    meta: dict,
    project_path: str | None = None,
) -> Path:
    """Return the full-resolution source frame PNG path for full mode.

    Resolution order
    ----------------
    1. Stored path as-is (works when the path is correct for this machine).
    2. Relative to *project_path* (when ``source_frame`` is project-relative).
    3. Re-rooted under *project_path* by stripping a stale machine-specific
       prefix — useful when the catalog was built on a different machine.
       Looks for the first ``media`` or ``data`` segment in the stored path
       and resolves everything from there under *project_path*.
    4. Relative to the ``source_json`` parent directory.

    Raises ``FileNotFoundError`` when none of the candidates exist.
    """
    source_frame = meta.get("source_frame", "")
    if not source_frame:
        raise FileNotFoundError(
            f"No source_frame in object metadata:\n  {source_json}\n"
            "The 'full' mode requires a source frame PNG in meta['source_frame']."
        )

    stored = Path(source_frame)

    # 1. Direct hit (absolute path already valid, or already relative and CWD-correct)
    if stored.exists():
        return stored

    # 2. Resolve relative to project_path
    if project_path and not stored.is_absolute():
        candidate = Path(project_path) / stored
        if candidate.exists():
            return candidate

    # 3. Re-root under project_path by stripping a stale machine-specific prefix.
    #    Find the first "media" or "data" anchor segment and use the tail.
    if project_path and stored.is_absolute():
        parts = stored.parts
        for anchor in ("media", "data"):
            if anchor in parts:
                tail = Path(*parts[parts.index(anchor):])
                candidate = Path(project_path) / tail
                if candidate.exists():
                    return candidate

    # 4. Relative to the source_json's own directory
    candidate = Path(source_json).parent / source_frame
    if candidate.exists():
        return candidate

    raise FileNotFoundError(
        f"Source frame not found: {stored}\n"
        "The 'full' mode requires the source frame PNG to exist on disk.\n"
        f"  Looked in project: {project_path}"
    )


# ---------------------------------------------------------------------------
# Post-processing
# ---------------------------------------------------------------------------

def _remove_white_background(image_bytes: bytes) -> bytes:
    """Convert a white-background engraving PNG to RGBA with transparent background.

    Maps inverted max-channel brightness to alpha so that:
    - Pure white  (255, 255, 255) → alpha = 0   (fully transparent)
    - Pure black  (0,   0,   0)   → alpha = 255  (fully opaque ink)
    - Mid-greys                   → proportionally semi-transparent

    The RGB channels are preserved so ink colour stays black.
    """
    import io
    import numpy as np
    from PIL import Image

    img = Image.open(io.BytesIO(image_bytes)).convert("RGBA")
    data = np.array(img, dtype=np.uint8)
    # alpha = 255 − max(R, G, B)  →  white→0, black→255
    brightness = data[:, :, :3].max(axis=2)
    data[:, :, 3] = 255 - brightness
    result = Image.fromarray(data, "RGBA")
    buf = io.BytesIO()
    result.save(buf, format="PNG")
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def _generate_registered_openai(project, registry, source, mode, model, size, approved, publish_selected, expected_revision):
    import hashlib
    import sys
    from data.resource_storage import ResourceStore, create_storage_directory
    from data.annotate import atomic_write_text
    from services.resource_publication import complete_registered_engraving_run, prepare_registered_frame
    if source["layout"] != "media-first-v1":
        raise ValueError("Registered generation requires an explicitly published canonical silhouette")
    store = ResourceStore(project, registry, approved=approved)
    record = source["record"]
    reservation = store.reserve_generation_run(source["instance_id"], record["field"], record["label"], "openai", mode)
    work = store.registry.parent / ".resource-attempts" / reservation["token"]
    try:
        create_storage_directory(project, work, exclusive=True)
        silhouette = work / "input.png"
        silhouette.write_bytes(source["image_bytes"])
        prompt_filename, template = load_engraving_prompt(str(project), mode)
        prompt = _expand_prompt(template, record)
        generation = {"service": OPENAI_API_NAME, "model": model, "prompt": prompt, "parameters": {"size": size, "n": 1},
                      "seed_policy": "provider-managed", "version_policy": "provider-managed", "software": {"python": sys.version.split()[0]},
                      "created": datetime.now(timezone.utc).isoformat(), "api": "images.edit", "prompt_file": str(prompt_filename)}
        atomic_write_text(work / "request.json", json.dumps({"generation": generation, "reservation": reservation}, sort_keys=True))
        source_info = {"image_sha256": hashlib.sha256(source["image_bytes"]).hexdigest(), "mask_association": "silhouette-alpha", "evidence": []}
        artifacts = {"input.png": silhouette, "request.json": work / "request.json"}
        api_key = get_key(OPENAI_API_NAME, str(project))
        if mode == "frame":
            frame = prepare_registered_frame(project, source, work / "frame.png")
            source_info["evidence"].append({"media_id": record["media_id"], "frame": record["frame"], "image_sha256": hashlib.sha256(frame.read_bytes()).hexdigest()})
            artifacts["frame.png"] = frame
            image_bytes = _call_openai_api_dual(api_key, prompt, frame, silhouette, model, size)
        else:
            image_bytes = _call_openai_api(api_key, prompt, silhouette, model, size)
        raw = work / "provider-response.png"
        raw.write_bytes(image_bytes)
        output = work / "result.png"
        output.write_bytes(_remove_white_background(image_bytes) if mode == "isolated" else image_bytes)
        artifacts.update({"provider-response.png": raw, "result.png": output})
    except Exception:
        store.finish_generation_attempt(reservation, "failed")
        raise
    completed = complete_registered_engraving_run(project, registry, reservation, generation, source_info, artifacts, approved=approved,
                                                 publish_selected=publish_selected, expected_revision=expected_revision)
    return {**completed, "generation_run": reservation, "metadata": completed.get("publication", {}).get("metadata", completed["run"]),
            "dir": completed["run"].parent, "raw_png": completed["run"].parent / "provider-response.png", "engraving_png": completed["run"].parent / "result.png",
            "mode": mode, "model": model, "size": size, "project": str(project), "expanded_prompt_length": len(prompt)}


def generate_engraving_openai(
    project_path: str,
    source_json: str | Path,
    *,
    mode: str = "isolated",
    model: str = DEFAULT_MODEL,
    size: str = DEFAULT_SIZE,
    force: bool = False,
    registry=None,
    instance_id=None,
    approved: bool = False,
    publish_selected: bool = False,
    expected_revision: int = 0,
) -> dict:
    """Prepare and generate an engraving via the OpenAI images API.

    Parameters
    ----------
    project_path:
        Absolute path to the crossing project directory.
    source_json:
        Path to an ``object_NNNN.json`` file inside the silhouette catalog.
    mode:
        ``"isolated"`` — use silhouette PNG as reference.
        ``"frame"``    — use full source frame + silhouette as references.
        ``"both"``     — generate isolated first, then frame.
    model:
        OpenAI model to use (default: ``gpt-image-2``).
    size:
        Output image size (default: ``1024x1024``).
    force:
        Overwrite an existing ``raw.png`` if present.

    Returns
    -------
    dict
        For single modes: keys ``dir``, ``raw_png``, ``engraving_png``,
        ``metadata``, ``expanded_prompt_length``, ``model``, ``size``,
        ``project``, ``mode``.
        For ``"both"``: list of the two per-mode result dicts.

    Raises
    ------
    FileExistsError
        If ``raw.png`` already exists and *force* is ``False``.
    MissingKeyError
        If the OpenAI API key is not configured.
    FileNotFoundError
        If the silhouette PNG cannot be resolved (or source frame for full mode).
    ImportError
        If the ``openai`` package is not installed.
    """
    if mode == "both":
        results = []
        for m in ("isolated", "frame"):
            r = generate_engraving_openai(
                project_path, source_json,
                mode=m, model=model, size=size, force=force, registry=registry, instance_id=instance_id, approved=approved,
                publish_selected=publish_selected, expected_revision=expected_revision,
            )
            results.append(r)
        return results  # type: ignore[return-value]

    source_json = Path(source_json).resolve()
    project = Path(project_path).resolve()

    from services.resource_resolution import resolve_source_silhouette
    source = resolve_source_silhouette(project, source_json, registry=registry, instance_id=instance_id)
    source_json, meta = source["metadata"], source["record"]
    if registry is not None:
        return _generate_registered_openai(project, registry, source, mode, model, size, approved, publish_selected, expected_revision)
    if source["layout"] != "legacy-v1":
        raise ValueError("Canonical generation output requires the later explicit provider cutover; legacy writer is inactive for canonical sources")
    if registry is not None:
        from data.resource_storage import ResourceStore
        store = ResourceStore(project, registry, approved=approved)
    paths = engraving_paths(str(project), source_json, meta, mode)

    # ── Guard: bail early before any expensive work ──────────────────────────
    from services.engraving_paths import repair_engraving_meta, ENGRAVING_STATUSES
    existing = repair_engraving_meta(paths["metadata"], approved=True)
    existing_status = (existing or {}).get("status")
    if existing_status == "generated" and not force:
        raise FileExistsError(
            f"Engraving already generated ({mode}):\n  {paths['metadata']}\n"
            "Pass force=True (or --force on the CLI) to regenerate."
        )

    # ── Step 1: (re-)prepare the metadata folder ─────────────────────────────
    reservation = None
    if registry is not None:
        reservation = store.reserve_generation_run(source["instance_id"], meta["field"], meta["label"], "openai", mode)
    try:
        prepare_engraving_from_source(str(project), source_json, mode=mode, force=True)

    # ── Step 2: resolve image(s) and prompt ──────────────────────────────────
        sil_png = resolve_silhouette_png(source_json, meta)
        prompt_filename, prompt_template = load_engraving_prompt(str(project), mode)
        expanded_prompt = _expand_prompt(prompt_template, meta)

    # ── Step 3: get OpenAI key ────────────────────────────────────────────────
        api_key = get_key(OPENAI_API_NAME, str(project))

    # ── Mark as generating before the API call so crashed processes are detected
        _set_status(paths["metadata"], "generating")
        # ── Step 4: call the API ──────────────────────────────────────────────────
        if mode == "frame":
            frame_png = _resolve_source_frame_png(source_json, meta, str(project))
            image_bytes = _call_openai_api_dual(
                api_key, expanded_prompt, frame_png, sil_png, model, size
            )
        else:
            image_bytes = _call_openai_api(api_key, expanded_prompt, sil_png, model, size)
    except Exception as exc:
        if reservation is not None:
            store.finish_generation_attempt(reservation, "failed")
        _set_status(paths["metadata"], "failed",
                    last_error=str(exc),
                    last_attempt=datetime.now(timezone.utc).isoformat())
        raise

    # ── Step 5: write raw.png → copy to named engraving output ───────────────
    # For isolated mode, strip the white background so output is RGBA with
    # transparent surroundings: alpha = inverted max-channel brightness so that
    # pure-white areas become fully transparent and black ink stays fully opaque.
    if mode == "isolated":
        image_bytes = _remove_white_background(image_bytes)
    paths["raw_png"].write_bytes(image_bytes)
    shutil.copy2(paths["raw_png"], paths["engraving_png"])

    created = datetime.now(timezone.utc).isoformat()

    # ── Step 6: update engraving.json ────────────────────────────────────────
    eng_meta = json.loads(paths["metadata"].read_text(encoding="utf-8"))
    eng_meta["status"] = "generated"
    eng_meta["mode"] = mode
    eng_meta["generation"].update({
        "service": OPENAI_API_NAME,
        "model": model,
        "api": "images.edit",
        "size": size,
        "created": created,
        "seed_policy": "provider-managed",
        "version_policy": "provider-managed",
        "prompt": expanded_prompt,
        "parameters": {"size": size, "n": 1},
    })
    if reservation is not None:
        eng_meta["generation_run"] = reservation
    # Store provenance for which image(s) were used
    eng_meta["inputs"] = {
        "silhouette_png": _project_rel(str(project), sil_png),
        "source_frame_png": (
            _project_rel(str(project), _resolve_source_frame_png(source_json, meta, str(project)))
            if mode == "frame" and meta.get("source_frame")
            else None
        ),
    }
    from services.resource_publication import publish_engraving
    publish_engraving(project, source, eng_meta, paths["engraving_png"], mode)
    try:
        from services.illustration_index import invalidate_index
        invalidate_index(project, "engravings", meta.get("media_type", "movie"))
    except Exception:
        pass

    # ── Step 7: update request.json ──────────────────────────────────────────
    request_rec = json.loads(paths["request"].read_text(encoding="utf-8"))
    request_rec.update({
        "status": "sent",
        "mode": mode,
        "service": OPENAI_API_NAME,
        "model": model,
        "api": "images.edit",
        "size": size,
        "silhouette_png": _project_rel(str(project), sil_png),
        "created": created,
    })
    paths["request"].write_text(
        json.dumps(request_rec, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    return {
        "dir": paths["dir"],
        "raw_png": paths["raw_png"],
        "engraving_png": paths["engraving_png"],
        "metadata": paths["metadata"],
        "expanded_prompt_length": len(expanded_prompt),
        "model": model,
        "size": size,
        "project": project,
        "mode": mode,
    }
