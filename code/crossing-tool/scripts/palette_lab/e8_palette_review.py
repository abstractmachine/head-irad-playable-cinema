"""E8 narratological branch — first end-to-end measured palette review.

    uv run python -m scripts.palette_lab.e8_palette_review pilot --project /path

Collects the frozen narratological hypotheses, operationalizes each material
just enough for segmentation, runs SAM3, measures the pixels, retrieves a
deterministic control, and writes review sheets.

The division of labour is the point of the experiment:

    VLM ....... what matters, and through what material it is made visible
    SAM3 ...... where that material is
    Python .... what colour those pixels actually have
    human ..... which palette tells the story of the frame

Python collects, deduplicates, segments, measures, renders and records. It does
not decide what is narratologically important, invent a field relation, rank
palettes, weight anything aesthetically, or select a winner. There is no
scorer here by design.
"""

from __future__ import annotations

import argparse
import re
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

from scripts.palette_lab import prompts as prompt_io
from scripts.palette_lab import stages
from scripts.palette_lab.e4_pilot import CORPUS_SPACE, PILOT, SPACE
from scripts.palette_lab.e5_two_color import _read_json, _write_json
from scripts.palette_lab.e8_curator import DIAGNOSTIC, MODEL
from scripts.palette_lab.e8_narratology import EXPERIMENT


ROLE = "operationalize"
PROMPT_FILES = {
    "v1": ("prompts/palettes/palette-operationalize-system-2026-09-15-v1.txt",
           "prompts/palettes/palette-operationalize-user-2026-09-15-v1.txt"),
}
LATEST = "v1"
STAGE = "measured-review"
NARRATOLOGY_STAGE = "narratology-v1"
CARRIER_STAGE = "carrier-v1"
ARTICULATION_STAGE = "articulation-v1"

# The operationalizer is a patch pass; its answers are short by construction.
OPERATIONALIZE_TOKENS = 512
STATUSES = ("atomic", "decomposed", "distributed", "unusable")
OPERATIONALIZE_FIELDS = ("source_material", "status", "localization_targets", "reason")
MAX_TARGETS = 3

STRATEGY_LABELS = {
    "direct": "DIRECT",
    "field": "FIELD",
    "alternative": "ALTERNATIVE",
    "no_adequate_two_colour": "NO ADEQUATE TWO-COLOUR PALETTE",
    "control": "CONTROL",
}
CONTROL_METHOD = (
    "production data.palette thumbnail palette (foreground/background), frozen "
    "in corpus-manifest.json on 2026-08-18. Bottom-up, deterministic, computed "
    "without any narratological evidence and not recomputed here."
)
# A single generic threshold, applied identically to every target in every
# image, for flagging a mask that has swallowed the frame.
BROAD_COVERAGE = 0.90
# Worst-first; the overall status of a material is the worst of its targets.
STATUS_PRECEDENCE = (
    "unusable", "error", "no_mask", "suspicious_broad", "multiple_masks", "success",
)


def normalise_material(value) -> str:
    text = str(value or "").strip().lower().replace("\u2019", "'")
    text = re.sub(r"[^a-z0-9 ]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _pair_key(materials) -> tuple:
    return tuple(normalise_material(item) for item in (materials or []))


def collect_hypotheses(narratology: dict, carrier: dict, articulation: dict,
                       production_palette: dict | None) -> dict:
    """Assemble up to four review choices out of frozen evidence only.

    Nothing is invented here. Choice 1 is carrier-v1's material pair, choice 2
    is articulation-v1's pair, choice 3 is a second relation that narratology-v1
    actually recorded, and choice 4 is a frozen deterministic palette. When
    narratology-v1 recorded no second relation, choice 3 becomes the refusal
    option rather than a manufactured third palette.
    """
    carrier_entry = (carrier.get("carriers") or [{}])[0]
    articulation_entry = (articulation.get("articulations") or [{}])[0]

    direct_materials = []
    for name in ("carrier_a", "carrier_b"):
        side = carrier_entry.get(name)
        if isinstance(side, dict):
            direct_materials.append(str(side.get("visible_material") or ""))
    pair = articulation_entry.get("chromatic_articulation")
    pair = pair if isinstance(pair, dict) else {}
    field_materials = [
        str(pair.get("distinguished_material") or ""),
        str(pair.get("counterfield_material") or ""),
    ]
    field_materials = [item for item in field_materials if item]

    choices = {
        "1": {
            "strategy": "direct",
            "active": bool(len(direct_materials) == 2),
            "source_stage": CARRIER_STAGE,
            "materials": direct_materials,
            "source_note": "the materials carrier-v1 named for each side of the distinction",
        },
        "2": {
            "strategy": "field",
            "active": bool(len(field_materials) == 2),
            "source_stage": ARTICULATION_STAGE,
            "materials": field_materials,
            "source_note": (
                "articulation-v1's distinguished material and counterfield "
                f"(it called the relationship {pair.get('relationship') or 'none'})"
            ),
        },
    }

    choices["3"] = _alternative_or_refusal(
        narratology, carrier_entry, articulation_entry
    )

    control_materials, control_colours = [], []
    if production_palette:
        control_materials = ["production foreground", "production background"]
        control_colours = [
            _colour_record(production_palette.get("foreground_rgb")),
            _colour_record(production_palette.get("background_rgb")),
        ]
    choices["4"] = {
        "strategy": "control",
        "active": bool(control_colours),
        "source_stage": "corpus-manifest.json (frozen production palette)",
        "materials": control_materials,
        "colours": control_colours,
        "method": CONTROL_METHOD,
        "source_note": "deterministic bottom-up baseline; no narratological evidence",
    }

    for key, choice in choices.items():
        choice["label"] = STRATEGY_LABELS[choice["strategy"]]
    return choices


def _alternative_or_refusal(narratology: dict, carrier_entry: dict,
                            articulation_entry: dict) -> dict:
    """Choice 3 from a second frozen relation, else the refusal option.

    A second relation counts only when narratology-v1 recorded one — a second
    organizing distinction, or a second "what stands out / against" entry.
    Rewording the first relation would be a manufactured alternative, so it is
    not treated as one.
    """
    distinctions = narratology.get("organizing_distinctions") or []
    if len(distinctions) > 1:
        extra = distinctions[1]
        return {
            "strategy": "alternative",
            "active": True,
            "source_stage": NARRATOLOGY_STAGE,
            "materials": [str(extra.get("side_a") or ""), str(extra.get("side_b") or "")],
            "source_note": "a second organizing distinction recorded by narratology-v1",
        }

    stands_out = narratology.get("what_stands_out") or []
    if len(stands_out) > 1:
        extra = stands_out[1]
        return {
            "strategy": "alternative",
            "active": True,
            "source_stage": NARRATOLOGY_STAGE,
            "materials": [str(extra.get("entity") or ""), str(extra.get("against") or "")],
            "source_note": "a second 'what stands out / against' relation recorded by narratology-v1",
        }

    return {
        "strategy": "no_adequate_two_colour",
        "active": True,
        "source_stage": None,
        "materials": [],
        "source_note": (
            "narratology-v1 recorded only one relation for this frame, so there "
            "is no second hypothesis to measure. The quadrant is offered to the "
            "reviewer as refusal rather than filled with a manufactured palette."
        ),
        "frozen_support": {
            "carrier_v1_strength": carrier_entry.get("two_colour_strength"),
            "articulation_v1_strength": articulation_entry.get("two_colour_strength"),
            "note": (
                "Neither frozen stage returned 'insufficient' for any pilot "
                "image, so no frozen verdict positively supports refusal. The "
                "verdicts are shown so the reviewer can see what choosing it "
                "would contradict."
            ),
        },
    }


def mark_convergence(choices: dict) -> dict:
    """Record which choices rest on the same materials. Exact match only.

    No perceptual tolerance is applied: two different measured palettes are
    never declared "the same" here.
    """
    seen: dict[tuple, str] = {}
    for key in sorted(choices, key=int):
        choice = choices[key]
        choice.setdefault("converges_with", [])
        materials = choice.get("materials") or []
        if not choice.get("active") or len(materials) != 2:
            continue
        key_pair = _pair_key(materials)
        if key_pair in seen:
            origin = seen[key_pair]
            choice["converges_with"] = [origin]
            choice["active"] = False
            choice["convergence_reason"] = "same source materials"
            choices[origin].setdefault("converged_by", []).append(key)
        else:
            seen[key_pair] = key
    return choices


def operationalization_problems(parsed, material: str) -> list[str]:
    """Check the patch's shape. It must be a patch and nothing more."""
    if not isinstance(parsed, dict):
        return ["response did not parse as a JSON object"]
    problems = []
    extra = sorted(set(parsed) - set(OPERATIONALIZE_FIELDS))
    if extra:
        problems.append(f"returned fields outside the patch schema: {', '.join(extra)}")
    missing = [field for field in OPERATIONALIZE_FIELDS if field not in parsed]
    if missing:
        problems.append(f"missing {', '.join(missing)}")

    if normalise_material(parsed.get("source_material")) != normalise_material(material):
        problems.append("source_material was not copied")
    status = str(parsed.get("status") or "").strip().lower()
    if status not in STATUSES:
        problems.append(f"status {status!r} is not one of {STATUSES}")

    targets = parsed.get("localization_targets")
    if not isinstance(targets, list):
        problems.append("localization_targets is not a list")
        return problems
    named = [str(item or "").strip() for item in targets if str(item or "").strip()]
    if status == "unusable":
        if named:
            problems.append("status is 'unusable' but targets were named")
    elif not named:
        problems.append("no localization targets")
    if len(named) > MAX_TARGETS:
        problems.append(f"{len(named)} targets exceeds the maximum of {MAX_TARGETS}")
    if not str(parsed.get("reason") or "").strip():
        problems.append("reason is empty")
    return problems


def localization_targets(parsed) -> list[str]:
    if not isinstance(parsed, dict):
        return []
    targets, seen = [], set()
    for item in parsed.get("localization_targets") or []:
        text = str(item or "").strip()
        key = normalise_material(text)
        if text and key not in seen:
            seen.add(key)
            targets.append(text)
    return targets[:MAX_TARGETS]


def segmentation_status(result: dict) -> str:
    """Classify one SAM attempt. Generic, inspectable, identical for every image."""
    masks = result.get("masks") or []
    if not masks:
        errored = any(attempt.get("error") for attempt in result.get("attempts") or [])
        return "error" if errored else "no_mask"
    union = result.get("union")
    coverage = float(np.asarray(union).mean()) if union is not None else 0.0
    if coverage >= BROAD_COVERAGE:
        return "suspicious_broad"
    if len(masks) > 1:
        return "multiple_masks"
    return "success"


def worst_status(statuses: list[str]) -> str:
    for status in STATUS_PRECEDENCE:
        if status in statuses:
            return status
    return "no_mask"


def _colour_record(rgb) -> dict | None:
    """Format an RGB triplet. No adjustment of any kind is applied."""
    if rgb is None:
        return None
    from scripts.palette_lab.two_color import _rgb_to_lab, rgb_hex

    values = [int(item) for item in rgb]
    lab = _rgb_to_lab(np.asarray([values], dtype=np.uint8))[0]
    return {
        "rgb": values,
        "hex": rgb_hex(values),
        "lab": [round(float(item), 1) for item in lab],
    }


def measure(arr_rgb: np.ndarray, mask: np.ndarray) -> dict:
    """Mean of the masked pixels, via the lab's existing measurement helper.

    No chroma bonus, no area weighting, no salience term, no delta-E gate.
    """
    from scripts.palette_lab.two_color import _measure_mask

    return _measure_mask(arr_rgb, mask)


def review_template(pal: str, choices: dict, version: str) -> dict:
    """A blank record in the shape the future visualizer will write."""
    return {
        "id": pal,
        "choice": None,
        "reviewer": None,
        "timestamp": None,
        "experiment_version": f"{STAGE}-{version}",
        "note": None,
        "selectable": [
            key for key in sorted(choices, key=int) if choices[key].get("active")
        ],
    }


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------

def _free_cuda() -> None:
    try:
        import torch

        if torch.cuda.is_available():
            torch.cuda.empty_cache()
    except Exception:
        pass


def _frozen(project: Path, pal: str) -> dict:
    branch = project / "outputs" / "tests" / SPACE / EXPERIMENT
    return {
        "narratology": _read_json(
            branch / NARRATOLOGY_STAGE / pal / "03-narratology.json")["parsed"],
        "carrier": _read_json(
            branch / CARRIER_STAGE / pal / "04-carrier.json")["parsed"],
        "articulation": _read_json(
            branch / ARTICULATION_STAGE / pal / "04-articulation.json")["parsed"],
        "paths": {
            "narratology": f"{NARRATOLOGY_STAGE}/{pal}/03-narratology.json",
            "carrier": f"{CARRIER_STAGE}/{pal}/04-carrier.json",
            "articulation": f"{ARTICULATION_STAGE}/{pal}/04-articulation.json",
        },
    }


def operationalize(pipeline, pair: dict, material: str, images: dict) -> dict:
    """One patch call for one material description."""
    system = prompt_io.strip_comments(pair["system"])
    user = prompt_io.substitute(
        prompt_io.strip_comments(pair["user"]), {"material": material}
    )
    result = stages.call_qwen(
        pipeline, system, user, [images["active"]], OPERATIONALIZE_TOKENS
    )
    parsed = result["parsed"]
    return {
        "material": material,
        "parsed": parsed,
        "raw": result["raw"],
        "problems": operationalization_problems(parsed, material),
        "status": str((parsed or {}).get("status") or "").strip().lower(),
        "targets": localization_targets(parsed),
    }


def locate_and_measure(segmenter, images: dict, patch: dict) -> dict:
    """Run SAM for every localization target, then measure the union.

    When a material needs several targets, or when SAM returns several
    instances of one target, every mask is kept and the union is what gets
    measured. That rule is generic and applied identically everywhere; each
    instance stays in the record so the ambiguity is visible rather than hidden.
    """
    from data.palette import _FIG_RESIZE

    records, statuses = [], []
    union = np.zeros((_FIG_RESIZE, _FIG_RESIZE), dtype=bool)
    if patch["status"] == "unusable" or not patch["targets"]:
        return {
            "targets": [],
            "status": "unusable" if patch["status"] == "unusable" else "no_mask",
            "mask": union,
            "mask_pixels": 0,
            "colour": None,
        }

    for target in patch["targets"]:
        result = stages.spatialize(segmenter, images, [target], resolution="working")
        status = segmentation_status(result)
        statuses.append(status)
        for item in result.get("masks") or []:
            union |= item["mask"]
        records.append({
            "target": target,
            "status": status,
            "accepted_phrase": result.get("accepted_phrase"),
            "mask_count": len(result.get("masks") or []),
            "instances": [
                {key: value for key, value in item.items() if key != "mask"}
                for item in result.get("masks") or []
            ],
            "attempts": [
                {key: value for key, value in attempt.items() if key != "masks"}
                for attempt in result.get("attempts") or []
            ],
        })

    status = worst_status(statuses)
    pixels = int(union.sum())
    return {
        "targets": records,
        "status": status,
        "mask": union,
        "mask_pixels": pixels,
        "coverage": round(float(union.mean()), 5),
        "colour": measure(images["working_rgb"], union) if pixels else None,
    }


def run_one(pal: str, entry: dict, project: Path, version: str, pair: dict,
            pipeline, segmenter, out_root: Path) -> dict:
    """Collect, operationalize, segment, measure and persist one image."""
    from scripts.palette_lab import e8_review_sheet as sheet

    out_dir = out_root / pal
    out_dir.mkdir(parents=True, exist_ok=True)
    corpus_dir = project / "outputs" / "tests" / CORPUS_SPACE / "corpus"
    images = stages.prepare_images(corpus_dir / entry["corpus_image"])
    images["active"].save(out_dir / "01-source.jpg", quality=95)

    frozen = _frozen(project, pal)
    choices = mark_convergence(collect_hypotheses(
        frozen["narratology"], frozen["carrier"], frozen["articulation"],
        entry.get("production_palette"),
    ))

    wanted = []
    for key in sorted(choices, key=int):
        if choices[key]["strategy"] == "control":
            continue
        if not choices[key].get("active"):
            continue
        wanted.extend(choices[key].get("materials") or [])
    unique = {}
    for material in wanted:
        unique.setdefault(normalise_material(material), material)

    patches = {}
    for material in unique.values():
        print(f"    operationalize: {material[:58]}", flush=True)
        patches[normalise_material(material)] = operationalize(
            pipeline, pair, material, images
        )
    _write_json(out_dir / "02-operationalization.json", {
        "id": pal,
        "purpose": (
            "Minimal translation from frozen material descriptions to "
            "segmentation targets. Patch-only: the model was shown one material "
            "and the image, never the upstream reading or verdicts."
        ),
        "prompts": {"system": pair["system_file"], "user": pair["user_file"]},
        "materials": [
            {key: value for key, value in patch.items() if key != "raw"}
            for patch in patches.values()
        ],
    })

    measured = {}
    for key, patch in patches.items():
        print(f"    segment: {patch['material'][:58]}", flush=True)
        measured[key] = locate_and_measure(segmenter, images, patch)

    for key in sorted(choices, key=int):
        choice = choices[key]
        if choice["strategy"] == "control":
            continue
        records, colours = [], []
        for material in choice.get("materials") or []:
            found = measured.get(normalise_material(material))
            patch = patches.get(normalise_material(material), {})
            if found is None:
                records.append(None)
                colours.append(None)
                continue
            records.append({
                "material": material,
                "operationalization": patch.get("parsed"),
                "segmentation": {
                    "status": found["status"],
                    "mask_pixels": found["mask_pixels"],
                    "coverage": found.get("coverage"),
                    "targets": found["targets"],
                },
            })
            colours.append(found["colour"])
        choice["measurement"] = records
        choice["colours"] = colours
        if choice.get("active") and any(colour is None for colour in colours):
            choice["measurement_incomplete"] = True

    sheet.save_masks(images, choices, measured, out_dir)
    sheet_path = out_dir / "04-review-sheet.jpg"
    sheet.render_sheet(pal, entry, images, frozen["narratology"], choices,
                       measured, sheet_path)

    manifest = {
        "id": pal,
        "stage": STAGE,
        "branch": EXPERIMENT,
        "space": SPACE,
        "prompt_version": version,
        "created": datetime.now(timezone.utc).isoformat(),
        "source_image": f"{CORPUS_SPACE}/corpus/{entry['corpus_image']}",
        "frozen_inputs": frozen["paths"],
        "narratology": _narratology_summary(frozen["narratology"]),
        "measurement_method": {
            "representative_colour": "mean sRGB of the masked pixels, converted to CIELAB",
            "implementation": "scripts.palette_lab.two_color._measure_mask",
            "resolution": "production working image (256x256, letterbox-cropped)",
            "mask_rule": "union of every returned instance of every localization target",
            "broad_mask_flag": BROAD_COVERAGE,
            "weighting": "none — no chroma bonus, no area weighting, no salience, no delta-E gate",
        },
        "models": {"operationalizer": MODEL, "segmenter": "sam3"},
        "choices": {
            key: {name: value for name, value in choice.items() if name != "mask"}
            for key, choice in choices.items()
        },
        "review_sheet": sheet_path.name,
    }
    _write_json(out_dir / "00-manifest.json", manifest)
    _write_json(out_dir / "05-review-record.json", review_template(pal, choices, version))
    return manifest


def _narratology_summary(reading: dict) -> dict:
    stands_out = (reading.get("what_stands_out") or [{}])[0]
    cue = reading.get("strongest_cue") or {}
    return {
        "stands_out": stands_out.get("entity"),
        "against": stands_out.get("against"),
        "cue": cue.get("visible_cue"),
        "distinction": (reading.get("organizing_distinctions") or [{}])[0].get("distinction"),
    }


def pilot(project_path: str, *, version: str = LATEST,
          only: list[str] | None = None) -> Path:
    """Run the end-to-end measured review over the four calibration images."""
    from data.annotate import _load_text_generation_pipeline
    from services.silhouette import load_sam_model

    from scripts.palette_lab import e8_review_sheet as sheet

    project = Path(project_path)
    corpus_dir = project / "outputs" / "tests" / CORPUS_SPACE / "corpus"
    out_root = project / "outputs" / "tests" / SPACE / EXPERIMENT / f"{STAGE}-{version}"
    out_root.mkdir(parents=True, exist_ok=True)
    corpus = {
        record["id"]: record
        for record in _read_json(corpus_dir / "corpus-manifest.json")["images"]
    }
    selected = list(only or DIAGNOSTIC)
    unexpected = set(selected) - set(PILOT)
    if unexpected:
        raise ValueError(f"This branch is restricted to the frozen pilot: {sorted(unexpected)}")
    system_file, user_file = PROMPT_FILES[version]
    pair = prompt_io.load_pair(
        str(project), ROLE, system_file=system_file, user_file=user_file,
    )

    print(f"loading {MODEL} \u2026", flush=True)
    pipeline = _load_text_generation_pipeline(str(project), MODEL)
    print("loading SAM3 \u2026", flush=True)
    segmenter, sam_name, device = load_sam_model(str(project), "sam3")

    manifests = []
    try:
        for pal in selected:
            print(f"[E8d] {pal}", flush=True)
            manifests.append(run_one(
                pal, corpus[pal], project, version, pair, pipeline, segmenter, out_root,
            ))
    finally:
        del segmenter
        _free_cuda()

    _write_json(out_root / "review-report.json", {
        "branch": EXPERIMENT,
        "stage": STAGE,
        "created": datetime.now(timezone.utc).isoformat(),
        "purpose": (
            "First end-to-end measured palette review of the narratological "
            "branch. Four calibration images, up to four review choices each. "
            "No aesthetic scorer, no image-specific tuning, no winner selected."
        ),
        "models": {"operationalizer": MODEL, "segmenter": sam_name, "device": device},
        "prompt_version": version,
        "prompts": {"system": pair["system_file"], "user": pair["user_file"]},
        "frozen_sources": [NARRATOLOGY_STAGE, CARRIER_STAGE, ARTICULATION_STAGE],
        "control_method": CONTROL_METHOD,
        "images": {
            manifest["id"]: {
                "narratology": manifest["narratology"],
                "choices": {
                    key: {
                        "strategy": choice["strategy"],
                        "active": choice["active"],
                        "materials": choice.get("materials"),
                        "converges_with": choice.get("converges_with"),
                        "hex": [
                            (colour or {}).get("hex")
                            for colour in choice.get("colours") or []
                        ],
                        "segmentation": [
                            (record or {}).get("segmentation", {}).get("status")
                            for record in choice.get("measurement") or []
                        ],
                    }
                    for key, choice in manifest["choices"].items()
                },
            }
            for manifest in manifests
        },
    })
    sheet.render_contact_sheet(manifests, out_root)
    print(f"[E8d] measured review over {len(manifests)} images -> {out_root}")
    return out_root


def main() -> None:
    parser = argparse.ArgumentParser(
        description="E8 end-to-end measured palette review"
    )
    parser.add_argument("mode", choices=("pilot",))
    parser.add_argument("--project", required=True, help="Crossing project root")
    parser.add_argument("--version", default=LATEST, choices=tuple(PROMPT_FILES))
    parser.add_argument("--only", nargs="*", help="Restrict to these pilot ids")
    args = parser.parse_args()
    pilot(args.project, version=args.version, only=args.only)


if __name__ == "__main__":
    main()
