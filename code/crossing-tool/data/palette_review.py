"""Palette review records — generated proposals, human review, manual palettes.

One JSON per media item, beside the existing palette caches:

    <project>/data/palettes/review/<media_type>/<stem>.json

This is a separate file from ``data/palettes/<media_type>/<stem>.json`` on
purpose. The existing cache is the production bottom-up palette; this one
records what was *proposed* by the narratological pipeline, what a human
*decided* about those proposals, and anything they authored by hand. Neither
file destroys the other.

Five blocks are kept distinct per frame so no operation flattens another:

    proposal_generation  what ran, when, with which version — including failures
    proposals            the generated hypotheses themselves
    review               the human's answer (accepted / rejected), with the
                         generation id they were actually looking at
    manual               hand-authored figure/background plus mask provenance
    final_palette        the resolved two colours, and which block produced them

Every mutation here is a pure function over a record dict. Loading, saving and
orchestration belong to ``services.palette_review``.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

PALETTE_REVIEW_SCHEMA = 1

# Canonical two-colour role names, matching the existing palette cache.
ROLE_FIGURE = "foreground"
ROLE_BACKGROUND = "background"
ROLES = (ROLE_FIGURE, ROLE_BACKGROUND)

# Derived frame states. Never stored — always computed from the blocks above.
STATE_NOT_GENERATED = "not_generated"
STATE_GENERATING = "generating"
STATE_GENERATION_FAILED = "generation_failed"
STATE_UNREVIEWED = "unreviewed"
STATE_ACCEPTED = "accepted"
STATE_REJECTED = "rejected"
STATE_MANUAL = "manual"
STATE_MANUAL_INCOMPLETE = "manual_incomplete"
STATES = (
    STATE_NOT_GENERATED, STATE_GENERATING, STATE_GENERATION_FAILED,
    STATE_UNREVIEWED, STATE_ACCEPTED, STATE_REJECTED,
    STATE_MANUAL, STATE_MANUAL_INCOMPLETE,
)

ANSWER_ACCEPTED = "accepted"
ANSWER_REJECTED = "rejected"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def get_review_path(project_path: str, filename: str, media_type: str) -> Path:
    """Canonical review-record path for one media item."""
    stem = Path(filename).stem
    return (
        Path(project_path) / "data" / "palettes" / "review" / media_type
        / f"{stem}.json"
    )


def empty_record(filename: str, media_type: str, media_id: str = "") -> dict:
    return {
        "schema_version": PALETTE_REVIEW_SCHEMA,
        "media_type": media_type,
        "media_id": media_id,
        "filename": filename,
        "frames": {},
    }


def load_review(project_path: str, filename: str, media_type: str) -> dict:
    """Load a review record, or an empty one when none exists yet."""
    path = get_review_path(project_path, filename, media_type)
    if not path.exists():
        return empty_record(filename, media_type)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return empty_record(filename, media_type)
    if not isinstance(data.get("frames"), dict):
        data["frames"] = {}
    return data


def save_review(project_path: str, filename: str, media_type: str,
                record: dict) -> Path:
    """Atomically persist a review record."""
    from data.annotate import atomic_write_text

    path = get_review_path(project_path, filename, media_type)
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(
        path, json.dumps(record, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    return path


def frame(record: dict, shot_id: str) -> dict:
    """Return one frame's block, creating it if absent."""
    frames = record.setdefault("frames", {})
    return frames.setdefault(str(shot_id), {})


def frame_state(entry: dict | None) -> str:
    """Derive a frame's state. Manual work outranks review, which outranks generation."""
    entry = entry or {}
    manual = entry.get("manual") or {}
    has_figure = isinstance(manual.get(ROLE_FIGURE), dict)
    has_background = isinstance(manual.get(ROLE_BACKGROUND), dict)
    if has_figure and has_background:
        return STATE_MANUAL
    if has_figure or has_background:
        return STATE_MANUAL_INCOMPLETE

    review = entry.get("review") or {}
    answer = review.get("answer")
    if answer == ANSWER_ACCEPTED:
        return STATE_ACCEPTED
    if answer == ANSWER_REJECTED:
        return STATE_REJECTED

    generation = entry.get("proposal_generation") or {}
    status = generation.get("status")
    if status == "running":
        return STATE_GENERATING
    if status == "failed":
        return STATE_GENERATION_FAILED
    if status == "ok" and (entry.get("proposals") or {}):
        return STATE_UNREVIEWED
    return STATE_NOT_GENERATED


def has_proposals(entry: dict | None) -> bool:
    entry = entry or {}
    generation = entry.get("proposal_generation") or {}
    return generation.get("status") == "ok" and bool(entry.get("proposals"))


def selectable_choices(entry: dict | None) -> list[str]:
    """Choice keys a reviewer may actually accept.

    A choice must be active *and* carry both measured colours. The refusal
    quadrant is active but has no palette, so it is never acceptable — without
    this the caller would reach ``accept_proposal`` and raise.
    """
    entry = entry or {}
    proposals = entry.get("proposals") or {}
    keys = []
    for key in sorted(proposals, key=lambda value: int(value)):
        proposal = proposals[key] or {}
        colours = [item for item in (proposal.get("colours") or []) if item]
        if proposal.get("active") and len(colours) == 2:
            keys.append(key)
    return keys


def begin_generation(entry: dict, version: str, generation_id: str) -> dict:
    entry["proposal_generation"] = {
        "status": "running",
        "version": version,
        "generation_id": generation_id,
        "started": _now(),
    }
    return entry


def record_generation(entry: dict, version: str, generation_id: str,
                      proposals: dict, provenance: dict) -> dict:
    """Store a successful generation without touching review or manual work."""
    entry["proposal_generation"] = {
        "status": "ok",
        "version": version,
        "generation_id": generation_id,
        "created": _now(),
        **provenance,
    }
    entry["proposals"] = proposals
    return entry


def record_generation_failure(entry: dict, version: str, generation_id: str,
                              error: str) -> dict:
    """Record a failure. Any previous proposals and decisions are left alone."""
    entry["proposal_generation"] = {
        "status": "failed",
        "version": version,
        "generation_id": generation_id,
        "created": _now(),
        "error": str(error),
    }
    return entry


def _colour(value) -> dict | None:
    if not isinstance(value, dict):
        return None
    return {
        key: value[key]
        for key in ("rgb", "hex", "lab", "source", "material", "pipette")
        if key in value
    }


def accept_proposal(entry: dict, choice: str, reviewer: str | None = None) -> dict:
    """Accept a generated proposal. Never recomputes it — the stored colours are used."""
    choice = str(choice)
    proposals = entry.get("proposals") or {}
    proposal = proposals.get(choice)
    if not isinstance(proposal, dict) or not proposal.get("active"):
        raise ValueError(f"choice {choice!r} is not a selectable proposal")
    colours = [item for item in (proposal.get("colours") or []) if item]
    if len(colours) != 2:
        raise ValueError(f"choice {choice!r} has no complete two-colour palette")

    generation = entry.get("proposal_generation") or {}
    entry["review"] = {
        "answer": ANSWER_ACCEPTED,
        "choice": choice,
        "strategy": proposal.get("strategy"),
        "accepted_generation_id": generation.get("generation_id"),
        "accepted_generation_version": generation.get("version"),
        "reviewer": reviewer,
        "timestamp": _now(),
    }
    entry["final_palette"] = {
        "source": "proposal",
        "choice": choice,
        "strategy": proposal.get("strategy"),
        ROLE_FIGURE: _colour(colours[0]),
        ROLE_BACKGROUND: _colour(colours[1]),
    }
    return entry


def reject_proposals(entry: dict, reviewer: str | None = None,
                     note: str | None = None) -> dict:
    """Record that a human looked at the proposals and wanted none of them.

    This is not the pipeline's own "no adequate two-colour palette" claim, and
    it is not the same as unreviewed. Proposals are preserved untouched.
    """
    generation = entry.get("proposal_generation") or {}
    entry["review"] = {
        "answer": ANSWER_REJECTED,
        "choice": None,
        "rejected_generation_id": generation.get("generation_id"),
        "rejected_generation_version": generation.get("version"),
        "reviewer": reviewer,
        "note": note,
        "timestamp": _now(),
    }
    entry.pop("final_palette", None)
    return entry


def set_manual_colour(entry: dict, role: str, colour: dict,
                      masks: list | None = None) -> dict:
    """Assign one hand-authored role colour, keeping the other role as it was.

    The supplied colour is stored verbatim. A pipetted value is never replaced
    by the mean of its mask.
    """
    if role not in ROLES:
        raise ValueError(f"role must be one of {ROLES}, got {role!r}")
    manual = entry.setdefault("manual", {})
    record = dict(colour)
    record.setdefault("source", "manual")
    if masks is not None:
        record["masks"] = masks
    record["timestamp"] = _now()
    manual[role] = record
    _refresh_manual_final(entry)
    return entry


def clear_manual_role(entry: dict, role: str) -> dict:
    if role not in ROLES:
        raise ValueError(f"role must be one of {ROLES}, got {role!r}")
    manual = entry.get("manual") or {}
    manual.pop(role, None)
    if not manual:
        entry.pop("manual", None)
    _refresh_manual_final(entry)
    return entry


def _refresh_manual_final(entry: dict) -> None:
    manual = entry.get("manual") or {}
    figure, background = manual.get(ROLE_FIGURE), manual.get(ROLE_BACKGROUND)
    if isinstance(figure, dict) and isinstance(background, dict):
        entry["final_palette"] = {
            "source": "manual",
            ROLE_FIGURE: _colour(figure),
            ROLE_BACKGROUND: _colour(background),
        }
    elif (entry.get("final_palette") or {}).get("source") == "manual":
        entry.pop("final_palette", None)


def reset_frame(entry: dict) -> dict:
    """Drop the human result, keep every generated artifact.

    Clears the review answer, the manual palette and the final palette. The
    generated proposals and the generation provenance survive so the reviewer
    can choose again.
    """
    entry.pop("review", None)
    entry.pop("manual", None)
    entry.pop("final_palette", None)
    return entry


def clear_proposals(entry: dict) -> dict:
    """Drop the generated proposals too, returning the frame to not_generated.

    The deeper half of the staged reset: once the proposals are gone a human
    answer could no longer refer to anything, so that goes with them.
    """
    reset_frame(entry)
    entry.pop("proposal_generation", None)
    entry.pop("proposals", None)
    return entry


def clear_media(record: dict) -> dict:
    """Reset every frame's human result for one media item, keeping proposals."""
    for entry in (record.get("frames") or {}).values():
        reset_frame(entry)
    return record


def media_summary(record: dict) -> dict:
    """Count frames by derived state."""
    counts = {state: 0 for state in STATES}
    for entry in (record.get("frames") or {}).values():
        counts[frame_state(entry)] += 1
    return counts
