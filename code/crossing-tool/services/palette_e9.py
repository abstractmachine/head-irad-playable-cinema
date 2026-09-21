"""E9-A — figure curation, then field curation conditioned on the figure.

    crossing index palette e9-propose-all --title "The Searchers"

The experimental change from ``measured-review-v1`` is the semantic
architecture and nothing else:

    image -> FIGURE reading -> ONE figure material
          -> FIELD reading (given the figure) -> ONE field material
          -> operationalization -> SAM -> measured colour

Everything downstream of the material is deliberately frozen: the same
operationalizer prompt, the same SAM model and thresholds, the same
union-of-masks rule, the same mean-of-mask measurement, the same frozen
control palette. Changing the semantics and the measurement at once would
make the result uninterpretable.

Two findings from the tmdb_95864 review drive the design. The figure was
hand-overridden about twice as often as the background and, where only one
role was replaced, it was the figure by roughly seven to one — so the figure
gets its own pass and is settled first. And 47% of the materials the old chain
named joined several visibly distinct things, of which 68% failed segmentation
or returned multiple masks whose union averaged to a colour belonging to
nothing in the frame — so a compound material is now repaired before
measurement rather than recorded as a shape problem and measured anyway.

No human review decision is ever supplied to any prompt here.
"""

from __future__ import annotations

import re
import uuid
from pathlib import Path

from data import palette_review as store

E9_VERSION = "e9a-figure-field-v1"
E9_EXPERIMENT = "E9-A-figure-field"

STAGES = {
    "figure": ("prompts/palettes/palette-e9-figure-system-2026-09-21-v2.txt",
               "prompts/palettes/palette-e9-figure-user-2026-09-21-v2.txt"),
    "field": ("prompts/palettes/palette-e9-field-system-2026-09-21-v2.txt",
              "prompts/palettes/palette-e9-field-user-2026-09-21-v2.txt"),
    "repair": ("prompts/palettes/palette-e9-material-repair-system-2026-09-21-v1.txt",
               "prompts/palettes/palette-e9-material-repair-user-2026-09-21-v1.txt"),
    # Frozen from measured-review-v1, unchanged.
    "operationalize": ("prompts/palettes/palette-operationalize-system-2026-09-15-v1.txt",
                       "prompts/palettes/palette-operationalize-user-2026-09-15-v1.txt"),
}

CURATION_TOKENS = 1536
REPAIR_TOKENS = 512

# A compound material is repaired, not measured. Two attempts, then the choice
# is marked unavailable rather than segmented against the wrong pixels.
MAX_REPAIRS = 2

CHOICE_PRIMARY = "1"
CHOICE_FIGURE_ALT = "2"
CHOICE_FIELD_ALT = "3"
CHOICE_CONTROL = "4"

STRATEGIES = {
    CHOICE_PRIMARY: ("e9_primary", "PRIMARY"),
    CHOICE_FIGURE_ALT: ("e9_figure_alternative", "FIGURE-ALT"),
    CHOICE_FIELD_ALT: ("e9_field_alternative", "FIELD-ALT"),
    CHOICE_CONTROL: ("control", "CONTROL"),
}

# Phrasings that signal one chromatically uniform field despite a conjunction.
# They only suppress the Python flag; a flagged material is still decided by
# the repair pass, never by this list alone.
_ONE_FIELD_HINTS = re.compile(
    r"\b(uniformly|the same|one continuous|continuous|unbroken|throughout|"
    r"all of the same|evenly)\b", re.IGNORECASE)


# ---------------------------------------------------------------------------
# Material coherence
# ---------------------------------------------------------------------------

def material_problem(material: str) -> str | None:
    """Why this material cannot go to SAM as written, or None if it can.

    Python only *flags* a candidate compound. Whether a conjunction really
    joins two colour-bearing things is a judgement about the image, so it is
    settled by the repair pass, never by this function alone.
    """
    from scripts.palette_lab.e8_articulation import _is_conjunctive
    from scripts.palette_lab.e8_carrier import _is_bare_colour

    text = str(material or "").strip()
    if not text:
        return "no material was named"
    if _is_bare_colour(text):
        return "names a colour rather than a material"
    if _is_conjunctive(text) and not _ONE_FIELD_HINTS.search(text):
        return ("joins several visibly distinct materials, whose union would "
                "average to a colour belonging to none of them")
    return None


def same_material(left: str, right: str) -> bool:
    from scripts.palette_lab.e8_palette_review import normalise_material

    return normalise_material(left) == normalise_material(right)


# ---------------------------------------------------------------------------
# Prompt plumbing
# ---------------------------------------------------------------------------

def prompt_pairs(project_path: str) -> dict:
    from scripts.palette_lab import prompts as prompt_io

    return {
        role: prompt_io.load_pair(project_path, f"e9-{role}",
                                  system_file=system, user_file=user)
        for role, (system, user) in STAGES.items()
    }


def _call(pipeline, pair, variables, images, tokens):
    from scripts.palette_lab import prompts as prompt_io
    from scripts.palette_lab import stages

    system = prompt_io.strip_comments(pair["system"])
    user = prompt_io.substitute(prompt_io.strip_comments(pair["user"]), variables)
    return stages.call_qwen(pipeline, system, user, images, tokens)


def _reading_text(record: dict, role: str) -> str:
    """The curatorial reading, rendered for the field and repair passes."""
    if role == "figure":
        return "\n".join([
            f"what the frame asks us to notice: {record.get('figure', '')}",
            f"why it matters: {record.get('why_it_matters', '')}",
            f"how it is set apart: {record.get('visible_distinction', '')}",
            f"convention in play: {record.get('genre_note') or 'none noted'}",
        ])
    return "\n".join([
        f"the world the figure is set against: {record.get('field', '')}",
        f"why this field: {record.get('why_this_field', '')}",
        f"relation to the figure: {record.get('relation_to_figure', '')}",
    ])


# ---------------------------------------------------------------------------
# Curatorial passes
# ---------------------------------------------------------------------------

def figure_pass(pipeline, pairs: dict, images: list) -> dict:
    """What is the frame asking us to notice? Sees the image and nothing else."""
    result = _call(pipeline, pairs["figure"], {}, images, CURATION_TOKENS)
    parsed = result["parsed"]
    if not isinstance(parsed, dict) or not isinstance(parsed.get("primary"), dict):
        raise ValueError("figure pass did not return a primary reading")
    return parsed


def field_pass(pipeline, pairs: dict, images: list, figure: dict,
               figure_material: str) -> dict:
    """Against what world is that figure articulated? Conditioned on the figure."""
    result = _call(pipeline, pairs["field"], {
        "figure_reading": _reading_text(figure, "figure"),
        "figure_material": figure_material,
    }, images, CURATION_TOKENS)
    parsed = result["parsed"]
    if not isinstance(parsed, dict) or not isinstance(parsed.get("primary"), dict):
        raise ValueError("field pass did not return a primary field")
    return parsed


def resolve_material(pipeline, pairs: dict, images: list, material: str,
                     reading: str, role: str) -> dict:
    """Return a measurable material, repairing a compound one up to MAX_REPAIRS.

    The repair never re-curates: it narrows the description to the component
    that carries the reading it was already chosen for.
    """
    history = []
    current = str(material or "").strip()
    for attempt in range(MAX_REPAIRS + 1):
        problem = material_problem(current)
        if problem is None:
            return {"material": current, "status": "ok", "repairs": history,
                    "original": str(material or "").strip()}
        if attempt == MAX_REPAIRS:
            history.append({"attempt": attempt + 1, "rejected": current,
                            "problem": problem, "status": "gave_up"})
            break
        result = _call(pipeline, pairs["repair"], {
            "material": current, "reading": reading, "role": role,
            "problem": problem,
        }, images, REPAIR_TOKENS)
        parsed = result["parsed"] if isinstance(result["parsed"], dict) else {}
        status = str(parsed.get("status") or "").strip().lower()
        repaired = str(parsed.get("material") or "").strip()
        history.append({
            "attempt": attempt + 1,
            "rejected": current,
            "problem": problem,
            "status": status or "unparsed",
            "repaired": repaired or None,
            "dropped": parsed.get("dropped"),
            "reason": parsed.get("reason"),
        })
        if status == "unrepairable" or not repaired:
            return {"material": None, "status": "unrepairable",
                    "repairs": history, "original": str(material or "").strip()}
        current = repaired
    return {"material": None, "status": "unrepaired_compound",
            "repairs": history, "original": str(material or "").strip()}

# ---------------------------------------------------------------------------
# Proposal assembly
# ---------------------------------------------------------------------------

def _control_choice(project_path: str, filename: str, media_type: str,
                    shot_id: str) -> dict:
    """Choice 4, frozen exactly as measured-review-v1 produced it."""
    from scripts.palette_lab.e8_palette_review import CONTROL_METHOD, _colour_record
    from services.palette_review import _control_palette

    palette = _control_palette(project_path, filename, media_type, shot_id)
    colours = []
    materials = []
    if palette:
        materials = ["production foreground", "production background"]
        colours = [_colour_record(palette.get("foreground_rgb")),
                   _colour_record(palette.get("background_rgb"))]
    strategy, label = STRATEGIES[CHOICE_CONTROL]
    return {
        "strategy": strategy,
        "label": label,
        "active": bool(colours and all(colours)),
        "source_stage": "corpus-manifest.json (frozen production palette)",
        "materials": materials,
        "colours": colours,
        "converges_with": [],
        "method": CONTROL_METHOD,
        "source_note": "deterministic bottom-up baseline; no narratological evidence",
        "inactive_reason": None if colours else "no production palette for this shot",
    }


def _curated_choice(key: str, figure: dict, field: dict,
                    figure_material: dict, field_material: dict) -> dict:
    """One E9-A choice, before measurement. Unavailable is a real answer."""
    strategy, label = STRATEGIES[key]
    reasons = []
    for role, resolved in (("figure", figure_material), ("field", field_material)):
        if resolved is None:
            reasons.append(f"no {role} reading")
        elif not resolved.get("material"):
            reasons.append(f"{role} material {resolved.get('status')}")
    materials = []
    if not reasons:
        materials = [figure_material["material"], field_material["material"]]
        if same_material(materials[0], materials[1]):
            reasons.append("figure and field resolved to the same material")
            materials = []
    return {
        "strategy": strategy,
        "label": label,
        "active": not reasons,
        "source_stage": f"{E9_EXPERIMENT}/figure+field",
        "materials": materials,
        "converges_with": [],
        "inactive_reason": "; ".join(reasons) or None,
        "e9": {
            "figure_reading": figure,
            "field_reading": field,
            "figure_material": figure_material,
            "field_material": field_material,
        },
    }


def _measure_choices(pipeline, pairs, segmenter, images, choices: dict) -> dict:
    """Operationalize and segment every named material. Frozen from E8."""
    from scripts.palette_lab import e8_palette_review as REV

    wanted = {}
    for choice in choices.values():
        if choice["strategy"] == "control" or not choice.get("active"):
            continue
        for material in choice.get("materials") or []:
            wanted.setdefault(REV.normalise_material(material), material)

    measured = {}
    for key, material in wanted.items():
        parsed = _call(pipeline, pairs["operationalize"], {"material": material},
                       images["frames"], REV.OPERATIONALIZE_TOKENS)["parsed"]
        patch = {
            "material": material,
            "parsed": parsed,
            "problems": REV.operationalization_problems(parsed, material),
            "status": str((parsed or {}).get("status") or "").strip().lower(),
            "targets": REV.localization_targets(parsed),
        }
        found = REV.locate_and_measure(segmenter, images["images"], patch)
        found["operationalization"] = parsed
        measured[key] = found

    for choice in choices.values():
        if choice["strategy"] == "control":
            continue
        records, colours = [], []
        for material in choice.get("materials") or []:
            found = measured.get(REV.normalise_material(material))
            if found is None:
                records.append(None)
                colours.append(None)
                continue
            records.append({
                "material": material,
                "operationalization": found.get("operationalization"),
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
        if choice.get("active") and (len(colours) != 2 or any(c is None for c in colours)):
            choice["active"] = False
            choice["inactive_reason"] = "measurement incomplete"
    return choices


def generate_frame_proposals(project_path: str, filename: str, media_type: str,
                             shot_id: str, *, models: dict,
                             image_path: str | None = None) -> dict:
    """Run E9-A over one frame and measure it. Raises on an unusable frame."""
    from scripts.palette_lab import stages
    from services.frame_match import best_frame_path

    source = Path(image_path or best_frame_path(
        project_path, media_type, filename, shot_id))
    if not source.exists():
        raise FileNotFoundError(f"No best frame for {shot_id}: {source}")

    pairs = prompt_pairs(project_path)
    images = stages.prepare_images(source)
    frames = [images["active"]]
    bundle = {"images": images, "frames": frames}
    pipeline = models["pipeline"]

    figure = figure_pass(pipeline, pairs, frames)
    primary_figure = figure["primary"]
    alternative_figure = figure.get("alternative")
    if isinstance(alternative_figure, dict) and same_material(
            alternative_figure.get("material", ""), primary_figure.get("material", "")):
        # A different wording of the same material is not a different reading.
        alternative_figure = None

    primary_figure_material = resolve_material(
        pipeline, pairs, frames, primary_figure.get("material"),
        _reading_text(primary_figure, "figure"), "figure")

    field = field_pass(pipeline, pairs, frames, primary_figure,
                       primary_figure_material.get("material")
                       or primary_figure.get("material") or "")
    primary_field = field["primary"]
    alternative_field = field.get("alternative")
    if isinstance(alternative_field, dict) and same_material(
            alternative_field.get("material", ""), primary_field.get("material", "")):
        alternative_field = None

    primary_field_material = resolve_material(
        pipeline, pairs, frames, primary_field.get("material"),
        _reading_text(primary_field, "field"), "field")

    choices = {CHOICE_PRIMARY: _curated_choice(
        CHOICE_PRIMARY, primary_figure, primary_field,
        primary_figure_material, primary_field_material)}

    # Choice 2: a genuinely different figure gets its own conditioned field.
    alt_field_for_alt_figure = None
    alt_figure_material = alt_figure_field_material = None
    if isinstance(alternative_figure, dict):
        alt_figure_material = resolve_material(
            pipeline, pairs, frames, alternative_figure.get("material"),
            _reading_text(alternative_figure, "figure"), "figure")
        if alt_figure_material.get("material"):
            second = field_pass(pipeline, pairs, frames, alternative_figure,
                                alt_figure_material["material"])
            alt_field_for_alt_figure = second["primary"]
            alt_figure_field_material = resolve_material(
                pipeline, pairs, frames, alt_field_for_alt_figure.get("material"),
                _reading_text(alt_field_for_alt_figure, "field"), "field")
    choices[CHOICE_FIGURE_ALT] = _curated_choice(
        CHOICE_FIGURE_ALT, alternative_figure, alt_field_for_alt_figure,
        alt_figure_material, alt_figure_field_material)
    if alternative_figure is None:
        choices[CHOICE_FIGURE_ALT]["inactive_reason"] = (
            "the figure pass found only one convincing reading")

    # Choice 3: the same figure against a genuinely different field.
    alt_field_material = None
    if isinstance(alternative_field, dict):
        alt_field_material = resolve_material(
            pipeline, pairs, frames, alternative_field.get("material"),
            _reading_text(alternative_field, "field"), "field")
    choices[CHOICE_FIELD_ALT] = _curated_choice(
        CHOICE_FIELD_ALT, primary_figure, alternative_field,
        primary_figure_material, alt_field_material)
    if alternative_field is None:
        choices[CHOICE_FIELD_ALT]["inactive_reason"] = (
            "the field pass found only one convincing counterfield")

    choices[CHOICE_CONTROL] = _control_choice(
        project_path, filename, media_type, shot_id)

    _measure_choices(pipeline, pairs, models["segmenter"], bundle, choices)
    _mark_convergence(choices)

    return {
        "choices": choices,
        "provenance": {
            "experiment": E9_EXPERIMENT,
            "figure": figure,
            "field": field,
            "alternative_field_pass": alt_field_for_alt_figure,
            "source_image": str(source),
            "prompts": {role: {"system": pair["system_file"],
                               "user": pair["user_file"]}
                        for role, pair in pairs.items()},
            "models": {
                "text": models.get("text_model"),
                "segmenter": models.get("segmenter_name"),
                "device": models.get("device"),
            },
            "measurement": {
                "representative_colour": "mean sRGB of the masked pixels",
                "implementation": "scripts.palette_lab.two_color._measure_mask",
                "weighting": "none",
                "frozen_from": "measured-review-v1",
            },
        },
    }


def _mark_convergence(choices: dict) -> dict:
    """Deactivate a later choice that rests on the same material pair as an earlier one."""
    from scripts.palette_lab.e8_palette_review import _pair_key

    seen = {}
    for key in sorted(choices, key=int):
        choice = choices[key]
        materials = choice.get("materials") or []
        if not choice.get("active") or len(materials) != 2:
            continue
        pair = _pair_key(materials)
        if pair in seen:
            choice["converges_with"] = [seen[pair]]
            choice["active"] = False
            choice["inactive_reason"] = "same materials as choice " + seen[pair]
            choices[seen[pair]].setdefault("converged_by", []).append(key)
        else:
            seen[pair] = key
    return choices


# ---------------------------------------------------------------------------
# Batch
# ---------------------------------------------------------------------------

def _public_choices(choices: dict) -> dict:
    return {key: {name: value for name, value in choice.items() if name != "mask"}
            for key, choice in choices.items()}


def create_frame(project_path: str, filename: str, media_type: str, shot_id: str,
                 *, models: dict, force: bool = False) -> dict:
    """Generate E9-A proposals for one frame. Never implies human acceptance."""
    record = store.load_review(project_path, filename, media_type)
    entry = store.frame(record, shot_id)
    generation = entry.get("proposal_generation") or {}
    if not force and store.has_proposals(entry) and generation.get("version") == E9_VERSION:
        return {"status": "skipped", "shot_id": shot_id,
                "reason": "E9-A proposals already present"}

    generation_id = uuid.uuid4().hex[:12]
    try:
        result = generate_frame_proposals(
            project_path, filename, media_type, shot_id, models=models)
    except Exception as exc:
        store.record_generation_failure(
            entry, E9_VERSION, generation_id, f"{type(exc).__name__}: {exc}")
        store.save_review(project_path, filename, media_type, record)
        return {"status": "failed", "shot_id": shot_id, "error": str(exc)}

    store.record_generation(entry, E9_VERSION, generation_id,
                            _public_choices(result["choices"]), result["provenance"])
    store.save_review(project_path, filename, media_type, record)
    return {"status": "generated", "shot_id": shot_id, "generation_id": generation_id}


def create_all(project_path: str, filename: str, media_type: str, *,
               models: dict | None = None, force: bool = False,
               shot_ids: list | None = None,
               on_progress=None, should_cancel=None) -> dict:
    """Generate across one media item, persisting each frame as it completes."""
    from services.palette_review import _free_models, load_models

    frames = [item for item in _list_frames(project_path, filename, media_type)
              if item["available"]]
    if shot_ids is not None:
        wanted = set(shot_ids)
        frames = [item for item in frames if item["shot_id"] in wanted]
    summary = {"filename": filename, "media_type": media_type,
               "version": E9_VERSION, "considered": len(frames),
               "generated": 0, "skipped": 0, "failed": 0,
               "cancelled": False, "errors": []}
    if not frames:
        summary["complete"] = True
        return summary

    owned = models is None
    if owned:
        models = load_models(project_path)
    try:
        for position, item in enumerate(frames, start=1):
            if should_cancel is not None and should_cancel():
                summary["cancelled"] = True
                break
            result = create_frame(project_path, filename, media_type,
                                  item["shot_id"], models=models, force=force)
            summary[{"generated": "generated", "skipped": "skipped"}
                    .get(result["status"], "failed")] += 1
            if result["status"] == "failed":
                summary["errors"].append((item["shot_id"], result.get("error", "")))
            if on_progress is not None:
                on_progress(position, len(frames), item["shot_id"], result["status"])
    finally:
        if owned:
            _free_models(models)
    summary["complete"] = not summary["cancelled"]
    return summary


def _list_frames(project_path: str, filename: str, media_type: str) -> list:
    from services.palette_review import list_frames

    return list_frames(project_path, filename, media_type)
