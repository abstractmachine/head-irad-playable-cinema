"""FLUX.1-Kontext-dev engraving generator.

Pipeline:

    preprocessing PNG (RGBA)
        ↓  composited onto white, passed as reference image
    FLUX.1-Kontext-dev
        ↓  conditioned on the project engraving prompt
    raw_png   (RGB FLUX output, saved first)
        ↓  binary threshold (strict B&W)
    output_png  (black = line, white = paper, no greys)

The preprocessing PNG is used directly — no scribble conversion, no mask.
Prompt is loaded from <project>/prompts/engravings/<latest>.txt.
Optional *context* dict is used for {variable} substitution in the prompt
(e.g. ``{label}``, ``{field}``).  Unknown placeholders are left unchanged.
"""

from __future__ import annotations

import json
from pathlib import Path

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

MODEL_NAME    = "FLUX.1-Kontext-dev"
GENERATOR_TAG = "flux_kontext_v1"

DEFAULT_SEED      = 42
DEFAULT_STEPS     = 28       # recommended for Kontext-dev
DEFAULT_GUIDANCE  = 2.5
DEFAULT_THRESHOLD = 128      # binary conversion midpoint


# ---------------------------------------------------------------------------
# Prompt context injection
# ---------------------------------------------------------------------------

class _SafeDict(dict):
    """dict subclass that returns ``{key}`` for missing keys.

    Allows ``prompt.format_map(_SafeDict(**ctx))`` to expand known variables
    while leaving unknown ``{placeholders}`` unchanged rather than raising.
    """

    def __missing__(self, key: str) -> str:
        return f"{{{key}}}"


# ---------------------------------------------------------------------------
# Model validation
# ---------------------------------------------------------------------------

def validate_models(project_path: str) -> None:
    """Raise RuntimeError if the FLUX Kontext model directory is missing."""
    model_dir = Path(project_path) / "models" / MODEL_NAME
    if not model_dir.is_dir():
        raise RuntimeError(
            f"Engraving model not found:\n  {model_dir}\n\n"
            f"Run:  crossing tool model download black-forest-labs/FLUX.1-Kontext-dev"
        )


# ---------------------------------------------------------------------------
# Core generation
# ---------------------------------------------------------------------------

def generate_engraving(
    *,
    project_path: str,
    preprocessing_path: str,
    engraving_id: str,
    cache_dir: Path,
    seed: int = DEFAULT_SEED,
    num_inference_steps: int = DEFAULT_STEPS,
    guidance_scale: float = DEFAULT_GUIDANCE,
    context: dict | None = None,
    binary_threshold: int = DEFAULT_THRESHOLD,
    # Accepted but ignored — kept for API compatibility with the Book Visualizer
    # worker which passes preprocessing_size from the layer record.
    preprocessing_size: list | None = None,
) -> dict:
    """Run FLUX.1-Kontext-dev on a preprocessing PNG and produce engraving PNGs.

    Steps
    -----
    1. Load the project engraving prompt; expand ``{variable}`` placeholders
       from *context* (label, field, etc.) using safe substitution.
    2. Composite the preprocessing PNG (RGBA) onto white → RGB reference image.
    3. Run FLUX.1-Kontext-dev → write ``{engraving_id}_raw.png``.
    4. Apply binary threshold → write ``{engraving_id}_output.png``
       (strict B&W: black = line, white = paper, no greys).
    5. Write ``{engraving_id}_generation.json`` with full provenance.

    Returns
    -------
    dict with keys:
        raw_png    — path to the FLUX output PNG
        output_png — path to the strict B&W PNG
        metadata   — provenance dict (also written as a sidecar JSON)
    """
    import os
    os.environ.setdefault("PYTORCH_CUDA_ALLOC_CONF", "expandable_segments:True")

    import torch
    from PIL import Image

    # Force CUDA + NVML fully initialised before any model loading
    torch.cuda.init()
    torch.cuda.mem_get_info()

    from services.engraving_prompt import load_engraving_prompt
    prompt_filename, prompt_text = load_engraving_prompt(project_path)

    # Expand any {variable} placeholders from the silhouette context.
    # _SafeDict leaves unknown placeholders unchanged rather than raising.
    if context:
        prompt_text = prompt_text.format_map(_SafeDict(**(context or {})))

    model_dir = Path(project_path) / "models" / MODEL_NAME
    cache_dir = Path(cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)

    raw_png_path    = cache_dir / f"{engraving_id}_raw.png"
    output_png_path = cache_dir / f"{engraving_id}_output.png"

    # Composite RGBA preprocessing PNG onto white — FLUX expects RGB.
    src = Image.open(preprocessing_path).convert("RGBA")
    tw, th = src.size
    bg = Image.new("RGB", (tw, th), (255, 255, 255))
    bg.paste(src, mask=src.split()[3])
    reference_image = bg

    from diffusers import FluxKontextPipeline

    pipe = FluxKontextPipeline.from_pretrained(
        str(model_dir),
        torch_dtype=torch.bfloat16,
        local_files_only=True,
    )
    # enable_model_cpu_offload() moves components to GPU one at a time,
    # avoiding the large single allocation that triggers the NVML assert
    # on Blackwell + PyTorch 2.12.
    pipe.enable_model_cpu_offload()

    generator = torch.Generator(device="cuda").manual_seed(seed)
    result = pipe(
        image=reference_image,
        prompt=prompt_text,
        num_inference_steps=num_inference_steps,
        guidance_scale=guidance_scale,
        width=tw,
        height=th,
        generator=generator,
    )
    raw_image: Image.Image = result.images[0]

    # Save the raw FLUX output first (full colour, unprocessed)
    raw_image.save(str(raw_png_path))

    del pipe
    torch.cuda.empty_cache()

    # ------------------------------------------------------------------
    # Binary conversion: strict B&W (black = line, white = paper, no greys)
    # ------------------------------------------------------------------
    grey = raw_image.convert("L")
    bw   = grey.point(lambda p: 0 if p < binary_threshold else 255)
    bw.convert("RGB").save(str(output_png_path))

    meta = {
        "generator":           GENERATOR_TAG,
        "model":               MODEL_NAME,
        "seed":                seed,
        "num_inference_steps": num_inference_steps,
        "guidance_scale":      guidance_scale,
        "binary_threshold":    binary_threshold,
        "prompt_filename":     prompt_filename,
        "prompt":              prompt_text,
        "context":             context or {},
        "preprocessing_path":  str(preprocessing_path),
        "raw_png":             str(raw_png_path),
        "output_png":          str(output_png_path),
    }
    (cache_dir / f"{engraving_id}_generation.json").write_text(
        json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8"
    )

    return {
        "raw_png":    str(raw_png_path),
        "output_png": str(output_png_path),
        "metadata":   meta,
    }
