"""E8 stage B pilot — does self-critique find the structure among the readings?

    uv run python -m scripts.palette_lab.e8_critic pilot --project /path

The critic receives the image and the frozen stage A (curation-v3) propositions,
and nothing else: no human review, no E5 selection, no E7 ranking, no mask, no
candidate inventory, no measured colour, no film metadata.

It is not a ranking task. The critic consolidates convergent readings, demotes
relations that do not organize the frame, and keeps genuinely different
organizations apart. Stage C, stage D and the CV audit are not built.
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
from scripts.palette_lab.e8_curator import (
    DIAGNOSTIC, EXPERIMENT, MAX_NEW_TOKENS, MODEL,
)


ROLE = "critic"
PROMPT_FILES = {
    "v1": ("prompts/palettes/palette-critic-system-2026-09-14-v1.txt",
           "prompts/palettes/palette-critic-user-2026-09-14-v1.txt"),
    "v2": ("prompts/palettes/palette-critic-system-2026-09-15-v2.txt",
           "prompts/palettes/palette-critic-user-2026-09-15-v2.txt"),
    "v3": ("prompts/palettes/palette-critic-system-2026-09-15-v3.txt",
           "prompts/palettes/palette-critic-user-2026-09-15-v3.txt"),
}
LATEST = "v3"
CURATOR_STAGE = "stage-a-v3"

# v1 recorded every proposition's fate alongside consolidation; v2 added
# primary/alternative labels; v3 drops both and asks only about propositions no
# relation cites.
SCHEMA_BY_VERSION = {"v1": "classic", "v2": "classified", "v3": "light"}

FIGURE_SIDES = ("side_a", "side_b", "unresolved")
CLASSIFICATIONS = ("primary", "alternative")
DISPOSITIONS_BY_VERSION = {
    "v1": ("rejected", "demoted", "absorbed"),
    "v2": ("rejected", "demoted"),
}
RELATION_TEXT_FIELDS = (
    "id", "side_a", "side_b", "relationship", "why_image_organizing",
    "would_survive_two_colour_reduction", "figure_side", "orientation_reasoning",
    "uncertainty", "alternatives",
)
LIGHT_RELATION_FIELDS = (
    "id", "side_a", "side_b", "why_these_are_one_relation",
    "curatorial_significance", "two_colour_relevance", "figure_side",
    "orientation_reasoning", "uncertainty",
)
# Proposition and relation ids are legitimate in prose; digits otherwise are not.
_REFERENCE_ID = re.compile(r"\b[PR]\d+\b")


def schema_of(version: str) -> str:
    return SCHEMA_BY_VERSION.get(version, SCHEMA_BY_VERSION[LATEST])


def _uses_classification(version: str) -> bool:
    return schema_of(version) == "classified"


def _dispositions(version: str) -> tuple[str, ...]:
    return DISPOSITIONS_BY_VERSION.get(version, DISPOSITIONS_BY_VERSION["v2"])

CURATOR_FIELDS = (
    ("side_a", "side A"),
    ("side_b", "side B"),
    ("relationship", "relationship"),
    ("why_it_organizes_the_frame", "why it organizes the frame"),
    ("formal_basis", "formal basis"),
    ("semantic_basis", "semantic basis"),
    ("spatial_basis", "spatial basis"),
    ("figure_side", "figure side"),
    ("role_reasoning", "role reasoning"),
    ("uncertainty", "uncertainty"),
)


def render_propositions(curator: dict) -> str:
    """Lay out stage A's reading as the critic's only textual evidence.

    Every field stage A emitted is passed through, including its own
    ``shares_regions_with`` declaration and orientation claim. Withholding parts
    of the other curator's argument would be editorialising, and the
    consolidation test is unaffected: stage A declared no shared regions on the
    image where convergence matters most.
    """
    lines = []
    frame = str(curator.get("what_the_frame_is_doing") or "").strip()
    if frame:
        lines.append(f"What the other curator says the frame is doing: {frame}")
        lines.append("")
    for proposition in curator.get("propositions") or []:
        lines.append(f"[{proposition.get('id', '?')}]")
        for key, label in CURATOR_FIELDS:
            value = str(proposition.get(key) or "").strip()
            if value:
                lines.append(f"  {label}: {value}")
        shares = proposition.get("shares_regions_with") or []
        lines.append(
            f"  shares regions with: {', '.join(shares) if shares else 'none declared'}"
        )
        lines.append("")
    return "\n".join(lines).strip()


def _prose_has_number(value: str) -> bool:
    return bool(re.search(r"\d", _REFERENCE_ID.sub("", str(value or ""))))


def shape_problems(parsed, curator_ids: list[str], version: str = LATEST) -> list[str]:
    """Check the critic's shape for whichever schema this prompt version uses."""
    if schema_of(version) == "light":
        return _light_problems(parsed, curator_ids)
    return _classic_problems(parsed, curator_ids, version)


def _light_problems(parsed, curator_ids: list[str]) -> list[str]:
    """Validate the v3 schema, where consolidation is the only fate recorded.

    A proposition supporting two genuinely different relations is allowed and
    reported by ``transition``; only unaccounted, contradictory or unknown ids
    are problems.
    """
    if not isinstance(parsed, dict):
        return ["response did not parse as a JSON object"]
    relations = parsed.get("relations")
    if not isinstance(relations, list) or not relations:
        return ["no relations array"]
    problems = []
    for field in ("reading_of_the_set", "set_judgment"):
        if not str(parsed.get(field) or "").strip():
            problems.append(f"{field} is empty")

    known = set(curator_ids)
    used: list[str] = []
    for index, relation in enumerate(relations, start=1):
        missing = [
            field for field in LIGHT_RELATION_FIELDS
            if not str(relation.get(field) or "").strip()
        ]
        if missing:
            problems.append(f"relation {index} missing {', '.join(missing)}")
        side = str(relation.get("figure_side") or "")
        if side and side not in FIGURE_SIDES:
            problems.append(
                f"relation {index} figure_side {side!r} is not one of {FIGURE_SIDES}"
            )
        for field in ("curatorial_significance", "two_colour_relevance",
                      "why_these_are_one_relation"):
            if _prose_has_number(relation.get(field)):
                problems.append(f"relation {index} {field} contains a number")
        sources = relation.get("from_propositions")
        if not isinstance(sources, list) or not sources:
            problems.append(f"relation {index} has no from_propositions")
            sources = []
        unknown = [item for item in sources if item not in known]
        if unknown:
            problems.append(f"relation {index} cites unknown propositions {unknown}")
        used.extend(item for item in sources if item in known)
        arguments = relation.get("supporting_arguments")
        if not isinstance(arguments, list) or not arguments:
            problems.append(f"relation {index} has no supporting_arguments")
        else:
            cited = {
                str(argument.get("from_proposition") or "")
                for argument in arguments if isinstance(argument, dict)
            }
            uncovered = [item for item in sources if item not in cited]
            if uncovered:
                problems.append(
                    f"relation {index} consolidates {uncovered} without preserving "
                    f"their distinct argument"
                )

    unused_rows = parsed.get("unused_propositions")
    if not isinstance(unused_rows, list):
        problems.append("unused_propositions is not a list")
        unused_rows = []
    unused: list[str] = []
    for index, row in enumerate(unused_rows, start=1):
        identifier = str(row.get("id") or "")
        if identifier not in known:
            problems.append(f"unused {index} cites unknown proposition {identifier!r}")
        else:
            unused.append(identifier)
        if not str(row.get("reason") or "").strip():
            problems.append(f"unused {index} has no reason")

    contradictory = sorted(set(used) & set(unused))
    if contradictory:
        problems.append(f"propositions both used and unused: {contradictory}")
    unaccounted = sorted(known - set(used) - set(unused))
    if unaccounted:
        problems.append(f"propositions never accounted for: {unaccounted}")
    return problems


def _classic_problems(parsed, curator_ids: list[str], version: str) -> list[str]:
    """Validate the v1/v2 schema, where every proposition's fate was recorded."""
    if not isinstance(parsed, dict):
        return ["response did not parse as a JSON object"]
    relations = parsed.get("relations")
    if not isinstance(relations, list) or not relations:
        return ["no relations array"]
    problems = []
    if not str(parsed.get("reading_of_the_set") or "").strip():
        problems.append("reading_of_the_set is empty")

    known = set(curator_ids)
    seen: list[str] = []
    for index, relation in enumerate(relations, start=1):
        missing = [
            field for field in RELATION_TEXT_FIELDS
            if not str(relation.get(field) or "").strip()
        ]
        if missing:
            problems.append(f"relation {index} missing {', '.join(missing)}")
        if _uses_classification(version):
            classification = str(relation.get("classification") or "")
            if classification not in CLASSIFICATIONS:
                problems.append(
                    f"relation {index} classification {classification!r} "
                    f"is not one of {CLASSIFICATIONS}"
                )
        side = str(relation.get("figure_side") or "")
        if side and side not in FIGURE_SIDES:
            problems.append(
                f"relation {index} figure_side {side!r} is not one of {FIGURE_SIDES}"
            )
        for field in ("relationship", "why_image_organizing",
                      "would_survive_two_colour_reduction"):
            if _prose_has_number(relation.get(field)):
                problems.append(f"relation {index} {field} contains a number")
        sources = relation.get("from_propositions")
        if not isinstance(sources, list) or not sources:
            problems.append(f"relation {index} has no from_propositions")
            sources = []
        unknown = [item for item in sources if item not in known]
        if unknown:
            problems.append(f"relation {index} cites unknown propositions {unknown}")
        seen.extend(item for item in sources if item in known)
        arguments = relation.get("supporting_arguments")
        if not isinstance(arguments, list) or not arguments:
            problems.append(f"relation {index} has no supporting_arguments")
        else:
            cited = {
                str(argument.get("from_proposition") or "")
                for argument in arguments if isinstance(argument, dict)
            }
            uncovered = [item for item in sources if item not in cited]
            if uncovered:
                problems.append(
                    f"relation {index} consolidates {uncovered} without preserving "
                    f"their distinct argument"
                )

    demoted = parsed.get("demoted")
    if not isinstance(demoted, list):
        problems.append("demoted is not a list")
        demoted = []
    for index, row in enumerate(demoted, start=1):
        identifier = str(row.get("from_proposition") or "")
        if identifier not in known:
            problems.append(f"demoted {index} cites unknown proposition {identifier!r}")
        else:
            seen.append(identifier)
        if str(row.get("disposition") or "") not in _dispositions(version):
            problems.append(
                f"demoted {index} disposition {row.get('disposition')!r} "
                f"is not one of {_dispositions(version)}"
            )
        if not str(row.get("reason") or "").strip():
            problems.append(f"demoted {index} has no reason")

    unaccounted = sorted(known - set(seen))
    if unaccounted:
        problems.append(f"propositions never accounted for: {unaccounted}")
    duplicated = sorted({item for item in seen if seen.count(item) > 1})
    if duplicated:
        problems.append(f"propositions accounted for more than once: {duplicated}")
    return problems


def _restated(relation: dict, curator_by_id: dict) -> bool:
    """True when the relation's sides are not lifted from any source proposition."""
    def words(text: str) -> set[str]:
        return {word for word in re.findall(r"[a-z]+", str(text).lower()) if len(word) > 3}

    sides = words(relation.get("side_a")) | words(relation.get("side_b"))
    for identifier in relation.get("from_propositions") or []:
        source = curator_by_id.get(identifier)
        if not source:
            continue
        original = words(source.get("side_a")) | words(source.get("side_b"))
        union = sides | original
        if union and len(sides & original) / len(union) >= 0.6:
            return False
    return True


def transition(curator: dict, critic: dict, version: str = LATEST) -> dict:
    """Describe what stage B did to stage A: consolidated, kept, unused, restated."""
    propositions = curator.get("propositions") or []
    curator_by_id = {str(item.get("id")): item for item in propositions}
    relations = critic.get("relations") or []
    consolidated = [
        {
            "relation": relation.get("id"),
            "classification": relation.get("classification"),
            "from_propositions": relation.get("from_propositions") or [],
            "side_a": relation.get("side_a"),
            "side_b": relation.get("side_b"),
            "figure_side": relation.get("figure_side"),
            "restated_in_critic_terms": _restated(relation, curator_by_id),
            "consolidates": len(relation.get("from_propositions") or []),
        }
        for relation in relations
    ]
    if schema_of(version) == "light":
        unused = [
            {"proposition": row.get("id"), "reason": row.get("reason"),
             "disposition": None}
            for row in critic.get("unused_propositions") or []
        ]
    else:
        unused = [
            {"proposition": row.get("from_proposition"), "reason": row.get("reason"),
             "disposition": row.get("disposition")}
            for row in critic.get("demoted") or []
        ]
    used: list[str] = []
    for relation in relations:
        used.extend(relation.get("from_propositions") or [])
    return {
        "stage_a_propositions": len(propositions),
        "stage_b_relations": len(relations),
        "primary": sum(1 for row in consolidated if row["classification"] == "primary"),
        "alternative": sum(
            1 for row in consolidated if row["classification"] == "alternative"
        ),
        "collapsed_by": len(propositions) - len(relations),
        "consolidated": consolidated,
        "largest_consolidation": max(
            (row["consolidates"] for row in consolidated), default=0
        ),
        "relations_restated_in_critic_terms": sum(
            1 for row in consolidated if row["restated_in_critic_terms"]
        ),
        "orientation": {
            "claimed": sum(
                1 for relation in relations
                if relation.get("figure_side") in ("side_a", "side_b")
            ),
            "unresolved": sum(
                1 for relation in relations if relation.get("figure_side") == "unresolved"
            ),
        },
        "unused": unused,
        "propositions_supporting_multiple_relations": sorted(
            {item for item in used if used.count(item) > 1}
        ),
        "stage_a_declared_shared_regions": sum(
            1 for item in propositions if item.get("shares_regions_with")
        ),
    }


def run_one(pal: str, entry: dict, project: Path, version: str, pair: dict,
            pipeline, out_root: Path) -> dict:
    out_dir = out_root / pal
    out_dir.mkdir(parents=True, exist_ok=True)
    corpus_dir = project / "outputs" / "tests" / CORPUS_SPACE / "corpus"
    curator_path = (
        project / "outputs" / "tests" / SPACE / EXPERIMENT / CURATOR_STAGE / pal
        / "03-curator.json"
    )
    curator = _read_json(curator_path)["parsed"]
    curator_ids = [str(item.get("id")) for item in curator.get("propositions") or []]
    images = stages.prepare_images(corpus_dir / entry["corpus_image"])

    system = prompt_io.strip_comments(pair["system"])
    user = prompt_io.substitute(
        prompt_io.strip_comments(pair["user"]),
        {"propositions": render_propositions(curator)},
    )
    (out_dir / "03-critic-user-prompt.txt").write_text(user, encoding="utf-8")
    result = stages.call_qwen(pipeline, system, user, [images["active"]], MAX_NEW_TOKENS)

    (out_dir / "04-critic-raw.txt").write_text(result["raw"] or "", encoding="utf-8")
    parsed = result["parsed"]
    problems = shape_problems(parsed, curator_ids, version)
    relations = (parsed or {}).get("relations") or [] if isinstance(parsed, dict) else []
    summary = transition(curator, parsed if isinstance(parsed, dict) else {}, version)
    _write_json(out_dir / "05-critic.json", {
        "id": pal,
        "stage": "B-critic",
        "prompt_version": version,
        "blind": True,
        "stage_a_source": f"{CURATOR_STAGE}/{pal}/03-curator.json",
        "parsed": parsed,
        "shape_problems": problems,
        "transition": summary,
    })

    manifest = {
        "id": pal,
        "status": "ok" if relations else "unparsed",
        "experiment": EXPERIMENT,
        "space": SPACE,
        "stage": "B-critic",
        "prompt_version": version,
        "created": datetime.now(timezone.utc).isoformat(),
        "inputs": {
            "image": f"{CORPUS_SPACE}/corpus/{entry['corpus_image']}",
            "stage_a": f"{CURATOR_STAGE}/{pal}/03-curator.json",
            "stage_a_propositions": curator_ids,
        },
        "blindness": {
            "sees": ["source image", "stage A propositions in full",
                     "critic system prompt", "critic user prompt"],
            "withheld": [
                "film title/year/director", "E5 selections", "E6 human review",
                "E7 rankings", "candidate inventories", "SAM masks",
                "hierarchy regions", "measured LAB values",
            ],
            "note": "Stage A's own figure_side and shares_regions_with are passed "
                    "through: they are part of the other curator's argument.",
        },
        "model": {
            "name": MODEL,
            "max_new_tokens": MAX_NEW_TOKENS,
            "do_sample": False,
            "device": result["device"],
        },
        "prompts": {"system": pair["system_file"], "user": pair["user_file"]},
        "relations": len(relations),
        "shape_problems": problems,
        "transition": summary,
    }
    _write_json(out_dir / "00-manifest.json", manifest)
    return manifest


def _unused_rows(step: dict) -> list[dict]:
    """Read either the v3 'unused' list or the v1/v2 'demoted' list."""
    if "unused" in step:
        return step["unused"]
    return [
        {"proposition": row.get("from_proposition"),
         "disposition": row.get("disposition"), "reason": row.get("reason")}
        for row in step.get("demoted") or []
    ]


def _report_markdown(manifests: list[dict], version: str) -> str:
    lines = [
        "# E8 Stage A -> Stage B Inspection",
        "",
        f"Stage A is frozen `curation-v3`. Stage B is `critic-{version}`. The critic "
        "saw the image and stage A's propositions only.",
        "",
        f"| Image | A props | B relations | primary | alternative | largest consolidation | orientation claimed | unresolved | unused | shape problems |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for manifest in manifests:
        step = manifest["transition"]
        lines.append(
            f"| {manifest['id']} | {step['stage_a_propositions']} | "
            f"{step['stage_b_relations']} | {step.get('primary', '-')} | "
            f"{step.get('alternative', '-')} | "
            f"{step['largest_consolidation']} | "
            f"{step['orientation']['claimed']} | {step['orientation']['unresolved']} | "
            f"{len(_unused_rows(step))} | {len(manifest['shape_problems'])} |"
        )
    for manifest in manifests:
        step = manifest["transition"]
        lines += ["", f"## {manifest['id']}", ""]
        for row in step["consolidated"]:
            restated = " (restated in the critic's terms)" if row["restated_in_critic_terms"] else ""
            label = f"{row['classification']} " if row.get("classification") else ""
            lines.append(
                f"- **{row['relation']}** [{label}from "
                f"{', '.join(row['from_propositions'])}]{restated}: "
                f"{row['side_a']} / {row['side_b']} [figure: {row['figure_side']}]"
            )
        for row in _unused_rows(step):
            label = row.get("disposition") or "unused"
            lines.append(f"- *{label}* {row['proposition']}: {row['reason']}")
    return "\n".join(lines) + "\n"


def pilot(project_path: str, *, version: str = LATEST,
          only: list[str] | None = None) -> Path:
    """Run stage B over the diagnostic subset and report the A->B transition."""
    from data.annotate import _load_text_generation_pipeline

    project = Path(project_path)
    corpus_dir = project / "outputs" / "tests" / CORPUS_SPACE / "corpus"
    out_root = project / "outputs" / "tests" / SPACE / EXPERIMENT / f"stage-b-{version}"
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
        print(f"[E8] critic {version} {pal}", flush=True)
        manifests.append(
            run_one(pal, corpus[pal], project, version, pair, pipeline, out_root)
        )

    _write_json(out_root / "stage-a-to-b-report.json", {
        "experiment": EXPERIMENT,
        "stage": "B-critic",
        "created": datetime.now(timezone.utc).isoformat(),
        "purpose": "Critic pilot only. Stage C, stage D and the CV audit are not built.",
        "model": MODEL,
        "prompt_version": version,
        "stage_a": CURATOR_STAGE,
        "prompts": {"system": pair["system_file"], "user": pair["user_file"]},
        "images": {
            manifest["id"]: {
                "status": manifest["status"],
                "shape_problems": manifest["shape_problems"],
                "transition": manifest["transition"],
            }
            for manifest in manifests
        },
    })
    (out_root / "stage-a-to-b-report.md").write_text(
        _report_markdown(manifests, version), encoding="utf-8"
    )
    print(f"[E8] stage B pilot over {len(manifests)} images -> {out_root}")
    return out_root


def main() -> None:
    parser = argparse.ArgumentParser(description="E8 stage B curatorial critic pilot")
    parser.add_argument("mode", choices=("pilot",))
    parser.add_argument("--project", required=True, help="Crossing project root")
    parser.add_argument("--version", default=LATEST, choices=tuple(PROMPT_FILES))
    parser.add_argument("--only", nargs="*", help="Restrict to these pilot ids")
    args = parser.parse_args()
    pilot(args.project, version=args.version, only=args.only or None)


if __name__ == "__main__":
    main()
