"""OpenAI image generation provider for the engraving pipeline.

Workflow (per mode)
-------------------
**silhouette mode** (default)
1. Check raw.png — bail early if already generated and force=False.
2. (Re-)prepare the canonical folder via ``prepare_engraving_from_source``.
3. Load the silhouette PNG and the silhouette prompt template.
4. Expand the template with silhouette + annotation context.
5. Call ``openai.images.edit()`` with the silhouette PNG as the reference.
6. Write ``raw.png``; copy to ``<named-output>.png``.
7. Update ``engraving.json`` and ``request.json``.

**full mode**
Same as above but uses the full-source-frame as the primary reference and
the silhouette PNG as a secondary guidance image.  Both are passed to the
API.  Requires ``meta["source_frame"]`` to resolve to an existing PNG.

**both mode** (dispatch helper)
Generates silhouette first, then full, in sequence.

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


# ---------------------------------------------------------------------------
# Prompt expansion
# ---------------------------------------------------------------------------

def _expand_prompt(template: str, meta: dict) -> str:
    """Expand ``$variable`` placeholders in *template* from silhouette *meta*.

    Variables expanded:
        $label, $field, $shot_id, $media_id, $filename_stem, $filename,
        $frame, $motif, $human_best
    """
    context = {
        "label":         meta.get("label", ""),
        "field":         meta.get("field", ""),
        "shot_id":       meta.get("shot_id", ""),
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
    """Call ``openai.images.edit()`` with two images (full mode) and return raw PNG bytes.

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
# Main entry point
# ---------------------------------------------------------------------------

def generate_engraving_openai(
    project_path: str,
    source_json: str | Path,
    *,
    mode: str = "silhouette",
    model: str = DEFAULT_MODEL,
    size: str = DEFAULT_SIZE,
    force: bool = False,
) -> dict:
    """Prepare and generate an engraving via the OpenAI images API.

    Parameters
    ----------
    project_path:
        Absolute path to the crossing project directory.
    source_json:
        Path to an ``object_NNNN.json`` file inside the silhouette catalog.
    mode:
        ``"silhouette"`` — use silhouette PNG as reference.
        ``"full"``       — use full source frame + silhouette as references.
        ``"both"``       — generate silhouette first, then full.
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
        for m in ("silhouette", "full"):
            r = generate_engraving_openai(
                project_path, source_json,
                mode=m, model=model, size=size, force=force,
            )
            results.append(r)
        return results  # type: ignore[return-value]

    source_json = Path(source_json).resolve()
    project = Path(project_path).resolve()

    meta = json.loads(source_json.read_text(encoding="utf-8"))
    paths = engraving_paths(str(project), source_json, meta, mode)

    # ── Guard: bail early before any expensive work ──────────────────────────
    if paths["raw_png"].exists() and not force:
        raise FileExistsError(
            f"Engraving already generated ({mode}):\n  {paths['raw_png']}\n"
            "Pass force=True (or --force on the CLI) to regenerate."
        )

    # ── Step 1: (re-)prepare the metadata folder ─────────────────────────────
    prepare_engraving_from_source(str(project), source_json, mode=mode, force=True)

    # ── Step 2: resolve image(s) and prompt ──────────────────────────────────
    sil_png = resolve_silhouette_png(source_json, meta)
    prompt_filename, prompt_template = load_engraving_prompt(str(project), mode)
    expanded_prompt = _expand_prompt(prompt_template, meta)

    # ── Step 3: get OpenAI key ────────────────────────────────────────────────
    api_key = get_key(OPENAI_API_NAME, str(project))

    # ── Step 4: call the API ──────────────────────────────────────────────────
    if mode == "full":
        frame_png = _resolve_source_frame_png(source_json, meta, str(project))
        image_bytes = _call_openai_api_dual(
            api_key, expanded_prompt, frame_png, sil_png, model, size
        )
    else:
        image_bytes = _call_openai_api(api_key, expanded_prompt, sil_png, model, size)

    # ── Step 5: write raw.png → copy to named engraving output ───────────────
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
    })
    # Store provenance for which image(s) were used
    eng_meta["inputs"] = {
        "silhouette_png": _project_rel(str(project), sil_png),
        "source_frame_png": (
            _project_rel(str(project), _resolve_source_frame_png(source_json, meta, str(project)))
            if mode == "full" and meta.get("source_frame")
            else None
        ),
    }
    paths["metadata"].write_text(
        json.dumps(eng_meta, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

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
