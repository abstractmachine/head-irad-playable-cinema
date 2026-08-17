"""Generate Markdown audits for synthetic untyped Project DATAVIS items."""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime
import json
from pathlib import Path
from typing import Any


_MEDIA_TYPES = ("movie", "gameplay")
_SOURCES = ("shot", "silhouettes")


def generate_untyped_audit(
    project_path: str | Path,
    source: str,
    *,
    now: datetime | None = None,
) -> Path:
    """Write one timestamped untyped audit and return its path."""
    if source not in _SOURCES:
        raise ValueError(f"Unknown untyped audit source: {source}")

    project = Path(project_path)
    grouped = (
        _collect_untyped_shots(project)
        if source == "shot"
        else _collect_untyped_silhouettes(project)
    )
    generated_at = now or datetime.now()
    timestamp = generated_at.strftime("%Y-%m-%d-%H-%M-%S")
    filename = (
        f"untyped-shot-{timestamp}.md"
        if source == "shot"
        else f"untyped-silhouettes-{timestamp}.md"
    )
    output_path = project / "outputs" / "audits" / filename
    output_path.parent.mkdir(parents=True, exist_ok=True)

    lines = ["# untyped", f"## {source}", ""]
    for media_filename in sorted(grouped, key=str.casefold):
        lines.append(f"# {media_filename}")
        for shot_id, timecode in sorted(
            grouped[media_filename], key=lambda item: _stamp_sort_key(item[1]),
        ):
            lines.append(f"- {shot_id} ({timecode})")
        lines.append("")

    from data.annotate import atomic_write_text

    atomic_write_text(output_path, "\n".join(lines))
    return output_path


def _collect_untyped_shots(project: Path) -> dict[str, list[tuple[str, str]]]:
    from data.metadata import load_json_metadata

    grouped: dict[str, list[tuple[str, str]]] = defaultdict(list)
    base = project / "data" / "annotations" / "shots"
    for media_type in _MEDIA_TYPES:
        metadata = load_json_metadata(str(project), media_type)
        metadata_by_stem = {
            Path(str(record.get("filename") or "")).stem: record
            for record in metadata
            if record.get("filename")
        }
        annotation_dir = base / media_type
        if not annotation_dir.is_dir():
            continue
        for annotation_path in sorted(annotation_dir.glob("*.json")):
            if annotation_path.name.endswith(".manifest.json"):
                continue
            try:
                payload = json.loads(annotation_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if not isinstance(payload, list):
                continue

            stem = _annotation_stem(annotation_path)
            metadata_record = metadata_by_stem.get(stem, {})
            filename = str(metadata_record.get("filename") or stem)
            rows, rows_by_id = _shotlist_context(
                project, filename, media_type, metadata_record,
            )
            for index, entry in enumerate(payload):
                if not isinstance(entry, dict):
                    continue
                shot = entry.get("shot")
                shot = shot if isinstance(shot, dict) else {}
                annotation = shot.get("annotation")
                annotation = annotation if isinstance(annotation, dict) else {}
                type_value = annotation.get("type")
                if isinstance(type_value, str) and type_value.strip():
                    continue
                shot_id = str(shot.get("shot_id") or "")
                row = rows_by_id.get(shot_id)
                if row is None and index < len(rows):
                    row = rows[index]
                grouped[filename].append(
                    _audit_item(row, shot_id=shot_id)
                )
    return grouped


def _collect_untyped_silhouettes(project: Path) -> dict[str, list[tuple[str, str]]]:
    from data.metadata import load_json_metadata
    from services.illustration_index import query_untyped_records

    grouped: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for media_type in _MEDIA_TYPES:
        metadata = load_json_metadata(str(project), media_type)
        metadata_by_stem = {
            Path(str(record.get("filename") or "")).stem: record
            for record in metadata
            if record.get("filename")
        }
        result = query_untyped_records(
            project, "silhouettes", media_type,
        )
        if not result.get("usable"):
            continue
        contexts: dict[str, tuple[list[dict], dict[str, dict]]] = {}
        for record in result.get("records", []):
            stem = str(record.get("filename_stem") or "")
            metadata_record = metadata_by_stem.get(stem, {})
            filename = str(record.get("filename") or metadata_record.get("filename") or stem)
            if filename not in contexts:
                contexts[filename] = _shotlist_context(
                    project, filename, media_type, metadata_record,
                )
            rows, rows_by_id = contexts[filename]
            shot_id = str(record.get("shot_id") or "")
            frame = _int_or_none(record.get("frame"))
            row = rows_by_id.get(shot_id)
            if row is None and frame is not None:
                row = next((
                    candidate for candidate in rows
                    if _frame_in_row(frame, candidate)
                ), None)
            grouped[filename].append(
                _audit_item(row, frame=frame, shot_id=shot_id)
            )
    return grouped


def _annotation_stem(path: Path) -> str:
    suffix = ".annotations.json"
    return path.name[:-len(suffix)] if path.name.endswith(suffix) else path.stem


def _shotlist_context(
    project: Path,
    filename: str,
    media_type: str,
    metadata_record: dict,
) -> tuple[list[dict], dict[str, dict]]:
    from data.media_id import build_shot_id, compute_media_id
    from data.shotlist import read_shotlist

    try:
        rows = read_shotlist(str(project), filename, media_type)
    except (FileNotFoundError, OSError):
        return [], {}

    media_id = str(metadata_record.get("media_id") or "")
    if not media_id and metadata_record:
        media_id = compute_media_id(metadata_record, media_type)
    rows_by_id: dict[str, dict] = {}
    for row in rows:
        shot_id = str(row.get("shot_id") or "")
        if not shot_id and media_id:
            start_frame = _int_or_none(row.get("start_frame"))
            end_frame = _int_or_none(row.get("end_frame"))
            if start_frame is not None and end_frame is not None:
                shot_id = build_shot_id(media_id, start_frame, end_frame)
        if shot_id:
            row["shot_id"] = shot_id
            rows_by_id[shot_id] = row
    return rows, rows_by_id


def _audit_item(
    row: dict | None,
    *,
    frame: int | None = None,
    shot_id: str = "",
) -> tuple[str, str]:
    resolved_shot_id = shot_id or str((row or {}).get("shot_id") or "unknown")
    return resolved_shot_id, _framecode_stamp(
        row, frame=frame, shot_id=resolved_shot_id,
    )


def _framecode_stamp(
    row: dict | None,
    *,
    frame: int | None = None,
    shot_id: str = "",
) -> str:
    if row:
        start_time = str(row.get("start_time") or "")
        if frame is not None:
            start_frame = _int_or_none(row.get("start_frame"))
            end_frame = _int_or_none(row.get("end_frame"))
            end_time = str(row.get("end_time") or "")
            start_seconds = _timecode_seconds(start_time)
            end_seconds = _timecode_seconds(end_time)
            if (
                start_frame is not None
                and end_frame is not None
                and end_frame > start_frame
                and start_seconds is not None
                and end_seconds is not None
            ):
                progress = (frame - start_frame) / (end_frame - start_frame)
                seconds = start_seconds + max(0.0, min(1.0, progress)) * (
                    end_seconds - start_seconds
                )
                from data.shot_detection import format_timecode

                return format_timecode(seconds)
        if start_time:
            return start_time
    if frame is not None:
        return f"f{frame:06d}"
    if shot_id:
        try:
            from data.media_id import parse_shot_id

            _, start_frame, _ = parse_shot_id(shot_id)
            return f"f{start_frame:06d}"
        except ValueError:
            pass
    return "unknown"


def _frame_in_row(frame: int, row: dict) -> bool:
    start = _int_or_none(row.get("start_frame"))
    end = _int_or_none(row.get("end_frame"))
    return start is not None and end is not None and start <= frame <= end


def _timecode_seconds(value: str) -> float | None:
    try:
        hours, minutes, seconds = value.split(":")
        return int(hours) * 3600 + int(minutes) * 60 + float(seconds)
    except (AttributeError, TypeError, ValueError):
        return None


def _stamp_sort_key(stamp: str) -> tuple[int, float | str]:
    seconds = _timecode_seconds(stamp)
    if seconds is not None:
        return 0, seconds
    if stamp.startswith("f") and stamp[1:].isdigit():
        return 1, float(stamp[1:])
    return 2, stamp


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
