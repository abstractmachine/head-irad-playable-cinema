"""E8 stage D pilot — can a curatorial relation become honest measurement requests?

    uv run python -m scripts.palette_lab.e8_spatializer pilot --project /path

One call per frozen critic-v3 relation. The spatializer sees the image, that one
relation, and the stage A propositions it was built from. It is blind to the E6
human review, E5 selections, E7 rankings, previous SAM results, candidate
inventories and measured colour.

Nothing here runs SAM or measures anything. The output is a specification to be
inspected before any segmentation is attempted.
"""

from __future__ import annotations

import argparse
import re
from datetime import datetime, timezone
from pathlib import Path

from scripts.palette_lab import prompts as prompt_io
from scripts.palette_lab import stages
from scripts.palette_lab.e4_pilot import CORPUS_SPACE, PILOT, SPACE
from scripts.palette_lab.e5_two_color import _read_json, _write_json
from scripts.palette_lab.e8_critic import CURATOR_STAGE
from scripts.palette_lab.e8_curator import (
    DIAGNOSTIC, EXPERIMENT, MAX_NEW_TOKENS, MODEL,
)


ROLE = "spatializer"
PROMPT_FILES = {
    "v1": ("prompts/palettes/palette-spatializer-system-2026-09-15-v1.txt",
           "prompts/palettes/palette-spatializer-user-2026-09-15-v1.txt"),
}
LATEST = "v1"
CRITIC_STAGE = "stage-b-v3"

RELATION_TO_CONCEPT = (
    "whole", "synonym", "part", "broader_field", "material", "proxy",
    "alternative_localization",
)
SIDE_TEXT_FIELDS = ("concept", "measurable_expectations", "interpretive_content")
RELATION_TEXT_FIELDS = ("relational_term", "measurable_proxies", "interpretive_content")
REFERENT_FIELDS = ("phrase", "relation_to_concept", "covers")
# Ids are legitimate in prose; digits otherwise are not.
_REFERENCE_ID = re.compile(r"\b[PR]\d+\b")
# A referent is meant to be a noun phrase a segmenter can act on. Length alone is
# a weak signal; a conjunction is the real one, since "X and Y" asks segmentation
# to return one mask for two things.
MAX_REFERENT_WORDS = 8
_CONJUNCTIVE = re.compile(r"(?<![\w-])(and|as well as|together with)(?![\w-])", re.IGNORECASE)


def _prose_has_number(value: str) -> bool:
    return bool(re.search(r"\d", _REFERENCE_ID.sub("", str(value or ""))))


def render_relation(relation: dict, propositions: list[dict]) -> str:
    """Lay out one critic relation plus the stage A propositions behind it."""
    by_id = {str(item.get("id")): item for item in propositions}
    lines = [
        f"Relation {relation.get('id', '?')}",
        f"  side A: {relation.get('side_a', '')}",
        f"  side B: {relation.get('side_b', '')}",
        f"  why these are one relation: {relation.get('why_these_are_one_relation', '')}",
        f"  curatorial significance: {relation.get('curatorial_significance', '')}",
        f"  two-colour relevance: {relation.get('two_colour_relevance', '')}",
        f"  figure side: {relation.get('figure_side', '')}",
        f"  orientation reasoning: {relation.get('orientation_reasoning', '')}",
        f"  the critic's uncertainty: {relation.get('uncertainty', '')}",
        "",
        "It was built from these earlier readings of the same frame:",
        "",
    ]
    for argument in relation.get("supporting_arguments") or []:
        identifier = str(argument.get("from_proposition") or "")
        source = by_id.get(identifier, {})
        lines.append(f"  [{identifier}] {source.get('side_a', '')} / {source.get('side_b', '')}")
        lines.append(f"      relationship: {source.get('relationship', '')}")
        lines.append(f"      route to this relation: {argument.get('argument', '')}")
        lines.append("")
    return "\n".join(lines).strip()


def shape_problems(parsed, relation_id: str) -> list[str]:
    """Check the specification's shape without judging its content."""
    if not isinstance(parsed, dict):
        return ["response did not parse as a JSON object"]
    problems = []
    if str(parsed.get("relation_id") or "") != relation_id:
        problems.append(
            f"relation_id {parsed.get('relation_id')!r} does not match {relation_id!r}"
        )
    if not str(parsed.get("uncertainty") or "").strip():
        problems.append("uncertainty is empty")

    for name in ("side_a", "side_b"):
        side = parsed.get(name)
        if not isinstance(side, dict):
            problems.append(f"{name} is missing")
            continue
        missing = [
            field for field in SIDE_TEXT_FIELDS
            if not str(side.get(field) or "").strip()
        ]
        if missing:
            problems.append(f"{name} missing {', '.join(missing)}")
        if _prose_has_number(side.get("measurable_expectations")):
            problems.append(f"{name} measurable_expectations contains a number")
        referents = side.get("referents")
        if not isinstance(referents, list):
            problems.append(f"{name} referents is not a list")
            continue
        for index, referent in enumerate(referents, start=1):
            if not isinstance(referent, dict):
                problems.append(f"{name} referent {index} is not an object")
                continue
            blank = [
                field for field in REFERENT_FIELDS
                if not str(referent.get(field) or "").strip()
            ]
            if blank:
                problems.append(f"{name} referent {index} missing {', '.join(blank)}")
            kind = str(referent.get("relation_to_concept") or "")
            if kind and kind not in RELATION_TO_CONCEPT:
                problems.append(
                    f"{name} referent {index} relation_to_concept {kind!r} "
                    f"is not one of {RELATION_TO_CONCEPT}"
                )
            phrase = str(referent.get("phrase") or "")
            if _CONJUNCTIVE.search(phrase):
                problems.append(
                    f"{name} referent {index} is conjunctive; segmentation cannot "
                    f"return one region for several things"
                )
            elif len(phrase.split()) > MAX_REFERENT_WORDS:
                problems.append(
                    f"{name} referent {index} phrase is long for a segmentation request"
                )

    relation = parsed.get("relation")
    if not isinstance(relation, dict):
        problems.append("relation is missing")
    else:
        missing = [
            field for field in RELATION_TEXT_FIELDS
            if not str(relation.get(field) or "").strip()
        ]
        if missing:
            problems.append(f"relation missing {', '.join(missing)}")
        if _prose_has_number(relation.get("measurable_proxies")):
            problems.append("relation measurable_proxies contains a number")
    return problems


def _referent_kinds(side: dict) -> dict[str, int]:
    kinds: dict[str, int] = {}
    for referent in side.get("referents") or []:
        if isinstance(referent, dict):
            kind = str(referent.get("relation_to_concept") or "?")
            kinds[kind] = kinds.get(kind, 0) + 1
    return kinds


def summarise(parsed, relation: dict) -> dict:
    """Describe the translation so it can be inspected without reading prose.

    ``compound`` means the concept needed more than one referent; it is the
    signal for the distributed-entity problem E6-E8 kept running into.
    """
    parsed = parsed if isinstance(parsed, dict) else {}
    sides = {}
    for name in ("side_a", "side_b"):
        side = parsed.get(name) if isinstance(parsed.get(name), dict) else {}
        referents = [r for r in (side.get("referents") or []) if isinstance(r, dict)]
        sides[name] = {
            "concept": side.get("concept"),
            "critic_side": relation.get(name),
            "referents": len(referents),
            "phrases": [str(r.get("phrase") or "") for r in referents],
            "referent_kinds": _referent_kinds(side),
            "compound": len(referents) > 1,
            "unlocatable": len(referents) == 0,
            "has_interpretive_content": bool(
                str(side.get("interpretive_content") or "").strip()
            ),
        }
    relation_block = parsed.get("relation") if isinstance(parsed.get("relation"), dict) else {}
    return {
        "relation_id": parsed.get("relation_id"),
        "sides": sides,
        "relational_term": relation_block.get("relational_term"),
        "has_measurable_proxies": bool(
            str(relation_block.get("measurable_proxies") or "").strip()
        ),
        "has_relation_interpretive_content": bool(
            str(relation_block.get("interpretive_content") or "").strip()
        ),
        "distinct_phrases": len({
            phrase for side in sides.values() for phrase in side["phrases"]
        }),
    }


def run_one(pal: str, entry: dict, project: Path, version: str, pair: dict,
            pipeline, out_root: Path) -> dict:
    out_dir = out_root / pal
    out_dir.mkdir(parents=True, exist_ok=True)
    corpus_dir = project / "outputs" / "tests" / CORPUS_SPACE / "corpus"
    experiment_root = project / "outputs" / "tests" / SPACE / EXPERIMENT
    critic = _read_json(experiment_root / CRITIC_STAGE / pal / "05-critic.json")["parsed"]
    curator = _read_json(
        experiment_root / CURATOR_STAGE / pal / "03-curator.json"
    )["parsed"]
    propositions = curator.get("propositions") or []
    images = stages.prepare_images(corpus_dir / entry["corpus_image"])

    system = prompt_io.strip_comments(pair["system"])
    records = []
    for relation in critic.get("relations") or []:
        relation_id = str(relation.get("id") or "?")
        user = prompt_io.substitute(
            prompt_io.strip_comments(pair["user"]),
            {"relation": render_relation(relation, propositions)},
        )
        (out_dir / f"06-{relation_id}-user-prompt.txt").write_text(user, encoding="utf-8")
        result = stages.call_qwen(
            pipeline, system, user, [images["active"]], MAX_NEW_TOKENS
        )
        (out_dir / f"07-{relation_id}-raw.txt").write_text(
            result["raw"] or "", encoding="utf-8"
        )
        parsed = result["parsed"]
        problems = shape_problems(parsed, relation_id)
        records.append({
            "relation_id": relation_id,
            "provenance": {
                "image": f"{CORPUS_SPACE}/corpus/{entry['corpus_image']}",
                "stage_a": f"{CURATOR_STAGE}/{pal}/03-curator.json",
                "stage_a_propositions": relation.get("from_propositions") or [],
                "stage_b": f"{CRITIC_STAGE}/{pal}/05-critic.json",
                "stage_b_relation": {
                    "side_a": relation.get("side_a"),
                    "side_b": relation.get("side_b"),
                    "figure_side": relation.get("figure_side"),
                },
            },
            "parsed": parsed,
            "shape_problems": problems,
            "summary": summarise(parsed, relation),
            "device": result["device"],
        })

    _write_json(out_dir / "08-spatialization.json", {
        "id": pal,
        "stage": "D-spatializer",
        "prompt_version": version,
        "blind": True,
        "note": "Measurement specifications only. No segmentation was run.",
        "records": records,
    })
    manifest = {
        "id": pal,
        "status": "ok" if records else "no relations",
        "experiment": EXPERIMENT,
        "space": SPACE,
        "stage": "D-spatializer",
        "prompt_version": version,
        "created": datetime.now(timezone.utc).isoformat(),
        "inputs": {
            "image": f"{CORPUS_SPACE}/corpus/{entry['corpus_image']}",
            "stage_a": f"{CURATOR_STAGE}/{pal}/03-curator.json",
            "stage_b": f"{CRITIC_STAGE}/{pal}/05-critic.json",
            "relations": [record["relation_id"] for record in records],
        },
        "blindness": {
            "sees": ["source image", "one critic relation", "its stage A provenance",
                     "spatializer system prompt", "spatializer user prompt"],
            "withheld": [
                "film title/year/director", "E5 selections", "E6 human review",
                "E7 rankings", "previous SAM results", "candidate inventories",
                "hierarchy regions", "measured LAB values",
            ],
        },
        "model": {
            "name": MODEL,
            "max_new_tokens": MAX_NEW_TOKENS,
            "do_sample": False,
            "device": records[0]["device"] if records else None,
        },
        "prompts": {"system": pair["system_file"], "user": pair["user_file"]},
        "shape_problems": {
            record["relation_id"]: record["shape_problems"] for record in records
        },
    }
    _write_json(out_dir / "00-manifest.json", manifest)
    return manifest


def pilot(project_path: str, *, version: str = LATEST,
          only: list[str] | None = None) -> Path:
    """Translate every frozen critic-v3 relation for the diagnostic subset."""
    from data.annotate import _load_text_generation_pipeline

    project = Path(project_path)
    corpus_dir = project / "outputs" / "tests" / CORPUS_SPACE / "corpus"
    out_root = (
        project / "outputs" / "tests" / SPACE / EXPERIMENT / f"stage-d-{version}"
    )
    out_root.mkdir(parents=True, exist_ok=True)
    corpus = {
        entry["id"]: entry
        for entry in _read_json(corpus_dir / "corpus-manifest.json")["images"]
    }
    selected = list(only or DIAGNOSTIC)
    unexpected = set(selected) - set(PILOT)
    if unexpected:
        raise ValueError(f"E8 is restricted to the frozen pilot: {sorted(unexpected)}")
    system_file, user_file = PROMPT_FILES[version]
    pair = prompt_io.load_pair(
        str(project), ROLE, system_file=system_file, user_file=user_file,
    )

    print(f"loading {MODEL} \u2026", flush=True)
    pipeline = _load_text_generation_pipeline(str(project), MODEL)
    manifests = []
    for pal in selected:
        print(f"[E8] spatializer {pal}", flush=True)
        manifests.append(
            run_one(pal, corpus[pal], project, version, pair, pipeline, out_root)
        )

    _write_json(out_root / "spatialization-report.json", {
        "experiment": EXPERIMENT,
        "stage": "D-spatializer",
        "created": datetime.now(timezone.utc).isoformat(),
        "purpose": (
            "Translation pilot only. No SAM run, no CV audit, no comparison with "
            "the human review."
        ),
        "model": MODEL,
        "prompt_version": version,
        "stage_a": CURATOR_STAGE,
        "stage_b": CRITIC_STAGE,
        "prompts": {"system": pair["system_file"], "user": pair["user_file"]},
        "images": {
            manifest["id"]: {
                "relations": manifest["inputs"]["relations"],
                "shape_problems": manifest["shape_problems"],
            }
            for manifest in manifests
        },
    })
    print(f"[E8] stage D pilot over {len(manifests)} images -> {out_root}")
    return out_root


def main() -> None:
    parser = argparse.ArgumentParser(description="E8 stage D spatializer pilot")
    parser.add_argument("mode", choices=("pilot",))
    parser.add_argument("--project", required=True, help="Crossing project root")
    parser.add_argument("--version", default=LATEST, choices=tuple(PROMPT_FILES))
    parser.add_argument("--only", nargs="*", help="Restrict to these pilot ids")
    args = parser.parse_args()
    pilot(args.project, version=args.version, only=args.only or None)


if __name__ == "__main__":
    main()
