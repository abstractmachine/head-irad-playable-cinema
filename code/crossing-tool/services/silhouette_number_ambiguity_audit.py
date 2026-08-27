"""Read-only forensic audit of semantic silhouette-number ambiguities.

This audit consumes the completed number-preference audit and selects only its
``NUMBER_SEMANTICALLY_AMBIGUOUS`` rows. It never reclassifies morphology and
never writes canonical project data; all generated artifacts stay under
``outputs/tests/silhouette-number-ambiguity-audit/``.
"""

from __future__ import annotations

import csv
import io
import json
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageOps

from data.annotate import atomic_write_text
from data.metadata import load_json_metadata
from generators._common import load_font_with_fallback
from services.silhouette_number_audit import _snapshot_tree
from services.silhouette_number_morphology_audit import (
    NUMBER_SEMANTICALLY_AMBIGUOUS,
    _normalize_text,
)


PREFERENCE_AUDIT_DIR = Path("outputs") / "tests" / "silhouette-number-preference-audit"
AMBIGUITY_AUDIT_DIR = Path("outputs") / "tests" / "silhouette-number-ambiguity-audit"

PREFERENCE_REPORT_NAME = "report.json"
PREFERENCE_RELATIONSHIPS_NAME = "relationships.csv"

REPORT_JSON_NAME = "report.json"
REPORT_MD_NAME = "report.md"
RELATIONSHIPS_CSV_NAME = "relationships.csv"
FAMILIES_CSV_NAME = "families.csv"
SAMPLED_EXAMPLES_CSV_NAME = "sampled_examples.csv"
EXAMPLES_DIR_NAME = "examples"

PLURALIA_TANTUM = "PLURALIA_TANTUM"
LEXICAL_NUMBER_ASYMMETRY = "LEXICAL_NUMBER_ASYMMETRY"
INVARIANT_NUMBER = "INVARIANT_NUMBER"
COLLECTIVE_OR_MASS_NOUN = "COLLECTIVE_OR_MASS_NOUN"
INFLECT_LIMITATION = "INFLECT_LIMITATION"
ANNOTATION_LEXICAL_ISSUE = "ANNOTATION_LEXICAL_ISSUE"
HISTORICAL_LABEL_ARTIFACT = "HISTORICAL_LABEL_ARTIFACT"
GENUINELY_AMBIGUOUS = "GENUINELY_AMBIGUOUS"
OTHER = "OTHER"

FORENSIC_CATEGORIES = (
    PLURALIA_TANTUM,
    LEXICAL_NUMBER_ASYMMETRY,
    INVARIANT_NUMBER,
    COLLECTIVE_OR_MASS_NOUN,
    INFLECT_LIMITATION,
    ANNOTATION_LEXICAL_ISSUE,
    HISTORICAL_LABEL_ARTIFACT,
    GENUINELY_AMBIGUOUS,
    OTHER,
)

RECOMMEND_CANDIDATE = "CANDIDATE_ACCEPT_VALID_NON_PREFERRED"
RECOMMEND_RETAIN = "RETAIN_AMBIGUOUS"
RECOMMEND_REVIEW = "REVIEW_BEFORE_POLICY"


def _relationship_key(field: str, annotation_value: str, silhouette_label: str) -> tuple[str, str, str]:
    """Return the exact normalized key used for documented lexical policies."""
    return (
        _normalize_text(field),
        _normalize_text(annotation_value),
        _normalize_text(silhouette_label),
    )


# These are the only documented lexical relationships found in the completed
# archive inventory. The table deliberately does not match stems or near forms.
_DOCUMENTED_RELATIONSHIPS: dict[tuple[str, str, str], dict[str, str]] = {
    _relationship_key("objects", "pants", "pant"): {
        "family": "pants / pant",
        "forensic_category": PLURALIA_TANTUM,
        "interpretation_reason": "Pants conventionally names one garment as a plural-form lexical item; pant is not the preferred ordinary object label.",
        "recommendation": RECOMMEND_CANDIDATE,
    },
    _relationship_key("objects", "scissors", "scissor"): {
        "family": "scissors / scissor",
        "forensic_category": PLURALIA_TANTUM,
        "interpretation_reason": "Scissors conventionally names one cutting instrument as a plural-form lexical item; scissor is not the preferred ordinary object label.",
        "recommendation": RECOMMEND_CANDIDATE,
    },
    _relationship_key("objects", "shot glass", "shot glasses"): {
        "family": "shot glass / shot glasses",
        "forensic_category": INFLECT_LIMITATION,
        "interpretation_reason": "The compound head is an ordinary count noun; the bare glass exception leaks into an otherwise regular compound number relation.",
        "recommendation": RECOMMEND_CANDIDATE,
    },
    _relationship_key("objects", "shot glasses", "shot glass"): {
        "family": "shot glass / shot glasses",
        "forensic_category": INFLECT_LIMITATION,
        "interpretation_reason": "The compound head is an ordinary count noun; the bare glass exception leaks into an otherwise regular compound number relation.",
        "recommendation": RECOMMEND_CANDIDATE,
    },
    _relationship_key("objects", "wine glass", "wine glasses"): {
        "family": "wine glass / wine glasses",
        "forensic_category": INFLECT_LIMITATION,
        "interpretation_reason": "The compound head is an ordinary count noun; the bare glass exception leaks into an otherwise regular compound number relation.",
        "recommendation": RECOMMEND_CANDIDATE,
    },
    _relationship_key("objects", "glasses", "glass"): {
        "family": "glass / glasses",
        "forensic_category": LEXICAL_NUMBER_ASYMMETRY,
        "interpretation_reason": "Unqualified glass and glasses can denote distinct concepts such as material, vessels, glassware, or eyewear; morphology alone cannot identify the intended sense.",
        "recommendation": RECOMMEND_RETAIN,
    },
    _relationship_key("objects", "glass", "glasses"): {
        "family": "glass / glasses",
        "forensic_category": LEXICAL_NUMBER_ASYMMETRY,
        "interpretation_reason": "Unqualified glass and glasses can denote distinct concepts such as material, vessels, glassware, or eyewear; morphology alone cannot identify the intended sense.",
        "recommendation": RECOMMEND_RETAIN,
    },
}


def _project_path(project_path: str | Path) -> Path:
    return Path(project_path)


def default_preference_audit_dir(project_path: str | Path) -> Path:
    return _project_path(project_path) / PREFERENCE_AUDIT_DIR


def default_output_dir(project_path: str | Path) -> Path:
    return _project_path(project_path) / AMBIGUITY_AUDIT_DIR


def _json_text(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _parse_json(raw: str | None, default: Any) -> Any:
    text = (raw or "").strip()
    if not text:
        return default
    try:
        return json.loads(text)
    except Exception:
        return default


def _slugify(value: str) -> str:
    return "-".join(_normalize_text(value).split()) or "example"


def _load_source_rows(source_dir: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    report_path = source_dir / PREFERENCE_REPORT_NAME
    relationships_path = source_dir / PREFERENCE_RELATIONSHIPS_NAME
    if not report_path.exists():
        raise FileNotFoundError(f"Missing preference audit report: {report_path}")
    if not relationships_path.exists():
        raise FileNotFoundError(f"Missing preference audit relationships: {relationships_path}")

    report = json.loads(report_path.read_text(encoding="utf-8"))
    selected_rows: list[dict[str, Any]] = []
    with relationships_path.open("r", encoding="utf-8", newline="") as handle:
        for raw_row in csv.DictReader(handle):
            # The completed preference audit is the sole selector. No label,
            # token, or semantic lookup can add a row to this population.
            if raw_row.get("relationship") != NUMBER_SEMANTICALLY_AMBIGUOUS:
                continue
            row = dict(raw_row)
            row["frame"] = int(row.get("frame") or 0)
            row["morphology_result"] = _parse_json(row.get("morphology_result"), {})
            row["annotation_values"] = _parse_json(row.get("annotation_values"), [])
            selected_rows.append(row)

    expected_count = (
        ((report.get("counts") or {}).get("relationship") or {}).get(NUMBER_SEMANTICALLY_AMBIGUOUS)
    )
    if expected_count is not None and int(expected_count) != len(selected_rows):
        raise ValueError(
            "Preference audit count mismatch for NUMBER_SEMANTICALLY_AMBIGUOUS: "
            f"report={expected_count}, relationships={len(selected_rows)}"
        )
    return report, selected_rows


def _policy_for(row: dict[str, Any]) -> dict[str, str]:
    key = _relationship_key(
        str(row.get("field") or ""),
        str(row.get("annotation_value") or ""),
        str(row.get("silhouette_label") or ""),
    )
    policy = _DOCUMENTED_RELATIONSHIPS.get(key)
    if policy is not None:
        return policy
    field = _normalize_text(str(row.get("field") or "")) or "unclassified"
    annotation = _normalize_text(str(row.get("annotation_value") or ""))
    silhouette = _normalize_text(str(row.get("silhouette_label") or ""))
    return {
        "family": f"{field}: {annotation} / {silhouette}".strip(" /"),
        "forensic_category": OTHER,
        "interpretation_reason": "No exact documented lexical relationship covers this row; no fuzzy or cross-value policy was applied.",
        "recommendation": RECOMMEND_REVIEW,
    }


def _enrich_row(row: dict[str, Any]) -> dict[str, Any]:
    enriched = dict(row)
    enriched.update(_policy_for(row))
    morphology = enriched.get("morphology_result") or {}
    enriched["inflect_result"] = _json_text(morphology)
    enriched["ambiguity_reason"] = str(morphology.get("reason") or "")
    return enriched


def _group_relationships(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[tuple[str, str, str], dict[str, Any]] = {}
    for row in rows:
        key = _relationship_key(
            str(row.get("field") or ""),
            str(row.get("annotation_value") or ""),
            str(row.get("silhouette_label") or ""),
        )
        group = groups.setdefault(
            key,
            {
                "field": str(row.get("field") or ""),
                "annotation_value": str(row.get("annotation_value") or ""),
                "silhouette_label": str(row.get("silhouette_label") or ""),
                "record_count": 0,
                "shots": set(),
                "media": set(),
                "media_types": set(),
                "directions": Counter(),
                "inflect_results": set(),
                "ambiguity_reasons": set(),
                "family": str(row["family"]),
                "forensic_category": str(row["forensic_category"]),
                "interpretation_reason": str(row["interpretation_reason"]),
                "recommendation": str(row["recommendation"]),
                "rows": [],
            },
        )
        group["record_count"] += 1
        group["shots"].add((str(row.get("media_type") or ""), str(row.get("shot_id") or "")))
        group["media"].add((str(row.get("media_type") or ""), str(row.get("media_id") or "")))
        group["media_types"].add(str(row.get("media_type") or ""))
        group["directions"][str(row.get("direction") or "")] += 1
        group["inflect_results"].add(str(row["inflect_result"]))
        group["ambiguity_reasons"].add(str(row["ambiguity_reason"]))
        group["rows"].append(row)

    result = list(groups.values())
    for group in result:
        group["rows"].sort(
            key=lambda row: (
                str(row.get("media_type") or ""),
                str(row.get("media_id") or ""),
                str(row.get("shot_id") or ""),
                int(row.get("frame") or 0),
                str(row.get("archive_json_path") or ""),
            )
        )
        group["unique_shots"] = len(group["shots"])
        group["unique_media"] = len(group["media"])
    result.sort(
        key=lambda group: (
            -int(group["record_count"]),
            str(group["field"]).casefold(),
            str(group["annotation_value"]).casefold(),
            str(group["silhouette_label"]).casefold(),
        )
    )
    return result


def _group_families(groups: list[dict[str, Any]]) -> list[dict[str, Any]]:
    families: dict[str, dict[str, Any]] = {}
    for group in groups:
        family_name = str(group["family"])
        family = families.setdefault(
            family_name,
            {
                "family": family_name,
                "annotation_forms": set(),
                "silhouette_forms": set(),
                "record_count": 0,
                "shots": set(),
                "fields": set(),
                "forensic_category": str(group["forensic_category"]),
                "recommendation": str(group["recommendation"]),
                "ambiguity_reasons": set(),
                "relationships": [],
            },
        )
        family["annotation_forms"].add(str(group["annotation_value"]))
        family["silhouette_forms"].add(str(group["silhouette_label"]))
        family["record_count"] += int(group["record_count"])
        family["shots"].update(group["shots"])
        family["fields"].add(str(group["field"]))
        family["ambiguity_reasons"].update(group["ambiguity_reasons"])
        family["relationships"].append(group)

    result = list(families.values())
    for family in result:
        family["unique_shots"] = len(family["shots"])
    result.sort(key=lambda family: (-int(family["record_count"]), str(family["family"]).casefold()))
    return result


def _concentration(groups: list[dict[str, Any]], total: int) -> dict[str, dict[str, float | int]]:
    summary: dict[str, dict[str, float | int]] = {}
    for limit in (10, 25, 50, 100):
        records = sum(int(group["record_count"]) for group in groups[:limit])
        summary[f"top_{limit}"] = {
            "relationships_included": min(limit, len(groups)),
            "record_count": records,
            "share": (records / total) if total else 0.0,
        }
    return summary


def _evenly_spaced(rows: list[dict[str, Any]], count: int) -> list[dict[str, Any]]:
    if count <= 0 or not rows:
        return []
    if len(rows) <= count:
        return list(rows)
    indices = {
        round(index * (len(rows) - 1) / (count - 1)) if count > 1 else 0
        for index in range(count)
    }
    return [rows[index] for index in sorted(indices)]


def _select_samples(groups: list[dict[str, Any]], target_count: int = 25) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    """Cover every exact relationship, then spread remaining samples over the largest one."""
    selected: list[tuple[dict[str, Any], dict[str, Any]]] = []
    seen_paths: set[str] = set()

    def add(group: dict[str, Any], row: dict[str, Any]) -> None:
        path = str(row.get("archive_json_path") or "")
        if path and path not in seen_paths:
            selected.append((group, row))
            seen_paths.add(path)

    for group in groups:
        if group["rows"]:
            add(group, group["rows"][0])

    if not groups or len(selected) >= target_count:
        return selected[:target_count]

    primary_group = groups[0]
    primary_rows: list[dict[str, Any]] = []
    seen_shots: set[tuple[str, str]] = set()
    for row in primary_group["rows"]:
        shot_key = (str(row.get("media_type") or ""), str(row.get("shot_id") or ""))
        path = str(row.get("archive_json_path") or "")
        if shot_key not in seen_shots and path not in seen_paths:
            primary_rows.append(row)
            seen_shots.add(shot_key)

    for row in _evenly_spaced(primary_rows, target_count - len(selected)):
        add(primary_group, row)
        if len(selected) >= target_count:
            break
    return selected[:target_count]


def _load_titles(project: Path) -> dict[tuple[str, str], str]:
    titles: dict[tuple[str, str], str] = {}
    for media_type in ("movie", "gameplay"):
        for record in load_json_metadata(str(project), media_type):
            media_id = str(record.get("media_id") or "")
            if media_id:
                titles[(media_type, media_id)] = str(
                    record.get("title") or record.get("filename") or media_id
                )
    return titles


def _read_catalog_metadata(path: str) -> dict[str, Any]:
    if not path:
        return {}
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return {}
    return value if isinstance(value, dict) else {}


def _sample_evidence(
    row: dict[str, Any],
    group: dict[str, Any],
    title_map: dict[tuple[str, str], str],
) -> dict[str, Any]:
    metadata = _read_catalog_metadata(str(row.get("archive_json_path") or ""))
    media_type = str(row.get("media_type") or "")
    media_id = str(row.get("media_id") or "")
    return {
        "media_type": media_type,
        "media_id": media_id,
        "title": title_map.get((media_type, media_id), str(metadata.get("filename") or media_id)),
        "shot_id": str(row.get("shot_id") or ""),
        "frame": int(row.get("frame") or 0),
        "field": str(row.get("field") or ""),
        "annotation_value": str(row.get("annotation_value") or ""),
        "silhouette_label": str(row.get("silhouette_label") or ""),
        "forensic_category": str(row["forensic_category"]),
        "interpretation_reason": str(row["interpretation_reason"]),
        "recommendation": str(row["recommendation"]),
        "number_preference_result": str(row.get("relationship") or ""),
        "inflect_diagnostics": row.get("morphology_result") or {},
        "existing_search_provenance": metadata.get("search_provenance", {}),
        "silhouette_json_path": str(row.get("archive_json_path") or ""),
        "silhouette_png_path": str(row.get("archive_png_path") or metadata.get("png") or ""),
        "source_frame_path": str(metadata.get("source_frame") or ""),
        "relationship_record_count": int(group["record_count"]),
        "relationship_unique_shots": int(group["unique_shots"]),
        "relationship_unique_media": int(group["unique_media"]),
    }


def _open_image(path: Path) -> Image.Image | None:
    if not path.exists():
        return None
    try:
        with Image.open(path) as image:
            return image.convert("RGBA")
    except Exception:
        return None


def _draw_image_box(
    canvas: Image.Image,
    draw: ImageDraw.ImageDraw,
    box: tuple[int, int, int, int],
    image_path: Path,
    label: str,
    font,
) -> None:
    x, y, width, height = box
    draw.rectangle((x, y, x + width, y + height), fill=(255, 255, 255, 255), outline=(80, 80, 80, 255), width=2)
    image = _open_image(image_path)
    if image is None:
        draw.text((x + 16, y + 16), f"[missing or unreadable: {image_path.name}]", font=font, fill=(130, 40, 40, 255))
    else:
        contained = ImageOps.contain(image, (width - 32, height - 64))
        canvas.alpha_composite(contained, (x + (width - contained.width) // 2, y + 20 + (height - 64 - contained.height) // 2))
    draw.text((x + 12, y + height - 30), label, font=font, fill=(30, 30, 30, 255))


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


def _render_panel(sample: dict[str, Any], output_path: Path) -> None:
    canvas = Image.new("RGBA", (1600, 1280), (245, 244, 239, 255))
    draw = ImageDraw.Draw(canvas)
    title_font = load_font_with_fallback(30)
    body_font = load_font_with_fallback(20)
    caption_font = load_font_with_fallback(17)
    margin = 32
    gap = 24

    draw.text((margin, margin), f"{sample['forensic_category']}: {sample['silhouette_label']} <- {sample['annotation_value']}", font=title_font, fill=(24, 24, 24, 255))
    draw.text((margin, margin + 48), f"{sample['title']} | {sample['shot_id']} | frame {sample['frame']} | field {sample['field']}", font=body_font, fill=(42, 42, 42, 255))

    image_y = 130
    image_height = 720
    image_width = (1600 - margin * 2 - gap) // 2
    _draw_image_box(
        canvas,
        draw,
        (margin, image_y, image_width, image_height),
        Path(str(sample["source_frame_path"])),
        "SOURCE FRAME",
        caption_font,
    )
    _draw_image_box(
        canvas,
        draw,
        (margin + image_width + gap, image_y, image_width, image_height),
        Path(str(sample["silhouette_png_path"])),
        "SILHOUETTE",
        caption_font,
    )

    inflect = sample.get("inflect_diagnostics") or {}
    lines = [
        f"ANNOTATION VALUE: {sample['annotation_value']}",
        f"SILHOUETTE LABEL: {sample['silhouette_label']}",
        f"INFLECT RESULT: {inflect.get('classification', '')}; {inflect.get('reason', '')}",
        f"AMBIGUITY CATEGORY: {sample['forensic_category']}",
        f"INTERPRETATION: {sample['interpretation_reason']}",
        f"PREFERENCE AUDIT: {sample['number_preference_result']}; recommendation: {sample['recommendation']}",
    ]
    cursor_y = image_y + image_height + 34
    for line in lines:
        for wrapped in _wrap_text(draw, line, body_font, 1600 - margin * 2):
            draw.text((margin, cursor_y), wrapped, font=body_font, fill=(30, 30, 30, 255))
            cursor_y += 30
    canvas.save(output_path)


def _csv_text(rows: list[dict[str, Any]], fieldnames: list[str]) -> str:
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=fieldnames)
    writer.writeheader()
    for row in rows:
        serialised: dict[str, str] = {}
        for field in fieldnames:
            value = row.get(field)
            if isinstance(value, (dict, list, tuple, set)):
                serialised[field] = _json_text(sorted(value) if isinstance(value, set) else value)
            elif value is None:
                serialised[field] = ""
            else:
                serialised[field] = str(value)
        writer.writerow(serialised)
    return output.getvalue()


def _relationship_csv_rows(groups: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result = []
    for group in groups:
        result.append(
            {
                "media_type": ", ".join(sorted(value for value in group["media_types"] if value)),
                "field": group["field"],
                "annotation_value": group["annotation_value"],
                "silhouette_label": group["silhouette_label"],
                "record_count": group["record_count"],
                "unique_shots": group["unique_shots"],
                "unique_media": group["unique_media"],
                "inflect_result": sorted(group["inflect_results"]),
                "ambiguity_reason": sorted(group["ambiguity_reasons"]),
                "forensic_category": group["forensic_category"],
                "interpretation_reason": group["interpretation_reason"],
                "recommendation": group["recommendation"],
                "direction": dict(sorted(group["directions"].items())),
            }
        )
    return result


def _family_csv_rows(families: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "family": family["family"],
            "annotation_forms": sorted(family["annotation_forms"]),
            "silhouette_forms": sorted(family["silhouette_forms"]),
            "record_count": family["record_count"],
            "unique_shots": family["unique_shots"],
            "fields": sorted(family["fields"]),
            "forensic_category": family["forensic_category"],
            "recommendation": family["recommendation"],
            "ambiguity_reason": sorted(family["ambiguity_reasons"]),
        }
        for family in families
    ]


def _sample_csv_row(sample: dict[str, Any]) -> dict[str, Any]:
    return {
        "media_type": sample["media_type"],
        "media_id": sample["media_id"],
        "title": sample["title"],
        "shot_id": sample["shot_id"],
        "frame": sample["frame"],
        "field": sample["field"],
        "annotation_value": sample["annotation_value"],
        "silhouette_label": sample["silhouette_label"],
        "forensic_category": sample["forensic_category"],
        "interpretation_reason": sample["interpretation_reason"],
        "silhouette_json_path": sample["silhouette_json_path"],
        "silhouette_png_path": sample["silhouette_png_path"],
        "source_frame_path": sample["source_frame_path"],
        "number_preference_result": sample["number_preference_result"],
        "inflect_diagnostics": sample["inflect_diagnostics"],
        "existing_search_provenance": sample["existing_search_provenance"],
        "recommendation": sample["recommendation"],
        "relationship_record_count": sample["relationship_record_count"],
        "example_panel_path": sample["example_panel_path"],
        "example_evidence_path": sample["example_evidence_path"],
    }


def audit_silhouette_number_ambiguity(
    project_path: str | Path,
    *,
    preference_audit_dir: str | Path | None = None,
    output_dir: str | Path | None = None,
    sample_size: int = 25,
) -> dict[str, Any]:
    """Inspect only the completed preference audit's semantic-ambiguity rows."""
    project = _project_path(project_path).resolve()
    source_dir = Path(preference_audit_dir) if preference_audit_dir is not None else default_preference_audit_dir(project)
    target_dir = Path(output_dir) if output_dir is not None else default_output_dir(project)
    target_dir.mkdir(parents=True, exist_ok=True)
    examples_dir = target_dir / EXAMPLES_DIR_NAME
    examples_dir.mkdir(parents=True, exist_ok=True)

    safety_roots = [
        project / "data" / "silhouettes",
        project / "data" / "annotations",
        project / "data" / "indexes",
        project / "data" / "metadata",
        project / "data" / "shotlists",
        project / "data" / "palettes",
    ]
    safety_before = {str(root): _snapshot_tree(root) for root in safety_roots}
    source_before = _snapshot_tree(source_dir)

    source_report, source_rows = _load_source_rows(source_dir)
    rows = [_enrich_row(row) for row in source_rows]
    groups = _group_relationships(rows)
    families = _group_families(groups)
    total_records = len(rows)

    title_map = _load_titles(project)
    samples: list[dict[str, Any]] = []
    for index, (group, row) in enumerate(_select_samples(groups, target_count=sample_size), 1):
        sample = _sample_evidence(row, group, title_map)
        stem = f"{index:02d}_{_slugify(sample['annotation_value'])}__{_slugify(sample['silhouette_label'])}"
        panel_path = examples_dir / f"{stem}.png"
        evidence_path = examples_dir / f"{stem}.json"
        sample["example_panel_path"] = str(panel_path)
        sample["example_evidence_path"] = str(evidence_path)
        _render_panel(sample, panel_path)
        atomic_write_text(evidence_path, json.dumps(sample, indent=2, ensure_ascii=False) + "\n")
        samples.append(sample)

    safety_after = {str(root): _snapshot_tree(root) for root in safety_roots}
    source_after = _snapshot_tree(source_dir)
    roots_same = {key: safety_before[key] == safety_after[key] for key in safety_before}
    source_same = source_before == source_after
    drifted_roots = [key for key, unchanged in roots_same.items() if not unchanged]

    category_counts = Counter(row["forensic_category"] for row in rows)
    field_counts = Counter(str(row.get("field") or "") for row in rows)
    label_counts = Counter(str(row.get("silhouette_label") or "") for row in rows)
    annotation_counts = Counter(str(row.get("annotation_value") or "") for row in rows)
    direction_counts = Counter(str(row.get("direction") or "") for row in rows)
    silhouette_word_counts = Counter(len(_normalize_text(str(row.get("silhouette_label") or "")).split()) for row in rows)
    annotation_word_counts = Counter(len(_normalize_text(str(row.get("annotation_value") or "")).split()) for row in rows)
    shot_keys = {(str(row.get("media_type") or ""), str(row.get("shot_id") or "")) for row in rows}
    media_keys = {(str(row.get("media_type") or ""), str(row.get("media_id") or "")) for row in rows}
    form_counts = {
        form: sum(
            1
            for row in rows
            if _normalize_text(str(row.get("annotation_value") or "")) == form
            or _normalize_text(str(row.get("silhouette_label") or "")) == form
        )
        for form in ("pants", "trousers", "scissors", "clothes")
    }
    candidate_records = sum(
        int(family["record_count"])
        for family in families
        if family["recommendation"] == RECOMMEND_CANDIDATE
    )
    retained_records = sum(
        int(family["record_count"])
        for family in families
        if family["recommendation"] == RECOMMEND_RETAIN
    )

    relationship_rows = _relationship_csv_rows(groups)
    family_rows = _family_csv_rows(families)
    sampled_rows = [_sample_csv_row(sample) for sample in samples]
    relationships_path = target_dir / RELATIONSHIPS_CSV_NAME
    families_path = target_dir / FAMILIES_CSV_NAME
    sampled_path = target_dir / SAMPLED_EXAMPLES_CSV_NAME
    report_json_path = target_dir / REPORT_JSON_NAME
    report_md_path = target_dir / REPORT_MD_NAME

    atomic_write_text(
        relationships_path,
        _csv_text(
            relationship_rows,
            [
                "media_type",
                "field",
                "annotation_value",
                "silhouette_label",
                "record_count",
                "unique_shots",
                "unique_media",
                "inflect_result",
                "ambiguity_reason",
                "forensic_category",
                "interpretation_reason",
                "recommendation",
                "direction",
            ],
        ),
    )
    atomic_write_text(
        families_path,
        _csv_text(
            family_rows,
            [
                "family",
                "annotation_forms",
                "silhouette_forms",
                "record_count",
                "unique_shots",
                "fields",
                "forensic_category",
                "recommendation",
                "ambiguity_reason",
            ],
        ),
    )
    atomic_write_text(
        sampled_path,
        _csv_text(
            sampled_rows,
            [
                "media_type",
                "media_id",
                "title",
                "shot_id",
                "frame",
                "field",
                "annotation_value",
                "silhouette_label",
                "forensic_category",
                "interpretation_reason",
                "silhouette_json_path",
                "silhouette_png_path",
                "source_frame_path",
                "number_preference_result",
                "inflect_diagnostics",
                "existing_search_provenance",
                "recommendation",
                "relationship_record_count",
                "example_panel_path",
                "example_evidence_path",
            ],
        ),
    )

    category_summary = {category: category_counts.get(category, 0) for category in FORENSIC_CATEGORIES}
    report = {
        "project_path": str(project),
        "source_preference_audit_dir": str(source_dir),
        "output_dir": str(target_dir),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "selection": {
            "source_relationship": NUMBER_SEMANTICALLY_AMBIGUOUS,
            "source_report_count": ((source_report.get("counts") or {}).get("relationship") or {}).get(NUMBER_SEMANTICALLY_AMBIGUOUS),
            "selected_record_count": total_records,
            "selection_matches_source_report": True,
            "selection_method": "Exact source CSV relationship equality; no morphology recomputation, fuzzy matching, or cross-value matching.",
        },
        "inventory": {
            "total_records": total_records,
            "unique_relationships": len(groups),
            "unique_silhouette_labels": len(label_counts),
            "unique_annotation_values": len(annotation_counts),
            "unique_shots": len(shot_keys),
            "unique_media": len(media_keys),
            "by_field": dict(sorted(field_counts.items())),
            "by_silhouette_word_count": dict(sorted(silhouette_word_counts.items())),
            "by_annotation_word_count": dict(sorted(annotation_word_counts.items())),
            "by_direction": dict(sorted(direction_counts.items())),
            "concentration": _concentration(groups, total_records),
        },
        "forensic_category_counts": category_summary,
        "named_form_counts": form_counts,
        "policy_candidates": {
            "candidate_records": candidate_records,
            "candidate_share": (candidate_records / total_records) if total_records else 0.0,
            "retain_ambiguous_records": retained_records,
            "retain_ambiguous_share": (retained_records / total_records) if total_records else 0.0,
            "recommendation": "Do not change production data from this audit. The exact pluralia-tantum and compound cases are small, reviewable future-policy candidates; unqualified glass/glasses remains ambiguous.",
        },
        "families": family_rows,
        "sample": {
            "target_count": sample_size,
            "rendered_count": len(samples),
            "selection_method": "One sample per exact relationship, then evenly distributed distinct-shot samples from the largest relationship.",
        },
        "safety": {
            "implementation_source_writes": False,
            "writes_only_under": str(target_dir),
            "source_preference_audit_unchanged": source_same,
            "data_roots_unchanged": roots_same,
            "concurrent_drift_detected": bool(drifted_roots or not source_same),
            "drifted_roots": drifted_roots,
            "note": "Any changed source root is recorded as concurrent drift. This implementation only reads project data and writes generated audit artifacts under outputs/tests.",
        },
        "artifacts": {
            "report_json": str(report_json_path),
            "report_md": str(report_md_path),
            "relationships_csv": str(relationships_path),
            "families_csv": str(families_path),
            "sampled_examples_csv": str(sampled_path),
            "examples_dir": str(examples_dir),
        },
    }

    report_md_lines = [
        "# Silhouette Number Ambiguity Audit",
        "",
        f"Project: `{project}`",
        f"Source preference audit: `{source_dir}`",
        f"Output dir: `{target_dir}`",
        "",
        "## Answer Summary",
        "",
        f"The completed preference audit contributed **{total_records}** `NUMBER_SEMANTICALLY_AMBIGUOUS` records across **{len(groups)}** exact linguistic relationships and **{len(families)}** documented noun families.",
        f"The top 10 relationships cover **{_concentration(groups, total_records)['top_10']['share']:.2%}** of the population.",
        f"A small exact lexical policy could review **{candidate_records}** records ({(candidate_records / total_records) if total_records else 0.0:.2%}); **{retained_records}** records remain ambiguous because unqualified `glass`/`glasses` does not identify a stable sense from morphology alone.",
        "",
        "## Inventory",
        "",
        f"- Records: **{total_records}**",
        f"- Relationships: **{len(groups)}**",
        f"- Silhouette labels: **{len(label_counts)}**",
        f"- Annotation values: **{len(annotation_counts)}**",
        f"- Unique shots: **{len(shot_keys)}**",
        f"- Unique media: **{len(media_keys)}**",
        f"- Fields: `{_json_text(dict(sorted(field_counts.items())))}`",
        f"- Direction: `{_json_text(dict(sorted(direction_counts.items())))}`",
        f"- Silhouette word count: `{_json_text(dict(sorted(silhouette_word_counts.items())))}`",
        f"- Annotation word count: `{_json_text(dict(sorted(annotation_word_counts.items())))}`",
        "",
        "## Concentration",
        "",
        "| slice | relationships | records | share |",
        "| --- | ---: | ---: | ---: |",
    ]
    for name, value in _concentration(groups, total_records).items():
        report_md_lines.append(
            f"| {name} | {value['relationships_included']} | {value['record_count']} | {value['share']:.2%} |"
        )
    report_md_lines.extend([
        "",
        "## Families",
        "",
        "| family | records | unique shots | category | recommendation |",
        "| --- | ---: | ---: | --- | --- |",
    ])
    for family in families:
        report_md_lines.append(
            f"| {family['family']} | {family['record_count']} | {family['unique_shots']} | {family['forensic_category']} | {family['recommendation']} |"
        )
    report_md_lines.extend([
        "",
        "## Categories",
        "",
        "| category | records |",
        "| --- | ---: |",
    ])
    for category, count in category_summary.items():
        report_md_lines.append(f"| {category} | {count} |")
    report_md_lines.extend([
        "",
        "## Specific Forms",
        "",
        f"- pants: **{form_counts['pants']}**",
        f"- trousers: **{form_counts['trousers']}**",
        f"- scissors: **{form_counts['scissors']}**",
        f"- clothes: **{form_counts['clothes']}**",
        "",
        "## Conclusions",
        "",
        "1. The ambiguous population is a small recurring family set, not hundreds of unrelated linguistic issues.",
        "2. `pants` and `scissors` are pluralia-tantum candidates; `shot glass(es)` and `wine glass(es)` expose a compound-specific inflect limitation.",
        "3. Unqualified `glass`/`glasses` remains too polysemous for automatic promotion, even when a visual sample suggests one likely sense.",
        "4. Nothing here weakens the rule that the original annotation is preferred. Any future accepted lexical variant remains valid but non-preferred.",
        "5. Recommended next production-data change: none. Review the small candidate classes before considering a narrowly documented future lexical policy; do not migrate the retained glass/glasses family.",
        "",
        "## Safety",
        "",
        "This audit reads project data and writes only its generated output directory.",
        f"Source preference artifact unchanged: **{source_same}**",
        f"Concurrent drift detected: **{bool(drifted_roots or not source_same)}**",
    ])
    if drifted_roots:
        report_md_lines.append(f"Drifted source roots: `{_json_text(drifted_roots)}`")

    atomic_write_text(report_json_path, json.dumps(report, indent=2, ensure_ascii=False) + "\n")
    atomic_write_text(report_md_path, "\n".join(report_md_lines) + "\n")
    return report