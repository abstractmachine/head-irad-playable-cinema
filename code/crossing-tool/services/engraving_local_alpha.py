"""Deterministic exterior-alpha extraction for locally generated engravings.

Stage 4 of the local engraving pipeline.  Contains no model inference: given an
engraving rendered on a uniform near-white field, it removes *only* the
edge-connected near-white background, keeps enclosed white areas opaque, crops
to the surviving ink and adds transparent padding.

The algorithm is the one validated by the prior engraving review:

1. threshold ink at luminance ``< white_point``;
2. seal small contour gaps with a 5x5 maximum filter;
3. flood the near-white field inward from all four canvas edges;
4. turn only that edge-connected exterior transparent;
5. contract the temporary seal with a 5x5 minimum filter;
6. crop to surviving alpha and add proportional transparent padding.

Step 3 is implemented with a connected-component labelling pass rather than a
per-edge-pixel flood fill; the two are equivalent but labelling is O(pixels)
instead of O(perimeter x fill).
"""

from __future__ import annotations

from pathlib import Path

DEFAULT_WHITE_POINT = 242
DEFAULT_PADDING_FRACTION = 0.03
DEFAULT_SEAL_SIZE = 5

# Minimum transparent padding in pixels, regardless of subject size.
_MIN_PAD_PX = 8


class AlphaExtractionError(RuntimeError):
    """Raised when an engraving contains no usable ink."""


def white_to_alpha(
    image,
    *,
    white_point: int = DEFAULT_WHITE_POINT,
    padding_fraction: float = DEFAULT_PADDING_FRACTION,
    seal_size: int = DEFAULT_SEAL_SIZE,
):
    """Return an RGBA crop of *image* with only edge-connected near-white removed.

    Parameters
    ----------
    image:
        A PIL image of the engraving on a uniform near-white background.
    white_point:
        Luminance below which a pixel counts as ink.
    padding_fraction:
        Transparent padding added on every side, as a fraction of the longest
        cropped dimension.
    seal_size:
        Kernel size for the temporary contour seal.  Must be odd.

    Raises
    ------
    AlphaExtractionError
        If no ink survives thresholding.
    ValueError
        If *white_point*, *padding_fraction* or *seal_size* is out of range.
    """
    import numpy as np
    from PIL import Image, ImageFilter
    from scipy import ndimage

    if not 1 <= int(white_point) <= 255:
        raise ValueError("white_point must be between 1 and 255")
    if not 0.0 <= float(padding_fraction) <= 1.0:
        raise ValueError("padding_fraction must be between 0 and 1")
    if int(seal_size) < 1 or int(seal_size) % 2 == 0:
        raise ValueError("seal_size must be a positive odd integer")

    rgb = image.convert("RGB")
    luminance = rgb.convert("L")
    ink = luminance.point(lambda value: 255 if value < white_point else 0)

    # Temporarily thicken the contour so the exterior flood cannot leak through
    # single-pixel gaps into whites enclosed by the subject.
    sealed = ink.filter(ImageFilter.MaxFilter(seal_size)) if seal_size > 1 else ink

    background = np.asarray(sealed, dtype=np.uint8) == 0
    labels, count = ndimage.label(background)
    if count == 0:
        # Every pixel is ink; nothing to make transparent.
        alpha_arr = np.full(background.shape, 255, dtype=np.uint8)
    else:
        border_labels = set()
        border_labels.update(np.unique(labels[0, :]).tolist())
        border_labels.update(np.unique(labels[-1, :]).tolist())
        border_labels.update(np.unique(labels[:, 0]).tolist())
        border_labels.update(np.unique(labels[:, -1]).tolist())
        border_labels.discard(0)

        exterior = np.isin(labels, list(border_labels)) if border_labels else np.zeros_like(background)
        alpha_arr = np.where(exterior, 0, 255).astype(np.uint8)

    alpha = Image.fromarray(alpha_arr, mode="L")
    # Undo the temporary expansion of the contour barrier.
    if seal_size > 1:
        alpha = alpha.filter(ImageFilter.MinFilter(seal_size))

    bbox = alpha.getbbox()
    if bbox is None:
        raise AlphaExtractionError("No engraving ink remained after alpha extraction")

    alpha = alpha.crop(bbox)
    pad = max(_MIN_PAD_PX, round(max(alpha.size) * padding_fraction))
    output = Image.new("RGBA", (alpha.width + 2 * pad, alpha.height + 2 * pad), (0, 0, 0, 0))
    subject = rgb.crop(bbox).convert("RGBA")
    subject.putalpha(alpha)
    output.alpha_composite(subject, (pad, pad))
    return output


def alpha_report(image) -> dict:
    """Return validation facts about an RGBA engraving's alpha channel."""
    import numpy as np

    if image.mode != "RGBA":
        return {
            "mode": image.mode,
            "has_alpha": False,
            "corners_transparent": False,
            "alpha_extrema": None,
            "transparent_fraction": 0.0,
            "opaque_fraction": 0.0,
            "partial_fraction": 0.0,
        }

    alpha = np.asarray(image.getchannel("A"), dtype=np.uint8)
    total = int(alpha.size)
    transparent = int((alpha == 0).sum())
    opaque = int((alpha == 255).sum())
    corners = [
        int(alpha[0, 0]),
        int(alpha[0, -1]),
        int(alpha[-1, 0]),
        int(alpha[-1, -1]),
    ]
    return {
        "mode": image.mode,
        "has_alpha": True,
        "corners_transparent": all(value == 0 for value in corners),
        "alpha_extrema": [int(alpha.min()), int(alpha.max())],
        "transparent_fraction": transparent / total,
        "opaque_fraction": opaque / total,
        "partial_fraction": (total - transparent - opaque) / total,
    }


def postprocess_file(
    src_png: str | Path,
    dest_png: str | Path,
    *,
    white_point: int = DEFAULT_WHITE_POINT,
    padding_fraction: float = DEFAULT_PADDING_FRACTION,
    seal_size: int = DEFAULT_SEAL_SIZE,
) -> dict:
    """Run :func:`white_to_alpha` on a file and return the alpha report."""
    from PIL import Image

    dest_png = Path(dest_png)
    with Image.open(src_png) as opened:
        result = white_to_alpha(
            opened,
            white_point=white_point,
            padding_fraction=padding_fraction,
            seal_size=seal_size,
        )
    dest_png.parent.mkdir(parents=True, exist_ok=True)
    result.save(dest_png)

    report = alpha_report(result)
    report["path"] = str(dest_png)
    report["size"] = [result.width, result.height]
    return report
