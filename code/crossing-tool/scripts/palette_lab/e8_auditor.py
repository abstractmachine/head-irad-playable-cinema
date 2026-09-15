"""E8 stage E pilot — is the proposed measurement actually evidence?

    uv run python -m scripts.palette_lab.e8_auditor pilot --project /path

One call per frozen critic-v3 relation. The auditor sees the image, that
relation, and the spatializer-v1 specification written for it. It is blind to
the E6 human review, E5 selections, E7 rankings, previous SAM masks, candidate
inventories and every measured colour.

Nothing is segmented or measured here. The output is a pre-registration of what
would count as evidence, written before the evidence exists.
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
from scripts.palette_lab.e8_spatializer import CRITIC_STAGE


ROLE = "auditor"
PROMPT_FILES = {
    "v1": ("prompts/palettes/palette-auditor-system-2026-09-15-v1.txt",
           "prompts/palettes/palette-auditor-user-2026-09-15-v1.txt"),
}
LATEST = "v1"
SPATIALIZER_STAGE = "stage-d-v1"

REFERENT_STATUS = (
    "atomic", "distributed_field", "compound_needs_decomposition", "not_spatializable",
)
EVIDENCE_STATUS = ("direct", "proxy", "descriptive_only", "not_operationalized")
# Only these two carry a claim that something would be learned, so only these
# two require an observation to have been proposed.
EVIDENCE_NEEDING_OBSERVATION = ("direct", "proxy")
SIDES = ("side_a", "side_b")
_REFERENCE_ID = re.compile(r"\b[PR]\d+\b")


def _prose_has_number(value: str) -> bool:
    return bool(re.search(r"\d", _REFERENCE_ID.sub("", str(value or ""))))


def _spec_referents(spec: dict) -> list[tuple[str, str]]:
    """Every (side, phrase) the spatializer proposed for this relation."""
    out = []
    for side in SIDES:
        block = spec.get(side) if isinstance(spec.get(side), dict) else {}
        for referent in block.get("referents") or []:
            if isinstance(referent, dict) and str(referent.get("phrase") or "").strip():
                out.append((side, str(referent["phrase"]).strip()))
    return out


def render_proposal(relation: dict, spec: dict) -> str:
    """Lay out the curatorial relation and the specification written for it."""
    lines = [
        "THE CURATORIAL RELATION",
        "",
        f"  id: {relation.get('id', '?')}",
        f"  side A: {relation.get('side_a', '')}",
        f"  side B: {relation.get('side_b', '')}",
        f"  what holds them together: {relation.get('why_these_are_one_relation', '')}",
        f"  curatorial significance: {relation.get('curatorial_significance', '')}",
        f"  figure side: {relation.get('figure_side', '')}",
        f"  orientation reasoning: {relation.get('orientation_reasoning', '')}",
        "",
        "THE PROPOSED OPERATIONALIZATION",
        "",
    ]
    for side in SIDES:
        block = spec.get(side) if isinstance(spec.get(side), dict) else {}
        lines.append(f"  {side}")
        lines.append(f"    concept: {block.get('concept', '')}")
        for referent in block.get("referents") or []:
            if not isinstance(referent, dict):
                continue
            lines.append(
                f"    phrase to find: \"{referent.get('phrase', '')}\""
                f"  [{referent.get('relation_to_concept', '')}]"
            )
            lines.append(f"        covers: {referent.get('covers', '')}")
        lines.append(f"    expected of these pixels: {block.get('measurable_expectations', '')}")
        lines.append(f"    stated as not measurable: {block.get('interpretive_content', '')}")
        lines.append("")
    relation_block = spec.get("relation") if isinstance(spec.get("relation"), dict) else {}
    lines += [
        "  the relation between them",
        f"    relational term: {relation_block.get('relational_term', '')}",
        f"    proposed observations: {relation_block.get('measurable_proxies', '')}",
        f"    stated as not measurable: {relation_block.get('interpretive_content', '')}",
        "",
        f"  the spatializer's own doubt: {spec.get('uncertainty', '')}",
    ]
    return "\n".join(lines).strip()


def shape_problems(parsed, relation_id: str, spec: dict) -> list[str]:
    """Check the audit's structure, not its judgements."""
    if not isinstance(parsed, dict):
        return ["response did not parse as a JSON object"]
    problems = []
    if str(parsed.get("relation_id") or "") != relation_id:
        problems.append(
            f"relation_id {parsed.get('relation_id')!r} does not match {relation_id!r}"
        )
    for field in ("curatorial_relation_preserved", "audit_summary", "uncertainty"):
        if not str(parsed.get(field) or "").strip():
            problems.append(f"{field} is empty")

    referent_audits = parsed.get("referent_audits")
    if not isinstance(referent_audits, list) or not referent_audits:
        problems.append("no referent_audits")
        referent_audits = []
    audited: list[str] = []
    for index, audit in enumerate(referent_audits, start=1):
        if not isinstance(audit, dict):
            problems.append(f"referent audit {index} is not an object")
            continue
        side = str(audit.get("side") or "")
        if side and side not in SIDES:
            problems.append(f"referent audit {index} side {side!r} is not one of {SIDES}")
        status = str(audit.get("referent_status") or "")
        if status not in REFERENT_STATUS:
            problems.append(
                f"referent audit {index} referent_status {status!r} "
                f"is not one of {REFERENT_STATUS}"
            )
        if not str(audit.get("limits") or "").strip():
            problems.append(f"referent audit {index} has no limits")
        phrase = str(audit.get("original_referent") or "").strip()
        if not phrase:
            problems.append(f"referent audit {index} has no original_referent")
        else:
            audited.append(phrase)
        targets = audit.get("operationalization")
        if not isinstance(targets, list):
            problems.append(f"referent audit {index} operationalization is not a list")
            continue
        if any(not str(target or "").strip() for target in targets):
            problems.append(f"referent audit {index} has an empty operationalization entry")
        if status == "compound_needs_decomposition" and len(targets) < 2:
            problems.append(
                f"referent audit {index} is compound but was not decomposed"
            )
        if status == "not_spatializable" and targets:
            problems.append(
                f"referent audit {index} is not spatializable yet proposes targets"
            )

    proposed = [phrase for _, phrase in _spec_referents(spec)]
    missing = [phrase for phrase in proposed if phrase not in audited]
    if missing:
        problems.append(f"spatializer referents never audited: {missing}")
    duplicated = sorted({phrase for phrase in audited if audited.count(phrase) > 1})
    if duplicated:
        problems.append(f"referents audited more than once: {duplicated}")

    evidence_audits = parsed.get("evidence_audits")
    if not isinstance(evidence_audits, list) or not evidence_audits:
        problems.append("no evidence_audits")
        evidence_audits = []
    for index, audit in enumerate(evidence_audits, start=1):
        if not isinstance(audit, dict):
            problems.append(f"evidence audit {index} is not an object")
            continue
        status = str(audit.get("evidence_status") or "")
        if status not in EVIDENCE_STATUS:
            problems.append(
                f"evidence audit {index} evidence_status {status!r} "
                f"is not one of {EVIDENCE_STATUS}"
            )
        for field in ("claim_component", "what_it_cannot_support"):
            if not str(audit.get(field) or "").strip():
                problems.append(f"evidence audit {index} has no {field}")
        observation = str(audit.get("proposed_observation") or "").strip()
        if status in EVIDENCE_NEEDING_OBSERVATION and not observation:
            problems.append(
                f"evidence audit {index} claims {status!r} evidence "
                f"without a proposed observation"
            )
        if _prose_has_number(audit.get("what_it_could_support")):
            problems.append(f"evidence audit {index} what_it_could_support contains a number")
    return problems


def _counts(values: list[str], vocabulary: tuple[str, ...]) -> dict[str, int]:
    return {name: values.count(name) for name in vocabulary if values.count(name)}


def summarise(parsed, spec: dict) -> dict:
    """Tally the audit so it can be scanned without reading every field."""
    parsed = parsed if isinstance(parsed, dict) else {}
    referent_audits = [a for a in (parsed.get("referent_audits") or []) if isinstance(a, dict)]
    evidence_audits = [a for a in (parsed.get("evidence_audits") or []) if isinstance(a, dict)]
    statuses = [str(a.get("referent_status") or "") for a in referent_audits]
    evidence = [str(a.get("evidence_status") or "") for a in evidence_audits]
    return {
        "spatializer_referents": len(_spec_referents(spec)),
        "referents_audited": len(referent_audits),
        "referent_status": _counts(statuses, REFERENT_STATUS),
        "decomposed_targets": sum(
            len(a.get("operationalization") or [])
            for a in referent_audits
            if str(a.get("referent_status")) == "compound_needs_decomposition"
        ),
        "claim_components": len(evidence_audits),
        "evidence_status": _counts(evidence, EVIDENCE_STATUS),
        "components_without_a_test": sum(
            1 for a in evidence_audits
            if str(a.get("evidence_status")) == "not_operationalized"
        ),
    }


def run_one(pal: str, entry: dict, project: Path, version: str, pair: dict,
            pipeline, out_root: Path) -> dict:
    out_dir = out_root / pal
    out_dir.mkdir(parents=True, exist_ok=True)
    corpus_dir = project / "outputs" / "tests" / CORPUS_SPACE / "corpus"
    experiment_root = project / "outputs" / "tests" / SPACE / EXPERIMENT
    critic = _read_json(experiment_root / CRITIC_STAGE / pal / "05-critic.json")["parsed"]
    spatialization = _read_json(
        experiment_root / SPATIALIZER_STAGE / pal / "08-spatialization.json"
    )
    specs = {
        record["relation_id"]: record for record in spatialization.get("records") or []
    }
    images = stages.prepare_images(corpus_dir / entry["corpus_image"])

    system = prompt_io.strip_comments(pair["system"])
    records = []
    for relation in critic.get("relations") or []:
        relation_id = str(relation.get("id") or "?")
        source = specs.get(relation_id)
        if source is None:
            continue
        spec = source.get("parsed") or {}
        user = prompt_io.substitute(
            prompt_io.strip_comments(pair["user"]),
            {"proposal": render_proposal(relation, spec)},
        )
        (out_dir / f"09-{relation_id}-user-prompt.txt").write_text(user, encoding="utf-8")
        result = stages.call_qwen(
            pipeline, system, user, [images["active"]], MAX_NEW_TOKENS
        )
        (out_dir / f"10-{relation_id}-raw.txt").write_text(
            result["raw"] or "", encoding="utf-8"
        )
        parsed = result["parsed"]
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
                "stage_d": f"{SPATIALIZER_STAGE}/{pal}/08-spatialization.json",
                "stage_d_referents": [phrase for _, phrase in _spec_referents(spec)],
            },
            "parsed": parsed,
            "shape_problems": shape_problems(parsed, relation_id, spec),
            "summary": summarise(parsed, spec),
            "device": result["device"],
        })

    _write_json(out_dir / "11-pre-cv-audit.json", {
        "id": pal,
        "stage": "E-pre-cv-audit",
        "prompt_version": version,
        "blind": True,
        "note": (
            "Pre-registration of what would count as evidence. No segmentation "
            "was run and no colour was measured."
        ),
        "records": records,
    })
    manifest = {
        "id": pal,
        "status": "ok" if records else "no relations",
        "experiment": EXPERIMENT,
        "space": SPACE,
        "stage": "E-pre-cv-audit",
        "prompt_version": version,
        "created": datetime.now(timezone.utc).isoformat(),
        "inputs": {
            "image": f"{CORPUS_SPACE}/corpus/{entry['corpus_image']}",
            "stage_a": f"{CURATOR_STAGE}/{pal}/03-curator.json",
            "stage_b": f"{CRITIC_STAGE}/{pal}/05-critic.json",
            "stage_d": f"{SPATIALIZER_STAGE}/{pal}/08-spatialization.json",
            "relations": [record["relation_id"] for record in records],
        },
        "blindness": {
            "sees": ["source image", "one critic relation",
                     "its spatializer specification", "auditor system prompt",
                     "auditor user prompt"],
            "withheld": [
                "film title/year/director", "E5 selections", "E6 human review",
                "E7 rankings", "SAM masks", "candidate inventories",
                "hierarchy regions", "measured LAB values", "any CV outcome",
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
        "summary": {record["relation_id"]: record["summary"] for record in records},
    }
    _write_json(out_dir / "00-manifest.json", manifest)
    return manifest


def pilot(project_path: str, *, version: str = LATEST,
          only: list[str] | None = None) -> Path:
    """Audit every frozen relation's specification for the diagnostic subset."""
    from data.annotate import _load_text_generation_pipeline

    project = Path(project_path)
    corpus_dir = project / "outputs" / "tests" / CORPUS_SPACE / "corpus"
    out_root = (
        project / "outputs" / "tests" / SPACE / EXPERIMENT / f"stage-e-{version}"
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
        print(f"[E8] auditor {pal}", flush=True)
        manifests.append(
            run_one(pal, corpus[pal], project, version, pair, pipeline, out_root)
        )

    _write_json(out_root / "pre-cv-audit-report.json", {
        "experiment": EXPERIMENT,
        "stage": "E-pre-cv-audit",
        "created": datetime.now(timezone.utc).isoformat(),
        "purpose": (
            "Pre-CV audit pilot only. No SAM run, no measurement, no post-CV "
            "audit, no comparison with the human review."
        ),
        "model": MODEL,
        "prompt_version": version,
        "stage_a": CURATOR_STAGE,
        "stage_b": CRITIC_STAGE,
        "stage_d": SPATIALIZER_STAGE,
        "prompts": {"system": pair["system_file"], "user": pair["user_file"]},
        "images": {
            manifest["id"]: {
                "relations": manifest["inputs"]["relations"],
                "shape_problems": manifest["shape_problems"],
                "summary": manifest["summary"],
            }
            for manifest in manifests
        },
    })
    print(f"[E8] stage E pilot over {len(manifests)} images -> {out_root}")
    return out_root


def main() -> None:
    parser = argparse.ArgumentParser(description="E8 stage E pre-CV audit pilot")
    parser.add_argument("mode", choices=("pilot",))
    parser.add_argument("--project", required=True, help="Crossing project root")
    parser.add_argument("--version", default=LATEST, choices=tuple(PROMPT_FILES))
    parser.add_argument("--only", nargs="*", help="Restrict to these pilot ids")
    args = parser.parse_args()
    pilot(args.project, version=args.version, only=args.only or None)


if __name__ == "__main__":
    main()
