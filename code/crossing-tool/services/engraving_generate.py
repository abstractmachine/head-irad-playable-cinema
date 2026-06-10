"""SDXL + ControlNet-Scribble engraving generator  (v1).

Minimal working pipeline:

    preprocessing PNG (RGBA)
        ↓  alpha → binary RGB scribble conditioning image
    ControlNet-Scribble (controlnet-scribble-sdxl-1.0)
        ↓
    SDXL base (stable-diffusion-xl-base-1.0)
    raw_png   (RGB output)
        ↓  greyscale → threshold
    output_png  (strict binary B&W, black lines on white)

Prompt is loaded from the project's prompt directory at generation time:
    <project>/prompts/engravings/<latest>.txt

No LoRA, no textual-inversion.
"""

from __future__ import annotations

import json
from pathlib import Path

# ---------------------------------------------------------------------------
# Model name constants
# ---------------------------------------------------------------------------

SDXL_BASE_NAME    = "stable-diffusion-xl-base-1.0"
CONTROLNET_NAME   = "controlnet-scribble-sdxl-1.0"

GENERATOR_TAG     = "sdxl_controlnet_scribble_v1"
DEFAULT_SEED      = 42
DEFAULT_STEPS     = 30
DEFAULT_GUIDANCE  = 7.5
DEFAULT_CN_SCALE  = 0.85
DEFAULT_THRESHOLD = 128

NEGATIVE_PROMPT = (
    "photograph, photorealistic, color, colour, painting, watercolor, "
    "sketch, pencil, charcoal, 3d render, blurry, noise, grain"
)


# ---------------------------------------------------------------------------
# Model validation
# ---------------------------------------------------------------------------

def validate_models(project_path: str) -> None:
    """Raise RuntimeError listing any missing required model directories."""
    models_dir = Path(project_path) / "models"
    required = [SDXL_BASE_NAME, CONTROLNET_NAME]
    missing = [n for n in required if not (models_dir / n).is_dir()]
    if missing:
        lines = "\n".join(f"  {m}" for m in missing)
        raise RuntimeError(
            f"Missing engraving model(s):\n{lines}\n\n"
            f"Run:  crossing tool model download <name>"
        )


# ---------------------------------------------------------------------------
# Conditioning image preparation
# ---------------------------------------------------------------------------

def _make_scribble_conditioning(preprocessing_path: str, target_size: tuple[int, int]) -> "Image.Image":
    """Convert RGBA preprocessing asset to an RGB scribble conditioning image.

    The preprocessing asset has:
      - subject pixels:   non-zero alpha
      - background:       transparent (alpha=0)

    ControlNet-Scribble expects:
      - white background
      - dark subject outlines / filled areas

    We convert alpha → inverted mask: subject=black, background=white.
    This gives the ControlNet a clean shape signal.
    """
    from PIL import Image
    import numpy as np

    img = Image.open(preprocessing_path).convert("RGBA")
    if img.size != target_size:
        img = img.resize(target_size, Image.LANCZOS)

    arr = np.array(img)
    alpha = arr[:, :, 3]

    # Binary mask: subject pixels → black (0), background → white (255)
    scribble = np.where(alpha > 10, 0, 255).astype(np.uint8)
    return Image.fromarray(scribble).convert("RGB")


# ---------------------------------------------------------------------------
# Core generation
# ---------------------------------------------------------------------------

def generate_engraving(
    *,
    project_path: str,
    preprocessing_path: str,
    preprocessing_size: list[int],
    engraving_id: str,
    cache_dir: Path,
    seed: int = DEFAULT_SEED,
    num_inference_steps: int = DEFAULT_STEPS,
    guidance_scale: float = DEFAULT_GUIDANCE,
    controlnet_conditioning_scale: float = DEFAULT_CN_SCALE,
    binary_threshold: int = DEFAULT_THRESHOLD,
) -> dict:
    """Run SDXL + ControlNet-Scribble and produce a binary engraving PNG."""
    import torch
    from PIL import Image
    import numpy as np
    from diffusers import StableDiffusionXLControlNetPipeline, ControlNetModel

    # Load prompt from project prompt directory
    from services.engraving_prompt import load_engraving_prompt, EngravingPromptError
    prompt_filename, prompt_text = load_engraving_prompt(project_path)

    models_dir = Path(project_path) / "models"
    cache_dir  = Path(cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)

    raw_png_path    = cache_dir / f"{engraving_id}_raw.png"
    output_png_path = cache_dir / f"{engraving_id}_binary.png"

    tw, th = int(preprocessing_size[0]), int(preprocessing_size[1])

    # 1. Build scribble conditioning image
    conditioning_img = _make_scribble_conditioning(preprocessing_path, (tw, th))

    # 2. Load ControlNet
    controlnet = ControlNetModel.from_pretrained(
        str(models_dir / CONTROLNET_NAME),
        torch_dtype=torch.float16,
        local_files_only=True,
    )

    # 3. Load SDXL pipeline
    pipe = StableDiffusionXLControlNetPipeline.from_pretrained(
        str(models_dir / SDXL_BASE_NAME),
        controlnet=controlnet,
        torch_dtype=torch.float16,
        use_safetensors=True,
        local_files_only=True,
        variant="fp16",
    )
    pipe = pipe.to("cuda")
    # The SDXL VAE is numerically unstable in fp16.
    # upcast_vae() is deprecated; the new recommended call is pipe.vae.to(torch.float32),
    # but that only casts the *weights* — the pipeline still passes fp16 *latents* in,
    # causing a dtype mismatch in post_quant_conv.
    # Fix: cast the VAE weights to fp32 AND wrap vae.decode so latents are upcast first.
    pipe.vae.to(torch.float32)
    _orig_vae_decode = pipe.vae.decode
    def _fp32_decode(z, *a, **kw):
        return _orig_vae_decode(z.to(torch.float32), *a, **kw)
    pipe.vae.decode = _fp32_decode

    # 4. Run generation
    generator = torch.Generator(device="cuda").manual_seed(seed)
    result = pipe(
        prompt=prompt_text,
        negative_prompt=NEGATIVE_PROMPT,
        image=conditioning_img,
        num_inference_steps=num_inference_steps,
        guidance_scale=guidance_scale,
        controlnet_conditioning_scale=controlnet_conditioning_scale,
        width=tw,
        height=th,
        generator=generator,
    )
    raw_image: Image.Image = result.images[0]

    # 5. Save raw output
    raw_image.save(str(raw_png_path))

    # 6. Binary threshold — black = line, white = paper
    grey = raw_image.convert("L")
    arr  = np.array(grey)
    binary_arr = ((arr < binary_threshold) * 255).astype(np.uint8)
    Image.fromarray(binary_arr, mode="L").save(str(output_png_path))

    # 7. Unload to free VRAM
    del pipe, controlnet
    torch.cuda.empty_cache()

    # 8. Sidecar JSON
    meta = {
        "generator":                     GENERATOR_TAG,
        "base_model":                    SDXL_BASE_NAME,
        "controlnet":                    CONTROLNET_NAME,
        "seed":                          seed,
        "num_inference_steps":           num_inference_steps,
        "guidance_scale":                guidance_scale,
        "controlnet_conditioning_scale": controlnet_conditioning_scale,
        "prompt_filename":               prompt_filename,
        "prompt":                        prompt_text,
        "negative_prompt":               NEGATIVE_PROMPT,
        "threshold":                     binary_threshold,
        "preprocessing_path":            str(preprocessing_path),
        "raw_png":                       str(raw_png_path),
        "output_png":                    str(output_png_path),
    }
    (cache_dir / f"{engraving_id}_generation.json").write_text(
        json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8"
    )

    return {
        "raw_png":    str(raw_png_path),
        "output_png": str(output_png_path),
        "metadata":   meta,
    }
