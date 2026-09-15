"""Palette review service — generation, review, and manual authoring.

Every persistent palette-review mutation lives here. The CLI and the Palette
Visualizer both call these functions; neither contains its own copy. Qt event
handlers are expected to stay thin wrappers over this module.

Generation reuses the frozen narratological methodology rather than forking it:
the prompt assets and the hypothesis-collection/segmentation-status/measurement
helpers are imported from the frozen research code in ``scripts.palette_lab``.
This module adapts that methodology to production media (a movie's shots and
their best frames); it does not re-implement or tune it.
"""

from __future__ import annotations

import uuid
from pathlib import Path

from data import palette_review as store

PROPOSAL_VERSION = "measured-review-v1"

_STAGES = (
    ("narratology", "prompts/palettes/palette-narratology-system-2026-09-15-v1.txt",
     "prompts/palettes/palette-narratology-user-2026-09-15-v1.txt"),
    ("carrier", "prompts/palettes/palette-carrier-system-2026-09-15-v1.txt",
     "prompts/palettes/palette-carrier-user-2026-09-15-v1.txt"),
    ("articulation", "prompts/palettes/palette-articulation-system-2026-09-15-v1.txt",
     "prompts/palettes/palette-articulation-user-2026-09-15-v1.txt"),
    ("operationalize", "prompts/palettes/palette-operationalize-system-2026-09-15-v1.txt",
     "prompts/palettes/palette-operationalize-user-2026-09-15-v1.txt"),
)
MAX_NEW_TOKENS = 3072


# ---------------------------------------------------------------------------
# Frame enumeration
# ---------------------------------------------------------------------------

def list_frames(project_path: str, filename: str, media_type: str) -> list[dict]:
    """Ordered reviewable frames for one media item.

    A frame is reviewable when its shot has a best-frame PNG on disk, which is
    what the palette pipeline measures.
    """
    from data.media_id import compute_media_id
    from data.metadata import get_metadata
    from data.shotlist import attach_shot_ids, read_shotlist
    from services.frame_match import best_frame_path

    entries = get_metadata(project_path, media_type=media_type)
    entry = next(
        (item for item in entries if item.get("filename") == filename), {}
    )
    media_id = str(entry.get("media_id") or compute_media_id(entry, media_type))
    try:
        shots = read_shotlist(project_path, filename, media_type)
    except FileNotFoundError:
        return []
    attach_shot_ids(shots, media_id)

    frames = []
    for index, shot in enumerate(shots):
        shot_id = str(shot.get("shot_id") or "")
        if not shot_id:
            continue
        path = best_frame_path(project_path, media_type, filename, shot_id)
        frames.append({
            "index": index,
            "shot_id": shot_id,
            "scene": str(shot.get("Scene", "") or ""),
            "start_time": shot.get("start_time", ""),
            "image": str(path),
            "available": path.exists(),
        })
    return frames


def media_state(project_path: str, filename: str, media_type: str) -> dict:
    """Per-frame derived state plus a count by state, for one media item."""
    record = store.load_review(project_path, filename, media_type)
    frames = list_frames(project_path, filename, media_type)
    per_frame = []
    for item in frames:
        entry = (record.get("frames") or {}).get(item["shot_id"]) or {}
        per_frame.append({
            **item,
            "state": store.frame_state(entry),
            "choices": store.selectable_choices(entry),
        })
    counts = {state: 0 for state in store.STATES}
    for item in per_frame:
        counts[item["state"]] += 1
    return {
        "filename": filename,
        "media_type": media_type,
        "frames": per_frame,
        "counts": counts,
    }


# ---------------------------------------------------------------------------
# Generation
# ---------------------------------------------------------------------------

def load_models(project_path: str):
    """Load the text and segmentation models once for a batch."""
    from data.annotate import _load_text_generation_pipeline
    from services.silhouette import load_sam_model
    from tool import prefs as _prefs

    text_model = _prefs.get("model_annotation") or "Qwen3-VL-8B-Instruct"
    pipeline = _load_text_generation_pipeline(project_path, text_model)
    sam_name = _prefs.get("model_segmentation") or "sam3"
    segmenter, effective, device = load_sam_model(project_path, sam_name)
    return {
        "pipeline": pipeline,
        "segmenter": segmenter,
        "text_model": text_model,
        "segmenter_name": effective,
        "device": device,
    }


def _prompt_pairs(project_path: str) -> dict:
    from scripts.palette_lab import prompts as prompt_io

    pairs = {}
    for role, system_file, user_file in _STAGES:
        pairs[role] = prompt_io.load_pair(
            project_path, role, system_file=system_file, user_file=user_file
        )
    return pairs


def _call(pipeline, pair, variables, images, tokens=MAX_NEW_TOKENS):
    from scripts.palette_lab import prompts as prompt_io
    from scripts.palette_lab import stages

    system = prompt_io.strip_comments(pair["system"])
    user = prompt_io.substitute(prompt_io.strip_comments(pair["user"]), variables)
    return stages.call_qwen(pipeline, system, user, images, tokens)


def _control_palette(project_path: str, filename: str, media_type: str,
                     shot_id: str) -> dict | None:
    """The frozen production palette for this shot — the deterministic control.

    Bottom-up, computed without any narratological evidence, and read rather
    than recomputed.
    """
    from data.palette import load_palette

    cached = load_palette(project_path, filename, media_type)
    if not cached:
        return None
    for shot in cached.get("shots") or []:
        if str(shot.get("shot_id") or "") != str(shot_id):
            continue
        return {
            "foreground_rgb": (shot.get("foreground") or {}).get("rgb"),
            "background_rgb": (shot.get("background") or {}).get("rgb"),
        }
    return None


def generate_frame_proposals(project_path: str, filename: str, media_type: str,
                             shot_id: str, *, models: dict,
                             image_path: str | None = None) -> dict:
    """Run the frozen narratological chain over one frame and measure it.

    Returns the choices dict in the measured-review-v1 shape. Raises on
    failure so the caller decides whether that ends a batch.
    """
    from scripts.palette_lab import e8_articulation as ART
    from scripts.palette_lab import e8_carrier as CARR
    from scripts.palette_lab import e8_palette_review as REV
    from scripts.palette_lab import stages
    from services.frame_match import best_frame_path

    source = Path(image_path or best_frame_path(
        project_path, media_type, filename, shot_id))
    if not source.exists():
        raise FileNotFoundError(f"No best frame for {shot_id}: {source}")

    pairs = _prompt_pairs(project_path)
    images = stages.prepare_images(source)
    frame_images = [images["active"]]
    pipeline = models["pipeline"]

    narratology = _call(pipeline, pairs["narratology"], {}, frame_images)["parsed"]
    if not isinstance(narratology, dict):
        raise ValueError("narratology stage did not return JSON")

    carrier = _call(pipeline, pairs["carrier"],
                    {"reading": CARR.render_reading(narratology)},
                    frame_images)["parsed"]
    if not isinstance(carrier, dict):
        raise ValueError("carrier stage did not return JSON")

    articulation = _call(pipeline, pairs["articulation"], {
        "narratology": ART.render_narratology(narratology),
        "carriers": ART.render_carriers(carrier),
    }, frame_images)["parsed"]
    if not isinstance(articulation, dict):
        raise ValueError("articulation stage did not return JSON")

    choices = REV.mark_convergence(REV.collect_hypotheses(
        narratology, carrier, articulation,
        _control_palette(project_path, filename, media_type, shot_id),
    ))

    wanted = {}
    for key in sorted(choices, key=int):
        choice = choices[key]
        if choice["strategy"] == "control" or not choice.get("active"):
            continue
        for material in choice.get("materials") or []:
            wanted.setdefault(REV.normalise_material(material), material)

    measured = {}
    for key, material in wanted.items():
        parsed = _call(pipeline, pairs["operationalize"], {"material": material},
                       frame_images, REV.OPERATIONALIZE_TOKENS)["parsed"]
        patch = {
            "material": material,
            "parsed": parsed,
            "problems": REV.operationalization_problems(parsed, material),
            "status": str((parsed or {}).get("status") or "").strip().lower(),
            "targets": REV.localization_targets(parsed),
        }
        found = REV.locate_and_measure(models["segmenter"], images, patch)
        found["operationalization"] = parsed
        measured[key] = found

    for key in sorted(choices, key=int):
        choice = choices[key]
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
        if choice.get("active") and any(colour is None for colour in colours):
            choice["active"] = False
            choice["inactive_reason"] = "measurement incomplete"

    return {
        "choices": choices,
        "provenance": {
            "narratology": narratology,
            "carrier": carrier,
            "articulation": articulation,
            "source_image": str(source),
            "models": {
                "text": models.get("text_model"),
                "segmenter": models.get("segmenter_name"),
                "device": models.get("device"),
            },
            "measurement": {
                "representative_colour": "mean sRGB of the masked pixels",
                "implementation": "scripts.palette_lab.two_color._measure_mask",
                "weighting": "none",
            },
        },
    }


def create_frame(project_path: str, filename: str, media_type: str, shot_id: str,
                 *, models: dict, force: bool = False) -> dict:
    """Generate proposals for one frame. Never implies human acceptance.

    Returns ``{"status": "generated" | "skipped" | "failed", ...}``.
    """
    record = store.load_review(project_path, filename, media_type)
    entry = store.frame(record, shot_id)
    if not force and store.has_proposals(entry):
        generation = entry.get("proposal_generation") or {}
        if generation.get("version") == PROPOSAL_VERSION:
            return {"status": "skipped", "shot_id": shot_id,
                    "reason": "compatible proposals already present"}

    generation_id = uuid.uuid4().hex[:12]
    try:
        result = generate_frame_proposals(
            project_path, filename, media_type, shot_id, models=models
        )
    except Exception as exc:
        store.record_generation_failure(
            entry, PROPOSAL_VERSION, generation_id, f"{type(exc).__name__}: {exc}"
        )
        store.save_review(project_path, filename, media_type, record)
        return {"status": "failed", "shot_id": shot_id, "error": str(exc)}

    store.record_generation(
        entry, PROPOSAL_VERSION, generation_id,
        _public_choices(result["choices"]), result["provenance"],
    )
    store.save_review(project_path, filename, media_type, record)
    return {"status": "generated", "shot_id": shot_id,
            "generation_id": generation_id}


def _public_choices(choices: dict) -> dict:
    """Strip in-memory masks before a choice set is persisted."""
    return {
        key: {name: value for name, value in choice.items() if name != "mask"}
        for key, choice in choices.items()
    }


def create_all(project_path: str, filename: str, media_type: str, *,
               models: dict | None = None, force: bool = False,
               on_progress=None, should_cancel=None) -> dict:
    """Generate proposals for every frame of ONE media item.

    Scoped to the given movie/gameplay — never the whole database. One frame's
    failure never discards the frames that already succeeded, because each
    frame is persisted as it completes.
    """
    frames = [item for item in list_frames(project_path, filename, media_type)
              if item["available"]]
    summary = {
        "filename": filename,
        "media_type": media_type,
        "considered": len(frames),
        "generated": 0,
        "skipped": 0,
        "failed": 0,
        "cancelled": False,
        "errors": [],
    }
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
            result = create_frame(
                project_path, filename, media_type, item["shot_id"],
                models=models, force=force,
            )
            if result["status"] == "generated":
                summary["generated"] += 1
            elif result["status"] == "skipped":
                summary["skipped"] += 1
            else:
                summary["failed"] += 1
                summary["errors"].append((item["shot_id"], result.get("error", "")))
            if on_progress is not None:
                on_progress(position, len(frames), item["shot_id"], result["status"])
    finally:
        if owned:
            _free_models(models)
    summary["complete"] = not summary["cancelled"]
    return summary


def _free_models(models: dict) -> None:
    models.pop("segmenter", None)
    models.pop("pipeline", None)
    try:
        import torch

        if torch.cuda.is_available():
            torch.cuda.empty_cache()
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Review mutations
# ---------------------------------------------------------------------------

def _mutate(project_path: str, filename: str, media_type: str, shot_id: str,
            apply) -> dict:
    record = store.load_review(project_path, filename, media_type)
    entry = store.frame(record, shot_id)
    apply(entry)
    store.save_review(project_path, filename, media_type, record)
    return entry


def accept(project_path: str, filename: str, media_type: str, shot_id: str,
           choice: str, *, reviewer: str | None = None) -> dict:
    """Accept proposal *choice* for one frame, using its already-measured colours."""
    return _mutate(project_path, filename, media_type, shot_id,
                   lambda entry: store.accept_proposal(entry, choice, reviewer))


def reject(project_path: str, filename: str, media_type: str, shot_id: str, *,
           reviewer: str | None = None, note: str | None = None) -> dict:
    """Record that a human judged none of the generated proposals satisfactory."""
    return _mutate(project_path, filename, media_type, shot_id,
                   lambda entry: store.reject_proposals(entry, reviewer, note))


def reset(project_path: str, filename: str, media_type: str, shot_id: str) -> dict:
    """Clear the human result for one frame, preserving every generated artifact."""
    return _mutate(project_path, filename, media_type, shot_id, store.reset_frame)


def clear_proposals(project_path: str, filename: str, media_type: str,
                    shot_id: str) -> dict:
    """Clear one frame's generated proposals as well as its human result."""
    return _mutate(project_path, filename, media_type, shot_id,
                   store.clear_proposals)


def set_manual(project_path: str, filename: str, media_type: str, shot_id: str,
               role: str, colour: dict, *, masks: list | None = None) -> dict:
    """Store a hand-authored role colour exactly as supplied."""
    return _mutate(
        project_path, filename, media_type, shot_id,
        lambda entry: store.set_manual_colour(entry, role, colour, masks),
    )


def clear_manual(project_path: str, filename: str, media_type: str, shot_id: str,
                 role: str) -> dict:
    return _mutate(project_path, filename, media_type, shot_id,
                   lambda entry: store.clear_manual_role(entry, role))


def clear_all(project_path: str, filename: str, media_type: str) -> dict:
    """Clear human review/manual results for ONE media item, keeping proposals."""
    record = store.load_review(project_path, filename, media_type)
    before = store.media_summary(record)
    store.clear_media(record)
    store.save_review(project_path, filename, media_type, record)
    return {
        "filename": filename,
        "media_type": media_type,
        "cleared": (
            before[store.STATE_ACCEPTED] + before[store.STATE_REJECTED]
            + before[store.STATE_MANUAL] + before[store.STATE_MANUAL_INCOMPLETE]
        ),
        "frames": len(record.get("frames") or {}),
    }
