"""E8 narratological branch, second pass — which materials carry the distinction?

    uv run python -m scripts.palette_lab.e8_carrier pilot --project /path

One call per image. The model sees the source image and that image's complete
frozen narratology-v1 reading, and nothing else: no mask, no candidate colour,
no measured value, no E5/E7 output, no E8 chain output, no human review.

Python here is deliberately boring. It loads, renders, calls, validates shape,
persists, and tallies. It does not select carriers, score them, infer narrative
importance, or choose a colour pair.
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
from scripts.palette_lab.e8_curator import DIAGNOSTIC, MAX_NEW_TOKENS, MODEL
from scripts.palette_lab.e8_narratology import EXPERIMENT


ROLE = "carrier"
PROMPT_FILES = {
    "v1": ("prompts/palettes/palette-carrier-system-2026-09-15-v1.txt",
           "prompts/palettes/palette-carrier-user-2026-09-15-v1.txt"),
}
LATEST = "v1"
NARRATOLOGY_STAGE = "narratology-v1"

STRENGTHS = ("strong", "partial", "insufficient")
# Only this one permits a null carrier pair.
STRENGTH_ALLOWING_NO_PAIR = "insufficient"
CARRIER_FIELDS = (
    "visible_material", "qualitative_colour_or_tone", "what_it_carries",
    "why_this_material",
)
CARRIER_TEXT_FIELDS = (
    "source_distinction", "narratological_meaning", "formal_carrier",
    "two_colour_strength", "what_the_pair_loses", "uncertainty",
)
_COLOUR_VALUE = re.compile(r"#[0-9a-f]{3,8}\b|\brgba?\s*\(", re.IGNORECASE)
# A carrier made only of these names a quality, not something anyone could find.
_COLOUR_WORDS = {
    "red", "blue", "green", "black", "white", "yellow", "orange", "brown",
    "grey", "gray", "purple", "pink", "warm", "cool", "cold", "dark", "light",
    "pale", "bright", "saturated", "desaturated", "muted", "neutral", "tone",
    "tones", "colour", "colours", "color", "colors", "hue", "shade", "and",
    "the", "a", "an", "of", "or",
}


def _is_bare_colour(value: str) -> bool:
    words = re.findall(r"[a-z]+", str(value or "").lower())
    return bool(words) and all(word in _COLOUR_WORDS for word in words)


def render_reading(reading: dict) -> str:
    """Lay out the frozen narratology reading as the carrier pass's evidence."""
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


def shape_problems(parsed, distinction_count: int) -> list[str]:
    """Check the carrier record's shape, not its curatorial judgement."""
    if not isinstance(parsed, dict):
        return ["response did not parse as a JSON object"]
    problems = []
    if not str(parsed.get("image_reading") or "").strip():
        problems.append("image_reading is empty")

    carriers = parsed.get("carriers")
    if not isinstance(carriers, list) or not carriers:
        return problems + ["no carriers"]
    if len(carriers) != distinction_count:
        problems.append(
            f"{len(carriers)} carriers for {distinction_count} source distinctions"
        )

    for index, carrier in enumerate(carriers, start=1):
        if not isinstance(carrier, dict):
            problems.append(f"carrier {index} is not an object")
            continue
        missing = [
            field for field in CARRIER_TEXT_FIELDS
            if not str(carrier.get(field) or "").strip()
        ]
        if missing:
            problems.append(f"carrier {index} missing {', '.join(missing)}")
        strength = str(carrier.get("two_colour_strength") or "").strip().lower()
        if strength and strength not in STRENGTHS:
            problems.append(
                f"carrier {index} two_colour_strength {strength!r} "
                f"is not one of {STRENGTHS}"
            )
        sides = [carrier.get("carrier_a"), carrier.get("carrier_b")]
        present = [side for side in sides if isinstance(side, dict)]
        if not present:
            if strength != STRENGTH_ALLOWING_NO_PAIR:
                problems.append(
                    f"carrier {index} gives no pair but is not {STRENGTH_ALLOWING_NO_PAIR!r}"
                )
            continue
        if len(present) == 1:
            problems.append(f"carrier {index} names one side of the pair but not the other")
        for name, side in zip(("carrier_a", "carrier_b"), sides):
            if not isinstance(side, dict):
                continue
            blank = [
                field for field in CARRIER_FIELDS
                if not str(side.get(field) or "").strip()
            ]
            if blank:
                problems.append(f"carrier {index} {name} missing {', '.join(blank)}")
            material = str(side.get("visible_material") or "")
            if _is_bare_colour(material):
                problems.append(
                    f"carrier {index} {name} visible_material {material!r} "
                    f"names a colour, not a material"
                )
        if strength != STRENGTH_ALLOWING_NO_PAIR and not str(
            carrier.get("what_the_pair_preserves") or ""
        ).strip():
            problems.append(f"carrier {index} has no what_the_pair_preserves")

    for field, value in _prose_fields(parsed):
        if _COLOUR_VALUE.search(str(value or "")):
            problems.append(f"{field} contains a colour value")
        if re.search(r"\d", str(value or "")):
            problems.append(f"{field} contains a number")
    return problems


def _prose_fields(parsed: dict):
    yield "image_reading", parsed.get("image_reading")
    for index, carrier in enumerate(parsed.get("carriers") or [], start=1):
        if not isinstance(carrier, dict):
            continue
        for field in ("narratological_meaning", "formal_carrier",
                      "what_the_pair_preserves", "what_the_pair_loses"):
            yield f"carrier {index} {field}", carrier.get(field)
        for name in ("carrier_a", "carrier_b"):
            side = carrier.get(name)
            if isinstance(side, dict):
                for field in CARRIER_FIELDS:
                    yield f"carrier {index} {name} {field}", side.get(field)


def _echoes(material: str, side: str) -> bool:
    """Lexical overlap only, reported for inspection — not a judgement."""
    def words(text):
        return {w for w in re.findall(r"[a-z]{4,}", str(text).lower())
                if w not in _COLOUR_WORDS}

    material_words, side_words = words(material), words(side)
    return bool(material_words & side_words)


def summarise(parsed, reading: dict) -> dict:
    """Tally the carrier pass. Descriptive only; nothing here selects anything."""
    parsed = parsed if isinstance(parsed, dict) else {}
    distinctions = reading.get("organizing_distinctions") or []
    rows = []
    for index, carrier in enumerate(parsed.get("carriers") or []):
        if not isinstance(carrier, dict):
            continue
        source = distinctions[index] if index < len(distinctions) else {}
        entry = {
            "source_distinction": carrier.get("source_distinction"),
            "two_colour_strength": str(carrier.get("two_colour_strength") or "").lower(),
            "formal_carrier": carrier.get("formal_carrier"),
            "narratology_colour_roles": [
                source.get("colour_role_a"), source.get("colour_role_b"),
            ],
            "materials": [],
            "material_echoes_source_side": [],
        }
        for name, side_key in (("carrier_a", "side_a"), ("carrier_b", "side_b")):
            side = carrier.get(name)
            if isinstance(side, dict):
                material = str(side.get("visible_material") or "")
                entry["materials"].append(material)
                entry["material_echoes_source_side"].append(
                    _echoes(material, source.get(side_key, ""))
                )
            else:
                entry["materials"].append(None)
                entry["material_echoes_source_side"].append(None)
        rows.append(entry)
    strengths = [row["two_colour_strength"] for row in rows]
    return {
        "source_distinctions": len(distinctions),
        "carriers": len(rows),
        "rows": rows,
        "strength": {name: strengths.count(name) for name in STRENGTHS if strengths.count(name)},
        "pairs_without_a_carrier": sum(1 for row in rows if None in row["materials"]),
        "echo_note": (
            "material_echoes_source_side is a lexical overlap observation between "
            "the named material and the narratological side it carries. It is "
            "reported for inspection and decides nothing."
        ),
    }


def run_one(pal: str, entry: dict, project: Path, version: str, pair: dict,
            pipeline, out_root: Path) -> dict:
    out_dir = out_root / pal
    out_dir.mkdir(parents=True, exist_ok=True)
    corpus_dir = project / "outputs" / "tests" / CORPUS_SPACE / "corpus"
    source = (
        project / "outputs" / "tests" / SPACE / EXPERIMENT / NARRATOLOGY_STAGE / pal
        / "03-narratology.json"
    )
    reading = _read_json(source)["parsed"]
    images = stages.prepare_images(corpus_dir / entry["corpus_image"])

    system = prompt_io.strip_comments(pair["system"])
    user = prompt_io.substitute(
        prompt_io.strip_comments(pair["user"]), {"reading": render_reading(reading)}
    )
    (out_dir / "02-carrier-user-prompt.txt").write_text(user, encoding="utf-8")
    result = stages.call_qwen(pipeline, system, user, [images["active"]], MAX_NEW_TOKENS)

    (out_dir / "03-carrier-raw.txt").write_text(result["raw"] or "", encoding="utf-8")
    parsed = result["parsed"]
    problems = shape_problems(parsed, len(reading.get("organizing_distinctions") or []))
    _write_json(out_dir / "04-carrier.json", {
        "id": pal,
        "branch": EXPERIMENT,
        "stage": "carrier",
        "prompt_version": version,
        "blind": True,
        "source_reading": f"{NARRATOLOGY_STAGE}/{pal}/03-narratology.json",
        "parsed": parsed,
        "shape_problems": problems,
        "summary": summarise(parsed, reading),
    })

    manifest = {
        "id": pal,
        "status": "ok" if isinstance(parsed, dict) and parsed.get("carriers") else "unparsed",
        "branch": EXPERIMENT,
        "stage": "carrier",
        "space": SPACE,
        "prompt_version": version,
        "created": datetime.now(timezone.utc).isoformat(),
        "inputs": {
            "image": f"{CORPUS_SPACE}/corpus/{entry['corpus_image']}",
            "reading": f"{NARRATOLOGY_STAGE}/{pal}/03-narratology.json",
            "source_distinctions": [
                item.get("distinction")
                for item in reading.get("organizing_distinctions") or []
            ],
        },
        "blindness": {
            "sees": ["source image", "the frozen narratology-v1 reading",
                     "carrier system prompt", "carrier user prompt"],
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
        "summary": summarise(parsed, reading),
    }
    _write_json(out_dir / "00-manifest.json", manifest)
    return manifest


def _descent_markdown(project: Path, manifests: list[dict], out_root: Path) -> str:
    """The conceptual descent, image by image, as asked for in the brief."""
    lines = [
        "# E8 narratological branch — the descent from distinction to material",
        "",
        "`narratology-v1` is frozen and unchanged. `carrier-v1` reads it plus the "
        "image, and nothing else. No segmentation was run and no colour measured.",
        "",
    ]
    narratology_root = project / "outputs" / "tests" / SPACE / EXPERIMENT / NARRATOLOGY_STAGE
    for manifest in manifests:
        pal = manifest["id"]
        reading = _read_json(narratology_root / pal / "03-narratology.json")["parsed"]
        carrier = _read_json(out_root / pal / "04-carrier.json")["parsed"]
        lines += [f"## {pal}", ""]
        for entry in reading.get("what_stands_out") or []:
            lines.append(f"- **stands out:** {entry.get('entity', '')}")
            lines.append(f"- **against:** {entry.get('against', '')}")
        distinctions = reading.get("organizing_distinctions") or []
        for index, item in enumerate(distinctions):
            record = (carrier.get("carriers") or [{}])[index] if carrier else {}
            lines += [
                "",
                f"- **narratological distinction:** {item.get('distinction', '')}",
                f"- **formal carrier (narratology-v1):** {item.get('formal_carrier', '')}",
                f"- **colour roles (narratology-v1):** "
                f"{item.get('colour_role_a', '')} / {item.get('colour_role_b', '')}",
                f"- **formal carrier (carrier-v1):** {record.get('formal_carrier', '')}",
            ]
            for name in ("carrier_a", "carrier_b"):
                side = record.get(name)
                if isinstance(side, dict):
                    lines.append(
                        f"- **material {name[-1].upper()}:** "
                        f"{side.get('visible_material', '')} "
                        f"({side.get('qualitative_colour_or_tone', '')}) — "
                        f"{side.get('what_it_carries', '')}"
                    )
                else:
                    lines.append(f"- **material {name[-1].upper()}:** none")
            lines += [
                f"- **two-colour strength:** {record.get('two_colour_strength', '')}",
                f"- **preserves:** {record.get('what_the_pair_preserves', '')}",
                f"- **loses:** {record.get('what_the_pair_loses', '')}",
            ]
        lines.append("")
    return "\n".join(lines) + "\n"


def pilot(project_path: str, *, version: str = LATEST,
          only: list[str] | None = None) -> Path:
    """Run the carrier pass over the frozen narratological readings."""
    from data.annotate import _load_text_generation_pipeline

    project = Path(project_path)
    corpus_dir = project / "outputs" / "tests" / CORPUS_SPACE / "corpus"
    out_root = project / "outputs" / "tests" / SPACE / EXPERIMENT / f"carrier-{version}"
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
        print(f"[E8b] carrier {pal}", flush=True)
        manifests.append(
            run_one(pal, corpus[pal], project, version, pair, pipeline, out_root)
        )

    _write_json(out_root / "carrier-report.json", {
        "branch": EXPERIMENT,
        "stage": "carrier",
        "created": datetime.now(timezone.utc).isoformat(),
        "purpose": (
            "Second curatorial pass over the frozen narratological readings. No "
            "SAM, no measurement, no scorer, no comparison with the human review."
        ),
        "model": MODEL,
        "prompt_version": version,
        "source_stage": NARRATOLOGY_STAGE,
        "prompts": {"system": pair["system_file"], "user": pair["user_file"]},
        "images": {
            manifest["id"]: {
                "shape_problems": manifest["shape_problems"],
                "summary": manifest["summary"],
            }
            for manifest in manifests
        },
    })
    (out_root / "carrier-descent.md").write_text(
        _descent_markdown(project, manifests, out_root), encoding="utf-8"
    )
    print(f"[E8b] carrier pilot over {len(manifests)} images -> {out_root}")
    return out_root


def main() -> None:
    parser = argparse.ArgumentParser(description="E8 narratological branch carrier pilot")
    parser.add_argument("mode", choices=("pilot",))
    parser.add_argument("--project", required=True, help="Crossing project root")
    parser.add_argument("--version", default=LATEST, choices=tuple(PROMPT_FILES))
    parser.add_argument("--only", nargs="*", help="Restrict to these pilot ids")
    args = parser.parse_args()
    pilot(args.project, version=args.version, only=args.only or None)


if __name__ == "__main__":
    main()
