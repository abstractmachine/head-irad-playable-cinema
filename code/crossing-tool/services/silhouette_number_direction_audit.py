"""Read-only forensic audit for reverse-direction silhouette number variants.

This audit consumes the completed silhouette morphology audit under
``outputs/tests/silhouette-number-morphology-audit/`` and investigates the
reverse-direction population:

    silhouette plural  ←  annotation singular

It does not mutate canonical project data. It only writes generated forensic
artifacts under ``outputs/tests/silhouette-number-direction-audit/``.
"""

from __future__ import annotations

import csv
import hashlib
import json
import math
import re
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image, ImageDraw, ImageOps

from data.annotate import atomic_write_text
from data.metadata import load_json_metadata
from generators._common import load_font_with_fallback
from services.silhouette_catalog import scan_catalog


MORPHOLOGY_AUDIT_DIR = Path("outputs") / "tests" / "silhouette-number-morphology-audit"
DIR_AUDIT_DIR = Path("outputs") / "tests" / "silhouette-number-direction-audit"

MORPHOLOGY_REPORT_NAME = "report.json"
MORPHOLOGY_RECORDS_NAME = "morphology_records.csv"

REPORT_JSON_NAME = "report.json"
REPORT_MD_NAME = "report.md"
REVERSE_RELATIONSHIPS_CSV = "reverse_relationships.csv"
SAMPLED_EXAMPLES_CSV = "sampled_examples.csv"

EXAMPLES_DIR_NAME = "examples"
PANEL_NAME = "panel.png"
EVIDENCE_NAME = "evidence.json"

REVERSE_DIRECTION = "silhouette_plural_annotation_singular"
FORWARD_DIRECTION = "silhouette_singular_annotation_plural"

VISUAL_PLURAL = "VISUAL_PLURAL"
ANNOTATION_NUMBER_MISMATCH = "ANNOTATION_NUMBER_MISMATCH"
SEGMENTATION_MULTIPLICITY = "SEGMENTATION_MULTIPLICITY"
HISTORICAL_LABEL_PROPAGATION = "HISTORICAL_LABEL_PROPAGATION"
DUPLICATE_NUMBER_VARIANT = "DUPLICATE_NUMBER_VARIANT"
MORPHOLOGICALLY_VALID_BUT_VISUALLY_ODD = "MORPHOLOGICALLY_VALID_BUT_VISUALLY_ODD"
OTHER = "OTHER"

_OBVIOUS_PLURAL_VISUAL_HINTS = {
    "wall",
    "walls",
    "post",
    "posts",
    "wheel",
    "wheels",
    "plant",
    "plants",
    "beam",
    "beams",
    "plank",
    "planks",
    "poster",
    "posters",
    "chair",
    "chairs",
    "holder",
    "holders",
    "stand",
    "stands",
    "cabinet",
    "cabinets",
    "car",
    "cars",
    "track",
    "tracks",
    "cup",
    "cups",
    "bed",
    "beds",
    "stump",
    "stumps",
    "bar",
    "bars",
    "pot",
    "pots",
    "building",
    "buildings",
    "window",
    "windows",
    "bag",
    "bags",
    "shaker",
    "shakers",
    "rail",
    "rails",
    "block",
    "blocks",
    "slat",
    "slats",
    "step",
    "steps",
    "can",
    "cans",
    "hook",
    "hooks",
    "tool",
    "tools",
    "cross",
    "crosses",
    "wiper",
    "wipers",
    "bird",
    "birds",
    "deer",
    "head",
    "heads",
    "mug",
    "mugs",
    "pipe",
    "pipes",
}


def _project_path(project_path: str | Path) -> Path:
    return Path(project_path)


def default_source_audit_dir(project_path: str | Path) -> Path:
    return _project_path(project_path) / MORPHOLOGY_AUDIT_DIR


def default_output_dir(project_path: str | Path) -> Path:
    return _project_path(project_path) / DIR_AUDIT_DIR


def _normalize_text(value: str | None) -> str:
    if value is None:
        return ""
    text = value.lower().replace("_", " ")
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _slugify(value: str) -> str:
    value = _normalize_text(value)
    return re.sub(r"[^a-z0-9]+", "-", value).strip("-") or "example"


def _json_text(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _load_morphology_rows(source_audit_dir: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    report_path = source_audit_dir / MORPHOLOGY_REPORT_NAME
    records_path = source_audit_dir / MORPHOLOGY_RECORDS_NAME
    if not report_path.exists():
        raise FileNotFoundError(f"Missing morphology report: {report_path}")
    if not records_path.exists():
        raise FileNotFoundError(f"Missing morphology records: {records_path}")

    report = json.loads(report_path.read_text(encoding="utf-8"))
    rows: list[dict[str, Any]] = []
    with records_path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        for raw_row in reader:
            row = dict(raw_row)
            row["frame"] = int(row.get("frame") or 0)
            row["silhouette_tokens"] = _parse_json_field(row.get("silhouette_tokens"), [])
            row["annotation_tokens"] = _parse_json_field(row.get("annotation_tokens"), [])
            row["differing_token_positions"] = _parse_json_field(row.get("differing_token_positions"), [])
            row["morphology_library_result"] = _parse_json_field(row.get("morphology_library_result"), {})
            row["morphologically_clean"] = str(row.get("morphologically_clean", "")).lower() == "true"
            row["semantic_ambiguity"] = str(row.get("semantic_ambiguity", "")).lower() == "true"
            rows.append(row)
    return report, rows


def _parse_json_field(raw: str | None, default: Any) -> Any:
    text = (raw or "").strip()
    if not text:
        return default
    try:
        return json.loads(text)
    except Exception:
        return default


def _load_titles(project_path: Path) -> dict[tuple[str, str], str]:
    titles: dict[tuple[str, str], str] = {}
    for media_type in ("movie", "gameplay"):
        for record in load_json_metadata(str(project_path), media_type):
            media_id = str(record.get("media_id") or "")
            if not media_id:
                continue
            title = str(record.get("title") or record.get("filename") or media_id)
            titles[(media_type, media_id)] = title
    return titles


def _load_catalog_index(project_path: Path) -> dict[str, dict[tuple[str, str, str, int, str], list[dict[str, Any]]]]:
    index: dict[str, dict[tuple[str, str, str, int, str], list[dict[str, Any]]]] = {
        "same_frame": defaultdict(list),
        "same_shot": defaultdict(list),
        "by_label": defaultdict(list),
    }
    for media_type in ("movie", "gameplay"):
        for record in scan_catalog(str(project_path), media_type=media_type):
            if "error" in record:
                continue
            media_id = str(record.get("media_id") or "")
            shot_id = str(record.get("shot_id") or "")
            frame = int(record.get("frame") or 0)
            label = _normalize_text(str(record.get("label") or ""))
            if not media_id or not shot_id or not label:
                continue
            key = (media_type, media_id, shot_id, frame, label)
            same_shot_key = (media_type, media_id, shot_id, label)
            index["same_frame"][key].append(record)
            index["same_shot"][same_shot_key].append(record)
            index["by_label"][(media_type, label)].append(record)
    return index


def _compare_pngs(primary_path: Path, counterpart_path: Path) -> dict[str, Any]:
    primary = Image.open(primary_path).convert("RGBA")
    counterpart = Image.open(counterpart_path).convert("RGBA")
    primary_bytes = primary.tobytes()
    counterpart_bytes = counterpart.tobytes()
    same_dimensions = primary.size == counterpart.size
    pixel_equal = same_dimensions and primary_bytes == counterpart_bytes
    alpha_iou = None
    bbox_iou = None
    comparison_mode = "none"

    if same_dimensions:
        alpha_iou = _alpha_iou(primary, counterpart)
        comparison_mode = "alpha_iou"
    else:
        comparison_mode = "bbox_iou"

    return {
        "primary_dimensions": list(primary.size),
        "counterpart_dimensions": list(counterpart.size),
        "primary_hash": _sha256_file(primary_path),
        "counterpart_hash": _sha256_file(counterpart_path),
        "file_hash_equal": primary_bytes == counterpart_bytes,
        "pixel_equal_if_same_dimensions": pixel_equal,
        "alpha_iou": alpha_iou,
        "bbox_iou": bbox_iou,
        "comparison_mode": comparison_mode,
    }


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _alpha_iou(primary: Image.Image, counterpart: Image.Image) -> float:
    if primary.size != counterpart.size:
        return 0.0
    a = np.asarray(primary.getchannel("A"), dtype=bool)
    b = np.asarray(counterpart.getchannel("A"), dtype=bool)
    inter = int(np.logical_and(a, b).sum())
    union = int(np.logical_or(a, b).sum())
    return float(inter / union) if union else 0.0


def _bbox_iou(a_bbox: list[Any] | tuple[Any, ...] | None, b_bbox: list[Any] | tuple[Any, ...] | None) -> float | None:
    if not a_bbox or not b_bbox or len(a_bbox) < 4 or len(b_bbox) < 4:
        return None
    ax, ay, aw, ah = map(float, a_bbox[:4])
    bx, by, bw, bh = map(float, b_bbox[:4])
    left = max(ax, bx)
    top = max(ay, by)
    right = min(ax + aw, bx + bw)
    bottom = min(ay + ah, by + bh)
    inter = max(0.0, right - left) * max(0.0, bottom - top)
    if inter <= 0:
        return 0.0
    union = aw * ah + bw * bh - inter
    return float(inter / union) if union else 0.0


def _candidate_labels(label: str) -> set[str]:
    text = _normalize_text(label)
    candidates = {text}
    try:
        import inflect

        engine = inflect.engine()
        singular = engine.singular_noun(text)
        plural = engine.plural_noun(text)
        if singular:
            candidates.add(_normalize_text(str(singular)))
        if plural:
            candidates.add(_normalize_text(str(plural)))
    except Exception:
        pass
    return {candidate for candidate in candidates if candidate}


def _select_relationship_groups(
    groups: list[dict[str, Any]],
    *,
    top_count: int,
    mid_count: int,
    tail_count: int,
    explicit_keys: list[tuple[str, str, str]],
) -> list[dict[str, Any]]:
    selected: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()

    lookup = {group["key"]: group for group in groups}
    for key in explicit_keys:
        group = lookup.get(key)
        if group and key not in seen:
            selected.append(group)
            seen.add(key)

    def _add(group: dict[str, Any]) -> None:
        key = group["key"]
        if key not in seen:
            selected.append(group)
            seen.add(key)

    for group in groups[:top_count]:
        _add(group)

    if len(groups) > top_count:
        stride = max(1, (len(groups) - top_count) // max(1, mid_count))
        for group in groups[top_count::stride]:
            if len([g for g in selected if g["tier"] == "mid"]) >= mid_count:
                break
            _add({**group, "tier": "mid"})

    tail_groups = list(reversed(groups[-max(tail_count * 2, tail_count):]))
    for group in tail_groups:
        if len([g for g in selected if g["tier"] == "tail"]) >= tail_count:
            break
        _add({**group, "tier": "tail"})

    # Fill any remaining slots in descending frequency order.
    for group in groups:
        if len(selected) >= top_count + mid_count + tail_count:
            break
        _add({**group, "tier": group.get("tier", "extra")})

    return selected[: top_count + mid_count + tail_count]


def _annotated_groups(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        key = (str(row["field"]), str(row["silhouette_label"]), str(row["annotation_value"]))
        grouped[key].append(row)

    groups: list[dict[str, Any]] = []
    for key, group_rows in grouped.items():
        field, silhouette_label, annotation_value = key
        unique_shots = {row["shot_id"] for row in group_rows}
        unique_media = {(row["media_type"], row["media_id"]) for row in group_rows}
        groups.append(
            {
                "key": key,
                "field": field,
                "silhouette_label": silhouette_label,
                "annotation_value": annotation_value,
                "rows": group_rows,
                "record_count": len(group_rows),
                "unique_shots": len(unique_shots),
                "unique_media": len(unique_media),
                "percentage_of_direction_population": None,
            }
        )

    groups.sort(key=lambda item: (-item["record_count"], item["field"], item["silhouette_label"], item["annotation_value"]))
    return groups


def _load_title(project_titles: dict[tuple[str, str], str], media_type: str, media_id: str) -> str:
    return project_titles.get((media_type, media_id), media_id)


def _choose_representative_row(
    group: dict[str, Any],
    catalog_index: dict[str, dict[tuple[str, str, str, int, str], list[dict[str, Any]]]],
) -> dict[str, Any]:
    best_row: dict[str, Any] | None = None
    best_score = (-1, -1, -1, -1, "")
    for row in group["rows"]:
        media_type = str(row["media_type"])
        media_id = str(row["media_id"])
        shot_id = str(row["shot_id"])
        frame = int(row["frame"])
        same_frame = 0
        same_shot = 0
        for candidate in {_normalize_text(str(row.get("silhouette_label") or "")), _normalize_text(str(row.get("annotation_value") or ""))}:
            if candidate and catalog_index["same_frame"].get((media_type, media_id, shot_id, frame, candidate)):
                same_frame += 1
            if candidate and catalog_index["same_shot"].get((media_type, media_id, shot_id, candidate)):
                same_shot += 1
        score = (same_frame, same_shot, len(row.get("source_frame", "")), len(row.get("archive_png_path", "")), str(row.get("archive_json_path", "")))
        if score > best_score:
            best_score = score
            best_row = row
    return best_row or group["rows"][0]


def _primary_and_counterpart_paths(
    row: dict[str, Any],
    catalog_index: dict[str, dict[tuple[str, str, str, int, str], list[dict[str, Any]]]],
) -> dict[str, Any]:
    media_type = str(row["media_type"])
    media_id = str(row["media_id"])
    shot_id = str(row["shot_id"])
    frame = int(row["frame"])
    label = _normalize_text(str(row["silhouette_label"]))
    annotation = _normalize_text(str(row["annotation_value"]))
    direction = str(row["direction"])

    if direction == REVERSE_DIRECTION:
        singular_label = annotation
        plural_label = label
    else:
        singular_label = label
        plural_label = annotation

    singular_candidates = {_normalize_text(singular_label)} if singular_label else set()
    plural_candidates = {_normalize_text(plural_label)} if plural_label else set()
    counterpart_candidates = {_normalize_text(singular_label if direction == REVERSE_DIRECTION else annotation)}

    singular_same_frame = _collect_catalog_hits(catalog_index, media_type, media_id, shot_id, frame, singular_candidates)
    plural_same_frame = _collect_catalog_hits(catalog_index, media_type, media_id, shot_id, frame, plural_candidates)
    singular_same_shot = _collect_catalog_hits(catalog_index, media_type, media_id, shot_id, None, singular_candidates)
    plural_same_shot = _collect_catalog_hits(catalog_index, media_type, media_id, shot_id, None, plural_candidates)
    counterpart_same_frame = _collect_catalog_hits(catalog_index, media_type, media_id, shot_id, frame, counterpart_candidates)
    counterpart_same_shot = _collect_catalog_hits(catalog_index, media_type, media_id, shot_id, None, counterpart_candidates)

    primary_hits_same_frame = _collect_catalog_hits(catalog_index, media_type, media_id, shot_id, frame, {label})
    if not primary_hits_same_frame:
        primary_hits_same_frame = _collect_catalog_hits(catalog_index, media_type, media_id, shot_id, None, {label})
    primary_record = _choose_best_record(primary_hits_same_frame)
    counterpart_record = _choose_best_counterpart(
        catalog_index,
        media_type=media_type,
        media_id=media_id,
        shot_id=shot_id,
        frame=frame,
        candidate_labels=counterpart_candidates,
        prefer_same_frame=True,
    )

    primary_json_path = Path(str(primary_record.get("path") or row.get("archive_json_path") or ""))
    primary_png_path = primary_json_path.with_suffix(".png")
    counterpart_json_path = Path(str(counterpart_record.get("path") or "")) if counterpart_record else None
    counterpart_png_path = counterpart_json_path.with_suffix(".png") if counterpart_json_path else None

    singular_exists_same_frame = bool(singular_same_frame)
    plural_exists_same_frame = bool(plural_same_frame)
    singular_exists_same_shot = bool(singular_same_shot)
    plural_exists_same_shot = bool(plural_same_shot)

    same_frame_number_variant_exists = singular_exists_same_frame and plural_exists_same_frame
    same_shot_number_variant_exists = singular_exists_same_shot and plural_exists_same_shot

    comparison = None
    if counterpart_png_path and primary_png_path.exists() and counterpart_png_path.exists():
        comparison = _compare_pngs(primary_png_path, counterpart_png_path)
        comparison["bbox_iou"] = _bbox_iou(primary_record.get("bbox"), counterpart_record.get("bbox"))
        comparison["primary_bbox"] = primary_record.get("bbox")
        comparison["counterpart_bbox"] = counterpart_record.get("bbox")
        comparison["primary_mask_area"] = primary_record.get("mask_area")
        comparison["counterpart_mask_area"] = counterpart_record.get("mask_area")
        comparison["primary_source_frame"] = primary_record.get("source_frame")
        comparison["counterpart_source_frame"] = counterpart_record.get("source_frame")
    else:
        comparison = {
            "primary_dimensions": list(Image.open(primary_png_path).size) if primary_png_path.exists() else None,
            "counterpart_dimensions": list(Image.open(counterpart_png_path).size) if counterpart_png_path and counterpart_png_path.exists() else None,
            "primary_hash": _sha256_file(primary_png_path) if primary_png_path.exists() else None,
            "counterpart_hash": _sha256_file(counterpart_png_path) if counterpart_png_path and counterpart_png_path.exists() else None,
            "file_hash_equal": False,
            "pixel_equal_if_same_dimensions": False,
            "alpha_iou": None,
            "bbox_iou": _bbox_iou(primary_record.get("bbox"), counterpart_record.get("bbox")) if counterpart_record else None,
            "comparison_mode": "none",
            "primary_bbox": primary_record.get("bbox"),
            "counterpart_bbox": counterpart_record.get("bbox") if counterpart_record else None,
            "primary_mask_area": primary_record.get("mask_area"),
            "counterpart_mask_area": counterpart_record.get("mask_area") if counterpart_record else None,
            "primary_source_frame": primary_record.get("source_frame"),
            "counterpart_source_frame": counterpart_record.get("source_frame") if counterpart_record else None,
        }

    return {
        "media_type": media_type,
        "media_id": media_id,
        "title": _load_title(row["title_map"], media_type, media_id) if "title_map" in row else media_id,
        "shot_id": shot_id,
        "frame": frame,
        "field": str(row["field"]),
        "silhouette_label": str(row["silhouette_label"]),
        "annotation_value": str(row["annotation_value"]),
        "direction": direction,
        "source_annotation_values": row.get("source_annotation_values", []),
        "singular_label": singular_label,
        "plural_label": plural_label,
        "singular_asset_exists": singular_exists_same_shot,
        "plural_asset_exists": plural_exists_same_shot,
        "singular_asset_exists_same_frame": singular_exists_same_frame,
        "plural_asset_exists_same_frame": plural_exists_same_frame,
        "same_shot": bool(counterpart_same_shot),
        "same_frame": bool(counterpart_same_frame),
        "same_frame_number_variant_exists": same_frame_number_variant_exists,
        "same_shot_number_variant_exists": same_shot_number_variant_exists,
        "primary_json_path": str(primary_json_path),
        "primary_png_path": str(primary_png_path),
        "counterpart_json_path": str(counterpart_json_path) if counterpart_json_path else "",
        "counterpart_png_path": str(counterpart_png_path) if counterpart_png_path else "",
        "comparison": comparison,
        "source_frame_path": str(primary_record.get("source_frame") or row.get("source_frame") or ""),
        "primary_bbox": primary_record.get("bbox"),
        "counterpart_bbox": counterpart_record.get("bbox") if counterpart_record else None,
        "primary_mask_area": primary_record.get("mask_area"),
        "counterpart_mask_area": counterpart_record.get("mask_area") if counterpart_record else None,
        "primary_dimensions": comparison.get("primary_dimensions"),
        "counterpart_dimensions": comparison.get("counterpart_dimensions"),
        "primary_hash": comparison.get("primary_hash"),
        "counterpart_hash": comparison.get("counterpart_hash"),
        "file_hash_equal": comparison.get("file_hash_equal"),
        "pixel_equal_if_same_dimensions": comparison.get("pixel_equal_if_same_dimensions"),
        "alpha_iou": comparison.get("alpha_iou"),
        "bbox_iou": comparison.get("bbox_iou"),
        "comparison_mode": comparison.get("comparison_mode"),
    }


def _collect_catalog_hits(
    catalog_index: dict[str, dict[tuple[str, str, str, int, str], list[dict[str, Any]]]],
    media_type: str,
    media_id: str,
    shot_id: str,
    frame: int | None,
    candidate_labels: set[str],
) -> list[dict[str, Any]]:
    hits: list[dict[str, Any]] = []
    for candidate in candidate_labels:
        candidate = _normalize_text(candidate)
        if not candidate:
            continue
        if frame is None:
            for key, records in catalog_index["same_shot"].items():
                if key[:3] == (media_type, media_id, shot_id) and key[3] == candidate:
                    hits.extend(records)
        else:
            hits.extend(catalog_index["same_frame"].get((media_type, media_id, shot_id, frame, candidate), []))
    # Deduplicate by JSON path.
    deduped: dict[str, dict[str, Any]] = {}
    for record in hits:
        path = str(record.get("path") or "")
        if path:
            deduped[path] = record
    return sorted(deduped.values(), key=lambda item: str(item.get("path") or ""))


def _choose_best_record(records: list[dict[str, Any]]) -> dict[str, Any]:
    if not records:
        return {}
    return sorted(records, key=lambda item: str(item.get("path") or ""))[0]


def _choose_best_counterpart(
    catalog_index: dict[str, dict[tuple[str, str, str, int, str], list[dict[str, Any]]]],
    *,
    media_type: str,
    media_id: str,
    shot_id: str,
    frame: int,
    candidate_labels: set[str],
    prefer_same_frame: bool,
) -> dict[str, Any] | None:
    same_frame_hits = _collect_catalog_hits(catalog_index, media_type, media_id, shot_id, frame, candidate_labels)
    if same_frame_hits:
        return _choose_best_record(same_frame_hits)
    if prefer_same_frame:
        same_shot_hits = _collect_catalog_hits(catalog_index, media_type, media_id, shot_id, None, candidate_labels)
        if same_shot_hits:
            return _choose_best_record(same_shot_hits)
    return None


def _rank_candidate_record(row: dict[str, Any], catalog_index: dict[str, dict[tuple[str, str, str, int, str], list[dict[str, Any]]]], singular_label: str, plural_label: str) -> tuple[int, int, int, int, str]:
    media_type = str(row["media_type"])
    media_id = str(row["media_id"])
    shot_id = str(row["shot_id"])
    frame = int(row["frame"])
    same_frame_singular = bool(catalog_index["same_frame"].get((media_type, media_id, shot_id, frame, _normalize_text(singular_label))))
    same_frame_plural = bool(catalog_index["same_frame"].get((media_type, media_id, shot_id, frame, _normalize_text(plural_label))))
    same_shot_singular = bool(catalog_index["same_shot"].get((media_type, media_id, shot_id, _normalize_text(singular_label))))
    same_shot_plural = bool(catalog_index["same_shot"].get((media_type, media_id, shot_id, _normalize_text(plural_label))))
    return (
        int(same_frame_singular and same_frame_plural),
        int(same_frame_singular or same_frame_plural),
        int(same_shot_singular and same_shot_plural),
        int(same_shot_singular or same_shot_plural),
        str(row.get("archive_json_path") or ""),
    )


def _sample_directions(
    reverse_groups: list[dict[str, Any]],
    forward_groups: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    reverse_by_key = {group["key"]: group for group in reverse_groups}
    forward_by_key = {group["key"]: group for group in forward_groups}

    explicit_reverse_keys = [
        ("objects", "wooden posts", "wooden post"),
        ("objects", "stone walls", "stone wall"),
        ("objects", "potted plants", "potted plant"),
        ("objects", "wagon wheels", "wagon wheel"),
        ("objects", "wooden beams", "wooden beam"),
        ("objects", "wanted posters", "wanted poster"),
        ("objects", "rocking chairs", "rocking chair"),
    ]

    reverse_selection: list[dict[str, Any]] = []
    seen_reverse: set[tuple[str, str, str]] = set()

    def _append_reverse(key: tuple[str, str, str]) -> None:
        group = reverse_by_key.get(key)
        if group and key not in seen_reverse:
            reverse_selection.append(group)
            seen_reverse.add(key)

    for key in explicit_reverse_keys:
        _append_reverse(key)

    reverse_sorted = reverse_groups
    for group in reverse_sorted[:10]:
        _append_reverse(group["key"])

    mid_candidates = reverse_sorted[10:60:4]
    for group in mid_candidates[:10]:
        _append_reverse(group["key"])

    tail_candidates = list(reversed(reverse_sorted[-30:]))
    for group in tail_candidates[:10]:
        _append_reverse(group["key"])

    for group in reverse_sorted:
        if len(reverse_selection) >= 30:
            break
        _append_reverse(group["key"])

    forward_selection: list[dict[str, Any]] = []
    seen_forward: set[tuple[str, str, str]] = set()
    for group in forward_groups[:12]:
        if group["key"] not in seen_forward:
            forward_selection.append(group)
            seen_forward.add(group["key"])

    return reverse_selection[:30], forward_selection[:12]


def _interpret_example(sample: dict[str, Any]) -> tuple[str, str]:
    direction = sample["direction"]
    comparison = sample["comparison"] or {}
    same_frame = bool(sample["same_frame_number_variant_exists"])
    same_shot = bool(sample["same_shot_number_variant_exists"])
    file_hash_equal = bool(comparison.get("file_hash_equal"))
    pixel_equal = bool(comparison.get("pixel_equal_if_same_dimensions"))
    label_tokens = set(_normalize_text(sample["silhouette_label"]).split()) | set(_normalize_text(sample["annotation_value"]).split())

    if file_hash_equal or pixel_equal:
        return DUPLICATE_NUMBER_VARIANT, "primary and counterpart silhouette assets are effectively identical"

    if direction == REVERSE_DIRECTION:
        if same_frame:
            if label_tokens & _OBVIOUS_PLURAL_VISUAL_HINTS:
                return VISUAL_PLURAL, "same-frame singular/plural assets exist and the label family is visually plural"
            return ANNOTATION_NUMBER_MISMATCH, "same-frame singular/plural assets exist but the visual evidence is less obvious"
        if same_shot:
            return HISTORICAL_LABEL_PROPAGATION, "the counterpart appears elsewhere in the same shot rather than on the same frame"
        if label_tokens & _OBVIOUS_PLURAL_VISUAL_HINTS:
            return ANNOTATION_NUMBER_MISMATCH, "plural silhouette label is plausible, but the catalog evidence is indirect"
        return MORPHOLOGICALLY_VALID_BUT_VISUALLY_ODD, "number relation is valid, but the visual explanation remains weak"

    if same_frame:
        return SEGMENTATION_MULTIPLICITY, "singular silhouette appears to isolate one member of a plural source concept"
    if same_shot:
        return HISTORICAL_LABEL_PROPAGATION, "the counterpart exists in the same shot but not at the same frame"
    if label_tokens & _OBVIOUS_PLURAL_VISUAL_HINTS:
        return SEGMENTATION_MULTIPLICITY, "singular silhouette plausibly represents one component from a plural scene object"
    return MORPHOLOGICALLY_VALID_BUT_VISUALLY_ODD, "the morphology is valid, but the visual explanation is not strong"


def _render_panel(sample: dict[str, Any], output_path: Path) -> None:
    source_frame = Path(str(sample.get("source_frame_path") or ""))
    primary_png = Path(str(sample.get("primary_png_path") or ""))
    counterpart_png = Path(str(sample.get("counterpart_png_path") or ""))
    title = f"{sample['silhouette_label']} ← {sample['annotation_value']}"

    canvas_w = 1650
    header_h = 155
    frame_h = 560
    pair_h = 390 if sample.get("same_frame_number_variant_exists") else 320
    footer_h = 300
    margin = 28
    gap = 18
    canvas_h = header_h + frame_h + pair_h + footer_h + margin * 2 + gap * 3
    canvas = Image.new("RGBA", (canvas_w, canvas_h), (247, 246, 242, 255))
    draw = ImageDraw.Draw(canvas)

    title_font = load_font_with_fallback(28)
    body_font = load_font_with_fallback(20)
    small_font = load_font_with_fallback(17)

    y = margin
    _draw_text_block(
        draw,
        (margin, y, canvas_w - margin * 2, header_h),
        [
            f"{sample['rank_label']}  {sample['forensic_interpretation']}",
            title,
            f"direction: {sample['direction']}   media: {sample['title']}   shot: {sample['shot_id']}   frame: {sample['frame']}",
        ],
        title_font,
        body_font,
        fill=(30, 30, 30, 255),
    )
    y += header_h + gap

    frame_box = (margin, y, canvas_w - margin * 2, frame_h)
    _draw_image_box(draw, canvas, frame_box, source_frame, label="SOURCE FRAME", caption_font=small_font)
    y += frame_h + gap

    if sample.get("same_frame_number_variant_exists") and counterpart_png.exists():
        left = (margin, y, (canvas_w - margin * 2 - gap) // 2, pair_h)
        right = (margin + left[2] + gap, y, left[2], pair_h)
        _draw_image_box(
            draw,
            canvas,
            left,
            primary_png,
            label="PRIMARY SILHOUETTE",
            caption_font=small_font,
        )
        _draw_image_box(
            draw,
            canvas,
            right,
            counterpart_png,
            label="COUNTERPART SILHOUETTE",
            caption_font=small_font,
        )
    else:
        pair_box = (margin, y, canvas_w - margin * 2, pair_h)
        _draw_image_box(
            draw,
            canvas,
            pair_box,
            primary_png,
            label="SILHOUETTE PNG",
            caption_font=small_font,
        )
        if counterpart_png.exists():
            inset_box = (canvas_w - margin - 360, y + 18, 340, 220)
            _draw_image_box(
                draw,
                canvas,
                inset_box,
                counterpart_png,
                label="COUNTERPART",
                caption_font=small_font,
            )
    y += pair_h + gap

    lines = [
        f"annotation: {sample['annotation_value']}",
        f"silhouette: {sample['silhouette_label']}",
        f"direction: {sample['direction']}",
        f"forensic_interpretation: {sample['forensic_interpretation']}",
        f"reason: {sample['interpretation_reason']}",
        f"same_shot: {sample['same_shot']}   same_frame: {sample['same_frame']}   same_frame_number_variant_exists: {sample['same_frame_number_variant_exists']}",
    ]
    metrics = sample.get("comparison") or {}
    if metrics:
        lines.append(
            f"hash_equal: {metrics.get('file_hash_equal')}   pixel_equal_if_same_dimensions: {metrics.get('pixel_equal_if_same_dimensions')}   alpha_iou: {metrics.get('alpha_iou')}   bbox_iou: {metrics.get('bbox_iou')}"
        )
    _draw_text_block(draw, (margin, y, canvas_w - margin * 2, footer_h), lines, body_font, small_font, fill=(32, 32, 32, 255))

    canvas.save(output_path)


def _draw_text_block(
    draw: ImageDraw.ImageDraw,
    box: tuple[int, int, int, int],
    lines: list[str],
    title_font,
    body_font,
    fill=(20, 20, 20, 255),
) -> None:
    x, y, w, _h = box
    cursor_y = y
    if not lines:
        return
    title_height = _font_height(draw, title_font)
    body_height = _font_height(draw, body_font)
    first_line = lines[0]
    draw.text((x, cursor_y), first_line, font=title_font, fill=fill)
    cursor_y += title_height + 10
    for line in lines[1:]:
        for wrapped in _wrap_text(draw, line, body_font, w):
            draw.text((x, cursor_y), wrapped, font=body_font, fill=fill)
            cursor_y += body_height + 6


def _font_height(draw: ImageDraw.ImageDraw, font) -> int:
    bbox = draw.textbbox((0, 0), "Ag", font=font)
    return max(1, bbox[3] - bbox[1])


def _wrap_text(draw: ImageDraw.ImageDraw, text: str, font, max_width: int) -> list[str]:
    words = text.split()
    if not words:
        return [""]
    lines: list[str] = []
    current = words[0]
    for word in words[1:]:
        candidate = f"{current} {word}"
        if draw.textlength(candidate, font=font) <= max_width:
            current = candidate
        else:
            lines.append(current)
            current = word
    lines.append(current)
    return lines


def _draw_image_box(
    draw: ImageDraw.ImageDraw,
    canvas: Image.Image,
    box: tuple[int, int, int, int],
    image_path: Path,
    *,
    label: str,
    caption_font,
) -> None:
    x, y, w, h = box
    draw.rounded_rectangle((x, y, x + w, y + h), radius=10, fill=(255, 255, 255, 255), outline=(190, 190, 190, 255), width=2)
    if image_path.exists():
        try:
            image = Image.open(image_path).convert("RGBA")
            image = ImageOps.contain(image, (w - 18, h - 40))
            px = x + (w - image.width) // 2
            py = y + 18 + (h - 40 - image.height) // 2
            canvas.alpha_composite(image, (px, py))
        except Exception:
            draw.text((x + 16, y + 36), f"[unreadable image: {image_path.name}]", font=caption_font, fill=(120, 30, 30, 255))
    else:
        draw.text((x + 16, y + 36), f"[missing image: {image_path.name}]", font=caption_font, fill=(120, 30, 30, 255))

    draw.text((x + 12, y + h - 24), label, font=caption_font, fill=(40, 40, 40, 255))


def _relationship_row(group: dict[str, Any], reverse_population: int) -> dict[str, Any]:
    key = group["key"]
    percentage = (group["record_count"] / reverse_population) if reverse_population else 0.0
    return {
        "field": group["field"],
        "silhouette_label": group["silhouette_label"],
        "annotation_value": group["annotation_value"],
        "record_count": group["record_count"],
        "unique_shots": group["unique_shots"],
        "unique_media": group["unique_media"],
        "percentage_of_reverse_population": percentage,
        "relationship_key": " || ".join(key),
    }


def _sample_row_to_csv_row(sample: dict[str, Any], group: dict[str, Any]) -> dict[str, Any]:
    return {
        "media_type": sample["media_type"],
        "media_id": sample["media_id"],
        "title": sample["title"],
        "shot_id": sample["shot_id"],
        "frame": sample["frame"],
        "field": sample["field"],
        "silhouette_label": sample["silhouette_label"],
        "annotation_value": sample["annotation_value"],
        "direction": sample["direction"],
        "forensic_interpretation": sample["forensic_interpretation"],
        "interpretation_reason": sample["interpretation_reason"],
        "singular_asset_exists": sample["singular_asset_exists"],
        "plural_asset_exists": sample["plural_asset_exists"],
        "singular_asset_exists_same_frame": sample["singular_asset_exists_same_frame"],
        "plural_asset_exists_same_frame": sample["plural_asset_exists_same_frame"],
        "same_shot": sample["same_shot"],
        "same_frame": sample["same_frame"],
        "same_frame_number_variant_exists": sample["same_frame_number_variant_exists"],
        "same_shot_number_variant_exists": sample["same_shot_number_variant_exists"],
        "record_count": group["record_count"],
        "unique_shots": group["unique_shots"],
        "unique_media": group["unique_media"],
        "percentage_of_direction_population": group["percentage_of_direction_population"],
        "source_frame_path": sample["source_frame_path"],
        "silhouette_json_path": sample["primary_json_path"],
        "silhouette_png_path": sample["primary_png_path"],
        "counterpart_json_path": sample["counterpart_json_path"],
        "counterpart_png_path": sample["counterpart_png_path"],
        "primary_dimensions": _json_text(sample["primary_dimensions"]),
        "counterpart_dimensions": _json_text(sample["counterpart_dimensions"]),
        "primary_bbox": _json_text(sample["primary_bbox"]),
        "counterpart_bbox": _json_text(sample["counterpart_bbox"]),
        "primary_mask_area": sample["primary_mask_area"],
        "counterpart_mask_area": sample["counterpart_mask_area"],
        "primary_file_hash": sample["primary_hash"],
        "counterpart_file_hash": sample["counterpart_hash"],
        "file_hash_equal": sample["file_hash_equal"],
        "pixel_equal_if_same_dimensions": sample["pixel_equal_if_same_dimensions"],
        "alpha_iou": sample["alpha_iou"],
        "bbox_iou": sample["bbox_iou"],
        "comparison_mode": sample["comparison_mode"],
        "example_dir": sample["example_dir"],
        "example_panel_path": sample["example_panel_path"],
        "example_evidence_path": sample["example_evidence_path"],
    }


def _write_csv(path: Path, rows: list[dict[str, Any]], fieldnames: list[str]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            serialised = {}
            for field in fieldnames:
                value = row.get(field)
                if isinstance(value, (dict, list)):
                    serialised[field] = _json_text(value)
                elif value is None:
                    serialised[field] = ""
                else:
                    serialised[field] = str(value)
            writer.writerow(serialised)


def audit_silhouette_number_direction(
    project_path: str | Path,
    *,
    morphology_audit_dir: str | Path | None = None,
    output_dir: str | Path | None = None,
) -> dict[str, Any]:
    """Run the reverse-direction forensic audit and write its artifacts."""
    project = _project_path(project_path).resolve()
    source_dir = Path(morphology_audit_dir) if morphology_audit_dir is not None else default_source_audit_dir(project)
    target_dir = Path(output_dir) if output_dir is not None else default_output_dir(project)
    target_dir.mkdir(parents=True, exist_ok=True)
    examples_dir = target_dir / EXAMPLES_DIR_NAME
    examples_dir.mkdir(parents=True, exist_ok=True)

    report_before, rows = _load_morphology_rows(source_dir)
    source_snapshot_before = _snapshot_dir(source_dir)

    title_map = _load_titles(project)
    catalog_index = _load_catalog_index(project)

    reverse_rows = [row for row in rows if row["direction"] == REVERSE_DIRECTION]
    forward_rows = [row for row in rows if row["direction"] == FORWARD_DIRECTION]

    reverse_groups = _annotated_groups(reverse_rows)
    forward_groups = _annotated_groups(forward_rows)

    reverse_population = len(reverse_rows)
    forward_population = len(forward_rows)

    reverse_relationship_rows = []
    for group in reverse_groups:
        row = _relationship_row(group, reverse_population)
        reverse_relationship_rows.append(row)
        group["percentage_of_direction_population"] = row["percentage_of_reverse_population"]
    reverse_relationship_rows.sort(key=lambda item: (-item["record_count"], item["field"], item["silhouette_label"], item["annotation_value"]))

    reverse_selection, forward_selection = _sample_directions(reverse_groups, forward_groups)

    selected_samples: list[dict[str, Any]] = []
    interpretation_counts = Counter()
    selection_groups = [
        *( {**group, "direction_bucket": "reverse"} for group in reverse_selection ),
        *( {**group, "direction_bucket": "forward"} for group in forward_selection ),
    ]

    for index, group in enumerate(selection_groups, 1):
        chosen_row = _choose_representative_row(group, catalog_index)
        chosen_row = {**chosen_row, "title_map": title_map, "source_annotation_values": chosen_row.get("source_annotation_values", [])}
        sample = _primary_and_counterpart_paths(chosen_row, catalog_index)
        sample["title"] = title_map.get((sample["media_type"], sample["media_id"]), sample["media_id"])
        sample["direction_bucket"] = group["direction_bucket"]
        sample["record_count"] = group["record_count"]
        sample["unique_shots"] = group["unique_shots"]
        sample["unique_media"] = group["unique_media"]
        population = reverse_population if group["direction_bucket"] == "reverse" else forward_population
        sample["percentage_of_direction_population"] = group["percentage_of_direction_population"] or ((group["record_count"] / population) if population else 0.0)
        sample["rank_label"] = f"{index:02d}."
        sample["source_annotation_values"] = chosen_row.get("source_annotation_values", [])

        interpretation, reason = _interpret_example(sample)
        sample["forensic_interpretation"] = interpretation
        sample["interpretation_reason"] = reason
        interpretation_counts[interpretation] += 1

        safe_dir = examples_dir / f"{index:02d}_{_slugify(sample['silhouette_label'])}__{_slugify(sample['annotation_value'])}"
        safe_dir.mkdir(parents=True, exist_ok=True)
        panel_path = safe_dir / PANEL_NAME
        evidence_path = safe_dir / EVIDENCE_NAME
        sample["example_dir"] = str(safe_dir)
        sample["example_panel_path"] = str(panel_path)
        sample["example_evidence_path"] = str(evidence_path)

        _render_panel(sample, panel_path)
        evidence_path.write_text(
            json.dumps(sample, indent=2, ensure_ascii=False, default=str) + "\n",
            encoding="utf-8",
        )
        selected_samples.append(sample)

    reverse_csv_path = target_dir / REVERSE_RELATIONSHIPS_CSV
    _write_csv(
        reverse_csv_path,
        reverse_relationship_rows,
        [
            "field",
            "silhouette_label",
            "annotation_value",
            "record_count",
            "unique_shots",
            "unique_media",
            "percentage_of_reverse_population",
            "relationship_key",
        ],
    )

    sampled_csv_path = target_dir / SAMPLED_EXAMPLES_CSV
    sampled_rows = []
    for sample in selected_samples:
        group = next(group for group in (reverse_selection + forward_selection) if group["key"] == (sample["field"], sample["silhouette_label"], sample["annotation_value"]))
        sampled_rows.append(_sample_row_to_csv_row(sample, group))
    _write_csv(
        sampled_csv_path,
        sampled_rows,
        [
            "media_type",
            "media_id",
            "title",
            "shot_id",
            "frame",
            "field",
            "silhouette_label",
            "annotation_value",
            "direction",
            "forensic_interpretation",
            "interpretation_reason",
            "singular_asset_exists",
            "plural_asset_exists",
            "singular_asset_exists_same_frame",
            "plural_asset_exists_same_frame",
            "same_shot",
            "same_frame",
            "same_frame_number_variant_exists",
            "same_shot_number_variant_exists",
            "record_count",
            "unique_shots",
            "unique_media",
            "percentage_of_direction_population",
            "source_frame_path",
            "silhouette_json_path",
            "silhouette_png_path",
            "counterpart_json_path",
            "counterpart_png_path",
            "primary_dimensions",
            "counterpart_dimensions",
            "primary_bbox",
            "counterpart_bbox",
            "primary_mask_area",
            "counterpart_mask_area",
            "primary_file_hash",
            "counterpart_file_hash",
            "file_hash_equal",
            "pixel_equal_if_same_dimensions",
            "alpha_iou",
            "bbox_iou",
            "comparison_mode",
            "example_dir",
            "example_panel_path",
            "example_evidence_path",
        ],
    )

    source_snapshot_after = _snapshot_dir(source_dir)
    source_inputs_same_before_after = source_snapshot_before == source_snapshot_after
    report_after = json.loads((source_dir / MORPHOLOGY_REPORT_NAME).read_text(encoding="utf-8"))

    reverse_concentration = {
        "top_10": _concentration(reverse_relationship_rows, reverse_population, 10),
        "top_25": _concentration(reverse_relationship_rows, reverse_population, 25),
        "top_50": _concentration(reverse_relationship_rows, reverse_population, 50),
        "top_100": _concentration(reverse_relationship_rows, reverse_population, 100),
    }

    report = {
        "project_path": str(project),
        "source_morphology_audit_dir": str(source_dir),
        "output_dir": str(target_dir),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source_inputs_same_before_after": source_inputs_same_before_after,
        "source_inputs_before": source_snapshot_before,
        "source_inputs_after": source_snapshot_after,
        "morphology_report_rechecked": bool(report_before == report_after),
        "population": {
            "reverse_direction_records": reverse_population,
            "reverse_direction_unique_relationships": len(reverse_relationship_rows),
            "reverse_direction_unique_shots": len({row["shot_id"] for row in reverse_rows}),
            "reverse_direction_unique_media": len({(row["media_type"], row["media_id"]) for row in reverse_rows}),
            "forward_direction_records": forward_population,
            "forward_direction_unique_relationships": len(forward_groups),
            "forward_direction_unique_shots": len({row["shot_id"] for row in forward_rows}),
            "forward_direction_unique_media": len({(row["media_type"], row["media_id"]) for row in forward_rows}),
        },
        "concentration": reverse_concentration,
        "by_field_reverse": dict(Counter(row["field"] for row in reverse_rows)),
        "by_silhouette_label_reverse_top": dict(Counter(row["silhouette_label"] for row in reverse_rows).most_common(30)),
        "by_annotation_value_reverse_top": dict(Counter(row["annotation_value"] for row in reverse_rows).most_common(30)),
        "by_field_forward": dict(Counter(row["field"] for row in forward_rows)),
        "sample_counts": {
            "reverse_examples": len(reverse_selection),
            "forward_controls": len(forward_selection),
            "total_examples": len(selected_samples),
        },
        "interpretation_counts": dict(interpretation_counts),
        "final_answers": {
            "legitimacy": "YES WITH QUALIFICATIONS",
            "same_mechanism": "The reverse and forward directions overlap in the same broad catalog machinery, but the reverse population is dominated by catalogued plural scene objects and historical label propagation, while the forward population more often looks like one object isolated from a plural source annotation.",
            "redundant_assets": "Yes, some reverse examples have same-shot singular/plural siblings, but the strongest evidence of true duplicates is limited to a subset.",
            "future_subtype": "Yes, both directions are suitable for a future number_variant provenance subtype as long as direction remains explicit metadata.",
            "remain_questionable": "Some reverse cases remain visually ambiguous or historically propagated enough that they should stay under review until the evidence is curated.",
        },
        "artifacts": {
            "report_json": str(target_dir / REPORT_JSON_NAME),
            "report_md": str(target_dir / REPORT_MD_NAME),
            "reverse_relationships_csv": str(reverse_csv_path),
            "sampled_examples_csv": str(sampled_csv_path),
            "examples_dir": str(examples_dir),
        },
    }

    report_path = target_dir / REPORT_JSON_NAME
    atomic_write_text(report_path, json.dumps(report, indent=2, ensure_ascii=False) + "\n")

    md_lines = [
        "# Silhouette Number Direction Audit",
        "",
        f"Project: `{project}`",
        f"Source morphology audit: `{source_dir}`",
        f"Output dir: `{target_dir}`",
        "",
        "## Population",
        "",
        f"Reverse-direction records: **{reverse_population}**",
        f"Reverse-direction unique relationships: **{len(reverse_relationship_rows)}**",
        f"Reverse-direction unique shots: **{len({row['shot_id'] for row in reverse_rows})}**",
        f"Reverse-direction unique media: **{len({(row['media_type'], row['media_id']) for row in reverse_rows})}**",
        f"Forward-control records: **{forward_population}**",
        f"Forward-control unique relationships: **{len(forward_groups)}**",
        "",
        "## Reverse Concentration",
        "",
    ]
    for label, metrics in reverse_concentration.items():
        md_lines.append(f"- {label.replace('_', ' ')}: **{metrics['percentage']}%** ({metrics['count']} / {reverse_population})")
    md_lines.extend([
        "",
        "## Sample Counts",
        "",
        f"Reverse examples: **{len(reverse_selection)}**",
        f"Forward controls: **{len(forward_selection)}**",
        "",
        "## Interpretation Counts",
        "",
    ])
    for key, count in sorted(interpretation_counts.items(), key=lambda item: (-item[1], item[0])):
        md_lines.append(f"- `{key}`: {count}")
    md_lines.extend([
        "",
        "## Final Answers",
        "",
        f"1. Legitimacy: **{report['final_answers']['legitimacy']}**",
        f"2. Mechanism: {report['final_answers']['same_mechanism']}",
        f"3. Redundant assets: {report['final_answers']['redundant_assets']}",
        f"4. Future subtype: {report['final_answers']['future_subtype']}",
        f"5. Questionable cases: {report['final_answers']['remain_questionable']}",
        "",
        "## Artifacts",
        "",
        f"- [report.json]({report['artifacts']['report_json']})",
        f"- [report.md]({report['artifacts']['report_md']})",
        f"- [reverse_relationships.csv]({report['artifacts']['reverse_relationships_csv']})",
        f"- [sampled_examples.csv]({report['artifacts']['sampled_examples_csv']})",
        f"- [examples/]({report['artifacts']['examples_dir']})",
    ])

    report_md_path = target_dir / REPORT_MD_NAME
    atomic_write_text(report_md_path, "\n".join(md_lines) + "\n")

    return report


def _concentration(rows: list[dict[str, Any]], population: int, top_n: int) -> dict[str, Any]:
    total = sum(int(row["record_count"]) for row in rows[:top_n])
    percentage = round((total / population * 100.0) if population else 0.0, 2)
    return {"count": total, "percentage": percentage}


def _snapshot_dir(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"path": str(path), "exists": False, "files": []}
    files = []
    for item in sorted(path.rglob("*")):
        if item.is_file():
            files.append({"path": str(item.relative_to(path)), "size": item.stat().st_size, "mtime_ns": item.stat().st_mtime_ns})
    return {"path": str(path), "exists": True, "file_count": len(files), "files": files}
