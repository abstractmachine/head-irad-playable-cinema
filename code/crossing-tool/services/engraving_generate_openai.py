"""OpenAI image generation provider for the engraving pipeline.

Workflow
--------
1. Check raw.png — bail early if already generated and force=False.
2. (Re-)prepare the canonical folder via ``prepare_engraving_from_source``.
3. Load the silhouette PNG and the latest prompt template.
4. Expand the template in memory with silhouette metadata.
5. Call ``openai.images.edit()`` with the silhouette as the reference image.
6. Write ``raw.png``; copy to ``engraving.png``.
7. Update ``engraving.json`` (status=generated) and ``request.json``.

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
    """Expand ``$variable`` placeholders in *template* from silhouette *meta*."""
    context = {
        "label":         meta.get("label", ""),
        "field":         meta.get("field", ""),
        "shot_id":       meta.get("shot_id", ""),
        "media_id":      meta.get("media_id", ""),
        "filename_stem": meta.get("filename_stem", ""),
        "filename":      meta.get("filename", ""),
        "frame":         str(meta.get("frame", "")),
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
    """Call ``openai.images.edit()`` and return the raw PNG bytes."""
    try:
        import openai
    except ImportError as exc:
        raise ImportError(
            "openai package not installed.\n"
            "Install with: pip install 'openai>=1.0.0'\n"
            "Or reinstall crossing with the engraving extra."
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


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def generate_engraving_openai(
    project_path: str,
    source_json: str | Path,
    *,
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
    model:
        OpenAI model to use (default: ``gpt-image-1``).
    size:
        Output image size (default: ``1024x1024``).
    force:
        Overwrite an existing ``raw.png`` if present.

    Returns
    -------
    dict
        Keys: ``dir``, ``raw_png``, ``engraving_png``, ``metadata``,
        ``expanded_prompt_length``, ``model``, ``size``, ``project``.

    Raises
    ------
    FileExistsError
        If ``raw.png`` already exists and *force* is ``False``.
    MissingKeyError
        If the OpenAI API key is not configured.
    FileNotFoundError
        If the silhouette PNG cannot be resolved.
    ImportError
        If the ``openai`` package is not installed.
    """
    source_json = Path(source_json).resolve()
    project = Path(project_path).resolve()

    meta = json.loads(source_json.read_text(encoding="utf-8"))
    paths = engraving_paths(str(project), source_json, meta)

    # ── Guard: bail early before any expensive work ──────────────────────────
    if paths["raw_png"].exists() and not force:
        raise FileExistsError(
            f"Engraving already generated:\n  {paths['raw_png']}\n"
            "Pass force=True (or --force on the CLI) to regenerate."
        )

    # ── Step 1: (re-)prepare the metadata folder ─────────────────────────────
    # force=True here so we always get fresh JSON that we can update after generation.
    prepare_engraving_from_source(str(project), source_json, force=True)

    # ── Step 2: resolve silhouette PNG and prompt ─────────────────────────────
    sil_png = resolve_silhouette_png(source_json, meta)
    prompt_filename, prompt_template = load_engraving_prompt(str(project))
    expanded_prompt = _expand_prompt(prompt_template, meta)

    # ── Step 3: get OpenAI key ────────────────────────────────────────────────
    api_key = get_key(OPENAI_API_NAME, str(project))

    # ── Step 4: call the API ──────────────────────────────────────────────────
    image_bytes = _call_openai_api(api_key, expanded_prompt, sil_png, model, size)

    # ── Step 5: write raw.png → copy to engraving.png ────────────────────────
    paths["raw_png"].write_bytes(image_bytes)
    shutil.copy2(paths["raw_png"], paths["engraving_png"])

    created = datetime.now(timezone.utc).isoformat()

    # ── Step 6: update engraving.json ────────────────────────────────────────
    eng_meta = json.loads(paths["metadata"].read_text(encoding="utf-8"))
    eng_meta["status"] = "generated"
    eng_meta["generation"].update({
        "service": OPENAI_API_NAME,
        "model": model,
        "api": "images.edit",
        "size": size,
        "created": created,
    })
    paths["metadata"].write_text(
        json.dumps(eng_meta, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    # ── Step 7: update request.json ──────────────────────────────────────────
    request_rec = json.loads(paths["request"].read_text(encoding="utf-8"))
    request_rec.update({
        "status": "sent",
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
    }
