"""FLUX.1-Kontext-dev engraving generator.

Pipeline:

    preprocessing PNG (RGBA)
        ↓  composited onto white, passed as reference image
    FLUX.1-Kontext-dev
        ↓  conditioned on the project engraving prompt (template expanded)
    raw_png   (RGB FLUX output, saved first)
        ↓  binary threshold (strict B&W)
    output_png  (black = line, white = paper, no greys)

The preprocessing PNG is used directly — no scribble conversion, no mask.
Prompt is loaded from <project>/prompts/engravings/<latest>.txt as a
``string.Template``; ``$variable`` placeholders are expanded from the
*context* dict (e.g. ``$label``, ``$field``, ``$movie``, ``$shot_id``,
``$description``).  Unknown placeholders are left unchanged (safe_substitute).
"""

from __future__ import annotations

import json
import string
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
# Prompt template expansion
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Prompt template expansion
# ---------------------------------------------------------------------------

# Size classes and their corresponding line-weight hints, keyed by the
# longest on-page dimension in millimetres.
_SIZE_CLASS_THRESHOLDS: list[tuple[float, str, str]] = [
    # (min_mm, size_class, line_weight_hint)
    (80.0,  "large",       "heavy — use bold 2–3 pt contours, coarse hatching"),
    (40.0,  "medium",      "medium — use 1–2 pt contours, moderate hatching"),
    (20.0,  "small",       "fine — use 0.75–1 pt contours, delicate hatching"),
    ( 0.0,  "very_small",  "minimal — use 0.5 pt contours, very sparse hatching"),
]

_PT_PER_MM = 2.8346   # 1 mm = 72 / 25.4 pt


def build_size_context(
    preprocessing_size: list | None,
    preprocess_dpi: int | float,
    page_pt_w: float,
    page_pt_h: float,
    width_frac: float,
    height_frac: float,
) -> dict:
    """Return a dict of size-aware prompt variables derived from placement metadata.

    Parameters
    ----------
    preprocessing_size : [w_px, h_px] from the preprocessing sidecar.
    preprocess_dpi     : DPI used to compute the preprocessing canvas.
    page_pt_w          : PDF page width in points.
    page_pt_h          : PDF page height in points.
    width_frac         : Normalised object width fraction [0, 1].
    height_frac        : Normalised object height fraction [0, 1].

    All parameters are safe-fallback: missing or zero values yield empty strings.
    """
    try:
        dpi = float(preprocess_dpi) if preprocess_dpi else 0.0
        if not dpi:
            dpi = 300.0

        # On-page physical size (mm)
        obj_w_mm = (width_frac  * page_pt_w / _PT_PER_MM) if (page_pt_w and width_frac)  else 0.0
        obj_h_mm = (height_frac * page_pt_h / _PT_PER_MM) if (page_pt_h and height_frac) else 0.0

        page_w_mm = (page_pt_w / _PT_PER_MM) if page_pt_w else 0.0
        page_h_mm = (page_pt_h / _PT_PER_MM) if page_pt_h else 0.0

        # Pixel dimensions from preprocessing sidecar (most accurate source)
        obj_w_px = int(preprocessing_size[0]) if (preprocessing_size and len(preprocessing_size) >= 2) else 0
        obj_h_px = int(preprocessing_size[1]) if (preprocessing_size and len(preprocessing_size) >= 2) else 0

        # Size class from longest on-page dimension
        longest_mm = max(obj_w_mm, obj_h_mm)
        size_class = "unknown"
        line_weight_hint = ""
        for threshold, sc, hint in _SIZE_CLASS_THRESHOLDS:
            if longest_mm >= threshold:
                size_class = sc
                line_weight_hint = hint
                break

        def _fmt_mm(v: float) -> str:
            return f"{v:.1f}" if v else ""

        def _fmt_px(v: int) -> str:
            return str(v) if v else ""

        return {
            "page_dpi":         str(int(round(dpi))) if dpi else "",
            "page_width_mm":    _fmt_mm(page_w_mm),
            "page_height_mm":   _fmt_mm(page_h_mm),
            "object_width_mm":  _fmt_mm(obj_w_mm),
            "object_height_mm": _fmt_mm(obj_h_mm),
            "object_width_px":  _fmt_px(obj_w_px),
            "object_height_px": _fmt_px(obj_h_px),
            "size_class":       size_class,
            "line_weight_hint": line_weight_hint,
        }
    except Exception:
        return {
            "page_dpi":         "",
            "page_width_mm":    "",
            "page_height_mm":   "",
            "object_width_mm":  "",
            "object_height_mm": "",
            "object_width_px":  "",
            "object_height_px": "",
            "size_class":       "",
            "line_weight_hint": "",
        }


def _expand_prompt(template_text: str, context: dict | None) -> str:
    """Expand ``$variable`` placeholders in *template_text* using *context*.

    Uses ``string.Template.safe_substitute`` so that unknown placeholders
    (variables not present in *context*) are left literally unchanged rather
    than raising.  The five canonical variables default to an empty string so
    they render as blank lines rather than un-expanded ``$label`` literals.
    """
    defaults = {
        "label":            "",
        "field":            "",
        "movie":            "",
        "shot_id":          "",
        "description":      "",
        # Size-aware variables (v4+) — available for display/metadata but not injected
        "page_dpi":         "",
        "page_width_mm":    "",
        "page_height_mm":   "",
        "object_width_mm":  "",
        "object_height_mm": "",
        "object_width_px":  "",
        "object_height_px": "",
        "size_class":       "",
        # User-controlled generation parameter
        "line_weight":      "1.0",
    }
    # Merge context but explicitly exclude line_weight_hint — it must never
    # appear in the compiled prompt sent to FLUX (kept in metadata only).
    _PROMPT_EXCLUDED = {"line_weight_hint"}
    if context:
        defaults.update(
            {k: str(v) for k, v in context.items()
             if v is not None and k not in _PROMPT_EXCLUDED}
        )
    return string.Template(template_text).safe_substitute(defaults)


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
    1. Load the latest engraving prompt template; expand ``$variable``
       placeholders from *context* (label, field, movie, shot_id, description)
       using ``string.Template.safe_substitute`` — missing variables default
       to empty strings, unknown placeholders are left unchanged.
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
    prompt_filename, prompt_template = load_engraving_prompt(project_path)
    prompt_text = _expand_prompt(prompt_template, context)

    print(f"\n[engraving] prompt file : {prompt_filename}")
    print(f"[engraving] context     : {context}")
    print(f"[engraving] compiled prompt:\n{'-' * 60}\n{prompt_text}\n{'-' * 60}\n")

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
        "line_weight":         float((context or {}).get("line_weight", 1.0)),
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
