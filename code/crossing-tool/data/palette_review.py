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
    review               the human's answer (accepted / rejected / split), with
                         the generation id they were actually looking at
    manual               hand-authored figure/background plus mask provenance,
                         and the proposal each pick displaced
    final_palette        the resolved two colours, and which block produced them

A *split* review is a partial validation: the reviewer took the figure from one
proposal and the background from another. It stores the choice per role, never
a copy of the colour, so the proposals stay the single source of the measured
values and a later pass can ask which strategy got which half right.

Overriding a proposal by hand is the other half of that evidence, so a manual
pick keeps a ``supersedes`` link to the choice it replaced. Without it, a
reviewer who hand-picked *both* roles would erase every trace of the proposal
they rejected — the most informative case for retraining.

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
STATE_SPLIT = "split"
STATE_SPLIT_INCOMPLETE = "split_incomplete"
STATES = (
    STATE_NOT_GENERATED, STATE_GENERATING, STATE_GENERATION_FAILED,
    STATE_UNREVIEWED, STATE_ACCEPTED, STATE_REJECTED,
    STATE_MANUAL, STATE_MANUAL_INCOMPLETE,
    STATE_SPLIT, STATE_SPLIT_INCOMPLETE,
)

ANSWER_ACCEPTED = "accepted"
ANSWER_REJECTED = "rejected"
ANSWER_SPLIT = "split"

# Where one resolved role's colour came from.
SOURCE_MANUAL = "manual"
SOURCE_PROPOSAL = "proposal"


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
    """Derive a frame's state. Composed roles outrank review, which outranks generation."""
    entry = entry or {}
    roles = resolved_roles(entry)
    if roles:
        split = any(role.get("source") == SOURCE_PROPOSAL for role in roles.values())
        if len(roles) == len(ROLES):
            return STATE_SPLIT if split else STATE_MANUAL
        return STATE_SPLIT_INCOMPLETE if split else STATE_MANUAL_INCOMPLETE

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


def proposal_colour(entry: dict | None, choice, role: str) -> dict | None:
    """The colour one proposal measured for one role, or None if it has none."""
    entry = entry or {}
    proposal = (entry.get("proposals") or {}).get(str(choice)) or {}
    colours = [item for item in (proposal.get("colours") or []) if item]
    if not proposal.get("active") or len(colours) != 2:
        return None
    return _colour(colours[0] if role == ROLE_FIGURE else colours[1])


def resolved_roles(entry: dict | None) -> dict:
    """Each composed role's colour and where it came from.

    A hand-picked colour outranks an endorsed proposal *for that role* — the
    record's "manual outranks review" rule applied per role rather than to the
    whole frame, which is what lets one half be pipetted and the other taken
    from a proposal. A whole-frame accept is not a composition and resolves to
    nothing here; ``final_palette`` already carries it.
    """
    entry = entry or {}
    manual = entry.get("manual") or {}
    review = entry.get("review") or {}
    endorsed = (review.get("roles") or {}) if review.get("answer") == ANSWER_SPLIT else {}
    resolved = {}
    for role in ROLES:
        if isinstance(manual.get(role), dict):
            colour = _colour(manual[role])
            colour.setdefault("source", SOURCE_MANUAL)
            resolved[role] = colour
            continue
        endorsement = endorsed.get(role)
        if not isinstance(endorsement, dict):
            continue
        colour = proposal_colour(entry, endorsement.get("choice"), role)
        if colour:
            colour.update({
                "source": SOURCE_PROPOSAL,
                "choice": str(endorsement.get("choice")),
                "strategy": endorsement.get("strategy"),
            })
            resolved[role] = colour
    return resolved


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
    # Choosing a proposal supersedes hand-picked colours, so the frame never
    # reads as manual while displaying a proposal.
    entry.pop("manual", None)
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


def _endorsement(entry: dict, choice, timestamp: str | None = None) -> dict:
    """One role's record of the proposal it was taken from — a decision, not a colour."""
    proposal = (entry.get("proposals") or {}).get(str(choice)) or {}
    return {
        "choice": str(choice),
        "strategy": proposal.get("strategy"),
        "label": proposal.get("label"),
        "timestamp": timestamp or _now(),
    }


def _endorsements(entry: dict) -> dict:
    """The per-role endorsements in force, decomposing a whole-frame accept.

    Taking one half of a different proposal leaves the other half where the
    reviewer already put it, so accepting 2 and then taking the figure from 1
    means figure from 1, background still from 2.
    """
    review = entry.get("review") or {}
    answer = review.get("answer")
    if answer == ANSWER_SPLIT:
        return {role: dict(value)
                for role, value in (review.get("roles") or {}).items()
                if role in ROLES and isinstance(value, dict)}
    if answer == ANSWER_ACCEPTED and review.get("choice"):
        return {role: _endorsement(entry, review["choice"], review.get("timestamp"))
                for role in ROLES}
    return {}


def _supersedes(endorsement: dict | None) -> dict | None:
    """What a hand-picked colour displaced. The choice, never a copy of its colour."""
    if not isinstance(endorsement, dict):
        return None
    return {key: endorsement[key] for key in ("choice", "strategy", "label")
            if endorsement.get(key) is not None}


def _write_split(entry: dict, roles: dict, reviewer: str | None) -> None:
    generation = entry.get("proposal_generation") or {}
    entry["review"] = {
        "answer": ANSWER_SPLIT,
        "choice": None,
        "roles": roles,
        "accepted_generation_id": generation.get("generation_id"),
        "accepted_generation_version": generation.get("version"),
        "reviewer": reviewer,
        "timestamp": _now(),
    }


def set_role_from_proposal(entry: dict, role: str, choice: str,
                           reviewer: str | None = None) -> dict:
    """Take one role from one proposal — figure from 1, background from 4.

    A split validation. The reviewer is confirming part of what the pipeline
    proposed, so it is recorded as a decision *about the proposals* rather than
    as a hand-authored colour: a later pass can then tell a half-right proposal
    from one nobody wanted, and attribute each half to its strategy.
    """
    if role not in ROLES:
        raise ValueError(f"role must be one of {ROLES}, got {role!r}")
    choice = str(choice)
    if choice not in selectable_choices(entry):
        raise ValueError(f"choice {choice!r} is not a selectable proposal")

    roles = _endorsements(entry)
    roles[role] = _endorsement(entry, choice)
    if (len(roles) == len(ROLES)
            and {value.get("choice") for value in roles.values()} == {choice}):
        # Both halves landed on the same proposal: that is a plain acceptance,
        # and recording it as a split would overstate the disagreement.
        return accept_proposal(entry, choice, reviewer)

    manual = entry.get("manual") or {}
    manual.pop(role, None)
    if not manual:
        entry.pop("manual", None)

    _write_split(entry, roles, reviewer)
    _refresh_composed_final(entry)
    return entry


def set_manual_colour(entry: dict, role: str, colour: dict,
                      masks: list | None = None) -> dict:
    """Assign one hand-authored role colour, keeping the other role as it was.

    The supplied colour is stored verbatim. A pipetted value is never replaced
    by the mean of its mask.
    """
    if role not in ROLES:
        raise ValueError(f"role must be one of {ROLES}, got {role!r}")
    # Picking by hand replaces this role and this role only. A whole-frame
    # acceptance therefore decomposes so the other half survives, exactly as
    # taking one half of another proposal does. Superseded endorsements are
    # dropped rather than merely outranked, so the record never claims a
    # proposal was validated when it was not.
    reviewer = (entry.get("review") or {}).get("reviewer")
    roles = _endorsements(entry)
    displaced = roles.pop(role, None)

    manual = entry.setdefault("manual", {})
    record = dict(colour)
    record.setdefault("source", SOURCE_MANUAL)
    if masks is not None:
        record["masks"] = masks
    superseded = _supersedes(displaced) or (manual.get(role) or {}).get("supersedes")
    if superseded:
        # Re-picking an already-overridden role still displaces the original
        # proposal, so the link is carried forward rather than lost.
        record["supersedes"] = superseded
    record["timestamp"] = _now()
    manual[role] = record

    if roles:
        _write_split(entry, roles, reviewer)
    else:
        entry.pop("review", None)
    _refresh_composed_final(entry)
    return entry


def clear_manual_role(entry: dict, role: str) -> dict:
    if role not in ROLES:
        raise ValueError(f"role must be one of {ROLES}, got {role!r}")
    manual = entry.get("manual") or {}
    manual.pop(role, None)
    if not manual:
        entry.pop("manual", None)
    _refresh_composed_final(entry)
    return entry


def _refresh_composed_final(entry: dict) -> None:
    roles = resolved_roles(entry)
    if len(roles) == len(ROLES):
        sources = {value.get("source") for value in roles.values()}
        entry["final_palette"] = {
            "source": "split" if SOURCE_PROPOSAL in sources else SOURCE_MANUAL,
            ROLE_FIGURE: roles[ROLE_FIGURE],
            ROLE_BACKGROUND: roles[ROLE_BACKGROUND],
        }
    elif roles or (entry.get("final_palette") or {}).get("source") in ("manual", "split"):
        # A composition owns the palette while any half exists, even one half.
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
