"""Acceptance gates for the local engraving converter.

Stage 5 of the pipeline.  Every check here is deterministic and runs without a
model.

An important limit is deliberate: these gates never return "accepted".  The
negative benchmark cases (a cow whose legs read on the wrong side of its
torso, a two-barrel pistol whose barrels diverge) were both polished,
attractive outputs that passed casual inspection.  Part counts, attachment
points, depth ordering and perspective-consistent axes are not reliably
checkable here, so the strongest automatic verdict is ``needs_review``.
Automatic checks exist to *reject* confidently bad output and to surface the
evidence a human reviewer needs — not to grant approval.
"""

from __future__ import annotations

STATUS_REJECTED = "rejected"
STATUS_NEEDS_REVIEW = "needs_review"

# Gates that a human must judge; listed in every report so the limits of the
# automatic pass stay visible in the run record.
MANUAL_GATES = (
    "identity_and_subtype",
    "pose_orientation_and_proportions",
    "exact_part_count_and_connectivity",
    "valid_attachment_points_and_depth_ordering",
    "side_of_body_anatomical_consistency",
    "perspective_consistent_axes_parallelism_and_spacing",
    "no_unsupported_additions",
    "anatomical_or_mechanical_coherence",
    "engraving_style_and_form_following_marks",
)


def _ink_mask(rgba, white_point: int):
    """Return a boolean array of opaque, non-white (inked) pixels."""
    import numpy as np

    arr = np.asarray(rgba.convert("RGBA"), dtype=np.uint8)
    alpha = arr[..., 3]
    luminance = (
        0.299 * arr[..., 0].astype(np.float32)
        + 0.587 * arr[..., 1].astype(np.float32)
        + 0.114 * arr[..., 2].astype(np.float32)
    )
    return (alpha > 0) & (luminance < white_point)


def check_alpha(report: dict) -> dict:
    """Gate: output must be RGBA with a genuinely transparent exterior."""
    failures = []
    if not report.get("has_alpha"):
        failures.append("output has no alpha channel")
    if not report.get("corners_transparent"):
        failures.append("output corners are not transparent")
    extrema = report.get("alpha_extrema") or []
    if len(extrema) == 2 and not (extrema[0] == 0 and extrema[1] == 255):
        failures.append(f"alpha channel does not contain both 0 and 255 (got {extrema})")
    return {"gate": "valid_alpha_crop_and_padding", "passed": not failures, "failures": failures}


def check_ink_coverage(rgba, config: dict) -> dict:
    """Gate: the crop must read as an engraving, not a blank or solid page."""
    import numpy as np

    rules = config["validate"]
    ink = _ink_mask(rgba, config["alpha"]["white_point"])
    fraction = float(ink.sum()) / float(ink.size) if ink.size else 0.0

    failures = []
    if fraction < rules["min_ink_fraction"]:
        failures.append(f"ink covers only {fraction:.3f} of the crop (min {rules['min_ink_fraction']})")
    if fraction > rules["max_ink_fraction"]:
        failures.append(f"ink covers {fraction:.3f} of the crop (max {rules['max_ink_fraction']})")
    return {
        "gate": "ink_coverage",
        "passed": not failures,
        "failures": failures,
        "ink_fraction": fraction,
    }


def check_isolation(rgba, config: dict) -> dict:
    """Gate: one article, not a collage of scattered debris."""
    import numpy as np
    from scipy import ndimage

    rules = config["validate"]
    ink = _ink_mask(rgba, config["alpha"]["white_point"])
    if not ink.any():
        return {
            "gate": "isolated_composition",
            "passed": False,
            "failures": ["no ink present"],
            "component_count": 0,
        }

    # Close hairline gaps so a single hatched object is not counted as many
    # disconnected marks.
    closed = ndimage.binary_closing(ink, structure=np.ones((5, 5)))
    labels, count = ndimage.label(closed)

    # Ignore specks below 0.1% of total ink area.
    if count:
        sizes = ndimage.sum(closed, labels, range(1, count + 1))
        significant = int((sizes >= max(1.0, 0.001 * closed.sum())).sum())
    else:
        significant = 0

    failures = []
    if significant > rules["max_ink_components"]:
        failures.append(
            f"{significant} disconnected ink regions (max {rules['max_ink_components']}); "
            f"possible contextual debris or a fragmented object"
        )
    return {
        "gate": "isolated_composition",
        "passed": not failures,
        "failures": failures,
        "component_count": significant,
    }


def check_print_legibility(rgba, config: dict) -> dict:
    """Gate: linework must survive reduction to catalogue print size."""
    import numpy as np
    from PIL import Image

    rules = config["validate"]
    dpi = float(rules["print_dpi"])
    target_px = int(round(rules["print_min_mm"] / 25.4 * dpi))

    longest = max(rgba.size)
    if longest <= target_px:
        scaled = rgba
    else:
        ratio = target_px / longest
        scaled = rgba.resize(
            (max(1, int(rgba.width * ratio)), max(1, int(rgba.height * ratio))),
            Image.Resampling.LANCZOS,
        )

    ink = _ink_mask(scaled, config["alpha"]["white_point"])
    fraction = float(ink.sum()) / float(ink.size) if ink.size else 0.0

    failures = []
    if fraction < rules["min_print_ink_fraction"]:
        failures.append(
            f"only {fraction:.4f} ink survives at {rules['print_min_mm']}mm/{int(dpi)}dpi "
            f"(min {rules['min_print_ink_fraction']}); linework is too fine for print"
        )
    return {
        "gate": "print_scale_legibility",
        "passed": not failures,
        "failures": failures,
        "print_ink_fraction": fraction,
        "simulated_longest_px": target_px,
    }


def check_silhouette_agreement(rgba, evidence: dict, config: dict) -> dict:
    """Report how far the result's shape drifted from the source silhouette.

    Both masks are normalised to their own bounding box before comparison, so
    this measures shape agreement independently of crop and scale.  Repair
    legitimately alters the outline where it completes occluded structure, so a
    moderate score is expected; a very low score means the object was replaced
    rather than reconstructed.
    """
    import numpy as np
    from PIL import Image

    def _normalised(mask_bool):
        ys, xs = np.nonzero(mask_bool)
        if not len(xs):
            return None
        cropped = mask_bool[ys.min(): ys.max() + 1, xs.min(): xs.max() + 1]
        img = Image.fromarray((cropped * 255).astype(np.uint8), mode="L")
        return np.asarray(img.resize((256, 256), Image.Resampling.NEAREST)) > 127

    result_mask = _normalised(np.asarray(rgba.convert("RGBA"))[..., 3] > 0)
    source_mask = _normalised(np.asarray(evidence["mask"]) > 127)

    if result_mask is None or source_mask is None:
        return {
            "gate": "silhouette_agreement",
            "passed": False,
            "failures": ["could not compute silhouette agreement"],
            "iou": None,
        }

    intersection = float(np.logical_and(result_mask, source_mask).sum())
    union = float(np.logical_or(result_mask, source_mask).sum())
    iou = intersection / union if union else 0.0

    # Deliberately permissive: this only catches wholesale object replacement.
    failures = []
    if iou < 0.20:
        failures.append(
            f"normalised silhouette IoU is {iou:.3f}; the result does not appear to be "
            f"the same object as the source silhouette"
        )
    return {
        "gate": "silhouette_agreement",
        "passed": not failures,
        "failures": failures,
        "iou": iou,
    }


def validate_result(rgba, evidence: dict, alpha_report: dict, config: dict) -> dict:
    """Run every automatic gate and return a consolidated verdict.

    The verdict is ``rejected`` when any automatic gate fails, otherwise
    ``needs_review``.  It is never ``accepted``; see the module docstring.
    """
    gates = [
        check_alpha(alpha_report),
        check_ink_coverage(rgba, config),
        check_isolation(rgba, config),
        check_print_legibility(rgba, config),
        check_silhouette_agreement(rgba, evidence, config),
    ]

    failures = [f for gate in gates for f in gate["failures"]]
    review = evidence.get("review", {})
    review_reasons = list(review.get("reasons", []))

    status = STATUS_REJECTED if failures else STATUS_NEEDS_REVIEW
    return {
        "status": status,
        "automatic_gates": gates,
        "automatic_failures": failures,
        "manual_review_required": True,
        "manual_gates": list(MANUAL_GATES),
        "evidence_review_reasons": review_reasons,
        "notes": (
            "Automatic gates can reject but never accept. Part count, connectivity, "
            "attachment points, depth ordering and perspective-consistent axes "
            "require human review at full size and at simulated print size."
        ),
    }
