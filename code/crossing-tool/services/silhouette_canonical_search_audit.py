"""Read-only revalidation of historical silhouette search relationships.

For every historical catalog object, this audit asks the current production
``services.search.search_shots`` implementation whether the object's source
shot is returned for its original ``field`` and ``label``.  Annotation values
are captured solely as provenance context; they never determine the verdict.

The audit reads the catalog and annotations, and writes only generated
artifacts under ``outputs/tests/silhouette-canonical-search-audit/``.
"""

from __future__ import annotations

import csv
import json
import os
import tempfile
import time
from collections import Counter, defaultdict
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from PIL import Image, ImageDraw, ImageOps

from data.annotate import atomic_write_text, get_annotation_json_path
from data.media_id import compute_media_id, parse_shot_id
from generators._common import load_font_with_fallback
from services.search import SearchCorpus, search_shots
from services.silhouette_catalog import iter_catalog


AUDIT_DIR = Path("outputs") / "tests" / "silhouette-canonical-search-audit"
EXAMPLES_DIR_NAME = "examples"
RECORDS_CSV_NAME = "canonical_search_records.csv"
LABEL_SUMMARIES_CSV_NAME = "label_summaries.csv"
REPRESENTATIVE_EXAMPLES_CSV_NAME = "representative_examples.csv"
REPORT_JSON_NAME = "report.json"
REPORT_MD_NAME = "report.md"
MAX_MATCHING_SHOT_IDS_PER_RECORD = 10

VALID = "VALID"
QUESTIONABLE = "QUESTIONABLE"
UNVERIFIABLE = "UNVERIFIABLE"
CLASSIFICATIONS = (VALID, QUESTIONABLE, UNVERIFIABLE)

PROBE_LABELS = (
    "coat",
    "yellow coat",
    "wooden post",
    "wanted poster",
    "arm band",
    "wooden plank",
    "sign",
)

_PRIORITY_FIELDS = {"objects": 0, "wearing": 1, "animals": 2}
_PROBE_LABEL_RANK = {label: index for index, label in enumerate(PROBE_LABELS)}

_RECORD_FIELDNAMES = [
    "media_type",
    "media_id",
    "filename",
    "filename_stem",
    "shot_id",
    "canonical_source_shot_id",
    "source_shot_identity",
    "frame",
    "field",
    "historical_label",
    "historical_search_label",
    "historical_json_path",
    "historical_png_path",
    "source_frame",
    "source_annotation_path",
    "source_annotation_found",
    "original_annotation_values",
    "canonical_search_match",
    "source_shot_returned",
    "canonical_search_result_count",
    "canonical_matching_shot_ids",
    "canonical_matching_shot_refs",
    "canonical_matching_shot_ids_truncated",
    "classification",
    "classification_reason",
]


def default_output_dir(project_path: str | Path, *, probe: bool = False) -> Path:
    """Return the canonical output directory for this audit mode."""
    base = Path(project_path) / AUDIT_DIR
    return base / "probe" if probe else base


def _normalise_legacy_shot_id(shot_id: Any) -> str | None:
    """Return the shot-ID display format emitted by ``search_shots``.

    Stable identifiers are returned unchanged.  Legacy integer annotation IDs
    are represented by search as ``shot_00001`` and must compare that way.
    """
    raw = str(shot_id or "").strip()
    if not raw:
        return None
    try:
        parse_shot_id(raw)
    except ValueError:
        try:
            return f"shot_{int(raw):05d}"
        except (TypeError, ValueError):
            return None
    return raw


def _safe_string(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _serialise_csv_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (dict, list, tuple)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    return str(value)


@contextmanager
def _atomic_csv_writer(path: Path, fieldnames: list[str]) -> Iterator[csv.DictWriter]:
    """Yield a CSV writer and atomically publish the completed output file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        newline="",
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
        delete=False,
    )
    temporary_path = Path(handle.name)
    writer = csv.DictWriter(handle, fieldnames=fieldnames)
    writer.writeheader()
    try:
        yield writer
        handle.close()
        os.replace(temporary_path, path)
    except BaseException:
        handle.close()
        try:
            temporary_path.unlink()
        except OSError:
            pass
        raise


def _write_csv(path: Path, rows: list[dict[str, Any]], fieldnames: list[str]) -> None:
    with _atomic_csv_writer(path, fieldnames) as writer:
        for row in rows:
            writer.writerow({field: _serialise_csv_value(row.get(field)) for field in fieldnames})


def _safe_output_dir(
    project: Path,
    output_dir: str | Path | None,
    *,
    probe: bool,
) -> Path:
    """Resolve an audit destination and enforce the outputs/tests boundary."""
    tests_root = (project / "outputs" / "tests").resolve()
    if output_dir is None:
        destination = default_output_dir(project, probe=probe)
    else:
        candidate = Path(output_dir)
        destination = candidate if candidate.is_absolute() else project / candidate
    destination = destination.resolve()
    if destination == tests_root or tests_root not in destination.parents:
        raise ValueError(
            "canonical-search-audit output_dir must be beneath "
            f"{tests_root}"
        )
    return destination


def _metadata_filename_lookups(
    project: Path,
    media_types: tuple[str, ...],
) -> tuple[dict[tuple[str, str], str], dict[tuple[str, str], str]]:
    """Build exact metadata fallback lookups for malformed historical rows."""
    from data.metadata import get_metadata

    by_media_id: dict[tuple[str, str], str] = {}
    by_stem: dict[tuple[str, str], str] = {}
    ambiguous_media_ids: set[tuple[str, str]] = set()
    ambiguous_stems: set[tuple[str, str]] = set()

    for media_type in media_types:
        for metadata in get_metadata(str(project), media_type=media_type):
            filename = _safe_string(metadata.get("filename"))
            if not filename:
                continue
            media_id = compute_media_id(metadata, media_type)
            stem = Path(filename).stem
            media_key = (media_type, media_id)
            stem_key = (media_type, stem)
            if media_key in by_media_id and by_media_id[media_key] != filename:
                ambiguous_media_ids.add(media_key)
            else:
                by_media_id[media_key] = filename
            if stem_key in by_stem and by_stem[stem_key] != filename:
                ambiguous_stems.add(stem_key)
            else:
                by_stem[stem_key] = filename

    for key in ambiguous_media_ids:
        by_media_id.pop(key, None)
    for key in ambiguous_stems:
        by_stem.pop(key, None)
    return by_media_id, by_stem


def _resolve_source_filename(
    record: dict[str, Any],
    media_type: str,
    by_media_id: dict[tuple[str, str], str],
    by_stem: dict[tuple[str, str], str],
) -> str:
    filename = _safe_string(record.get("filename"))
    if filename:
        return filename
    media_id = _safe_string(record.get("media_id"))
    if media_id:
        from_media_id = by_media_id.get((media_type, media_id))
        if from_media_id:
            return from_media_id
    stem = _safe_string(record.get("filename_stem"))
    return by_stem.get((media_type, stem), "") if stem else ""


class _SourceAnnotationCache:
    """Read source annotations lazily for output context, never for verdicts."""

    def __init__(self, project: Path) -> None:
        self._project = project
        self._entries: dict[tuple[str, str], dict[str, dict[str, Any]]] = {}
        self._paths: dict[tuple[str, str], Path] = {}

    def find(
        self,
        media_type: str,
        filename: str,
        canonical_shot_id: str | None,
    ) -> tuple[list[Any], bool, str]:
        if not filename:
            return [], False, ""
        key = (media_type, filename)
        if key not in self._entries:
            self._load(media_type, filename)
        path = self._paths[key]
        annotation = self._entries[key].get(canonical_shot_id or "")
        if not isinstance(annotation, dict):
            return [], False, str(path)
        return annotation, True, str(path)

    def _load(self, media_type: str, filename: str) -> None:
        key = (media_type, filename)
        path = get_annotation_json_path(str(self._project), filename, media_type)
        self._paths[key] = path
        values: dict[str, dict[str, Any]] = {}
        try:
            raw_entries = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            raw_entries = []
        if isinstance(raw_entries, list):
            for entry in raw_entries:
                shot = entry.get("shot") if isinstance(entry, dict) else None
                annotation = shot.get("annotation") if isinstance(shot, dict) else None
                canonical_shot_id = _normalise_legacy_shot_id(
                    shot.get("shot_id") if isinstance(shot, dict) else None
                )
                if canonical_shot_id and isinstance(annotation, dict):
                    values[canonical_shot_id] = annotation
        self._entries[key] = values


def _annotation_values(annotation: list[Any] | dict[str, Any], field: str) -> list[Any]:
    if not isinstance(annotation, dict):
        return []
    value = annotation.get(field)
    if value is None:
        return []
    return list(value) if isinstance(value, list) else [value]


def _historical_png_path(record: dict[str, Any], json_path: Path) -> Path:
    png_name = _safe_string(record.get("png"))
    return json_path.parent / png_name if png_name else json_path.with_suffix(".png")


def _source_frame_path(project: Path, source_frame: Any) -> str:
    value = _safe_string(source_frame)
    if not value or value.startswith("frame:"):
        return ""
    path = Path(value)
    return str(path if path.is_absolute() else project / path)


def _canonical_search_outcome(
    project: Path,
    media_type: str,
    field: str,
    label: str,
    corpus: SearchCorpus,
    source_identities: set[tuple[str, str]],
) -> dict[str, Any]:
    """Run the unmodified canonical search service for one historical query."""
    result = search_shots(
        query=label,
        scopes=None,
        field=field,
        limit=None,
        limit_per_item=None,
        use_all=True,
        project_path=str(project),
        media_type=media_type,
        corpus=corpus,
    )
    raw_results = result.get("results", []) if isinstance(result, dict) else []
    if not isinstance(raw_results, list):
        raise RuntimeError("canonical search returned a non-list results payload")

    refs: list[dict[str, str]] = []
    matching_source_identities: set[tuple[str, str]] = set()
    for item in raw_results:
        if not isinstance(item, dict):
            continue
        filename = _safe_string(item.get("filename"))
        shot_id = _safe_string(item.get("shot_id"))
        if filename and shot_id:
            refs.append({"filename": filename, "shot_id": shot_id})
            source_identity = (filename, shot_id)
            if source_identity in source_identities:
                matching_source_identities.add(source_identity)
    refs.sort(key=lambda item: (item["filename"], item["shot_id"]))
    matching_shot_ids = sorted({item["shot_id"] for item in refs})
    return {
        "result_count": len(raw_results),
        "refs": refs[:MAX_MATCHING_SHOT_IDS_PER_RECORD],
        "source_identities": matching_source_identities,
        "shot_ids": matching_shot_ids[:MAX_MATCHING_SHOT_IDS_PER_RECORD],
        "shot_ids_truncated": len(matching_shot_ids) > MAX_MATCHING_SHOT_IDS_PER_RECORD,
    }


def _base_row(
    record: dict[str, Any],
    *,
    project: Path,
    media_type: str,
    filename: str,
    canonical_shot_id: str | None,
    annotation_values: list[Any],
    annotation_found: bool,
    annotation_path: str,
) -> dict[str, Any]:
    json_path = Path(record.get("path") or "")
    historical_label = record.get("label") if isinstance(record.get("label"), str) else ""
    field = record.get("field") if isinstance(record.get("field"), str) else ""
    return {
        "media_type": media_type,
        "media_id": _safe_string(record.get("media_id")),
        "filename": filename,
        "filename_stem": _safe_string(record.get("filename_stem")),
        "shot_id": _safe_string(record.get("shot_id")),
        "canonical_source_shot_id": canonical_shot_id,
        "source_shot_identity": {
            "media_type": media_type,
            "filename": filename,
            "shot_id": canonical_shot_id,
        },
        "frame": record.get("frame"),
        "field": field,
        "historical_label": historical_label,
        "historical_search_label": historical_label,
        "historical_json_path": str(json_path),
        "historical_png_path": str(_historical_png_path(record, json_path)) if json_path else "",
        "source_frame": record.get("source_frame") or "",
        "source_frame_path": _source_frame_path(project, record.get("source_frame")),
        "source_annotation_path": annotation_path,
        "source_annotation_found": annotation_found,
        "original_annotation_values": annotation_values,
        "canonical_search_match": None,
        "source_shot_returned": None,
        "canonical_search_result_count": None,
        "canonical_matching_shot_ids": [],
        "canonical_matching_shot_refs": [],
        "canonical_matching_shot_ids_truncated": None,
        "classification": UNVERIFIABLE,
        "classification_reason": "",
    }


def _diagnostic_result_sample(
    outcome: dict[str, Any],
    source_identity: tuple[str, str],
) -> tuple[list[str], list[dict[str, str]]]:
    """Return bounded result diagnostics, retaining a matching source shot."""
    sample_refs = list(outcome["refs"])
    if source_identity in outcome["source_identities"]:
        source_ref = {"filename": source_identity[0], "shot_id": source_identity[1]}
        if source_ref not in sample_refs:
            sample_refs = [source_ref, *sample_refs[:MAX_MATCHING_SHOT_IDS_PER_RECORD - 1]]
    sample_ids = []
    for item in sample_refs:
        shot_id = item["shot_id"]
        if shot_id not in sample_ids:
            sample_ids.append(shot_id)
    return sample_ids, sample_refs


def _row_for_record(
    record: dict[str, Any],
    *,
    project: Path,
    media_type: str,
    by_media_id: dict[tuple[str, str], str],
    by_stem: dict[tuple[str, str], str],
    annotations: _SourceAnnotationCache,
    query_cache: dict[tuple[str, str, str], dict[str, Any]],
) -> dict[str, Any]:
    """Build one audit row from a memoized canonical search query outcome."""
    if record.get("error"):
        row = _base_row(
            record,
            project=project,
            media_type=media_type,
            filename="",
            canonical_shot_id=None,
            annotation_values=[],
            annotation_found=False,
            annotation_path="",
        )
        row["classification_reason"] = f"historical catalog JSON is unreadable: {record['error']}"
        return row

    filename = _resolve_source_filename(record, media_type, by_media_id, by_stem)
    canonical_shot_id = _normalise_legacy_shot_id(record.get("shot_id"))
    annotation, annotation_found, annotation_path = annotations.find(
        media_type,
        filename,
        canonical_shot_id,
    )
    field = _safe_string(record.get("field"))
    label = record.get("label") if isinstance(record.get("label"), str) else ""
    row = _base_row(
        record,
        project=project,
        media_type=media_type,
        filename=filename,
        canonical_shot_id=canonical_shot_id,
        annotation_values=_annotation_values(annotation, field),
        annotation_found=annotation_found,
        annotation_path=annotation_path,
    )

    if not field:
        row["classification_reason"] = "historical extraction field is missing"
        return row
    if not label.strip():
        row["classification_reason"] = "historical extraction search label is missing"
        return row
    if not filename:
        row["classification_reason"] = "source filename cannot be established"
        return row
    if canonical_shot_id is None:
        row["classification_reason"] = "source shot identity cannot be established"
        return row

    key = (media_type, field, label)
    outcome = query_cache.get(key)
    if outcome is None:
        row["classification_reason"] = "canonical search outcome was not prepared"
        return row

    if "error" in outcome:
        row["classification_reason"] = str(outcome["error"])
        return row

    source_identity = (filename, canonical_shot_id)
    source_returned = source_identity in outcome["source_identities"]
    matching_shot_ids, matching_shot_refs = _diagnostic_result_sample(
        outcome,
        source_identity,
    )
    row.update({
        "canonical_search_match": source_returned,
        "source_shot_returned": source_returned,
        "canonical_search_result_count": outcome["result_count"],
        "canonical_matching_shot_ids": matching_shot_ids,
        "canonical_matching_shot_refs": matching_shot_refs,
        "canonical_matching_shot_ids_truncated": outcome["shot_ids_truncated"],
        "classification": VALID if source_returned else QUESTIONABLE,
        "classification_reason": (
            "exact source (filename, shot_id) is present in canonical search results"
            if source_returned
            else "exact source (filename, shot_id) is absent from canonical search results"
        ),
    })
    return row


def _increment_breakdown(
    bucket: dict[str, Counter],
    value: Any,
    classification: str,
) -> None:
    key = str(value) if value not in (None, "") else "<missing>"
    bucket[key]["total"] += 1
    bucket[key][classification.lower()] += 1


def _breakdown_dict(bucket: dict[str, Counter]) -> dict[str, dict[str, int]]:
    result: dict[str, dict[str, int]] = {}
    for key in sorted(bucket, key=str.casefold):
        values = bucket[key]
        result[key] = {
            "total": values["total"],
            "valid": values["valid"],
            "questionable": values["questionable"],
            "unverifiable": values["unverifiable"],
        }
    return result


def _word_count(label: str) -> int:
    return len(label.split())


def _example_priority(group: dict[str, Any]) -> tuple[Any, ...]:
    field = str(group["field"])
    label = str(group["label"])
    label_key = label.casefold()
    return (
        _PRIORITY_FIELDS.get(field.casefold(), 3),
        0 if label_key in _PROBE_LABEL_RANK else 1,
        _PROBE_LABEL_RANK.get(label_key, len(PROBE_LABELS)),
        0 if _word_count(label) > 1 else 1,
        -int(group["record_count"]),
        field.casefold(),
        label_key,
    )


def _slug(value: str) -> str:
    safe = "".join(char if char.isalnum() else "_" for char in value.casefold())
    return safe.strip("_")[:80] or "example"


def _font_height(draw: ImageDraw.ImageDraw, font: Any) -> int:
    bbox = draw.textbbox((0, 0), "Ag", font=font)
    return max(1, bbox[3] - bbox[1])


def _wrap_text(draw: ImageDraw.ImageDraw, text: str, font: Any, width: int) -> list[str]:
    words = text.split()
    if not words:
        return [""]
    lines: list[str] = []
    current = words[0]
    for word in words[1:]:
        candidate = f"{current} {word}"
        if draw.textlength(candidate, font=font) <= width:
            current = candidate
        else:
            lines.append(current)
            current = word
    lines.append(current)
    return lines


def _draw_image_box(
    canvas: Image.Image,
    draw: ImageDraw.ImageDraw,
    box: tuple[int, int, int, int],
    image_path: Path,
    *,
    label: str,
    font: Any,
) -> None:
    x, y, width, height = box
    draw.rectangle((x, y, x + width, y + height), fill=(255, 255, 255, 255), outline=(174, 174, 174, 255), width=2)
    if image_path.is_file():
        try:
            image = Image.open(image_path).convert("RGBA")
            image = ImageOps.contain(image, (width - 28, height - 58))
            canvas.alpha_composite(image, (x + (width - image.width) // 2, y + 14 + (height - 58 - image.height) // 2))
        except Exception:
            draw.text((x + 14, y + 34), f"Unreadable image: {image_path.name}", font=font, fill=(120, 30, 30, 255))
    else:
        draw.text((x + 14, y + 34), "Historical image unavailable", font=font, fill=(120, 30, 30, 255))
    draw.text((x + 14, y + height - 28), label, font=font, fill=(40, 40, 40, 255))


def _render_example_panel(row: dict[str, Any], output_path: Path) -> None:
    """Render one source-frame versus historical-silhouette evidence panel."""
    canvas_width = 1640
    margin = 32
    gap = 24
    header_height = 152
    image_height = 560
    body_width = canvas_width - margin * 2

    title_font = load_font_with_fallback(30)
    body_font = load_font_with_fallback(22)
    small_font = load_font_with_fallback(18)
    scratch = Image.new("RGBA", (1, 1), (0, 0, 0, 0))
    scratch_draw = ImageDraw.Draw(scratch)
    annotation_text = json.dumps(row["original_annotation_values"], ensure_ascii=False)
    lines = [
        f"Historical label: {row['historical_label']}    Field: {row['field']}",
        f"Original annotation: {annotation_text}",
        f"Canonical search query: {row['historical_search_label']}",
        f"Canonical result count: {row['canonical_search_result_count']}",
        f"Source shot returned: {'YES' if row['source_shot_returned'] else 'NO'}",
        f"Classification: {row['classification']}",
        f"Source shot: {row['source_shot_identity']['filename']} | {row['source_shot_identity']['shot_id']}",
    ]
    wrapped_lines = [wrapped for line in lines for wrapped in _wrap_text(scratch_draw, line, small_font, body_width)]
    footer_height = max(250, 46 + len(wrapped_lines) * (_font_height(scratch_draw, small_font) + 8))
    canvas_height = margin * 2 + header_height + gap + image_height + gap + footer_height
    canvas = Image.new("RGBA", (canvas_width, canvas_height), (247, 246, 242, 255))
    draw = ImageDraw.Draw(canvas)

    draw.text((margin, margin), "Canonical Search Revalidation", font=title_font, fill=(28, 28, 28, 255))
    header_lines = [
        f"{row['historical_label']} | {row['field']} | {row['classification']}",
        f"Media: {row['media_type']} | {row['filename']} | Shot: {row['shot_id']} | Frame: {row['frame']}",
    ]
    cursor_y = margin + _font_height(draw, title_font) + 12
    for line in header_lines:
        draw.text((margin, cursor_y), line, font=body_font, fill=(50, 50, 50, 255))
        cursor_y += _font_height(draw, body_font) + 8

    image_y = margin + header_height + gap
    image_width = (body_width - gap) // 2
    source_path = Path(str(row.get("source_frame_path") or ""))
    silhouette_path = Path(str(row.get("historical_png_path") or ""))
    _draw_image_box(
        canvas,
        draw,
        (margin, image_y, image_width, image_height),
        source_path,
        label="SOURCE FRAME",
        font=small_font,
    )
    _draw_image_box(
        canvas,
        draw,
        (margin + image_width + gap, image_y, image_width, image_height),
        silhouette_path,
        label="HISTORICAL SILHOUETTE",
        font=small_font,
    )

    cursor_y = image_y + image_height + gap + 20
    for line in wrapped_lines:
        draw.text((margin, cursor_y), line, font=small_font, fill=(32, 32, 32, 255))
        cursor_y += _font_height(draw, small_font) + 8
    canvas.save(output_path)


def _render_examples(
    target_dir: Path,
    groups: dict[tuple[str, str], dict[str, Any]],
    sample_size: int,
) -> list[dict[str, Any]]:
    examples_dir = target_dir / EXAMPLES_DIR_NAME
    examples_dir.mkdir(parents=True, exist_ok=True)
    selected = _select_representative_groups(groups.values(), sample_size)
    rows: list[dict[str, Any]] = []
    for index, group in enumerate(selected, 1):
        row = dict(group["representative"])
        example_dir = examples_dir / f"{index:02d}_{_slug(str(row['field']))}_{_slug(str(row['historical_label']))}"
        example_dir.mkdir(parents=True, exist_ok=True)
        panel_path = example_dir / "panel.png"
        evidence_path = example_dir / "evidence.json"
        _render_example_panel(row, panel_path)
        atomic_write_text(evidence_path, json.dumps(row, indent=2, ensure_ascii=False) + "\n")
        rows.append({
            "media_type": row["media_type"],
            "media_id": row["media_id"],
            "filename": row["filename"],
            "shot_id": row["shot_id"],
            "field": row["field"],
            "historical_label": row["historical_label"],
            "original_annotation_values": row["original_annotation_values"],
            "canonical_search_result_count": row["canonical_search_result_count"],
            "source_shot_returned": row["source_shot_returned"],
            "classification": row["classification"],
            "record_count_for_label": group["record_count"],
            "panel_path": str(panel_path),
            "evidence_path": str(evidence_path),
        })
    return rows


def _select_representative_groups(
    groups: Any,
    sample_size: int,
) -> list[dict[str, Any]]:
    """Select bounded examples with known-label and priority-field coverage."""
    ranked = sorted(groups, key=_example_priority)
    if sample_size <= 0:
        return []

    selected: list[dict[str, Any]] = []
    selected_keys: set[tuple[str, str]] = set()

    def add(group: dict[str, Any]) -> None:
        key = (str(group["field"]), str(group["label"]))
        if key not in selected_keys and len(selected) < sample_size:
            selected.append(group)
            selected_keys.add(key)

    # Preserve direct evidence for the known historical problem labels first.
    for group in ranked:
        if str(group["label"]).casefold() in _PROBE_LABEL_RANK:
            add(group)

    # A giant objects population must not hide wearing/animals evidence.
    for field in _PRIORITY_FIELDS:
        for group in ranked:
            if str(group["field"]).casefold() == field:
                add(group)
                break

    for group in ranked:
        add(group)
    return selected


def _label_summary_rows(by_label: dict[str, Counter]) -> list[dict[str, Any]]:
    rows = []
    for label, values in by_label.items():
        rows.append({
            "historical_label": label,
            "word_count": _word_count(label),
            "total": values["total"],
            "valid": values["valid"],
            "questionable": values["questionable"],
            "unverifiable": values["unverifiable"],
        })
    return sorted(rows, key=lambda row: (-int(row["total"]), str(row["historical_label"]).casefold()))


def _top_questionable_labels(by_label: dict[str, Counter], *, multiword_only: bool = False) -> list[dict[str, Any]]:
    values = []
    for label, counts in by_label.items():
        if counts["questionable"] <= 0 or (multiword_only and _word_count(label) <= 1):
            continue
        values.append({
            "historical_label": label,
            "record_count": counts["questionable"],
            "word_count": _word_count(label),
        })
    return sorted(values, key=lambda row: (-int(row["record_count"]), str(row["historical_label"]).casefold()))[:30]


def _top_questionable_fields(by_field: dict[str, Counter]) -> list[dict[str, Any]]:
    values = [
        {"field": field, "record_count": counts["questionable"]}
        for field, counts in by_field.items()
        if counts["questionable"] > 0
    ]
    return sorted(values, key=lambda row: (-int(row["record_count"]), str(row["field"]).casefold()))


def _report_markdown(report: dict[str, Any]) -> str:
    summary = report["summary"]
    safety = report["live_data_safety"]
    lines = [
        "# Silhouette Canonical Search Revalidation Audit",
        "",
        f"Project: `{report['project_path']}`",
        f"Output directory: `{report['output_dir']}`",
        f"Mode: `{'probe' if report['probe'] else 'full archive'}`",
        "",
        "## Validity Criterion",
        "",
        "Each historical silhouette is rechecked by calling `services.search.search_shots` with its original field and historical label. `VALID` means the exact source `(media_type, filename, shot_id)` is returned. `QUESTIONABLE` means it is not returned. Source annotations are recorded only as context.",
        f"Per-record canonical matching shot IDs are capped at {MAX_MATCHING_SHOT_IDS_PER_RECORD} diagnostic entries; the complete canonical result count and exact source-shot membership remain authoritative.",
        "",
        "## Summary",
        "",
        f"- Total records: **{summary['total_records']}**",
        f"- Valid: **{summary['valid']}** ({summary['valid_percentage']}%)",
        f"- Questionable: **{summary['questionable']}** ({summary['questionable_percentage']}%)",
        f"- Unverifiable: **{summary['unverifiable']}**",
        f"- Canonical queries executed: **{report['canonical_search']['executed_queries']}**",
        f"- Runtime: **{report['runtime_seconds']} seconds**",
        "",
        "## Safety",
        "",
        f"- LIVE SILHOUETTE DATA MODIFIED = {safety['live_silhouette_data_modified']}",
        f"- LIVE PNG DATA MODIFIED = {safety['live_png_data_modified']}",
        f"- LIVE ANNOTATIONS MODIFIED = {safety['live_annotations_modified']}",
        "",
        "## Top Questionable Labels",
        "",
    ]
    for item in report["top_questionable_labels"]:
        lines.append(f"- `{item['historical_label']}`: {item['record_count']}")
    if not report["top_questionable_labels"]:
        lines.append("- None")
    lines.extend(["", "## Artifacts", ""])
    for name, path in report["artifacts"].items():
        lines.append(f"- `{name}`: `{path}`")
    return "\n".join(lines) + "\n"


def _iter_audit_records(
    project: Path,
    media_types: tuple[str, ...],
    *,
    probe_labels: set[str] | None,
) -> Iterator[tuple[str, dict[str, Any]]]:
    """Yield audit records, traversing only known label directories for a probe."""
    for media_type in media_types:
        if probe_labels is None:
            for record in iter_catalog(str(project), media_type=media_type):
                yield media_type, record
            continue
        for label in PROBE_LABELS:
            if label.casefold() not in probe_labels:
                continue
            for record in iter_catalog(str(project), media_type=media_type, label=label):
                yield media_type, record


def audit_silhouette_canonical_search(
    project_path: str | Path,
    *,
    media_type: str = "both",
    output_dir: str | Path | None = None,
    sample_size: int = 25,
    probe: bool = False,
) -> dict[str, Any]:
    """Audit historical silhouettes against the current canonical search API.

    The only files written are artifacts below ``outputs/tests``.  The source
    catalog, PNG assets, annotations, provenance, assignment metadata, and all
    production indexes remain untouched.
    """
    project = Path(project_path).resolve()
    if media_type not in {"movie", "gameplay", "both"}:
        raise ValueError("media_type must be 'movie', 'gameplay', or 'both'")
    if sample_size < 0:
        raise ValueError("sample_size must be non-negative")
    media_types = ("movie", "gameplay") if media_type == "both" else (media_type,)
    target_dir = _safe_output_dir(project, output_dir, probe=probe)
    target_dir.mkdir(parents=True, exist_ok=True)

    started = time.monotonic()

    by_media_id, by_stem = _metadata_filename_lookups(project, media_types)
    annotations = _SourceAnnotationCache(project)
    corpus = SearchCorpus(str(project))
    probe_labels = {label.casefold() for label in PROBE_LABELS} if probe else None
    query_cache: dict[tuple[str, str, str], dict[str, Any]] = {}
    query_sets: dict[str, set[tuple[str, str]]] = defaultdict(set)
    query_source_identities: dict[tuple[str, str, str], set[tuple[str, str]]] = defaultdict(set)
    for current_media_type, record in _iter_audit_records(
        project,
        media_types,
        probe_labels=probe_labels,
    ):
        if record.get("error"):
            continue
        field = _safe_string(record.get("field"))
        label = record.get("label") if isinstance(record.get("label"), str) else ""
        if field and label.strip():
            query_sets[current_media_type].add((field, label))
            filename = _resolve_source_filename(record, current_media_type, by_media_id, by_stem)
            canonical_shot_id = _normalise_legacy_shot_id(record.get("shot_id"))
            if filename and canonical_shot_id:
                query_source_identities[(current_media_type, field, label)].add(
                    (filename, canonical_shot_id)
                )
    for current_media_type, queries in query_sets.items():
        corpus.prepare_queries(current_media_type, queries)
    for key, source_identities in query_source_identities.items():
        current_media_type, field, label = key
        try:
            query_cache[key] = _canonical_search_outcome(
                project,
                current_media_type,
                field,
                label,
                corpus,
                source_identities,
            )
        except Exception as exc:
            query_cache[key] = {"error": f"canonical search failed: {exc}"}

    classification_counts: Counter = Counter()
    by_field: dict[str, Counter] = defaultdict(Counter)
    by_media: dict[str, Counter] = defaultdict(Counter)
    by_words: dict[str, Counter] = defaultdict(Counter)
    by_label: dict[str, Counter] = defaultdict(Counter)
    questionable_groups: dict[tuple[str, str], dict[str, Any]] = {}

    records_csv_path = target_dir / RECORDS_CSV_NAME
    total_records = 0
    with _atomic_csv_writer(records_csv_path, _RECORD_FIELDNAMES) as writer:
        for current_media_type, record in _iter_audit_records(
            project,
            media_types,
            probe_labels=probe_labels,
        ):
            total_records += 1
            row = _row_for_record(
                record,
                project=project,
                media_type=current_media_type,
                by_media_id=by_media_id,
                by_stem=by_stem,
                annotations=annotations,
                query_cache=query_cache,
            )
            writer.writerow({field: _serialise_csv_value(row.get(field)) for field in _RECORD_FIELDNAMES})
            classification = str(row["classification"])
            classification_counts[classification] += 1
            _increment_breakdown(by_field, row["field"], classification)
            _increment_breakdown(by_media, row["media_type"], classification)
            label = str(row["historical_label"] or "<missing>")
            _increment_breakdown(by_label, label, classification)
            _increment_breakdown(by_words, str(_word_count(label)), classification)
            if classification == QUESTIONABLE:
                key = (str(row["field"]), label)
                group = questionable_groups.setdefault(
                    key,
                    {"field": row["field"], "label": label, "record_count": 0, "representative": row},
                )
                group["record_count"] += 1

    label_summaries_path = target_dir / LABEL_SUMMARIES_CSV_NAME
    _write_csv(
        label_summaries_path,
        _label_summary_rows(by_label),
        ["historical_label", "word_count", "total", "valid", "questionable", "unverifiable"],
    )
    representative_rows = _render_examples(target_dir, questionable_groups, sample_size)
    representative_csv_path = target_dir / REPRESENTATIVE_EXAMPLES_CSV_NAME
    _write_csv(
        representative_csv_path,
        representative_rows,
        [
            "media_type",
            "media_id",
            "filename",
            "shot_id",
            "field",
            "historical_label",
            "original_annotation_values",
            "canonical_search_result_count",
            "source_shot_returned",
            "classification",
            "record_count_for_label",
            "panel_path",
            "evidence_path",
        ],
    )

    valid = classification_counts[VALID]
    questionable = classification_counts[QUESTIONABLE]
    unverifiable = classification_counts[UNVERIFIABLE]
    elapsed = time.monotonic() - started

    report = {
        "project_path": str(project),
        "output_dir": str(target_dir),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "probe": probe,
        "probe_labels": list(PROBE_LABELS) if probe else [],
        "media_types": list(media_types),
        "canonical_search": {
            "service": "services.search.search_shots",
            "parameters": {
                "scopes": None,
                "use_all": True,
                "limit": None,
                "limit_per_item": None,
            },
            "query_identity": "(media_type, field, historical_search_label)",
            "source_identity": "(media_type, filename, canonical_search_shot_id)",
            "executed_queries": len(query_cache),
            "failed_queries": sum(1 for outcome in query_cache.values() if "error" in outcome),
        },
        "summary": {
            "total_records": total_records,
            "valid": valid,
            "questionable": questionable,
            "unverifiable": unverifiable,
            "valid_percentage": round((valid / total_records * 100.0) if total_records else 0.0, 2),
            "questionable_percentage": round((questionable / total_records * 100.0) if total_records else 0.0, 2),
        },
        "breakdowns": {
            "by_field": _breakdown_dict(by_field),
            "by_media_type": _breakdown_dict(by_media),
            "by_word_count": _breakdown_dict(by_words),
            "by_historical_label": _breakdown_dict(by_label),
        },
        "top_questionable_labels": _top_questionable_labels(by_label),
        "top_questionable_fields": _top_questionable_fields(by_field),
        "top_questionable_multiword_labels": _top_questionable_labels(by_label, multiword_only=True),
        "representative_examples_rendered": len(representative_rows),
        "runtime_seconds": round(elapsed, 3),
        "live_data_safety": {
            "enforcement": "audit code writes only generated artifacts beneath outputs/tests",
            "live_silhouette_data_modified": "NO",
            "live_png_data_modified": "NO",
            "live_annotations_modified": "NO",
        },
        "artifacts": {
            "records_csv": str(records_csv_path),
            "label_summaries_csv": str(label_summaries_path),
            "representative_examples_csv": str(representative_csv_path),
            "examples_dir": str(target_dir / EXAMPLES_DIR_NAME),
            "report_json": str(target_dir / REPORT_JSON_NAME),
            "report_md": str(target_dir / REPORT_MD_NAME),
        },
    }
    report_json_path = target_dir / REPORT_JSON_NAME
    atomic_write_text(report_json_path, json.dumps(report, indent=2, ensure_ascii=False) + "\n")
    atomic_write_text(target_dir / REPORT_MD_NAME, _report_markdown(report))
    return report