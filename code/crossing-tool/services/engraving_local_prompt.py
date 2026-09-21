"""Prompt construction for the local engraving converter.

Reconstruction and stylization are deliberately separate prompts even though
they run on the same model, so each can be tuned — or disabled — without
disturbing the other.

Built-in templates encode the behaviour established by the prior engraving
review.  A project may override either stage by dropping a ``.txt`` file into
``<project>/prompts/engravings-local/``; the alphabetically last file matching
the stage prefix wins, mirroring ``services/engraving_prompt.py``.

Templates use ``string.Template`` (``$variable``) syntax.  Canonical variables:

    $label       — silhouette subject label (fallible retrieval metadata)
    $field       — category / field (e.g. "animals")
    $movie       — film title + year
    $shot_id     — canonical shot identifier
    $description — annotation-derived description text
    $motif       — cinematic motif string for the shot
    $evidence    — assembled evidence/uncertainty notes for this object

Unknown placeholders are left unchanged by ``safe_substitute``; missing
variables default to empty strings.
"""

from __future__ import annotations

import string
from pathlib import Path

PROMPT_STAGES = ("repair", "engrave")

PROMPT_VARIABLES = (
    "label",
    "field",
    "movie",
    "shot_id",
    "description",
    "motif",
    "evidence",
    "parts",
)

# Naming the parts that must be present is what actually makes the model
# reconstruct rather than pass the cut-out through unchanged, so the hint is
# conditioned on the catalog ``field`` instead of being generic.
_PART_HINTS = {
    "animals": "The head, face, every leg and every foot",
    "animal": "The head, face, every leg and every foot",
    "characters": "The head, face, both arms, both hands and both legs",
    "character": "The head, face, both arms, both hands and both legs",
    "people": "The head, face, both arms, both hands and both legs",
}
_DEFAULT_PART_HINT = "Every end, edge, fitting and attachment"

DEFAULT_REPAIR_TEMPLATE = """\
This cut-out is incomplete: parts of the $label were hidden or cut off by the mask.

Redraw it as one complete $label, whole and unobstructed, on plain white. $parts must be clearly visible. Keep the same identity, surface pattern, viewing angle and proportions as the cut-out, and keep damage that belongs to the object itself.

Attach every part where it really belongs, on the correct side, with correct overlap. Keep rigid parallel parts straight and parallel. Keep the number of parts the same as the cut-out shows.

$evidence

Show only the $label, with nothing beneath it. No perch, stand, base, support, ground, shadow, scenery, text or border.
"""

DEFAULT_ENGRAVE_TEMPLATE = """\
Redraw this object as a nineteenth-century catalogue engraving: black ink line work on plain white.

Keep the shape, pose, proportions and every part exactly as shown. Change only the medium.

Use a firm dark outline. Model the form with hatching and cross-hatching that follows the surface, with some stipple. Keep strong contrast and clean white space. Make the lines bold enough to stay readable when printed small; avoid faint hairlines, flat grey tone and soft blur.

$evidence

Show only the object on plain white. No shadow, scenery, support, text, border or watermark.
"""

_DEFAULT_TEMPLATES = {
    "repair": DEFAULT_REPAIR_TEMPLATE,
    "engrave": DEFAULT_ENGRAVE_TEMPLATE,
}

_PROMPTS_SUBDIR = Path("prompts") / "engravings-local"


class EngravingPromptError(RuntimeError):
    """Raised when a requested prompt stage is unknown."""


def load_template(project_path: str | Path, stage: str) -> tuple[str, str]:
    """Return *(source_name, template_text)* for *stage*.

    Falls back to the built-in template when the project defines no override.
    """
    if stage not in PROMPT_STAGES:
        raise EngravingPromptError(
            f"Unknown prompt stage {stage!r}. Valid stages: {', '.join(PROMPT_STAGES)}"
        )

    prompts_dir = Path(project_path) / _PROMPTS_SUBDIR
    if prompts_dir.is_dir():
        candidates = sorted(p for p in prompts_dir.glob(f"{stage}-*.txt") if p.is_file())
        if candidates:
            chosen = candidates[-1]
            text = chosen.read_text(encoding="utf-8").strip()
            if text:
                return chosen.name, text

    return f"<built-in {stage}>", _DEFAULT_TEMPLATES[stage].strip()


def build_context(evidence: dict, extra: dict | None = None) -> dict:
    """Return the ``$variable`` context for an assembled evidence bundle."""
    record = evidence.get("record", {})
    review = evidence.get("review", {})

    if review.get("ambiguous"):
        evidence_note = (
            "Where the source does not show what is missing, keep that area plain "
            "and simple rather than inventing confident detail."
        )
    else:
        evidence_note = (
            "Complete the object only where the source clearly implies it continues."
        )

    context = {
        "label": record.get("label") or "object",
        "field": record.get("field") or "",
        "movie": record.get("filename_stem") or "",
        "shot_id": record.get("shot_id") or "",
        "description": record.get("description") or "",
        "motif": record.get("motif") or "",
        "evidence": evidence_note,
        "parts": _PART_HINTS.get(str(record.get("field") or "").lower(), _DEFAULT_PART_HINT),
    }
    if extra:
        context.update({k: str(v) for k, v in extra.items() if v is not None})
    return context


def expand(template_text: str, context: dict | None) -> str:
    """Expand ``$variable`` placeholders, leaving unknown ones unchanged."""
    defaults = {name: "" for name in PROMPT_VARIABLES}
    if context:
        defaults.update({k: str(v) for k, v in context.items() if v is not None})
    return string.Template(template_text).safe_substitute(defaults).strip()


def build_prompt(project_path: str | Path, stage: str, evidence: dict, extra: dict | None = None) -> dict:
    """Return the compiled prompt for *stage* plus its provenance."""
    source_name, template_text = load_template(project_path, stage)
    context = build_context(evidence, extra)
    return {
        "stage": stage,
        "template_source": source_name,
        "context": context,
        "prompt": expand(template_text, context),
    }
