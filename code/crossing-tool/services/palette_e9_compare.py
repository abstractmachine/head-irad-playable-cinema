"""Compare E9-A against measured-review-v1 on the calibration film.

Evaluation only. Reads the frozen review record and the E9-A calibration tree
and writes a report; it mutates neither.

The honest comparison is not "which proposal set is nearer the final palette".
On a frame where the reviewer accepted an E8 proposal, E8 is at distance zero
by construction, and no revision can ever beat that. So the headline metric is
restricted to the roles the reviewer actually hand-picked — the roles where he
rejected everything E8 offered. That is the only population where both systems
are being asked the same open question.
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

from data import palette_review as store
from services import palette_e9 as e9
from services.palette_review_analysis import distance, role_origins, to_lab
from services.palette_review_stats import _summary

F, B = store.ROLE_FIGURE, store.ROLE_BACKGROUND
ROLES = (F, B)


def _selectable(choices: dict) -> list:
    keys = []
    for key, choice in (choices or {}).items():
        colours = [c for c in (choice.get("colours") or []) if c]
        if choice.get("active") and len(colours) == 2:
            keys.append(key)
    return sorted(keys)


def _role_colours(choices: dict, role: str) -> list:
    index = 0 if role == F else 1
    out = []
    for key in _selectable(choices):
        colour = (choices[key].get("colours") or [None, None])[index]
        if colour and colour.get("lab"):
            out.append({"choice": key, "strategy": choices[key].get("strategy"),
                        "lab": colour["lab"], "hex": colour.get("hex")})
    return out


def _nearest(colour_lab, candidates: list):
    scored = [(distance(colour_lab, c["lab"]), c) for c in candidates]
    scored = [(d, c) for d, c in scored if d is not None]
    return min(scored, key=lambda item: item[0]) if scored else (None, None)


def _materials(choices: dict) -> list:
    out = []
    for choice in (choices or {}).values():
        if choice.get("strategy") == "control":
            continue
        for material in choice.get("materials") or []:
            if material:
                out.append(material)
    return out


def _segmentation(choices: dict) -> Counter:
    counts = Counter()
    for choice in (choices or {}).values():
        for record in choice.get("measurement") or []:
            if record:
                counts[str((record.get("segmentation") or {}).get("status"))] += 1
    return counts


def compare(project_path: str, filename: str, media_type: str,
            calibration_root: Path) -> dict:
    record = store.load_review(project_path, filename, media_type)
    frames = record.get("frames") or {}

    rows = []
    for path in sorted((calibration_root / "frames").glob("*.json")):
        payload = json.loads(path.read_text(encoding="utf-8"))
        entry = frames.get(payload["shot_id"]) or {}
        if payload.get("status") != "generated" or not entry:
            rows.append({"shot_id": payload["shot_id"], "status": payload.get("status"),
                         "error": payload.get("error")})
            continue
        rows.append({
            "shot_id": payload["shot_id"],
            "status": "generated",
            "e8": entry.get("proposals") or {},
            "e9": payload.get("choices") or {},
            "human": role_origins(entry),
            "state": store.frame_state(entry),
        })

    usable = [r for r in rows if r["status"] == "generated"]

    # ---- D. availability
    availability = {"e8": Counter(), "e9": Counter(),
                    "e8_per_choice": Counter(), "e9_per_choice": Counter()}
    for row in usable:
        for system in ("e8", "e9"):
            keys = _selectable(row[system])
            availability[system][len(keys)] += 1
            for key in keys:
                availability[f"{system}_per_choice"][key] += 1

    # ---- E. compound materials
    compound = {}
    for system in ("e8", "e9"):
        total = flagged = 0
        for row in usable:
            for material in _materials(row[system]):
                total += 1
                if e9.material_problem(material):
                    flagged += 1
        compound[system] = {
            "materials": total, "compound": flagged,
            "percent": round(100.0 * flagged / total, 1) if total else None,
        }

    # ---- F. segmentation
    segmentation = {}
    for system in ("e8", "e9"):
        counts = Counter()
        for row in usable:
            counts += _segmentation(row[system])
        total = sum(counts.values())
        segmentation[system] = {
            "counts": dict(counts), "total": total,
            "success_percent": round(100.0 * counts.get("success", 0) / total, 1)
            if total else None,
        }

    # ---- A/B/C/G. distance to what the reviewer actually kept
    per_role = {}
    for role in ROLES:
        hand, accepted, pair_rows = [], [], []
        for row in usable:
            human = (row["human"] or {}).get(role) or {}
            lab = human.get("lab") or to_lab(human.get("rgb"))
            if not lab:
                continue
            d8, n8 = _nearest(lab, _role_colours(row["e8"], role))
            d9, n9 = _nearest(lab, _role_colours(row["e9"], role))
            item = {"shot_id": row["shot_id"], "source": human.get("source"),
                    "e8": d8, "e9": d9,
                    "e8_strategy": (n8 or {}).get("strategy"),
                    "e9_choice": (n9 or {}).get("choice")}
            (hand if human.get("source") == store.SOURCE_MANUAL else accepted).append(item)
            pair_rows.append(item)
        both = [i for i in hand if i["e8"] is not None and i["e9"] is not None]
        per_role[role] = {
            "hand_picked_roles": len(hand),
            "roles_taken_from_a_proposal": len(accepted),
            "headline_population": len(both),
            "delta_e_to_nearest_offered": {
                "e8": _summary([i["e8"] for i in both]),
                "e9": _summary([i["e9"] for i in both]),
            },
            "e9_closer": sum(1 for i in both if i["e9"] < i["e8"]),
            "e8_closer": sum(1 for i in both if i["e8"] < i["e9"]),
            "improved_by_more_than_5": sum(1 for i in both if i["e8"] - i["e9"] > 5),
            "worsened_by_more_than_5": sum(1 for i in both if i["e9"] - i["e8"] > 5),
            "e9_nearest_choice": dict(Counter(i["e9_choice"] for i in both)),
            "all_roles_including_accepted": {
                "e8": _summary([i["e8"] for i in pair_rows if i["e8"] is not None]),
                "e9": _summary([i["e9"] for i in pair_rows if i["e9"] is not None]),
                "note": ("E8 is at distance zero wherever the reviewer accepted "
                         "it, so this view is biased towards E8 by construction "
                         "and is reported only for completeness."),
            },
        }

    # ---- C. complete pair
    pair = {"e8_closer": 0, "e9_closer": 0, "tied": 0, "n": 0}
    for row in usable:
        human = row["human"] or {}
        if not all((human.get(r) or {}).get("lab") or (human.get(r) or {}).get("rgb")
                   for r in ROLES):
            continue
        totals = {}
        for system in ("e8", "e9"):
            total = 0.0
            ok = True
            for role in ROLES:
                lab = (human[role].get("lab") or to_lab(human[role].get("rgb")))
                value, _ = _nearest(lab, _role_colours(row[system], role))
                if value is None:
                    ok = False
                    break
                total += value
            totals[system] = total if ok else None
        if totals.get("e8") is None or totals.get("e9") is None:
            continue
        pair["n"] += 1
        if abs(totals["e8"] - totals["e9"]) < 1e-6:
            pair["tied"] += 1
        elif totals["e9"] < totals["e8"]:
            pair["e9_closer"] += 1
        else:
            pair["e8_closer"] += 1

    # ---- H. semantics improved but measurement still poor
    still_poor = 0
    for row in usable:
        seg = _segmentation(row["e9"])
        if seg and seg.get("success", 0) == 0 and _selectable(row["e9"]):
            still_poor += 1

    return {
        "population": {
            "calibration_frames": len(rows),
            "generated": len(usable),
            "failed": len(rows) - len(usable),
        },
        "D_availability": {
            "selectable_choices_per_frame": {
                "e8": dict(availability["e8"]), "e9": dict(availability["e9"])},
            "frames_offering_each_choice": {
                "e8": dict(availability["e8_per_choice"]),
                "e9": dict(availability["e9_per_choice"])},
        },
        "E_compound_materials": compound,
        "F_segmentation": segmentation,
        "AB_per_role": per_role,
        "C_complete_pair": pair,
        "H_selectable_but_no_clean_mask": still_poor,
        "method": (
            "Headline distances are restricted to roles the reviewer hand-picked, "
            "because on accepted roles E8 is at zero by construction."
        ),
    }


def main() -> None:
    import argparse

    from services.palette_e9_calibration import output_root

    parser = argparse.ArgumentParser()
    parser.add_argument("--project", default="/home/cowpoke/playable/dead-crossing")
    parser.add_argument("--film", default="10 000 Dollari Per Un Massacro (1967) {tmdb-95864}.mp4")
    args = parser.parse_args()
    root = output_root(args.project)
    report = compare(args.project, args.film, "movie", root)
    (root / "01-comparison.json").write_text(
        json.dumps(report, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    print(json.dumps(report, indent=2, default=str)[:4000])


if __name__ == "__main__":
    main()
