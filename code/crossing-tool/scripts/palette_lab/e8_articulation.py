"""E8 narratological branch, third pass — how is the distinction articulated in colour?

    uv run python -m scripts.palette_lab.e8_articulation pilot --project /path

One call per image. The model sees the source image, that image's complete
frozen narratology-v1 reading, and its complete frozen carrier-v1 reading.
Nothing else: no mask, no candidate colour, no measured value, no E5/E6/E7
output, no E8 chain output outside this branch, no human review.

Python here is deliberately boring. It loads, renders, calls, validates shape,
preserves provenance, and tallies. It does not choose a counterfield, score
contrast, rank materials, infer salience, decide direct_pair/broader_field, or
choose strong/partial/insufficient. Those are the model's judgements.
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
from scripts.palette_lab.e8_carrier import _COLOUR_VALUE, _is_bare_colour
from scripts.palette_lab.e8_curator import DIAGNOSTIC, MAX_NEW_TOKENS, MODEL
from scripts.palette_lab.e8_narratology import EXPERIMENT


ROLE = "articulation"
PROMPT_FILES = {
    "v1": ("prompts/palettes/palette-articulation-system-2026-09-15-v1.txt",
           "prompts/palettes/palette-articulation-user-2026-09-15-v1.txt"),
}
LATEST = "v1"
NARRATOLOGY_STAGE = "narratology-v1"
CARRIER_STAGE = "carrier-v1"

RELATIONSHIPS = ("direct_pair", "broader_field", "mixed")
STRENGTHS = ("strong", "partial", "insufficient")
# Only this one permits a null chromatic articulation.
STRENGTH_ALLOWING_NO_PAIR = "insufficient"
ARTICULATION_FIELDS = (
    "distinguished_material", "counterfield_material", "relationship",
    "why_this_pair",
)
TEXT_FIELDS = (
    "source_distinction", "narratological_meaning", "two_colour_strength",
    "what_the_pair_loses", "uncertainty",
)
MATERIAL_FIELDS = ("distinguished_material", "counterfield_material")

# A standalone conjunction, never one inside a hyphenated compound.
_CONJUNCTION = re.compile(r"(?<![\w-])(?:and|&)(?![\w-])", re.IGNORECASE)
# Hue/tone words only. The bare-colour set also holds filler words ("the",
# "of"), which must not count as one half of a colour phrase.
_TONE_WORDS = {
    "red", "blue", "green", "black", "white", "yellow", "orange", "brown",
    "grey", "gray", "purple", "pink", "gold", "golden", "silver", "warm",
    "cool", "cold", "dark", "light", "pale", "bright", "saturated",
    "desaturated", "muted", "neutral", "monochrome", "sepia", "tone", "tones",
}
_ARTICLES = {"the", "a", "an"}


def _normalise(text) -> str:
    text = str(text or "").strip().lower().replace("\u2019", "'").replace("\u2018", "'")
    return re.sub(r"\s+", " ", text).rstrip(" .;,")


def _is_conjunctive(value) -> bool:
    """True when a material joins visibly distinct colour-bearing things.

    Deliberately narrow. It fires only on a standalone "and"/"&", and not when
    that conjunction joins two tone words, because "black and white" describes
    one material rather than two. It does not attempt to read comma lists.
    """
    text = str(value or "")
    for match in _CONJUNCTION.finditer(text):
        before = re.findall(r"[A-Za-z]+", text[:match.start()])
        after = [
            word for word in re.findall(r"[A-Za-z]+", text[match.end():])
            if word.lower() not in _ARTICLES
        ]
        joins_two_tones = (
            before and after
            and before[-1].lower() in _TONE_WORDS
            and after[0].lower() in _TONE_WORDS
        )
        if not joins_two_tones:
            return True
    return False


def render_narratology(reading: dict) -> str:
    """The complete frozen narratology-v1 reading, including its colour roles.

    Deliberately fuller than the carrier pass's renderer, which withheld the
    colour roles and the two-colour verdict. This stage is given the whole
    reading.
    """
    lines = ["WHAT STANDS OUT", ""]
    for entry in reading.get("what_stands_out") or []:
        lines.append(f"  - {entry.get('entity', '')}")
        lines.append(f"      against: {entry.get('against', '')}")
        lines.append(f"      what makes it stand out: {entry.get('what_makes_it_stand_out', '')}")
        lines.append(f"      why it matters: {entry.get('why_it_matters', '')}")
        lines.append("")
    cue = reading.get("strongest_cue") or {}
    lines += [
        "THE STRONGEST CUE",
        "",
        f"  visible cue: {cue.get('visible_cue', '')}",
        f"  what it leads the spectator to infer: {cue.get('spectator_inference', '')}",
        f"  genre context: {cue.get('genre_context', '')}",
        f"  convention being used: {cue.get('critical_context', '')}",
        "",
        "THE DISTINCTIONS THAT ORGANIZE THE FRAME",
        "",
    ]
    for entry in reading.get("organizing_distinctions") or []:
        lines.append(f"  - {entry.get('distinction', '')}")
        lines.append(f"      side A: {entry.get('side_a', '')}")
        lines.append(f"      side B: {entry.get('side_b', '')}")
        lines.append(f"      why it is what the frame is about: {entry.get('narratological_reason', '')}")
        lines.append(f"      formal device carrying it: {entry.get('formal_carrier', '')}")
        lines.append(f"      could two colours carry it: {entry.get('two_colour_potential', '')}")
        lines.append(f"      chromatic role of side A: {entry.get('colour_role_a', '')}")
        lines.append(f"      chromatic role of side B: {entry.get('colour_role_b', '')}")
        lines.append(f"      what two colours would preserve: {entry.get('what_two_colours_would_preserve', '')}")
        lines.append(f"      what two colours would lose: {entry.get('what_two_colours_would_lose', '')}")
        lines.append("")
    lines += [
        "OVERALL",
        "",
        f"  {reading.get('overall_reading', '')}",
        "",
        f"  the reader's own doubt: {reading.get('uncertainty', '')}",
    ]
    return "\n".join(lines).strip()


def render_carriers(carrier: dict) -> str:
    """The complete frozen carrier-v1 reading, as evidence from the pass before."""
    lines = [
        "HOW THE PREVIOUS PASS READ THIS FRAME",
        "",
        f"  {carrier.get('image_reading', '')}",
        "",
        "THE MATERIALS IT FOUND",
        "",
    ]
    for entry in carrier.get("carriers") or []:
        lines.append(f"  - for the distinction: {entry.get('source_distinction', '')}")
        lines.append(f"      what it means: {entry.get('narratological_meaning', '')}")
        lines.append(f"      formal device carrying it: {entry.get('formal_carrier', '')}")
        for name, label in (("carrier_a", "material A"), ("carrier_b", "material B")):
            side = entry.get(name)
            if isinstance(side, dict):
                lines.append(f"      {label}: {side.get('visible_material', '')}")
                lines.append(f"          how it looks: {side.get('qualitative_colour_or_tone', '')}")
                lines.append(f"          what it carries: {side.get('what_it_carries', '')}")
                lines.append(f"          why this material: {side.get('why_this_material', '')}")
            else:
                lines.append(f"      {label}: none named")
        lines.append(f"      how strong it judged the pair: {entry.get('two_colour_strength', '')}")
        lines.append(f"      what it said the pair preserves: {entry.get('what_the_pair_preserves', '')}")
        lines.append(f"      what it said the pair loses: {entry.get('what_the_pair_loses', '')}")
        lines.append(f"      its own doubt: {entry.get('uncertainty', '')}")
        lines.append("")
    return "\n".join(lines).strip()


def shape_problems(parsed, distinctions: list) -> list[str]:
    """Check the record's shape, not its curatorial judgement."""
    if not isinstance(parsed, dict):
        return ["response did not parse as a JSON object"]
    problems = []
    if not str(parsed.get("image_reading") or "").strip():
        problems.append("image_reading is empty")

    articulations = parsed.get("articulations")
    if not isinstance(articulations, list) or not articulations:
        return problems + ["no articulations"]
    if len(articulations) != len(distinctions):
        problems.append(
            f"{len(articulations)} articulations for {len(distinctions)} "
            f"source distinctions"
        )

    for index, record in enumerate(articulations, start=1):
        if not isinstance(record, dict):
            problems.append(f"articulation {index} is not an object")
            continue
        missing = [
            field for field in TEXT_FIELDS
            if not str(record.get(field) or "").strip()
        ]
        if missing:
            problems.append(f"articulation {index} missing {', '.join(missing)}")

        source = distinctions[index - 1] if index <= len(distinctions) else {}
        frozen = _normalise(source.get("distinction"))
        if frozen and _normalise(record.get("source_distinction")) != frozen:
            problems.append(
                f"articulation {index} source_distinction was not copied from "
                f"the frozen reading"
            )

        strength = str(record.get("two_colour_strength") or "").strip().lower()
        if strength and strength not in STRENGTHS:
            problems.append(
                f"articulation {index} two_colour_strength {strength!r} "
                f"is not one of {STRENGTHS}"
            )

        pair = record.get("chromatic_articulation")
        if not isinstance(pair, dict):
            if strength != STRENGTH_ALLOWING_NO_PAIR:
                problems.append(
                    f"articulation {index} gives no pair but is not "
                    f"{STRENGTH_ALLOWING_NO_PAIR!r}"
                )
            continue

        blank = [
            field for field in ARTICULATION_FIELDS
            if not str(pair.get(field) or "").strip()
        ]
        if blank:
            problems.append(f"articulation {index} missing {', '.join(blank)}")
        relationship = str(pair.get("relationship") or "").strip().lower()
        if relationship and relationship not in RELATIONSHIPS:
            problems.append(
                f"articulation {index} relationship {relationship!r} "
                f"is not one of {RELATIONSHIPS}"
            )
        for field in MATERIAL_FIELDS:
            material = str(pair.get(field) or "")
            if _is_bare_colour(material):
                problems.append(
                    f"articulation {index} {field} {material!r} names a colour, "
                    f"not a material"
                )
            if _is_conjunctive(material):
                problems.append(
                    f"articulation {index} {field} {material!r} joins several "
                    f"materials"
                )
        if not str(record.get("what_the_pair_preserves") or "").strip():
            problems.append(f"articulation {index} has no what_the_pair_preserves")

    for field, value in _prose_fields(parsed):
        if _COLOUR_VALUE.search(str(value or "")):
            problems.append(f"{field} contains a colour value")
        if re.search(r"\d", str(value or "")):
            problems.append(f"{field} contains a number")
    return problems


def _prose_fields(parsed: dict):
    yield "image_reading", parsed.get("image_reading")
    for index, record in enumerate(parsed.get("articulations") or [], start=1):
        if not isinstance(record, dict):
            continue
        for field in ("narratological_meaning", "what_the_pair_preserves",
                      "what_the_pair_loses", "uncertainty"):
            yield f"articulation {index} {field}", record.get(field)
        pair = record.get("chromatic_articulation")
        if isinstance(pair, dict):
            for field in ARTICULATION_FIELDS:
                yield f"articulation {index} {field}", pair.get(field)


def provenance(pal: str) -> dict:
    """The exact frozen inputs this pass read, recorded with every response."""
    return {
        "narratology": f"{NARRATOLOGY_STAGE}/{pal}/03-narratology.json",
        "carrier": f"{CARRIER_STAGE}/{pal}/04-carrier.json",
    }


def _frozen_materials(carrier_record: dict) -> list:
    materials = []
    for name in ("carrier_a", "carrier_b"):
        side = carrier_record.get(name)
        materials.append(
            str(side.get("visible_material") or "") if isinstance(side, dict) else None
        )
    return materials


def summarise(parsed, reading: dict, carrier: dict) -> dict:
    """Tally the pass. Descriptive only; nothing here selects or scores anything."""
    parsed = parsed if isinstance(parsed, dict) else {}
    carrier = carrier if isinstance(carrier, dict) else {}
    distinctions = reading.get("organizing_distinctions") or []
    frozen_carriers = carrier.get("carriers") or []
    rows = []
    for index, record in enumerate(parsed.get("articulations") or []):
        if not isinstance(record, dict):
            continue
        source = distinctions[index] if index < len(distinctions) else {}
        previous = frozen_carriers[index] if index < len(frozen_carriers) else {}
        pair = record.get("chromatic_articulation")
        pair = pair if isinstance(pair, dict) else {}
        rows.append({
            "source_distinction": record.get("source_distinction"),
            "relationship": str(pair.get("relationship") or "").lower() or None,
            "two_colour_strength": str(record.get("two_colour_strength") or "").lower(),
            "distinguished_material": pair.get("distinguished_material"),
            "counterfield_material": pair.get("counterfield_material"),
            "carrier_v1_materials": _frozen_materials(previous),
            "narratology_colour_roles": [
                source.get("colour_role_a"), source.get("colour_role_b"),
            ],
            "carrier_v1_strength": str(previous.get("two_colour_strength") or "").lower() or None,
        })
    relationships = [row["relationship"] for row in rows]
    strengths = [row["two_colour_strength"] for row in rows]
    return {
        "source_distinctions": len(distinctions),
        "articulations": len(rows),
        "rows": rows,
        "relationship": {
            name: relationships.count(name)
            for name in RELATIONSHIPS if relationships.count(name)
        },
        "strength": {
            name: strengths.count(name)
            for name in STRENGTHS if strengths.count(name)
        },
        "articulations_without_a_pair": sum(
            1 for row in rows if not row["distinguished_material"]
        ),
        "note": (
            "Every field here is copied from the model's own answer or from the "
            "frozen stages it read. Python chose no material, no counterfield, "
            "no relationship and no strength."
        ),
    }


def run_one(pal: str, entry: dict, project: Path, version: str, pair: dict,
            pipeline, out_root: Path) -> dict:
    out_dir = out_root / pal
    out_dir.mkdir(parents=True, exist_ok=True)
    corpus_dir = project / "outputs" / "tests" / CORPUS_SPACE / "corpus"
    branch = project / "outputs" / "tests" / SPACE / EXPERIMENT
    sources = provenance(pal)
    reading = _read_json(branch / sources["narratology"])["parsed"]
    carrier = _read_json(branch / sources["carrier"])["parsed"]
    images = stages.prepare_images(corpus_dir / entry["corpus_image"])

    system = prompt_io.strip_comments(pair["system"])
    user = prompt_io.substitute(prompt_io.strip_comments(pair["user"]), {
        "narratology": render_narratology(reading),
        "carriers": render_carriers(carrier),
    })
    (out_dir / "02-articulation-user-prompt.txt").write_text(user, encoding="utf-8")
    result = stages.call_qwen(pipeline, system, user, [images["active"]], MAX_NEW_TOKENS)

    (out_dir / "03-articulation-raw.txt").write_text(result["raw"] or "", encoding="utf-8")
    parsed = result["parsed"]
    distinctions = reading.get("organizing_distinctions") or []
    problems = shape_problems(parsed, distinctions)
    summary = summarise(parsed, reading, carrier)
    _write_json(out_dir / "04-articulation.json", {
        "id": pal,
        "branch": EXPERIMENT,
        "stage": "articulation",
        "prompt_version": version,
        "blind": True,
        "provenance": sources,
        "parsed": parsed,
        "shape_problems": problems,
        "summary": summary,
    })

    manifest = {
        "id": pal,
        "status": "ok" if isinstance(parsed, dict) and parsed.get("articulations") else "unparsed",
        "branch": EXPERIMENT,
        "stage": "articulation",
        "space": SPACE,
        "prompt_version": version,
        "created": datetime.now(timezone.utc).isoformat(),
        "inputs": {
            "image": f"{CORPUS_SPACE}/corpus/{entry['corpus_image']}",
            **sources,
            "source_distinctions": [
                item.get("distinction") for item in distinctions
            ],
        },
        "blindness": {
            "sees": ["source image", "the frozen narratology-v1 reading",
                     "the frozen carrier-v1 reading", "articulation system prompt",
                     "articulation user prompt"],
            "withheld": [
                "film title/year/director", "E5 selections", "E6 human review",
                "E7 rankings", "E8 curator/critic/spatializer/auditor output",
                "SAM masks", "hierarchy regions", "candidate inventories",
                "measured LAB values",
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
        "summary": summary,
    }
    _write_json(out_dir / "00-manifest.json", manifest)
    return manifest


def _descent_markdown(project: Path, manifests: list[dict], out_root: Path) -> str:
    """The full descent, image by image, as asked for in the brief."""
    lines = [
        "# E8 narratological branch — from distinction to chromatic articulation",
        "",
        "`narratology-v1` and `carrier-v1` are frozen and unchanged. "
        "`articulation-v1` reads both plus the image, and nothing else. No "
        "segmentation was run and no colour measured.",
        "",
    ]
    branch = project / "outputs" / "tests" / SPACE / EXPERIMENT
    for manifest in manifests:
        pal = manifest["id"]
        sources = provenance(pal)
        reading = _read_json(branch / sources["narratology"])["parsed"]
        carrier = _read_json(branch / sources["carrier"])["parsed"]
        record = _read_json(out_root / pal / "04-articulation.json")["parsed"]
        lines += [f"## {pal}", ""]
        for entry in reading.get("what_stands_out") or []:
            lines.append(f"- **stands out:** {entry.get('entity', '')}")
            lines.append(f"- **against:** {entry.get('against', '')}")
        for index, item in enumerate(reading.get("organizing_distinctions") or []):
            previous = (carrier.get("carriers") or [{}])[index] if carrier else {}
            current = (record.get("articulations") or [{}])[index] if record else {}
            pair = current.get("chromatic_articulation")
            pair = pair if isinstance(pair, dict) else {}
            material_a, material_b = _frozen_materials(previous)
            lines += [
                "",
                f"- **narratological distinction:** {item.get('distinction', '')}",
                f"- **formal carrier:** {item.get('formal_carrier', '')}",
                f"- **carrier-v1 direct materials:** {material_a or 'none'} / "
                f"{material_b or 'none'}",
                f"- **distinguished material:** {pair.get('distinguished_material', 'none')}",
                f"- **counterfield material:** {pair.get('counterfield_material', 'none')}",
                f"- **relationship:** {pair.get('relationship', '')}",
                f"- **why this pair:** {pair.get('why_this_pair', '')}",
                f"- **two-colour strength:** {current.get('two_colour_strength', '')} "
                f"(carrier-v1 said {previous.get('two_colour_strength', '')})",
                f"- **preserves:** {current.get('what_the_pair_preserves', '')}",
                f"- **loses:** {current.get('what_the_pair_loses', '')}",
                f"- **uncertainty:** {current.get('uncertainty', '')}",
            ]
        lines.append("")
    return "\n".join(lines) + "\n"


def pilot(project_path: str, *, version: str = LATEST,
          only: list[str] | None = None) -> Path:
    """Run the chromatic articulation pass over the two frozen readings."""
    from data.annotate import _load_text_generation_pipeline

    project = Path(project_path)
    corpus_dir = project / "outputs" / "tests" / CORPUS_SPACE / "corpus"
    out_root = project / "outputs" / "tests" / SPACE / EXPERIMENT / f"articulation-{version}"
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
        print(f"[E8c] articulation {pal}", flush=True)
        manifests.append(
            run_one(pal, corpus[pal], project, version, pair, pipeline, out_root)
        )

    _write_json(out_root / "articulation-report.json", {
        "branch": EXPERIMENT,
        "stage": "articulation",
        "created": datetime.now(timezone.utc).isoformat(),
        "purpose": (
            "Third curatorial pass over the frozen narratological branch. Asks "
            "how the distinction is articulated through colour, and against what "
            "field. No SAM, no measurement, no scorer, no comparison with the "
            "human review."
        ),
        "model": MODEL,
        "prompt_version": version,
        "source_stages": [NARRATOLOGY_STAGE, CARRIER_STAGE],
        "prompts": {"system": pair["system_file"], "user": pair["user_file"]},
        "images": {
            manifest["id"]: {
                "provenance": manifest["inputs"],
                "shape_problems": manifest["shape_problems"],
                "summary": manifest["summary"],
            }
            for manifest in manifests
        },
    })
    (out_root / "articulation-descent.md").write_text(
        _descent_markdown(project, manifests, out_root), encoding="utf-8"
    )
    print(f"[E8c] articulation pilot over {len(manifests)} images -> {out_root}")
    return out_root


def main() -> None:
    parser = argparse.ArgumentParser(
        description="E8 narratological branch chromatic articulation pilot"
    )
    parser.add_argument("mode", choices=("pilot",))
    parser.add_argument("--project", required=True, help="Crossing project root")
    parser.add_argument("--version", default=LATEST, choices=tuple(PROMPT_FILES))
    parser.add_argument("--only", nargs="*", help="Restrict to these pilot ids")
    args = parser.parse_args()
    pilot(args.project, version=args.version, only=args.only)


if __name__ == "__main__":
    main()
