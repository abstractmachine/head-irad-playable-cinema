"""Configuration for the local (offline) engraving converter.

The converter replaces the OpenAI image pipeline with a fully local
Qwen-Image-Edit stack.  Every tunable lives here as a plain dict so a run
record can embed the exact resolved configuration verbatim.

Layering, lowest priority first:

1. :data:`DEFAULT_CONFIG`
2. a JSON config file passed via ``--config``
3. explicit keyword overrides from the CLI

``resolve_config`` performs the merge and validates the result.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

CONFIG_SCHEMA_VERSION = "1"

# Local directory name under <project>/models/ — matches the existing
# services/models.py convention of one directory per model.
MODEL_DIR_NAME = "Qwen-Image-Edit-2511"

# Upstream sources, recorded in every run record for provenance.
MODEL_SOURCES = {
    "pipeline_repo": "Qwen/Qwen-Image-Edit-2511",
    "pipeline_license": "apache-2.0",
    "transformer_gguf_repo": "unsloth/Qwen-Image-Edit-2511-GGUF",
    "transformer_gguf_license": "apache-2.0",
    "gguf_filenames": {
        "q8": "qwen-image-edit-2511-Q8_0.gguf",
        "q6": "qwen-image-edit-2511-Q6_K.gguf",
        "q5": "qwen-image-edit-2511-Q5_K_M.gguf",
        "q4": "qwen-image-edit-2511-Q4_K_M.gguf",
    },
}

# Backends for the 20B transformer, ordered by decreasing VRAM footprint.
TRANSFORMER_BACKENDS = ("bf16", "q8", "q6", "q5", "q4")

DEFAULT_CONFIG: dict = {
    "schema_version": CONFIG_SCHEMA_VERSION,
    "model": {
        "dir_name": MODEL_DIR_NAME,
        # Quantisation backend for the transformer.  "q8" keeps the whole
        # transformer resident on a 32 GB card with room for the text encoder.
        "transformer_backend": "q8",
        # Stage components onto the GPU one at a time.  Required on a 32 GB
        # card: a Q8 transformer (~21.8 GB) plus the bf16 Qwen2.5-VL text
        # encoder (~15 GB) does not fit resident simultaneously.
        "cpu_offload": True,
        # Reuse encoded prompt embeddings across a batch.  The repair and
        # engrave prompts are near-identical between objects, so this removes
        # most text-encoder work from a batch run.
        "cache_prompt_embeds": True,
    },
    "evidence": {
        # Longest edge of the square working canvas handed to the model.
        "working_size": 1024,
        # Include the exact source frame as a second reference image.
        "use_source_frame": True,
        # Maximum number of auxiliary context frames (0-2).  These may clarify
        # topology and construction; they may not donate ornament or damage.
        "max_context_frames": 1,
        # Near-white value used for the working background.
        "background_level": 248,
        # Pad the silhouette crop by this fraction so the model has room to
        # complete occluded structure beyond the mask boundary.
        "crop_padding_fraction": 0.12,
    },
    "repair": {
        "enabled": True,
        "num_inference_steps": 40,
        # Above the model card's default of 4.0: measured on the benchmark bird,
        # a higher value holds source-specific detail (banded tail, plumage
        # tone, rear three-quarter pose) that 4.0 smoothed into a generic
        # example of the subject.
        "true_cfg_scale": 7.0,
        "guidance_scale": 1.0,
        "negative_prompt": (
            "scenery, background, ground plane, cast shadow, perch, stand, base, "
            "mount, pedestal, support, caption, lettering, text, label, border, "
            "frame, signature, watermark, checkerboard, second object, duplicate, "
            "extra limbs, extra parts, collage, cropped"
        ),
    },
    "engrave": {
        "enabled": True,
        "num_inference_steps": 40,
        "true_cfg_scale": 4.0,
        "guidance_scale": 1.0,
        "negative_prompt": (
            "photograph, colour, color, greyscale photo, painterly wash, airbrush, "
            "soft shading, blur, gradient background, uniform halftone screen, "
            "hairline-only texture, cast shadow, scenery, ground plane, perch, stand, "
            "base, mount, pedestal, support, caption, lettering, text, border, "
            "signature, watermark, checkerboard"
        ),
        # Binary threshold applied to produce the strict black/white engraving.
        # ``None`` keeps the model's continuous tone, which preserves hatching
        # better than a hard threshold; alpha extraction handles the background.
        "binary_threshold": None,
    },
    "alpha": {
        "white_point": 242,
        "padding_fraction": 0.03,
        "seal_size": 5,
    },
    "validate": {
        # Fraction of the final crop that must be ink for the result to read as
        # an engraving rather than a near-empty page.
        "min_ink_fraction": 0.02,
        "max_ink_fraction": 0.85,
        # Simulated print check: longest edge in millimetres at 300 DPI.
        "print_min_mm": 35.0,
        "print_max_mm": 80.0,
        "print_dpi": 300,
        # Minimum surviving ink fraction after downscaling to print size.
        "min_print_ink_fraction": 0.01,
        # Largest allowed count of disconnected ink components before the
        # result is flagged as fragmented / non-isolated.
        "max_ink_components": 12,
        # Evidence signals that force a manual-review flag.
        "review_on_occlusion": ("heavy",),
        "review_on_completeness": ("cropped", "partial"),
        "review_on_fragment_count": 3,
    },
    "run": {
        "seed": 42,
        # Never overwrite an existing completed run unless forced.
        "force": False,
        # Keep intermediate stage PNGs for inspection.
        "keep_intermediates": True,
    },
}


class EngravingConfigError(ValueError):
    """Raised when a supplied configuration is structurally invalid."""


def _deep_merge(base: dict, overlay: dict) -> dict:
    """Return *base* recursively updated with *overlay* (neither is mutated)."""
    result = copy.deepcopy(base)
    for key, value in (overlay or {}).items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def load_config_file(config_path: str | Path) -> dict:
    """Read a JSON config overlay from disk."""
    config_path = Path(config_path)
    if not config_path.is_file():
        raise EngravingConfigError(f"Config file not found: {config_path}")
    try:
        data = json.loads(config_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise EngravingConfigError(f"Config file is not valid JSON: {config_path}\n  {exc}") from exc
    if not isinstance(data, dict):
        raise EngravingConfigError(f"Config file must contain a JSON object: {config_path}")
    return data


def resolve_config(
    config_path: str | Path | None = None,
    overrides: dict | None = None,
) -> dict:
    """Return the fully merged, validated configuration dict."""
    config = copy.deepcopy(DEFAULT_CONFIG)
    if config_path:
        config = _deep_merge(config, load_config_file(config_path))
    if overrides:
        config = _deep_merge(config, overrides)
    validate_config(config)
    return config


def validate_config(config: dict) -> None:
    """Raise :class:`EngravingConfigError` if *config* is structurally invalid."""
    backend = config.get("model", {}).get("transformer_backend")
    if backend not in TRANSFORMER_BACKENDS:
        raise EngravingConfigError(
            f"Unknown transformer_backend {backend!r}. "
            f"Valid backends: {', '.join(TRANSFORMER_BACKENDS)}"
        )

    working_size = config.get("evidence", {}).get("working_size")
    if not isinstance(working_size, int) or working_size < 256:
        raise EngravingConfigError("evidence.working_size must be an integer >= 256")
    if working_size % 16:
        raise EngravingConfigError("evidence.working_size must be a multiple of 16")

    context_frames = config.get("evidence", {}).get("max_context_frames")
    if not isinstance(context_frames, int) or not 0 <= context_frames <= 2:
        raise EngravingConfigError("evidence.max_context_frames must be 0, 1 or 2")

    alpha = config.get("alpha", {})
    if not 1 <= int(alpha.get("white_point", 0)) <= 255:
        raise EngravingConfigError("alpha.white_point must be between 1 and 255")
    if not 0.0 <= float(alpha.get("padding_fraction", -1)) <= 1.0:
        raise EngravingConfigError("alpha.padding_fraction must be between 0 and 1")

    if not (config.get("repair", {}).get("enabled") or config.get("engrave", {}).get("enabled")):
        raise EngravingConfigError("At least one of repair.enabled or engrave.enabled must be true")


def model_dir(project_path: str | Path, config: dict | None = None) -> Path:
    """Return ``<project>/models/<dir_name>`` for the resolved config."""
    name = (config or DEFAULT_CONFIG)["model"]["dir_name"]
    return Path(project_path) / "models" / name


def gguf_filename(backend: str) -> str | None:
    """Return the GGUF filename for a quantised *backend*, or ``None`` for bf16."""
    return MODEL_SOURCES["gguf_filenames"].get(backend)
