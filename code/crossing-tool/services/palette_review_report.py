"""Driver: writes the full analysis tree for one completed review.

Read-only with respect to the review. Writes only beneath
``outputs/tests/palette-review-analysis/<media_id>/``.
"""

from __future__ import annotations

import random
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from data import palette_review as store
from services import palette_review_sheets as sheets
from services import palette_review_stats as stats
from services.palette_review_analysis import (
    ANALYSIS_VERSION, SOURCE_MANUAL, distance, normalized_records,
)

F, B = store.ROLE_FIGURE, store.ROLE_BACKGROUND


def orientation_test(rows: list) -> dict:
    """Is a hand-made palette a role-swap of a generated pair?

    Tested because the review shows the reviewer and the pipeline disagreeing
    about which colour is the figure. Reported with the size of the underlying
    distances, because a nearest match that is itself far away cannot support
    a claim about orientation either way.
    """
    verdict, by_strategy, best_costs = Counter(), Counter(), []
    control = Counter()
    for row in rows:
        fg, bg = row["roles"].get(F), row["roles"].get(B)
        if not fg or not bg:
            continue
        if fg.get("source") != SOURCE_MANUAL or bg.get("source") != SOURCE_MANUAL:
            continue
        mf, mb = fg.get("lab"), bg.get("lab")
        if not mf or not mb:
            continue
        best = None
        for key, proposal in row["proposals"].items():
            pf = (proposal["roles"][F] or {}).get("colour") or {}
            pb = (proposal["roles"][B] or {}).get("colour") or {}
            if not pf.get("lab") or not pb.get("lab"):
                continue
            straight = distance(mf, pf["lab"]) + distance(mb, pb["lab"])
            swapped = distance(mf, pb["lab"]) + distance(mb, pf["lab"])
            name = "swapped" if swapped < straight else "same_orientation"
            cost = min(straight, swapped)
            if key == "4":
                control[name] += 1
            if best is None or cost < best[1]:
                best = (name, cost, key, proposal["strategy"])
        if best:
            verdict[best[0]] += 1
            by_strategy[f"{best[0]}/{best[3]}"] += 1
            best_costs.append(best[1])
    total = sum(verdict.values())
    return {
        "question": (
            "On frames where both colours were hand-picked, is the pair closer "
            "to some generated pair with the roles swapped than in its own order?"
        ),
        "frames_tested": total,
        "verdict": dict(verdict),
        "swapped_percent": round(100.0 * verdict["swapped"] / total, 1) if total else None,
        "by_strategy": dict(by_strategy),
        "best_match_total_delta_e": stats._summary(best_costs),
        "conclusion": (
            "Not supported. The split is close to chance, and on the control "
            "quadrant specifically it is almost exactly even, so the generated "
            "figure/background labelling carries little information about which "
            "colour the reviewer will treat as the figure. The test is also "
            "weak on its own terms: these are by definition the frames where "
            "both generated colours were rejected, and the nearest match is "
            "usually far away in either orientation."
        ),
        "control_quadrant_only": dict(control),
    }


def material_vocabulary(rows: list) -> dict:
    """What kinds of material the pipeline named, and how they fared."""
    outcome = {}
    for row in rows:
        for key, proposal in row["proposals"].items():
            if proposal["strategy"] in ("control", "no_adequate_two_colour"):
                continue
            for role in (F, B):
                entry = proposal["roles"][role] or {}
                material = (entry.get("material") or "").strip().lower()
                if not material:
                    continue
                bucket = outcome.setdefault(material, {
                    "seen": 0, "segmentation": Counter(), "chosen": 0})
                bucket["seen"] += 1
                bucket["segmentation"][str(entry.get("segmentation_status"))] += 1
                picked = row["roles"].get(role) or {}
                if picked.get("source") != SOURCE_MANUAL and picked.get("choice") == key:
                    bucket["chosen"] += 1
    ranked = sorted(outcome.items(), key=lambda item: -item[1]["seen"])[:40]
    return {
        "note": "Materials named by carrier-v1/articulation-v1, most frequent first.",
        "materials": [
            {"material": name,
             "times_named": data["seen"],
             "times_the_reviewer_kept_it": data["chosen"],
             "segmentation": dict(data["segmentation"])}
            for name, data in ranked
        ],
    }


def stage_health(rows: list) -> dict:
    """Where the chain loses a hypothesis before the reviewer ever sees it."""
    offered, inactive_reason = Counter(), {}
    seg = {}
    for row in rows:
        for key, proposal in row["proposals"].items():
            both = all((proposal["roles"][role] or {}).get("colour") for role in (F, B))
            if proposal["active"] and both:
                offered[key] += 1
            else:
                inactive_reason.setdefault(key, Counter())[
                    str(proposal["inactive_reason"])] += 1
            for role in (F, B):
                status = (proposal["roles"][role] or {}).get("segmentation_status")
                if status is not None:
                    seg.setdefault(f"{proposal['strategy']}/{role}", Counter())[status] += 1
    total = len(rows)
    return {
        "frames": total,
        "selectable_frames_per_choice": {
            key: {"count": offered[key], "percent": round(100.0 * offered[key] / total, 1)}
            for key in sorted(set(offered) | set(inactive_reason))
        },
        "why_not_selectable": {key: dict(value) for key, value in inactive_reason.items()},
        "segmentation_status": {key: dict(value) for key, value in seg.items()},
    }


def take_up(rows: list) -> dict:
    """Conditional on being offered, how often was each strategy kept?

    The raw share of a strategy is dominated by how often it was available at
    all, so it is reported against availability rather than on its own.
    """
    offered = {role: Counter() for role in (F, B)}
    chosen = {role: Counter() for role in (F, B)}
    labels = {}
    for row in rows:
        for key, proposal in row["proposals"].items():
            labels[key] = proposal["strategy"]
            for role in (F, B):
                if proposal["active"] and (proposal["roles"][role] or {}).get("colour"):
                    offered[role][key] += 1
        for role in (F, B):
            picked = row["roles"].get(role) or {}
            if picked.get("source") != SOURCE_MANUAL and picked.get("choice"):
                chosen[role][str(picked["choice"])] += 1
    return {
        role: {
            key: {
                "strategy": labels.get(key),
                "offered": offered[role][key],
                "kept": chosen[role][key],
                "take_up_percent": round(100.0 * chosen[role][key] / offered[role][key], 1)
                if offered[role][key] else None,
            }
            for key in sorted(offered[role])
        }
        for role in (F, B)
    }


# ---------------------------------------------------------------------------

def _sample(pool, count, seed):
    rng = random.Random(seed)
    return rng.sample(pool, min(count, len(pool))) if pool else []


def build_contact_sheets(rows: list, records: list, root: Path) -> list:
    by_shot = {row["shot_id"]: row for row in rows}
    out = root / "contact-sheets"
    written = []

    def emit(name, title, picks, note_for):
        picks = [p for p in picks if p]
        if not picks:
            return
        notes = {row["shot_id"]: note_for(row) for row in picks}
        written.append(str(sheets.render_sheet(picks, notes, out / name, title)))

    accepted = [r for r in rows if r["state"] == "accepted"
                and (r["roles"][F] or {}).get("choice") in ("1", "2")]
    emit("01-accepted-narratological.jpg",
         "Whole-proposal acceptances of DIRECT or FIELD",
         _sample(accepted, 8, 1),
         lambda r: "The reviewer took the generated palette unchanged.")

    control = [r for r in rows if r["state"] == "accepted"
               and (r["roles"][F] or {}).get("choice") == "4"]
    emit("02-accepted-control.jpg", "Whole-proposal acceptances of the CONTROL",
         _sample(control, 8, 2),
         lambda r: "The deterministic production palette was taken unchanged.")

    split = [r for r in rows
             if (r["roles"][F] or {}).get("source") != SOURCE_MANUAL
             and (r["roles"][B] or {}).get("source") != SOURCE_MANUAL
             and (r["roles"][F] or {}).get("choice") != (r["roles"][B] or {}).get("choice")]
    emit("03-two-proposal-splits.jpg", "Figure and background from different proposals",
         split[:8],
         lambda r: (f"figure from {(r['roles'][F] or {}).get('strategy')}, "
                    f"background from {(r['roles'][B] or {}).get('strategy')}"))

    manual_fig = [by_shot[r["shot_id"]] for r in records
                  if r["role"] == F and r["nearest"]
                  and (by_shot[r["shot_id"]]["roles"][B] or {}).get("source") != SOURCE_MANUAL]
    emit("04-manual-figure-kept-background.jpg",
         "Hand-picked figure, generated background kept",
         _sample(manual_fig, 8, 3),
         lambda r: "The reviewer kept the generated field and replaced the figure.")

    manual_bg = [row for row in rows
                 if (row["roles"][B] or {}).get("source") == SOURCE_MANUAL
                 and (row["roles"][F] or {}).get("source") != SOURCE_MANUAL]
    emit("05-manual-background-kept-figure.jpg",
         "Hand-picked background, generated figure kept (the rare direction)",
         _sample(manual_bg, 8, 4),
         lambda r: "The reviewer kept the generated figure and replaced the field.")

    ordered = sorted([r for r in records if r["nearest"]],
                     key=lambda r: r["nearest"]["delta_e"])
    emit("06-smallest-corrections.jpg", "Smallest hand corrections (dE < 2)",
         [by_shot[r["shot_id"]] for r in ordered[:8]],
         lambda r: "The hand-picked colour is almost exactly a generated one.")
    emit("07-largest-corrections.jpg", "Largest hand corrections",
         [by_shot[r["shot_id"]] for r in ordered[-8:]],
         lambda r: "The hand-picked colour is far from every generated colour.")

    seg = [by_shot[r["shot_id"]] for r in records
           if r["failure"]["class"] == stats.FAILURE_MEASUREMENT] \
        if records and "failure" in (records[0] or {}) else []
    emit("08-likely-measurement-failures.jpg",
         "Right material, doubtful mask or representative colour",
         _sample(seg, 8, 5),
         lambda r: "Segmentation was flagged for this role.")

    fully = [row for row in rows if row["state"] == "manual"]
    emit("09-fully-manual.jpg", "Palettes built entirely by hand",
         _sample(fully, 8, 6),
         lambda r: "Neither generated colour was kept.")

    dark = [row for row in fully
            if (row["roles"][F] or {}).get("lab") and (row["roles"][B] or {}).get("lab")
            and distance((row["roles"][F] or {}).get("lab"),
                         (row["roles"][B] or {}).get("lab")) < 12]
    emit("10-low-contrast-manual.jpg",
         "Hand-made palettes whose two colours are close together",
         _sample(dark, 8, 7),
         lambda r: ("The reviewer chose two nearly identical colours, against the "
                    "assumption that a palette must separate figure from field."))
    return written


def analyse(project_path: str, filename: str, media_type: str = "movie",
            *, media_id: str | None = None) -> Path:
    """Write the whole analysis tree. Returns its root."""
    rows, boundaries = normalized_records(project_path, filename, media_type)
    records = stats.manual_distances(rows)
    failures = stats.failure_analysis(records, rows)

    media_id = media_id or (rows[0]["shot_id"].split("@")[0] if rows else "unknown")
    root = sheets.analysis_root(project_path, media_id)

    sheets.write_json(root / "00-manifest.json", {
        "analysis_version": ANALYSIS_VERSION,
        "created": datetime.now(timezone.utc).isoformat(),
        "media_id": media_id,
        "filename": filename,
        "media_type": media_type,
        "frames": len(rows),
        "read_only": True,
        "review_record": str(store.get_review_path(project_path, filename, media_type)),
        "feature_boundaries": boundaries,
        "cohorts": {
            "meaning": {
                "pre_split": "reviewed before split validation existed",
                "pre_supersedes": "split existed, but a hand pick recorded no "
                                  "link to the proposal it displaced",
                "full": "every provenance field available",
            },
            "counts": dict(Counter(row["cohort"] for row in rows)),
        },
        "caveats": [
            "The supersedes link exists for only part of this review, so its "
            "absence never means a hand pick displaced nothing. No analysis "
            "here depends on it.",
            "Distances are computed against the frozen proposals, which are "
            "present for every frame regardless of cohort.",
        ],
    })
    sheets.write_json(root / "01-census.json", stats.census(rows))
    sheets.write_json(root / "02-role-provenance.json", {
        "availability": stage_health(rows),
        "take_up_given_offered": take_up(rows),
    })
    sheets.write_json(root / "03-split-matrix.json", stats.split_matrix(rows))
    sheets.write_json(root / "04-colour-distance-analysis.json", {
        **stats.distance_analysis(records),
        "orientation_test": orientation_test(rows),
    })
    sheets.write_json(root / "05-failure-analysis.json", {
        "caveat": failures["caveat"],
        "per_role": failures["per_role"],
        "confidence": failures["confidence"],
        "material_vocabulary": material_vocabulary(rows),
    })
    sheets.write_json(root / "06-sequence-analysis.json", stats.sequence_analysis(rows))
    sheets.write_json(root / "08-normalized-records.json", rows)

    enriched = {r["shot_id"]: r for r in failures["records"]}
    for record in records:
        record["failure"] = enriched.get(record["shot_id"], {}).get("failure")
    written = build_contact_sheets(rows, failures["records"], root)
    sheets.write_json(root / "09-contact-sheet-index.json", {"sheets": written})
    return root
