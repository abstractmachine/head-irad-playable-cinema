"""Frame-vector service: embed a single RGB frame using a CLIP vision encoder.

This is a thin wrapper around the existing frame_match infrastructure.
It provides lazy model loading and single-frame embedding for interactive
use in the Sync Visualizer.

Usage::

    from services.frame_vector import load_frame_vector_model, embed_rgb_frame

    bundle = load_frame_vector_model(project_path=".")
    vec = embed_rgb_frame(frame_rgb, bundle)   # np.ndarray shape (dim,)

The model is cached per (project_path, model_name) key so the visualizer
only pays the load cost once per session.
"""

from __future__ import annotations

from typing import Tuple
import numpy as np

_DEFAULT_MODEL = "clip-vit-base-patch32"

# Module-level cache: (project_path, model_name) → (model, processor, device, name)
_model_cache: dict[tuple, tuple] = {}


def load_frame_vector_model(
    project_path: str = ".",
    model_name: str | None = None,
) -> tuple:
    """Load (or return cached) the CLIP model bundle.

    Returns a 4-tuple ``(model, processor, device, model_name)`` suitable
    for passing to :func:`embed_rgb_frame`.
    """
    from services.frame_match import load_frame_embedding_model

    name = model_name or _DEFAULT_MODEL
    key  = (str(project_path), name)
    if key not in _model_cache:
        model, processor, device = load_frame_embedding_model(project_path, name)
        _model_cache[key] = (model, processor, device, name)
    return _model_cache[key]


def embed_rgb_frame(
    frame_rgb: "np.ndarray",
    model_bundle: tuple,
) -> "np.ndarray":
    """Embed a single RGB numpy frame using the CLIP image encoder.

    Args:
        frame_rgb:    HxWx3 uint8 numpy array in RGB order.
        model_bundle: 4-tuple returned by :func:`load_frame_vector_model`.

    Returns:
        1-D float32 numpy array of shape ``(dim,)``, L2-normalised.
    """
    from PIL import Image
    from services.frame_match import embed_frame_images

    model, processor, device, _name = model_bundle
    pil_img = Image.fromarray(frame_rgb)
    vectors = embed_frame_images([pil_img], model, processor, device)
    return vectors[0]  # shape (dim,)
