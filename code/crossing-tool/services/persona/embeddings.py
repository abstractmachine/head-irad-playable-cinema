"""Torchreid-based person appearance embedding extractor.

Uses OSNet x1.0 (omni-scale network) as the re-ID backbone.
Weights are auto-downloaded on first use (~11 MB from Google Drive).

The extractor is loaded lazily: the model is only instantiated when
``extract_embedding()`` is first called.  This keeps import time fast.
"""

from __future__ import annotations

import warnings
from typing import Optional

import numpy as np

_MODEL = None
_TRANSFORM = None
_DEVICE: Optional[str] = None

# OSNet x1.0 produces 512-d embeddings.
EMBED_DIM = 512
_MODEL_NAME = "osnet_x1_0"


def _get_model():
    """Lazily load the torchreid OSNet model on first call.

    Returns (model, transform, device).

    Raises:
        ImportError: if torchreid or required deps are not installed.
    """
    global _MODEL, _TRANSFORM, _DEVICE

    if _MODEL is not None:
        return _MODEL, _TRANSFORM, _DEVICE

    try:
        with warnings.catch_warnings():
            warnings.filterwarnings("ignore", category=UserWarning, module="torchreid")
            import torchreid  # noqa: F401 — import triggers model factory registration
    except ImportError as exc:
        raise ImportError(
            "torchreid is required for persona embedding. Install with:\n"
            "  pip install torchreid gdown tensorboard"
        ) from exc

    import torch
    from torchvision import transforms as T

    _DEVICE = "cuda" if torch.cuda.is_available() else "cpu"

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        model = torchreid.models.build_model(
            name=_MODEL_NAME,
            num_classes=1000,  # value is irrelevant for feature extraction
            pretrained=True,
        )

    model.eval()
    model = model.to(_DEVICE)
    _MODEL = model

    # Standard re-ID preprocessing (mean/std from ImageNet / Market-1501 practice)
    _TRANSFORM = T.Compose([
        T.Resize((256, 128)),
        T.ToTensor(),
        T.Normalize(mean=[0.485, 0.456, 0.406], std=[0.229, 0.224, 0.225]),
    ])

    return _MODEL, _TRANSFORM, _DEVICE


def warmup() -> None:
    """Pre-load the OSNet model so inference calls don't block.

    Call this once before running bulk extraction.  Any ImportError or
    download failure is raised immediately rather than being silently
    swallowed per crop.
    """
    _get_model()


def extract_embedding(crop_bgr: np.ndarray) -> Optional[tuple]:
    """Extract a normalised re-ID embedding from a person crop.

    Args:
        crop_bgr: Person bounding-box crop in BGR format (as returned by
            OpenCV), shape (H, W, 3).  Minimum useful size is ~32×16.

    Returns:
        float32 numpy array of shape (EMBED_DIM,) with L2 norm ≈ 1.0,
        or None if the crop is too small or an error occurs.
    """
    if crop_bgr is None or crop_bgr.size == 0:
        return None

    h, w = crop_bgr.shape[:2]
    if h < 16 or w < 8:
        return None

    # Model must already be loaded by warmup(); re-raise any loading error
    # so it surfaces rather than being silently swallowed.
    model, transform, device = _get_model()

    try:
        import torch
        from PIL import Image

        # OpenCV returns BGR; convert to RGB for PIL / torchvision
        crop_rgb = crop_bgr[:, :, ::-1].copy()
        pil_img = Image.fromarray(crop_rgb)

        tensor = transform(pil_img).unsqueeze(0).to(device)

        with torch.no_grad():
            feat = model(tensor)

        emb = feat.squeeze().cpu().numpy().astype(np.float32)
        norm = float(np.linalg.norm(emb))

        if norm < 1e-6:
            return None

        return emb / norm, norm  # return (normalised_embedding, original_norm)

    except Exception:  # inference error on a single crop — skip it
        return None


def model_name() -> str:
    return _MODEL_NAME
