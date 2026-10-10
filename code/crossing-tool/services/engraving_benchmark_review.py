"""Human review decisions for engraving benchmark proposals.

Curatorial judgement is *research data*, not derived data.  It therefore lives
beside the proposal it belongs to and is never written into the canonical
silhouette catalog or the Illustration index, and never back into the proposal
manifest itself -- a proposal must stay exactly as it was generated so its
candidate IDs keep their experimental meaning.

A decision records what a human concluded from the review sheet:

``accepted``
    Worth carrying forward as benchmark material.
``rejected_visual_viability``
    The available visual evidence was not convincing enough *in that sheet*.
    This is deliberately not a claim that the object is permanently unusable:
    a weak source frame may still be rescued by other frames in its shot.
``unreviewed``
    No human has judged it.
"""

from __future__ import annotations

import json
from pathlib import Path

ACCEPTED = "accepted"
REJECTED_VISUAL = "rejected_visual_viability"
UNREVIEWED = "unreviewed"
DECISIONS = (ACCEPTED, REJECTED_VISUAL, UNREVIEWED)

# Optional, for future reviews.  The first review recorded only the overall
# accept/reject judgement, so no per-case reason is invented for it.
REJECTION_REASONS = (
    "too_small",
    "too_blurry",
    "visually_ambiguous",
    "bad_segmentation",
    "insufficient_evidence",
    "duplicate_or_redundant",
)

REVIEW_SCHEMA_VERSION = 1


class ReviewError(RuntimeError):
    """Raised when a review artifact is malformed or inconsistent."""


def review_path(benchmark_root: str | Path, proposal: int = 1) -> Path:
    """Return the review artifact path for one proposal."""
    root = Path(benchmark_root)
    return root / (f"review.json" if proposal == 1 else f"review-proposal-{proposal}.json")


def build_review(
    manifest: dict,
    accepted_ids: list[str],
    *,
    proposal: int = 1,
    manifest_path: str | None = None,
    note: str | None = None,
) -> dict:
    """Return a review artifact marking *accepted_ids* accepted, the rest rejected.

    Every candidate in the manifest gets an explicit decision so the artifact
    is self-describing; an unknown accepted id is an error rather than a
    silent no-op.
    """
    known = [candidate["benchmark_id"] for candidate in manifest.get("candidates", [])]
    unknown = sorted(set(accepted_ids) - set(known))
    if unknown:
        raise ReviewError(f"Accepted ids not present in the manifest: {', '.join(unknown)}")

    accepted = set(accepted_ids)
    decisions = {
        benchmark_id: {"decision": ACCEPTED if benchmark_id in accepted else REJECTED_VISUAL}
        for benchmark_id in known
    }
    return {
        "review_version": REVIEW_SCHEMA_VERSION,
        "proposal": proposal,
        "benchmark_manifest": manifest_path or "candidates.json",
        "note": note or (
            "Rejection means the visual evidence was unconvincing in this review "
            "sheet, not that the object is permanently unusable."
        ),
        "counts": {
            ACCEPTED: sum(1 for d in decisions.values() if d["decision"] == ACCEPTED),
            REJECTED_VISUAL: sum(1 for d in decisions.values() if d["decision"] == REJECTED_VISUAL),
        },
        "decisions": decisions,
    }


def save_review(path: str | Path, review: dict) -> Path:
    """Persist a review artifact atomically."""
    from data.annotate import atomic_write_text

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(path, json.dumps(review, indent=2, ensure_ascii=False) + "\n")
    return path


def load_review(path: str | Path) -> dict:
    """Read a review artifact."""
    path = Path(path)
    if not path.is_file():
        raise ReviewError(f"No review artifact at {path}")
    try:
        review = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ReviewError(f"Review artifact is not valid JSON: {path}\n  {exc}") from exc
    if "decisions" not in review:
        raise ReviewError(f"Review artifact has no decisions: {path}")
    return review


def decision_for(review: dict, benchmark_id: str) -> str:
    """Return the recorded decision, or ``unreviewed``."""
    entry = (review.get("decisions") or {}).get(benchmark_id) or {}
    return str(entry.get("decision") or UNREVIEWED)


def accepted_ids(review: dict) -> list[str]:
    """Return the accepted benchmark ids, in manifest order."""
    return [
        benchmark_id
        for benchmark_id, entry in (review.get("decisions") or {}).items()
        if entry.get("decision") == ACCEPTED
    ]


def annotate_candidates(manifest: dict, review: dict) -> list[dict]:
    """Return manifest candidates paired with their decision.

    Returns copies: the manifest passed in is never mutated, so proposal 1's
    artifact stays byte-stable.
    """
    annotated = []
    for candidate in manifest.get("candidates", []):
        entry = dict(candidate)
        entry["human_review"] = decision_for(review, candidate["benchmark_id"])
        annotated.append(entry)
    return annotated


def analyze_viability(manifest: dict, review: dict, metrics: dict) -> dict:
    """Compare an interpretable floor against exploratory human decisions."""
    import numpy as np
    from services.engraving_viability import DEFAULT_GATE, evaluate_gate

    cases = []
    confusion = {"TP": 0, "FP": 0, "FN": 0, "TN": 0}
    for candidate in manifest["candidates"]:
        benchmark_id = candidate["benchmark_id"]
        record = dict(candidate["source"], **candidate["evidence"])
        found = dict(metrics.get(record["record_path"]) or {})
        found["mask_area_fraction"] = record.get("mask_area_fraction")
        verdict = evaluate_gate(found, record)
        decision = decision_for(review, benchmark_id)
        if decision not in (ACCEPTED, REJECTED_VISUAL):
            raise ReviewError(f"Cannot analyze unreviewed candidate: {benchmark_id}")
        human = decision == ACCEPTED
        outcome = ("TP" if verdict["viable"] else "FN") if human else ("FP" if verdict["viable"] else "TN")
        confusion[outcome] += 1
        cases.append({
            "benchmark_id": benchmark_id, "label": record["label"],
            "record_path": record["record_path"], "decision": decision,
            "viability": verdict, "metrics": found,
        })

    distributions = {}
    for name in (
        "short_dimension_px", "long_dimension_px", "mask_area_px", "mask_area_fraction",
        "mask_fill_fraction", "laplacian_variance", "edge_energy", "edge_density",
        "local_contrast", "largest_component_fraction", "connected_components",
    ):
        distributions[name] = {}
        for decision in (ACCEPTED, REJECTED_VISUAL):
            values = [case["metrics"][name] for case in cases
                      if case["decision"] == decision and case["metrics"].get(name) is not None]
            if values:
                quantiles = np.quantile(values, [0, .25, .5, .75, 1]).tolist()
                distributions[name][decision] = dict(zip(("min", "q25", "median", "q75", "max"), quantiles))

    return {
        "proposal": manifest.get("proposal", 1), "gate": dict(DEFAULT_GATE),
        "confusion": confusion,
        "precision": confusion["TP"] / max(1, confusion["TP"] + confusion["FP"]),
        "recall": confusion["TP"] / max(1, confusion["TP"] + confusion["FN"]),
        "accepted_anchors_lost": [case["benchmark_id"] for case in cases
                                  if case["decision"] == ACCEPTED and not case["viability"]["viable"]],
        "rejected_passing": [case["benchmark_id"] for case in cases
                             if case["decision"] == REJECTED_VISUAL and case["viability"]["viable"]],
        "distributions": distributions, "cases": cases,
        "interpretation": "An anchor-preserving floor, not a perceptual model. In-sample evaluation on 40 cases is not held-out validation.",
    }


def compare_proposals(analysis: dict, proposal: dict, project_path: str | Path) -> dict:
    """Account for every accepted anchor and every rejection reappearance."""
    from services.engraving_viability import canonical_record_path

    selected = {canonical_record_path(project_path, candidate["source"]["record_path"]): candidate["benchmark_id"]
                for candidate in proposal["candidates"]}
    reference_only = {canonical_record_path(project_path, path)
                      for path in proposal["config"].get("reference_only_paths", [])}
    anchors = []
    reappeared = []
    for case in analysis["cases"]:
        key = canonical_record_path(project_path, case["record_path"])
        selected_id = selected.get(key)
        if case["decision"] == ACCEPTED:
            anchors.append({
                "proposal_1_id": case["benchmark_id"], "label": case["label"],
                "viability": case["viability"], "proposal_2_id": selected_id,
                "pinned": False, "reference_only": selected_id is None,
                "excluded_from_new_candidate_quota": key in reference_only,
            })
        elif selected_id:
            reappeared.append({"proposal_1_id": case["benchmark_id"],
                              "proposal_2_id": selected_id, "label": case["label"]})
    return {"anchors": anchors, "previous_rejections_reappeared": reappeared}
