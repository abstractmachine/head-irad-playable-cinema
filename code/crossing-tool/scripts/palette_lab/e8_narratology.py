"""E8 parallel branch — what does the frame ask the spectator to distinguish?

    uv run python -m scripts.palette_lab.e8_narratology pilot --project /path

A separate exploratory branch, not a revision of the frozen curator/critic/
spatializer/auditor chain and not connected to it. The model sees the source
image and the narratological prompt, and nothing else: no E4-E8 output, no mask,
no candidate colour, no annotation, no film metadata, no human review.

Nothing here segments or measures anything.
"""

from __future__ import annotations

import argparse
import re
from datetime import datetime, timezone
from pathlib import Path

from scripts.palette_lab import prompts as prompt_io
from scripts.palette_lab import render as R
from scripts.palette_lab import stages
from scripts.palette_lab.e4_pilot import CORPUS_SPACE, PILOT, SPACE
from scripts.palette_lab.e5_two_color import _read_json, _write_json
from scripts.palette_lab.e8_curator import DIAGNOSTIC, MAX_NEW_TOKENS, MODEL


EXPERIMENT = "E8-narratological-branch"
ROLE = "narratology"
PROMPT_FILES = {
    "v1": ("prompts/palettes/palette-narratology-system-2026-09-15-v1.txt",
           "prompts/palettes/palette-narratology-user-2026-09-15-v1.txt"),
}
LATEST = "v1"

STANDS_OUT_FIELDS = ("entity", "against", "what_makes_it_stand_out", "why_it_matters")
CUE_FIELDS = ("visible_cue", "spectator_inference", "genre_context", "critical_context")
DISTINCTION_FIELDS = (
    "distinction", "side_a", "side_b", "narratological_reason", "formal_carrier",
    "two_colour_potential", "what_two_colours_would_lose",
)
TWO_COLOUR_POTENTIAL = ("yes", "partial", "no")
# Only these commit to a colour pair, so only these need the roles named.
POTENTIAL_NEEDING_ROLES = ("yes", "partial")
MAX_STANDS_OUT = 3
# Measurement vocabulary the prompt rules out; this branch is upstream of pixels.
_MEASUREMENT_WORDS = re.compile(
    r"\b(pixel|pixels|mask|masks|centroid|bounding box|delta e|rgb|lab value)\b",
    re.IGNORECASE,
)


def _has_number(value: str) -> bool:
    return bool(re.search(r"\d", str(value or "")))


def _mentions_measurement(value: str) -> bool:
    return bool(_MEASUREMENT_WORDS.search(str(value or "")))


def shape_problems(parsed) -> list[str]:
    """Check the reading's shape without judging its interpretation."""
    if not isinstance(parsed, dict):
        return ["response did not parse as a JSON object"]
    problems = []
    for field in ("overall_reading", "uncertainty"):
        if not str(parsed.get(field) or "").strip():
            problems.append(f"{field} is empty")

    stands_out = parsed.get("what_stands_out")
    if not isinstance(stands_out, list) or not stands_out:
        problems.append("no what_stands_out")
        stands_out = []
    if len(stands_out) > MAX_STANDS_OUT:
        problems.append(f"{len(stands_out)} what_stands_out entries, expected at most {MAX_STANDS_OUT}")
    for index, entry in enumerate(stands_out, start=1):
        if not isinstance(entry, dict):
            problems.append(f"what_stands_out {index} is not an object")
            continue
        missing = [
            field for field in STANDS_OUT_FIELDS
            if not str(entry.get(field) or "").strip()
        ]
        if missing:
            problems.append(f"what_stands_out {index} missing {', '.join(missing)}")

    cue = parsed.get("strongest_cue")
    if not isinstance(cue, dict):
        problems.append("strongest_cue is missing")
    else:
        missing = [field for field in CUE_FIELDS if not str(cue.get(field) or "").strip()]
        if missing:
            problems.append(f"strongest_cue missing {', '.join(missing)}")

    distinctions = parsed.get("organizing_distinctions")
    if not isinstance(distinctions, list) or not distinctions:
        problems.append("no organizing_distinctions")
        distinctions = []
    for index, entry in enumerate(distinctions, start=1):
        if not isinstance(entry, dict):
            problems.append(f"organizing_distinction {index} is not an object")
            continue
        missing = [
            field for field in DISTINCTION_FIELDS
            if not str(entry.get(field) or "").strip()
        ]
        if missing:
            problems.append(f"organizing_distinction {index} missing {', '.join(missing)}")
        potential = str(entry.get("two_colour_potential") or "").strip().lower()
        if potential and potential not in TWO_COLOUR_POTENTIAL:
            problems.append(
                f"organizing_distinction {index} two_colour_potential {potential!r} "
                f"is not one of {TWO_COLOUR_POTENTIAL}"
            )
        roles = [
            str(entry.get(field) or "").strip()
            for field in ("colour_role_a", "colour_role_b")
        ]
        if potential in POTENTIAL_NEEDING_ROLES and not all(roles):
            problems.append(
                f"organizing_distinction {index} claims {potential!r} colour potential "
                f"without naming both colour roles"
            )
        if potential == "no" and any(roles):
            problems.append(
                f"organizing_distinction {index} rules out two colours yet names roles"
            )

    for field, value in _prose_fields(parsed):
        if _has_number(value):
            problems.append(f"{field} contains a number")
        if _mentions_measurement(value):
            problems.append(f"{field} refers to measurement")
    return problems


def _prose_fields(parsed: dict):
    for index, entry in enumerate(parsed.get("what_stands_out") or [], start=1):
        if isinstance(entry, dict):
            for field in STANDS_OUT_FIELDS:
                yield f"what_stands_out {index} {field}", entry.get(field)
    cue = parsed.get("strongest_cue")
    if isinstance(cue, dict):
        for field in CUE_FIELDS:
            yield f"strongest_cue {field}", cue.get(field)
    for index, entry in enumerate(parsed.get("organizing_distinctions") or [], start=1):
        if isinstance(entry, dict):
            for field in ("distinction", "narratological_reason", "formal_carrier"):
                yield f"organizing_distinction {index} {field}", entry.get(field)


def summarise(parsed) -> dict:
    """Tally the reading so the four frames can be scanned side by side."""
    parsed = parsed if isinstance(parsed, dict) else {}
    stands_out = [e for e in (parsed.get("what_stands_out") or []) if isinstance(e, dict)]
    distinctions = [
        e for e in (parsed.get("organizing_distinctions") or []) if isinstance(e, dict)
    ]
    potentials = [
        str(e.get("two_colour_potential") or "").strip().lower() for e in distinctions
    ]
    cue = parsed.get("strongest_cue") if isinstance(parsed.get("strongest_cue"), dict) else {}
    return {
        "stands_out_entries": len(stands_out),
        "stands_out_entities": [str(e.get("entity") or "") for e in stands_out],
        "counterfields": [str(e.get("against") or "") for e in stands_out],
        "distinctions": len(distinctions),
        "distinction_labels": [str(e.get("distinction") or "") for e in distinctions],
        "formal_carriers": [str(e.get("formal_carrier") or "") for e in distinctions],
        "two_colour_potential": {
            name: potentials.count(name) for name in TWO_COLOUR_POTENTIAL
            if potentials.count(name)
        },
        "rejects_two_colour": "no" in potentials,
        "strongest_cue": str(cue.get("visible_cue") or ""),
        "names_a_convention": bool(str(cue.get("critical_context") or "").strip()),
    }


def run_one(pal: str, entry: dict, project: Path, version: str, pair: dict,
            pipeline, out_root: Path) -> dict:
    out_dir = out_root / pal
    out_dir.mkdir(parents=True, exist_ok=True)
    corpus_dir = project / "outputs" / "tests" / CORPUS_SPACE / "corpus"
    images = stages.prepare_images(corpus_dir / entry["corpus_image"])
    R.save_source(images, out_dir / "01-source.jpg")

    system = prompt_io.strip_comments(pair["system"])
    user = prompt_io.strip_comments(pair["user"])
    result = stages.call_qwen(pipeline, system, user, [images["active"]], MAX_NEW_TOKENS)

    (out_dir / "02-narratology-raw.txt").write_text(result["raw"] or "", encoding="utf-8")
    parsed = result["parsed"]
    problems = shape_problems(parsed)
    _write_json(out_dir / "03-narratology.json", {
        "id": pal,
        "branch": EXPERIMENT,
        "prompt_version": version,
        "blind": True,
        "parsed": parsed,
        "shape_problems": problems,
        "summary": summarise(parsed),
    })

    manifest = {
        "id": pal,
        "status": "ok" if isinstance(parsed, dict) and parsed.get("what_stands_out") else "unparsed",
        "branch": EXPERIMENT,
        "space": SPACE,
        "prompt_version": version,
        "created": datetime.now(timezone.utc).isoformat(),
        "source": f"{CORPUS_SPACE}/corpus/{entry['corpus_image']}",
        "blindness": {
            "sees": ["source image", "narratology system prompt",
                     "narratology user prompt"],
            "withheld": [
                "film title/year/director", "E4 readings", "E5 selections",
                "E6 human review", "E7 rankings", "E8 curator/critic/spatializer/"
                "auditor output", "SAM masks", "hierarchy regions",
                "candidate inventories", "measured LAB values", "annotations",
            ],
        },
        "model": {
            "name": MODEL,
            "max_new_tokens": MAX_NEW_TOKENS,
            "do_sample": False,
            "device": result["device"],
        },
        "prompts": {"system": pair["system_file"], "user": pair["user_file"]},
        "shape_problems": problems,
        "summary": summarise(parsed),
    }
    _write_json(out_dir / "00-manifest.json", manifest)
    return manifest


def pilot(project_path: str, *, version: str = LATEST,
          only: list[str] | None = None) -> Path:
    """Read the diagnostic frames blind, as narratological distinctions."""
    from data.annotate import _load_text_generation_pipeline

    project = Path(project_path)
    corpus_dir = project / "outputs" / "tests" / CORPUS_SPACE / "corpus"
    out_root = project / "outputs" / "tests" / SPACE / EXPERIMENT / f"narratology-{version}"
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
    manifests = []
    for pal in selected:
        print(f"[E8b] narratology {pal}", flush=True)
        manifests.append(
            run_one(pal, corpus[pal], project, version, pair, pipeline, out_root)
        )

    _write_json(out_root / "narratology-report.json", {
        "branch": EXPERIMENT,
        "created": datetime.now(timezone.utc).isoformat(),
        "purpose": (
            "Exploratory branch. Not connected to the frozen curator/critic/"
            "spatializer/auditor chain. No SAM, no measurement, no comparison "
            "baked into the run."
        ),
        "model": MODEL,
        "prompt_version": version,
        "prompts": {"system": pair["system_file"], "user": pair["user_file"]},
        "images": {
            manifest["id"]: {
                "shape_problems": manifest["shape_problems"],
                "summary": manifest["summary"],
            }
            for manifest in manifests
        },
    })
    print(f"[E8b] narratological pilot over {len(manifests)} images -> {out_root}")
    return out_root


def main() -> None:
    parser = argparse.ArgumentParser(description="E8 narratological branch pilot")
    parser.add_argument("mode", choices=("pilot",))
    parser.add_argument("--project", required=True, help="Crossing project root")
    parser.add_argument("--version", default=LATEST, choices=tuple(PROMPT_FILES))
    parser.add_argument("--only", nargs="*", help="Restrict to these pilot ids")
    args = parser.parse_args()
    pilot(args.project, version=args.version, only=args.only or None)


if __name__ == "__main__":
    main()
