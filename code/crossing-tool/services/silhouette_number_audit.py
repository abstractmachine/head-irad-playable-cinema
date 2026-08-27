"""Read-only forensic audit for silhouette label singular/plural ambiguity.

The historical semantic provenance audit classifies labels by exact-match,
split, partial, unsupported, and unverifiable evidence.  This module keeps the
same source-of-truth join between silhouette sidecars and shot annotations, but
adds a conservative phrase-level number-aware matcher so we can isolate records
whose only ambiguity is singular/plural morphology.

The audit is deliberately non-destructive:
- source trees under ``data/`` are never modified
- all generated artifacts are written under ``outputs/tests/silhouette-number-audit/``
- PNG sidecars are never touched
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import re
import subprocess
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


NUMBER_AUDIT_VERSION = "number-v1"
AUDIT_DIR_NAME = Path("outputs") / "tests" / "silhouette-number-audit"
PROVENANCE_CSV_NAME = "silhouette_number_provenance.csv"
LABEL_SUMMARY_CSV_NAME = "label_summary.csv"
REPORT_JSON_NAME = "report.json"
REPORT_MD_NAME = "report.md"

VALID_CLASSIFICATION = "VALID"
QUESTIONABLE_NUMBER = "QUESTIONABLE_NUMBER"
QUESTIONABLE_SPLIT = "QUESTIONABLE_SPLIT"
QUESTIONABLE_PARTIAL = "QUESTIONABLE_PARTIAL"
QUESTIONABLE_UNSUPPORTED = "QUESTIONABLE_UNSUPPORTED"
UNVERIFIABLE = "UNVERIFIABLE"

_AMBIGUOUS_PLURAL_SURFACES = frozenset({
    "clothes",
    "glasses",
    "headphones",
    "jeans",
    "means",
    "news",
    "pants",
    "pliers",
    "pyjamas",
    "pajamas",
    "scissors",
    "series",
    "shorts",
    "species",
    "sunglasses",
    "tweezers",
    "trousers",
    "eyeglasses",
    "binoculars",
    "thanks",
    "physics",
    "mathematics",
    "economics",
    "linguistics",
})

_IRREGULAR_SINGULARS: dict[str, str] = {
    "children": "child",
    "men": "man",
    "women": "woman",
    "people": "person",
    "geese": "goose",
    "mice": "mouse",
    "oxen": "ox",
    "feet": "foot",
    "teeth": "tooth",
    "lice": "louse",
    "dice": "die",
    "wolves": "wolf",
    "calves": "calf",
    "horses": "horse",
    "cows": "cow",
    "dogs": "dog",
    "cats": "cat",
    "mules": "mule",
    "donkeys": "donkey",
    "chickens": "chicken",
    "rabbits": "rabbit",
    "birds": "bird",
    "goats": "goat",
    "pigs": "pig",
    "turkeys": "turkey",
    "ducks": "duck",
    "pronghorns": "pronghorn",
}

_FALSE_PLURAL_SUFFIXES: tuple[str, ...] = (
    "ss",
    "is",
    "us",
    "as",
    "os",
    "ious",
    "ous",
)


def _project_path(project_path: str | Path) -> Path:
    return Path(project_path)


def default_audit_dir(project_path: str | Path) -> Path:
    """Return the canonical output directory for the number audit."""
    return _project_path(project_path) / AUDIT_DIR_NAME


def _normalize_text(value: str | None) -> str:
    if value is None:
        return ""
    text = value.lower().replace("_", " ")
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _tokens(value: str | None) -> list[str]:
    text = _normalize_text(value)
    return text.split() if text else []


def _normalize_annotation_values(raw_value: Any) -> list[str]:
    if isinstance(raw_value, list):
        return [str(v).strip() for v in raw_value if str(v).strip()]
    if raw_value is None:
        return []
    text = str(raw_value).strip()
    return [text] if text else []


def _number_candidates(token: str) -> list[str]:
    word = token.lower()
    if not word or word in _AMBIGUOUS_PLURAL_SURFACES:
        return []

    candidates: list[str] = []
    irregular = _IRREGULAR_SINGULARS.get(word)
    if irregular:
        candidates.append(irregular)

    if len(word) > 4 and word.endswith("ies"):
        candidates.append(word[:-3] + "y")

    if len(word) > 4 and word.endswith("ves"):
        candidates.extend([word[:-3] + "f", word[:-3] + "fe", word[:-3] + "ve"])

    if len(word) > 4 and word.endswith("sses"):
        candidates.append(word[:-2])
    elif len(word) > 3 and word.endswith(("xes", "zes", "ches", "shes", "oes")):
        candidates.append(word[:-2])
    elif len(word) > 3 and word.endswith("ses"):
        candidates.extend([word[:-2], word[:-1]])

    if len(word) > 3 and word.endswith("s") and word[-2] not in "aeiou" and not any(word.endswith(sfx) for sfx in _FALSE_PLURAL_SUFFIXES):
        candidates.append(word[:-1])

    unique: list[str] = []
    for candidate in candidates:
        if candidate and candidate not in unique:
            unique.append(candidate)
    return unique


def _canonical_number_token(token: str, surface_forms: set[str]) -> str:
    word = token.lower()
    for candidate in _number_candidates(word):
        if candidate in surface_forms:
            return candidate
    return word


def _canonical_number_sequence(tokens: list[str], surface_forms: set[str]) -> tuple[str, ...]:
    return tuple(_canonical_number_token(token, surface_forms) for token in tokens)


def _snapshot_tree(root: Path) -> dict[str, Any]:
    digest = hashlib.sha256()
    file_count = 0
    dir_count = 0
    size_total = 0
    mtime_min = None
    mtime_max = None

    if not root.exists():
        return {
            "root": str(root),
            "exists": False,
            "file_count": 0,
            "dir_count": 0,
            "size_total": 0,
            "mtime_min": None,
            "mtime_max": None,
            "sha256": None,
        }

    for dirpath, dirnames, filenames in os.walk(root):
        dir_count += len(dirnames)
        base = Path(dirpath)
        for name in filenames:
            path = base / name
            try:
                st = path.stat()
            except FileNotFoundError:
                continue
            file_count += 1
            size_total += st.st_size
            if mtime_min is None or st.st_mtime_ns < mtime_min:
                mtime_min = st.st_mtime_ns
            if mtime_max is None or st.st_mtime_ns > mtime_max:
                mtime_max = st.st_mtime_ns
            digest.update(f"{path.relative_to(root)}|{st.st_size}|{st.st_mtime_ns}\n".encode("utf-8"))

    return {
        "root": str(root),
        "exists": True,
        "file_count": file_count,
        "dir_count": dir_count,
        "size_total": size_total,
        "mtime_min": mtime_min,
        "mtime_max": mtime_max,
        "sha256": digest.hexdigest(),
    }


@dataclass
class AnnotationFieldInfo:
    values: list[str] = field(default_factory=list)
    norm_values: set[str] = field(default_factory=set)
    tokens: set[str] = field(default_factory=set)
    token_support: dict[str, set[str]] = field(default_factory=lambda: defaultdict(set))


AnnotationIndex = dict[str, dict[str, dict[str, AnnotationFieldInfo]]]


@dataclass
class RecordMeta:
    json_path: Path
    png_path: Path
    media_type: str
    filename_stem: str
    media_id: str
    shot_id: str
    frame: int
    field: str
    label: str
    timestamp: str
    source_frame: str
    error: str | None = None


@dataclass
class LabelSummary:
    record_count: int = 0
    shots: set[str] = field(default_factory=set)
    valid: int = 0
    valid_single: int = 0
    valid_exact: int = 0
    questionable_number: int = 0
    questionable_split: int = 0
    questionable_partial: int = 0
    questionable_unsupported: int = 0
    unverifiable: int = 0

    @property
    def questionable_total(self) -> int:
        return (
            self.questionable_number
            + self.questionable_split
            + self.questionable_partial
            + self.questionable_unsupported
        )

    @property
    def questionable_percentage(self) -> float:
        return (self.questionable_total / self.record_count) if self.record_count else 0.0


def _annotation_roots(project: Path) -> dict[str, Path]:
    return {
        "movie": project / "data" / "annotations" / "shots" / "movie",
        "gameplay": project / "data" / "annotations" / "shots" / "gameplay",
    }


def _collect_annotation_stats(annotation_roots: dict[str, Path]) -> tuple[dict[str, int], set[str]]:
    stats = {
        "files_seen": 0,
        "files_parsed": 0,
        "files_failed": 0,
        "entries_seen": 0,
        "entries_missing_shot": 0,
        "entries_missing_annotation": 0,
    }
    known_tokens: set[str] = set()

    for root in annotation_roots.values():
        if not root.exists():
            continue
        for ann_path in sorted(root.glob("*.annotations.json")):
            stats["files_seen"] += 1
            try:
                payload = json.loads(ann_path.read_text(encoding="utf-8"))
            except Exception:
                stats["files_failed"] += 1
                continue
            if not isinstance(payload, list):
                stats["files_failed"] += 1
                continue
            stats["files_parsed"] += 1
            for entry in payload:
                stats["entries_seen"] += 1
                if not isinstance(entry, dict):
                    continue
                shot = entry.get("shot")
                if not isinstance(shot, dict):
                    stats["entries_missing_shot"] += 1
                    continue
                annotation = shot.get("annotation")
                if not isinstance(annotation, dict):
                    stats["entries_missing_annotation"] += 1
                    continue
                for raw_value in annotation.values():
                    for value in _normalize_annotation_values(raw_value):
                        known_tokens.update(_tokens(value))

    return stats, known_tokens


def _load_annotation_index(annotation_roots: dict[str, Path]) -> AnnotationIndex:
    index: AnnotationIndex = defaultdict(lambda: defaultdict(dict))

    for media_type, root in annotation_roots.items():
        if not root.exists():
            continue
        for ann_path in sorted(root.glob("*.annotations.json")):
            try:
                payload = json.loads(ann_path.read_text(encoding="utf-8"))
            except Exception:
                continue
            if not isinstance(payload, list):
                continue
            for entry in payload:
                if not isinstance(entry, dict):
                    continue
                shot = entry.get("shot")
                if not isinstance(shot, dict):
                    continue
                shot_id = str(shot.get("shot_id") or "")
                annotation = shot.get("annotation")
                if not shot_id or not isinstance(annotation, dict):
                    continue
                shot_bucket = index[media_type][shot_id]
                for field_name, raw_value in annotation.items():
                    field_info = shot_bucket.setdefault(field_name, AnnotationFieldInfo())
                    values = _normalize_annotation_values(raw_value)
                    for value in values:
                        norm = _normalize_text(value)
                        if not norm:
                            continue
                        if norm not in field_info.norm_values:
                            field_info.norm_values.add(norm)
                            field_info.values.append(value)
                        for token in set(_tokens(value)):
                            field_info.tokens.add(token)
                            field_info.token_support[token].add(value)

    return index


def _iter_catalog_paths(project: Path, media_type: str) -> list[Path]:
    base = project / "data" / "silhouettes" / "catalog" / media_type
    if not base.exists():
        return []

    paths: list[Path] = []
    for item_dir in sorted(base.iterdir()):
        if not item_dir.is_dir() or item_dir.name.startswith("."):
            continue
        for label_dir in sorted(item_dir.iterdir()):
            if not label_dir.is_dir() or label_dir.name.startswith("."):
                continue
            paths.extend(sorted(label_dir.glob("object_????.json")))
    return paths


def _load_record_meta(json_path: Path) -> RecordMeta:
    media_type = json_path.parents[2].name if len(json_path.parents) >= 3 else ""
    filename_stem = json_path.parents[1].name if len(json_path.parents) >= 2 else ""
    label = json_path.parents[0].name if len(json_path.parents) >= 1 else ""
    png_path = json_path.with_suffix(".png")

    try:
        raw = json.loads(json_path.read_text(encoding="utf-8"))
    except Exception as exc:
        return RecordMeta(
            json_path=json_path,
            png_path=png_path,
            media_type=media_type,
            filename_stem=filename_stem,
            media_id="",
            shot_id="",
            frame=0,
            field="",
            label=label,
            timestamp="",
            source_frame="",
            error=str(exc),
        )

    if not isinstance(raw, dict):
        return RecordMeta(
            json_path=json_path,
            png_path=png_path,
            media_type=media_type,
            filename_stem=filename_stem,
            media_id="",
            shot_id="",
            frame=0,
            field="",
            label=label,
            timestamp="",
            source_frame="",
            error="archive record is not a JSON object",
        )

    label_value = str(raw.get("label") or label)
    png_name = str(raw.get("png") or png_path.name)
    return RecordMeta(
        json_path=json_path,
        png_path=json_path.with_name(png_name),
        media_type=str(raw.get("media_type") or media_type),
        filename_stem=str(raw.get("filename_stem") or filename_stem),
        media_id=str(raw.get("media_id") or ""),
        shot_id=str(raw.get("shot_id") or ""),
        frame=int(raw.get("frame") or 0),
        field=str(raw.get("field") or ""),
        label=label_value,
        timestamp=str(raw.get("timestamp") or ""),
        source_frame=str(raw.get("source_frame") or ""),
    )


def _field_info(index: AnnotationIndex, record: RecordMeta) -> AnnotationFieldInfo | None:
    if not record.media_type or not record.shot_id or not record.field:
        return None
    media_bucket = index.get(record.media_type)
    if media_bucket is None:
        return None
    shot_bucket = media_bucket.get(record.shot_id)
    if shot_bucket is None:
        return None
    return shot_bucket.get(record.field)


def _classify_record(
    index: AnnotationIndex,
    record: RecordMeta,
) -> dict[str, Any]:
    words = _tokens(record.label)
    word_count = len(words)

    if record.error:
        return {
            "media_type": record.media_type,
            "media_id": record.media_id,
            "shot_id": record.shot_id,
            "frame": record.frame,
            "field": record.field,
            "historical_label": record.label,
            "word_count": word_count,
            "classification": UNVERIFIABLE,
            "valid_reason": "",
            "annotation_values": [],
            "annotation_value_count": 0,
            "exact_annotation_match": False,
            "number_annotation_match": False,
            "all_words_present": False,
            "all_words_present_as_one_value": False,
            "separate_component_values": False,
            "matched_words": [],
            "missing_words": words,
            "support_values": {word: [] for word in words},
            "number_support_values": [],
            "reason": f"archive record unreadable: {record.error}",
            "archive_json_path": str(record.json_path),
            "archive_png_path": str(record.png_path),
            "timestamp": record.timestamp,
            "source_frame": record.source_frame,
        }

    field_info = _field_info(index, record)
    annotation_values = list(field_info.values) if field_info is not None else []
    annotation_value_count = len(annotation_values)
    annotation_value_norms = field_info.norm_values if field_info is not None else set()
    annotation_tokens = field_info.tokens if field_info is not None else set()

    exact_annotation_match = bool(record.label and _normalize_text(record.label) in annotation_value_norms)
    exact_support_values = {
        word: sorted(field_info.token_support[word]) if field_info is not None and word in field_info.token_support else []
        for word in words
    }
    matched_words = [word for word in words if word in annotation_tokens]
    missing_words = [word for word in words if word not in annotation_tokens]
    all_words_present = not missing_words
    all_words_present_as_one_value = exact_annotation_match

    surface_forms = set(words)
    for value in annotation_values:
        surface_forms.update(_tokens(value))

    number_support_values: list[str] = []
    if field_info is not None and word_count > 1:
        label_canonical = _canonical_number_sequence(words, surface_forms)
        number_support_values = [
            value
            for value in annotation_values
            if _canonical_number_sequence(_tokens(value), surface_forms) == label_canonical
            and _normalize_text(value) != _normalize_text(record.label)
        ]

    number_annotation_match = bool(number_support_values)

    if word_count <= 1:
        classification = VALID_CLASSIFICATION
        valid_reason = "single_word_label"
        reason = "single-word label"
    elif field_info is None:
        classification = UNVERIFIABLE
        valid_reason = ""
        reason = "source annotation unavailable"
    elif exact_annotation_match:
        classification = VALID_CLASSIFICATION
        valid_reason = "exact_annotation_value"
        reason = "exact annotation value"
    elif number_annotation_match:
        classification = QUESTIONABLE_NUMBER
        valid_reason = ""
        reason = "single annotation value matches after conservative singular/plural normalization"
    elif all_words_present:
        classification = QUESTIONABLE_SPLIT
        valid_reason = ""
        reason = "all words occur separately but the compound does not exist as one annotation value"
    elif matched_words:
        classification = QUESTIONABLE_PARTIAL
        valid_reason = ""
        reason = f"only {', '.join(repr(word) for word in matched_words)} supported by annotation"
    else:
        classification = QUESTIONABLE_UNSUPPORTED
        valid_reason = ""
        reason = "no component words supported"

    return {
        "media_type": record.media_type,
        "media_id": record.media_id,
        "shot_id": record.shot_id,
        "frame": record.frame,
        "field": record.field,
        "historical_label": record.label,
        "word_count": word_count,
        "classification": classification,
        "valid_reason": valid_reason,
        "annotation_values": annotation_values,
        "annotation_value_count": annotation_value_count,
        "exact_annotation_match": exact_annotation_match,
        "number_annotation_match": number_annotation_match,
        "all_words_present": all_words_present,
        "all_words_present_as_one_value": all_words_present_as_one_value,
        "separate_component_values": all_words_present and not exact_annotation_match and not number_annotation_match,
        "matched_words": matched_words,
        "missing_words": missing_words,
        "support_values": exact_support_values,
        "number_support_values": number_support_values,
        "reason": reason,
        "archive_json_path": str(record.json_path),
        "archive_png_path": str(record.png_path),
        "timestamp": record.timestamp,
        "source_frame": record.source_frame,
    }


def _json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _format_percentage(value: float) -> str:
    return f"{value * 100:.2f}%"


def _csv_text(rows: list[dict[str, Any]], fieldnames: list[str]) -> str:
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=fieldnames)
    writer.writeheader()
    for row in rows:
        writer.writerow(row)
    return buffer.getvalue()


def _record_to_csv_row(result: dict[str, Any]) -> dict[str, str]:
    return {
        "media_type": result["media_type"],
        "media_id": result["media_id"],
        "shot_id": result["shot_id"],
        "frame": str(result["frame"]),
        "field": result["field"],
        "historical_label": result["historical_label"],
        "word_count": str(result["word_count"]),
        "classification": result["classification"],
        "valid_reason": result["valid_reason"],
        "annotation_values": _json_dumps(result["annotation_values"]),
        "annotation_value_count": str(result["annotation_value_count"]),
        "exact_annotation_match": str(result["exact_annotation_match"]),
        "number_annotation_match": str(result["number_annotation_match"]),
        "all_words_present": str(result["all_words_present"]),
        "all_words_present_as_one_value": str(result["all_words_present_as_one_value"]),
        "separate_component_values": str(result["separate_component_values"]),
        "matched_words": _json_dumps(result["matched_words"]),
        "missing_words": _json_dumps(result["missing_words"]),
        "support_values": _json_dumps(result["support_values"]),
        "number_support_values": _json_dumps(result["number_support_values"]),
        "reason": result["reason"],
        "archive_json_path": result["archive_json_path"],
        "archive_png_path": result["archive_png_path"],
        "timestamp": result["timestamp"],
        "source_frame": result["source_frame"],
    }


def _summary_row_from_counts(
    media_type: str,
    field: str,
    label: str,
    word_count: int,
    summary: LabelSummary,
) -> dict[str, Any]:
    return {
        "media_type": media_type,
        "field": field,
        "historical_label": label,
        "word_count": word_count,
        "record_count": summary.record_count,
        "unique_shots": len(summary.shots),
        "valid": summary.valid,
        "valid_single": summary.valid_single,
        "valid_exact": summary.valid_exact,
        "questionable_number": summary.questionable_number,
        "questionable_split": summary.questionable_split,
        "questionable_partial": summary.questionable_partial,
        "questionable_unsupported": summary.questionable_unsupported,
        "unverifiable": summary.unverifiable,
        "questionable_total": summary.questionable_total,
        "questionable_percentage": summary.questionable_percentage,
    }


def audit_silhouette_number_provenance(
    project_path: str | Path,
    *,
    media_type: str = "movie",
    audit_dir: str | Path | None = None,
) -> dict[str, Any]:
    """Run the read-only number-ambiguity audit and write summary artifacts.

    The audit only reads from ``data/`` trees and writes generated reports under
    the chosen audit directory.
    """
    from data.annotate import atomic_write_text

    project = _project_path(project_path).resolve()
    archive_root = project / "data" / "silhouettes" / "catalog" / media_type
    audit_root = Path(audit_dir) if audit_dir is not None else default_audit_dir(project)
    audit_root.mkdir(parents=True, exist_ok=True)

    annotation_roots = _annotation_roots(project)
    annotation_stats, known_tokens = _collect_annotation_stats(annotation_roots)
    annotation_index = _load_annotation_index(annotation_roots)

    roots = [
        archive_root,
        project / "data" / "silhouettes",
        project / "data" / "annotations",
        project / "data" / "indexes",
        project / "data" / "metadata",
        project / "data" / "palettes",
        project / "data" / "shotlists",
    ]
    pre_snapshots = {str(root): _snapshot_tree(root) for root in roots}
    git_before = subprocess.run(
        ["git", "-C", str(project), "status", "--short"],
        capture_output=True,
        text=True,
        check=False,
    )
    archive_git_status_before = git_before.stdout.strip() if git_before.returncode == 0 else ""
    archive_git_status_available = git_before.returncode == 0

    provenance_fieldnames = [
        "media_type", "media_id", "shot_id", "frame", "field", "historical_label",
        "word_count", "classification", "valid_reason", "annotation_values",
        "annotation_value_count", "exact_annotation_match", "number_annotation_match",
        "all_words_present", "all_words_present_as_one_value", "separate_component_values",
        "matched_words", "missing_words", "support_values", "number_support_values",
        "reason", "archive_json_path", "archive_png_path", "timestamp", "source_frame",
    ]
    summary_fieldnames = [
        "media_type", "field", "historical_label", "word_count", "record_count",
        "unique_shots", "valid", "valid_single", "valid_exact", "questionable_number",
        "questionable_split", "questionable_partial", "questionable_unsupported",
        "unverifiable", "questionable_total", "questionable_percentage",
    ]

    overall = Counter()
    valid_single_total = 0
    valid_exact_total = 0
    by_field: dict[str, Counter] = defaultdict(Counter)
    by_media_type: dict[str, Counter] = defaultdict(Counter)
    by_word_count: dict[str, Counter] = defaultdict(Counter)
    by_field_valid_single: dict[str, int] = defaultdict(int)
    by_field_valid_exact: dict[str, int] = defaultdict(int)
    by_media_valid_single: dict[str, int] = defaultdict(int)
    by_media_valid_exact: dict[str, int] = defaultdict(int)
    by_word_valid_single: dict[str, int] = defaultdict(int)
    by_word_valid_exact: dict[str, int] = defaultdict(int)
    label_summary: dict[tuple[str, str, str, int], LabelSummary] = {}
    explicit_examples: dict[str, dict[str, Any]] = {}
    fields_seen: set[str] = set()
    media_types_seen: set[str] = set()
    source_shots_seen: set[str] = set()
    archive_timestamp_min: str | None = None
    archive_timestamp_max: str | None = None
    total_records = 0
    single_word_records = 0
    multi_word_records = 0

    archive_paths = _iter_catalog_paths(project, media_type)
    provenance_rows: list[dict[str, str]] = []

    for json_path in archive_paths:
        record = _load_record_meta(json_path)
        result = _classify_record(annotation_index, record)
        provenance_rows.append(_record_to_csv_row(result))

        total_records += 1
        classification = str(result["classification"])
        word_count = int(result["word_count"])
        bucket = str(word_count) if word_count <= 5 else "6+"

        fields_seen.add(str(result["field"]))
        media_types_seen.add(str(result["media_type"]))
        if result["shot_id"]:
            source_shots_seen.add(str(result["shot_id"]))
        if result["timestamp"]:
            timestamp = str(result["timestamp"])
            if archive_timestamp_min is None or timestamp < archive_timestamp_min:
                archive_timestamp_min = timestamp
            if archive_timestamp_max is None or timestamp > archive_timestamp_max:
                archive_timestamp_max = timestamp

        overall[classification] += 1
        by_field[str(result["field"])][classification] += 1
        by_media_type[str(result["media_type"])][classification] += 1
        by_word_count[bucket][classification] += 1

        if word_count <= 1:
            single_word_records += 1
        else:
            multi_word_records += 1

        if classification == VALID_CLASSIFICATION:
            if result["valid_reason"] == "single_word_label":
                overall["valid_single"] += 1
                valid_single_total += 1
                by_field_valid_single[str(result["field"])] += 1
                by_media_valid_single[str(result["media_type"])] += 1
                by_word_valid_single[bucket] += 1
            elif result["valid_reason"] == "exact_annotation_value":
                overall["valid_exact"] += 1
                valid_exact_total += 1
                by_field_valid_exact[str(result["field"])] += 1
                by_media_valid_exact[str(result["media_type"])] += 1
                by_word_valid_exact[bucket] += 1
        elif classification == QUESTIONABLE_NUMBER:
            overall["questionable_number"] += 1
        elif classification == QUESTIONABLE_SPLIT:
            overall["questionable_split"] += 1
        elif classification == QUESTIONABLE_PARTIAL:
            overall["questionable_partial"] += 1
        elif classification == QUESTIONABLE_UNSUPPORTED:
            overall["questionable_unsupported"] += 1
        elif classification == UNVERIFIABLE:
            overall["unverifiable"] += 1

        summary_key = (
            str(result["media_type"]),
            str(result["field"]),
            str(result["historical_label"]),
            word_count,
        )
        summary = label_summary.get(summary_key)
        if summary is None:
            summary = label_summary[summary_key] = LabelSummary()
        summary.record_count += 1
        if result["shot_id"]:
            summary.shots.add(str(result["shot_id"]))
        if classification == VALID_CLASSIFICATION:
            summary.valid += 1
            if result["valid_reason"] == "single_word_label":
                summary.valid_single += 1
            elif result["valid_reason"] == "exact_annotation_value":
                summary.valid_exact += 1
        elif classification == QUESTIONABLE_NUMBER:
            summary.questionable_number += 1
        elif classification == QUESTIONABLE_SPLIT:
            summary.questionable_split += 1
        elif classification == QUESTIONABLE_PARTIAL:
            summary.questionable_partial += 1
        elif classification == QUESTIONABLE_UNSUPPORTED:
            summary.questionable_unsupported += 1
        elif classification == UNVERIFIABLE:
            summary.unverifiable += 1

        normalized_label = _normalize_text(str(result["historical_label"]))
        if normalized_label and normalized_label not in explicit_examples:
            explicit_examples[normalized_label] = {
                "summary_key": summary_key,
                "result": result,
            }

    summary_rows = [
        _summary_row_from_counts(media_type_name, field, label, word_count, summary)
        for (media_type_name, field, label, word_count), summary in label_summary.items()
    ]
    summary_rows.sort(
        key=lambda row: (
            -int(row["questionable_number"]),
            -int(row["questionable_total"]),
            -int(row["record_count"]),
            str(row["historical_label"]).casefold(),
        )
    )

    provenance_csv = audit_root / PROVENANCE_CSV_NAME
    summary_csv = audit_root / LABEL_SUMMARY_CSV_NAME
    report_json = audit_root / REPORT_JSON_NAME
    report_md = audit_root / REPORT_MD_NAME

    atomic_write_text(provenance_csv, _csv_text(provenance_rows, provenance_fieldnames))
    atomic_write_text(summary_csv, _csv_text([
        {
            **row,
            "questionable_percentage": f"{float(row['questionable_percentage']):.6f}",
        }
        for row in summary_rows
    ], summary_fieldnames))

    questionable_total = (
        overall[QUESTIONABLE_NUMBER]
        + overall[QUESTIONABLE_SPLIT]
        + overall[QUESTIONABLE_PARTIAL]
        + overall[QUESTIONABLE_UNSUPPORTED]
    )
    questionable_percentage = (questionable_total / total_records) if total_records else 0.0

    total_by_field = {
        field_name: {
            "valid": counts[VALID_CLASSIFICATION],
            "valid_single": by_field_valid_single[field_name],
            "valid_exact": by_field_valid_exact[field_name],
            "questionable_number": counts[QUESTIONABLE_NUMBER],
            "questionable_split": counts[QUESTIONABLE_SPLIT],
            "questionable_partial": counts[QUESTIONABLE_PARTIAL],
            "questionable_unsupported": counts[QUESTIONABLE_UNSUPPORTED],
            "unverifiable": counts[UNVERIFIABLE],
            "questionable_total": counts[QUESTIONABLE_NUMBER] + counts[QUESTIONABLE_SPLIT] + counts[QUESTIONABLE_PARTIAL] + counts[QUESTIONABLE_UNSUPPORTED],
            "questionable_percentage": (
                (counts[QUESTIONABLE_NUMBER] + counts[QUESTIONABLE_SPLIT] + counts[QUESTIONABLE_PARTIAL] + counts[QUESTIONABLE_UNSUPPORTED]) / sum(counts.values())
            ) if sum(counts.values()) else 0.0,
            "total_records": sum(counts.values()),
        }
        for field_name, counts in by_field.items()
    }
    total_by_media_type = {
        media_name: {
            "valid": counts[VALID_CLASSIFICATION],
            "valid_single": by_media_valid_single[media_name],
            "valid_exact": by_media_valid_exact[media_name],
            "questionable_number": counts[QUESTIONABLE_NUMBER],
            "questionable_split": counts[QUESTIONABLE_SPLIT],
            "questionable_partial": counts[QUESTIONABLE_PARTIAL],
            "questionable_unsupported": counts[QUESTIONABLE_UNSUPPORTED],
            "unverifiable": counts[UNVERIFIABLE],
            "questionable_total": counts[QUESTIONABLE_NUMBER] + counts[QUESTIONABLE_SPLIT] + counts[QUESTIONABLE_PARTIAL] + counts[QUESTIONABLE_UNSUPPORTED],
            "questionable_percentage": (
                (counts[QUESTIONABLE_NUMBER] + counts[QUESTIONABLE_SPLIT] + counts[QUESTIONABLE_PARTIAL] + counts[QUESTIONABLE_UNSUPPORTED]) / sum(counts.values())
            ) if sum(counts.values()) else 0.0,
            "total_records": sum(counts.values()),
        }
        for media_name, counts in by_media_type.items()
    }
    total_by_word_count = {
        bucket: {
            "valid": counts[VALID_CLASSIFICATION],
            "valid_single": by_word_valid_single[bucket],
            "valid_exact": by_word_valid_exact[bucket],
            "questionable_number": counts[QUESTIONABLE_NUMBER],
            "questionable_split": counts[QUESTIONABLE_SPLIT],
            "questionable_partial": counts[QUESTIONABLE_PARTIAL],
            "questionable_unsupported": counts[QUESTIONABLE_UNSUPPORTED],
            "unverifiable": counts[UNVERIFIABLE],
            "questionable_total": counts[QUESTIONABLE_NUMBER] + counts[QUESTIONABLE_SPLIT] + counts[QUESTIONABLE_PARTIAL] + counts[QUESTIONABLE_UNSUPPORTED],
            "questionable_percentage": (
                (counts[QUESTIONABLE_NUMBER] + counts[QUESTIONABLE_SPLIT] + counts[QUESTIONABLE_PARTIAL] + counts[QUESTIONABLE_UNSUPPORTED]) / sum(counts.values())
            ) if sum(counts.values()) else 0.0,
            "total_records": sum(counts.values()),
        }
        for bucket, counts in by_word_count.items()
    }

    most_number_affected_labels = [
        row for row in sorted(
            summary_rows,
            key=lambda row: (
                -int(row["questionable_number"]),
                -int(row["questionable_total"]),
                -int(row["record_count"]),
                str(row["historical_label"]).casefold(),
            ),
        )[:50]
    ]
    most_affected_labels = [
        row for row in sorted(
            summary_rows,
            key=lambda row: (
                -int(row["questionable_total"]),
                -int(row["questionable_number"]),
                -float(row["questionable_percentage"]),
                -int(row["record_count"]),
                str(row["historical_label"]).casefold(),
            ),
        )[:50]
    ]

    explicit_targets = [
        "arm band",
        "black glove",
        "wooden plank",
        "yellow coat",
        "black neckerchief",
        "brown pants",
        "white shirt",
    ]
    explicit_sections: list[dict[str, Any]] = []
    for target in explicit_targets:
        normalized_target = _normalize_text(target)
        matches = [
            row for row in summary_rows
            if _normalize_text(str(row["historical_label"])) == normalized_target
        ]
        if not matches:
            explicit_sections.append({
                "historical_label": target,
                "found": False,
            })
            continue
        match = matches[0]
        example = explicit_examples.get(normalized_target, {})
        result = example.get("result")
        explicit_sections.append({
            "historical_label": match["historical_label"],
            "field": match["field"],
            "record_count": match["record_count"],
            "valid": match["valid"],
            "valid_single": match["valid_single"],
            "valid_exact": match["valid_exact"],
            "questionable_number": match["questionable_number"],
            "questionable_split": match["questionable_split"],
            "questionable_partial": match["questionable_partial"],
            "questionable_unsupported": match["questionable_unsupported"],
            "unverifiable": match["unverifiable"],
            "questionable_total": match["questionable_total"],
            "questionable_percentage": match["questionable_percentage"],
            "representative_example": {
                "classification": result["classification"] if result else None,
                "valid_reason": result["valid_reason"] if result else None,
                "reason": result["reason"] if result else None,
                "annotation_values": result["annotation_values"] if result else None,
                "matched_words": result["matched_words"] if result else None,
                "missing_words": result["missing_words"] if result else None,
                "support_values": result["support_values"] if result else None,
                "number_support_values": result["number_support_values"] if result else None,
                "shot_id": result["shot_id"] if result else None,
                "frame": result["frame"] if result else None,
                "archive_json_path": result["archive_json_path"] if result else None,
                "archive_png_path": result["archive_png_path"] if result else None,
            },
        })

    post_snapshots = {str(root): _snapshot_tree(root) for root in roots}
    git_after = subprocess.run(
        ["git", "-C", str(project), "status", "--short"],
        capture_output=True,
        text=True,
        check=False,
    )
    archive_git_status_after = git_after.stdout.strip() if git_after.returncode == 0 else ""
    archive_git_status_same = (
        archive_git_status_before == archive_git_status_after
        if archive_git_status_available and git_after.returncode == 0
        else archive_git_status_available == (git_after.returncode == 0)
    )

    for root in roots:
        if pre_snapshots[str(root)] != post_snapshots[str(root)]:
            raise RuntimeError(f"Root changed during audit: {root}")

    report = {
        "project_path": str(project),
        "archive_root": str(archive_root),
        "audit_dir": str(audit_root),
        "audit_version": NUMBER_AUDIT_VERSION,
        "archive_modified": False,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "archive_git_status_before": archive_git_status_before,
        "archive_git_status_after": archive_git_status_after,
        "archive_git_status_available": archive_git_status_available,
        "inventory": {
            "archive_file_count": post_snapshots[str(archive_root)]["file_count"],
            "archive_json_count": len(archive_paths),
            "archive_png_count": sum(1 for _ in archive_root.rglob("object_????.png")) if archive_root.exists() else 0,
            "archive_scan_sentinel_count": sum(1 for _ in (archive_root / ".scanned").rglob("*") if _.is_file()) if (archive_root / ".scanned").exists() else 0,
            "archive_media_item_count": len([p for p in archive_root.iterdir() if p.is_dir() and p.name != ".scanned"]) if archive_root.exists() else 0,
            "unique_label_strings": len(label_summary),
            "multi_word_label_strings": sum(1 for row in summary_rows if int(row["word_count"]) > 1),
            "multi_word_record_count": multi_word_records,
            "source_shots_represented": len(source_shots_seen),
            "source_media_represented": len(media_types_seen),
            "provenance_field_count": len(fields_seen),
            "provenance_fields": sorted(fields_seen),
            "annotation_files_seen": annotation_stats["files_seen"],
            "annotation_files_parsed": annotation_stats["files_parsed"],
            "annotation_files_failed": annotation_stats["files_failed"],
            "annotation_entries_seen": annotation_stats["entries_seen"],
            "annotation_tokens_seen": len(known_tokens),
        },
        "safety": {
            "archive_same_before_after": pre_snapshots[str(archive_root)] == post_snapshots[str(archive_root)],
            "annotations_same_before_after": pre_snapshots[str(project / "data" / "annotations")] == post_snapshots[str(project / "data" / "annotations")],
            "indexes_same_before_after": pre_snapshots[str(project / "data" / "indexes")] == post_snapshots[str(project / "data" / "indexes")],
            "metadata_same_before_after": pre_snapshots[str(project / "data" / "metadata")] == post_snapshots[str(project / "data" / "metadata")],
            "palettes_same_before_after": pre_snapshots[str(project / "data" / "palettes")] == post_snapshots[str(project / "data" / "palettes")],
            "shotlists_same_before_after": pre_snapshots[str(project / "data" / "shotlists")] == post_snapshots[str(project / "data" / "shotlists")],
            "git_status_same_before_after": archive_git_status_same,
            "pre_snapshots": pre_snapshots,
            "post_snapshots": post_snapshots,
        },
        "classification": {
            "total_silhouette_records": total_records,
            "single_word_records": single_word_records,
            "multi_word_records": multi_word_records,
            "valid": overall[VALID_CLASSIFICATION],
            "valid_single": valid_single_total,
            "valid_exact": valid_exact_total,
            "questionable_number": overall[QUESTIONABLE_NUMBER],
            "questionable_split": overall[QUESTIONABLE_SPLIT],
            "questionable_partial": overall[QUESTIONABLE_PARTIAL],
            "questionable_unsupported": overall[QUESTIONABLE_UNSUPPORTED],
            "unverifiable": overall[UNVERIFIABLE],
            "questionable_total": questionable_total,
            "questionable_percentage": questionable_percentage,
            "by_field": total_by_field,
            "by_media_type": total_by_media_type,
            "by_word_count": total_by_word_count,
            "most_number_affected_labels": most_number_affected_labels,
            "most_affected_labels": most_affected_labels,
        },
        "explicit_examples": explicit_sections,
        "timeline": {
            "archive_timestamp_min": archive_timestamp_min,
            "archive_timestamp_max": archive_timestamp_max,
        },
        "summary_csv": str(summary_csv),
        "provenance_csv": str(provenance_csv),
        "report_json": str(report_json),
        "report_md": str(report_md),
    }

    atomic_write_text(report_json, json.dumps(report, indent=2, ensure_ascii=False))

    md_lines = [
        "# Historical Silhouette Number-Ambiguity Audit",
        "",
        f"Project: `{project}`",
        "",
        f"Archive root: `{archive_root}`",
        "",
        "## Verdict",
        "",
        "- Archive modified: **NO**",
        f"- Total silhouette records: **{total_records}**",
        f"- Single-word records: **{single_word_records}**",
        f"- Multi-word records: **{multi_word_records}**",
        f"- VALID: **{overall[VALID_CLASSIFICATION]}**",
        f"-   - single-word valid: **{valid_single_total}**",
        f"-   - exact multi-word valid: **{valid_exact_total}**",
        f"- QUESTIONABLE_NUMBER: **{overall[QUESTIONABLE_NUMBER]}**",
        f"- QUESTIONABLE_SPLIT: **{overall[QUESTIONABLE_SPLIT]}**",
        f"- QUESTIONABLE_PARTIAL: **{overall[QUESTIONABLE_PARTIAL]}**",
        f"- QUESTIONABLE_UNSUPPORTED: **{overall[QUESTIONABLE_UNSUPPORTED]}**",
        f"- UNVERIFIABLE: **{overall[UNVERIFIABLE]}**",
        f"- Questionable total: **{questionable_total}**",
        f"- Questionable share: **{_format_percentage(questionable_percentage)}**",
        "",
        "## Safety",
        "",
        f"- Silhouettes snapshot identical before/after: **{report['safety']['archive_same_before_after']}**",
        f"- Annotations snapshot identical before/after: **{report['safety']['annotations_same_before_after']}**",
        f"- Indexes snapshot identical before/after: **{report['safety']['indexes_same_before_after']}**",
        f"- Metadata snapshot identical before/after: **{report['safety']['metadata_same_before_after']}**",
        f"- Palettes snapshot identical before/after: **{report['safety']['palettes_same_before_after']}**",
        f"- Shotlists snapshot identical before/after: **{report['safety']['shotlists_same_before_after']}**",
        f"- Git status identical before/after: **{report['safety']['git_status_same_before_after']}**",
        "",
        "## Interpretation",
        "",
        "Single-word labels are treated as valid evidence on their own. Multi-word labels are only VALID when the complete normalized label exists as one annotation value in the same shot and field. If the words are all present but only separately, the record is QUESTIONABLE_SPLIT; if a single annotation value matches after conservative singular/plural normalization, the record is QUESTIONABLE_NUMBER; if only some words are present, QUESTIONABLE_PARTIAL; if none are present, QUESTIONABLE_UNSUPPORTED; and if provenance cannot be established, UNVERIFIABLE.",
        "",
        "## By Field",
        "",
    ]

    for field_name, counts in sorted(report["classification"]["by_field"].items(), key=lambda kv: (-kv[1]["questionable_number"], kv[0])):
        md_lines.append(
            f"- `{field_name}`: valid={counts['valid']}, number={counts['questionable_number']}, split={counts['questionable_split']}, partial={counts['questionable_partial']}, unsupported={counts['questionable_unsupported']}, unverifiable={counts['unverifiable']}, questionable_total={counts['questionable_total']}, questionable_share={_format_percentage(counts['questionable_percentage'])}"
        )

    md_lines.extend(["", "## By Word Count", ""])
    for bucket in ["1", "2", "3", "4", "5", "6+"]:
        counts = report["classification"]["by_word_count"].get(bucket, {
            "valid": 0,
            "valid_single": 0,
            "valid_exact": 0,
            "questionable_number": 0,
            "questionable_split": 0,
            "questionable_partial": 0,
            "questionable_unsupported": 0,
            "unverifiable": 0,
            "questionable_total": 0,
            "questionable_percentage": 0.0,
            "total_records": 0,
        })
        md_lines.append(
            f"- `{bucket}`: records={counts['total_records']}, valid={counts['valid']}, number={counts['questionable_number']}, split={counts['questionable_split']}, partial={counts['questionable_partial']}, unsupported={counts['questionable_unsupported']}, unverifiable={counts['unverifiable']}, questionable_total={counts['questionable_total']}, questionable_share={_format_percentage(counts['questionable_percentage'])}"
        )

    md_lines.extend(["", "## By Media Type", ""])
    for media_name, counts in sorted(report["classification"]["by_media_type"].items(), key=lambda kv: (-kv[1]["questionable_number"], kv[0])):
        md_lines.append(
            f"- `{media_name}`: valid={counts['valid']}, number={counts['questionable_number']}, split={counts['questionable_split']}, partial={counts['questionable_partial']}, unsupported={counts['questionable_unsupported']}, unverifiable={counts['unverifiable']}, questionable_total={counts['questionable_total']}, questionable_share={_format_percentage(counts['questionable_percentage'])}"
        )

    md_lines.extend(["", "## Most Number-Ambiguous Labels", ""])
    for row in report["classification"]["most_number_affected_labels"][:20]:
        md_lines.append(
            f"- `{row['historical_label']}` ({row['field']}, {row['media_type']}, word_count={row['word_count']}): records={row['record_count']}, number={row['questionable_number']}, questionable_total={row['questionable_total']}, questionable_share={_format_percentage(row['questionable_percentage'])}"
        )

    md_lines.extend(["", "## Explicit Examples", ""])
    for example in explicit_sections:
        if not example.get("found", True):
            md_lines.append(f"- `{example['historical_label']}`: not found in the historical silhouette archive")
            continue
        rep = example["representative_example"]
        md_lines.append(
            f"- `{example['historical_label']}` ({example['field']}): records={example['record_count']}, valid={example['valid']}, number={example['questionable_number']}, split={example['questionable_split']}, partial={example['questionable_partial']}, unsupported={example['questionable_unsupported']}, unverifiable={example['unverifiable']}"
        )
        if rep["classification"] is not None:
            md_lines.append(f"  - Representative classification: `{rep['classification']}`")
            md_lines.append(f"  - Valid reason: {rep['valid_reason']}")
            md_lines.append(f"  - Reason: {rep['reason']}")
            md_lines.append(f"  - Source annotation values: {_json_dumps(rep['annotation_values'])}")
            md_lines.append(f"  - Matched words: {_json_dumps(rep['matched_words'])}")
            md_lines.append(f"  - Missing words: {_json_dumps(rep['missing_words'])}")
            md_lines.append(f"  - Support values: {_json_dumps(rep['support_values'])}")
            md_lines.append(f"  - Number support values: {_json_dumps(rep['number_support_values'])}")

    md_lines.extend(["", "## Recommendation", ""])
    md_lines.append(
        "The archive can be trusted for single-word labels and for multi-word labels that are VALID. The repairable cleanup target is the multi-word set that is QUESTIONABLE_NUMBER, QUESTIONABLE_SPLIT, or QUESTIONABLE_PARTIAL, starting with the highest-number and highest-questionable labels in the tables above. Do not relink or regenerate anything yet; the next phase should be a scoped repair plan over the labels with the highest ambiguous totals."
    )

    atomic_write_text(report_md, "\n".join(md_lines) + "\n")

    return report
