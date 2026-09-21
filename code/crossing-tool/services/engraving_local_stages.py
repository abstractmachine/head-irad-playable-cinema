"""Repair and engrave stages for the local engraving converter.

Stage 2 (``repair``) reconstructs one coherent isolated object from the
silhouette plus its supporting frames.  Stage 3 (``engrave``) restyles that
object as catalogue linework.  Both run on the same
:class:`~services.engraving_local_model.LocalEngravingModel`, but they are
driven by independent prompts and independent sampling settings so either can
be tuned or skipped without touching the other.
"""

from __future__ import annotations

from pathlib import Path

from services.engraving_local_prompt import build_prompt


def _flatten_to_background(image, level: int):
    """Composite any transparency onto a uniform near-white field."""
    from PIL import Image

    if "A" not in image.getbands():
        return image.convert("RGB")
    background = Image.new("RGB", image.size, (level, level, level))
    background.paste(image.convert("RGBA"), mask=image.convert("RGBA").split()[3])
    return background


def _load_reference(path: str | Path, level: int):
    from PIL import Image

    with Image.open(path) as opened:
        return _flatten_to_background(opened, level)


def run_repair(
    *,
    model,
    evidence: dict,
    config: dict,
    project_path: str | Path,
    seed: int,
) -> dict:
    """Reconstruct one coherent isolated object from the assembled evidence.

    The silhouette is always the first reference image; the exact extraction
    frame and at most one same-shot context view follow, in that order of
    authority.
    """
    repair_cfg = config["repair"]
    level = int(config["evidence"]["background_level"])

    images = [evidence["subject"]]
    if evidence.get("source_frame"):
        images.append(_load_reference(evidence["source_frame"], level))
    for frame in evidence.get("context_frames", []):
        images.append(_load_reference(frame, level))

    prompt = build_prompt(project_path, "repair", evidence)
    working = int(config["evidence"]["working_size"])
    result = model.edit(
        images=images,
        prompt=prompt["prompt"],
        negative_prompt=repair_cfg["negative_prompt"],
        num_inference_steps=repair_cfg["num_inference_steps"],
        true_cfg_scale=repair_cfg["true_cfg_scale"],
        guidance_scale=repair_cfg["guidance_scale"],
        seed=seed,
        height=working,
        width=working,
    )

    return {
        "stage": "repair",
        "image": result["image"],
        "seconds": result["seconds"],
        "peak_vram_bytes": result["peak_vram_bytes"],
        "reference_count": len(images),
        "references": {
            "silhouette": evidence.get("silhouette_png"),
            "source_frame": evidence.get("source_frame"),
            "context_frames": list(evidence.get("context_frames", [])),
        },
        "settings": {
            "num_inference_steps": repair_cfg["num_inference_steps"],
            "true_cfg_scale": repair_cfg["true_cfg_scale"],
            "guidance_scale": repair_cfg["guidance_scale"],
            "negative_prompt": repair_cfg["negative_prompt"],
            "seed": seed,
        },
        "prompt": prompt,
    }


def run_engrave(
    *,
    model,
    evidence: dict,
    source_image,
    config: dict,
    project_path: str | Path,
    seed: int,
) -> dict:
    """Restyle *source_image* as a catalogue engraving on uniform near-white."""
    engrave_cfg = config["engrave"]
    level = int(config["evidence"]["background_level"])

    prompt = build_prompt(project_path, "engrave", evidence)
    flattened = _flatten_to_background(source_image, level)
    result = model.edit(
        images=[flattened],
        prompt=prompt["prompt"],
        negative_prompt=engrave_cfg["negative_prompt"],
        num_inference_steps=engrave_cfg["num_inference_steps"],
        true_cfg_scale=engrave_cfg["true_cfg_scale"],
        guidance_scale=engrave_cfg["guidance_scale"],
        seed=seed,
        height=flattened.height,
        width=flattened.width,
    )

    image = result["image"]
    threshold = engrave_cfg.get("binary_threshold")
    if threshold is not None:
        grey = image.convert("L")
        image = grey.point(lambda p: 0 if p < int(threshold) else 255).convert("RGB")

    return {
        "stage": "engrave",
        "image": image,
        "seconds": result["seconds"],
        "peak_vram_bytes": result["peak_vram_bytes"],
        "settings": {
            "num_inference_steps": engrave_cfg["num_inference_steps"],
            "true_cfg_scale": engrave_cfg["true_cfg_scale"],
            "guidance_scale": engrave_cfg["guidance_scale"],
            "negative_prompt": engrave_cfg["negative_prompt"],
            "binary_threshold": threshold,
            "seed": seed,
        },
        "prompt": prompt,
    }
