"""FLUX.1-Kontext-dev engraving smoke test generator.

Research benchmark — NOT integrated into the Book Visualizer.

Pipeline:

    preprocessing PNG (RGBA)
        ↓  passed directly as the reference image (no scribble conversion)
    FLUX.1-Kontext-dev  (image-editing model)
        ↓  conditioned on the engraving prompt
    raw_png   (RGB output)

No binary threshold pass — evaluating image quality first.

Comparison target: services/engraving_generate.py (SDXL + ControlNet Scribble)
"""

from __future__ import annotations

import json
from pathlib import Path

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

FLUX_MODEL_NAME  = "FLUX.1-Kontext-dev"
GENERATOR_TAG    = "flux_kontext_smoke_test"

DEFAULT_SEED     = 42
DEFAULT_STEPS    = 28       # recommended for Kontext-dev
DEFAULT_GUIDANCE = 2.5      # Kontext uses lower CFG than SDXL


# ---------------------------------------------------------------------------
# Model validation
# ---------------------------------------------------------------------------

def validate_flux_model(project_path: str) -> None:
    """Raise RuntimeError if the FLUX Kontext model directory is missing."""
    model_dir = Path(project_path) / "models" / FLUX_MODEL_NAME
    if not model_dir.is_dir():
        raise RuntimeError(
            f"FLUX model not found:\n  {model_dir}\n\n"
            f"Run:  crossing tool model download black-forest-labs/FLUX.1-Kontext-dev"
        )


# ---------------------------------------------------------------------------
# Core generation
# ---------------------------------------------------------------------------

def generate_engraving_flux(
    *,
    project_path: str,
    preprocessing_path: str,
    engraving_id: str,
    cache_dir: Path,
    seed: int = DEFAULT_SEED,
    num_inference_steps: int = DEFAULT_STEPS,
    guidance_scale: float = DEFAULT_GUIDANCE,
) -> dict:
    """Run FLUX.1-Kontext-dev on a preprocessing PNG and produce a raw output PNG.

    The preprocessing PNG is passed directly as the reference image — no
    scribble conversion, no alpha stripping.  The full RGBA content is
    composited onto white before passing to FLUX (which expects RGB).

    Returns
    -------
    dict with keys:
        raw_png   — path to the output PNG
        metadata  — provenance dict (also written as a sidecar JSON)
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

    model_dir = Path(project_path) / "models" / FLUX_MODEL_NAME
    cache_dir = Path(cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)

    raw_png_path = cache_dir / f"{engraving_id}_flux_raw.png"

    # ------------------------------------------------------------------
    # Prepare reference image
    # Composite RGBA preprocessing PNG onto white — FLUX expects RGB.
    # ------------------------------------------------------------------
    src = Image.open(preprocessing_path).convert("RGBA")
    tw, th = src.size
    bg = Image.new("RGB", (tw, th), (255, 255, 255))
    bg.paste(src, mask=src.split()[3])   # alpha channel as mask
    reference_image = bg

    # ------------------------------------------------------------------
    # Load FLUX Kontext pipeline
    # ------------------------------------------------------------------
    from diffusers import FluxKontextPipeline

    pipe = FluxKontextPipeline.from_pretrained(
        str(model_dir),
        torch_dtype=torch.bfloat16,
        local_files_only=True,
    )
    # Use CPU offload instead of pipe.to("cuda").
    # FLUX is ~24 GB in bfloat16; a single .to("cuda") allocates all tensors
    # at once which triggers the NVML lazy-init assert on Blackwell+PyTorch 2.12.
    # enable_model_cpu_offload() moves components to GPU one at a time via
    # accelerate hooks, avoiding the massive single allocation.
    pipe.enable_model_cpu_offload()

    # ------------------------------------------------------------------
    # Run generation
    # ------------------------------------------------------------------
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

    # ------------------------------------------------------------------
    # Save output
    # ------------------------------------------------------------------
    raw_image.save(str(raw_png_path))

    # ------------------------------------------------------------------
    # Unload to free VRAM
    # ------------------------------------------------------------------
    del pipe
    torch.cuda.empty_cache()

    # ------------------------------------------------------------------
    # Sidecar JSON
    # ------------------------------------------------------------------
    meta = {
        "generator":           GENERATOR_TAG,
        "model":               FLUX_MODEL_NAME,
        "seed":                seed,
        "num_inference_steps": num_inference_steps,
        "guidance_scale":      guidance_scale,
        "prompt_filename":     prompt_filename,
        "prompt":              prompt_text,
        "preprocessing_path":  str(preprocessing_path),
        "raw_png":             str(raw_png_path),
    }
    (cache_dir / f"{engraving_id}_flux_generation.json").write_text(
        json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8"
    )

    return {
        "raw_png":  str(raw_png_path),
        "metadata": meta,
    }
