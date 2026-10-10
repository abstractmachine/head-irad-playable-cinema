"""Shot-evidence assembly for engraving benchmark inspection.

Answers one research question: *does another frame in the same cinematic shot
reveal structure the selected source frame does not?*

The frame count and temporal positions are **not** decided here.  They come
from ``data.annotate.canonical_context_frame_indices`` -- the annotator's own
shot-length-aware sampler, which is explicitly documented for read-only
evidence consumers.  This module only adds the three temporal anchors
(``first`` / ``best`` / ``last``), merges them with the canonical adaptive
frames, and records why each frame was kept.

Two boundaries are deliberate:

- **The shot is the evidence unit.** Every frame is clamped to the inclusive
  ``shot_id`` bounds; nothing bleeds into an adjacent shot.
- **A shot frame is not object evidence.** These frames represent the *shot*.
  Whether the target object is visible in any of them is a human judgement at
  this stage; no cross-frame identity is claimed or fabricated.
"""

from __future__ import annotations

from pathlib import Path

ROLE_FIRST = "first"
ROLE_BEST = "best"
ROLE_LAST = "last"
ROLE_ADAPTIVE = "adaptive"
ROLE_SOURCE = "source"


class ShotEvidenceError(RuntimeError):
    """Raised when a shot's evidence frames cannot be assembled."""


def merge_roles(
    *,
    first: int | None,
    best: int | None,
    last: int | None,
    adaptive: list[int],
    source: int | None = None,
    shot_start: int,
    shot_end: int,
) -> list[dict]:
    """Merge anchors and adaptive frames into unique, role-tagged frames.

    Deduplicates on the frame number itself -- the actual source frame
    identity -- so a frame that is both ``best`` and ``adaptive`` appears once
    carrying both roles, never twice.
    """
    ordered: dict[int, list[str]] = {}

    def _add(frame: int | None, role: str) -> None:
        if frame is None:
            return
        value = int(frame)
        if value < shot_start or value > shot_end:
            return
        roles = ordered.setdefault(value, [])
        if role not in roles:
            roles.append(role)

    _add(first, ROLE_FIRST)
    _add(best, ROLE_BEST)
    _add(last, ROLE_LAST)
    for frame in adaptive:
        _add(frame, ROLE_ADAPTIVE)
    _add(source, ROLE_SOURCE)

    return [
        {"frame": frame, "roles": ordered[frame]}
        for frame in sorted(ordered)
    ]


def _shot_row(project_path: str | Path, filename: str, media_type: str, shot_id: str) -> dict:
    from data.shotlist import read_shotlist

    try:
        shots = read_shotlist(str(project_path), filename, media_type)
    except (FileNotFoundError, OSError) as exc:
        raise ShotEvidenceError(f"Shotlist unavailable for {filename!r}: {exc}") from exc

    matches = [shot for shot in shots if str(shot.get("shot_id") or "") == shot_id]
    if not matches:
        raise ShotEvidenceError(f"Shot {shot_id!r} not present in {filename!r}")
    return matches[0]


def _best_frame_number(project_path: str | Path, record: dict, shot_id: str) -> int | None:
    """Return the catalog's existing best frame for this shot, if known.

    The silhouette record's ``frame`` is the frame the catalog extracted the
    object from, which *is* the shot's best frame in the current pipeline; the
    best-frame PNG is named after the shot.  Nothing is recomputed here.
    """
    frame = record.get("frame")
    return int(frame) if isinstance(frame, (int, float)) else None


def best_frame_image(project_path: str | Path, record: dict) -> Path | None:
    """Return the cached best-frame PNG for this record's shot, if present."""
    from services.frame_match import best_frame_path

    filename = str(record.get("filename") or "")
    media_type = str(record.get("media_type") or "movie")
    shot_id = str(record.get("shot_id") or "")
    if not filename or not shot_id:
        return None
    path = best_frame_path(str(project_path), media_type, filename, shot_id)
    return path if path.is_file() else None


def shot_evidence_for_record(
    project_path: str | Path,
    record: dict,
    *,
    frames_per_shot: int | None = None,
    include_history: bool = False,
) -> dict:
    """Return the deduplicated shot-evidence plan for one catalog object.

    *frames_per_shot* raises the sampler's baseline N for inspection purposes
    only; the duration bucketing, spacing, clamping and deduplication all
    remain the canonical sampler's. No frame is extracted here.
    """
    from data.annotate import canonical_context_frame_indices
    from data.media_id import parse_shot_id
    from services.silhouette_reference import _selection_preferences

    from services.silhouette_catalog import require_active_record
    if not include_history:
        source = record.get("record_path") or record.get("path")
        if source:
            from services.silhouette_catalog import load_active_record
            path = Path(source)
            if not path.is_absolute():
                path = Path(project_path) / path
            load_active_record(path, project_path)
        require_active_record(record, source=record.get("path"), project_path=project_path)

    shot_id = str(record.get("shot_id") or "")
    filename = str(record.get("filename") or "")
    media_type = str(record.get("media_type") or "movie")
    if not shot_id:
        raise ShotEvidenceError("Record has no shot_id")

    try:
        _, shot_start, shot_end = parse_shot_id(shot_id)
    except ValueError as exc:
        raise ShotEvidenceError(str(exc)) from exc

    shot = _shot_row(project_path, filename, media_type, shot_id)
    fps = _shot_fps(project_path, filename, shot, shot_start, shot_end)

    frames_per_shot_pref, min_interval, max_frames = _selection_preferences()
    baseline = int(frames_per_shot or frames_per_shot_pref)
    adaptive = canonical_context_frame_indices(
        shot_start,
        shot_end,
        str(shot.get("start_time") or ""),
        str(shot.get("end_time") or ""),
        fps,
        frames_per_shot=baseline,
        min_frame_interval_s=min_interval,
        max_frames_per_shot=max_frames,
    )

    best = _best_frame_number(project_path, record, shot_id)
    selected = merge_roles(
        first=shot_start,
        best=best,
        last=shot_end,
        adaptive=list(adaptive),
        shot_start=shot_start,
        shot_end=shot_end,
    )

    return {
        "shot_id": shot_id,
        "shot_start": shot_start,
        "shot_end": shot_end,
        "shot_length_frames": shot_end - shot_start + 1,
        "fps": fps,
        "sampler": {
            "source": "data.annotate.canonical_context_frame_indices",
            "frames_per_shot": baseline,
            "frames_per_shot_pref": frames_per_shot_pref,
            "min_frame_interval_s": min_interval,
            "max_frames_per_shot": max_frames,
        },
        "first": shot_start,
        "best": best,
        "last": shot_end,
        "adaptive": list(adaptive),
        "selected_unique": selected,
        "note": (
            "Frames represent the shot, not the object. Object visibility in "
            "each frame is a human judgement; no cross-frame identity is claimed."
        ),
    }


def _shot_fps(
    project_path: str | Path,
    filename: str,
    shot: dict,
    shot_start: int,
    shot_end: int,
) -> float:
    """Return fps, preferring the shotlist's own timing over probing the video."""
    from data.annotate import _timecode_to_seconds

    try:
        duration = _timecode_to_seconds(str(shot.get("end_time") or "")) - _timecode_to_seconds(
            str(shot.get("start_time") or "")
        )
        frames = (shot_end - shot_start) or 1
        if duration > 0:
            return float(frames) / float(duration)
    except (TypeError, ValueError):
        pass

    from generators.mosaic import _find_video_path
    from services.frame_match import _get_video_fps

    video = _find_video_path(str(project_path), Path(filename).stem)
    return float(_get_video_fps(str(video))) if video else 25.0
