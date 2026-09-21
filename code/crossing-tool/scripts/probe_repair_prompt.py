"""Throwaway A/B probe for the repair-stage prompt. Not part of the pipeline.

Loads the model once and renders several repair variants for one silhouette so
the reconstruction behaviour can be compared side by side.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

OUT = Path("/tmp/repair_probe")
SRC = (
    "/home/cowpoke/playable/dead-crossing/data/silhouettes/catalog/movie/"
    "Companeros (1970) {tmdb-61044}/bird/object_0002.json"
)
PROJECT = "/home/cowpoke/playable/dead-crossing"

NEGATIVE = (
    "scenery, background, ground plane, cast shadow, perch, stand, base, mount, "
    "pedestal, support, caption, lettering, text, label, border, frame, signature, "
    "watermark, checkerboard, second object, duplicate, extra limbs, collage, cropped"
)

VARIANTS = [
    (
        "A_current_cfg4",
        "Complete this damaged cut-out into one whole, undamaged bird, shown in full and "
        "isolated on a plain white background.\n\nFill in every part hidden or cut away by "
        "the mask: restore the missing head, limbs, ends and edges so the object reads as "
        "complete. Keep the pose, viewing angle, proportions and markings exactly as shown.",
        4.0,
        True,
    ),
    (
        "B_redraw_whole_cfg4",
        "This cut-out is incomplete: parts of the bird were hidden or cut off by the mask.\n\n"
        "Redraw it as one complete bird, whole and unobstructed, standing on plain white. "
        "The head, beak, eye, both wings, both legs and both feet must all be clearly "
        "visible. Keep the same species, plumage pattern, body angle and proportions as the "
        "cut-out. Show only the bird, with nothing beneath it.",
        4.0,
        True,
    ),
    (
        "C_redraw_whole_cfg7",
        "This cut-out is incomplete: parts of the bird were hidden or cut off by the mask.\n\n"
        "Redraw it as one complete bird, whole and unobstructed, standing on plain white. "
        "The head, beak, eye, both wings, both legs and both feet must all be clearly "
        "visible. Keep the same species, plumage pattern, body angle and proportions as the "
        "cut-out. Show only the bird, with nothing beneath it.",
        7.0,
        True,
    ),
    (
        "D_redraw_whole_cfg4_noframe",
        "This cut-out is incomplete: parts of the bird were hidden or cut off by the mask.\n\n"
        "Redraw it as one complete bird, whole and unobstructed, standing on plain white. "
        "The head, beak, eye, both wings, both legs and both feet must all be clearly "
        "visible. Keep the same species, plumage pattern, body angle and proportions as the "
        "cut-out. Show only the bird, with nothing beneath it.",
        4.0,
        False,
    ),
]


def main() -> None:
    from PIL import Image

    from services.engraving_local_config import resolve_config
    from services.engraving_local_evidence import assemble_evidence
    from services.engraving_local_model import LocalEngravingModel
    from services.engraving_local_stages import _flatten_to_background

    OUT.mkdir(parents=True, exist_ok=True)
    config = resolve_config()
    evidence = assemble_evidence(SRC, config)
    evidence["subject"].save(OUT / "00_input.png")

    frame = None
    if evidence["source_frame"]:
        frame = _flatten_to_background(
            Image.open(evidence["source_frame"]), config["evidence"]["background_level"]
        )

    model = LocalEngravingModel(PROJECT, config)
    try:
        for name, prompt, cfg, use_frame in VARIANTS:
            images = [evidence["subject"]]
            if use_frame and frame is not None:
                images.append(frame)
            started = time.time()
            result = model.edit(
                images=images,
                prompt=prompt,
                negative_prompt=NEGATIVE,
                num_inference_steps=30,
                true_cfg_scale=cfg,
                guidance_scale=1.0,
                seed=42,
                height=1024,
                width=1024,
            )
            result["image"].save(OUT / f"{name}.png")
            print(f"{name}: {time.time() - started:.0f}s -> {OUT / f'{name}.png'}", flush=True)
    finally:
        model.release()


if __name__ == "__main__":
    main()
