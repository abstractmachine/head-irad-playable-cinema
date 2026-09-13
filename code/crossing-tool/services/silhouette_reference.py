"""Read-only exact visual evidence packets for existing silhouette objects."""

from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path
from typing import Any


_MAX_PACKET_BYTES = 900_000


class SilhouetteReferenceError(Exception):
    """Structured failure raised while assembling a reference packet."""

    def __init__(
        self,
        code: str,
        stage: str,
        message: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.stage = stage
        self.message = message
        self.details = details or {}

    def payload(self) -> dict[str, Any]:
        return {
            "ok": False,
            "error": {
                "code": self.code,
                "stage": self.stage,
                "message": self.message,
                "details": self.details,
            },
        }


def _project_relative(project_path: str | Path, path: Path) -> str:
    return str(path.resolve().relative_to(Path(project_path).resolve()))


def _catalog_root(project_path: str | Path, media_type: str) -> Path:
    from services.silhouette_catalog import catalog_base_dir

    root = catalog_base_dir(str(project_path), media_type).resolve()
    if not root.is_dir():
        raise SilhouetteReferenceError(
            "CANDIDATE_NOT_FOUND",
            "CANDIDATE_RESOLUTION",
            f"Silhouette catalog does not exist for media type {media_type!r}.",
            {"media_type": media_type},
        )
    return root


def _resolve_film(
    project_path: str | Path,
    media_type: str,
    media_id: str,
    *,
    expected_filename: str = "",
) -> dict[str, Any]:
    from data.media_id import compute_media_id
    from data.metadata import get_metadata

    matches = [
        entry
        for entry in get_metadata(str(project_path), media_type=media_type)
        if compute_media_id(entry, media_type) == media_id
    ]
    if expected_filename:
        matches = [
            entry for entry in matches
            if str(entry.get("filename") or "") == expected_filename
        ]
    if len(matches) == 1:
        return matches[0]
    if not matches:
        raise SilhouetteReferenceError(
            "FILM_NOT_FOUND",
            "FILM_RESOLUTION",
            f"No {media_type!r} metadata entry matches media_id {media_id!r}.",
            {"media_type": media_type, "media_id": media_id},
        )
    raise SilhouetteReferenceError(
        "FILM_AMBIGUOUS",
        "FILM_RESOLUTION",
        f"Multiple {media_type!r} metadata entries match media_id {media_id!r}.",
        {
            "media_type": media_type,
            "media_id": media_id,
            "filenames": [str(entry.get("filename") or "") for entry in matches],
        },
    )


def _record_png_path(
    project_path: str | Path,
    media_type: str,
    record: dict[str, Any],
) -> Path:
    record_path = Path(record["path"]).resolve()
    png_name = str(record.get("png") or "")
    if not png_name:
        raise SilhouetteReferenceError(
            "SILHOUETTE_MISSING",
            "SILHOUETTE_DECODING",
            "Catalog object has no silhouette PNG reference.",
            {"candidate_id": _project_relative(project_path, record_path)},
        )
    if Path(png_name).name != png_name:
        raise SilhouetteReferenceError(
            "SILHOUETTE_PATH_INVALID",
            "SILHOUETTE_DECODING",
            "Catalog object references a non-sibling silhouette PNG path.",
            {"candidate_id": _project_relative(project_path, record_path)},
        )
    png_path = (record_path.parent / png_name).resolve()
    root = _catalog_root(project_path, media_type)
    if png_path.suffix.lower() != ".png" or not png_path.is_relative_to(root):
        raise SilhouetteReferenceError(
            "SILHOUETTE_PATH_INVALID",
            "SILHOUETTE_DECODING",
            "Catalog object references a PNG outside the silhouette catalog root.",
            {"candidate_id": _project_relative(project_path, record_path)},
        )
    if not png_path.is_file():
        raise SilhouetteReferenceError(
            "SILHOUETTE_MISSING",
            "SILHOUETTE_DECODING",
            "Catalog object references a missing silhouette PNG.",
            {"silhouette_path": _project_relative(project_path, png_path)},
        )
    return png_path


def _candidate_summary(project_path: str | Path, record: dict[str, Any]) -> dict[str, Any]:
    path = Path(record["path"])
    return {
        "candidate_id": _project_relative(project_path, path),
        "object_id": str(record.get("object_id") or path.stem),
        "media_type": record.get("media_type"),
        "media_id": record.get("media_id"),
        "shot_id": record.get("shot_id"),
        "field": record.get("field"),
        "word": record.get("label"),
        "frame_index": record.get("frame"),
        "bbox": record.get("bbox"),
        "score": record.get("confidence"),
        "mask_area": record.get("mask_area"),
        "silhouette_path": _project_relative(
            project_path, Path(record["path"]).parent / str(record.get("png") or ""),
        ),
    }


def _resolve_candidate(
    project_path: str | Path,
    *,
    media_type: str,
    media_id: str,
    shot_id: str,
    frame_index: int,
    word: str,
    field: str,
    candidate_id: str | None,
    png_path: str | None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    if not all((media_type, media_id, shot_id, word, field)):
        raise SilhouetteReferenceError(
            "CANDIDATE_SELECTOR_INVALID",
            "CANDIDATE_RESOLUTION",
            "media_type, media_id, shot_id, frame_index, word, and field are required.",
        )
    from services.silhouette_discovery import (
        ActiveSilhouetteIndexError,
        resolve_exact_active_candidate,
    )

    try:
        record = resolve_exact_active_candidate(
            project_path,
            media_type=media_type,
            media_id=media_id,
            shot_id=shot_id,
            frame_index=frame_index,
            word=word,
            field=field,
            candidate_id=candidate_id,
            png_path=png_path,
        )
    except ActiveSilhouetteIndexError as exc:
        raise SilhouetteReferenceError(
            exc.code,
            "CANDIDATE_RESOLUTION",
            exc.message,
            exc.details,
        ) from exc
    film = _resolve_film(
        project_path,
        media_type,
        media_id,
        expected_filename=str(record.get("filename") or ""),
    )
    return record, film


def _resolve_shot(
    project_path: str | Path,
    film: dict[str, Any],
    media_type: str,
    shot_id: str,
) -> dict[str, Any]:
    from data.shotlist import read_shotlist

    filename = str(film.get("filename") or "")
    try:
        shots = read_shotlist(str(project_path), filename, media_type)
    except (FileNotFoundError, OSError) as exc:
        raise SilhouetteReferenceError(
            "SHOTLIST_NOT_FOUND",
            "SHOT_RESOLUTION",
            f"Shotlist could not be read for {filename!r}.",
            {"filename": filename, "shot_id": shot_id},
        ) from exc
    matches = [shot for shot in shots if str(shot.get("shot_id") or "") == shot_id]
    if len(matches) == 1:
        return matches[0]
    if not matches:
        raise SilhouetteReferenceError(
            "SHOT_NOT_FOUND",
            "SHOT_RESOLUTION",
            f"Shot {shot_id!r} is not present in {filename!r}.",
            {"filename": filename, "shot_id": shot_id},
        )
    raise SilhouetteReferenceError(
        "SHOT_AMBIGUOUS",
        "SHOT_RESOLUTION",
        f"Shotlist contains multiple rows for shot {shot_id!r}.",
        {"filename": filename, "shot_id": shot_id},
    )


def _video_path(project_path: str | Path, filename: str) -> Path:
    from generators.mosaic import _find_video_path

    video_path = _find_video_path(str(project_path), Path(filename).stem)
    root = (Path(project_path) / "media" / "videos").resolve()
    if video_path is None or not video_path.is_file():
        raise SilhouetteReferenceError(
            "SOURCE_VIDEO_MISSING",
            "FILM_RESOLUTION",
            f"Source video is unavailable for {filename!r}.",
            {"filename": filename},
        )
    video_path = video_path.resolve()
    if not video_path.is_relative_to(root):
        raise SilhouetteReferenceError(
            "SOURCE_VIDEO_PATH_INVALID",
            "FILM_RESOLUTION",
            "Resolved source video is outside the authorized project media root.",
            {"filename": filename},
        )
    return video_path


def _selection_preferences() -> tuple[int, float, int]:
    from data.annotate import FRAMES_PER_SHOT
    from tool import prefs

    try:
        frames_per_shot = int(prefs.get("annotate_frames_per_shot", 3) or 3)
        min_interval = float(prefs.get("annotate_min_frame_interval", 4.0) or 4.0)
        max_frames = int(prefs.get("annotate_max_frames_per_shot", 16) or 16)
    except (TypeError, ValueError):
        return FRAMES_PER_SHOT, 4.0, 16
    return max(1, frames_per_shot), max(0.1, min_interval), max(1, max_frames)


def _image_bytes(image) -> bytes:
    from services.frame_retrieval import _pil_to_jpeg_bytes

    return _pil_to_jpeg_bytes(image)


def _image_metadata(image_data: bytes, image, frame_index: int, fps: float) -> dict[str, Any]:
    return {
        "frame_index": frame_index,
        "timestamp_seconds": frame_index / fps if fps > 0 else None,
        "width": image.width,
        "height": image.height,
        "mime_type": "image/jpeg",
        "sha256": hashlib.sha256(image_data).hexdigest(),
    }


def _subtitles_for_shot(
    project_path: str | Path,
    media_type: str,
    filename: str,
    start_time: str,
    end_time: str,
) -> list[dict[str, Any]]:
    from data.annotate import _timecode_to_seconds
    from data.subtitles import load_subtitle_cues

    start_seconds = _timecode_to_seconds(start_time)
    end_seconds = _timecode_to_seconds(end_time)
    return [
        {
            "start_seconds": cue.start_secs,
            "end_seconds": cue.end_secs,
            "text": cue.text,
        }
        for cue in load_subtitle_cues(str(project_path), media_type, filename)
        if cue.end_secs > start_seconds and cue.start_secs < end_seconds
    ]


def _validate_packet_size(content: list[dict[str, Any]]) -> None:
    item_sizes = [
        {"content_id": item["content_id"], "bytes": len(item["data"])}
        for item in content
    ]
    total = sum(item["bytes"] for item in item_sizes)
    if total > _MAX_PACKET_BYTES:
        raise SilhouetteReferenceError(
            "PAYLOAD_TOO_LARGE",
            "PAYLOAD_SIZE_VALIDATION",
            "Native-resolution reference packet exceeds the MCP payload budget.",
            {
                "limit_bytes": _MAX_PACKET_BYTES,
                "total_bytes": total,
                "content": item_sizes,
            },
        )


def retrieve_silhouette_reference_packet(
    project_path: str | Path,
    *,
    media_id: str,
    shot_id: str,
    frame_index: int,
    word: str,
    field: str,
    media_type: str = "movie",
    candidate_id: str | None = None,
    png_path: str | None = None,
) -> dict[str, Any]:
    """Return exact cached silhouette evidence plus canonical same-shot context.

    The returned ``content`` sequence contains raw image bytes in the order
    required by the MCP adapter: silhouette PNG, exact source-frame JPEG, then
    canonical contextual-frame JPEGs. This function performs no model loading,
    inference, candidate ranking, catalog mutation, or image resizing.
    """
    try:
        exact_frame = int(frame_index)
    except (TypeError, ValueError) as exc:
        raise SilhouetteReferenceError(
            "CANDIDATE_SELECTOR_INVALID",
            "CANDIDATE_RESOLUTION",
            "frame_index must be an integer.",
            {"frame_index": frame_index},
        ) from exc

    record, film = _resolve_candidate(
        project_path,
        media_type=media_type,
        media_id=str(media_id),
        shot_id=str(shot_id),
        frame_index=exact_frame,
        word=str(word),
        field=str(field),
        candidate_id=candidate_id or None,
        png_path=png_path or None,
    )
    silhouette_path = _record_png_path(project_path, media_type, record)
    silhouette_bytes = silhouette_path.read_bytes()
    try:
        from PIL import Image

        with Image.open(io.BytesIO(silhouette_bytes)) as silhouette_image:
            silhouette_image.load()
            silhouette_width, silhouette_height = silhouette_image.size
            silhouette_mode = silhouette_image.mode
    except Exception as exc:
        raise SilhouetteReferenceError(
            "SILHOUETTE_DECODING_FAILED",
            "SILHOUETTE_DECODING",
            "Cached silhouette PNG could not be decoded.",
            {"silhouette_path": _project_relative(project_path, silhouette_path)},
        ) from exc

    shot = _resolve_shot(project_path, film, media_type, str(shot_id))
    try:
        shot_start = int(shot.get("start_frame"))
        shot_end = int(shot.get("end_frame"))
    except (TypeError, ValueError) as exc:
        raise SilhouetteReferenceError(
            "SHOT_FRAME_RANGE_INVALID",
            "SHOT_RESOLUTION",
            f"Shot {shot_id!r} has no usable frame range.",
        ) from exc
    if exact_frame < shot_start or exact_frame > shot_end:
        raise SilhouetteReferenceError(
            "EXTRACTION_FRAME_OUTSIDE_SHOT",
            "SHOT_RESOLUTION",
            "Catalog extraction frame lies outside the catalog shot boundaries.",
            {
                "frame_index": exact_frame,
                "shot_start_frame": shot_start,
                "shot_end_frame": shot_end,
            },
        )

    filename = str(film.get("filename") or "")
    video_path = _video_path(project_path, filename)
    from generators.mosaic import extract_frame_pil
    from services.frame_match import _get_video_fps

    fps = float(_get_video_fps(str(video_path)))
    source_image = extract_frame_pil(
        video_path,
        exact_frame,
        apply_sample_aspect_ratio=False,
    )
    if source_image is None:
        raise SilhouetteReferenceError(
            "EXACT_FRAME_UNAVAILABLE",
            "EXACT_FRAME_EXTRACTION",
            "Could not extract the catalog's exact source frame from the source video.",
            {"filename": filename, "frame_index": exact_frame},
        )
    source_bytes = _image_bytes(source_image)

    frames_per_shot, min_interval, max_frames = _selection_preferences()
    from data.annotate import canonical_context_frame_indices

    canonical_indices = canonical_context_frame_indices(
        shot_start,
        shot_end,
        str(shot.get("start_time") or ""),
        str(shot.get("end_time") or ""),
        fps,
        frames_per_shot=frames_per_shot,
        min_frame_interval_s=min_interval,
        max_frames_per_shot=max_frames,
    )
    canonical_indices = sorted(set(canonical_indices))

    context_items: list[dict[str, Any]] = []
    context_metadata: list[dict[str, Any]] = []
    warnings: list[str] = []
    context_number = 1
    for context_index in canonical_indices:
        is_source_frame = context_index == exact_frame
        entry: dict[str, Any] = {
            "frame_index": context_index,
            "timestamp_seconds": context_index / fps if fps > 0 else None,
            "relative_frame": context_index - exact_frame,
            "is_source_frame": is_source_frame,
            "width": None,
            "height": None,
            "mime_type": "image/jpeg",
            "sha256": None,
            "content_id": "exact_source_frame" if is_source_frame else None,
        }
        if is_source_frame:
            entry.update(_image_metadata(source_bytes, source_image, context_index, fps))
            entry["is_source_frame"] = True
            entry["relative_frame"] = 0
            entry["content_id"] = "exact_source_frame"
            context_metadata.append(entry)
            continue
        context_image = extract_frame_pil(
            video_path,
            context_index,
            apply_sample_aspect_ratio=False,
        )
        if context_image is None:
            raise SilhouetteReferenceError(
                "CONTEXT_FRAME_UNAVAILABLE",
                "CONTEXT_FRAME_EXTRACTION",
                "Could not extract a canonical contextual frame from the source video.",
                {"filename": filename, "frame_index": context_index},
            )
        context_bytes = _image_bytes(context_image)
        content_id = f"context_frame_{context_number:02d}"
        context_number += 1
        entry.update(_image_metadata(context_bytes, context_image, context_index, fps))
        entry["relative_frame"] = context_index - exact_frame
        entry["is_source_frame"] = False
        entry["content_id"] = content_id
        context_metadata.append(entry)
        context_items.append({
            "content_id": content_id,
            "data": context_bytes,
            "format": "jpeg",
        })

    if exact_frame in canonical_indices:
        warnings.append(
            "Canonical context included the extraction frame; it is emitted only as exact_source_frame."
        )
    if len(canonical_indices) < 2:
        warnings.append(
            "Canonical context produced fewer than two distinct in-shot frames."
        )

    candidate = _candidate_summary(project_path, record)
    candidate.update({
        "scope": f"{media_type}-{media_id}",
        "film_title": film.get("title"),
        "film_filename": filename,
        "shot_start_frame": shot_start,
        "shot_end_frame": shot_end,
        "timestamp_seconds": exact_frame / fps if fps > 0 else None,
        "fps": fps if fps > 0 else None,
        "silhouette_width": silhouette_width,
        "silhouette_height": silhouette_height,
        "silhouette_mode": silhouette_mode,
        "silhouette_sha256": hashlib.sha256(silhouette_bytes).hexdigest(),
    })
    source_metadata = _image_metadata(source_bytes, source_image, exact_frame, fps)
    source_metadata["is_exact_extraction_frame"] = True
    source_metadata["content_id"] = "exact_source_frame"

    content = [
        {"content_id": "silhouette", "data": silhouette_bytes, "format": "png"},
        {"content_id": "exact_source_frame", "data": source_bytes, "format": "jpeg"},
        *context_items,
    ]
    _validate_packet_size(content)

    content_order = ["metadata", *(item["content_id"] for item in content)]
    return {
        "metadata": {
            "ok": True,
            "candidate_ref": {
                "candidate_id": candidate["candidate_id"],
                "object_id": candidate["object_id"],
                "media_type": media_type,
                "media_id": media_id,
                "shot_id": shot_id,
                "field": field,
                "word": word,
            },
            "candidate": candidate,
            "source_frame": source_metadata,
            "context": {
                "selection_method": "canonical_shot_frame_selection",
                "frames": context_metadata,
            },
            "shot": {
                "scene": shot.get("Scene") or None,
                "start_time": shot.get("start_time") or None,
                "end_time": shot.get("end_time") or None,
                "caption": shot.get("Shot_Caption") or None,
                "scene_caption": shot.get("Scene_Caption") or None,
                "subtitles": _subtitles_for_shot(
                    project_path,
                    media_type,
                    filename,
                    str(shot.get("start_time") or ""),
                    str(shot.get("end_time") or ""),
                ),
            },
            "provenance": {
                "silhouette_is_existing_cache": True,
                "source_frame_is_exact": True,
                "context_selection_method": "canonical_shot_frame_selection",
                "new_inference_performed": False,
                "selection_changed": False,
            },
            "content_order": content_order,
            "warnings": warnings,
        },
        "content": content,
    }