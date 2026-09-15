"""E8 stage A pilot — what does the VLM see when asked to curate, not analyse?

    uv run python -m scripts.palette_lab.e8_curator pilot --project /path
    uv run python -m scripts.palette_lab.e8_curator pilot --project /path --versions v1 v2

This is deliberately only the first stage of E8. No critic, no spatialization,
no CV audit, no comparison with the human review. The question it exists to
answer is whether the curatorial prompt elicits plural, image-specific readings
at all.

The call is blind: the model receives the image and the frozen curation prompt
pair, and nothing from E4-E7 or from the human review. Each prompt version
writes its own tree so revisions stay auditable against each other.
"""

from __future__ import annotations

import argparse
import re
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from scripts.palette_lab import prompts as prompt_io
from scripts.palette_lab import render as R
from scripts.palette_lab import stages
from scripts.palette_lab.e4_pilot import CORPUS_SPACE, PILOT, SPACE
from scripts.palette_lab.e5_two_color import _read_json, _write_json


EXPERIMENT = "E8-vlm-curatorial"
ROLE = "curation"
PROMPT_STEM = "prompts/palettes/palette-curation-{kind}-2026-09-14-{version}.txt"
MODEL = "Qwen3-VL-8B-Instruct"
MAX_NEW_TOKENS = 3072
VERSIONS = ("v1", "v2", "v3")

# The subset named for the stage A pilot; the full pilot stays frozen at nine.
DIAGNOSTIC = ["PAL-001", "PAL-004", "PAL-015", "PAL-019"]

COMMON_FIELDS = (
    "id", "side_a", "side_b", "relationship", "why_it_organizes_the_frame",
    "formal_basis", "semantic_basis", "spatial_basis", "uncertainty",
)
V1_FIELDS = COMMON_FIELDS + ("role_claim",)
V2_FIELDS = COMMON_FIELDS + ("figure_side", "role_reasoning")
FIGURE_SIDES = ("side_a", "side_b", "no_role_claim")

# Only v1 used the free-prose role_claim; every later version uses the enum.
LEGACY_ROLE_CLAIM_VERSIONS = ("v1",)


def _uses_role_enum(version: str) -> bool:
    return version not in LEGACY_ROLE_CLAIM_VERSIONS


def _terms(proposition: dict) -> set[str]:
    text = f"{proposition.get('side_a', '')} {proposition.get('side_b', '')}".lower()
    return {word for word in re.findall(r"[a-z]+", text) if len(word) > 3}


def _repeated_text(propositions: list[dict]) -> dict:
    """Find prose reused verbatim across propositions, a paraphrase symptom."""
    repeated = {}
    for field in ("uncertainty", "role_reasoning", "role_claim", "relationship"):
        counts = Counter(
            str(proposition.get(field) or "").strip()
            for proposition in propositions
            if str(proposition.get(field) or "").strip()
        )
        duplicates = [text for text, count in counts.items() if count > 1]
        if duplicates:
            repeated[field] = len(duplicates)
    return repeated


def _diversity(propositions: list[dict]) -> dict:
    """Report how distinct the propositions' two terms are, without gating.

    Lexical overlap is a crude proxy; it flags likely paraphrases for human
    inspection rather than deciding anything.
    """
    pairs = []
    for left in range(len(propositions)):
        for right in range(left + 1, len(propositions)):
            first, second = _terms(propositions[left]), _terms(propositions[right])
            union = first | second
            overlap = len(first & second) / len(union) if union else 0.0
            pairs.append({
                "a": propositions[left].get("id", f"#{left + 1}"),
                "b": propositions[right].get("id", f"#{right + 1}"),
                "term_overlap": round(overlap, 3),
            })
    pairs.sort(key=lambda row: -row["term_overlap"])
    return {
        "propositions": len(propositions),
        "distinct_side_pairs": len({
            (str(p.get("side_a", "")).strip().lower(),
             str(p.get("side_b", "")).strip().lower())
            for p in propositions
        }),
        "highest_term_overlap": pairs[0] if pairs else None,
        "pairs_above_half_overlap": [row for row in pairs if row["term_overlap"] >= 0.5],
        "declared_shared_regions": sum(
            1 for proposition in propositions if proposition.get("shares_regions_with")
        ),
        "repeated_field_text": _repeated_text(propositions),
    }


def _shape_problems(parsed, version: str) -> list[str]:
    if not isinstance(parsed, dict):
        return ["response did not parse as a JSON object"]
    propositions = parsed.get("propositions")
    if not isinstance(propositions, list) or not propositions:
        return ["no propositions array"]
    problems = []
    if not str(parsed.get("what_the_frame_is_doing") or "").strip():
        problems.append("what_the_frame_is_doing is empty")
    if not 4 <= len(propositions) <= 6:
        problems.append(f"{len(propositions)} propositions, expected 4-6")
    required = V2_FIELDS if _uses_role_enum(version) else V1_FIELDS
    known_ids: set[str] = set()
    for index, proposition in enumerate(propositions, start=1):
        missing = [
            field for field in required
            if not str(proposition.get(field) or "").strip()
        ]
        if missing:
            problems.append(f"proposition {index} missing {', '.join(missing)}")
        for field in ("formal_basis", "semantic_basis", "spatial_basis"):
            if re.search(r"\d", str(proposition.get(field) or "")):
                problems.append(f"proposition {index} {field} contains a number")
        if _uses_role_enum(version):
            side = str(proposition.get("figure_side") or "")
            if side and side not in FIGURE_SIDES:
                problems.append(
                    f"proposition {index} figure_side {side!r} is not one of {FIGURE_SIDES}"
                )
            shares = proposition.get("shares_regions_with")
            if not isinstance(shares, list):
                problems.append(f"proposition {index} shares_regions_with is not a list")
            else:
                unknown = [item for item in shares if item not in known_ids]
                if unknown:
                    problems.append(
                        f"proposition {index} shares_regions_with names {unknown}, "
                        f"which are not earlier proposition ids"
                    )
        known_ids.add(str(proposition.get("id") or f"P{index}"))
    return problems


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

    (out_dir / "02-curator-raw.txt").write_text(result["raw"] or "", encoding="utf-8")
    parsed = result["parsed"]
    propositions = (parsed or {}).get("propositions") or [] if isinstance(parsed, dict) else []
    problems = _shape_problems(parsed, version)
    _write_json(out_dir / "03-curator.json", {
        "id": pal,
        "stage": "A-curator",
        "prompt_version": version,
        "blind": True,
        "parsed": parsed,
        "shape_problems": problems,
        "diversity": _diversity(propositions),
    })

    manifest = {
        "id": pal,
        "status": "ok" if propositions else "unparsed",
        "experiment": EXPERIMENT,
        "space": SPACE,
        "stage": "A-curator",
        "prompt_version": version,
        "created": datetime.now(timezone.utc).isoformat(),
        "source": f"{CORPUS_SPACE}/corpus/{entry['corpus_image']}",
        "blindness": {
            "sees": ["source image", "curation system prompt", "curation user prompt"],
            "withheld": [
                "film title/year/director", "E4 annotation evidence",
                "E5 selections", "E6 human review", "E7 rankings",
                "candidate inventories", "SAM masks", "hierarchy regions",
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
        "propositions": len(propositions),
        "shape_problems": problems,
    }
    _write_json(out_dir / "00-manifest.json", manifest)
    return manifest


def _version_pair(project: Path, version: str) -> dict:
    return prompt_io.load_pair(
        str(project), ROLE,
        system_file=PROMPT_STEM.format(kind="system", version=version),
        user_file=PROMPT_STEM.format(kind="user", version=version),
    )


def pilot(project_path: str, *, versions: list[str] | None = None,
          only: list[str] | None = None) -> Path:
    """Run stage A over the diagnostic subset and report prompt behaviour."""
    from data.annotate import _load_text_generation_pipeline

    project = Path(project_path)
    corpus_dir = project / "outputs" / "tests" / CORPUS_SPACE / "corpus"
    out_root = project / "outputs" / "tests" / SPACE / EXPERIMENT
    out_root.mkdir(parents=True, exist_ok=True)
    corpus = {
        entry["id"]: entry
        for entry in _read_json(corpus_dir / "corpus-manifest.json")["images"]
    }
    selected = list(only or DIAGNOSTIC)
    unexpected = set(selected) - set(PILOT)
    if unexpected:
        raise ValueError(f"E8 is restricted to the frozen pilot: {sorted(unexpected)}")
    chosen_versions = list(versions or VERSIONS)
    pairs = {version: _version_pair(project, version) for version in chosen_versions}

    print(f"loading {MODEL} \u2026", flush=True)
    pipeline = _load_text_generation_pipeline(str(project), MODEL)
    results = {}
    for version in chosen_versions:
        version_root = out_root / f"stage-a-{version}"
        manifests = []
        for pal in selected:
            print(f"[E8] {version} {pal}", flush=True)
            manifests.append(
                run_one(pal, corpus[pal], project, version, pairs[version],
                        pipeline, version_root)
            )
        results[version] = manifests

    _write_json(out_root / "pilot-report.json", {
        "experiment": EXPERIMENT,
        "stage": "A-curator",
        "created": datetime.now(timezone.utc).isoformat(),
        "purpose": (
            "Prompt pilot only. Establishes whether the curatorial prompt elicits "
            "plural, image-specific readings before the rest of E8 is built."
        ),
        "model": MODEL,
        "images": selected,
        "versions": {
            version: {
                "prompts": {
                    "system": pairs[version]["system_file"],
                    "user": pairs[version]["user_file"],
                },
                "results": [
                    {
                        "id": manifest["id"],
                        "status": manifest["status"],
                        "propositions": manifest["propositions"],
                        "shape_problems": manifest["shape_problems"],
                    }
                    for manifest in results[version]
                ],
            }
            for version in chosen_versions
        },
    })
    print(f"[E8] stage A pilot: {len(chosen_versions)} prompt versions x "
          f"{len(selected)} images -> {out_root}")
    return out_root


def main() -> None:
    parser = argparse.ArgumentParser(description="E8 stage A curatorial pilot")
    parser.add_argument("mode", choices=("pilot",))
    parser.add_argument("--project", required=True, help="Crossing project root")
    parser.add_argument("--versions", nargs="*", help="Prompt versions to run")
    parser.add_argument("--only", nargs="*", help="Restrict to these pilot ids")
    args = parser.parse_args()
    pilot(args.project, versions=args.versions or None, only=args.only or None)


if __name__ == "__main__":
    main()
