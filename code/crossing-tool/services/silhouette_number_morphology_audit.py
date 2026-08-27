"""Read-only morphology audit for historical silhouette number questions.

This audit consumes the completed silhouette number audit under
``outputs/tests/silhouette-number-audit/`` and reclassifies only the existing
``QUESTIONABLE_NUMBER`` rows using a strict phrase-level comparator backed by
``inflect``.

The audit is intentionally read-only with respect to project data:
- it never modifies ``data/silhouettes/``
- it never modifies ``data/annotations/``
- it never rebuilds indexes or provenance
- it only writes generated audit artifacts under
  ``outputs/tests/silhouette-number-morphology-audit/``
"""

from __future__ import annotations

import csv
import json
import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import inflect

from data.annotate import atomic_write_text


SOURCE_AUDIT_DIR = Path("outputs") / "tests" / "silhouette-number-audit"
MORPHOLOGY_AUDIT_DIR = Path("outputs") / "tests" / "silhouette-number-morphology-audit"

SOURCE_REPORT_NAME = "report.json"
SOURCE_PROVENANCE_NAME = "silhouette_number_provenance.csv"

REPORT_JSON_NAME = "report.json"
REPORT_MD_NAME = "report.md"
MORPHOLOGY_RECORDS_NAME = "morphology_records.csv"
MORPHOLOGY_LABELS_NAME = "morphology_labels.csv"

SOURCE_NUMBER_CLASSIFICATION = "QUESTIONABLE_NUMBER"

EXACT = "EXACT"
NUMBER_VARIANT = "NUMBER_VARIANT"
MORPHOLOGY_UNRESOLVED = "MORPHOLOGY_UNRESOLVED"
NUMBER_SEMANTICALLY_AMBIGUOUS = "NUMBER_SEMANTICALLY_AMBIGUOUS"
NOT_NUMBER_VARIANT = "NOT_NUMBER_VARIANT"

SINGULAR_TO_PLURAL = "silhouette_singular_annotation_plural"
PLURAL_TO_SINGULAR = "silhouette_plural_annotation_singular"
EXACT_DIRECTION = "exact"
UNKNOWN_DIRECTION = "unknown"
NOT_NUMBER_DIRECTION = "not_number"
MIXED_DIRECTION = "mixed"

SEMANTIC_EXCEPTION_TOKENS = frozenset({
    "pants",
    "trousers",
    "scissors",
    "clothes",
    "cattle",
    "glasses",
})

PHASE1_REQUIRED_PAIRS: tuple[tuple[str, str], ...] = (
    ("band", "bands"),
    ("glove", "gloves"),
    ("plank", "planks"),
    ("box", "boxes"),
    ("branch", "branches"),
    ("berry", "berries"),
    ("knife", "knives"),
    ("shelf", "shelves"),
    ("man", "men"),
    ("woman", "women"),
    ("person", "people"),
    ("foot", "feet"),
    ("tooth", "teeth"),
    ("mouse", "mice"),
    ("deer", "deer"),
    ("sheep", "sheep"),
)

PHASE1_EXCEPTION_WORDS: tuple[str, ...] = (
    "pants",
    "trousers",
    "scissors",
    "clothes",
    "cattle",
)

PHASE1_LIBRARY_PROBE: tuple[tuple[str, str], ...] = (
    ("pants", "pants"),
    ("pants", "pant"),
    ("trousers", "trouser"),
    ("scissors", "scissor"),
    ("clothes", "clothe"),
    ("cattle", "cattle"),
    ("glass", "glasses"),
)

EXPLICIT_CONTROL_EXAMPLES: tuple[dict[str, Any], ...] = (
    {
        "name": "yellow coat / coat",
        "label": "yellow coat",
        "annotation_value": "coat",
        "expected": NOT_NUMBER_VARIANT,
    },
    {
        "name": "black neckerchief / black gloves + green neckerchief",
        "label": "black neckerchief",
        "annotation_value": "black gloves",
        "expected": MORPHOLOGY_UNRESOLVED,
        "annotation_values": ["black gloves", "green neckerchief"],
    },
    {
        "name": "black glove / brown gloves",
        "label": "black glove",
        "annotation_value": "brown gloves",
        "expected": NOT_NUMBER_VARIANT,
    },
    {
        "name": "brown pants / pants",
        "label": "brown pants",
        "annotation_value": "pants",
        "expected": NOT_NUMBER_VARIANT,
    },
)


def _project_path(project_path: str | Path) -> Path:
    return Path(project_path)


def default_source_audit_dir(project_path: str | Path) -> Path:
    return _project_path(project_path) / SOURCE_AUDIT_DIR


def default_output_dir(project_path: str | Path) -> Path:
    return _project_path(project_path) / MORPHOLOGY_AUDIT_DIR


def _normalize_text(value: str | None) -> str:
    if value is None:
        return ""
    text = value.lower().replace("_", " ")
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _tokens(value: str | None) -> list[str]:
    text = _normalize_text(value)
    return text.split() if text else []


def _parse_json_list(raw: str | None, default: list[str] | None = None) -> list[str]:
    if default is None:
        default = []
    text = (raw or "").strip()
    if not text:
        return list(default)
    try:
        value = json.loads(text)
    except Exception:
        return list(default)
    if isinstance(value, list):
        return [str(item) for item in value]
    return list(default)


def _parse_json_dict(raw: str | None, default: dict[str, Any] | None = None) -> dict[str, Any]:
    if default is None:
        default = {}
    text = (raw or "").strip()
    if not text:
        return dict(default)
    try:
        value = json.loads(text)
    except Exception:
        return dict(default)
    if isinstance(value, dict):
        return value
    return dict(default)


def _json_text(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _format_percentage(value: float) -> str:
    return f"{value * 100:.2f}%"


def _input_snapshot(path: Path) -> dict[str, Any]:
    try:
        stat = path.stat()
    except FileNotFoundError:
        return {"path": str(path), "exists": False, "size": None, "mtime_ns": None}
    return {
        "path": str(path),
        "exists": True,
        "size": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
    }


def _pair_relation(
    left_token: str,
    right_token: str,
    engine: inflect.engine,
) -> dict[str, Any]:
    left = left_token.lower()
    right = right_token.lower()
    if left == right:
        return {
            "kind": EXACT,
            "direction": EXACT_DIRECTION,
            "singular_form": left,
            "plural_form": right,
            "compare_nouns": "eq",
            "semantic_ambiguity": False,
            "morphologically_clean": True,
        }

    compare_result = engine.compare_nouns(left, right)
    if compare_result == "p:p":
        return {
            "kind": MORPHOLOGY_UNRESOLVED,
            "direction": UNKNOWN_DIRECTION,
            "singular_form": "",
            "plural_form": "",
            "compare_nouns": str(compare_result),
            "semantic_ambiguity": False,
            "morphologically_clean": False,
        }
    if compare_result == "s:p":
        singular_form = left
        plural_form = right
        direction = SINGULAR_TO_PLURAL
    elif compare_result == "p:s":
        singular_form = right
        plural_form = left
        direction = PLURAL_TO_SINGULAR
    else:
        return {
            "kind": NOT_NUMBER_VARIANT,
            "direction": NOT_NUMBER_DIRECTION,
            "singular_form": "",
            "plural_form": "",
            "compare_nouns": str(compare_result),
            "semantic_ambiguity": False,
            "morphologically_clean": False,
        }

    semantic_ambiguity = singular_form in SEMANTIC_EXCEPTION_TOKENS or plural_form in SEMANTIC_EXCEPTION_TOKENS
    return {
        "kind": NUMBER_SEMANTICALLY_AMBIGUOUS if semantic_ambiguity else NUMBER_VARIANT,
        "direction": direction,
        "singular_form": singular_form,
        "plural_form": plural_form,
        "compare_nouns": str(compare_result),
        "semantic_ambiguity": semantic_ambiguity,
        "morphologically_clean": not semantic_ambiguity,
    }


def _phrase_comparison(
    silhouette_label: str,
    annotation_value: str,
    engine: inflect.engine,
) -> dict[str, Any]:
    silhouette_text = _normalize_text(silhouette_label)
    annotation_text = _normalize_text(annotation_value)
    silhouette_tokens = _tokens(silhouette_text)
    annotation_tokens = _tokens(annotation_text)

    if not silhouette_tokens or not annotation_tokens:
        return {
            "classification": NOT_NUMBER_VARIANT,
            "direction": NOT_NUMBER_DIRECTION,
            "reason": "missing tokens after normalization",
            "silhouette_tokens": silhouette_tokens,
            "annotation_tokens": annotation_tokens,
            "differing_token_positions": [],
            "singular_form": "",
            "plural_form": "",
            "morphology_library_result": {
                "compare_nouns": None,
                "pairs": [],
            },
            "morphologically_clean": False,
            "semantic_ambiguity": False,
        }

    if silhouette_text == annotation_text:
        return {
            "classification": EXACT,
            "direction": EXACT_DIRECTION,
            "reason": "normalized phrases are identical",
            "silhouette_tokens": silhouette_tokens,
            "annotation_tokens": annotation_tokens,
            "differing_token_positions": [],
            "singular_form": "",
            "plural_form": "",
            "morphology_library_result": {
                "compare_nouns": "eq",
                "pairs": [],
            },
            "morphologically_clean": True,
            "semantic_ambiguity": False,
        }

    if len(silhouette_tokens) != len(annotation_tokens):
        return {
            "classification": NOT_NUMBER_VARIANT,
            "direction": NOT_NUMBER_DIRECTION,
            "reason": "token count differs",
            "silhouette_tokens": silhouette_tokens,
            "annotation_tokens": annotation_tokens,
            "differing_token_positions": [],
            "singular_form": "",
            "plural_form": "",
            "morphology_library_result": {
                "compare_nouns": None,
                "pairs": [],
            },
            "morphologically_clean": False,
            "semantic_ambiguity": False,
        }

    differing_positions = [
        index
        for index, (left_token, right_token) in enumerate(zip(silhouette_tokens, annotation_tokens))
        if left_token != right_token
    ]
    if not differing_positions:
        return {
            "classification": EXACT,
            "direction": EXACT_DIRECTION,
            "reason": "normalized phrases are identical",
            "silhouette_tokens": silhouette_tokens,
            "annotation_tokens": annotation_tokens,
            "differing_token_positions": [],
            "singular_form": "",
            "plural_form": "",
            "morphology_library_result": {
                "compare_nouns": "eq",
                "pairs": [],
            },
            "morphologically_clean": True,
            "semantic_ambiguity": False,
        }

    pair_results: list[dict[str, Any]] = []
    for position in differing_positions:
        pair_results.append(_pair_relation(silhouette_tokens[position], annotation_tokens[position], engine))

    pair_kinds = {result["kind"] for result in pair_results}

    if pair_kinds <= {NUMBER_VARIANT, NUMBER_SEMANTICALLY_AMBIGUOUS}:
        directions = {result["direction"] for result in pair_results}
        if len(directions) == 1:
            direction = next(iter(directions))
        else:
            direction = MIXED_DIRECTION
        singular_forms = [result["singular_form"] for result in pair_results]
        plural_forms = [result["plural_form"] for result in pair_results]
        classification = NUMBER_VARIANT
        semantic_ambiguity = False
        if any(result["semantic_ambiguity"] for result in pair_results):
            classification = NUMBER_SEMANTICALLY_AMBIGUOUS
            semantic_ambiguity = True
        return {
            "classification": classification,
            "direction": direction,
            "reason": "all differing tokens are grammatical-number variants" if classification == NUMBER_VARIANT else "number relation involves a semantic exception",
            "silhouette_tokens": silhouette_tokens,
            "annotation_tokens": annotation_tokens,
            "differing_token_positions": differing_positions,
            "singular_form": singular_forms[0] if len(singular_forms) == 1 else singular_forms,
            "plural_form": plural_forms[0] if len(plural_forms) == 1 else plural_forms,
            "morphology_library_result": {
                "compare_nouns": [result["compare_nouns"] for result in pair_results],
                "pairs": pair_results,
            },
            "morphologically_clean": classification == NUMBER_VARIANT,
            "semantic_ambiguity": semantic_ambiguity,
        }

    if any(result["kind"] == NOT_NUMBER_VARIANT for result in pair_results):
        return {
            "classification": NOT_NUMBER_VARIANT,
            "direction": NOT_NUMBER_DIRECTION,
            "reason": "inflect explicitly rejected the differing tokens as a number relation",
            "silhouette_tokens": silhouette_tokens,
            "annotation_tokens": annotation_tokens,
            "differing_token_positions": differing_positions,
            "singular_form": "",
            "plural_form": "",
            "morphology_library_result": {
                "compare_nouns": [result["compare_nouns"] for result in pair_results],
                "pairs": pair_results,
            },
            "morphologically_clean": False,
            "semantic_ambiguity": False,
        }

    if any(result["kind"] == MORPHOLOGY_UNRESOLVED for result in pair_results):
        directions = {result["direction"] for result in pair_results if result["direction"] != EXACT_DIRECTION}
        direction = next(iter(directions)) if len(directions) == 1 else MIXED_DIRECTION if directions else UNKNOWN_DIRECTION
        return {
            "classification": MORPHOLOGY_UNRESOLVED,
            "direction": direction,
            "reason": "inflect could not establish a singular/plural relation for the differing tokens",
            "silhouette_tokens": silhouette_tokens,
            "annotation_tokens": annotation_tokens,
            "differing_token_positions": differing_positions,
            "singular_form": "",
            "plural_form": "",
            "morphology_library_result": {
                "compare_nouns": [result["compare_nouns"] for result in pair_results],
                "pairs": pair_results,
            },
            "morphologically_clean": False,
            "semantic_ambiguity": False,
        }

    if len(differing_positions) == 1:
        return {
            "classification": MORPHOLOGY_UNRESOLVED,
            "direction": UNKNOWN_DIRECTION,
            "reason": "single-token mismatch but inflect did not prove a number relation",
            "silhouette_tokens": silhouette_tokens,
            "annotation_tokens": annotation_tokens,
            "differing_token_positions": differing_positions,
            "singular_form": "",
            "plural_form": "",
            "morphology_library_result": {
                "compare_nouns": [result["compare_nouns"] for result in pair_results],
                "pairs": pair_results,
            },
            "morphologically_clean": False,
            "semantic_ambiguity": False,
        }

    return {
        "classification": NOT_NUMBER_VARIANT,
        "direction": NOT_NUMBER_DIRECTION,
        "reason": "token sequence differs in a way that is not a clean number variant",
        "silhouette_tokens": silhouette_tokens,
        "annotation_tokens": annotation_tokens,
        "differing_token_positions": differing_positions,
        "singular_form": "",
        "plural_form": "",
        "morphology_library_result": {
            "compare_nouns": [result["compare_nouns"] for result in pair_results],
            "pairs": pair_results,
        },
        "morphologically_clean": False,
        "semantic_ambiguity": False,
    }


def _best_candidate(
    silhouette_label: str,
    annotation_values: list[str],
    engine: inflect.engine,
) -> dict[str, Any]:
    candidates: list[dict[str, Any]] = []
    if not annotation_values:
        annotation_values = [""]
    for annotation_value in annotation_values:
        comparison = _phrase_comparison(silhouette_label, annotation_value, engine)
        comparison["annotation_value"] = annotation_value
        comparison["match_count"] = sum(
            1 for left_token, right_token in zip(comparison["silhouette_tokens"], comparison["annotation_tokens"])
            if left_token == right_token
        )
        comparison["difference_count"] = len(comparison["differing_token_positions"])
        comparison["rank"] = {
            EXACT: 0,
            NUMBER_VARIANT: 1,
            NUMBER_SEMANTICALLY_AMBIGUOUS: 2,
            MORPHOLOGY_UNRESOLVED: 3,
            NOT_NUMBER_VARIANT: 4,
        }[comparison["classification"]]
        candidates.append(comparison)

    candidates.sort(
        key=lambda item: (
            item["rank"],
            -item["match_count"],
            item["difference_count"],
            len(item["annotation_tokens"]),
            item["annotation_value"].casefold(),
        )
    )
    return candidates[0]


def _serialize_form(value: Any) -> str:
    if value in (None, ""):
        return ""
    if isinstance(value, list):
        if len(value) == 1:
            return str(value[0])
        return _json_text(value)
    return str(value)


def _serialize_csv_row(row: dict[str, Any]) -> dict[str, str]:
    return {
        "media_type": str(row["media_type"]),
        "media_id": str(row["media_id"]),
        "shot_id": str(row["shot_id"]),
        "frame": str(row["frame"]),
        "field": str(row["field"]),
        "silhouette_label": str(row["silhouette_label"]),
        "annotation_value": str(row["annotation_value"]),
        "existing_number_classification": str(row["existing_number_classification"]),
        "new_classification": str(row["new_classification"]),
        "direction": str(row["direction"]),
        "silhouette_tokens": _json_text(row["silhouette_tokens"]),
        "annotation_tokens": _json_text(row["annotation_tokens"]),
        "differing_token_positions": _json_text(row["differing_token_positions"]),
        "singular_form": _serialize_form(row["singular_form"]),
        "plural_form": _serialize_form(row["plural_form"]),
        "morphology_library": str(row["morphology_library"]),
        "morphology_library_result": _json_text(row["morphology_library_result"]),
        "morphologically_clean": str(row["morphologically_clean"]),
        "semantic_ambiguity": str(row["semantic_ambiguity"]),
        "reason": str(row["reason"]),
        "archive_json_path": str(row["archive_json_path"]),
        "archive_png_path": str(row["archive_png_path"]),
    }


@dataclass
class SummaryBucket:
    record_count: int = 0
    shots: set[str] = None  # type: ignore[assignment]
    number_variant: int = 0
    exact: int = 0
    unresolved: int = 0
    semantic_ambiguous: int = 0
    not_number_variant: int = 0

    def __post_init__(self) -> None:
        if self.shots is None:
            self.shots = set()

    @property
    def total_classified(self) -> int:
        return self.number_variant + self.exact + self.unresolved + self.semantic_ambiguous + self.not_number_variant

    @property
    def number_total(self) -> int:
        return self.number_variant + self.semantic_ambiguous

    @property
    def number_share(self) -> float:
        return (self.number_total / self.record_count) if self.record_count else 0.0


def _bucket_key(row: dict[str, Any]) -> tuple[str, str, str, str, str]:
    return (
        str(row["silhouette_label"]),
        str(row["annotation_value"]),
        str(row["field"]),
        str(row["new_classification"]),
        str(row["direction"]),
    )


def _pattern_for_row(row: dict[str, Any]) -> str:
    if row["new_classification"] == EXACT:
        return "exact"
    singular_form = row["singular_form"]
    plural_form = row["plural_form"]
    if isinstance(singular_form, list) and isinstance(plural_form, list):
        if len(singular_form) == len(plural_form) and singular_form:
            return " + ".join(f"{s}->{p}" for s, p in zip(singular_form, plural_form))
    if singular_form and plural_form:
        return f"{singular_form}->{plural_form}"
    return row["new_classification"].lower()


def _load_source_audit(source_audit_dir: Path) -> tuple[dict[str, Any], Path, Path]:
    report_path = source_audit_dir / SOURCE_REPORT_NAME
    provenance_path = source_audit_dir / SOURCE_PROVENANCE_NAME
    if not report_path.exists():
        raise FileNotFoundError(f"Missing source audit report: {report_path}")
    if not provenance_path.exists():
        raise FileNotFoundError(f"Missing source audit CSV: {provenance_path}")
    report = json.loads(report_path.read_text(encoding="utf-8"))
    return report, report_path, provenance_path


def _sample_controls(
    provenance_path: Path,
    engine: inflect.engine,
) -> list[dict[str, Any]]:
    targets = {
        "yellow coat": "QUESTIONABLE_PARTIAL",
        "black neckerchief": "QUESTIONABLE_SPLIT",
        "brown pants": "QUESTIONABLE_PARTIAL",
    }
    found: dict[str, dict[str, Any]] = {}
    with provenance_path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        for raw_row in reader:
            existing_classification = str(raw_row.get("classification") or "")
            if existing_classification not in {"QUESTIONABLE_SPLIT", "QUESTIONABLE_PARTIAL"}:
                continue
            label = _normalize_text(raw_row.get("historical_label"))
            if label not in targets or targets[label] != existing_classification:
                continue
            if label in found:
                continue
            annotation_values = _parse_json_list(raw_row.get("annotation_values"), [])
            candidate = _best_candidate(raw_row.get("historical_label") or "", annotation_values, engine)
            found[label] = {
                "label": raw_row.get("historical_label") or "",
                "annotation_value": candidate["annotation_value"],
                "existing_number_classification": existing_classification,
                "new_classification": candidate["classification"],
                "direction": candidate["direction"],
                "silhouette_tokens": candidate["silhouette_tokens"],
                "annotation_tokens": candidate["annotation_tokens"],
                "differing_token_positions": candidate["differing_token_positions"],
                "singular_form": candidate["singular_form"],
                "plural_form": candidate["plural_form"],
                "morphology_library": "inflect",
                "morphology_library_result": candidate["morphology_library_result"],
                "morphologically_clean": candidate["morphologically_clean"],
                "semantic_ambiguity": candidate["semantic_ambiguity"],
                "reason": candidate["reason"],
                "archive_json_path": raw_row.get("archive_json_path") or "",
                "archive_png_path": raw_row.get("archive_png_path") or "",
                "controls_from": existing_classification,
            }

    controls: list[dict[str, Any]] = []
    for label, expected_classification in targets.items():
        record = found.get(label)
        if record is None:
            controls.append({
                "label": label,
                "expected_source_classification": expected_classification,
                "found": False,
            })
            continue
        controls.append({
            "label": record["label"],
            "annotation_value": record["annotation_value"],
            "existing_number_classification": record["existing_number_classification"],
            "new_classification": record["new_classification"],
            "direction": record["direction"],
            "morphology_library_result": record["morphology_library_result"],
            "reason": record["reason"],
            "found": True,
            "archive_json_path": record["archive_json_path"],
            "archive_png_path": record["archive_png_path"],
        })
    return controls


def audit_silhouette_number_morphology(
    project_path: str | Path,
    *,
    source_audit_dir: str | Path | None = None,
    output_dir: str | Path | None = None,
) -> dict[str, Any]:
    """Run the strict phrase-level morphology audit and write its artifacts."""
    project = _project_path(project_path).resolve()
    source_dir = Path(source_audit_dir) if source_audit_dir is not None else default_source_audit_dir(project)
    target_dir = Path(output_dir) if output_dir is not None else default_output_dir(project)
    target_dir.mkdir(parents=True, exist_ok=True)

    source_report, report_path, provenance_path = _load_source_audit(source_dir)
    source_snapshot_before = {
        "report": _input_snapshot(report_path),
        "provenance": _input_snapshot(provenance_path),
    }

    engine = inflect.engine()
    library_version = getattr(inflect, "__version__", "unknown")

    total_questionable_number = int((source_report.get("classification") or {}).get("questionable_number", 0) or 0)
    input_rows: list[dict[str, Any]] = []
    records_by_classification = Counter()
    records_by_direction = Counter()
    records_by_field = Counter()
    records_by_word_count = Counter()
    records_by_pattern = Counter()
    records_by_label_value = Counter()
    by_label_summary: dict[tuple[str, str, str, str, str], SummaryBucket] = {}
    seen_shots_by_key: dict[tuple[str, str, str, str, str], set[str]] = defaultdict(set)
    number_variant_count = 0
    exact_count = 0
    unresolved_count = 0
    semantic_ambiguous_count = 0
    not_number_variant_count = 0

    sampled_controls = _sample_controls(provenance_path, engine)

    phase1_probe: list[dict[str, Any]] = []
    for left, right in PHASE1_REQUIRED_PAIRS:
        relation = _pair_relation(left, right, engine)
        phase1_probe.append({
            "left": left,
            "right": right,
            "compare_nouns": relation["compare_nouns"],
            "singular_form": relation["singular_form"],
            "plural_form": relation["plural_form"],
            "direction": relation["direction"],
            "semantic_ambiguity": relation["semantic_ambiguity"],
        })

    phase1_exceptions: list[dict[str, Any]] = []
    for word in PHASE1_EXCEPTION_WORDS:
        singular_to_plural = _pair_relation(word.rstrip("s"), word, engine)
        phase1_exceptions.append({
            "word": word,
            "compare_probe": singular_to_plural["compare_nouns"],
            "singular_form": singular_to_plural["singular_form"],
            "plural_form": singular_to_plural["plural_form"],
            "semantic_ambiguity": singular_to_plural["semantic_ambiguity"],
        })

    with provenance_path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        for raw_row in reader:
            existing_classification = str(raw_row.get("classification") or "")
            if existing_classification != SOURCE_NUMBER_CLASSIFICATION:
                continue

            annotation_values = _parse_json_list(raw_row.get("annotation_values"), [])
            selected = _best_candidate(raw_row.get("historical_label") or "", annotation_values, engine)
            record = {
                "media_type": raw_row.get("media_type") or "",
                "media_id": raw_row.get("media_id") or "",
                "shot_id": raw_row.get("shot_id") or "",
                "frame": int(raw_row.get("frame") or 0),
                "field": raw_row.get("field") or "",
                "silhouette_label": raw_row.get("historical_label") or "",
                "annotation_value": selected["annotation_value"],
                "existing_number_classification": existing_classification,
                "new_classification": selected["classification"],
                "direction": selected["direction"],
                "silhouette_tokens": selected["silhouette_tokens"],
                "annotation_tokens": selected["annotation_tokens"],
                "differing_token_positions": selected["differing_token_positions"],
                "singular_form": selected["singular_form"],
                "plural_form": selected["plural_form"],
                "morphology_library": "inflect",
                "morphology_library_result": selected["morphology_library_result"],
                "morphologically_clean": selected["morphologically_clean"],
                "semantic_ambiguity": selected["semantic_ambiguity"],
                "reason": selected["reason"],
                "archive_json_path": raw_row.get("archive_json_path") or "",
                "archive_png_path": raw_row.get("archive_png_path") or "",
                "source_annotation_values": annotation_values,
            }
            input_rows.append(record)

            classification = record["new_classification"]
            records_by_classification[classification] += 1
            records_by_direction[record["direction"]] += 1
            records_by_field[str(record["field"])] += 1
            word_count = len(_tokens(record["silhouette_label"]))
            records_by_word_count[str(word_count)] += 1
            pattern = _pattern_for_row(record)
            records_by_pattern[pattern] += 1
            label_value_key = (str(record["silhouette_label"]), str(record["annotation_value"]), str(record["field"]), classification, record["direction"])
            records_by_label_value[label_value_key] += 1

            bucket = by_label_summary.setdefault(label_value_key, SummaryBucket())
            bucket.record_count += 1
            bucket.shots.add(str(record["shot_id"]))
            if classification == EXACT:
                bucket.exact += 1
                exact_count += 1
            elif classification == NUMBER_VARIANT:
                bucket.number_variant += 1
                number_variant_count += 1
            elif classification == MORPHOLOGY_UNRESOLVED:
                bucket.unresolved += 1
                unresolved_count += 1
            elif classification == NUMBER_SEMANTICALLY_AMBIGUOUS:
                bucket.semantic_ambiguous += 1
                semantic_ambiguous_count += 1
            else:
                bucket.not_number_variant += 1
                not_number_variant_count += 1

            seen_shots_by_key[label_value_key].add(str(record["shot_id"]))

    if len(input_rows) != total_questionable_number:
        raise RuntimeError(
            f"Expected {total_questionable_number} QUESTIONABLE_NUMBER rows from the source audit, got {len(input_rows)}"
        )

    source_snapshot_after = {
        "report": _input_snapshot(report_path),
        "provenance": _input_snapshot(provenance_path),
    }

    source_inputs_same = source_snapshot_before == source_snapshot_after

    record_rows = [_serialize_csv_row(row) for row in input_rows]
    record_fieldnames = [
        "media_type",
        "media_id",
        "shot_id",
        "frame",
        "field",
        "silhouette_label",
        "annotation_value",
        "existing_number_classification",
        "new_classification",
        "direction",
        "silhouette_tokens",
        "annotation_tokens",
        "differing_token_positions",
        "singular_form",
        "plural_form",
        "morphology_library",
        "morphology_library_result",
        "morphologically_clean",
        "semantic_ambiguity",
        "reason",
        "archive_json_path",
        "archive_png_path",
    ]
    record_csv = target_dir / MORPHOLOGY_RECORDS_NAME
    with record_csv.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=record_fieldnames)
        writer.writeheader()
        writer.writerows(record_rows)

    label_rows: list[dict[str, Any]] = []
    for (silhouette_label, annotation_value, field, classification, direction), bucket in by_label_summary.items():
        label_rows.append({
            "silhouette_label": silhouette_label,
            "annotation_value": annotation_value,
            "field": field,
            "record_count": bucket.record_count,
            "unique_shots": len(bucket.shots),
            "new_classification": classification,
            "direction": direction,
            "morphology_pattern": _pattern_for_row({
                "new_classification": classification,
                "singular_form": [],
                "plural_form": [],
            }) if classification == EXACT else classification,
        })
    label_rows.sort(key=lambda row: (-int(row["record_count"]), str(row["silhouette_label"]).casefold(), str(row["annotation_value"]).casefold(), str(row["field"]).casefold(), str(row["new_classification"]), str(row["direction"])))
    label_csv = target_dir / MORPHOLOGY_LABELS_NAME
    label_fieldnames = [
        "silhouette_label",
        "annotation_value",
        "field",
        "record_count",
        "unique_shots",
        "new_classification",
        "direction",
        "morphology_pattern",
    ]
    with label_csv.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=label_fieldnames)
        writer.writeheader()
        for row in label_rows:
            writer.writerow({
                **row,
                "record_count": str(row["record_count"]),
                "unique_shots": str(row["unique_shots"]),
            })

    top_label_value_counts = sorted(records_by_label_value.items(), key=lambda item: (-item[1], item[0][0].casefold(), item[0][1].casefold(), item[0][2].casefold(), item[0][3], item[0][4]))
    total_main_records = len(input_rows)
    top_10_share = (sum(count for _, count in top_label_value_counts[:10]) / total_main_records) if total_main_records else 0.0
    top_50_share = (sum(count for _, count in top_label_value_counts[:50]) / total_main_records) if total_main_records else 0.0
    top_100_share = (sum(count for _, count in top_label_value_counts[:100]) / total_main_records) if total_main_records else 0.0

    report = {
        "project_path": str(project),
        "source_audit_dir": str(source_dir),
        "output_dir": str(target_dir),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "library": {
            "name": "inflect",
            "version": library_version,
        },
        "phase1": {
            "assessment": "YES WITH EXCEPTIONS",
            "required_pairs": phase1_probe,
            "exception_words": list(PHASE1_EXCEPTION_WORDS),
            "problematic_words": phase1_exceptions,
            "library_choice": "inflect",
        },
        "safety": {
            "source_inputs_same_before_after": source_inputs_same,
            "source_inputs_before": source_snapshot_before,
            "source_inputs_after": source_snapshot_after,
            "source_data_write_attempts": 0,
            "output_dir": str(target_dir),
        },
        "source_audit": {
            "questionable_number_records_examined": total_main_records,
            "source_questionable_number_records": total_questionable_number,
            "controls_sampled": sampled_controls,
        },
        "classification": {
            "total_existing_questionable_number_records_examined": total_main_records,
            "exact": exact_count,
            "number_variant": number_variant_count,
            "morphology_unresolved": unresolved_count,
            "number_semantically_ambiguous": semantic_ambiguous_count,
            "not_number_variant": not_number_variant_count,
        },
        "by_field": dict(records_by_field),
        "by_word_count": dict(records_by_word_count),
        "by_direction": dict(records_by_direction),
        "by_pattern": dict(records_by_pattern),
        "by_label_value": {
            f"{label} || {annotation_value} || {field} || {classification} || {direction}": count
            for (label, annotation_value, field, classification, direction), count in top_label_value_counts
        },
        "concentration": {
            "top_10_label_value_share": top_10_share,
            "top_50_label_value_share": top_50_share,
            "top_100_label_value_share": top_100_share,
        },
        "named_examples": {},
        "sample_controls": sampled_controls,
        "artifacts": {
            "report_json": str(target_dir / REPORT_JSON_NAME),
            "report_md": str(target_dir / REPORT_MD_NAME),
            "morphology_records_csv": str(record_csv),
            "morphology_labels_csv": str(label_csv),
        },
    }

    named_targets = [
        "arm band",
        "black glove",
        "wooden plank",
        "wagon wheel",
        "wooden beam",
        "wooden post",
        "wanted poster",
        "rocking chair",
    ]
    named_examples: dict[str, Any] = {}
    for target in named_targets:
        matches = [row for row in input_rows if _normalize_text(row["silhouette_label"]) == _normalize_text(target)]
        if not matches:
            named_examples[target] = {"found": False}
            continue
        bucket = defaultdict(int)
        shots = set()
        selected_rows = []
        for row in matches:
            bucket[row["new_classification"]] += 1
            shots.add(str(row["shot_id"]))
            selected_rows.append(row)
        representative = selected_rows[0]
        named_examples[target] = {
            "found": True,
            "record_count": len(matches),
            "unique_shots": len(shots),
            "direction": representative["direction"],
            "library_result": representative["morphology_library_result"],
            "final_classification": representative["new_classification"],
        }
    report["named_examples"] = named_examples

    report_json = target_dir / REPORT_JSON_NAME
    atomic_write_text(report_json, json.dumps(report, indent=2, ensure_ascii=False))

    md_lines = [
        "# Historical Silhouette Number Morphology Audit",
        "",
        f"Project: `{project}`",
        "",
        f"Source audit: `{source_dir}`",
        f"Output dir: `{target_dir}`",
        "",
        "## Phase 1",
        "",
        "Library: `inflect`",
        f"Assessment: **YES WITH EXCEPTIONS**",
        "",
        "Required pair probe (left -> right):",
        "",
    ]
    for row in phase1_probe:
        md_lines.append(
            f"- `{row['left']}` / `{row['right']}`: compare_nouns={row['compare_nouns']!r}, singular_form={row['singular_form']!r}, plural_form={row['plural_form']!r}, direction={row['direction']}, semantic_ambiguity={row['semantic_ambiguity']}"
        )
    md_lines.extend([
        "",
        "Exception words checked:",
        "",
    ])
    for row in phase1_exceptions:
        md_lines.append(
            f"- `{row['word']}`: compare_probe={row['compare_probe']!r}, singular_form={row['singular_form']!r}, plural_form={row['plural_form']!r}, semantic_ambiguity={row['semantic_ambiguity']}"
        )
    md_lines.extend([
        "",
        "## Corpus Results",
        "",
        f"Existing QUESTIONABLE_NUMBER records examined: **{total_main_records}**",
        f"EXACT: **{exact_count}**",
        f"NUMBER_VARIANT: **{number_variant_count}**",
        f"MORPHOLOGY_UNRESOLVED: **{unresolved_count}**",
        f"NUMBER_SEMANTICALLY_AMBIGUOUS: **{semantic_ambiguous_count}**",
        f"NOT_NUMBER_VARIANT: **{not_number_variant_count}**",
        "",
        "Direction counts:",
        "",
    ])
    for direction, count in sorted(records_by_direction.items(), key=lambda item: (-item[1], item[0])):
        md_lines.append(f"- `{direction}`: {count}")

    md_lines.extend([
        "",
        "Top label/value concentration:",
        "",
        f"- top 10 label/value relationships: {_format_percentage(top_10_share)}",
        f"- top 50 label/value relationships: {_format_percentage(top_50_share)}",
        f"- top 100 label/value relationships: {_format_percentage(top_100_share)}",
        "",
        "## Named Examples",
        "",
    ])
    for target in named_targets:
        example = named_examples.get(target, {"found": False})
        if not example.get("found"):
            md_lines.append(f"- `{target}`: not found")
            continue
        md_lines.append(
            f"- `{target}`: count={example['record_count']}, unique_shots={example['unique_shots']}, direction={example['direction']}, library_result={_json_text(example['library_result'])}, final_classification={example['final_classification']}"
        )

    md_lines.extend([
        "",
        "## Controls",
        "",
    ])
    for control in sampled_controls:
        if not control.get("found"):
            md_lines.append(f"- `{control['label']}`: not found in the source audit sample")
            continue
        md_lines.append(
            f"- `{control['label']}` vs `{control['annotation_value']}`: existing={control['existing_number_classification']}, new={control['new_classification']}, direction={control['direction']}, reason={control['reason']}"
        )

    md_lines.extend([
        "",
        "## Decision",
        "",
    ])
    if number_variant_count and (unresolved_count or semantic_ambiguous_count or not_number_variant_count):
        md_lines.append(
            "Library-backed strict phrase comparison supports QUESTIONABLE_NUMBER as a coherent future provenance subtype, but only with explicit semantic exceptions and with unresolved/negative outcomes kept separate from true number variants."
        )
    elif number_variant_count and not (unresolved_count or semantic_ambiguous_count or not_number_variant_count):
        md_lines.append(
            "Library-backed strict phrase comparison supports QUESTIONABLE_NUMBER as a coherent future provenance subtype."
        )
    else:
        md_lines.append(
            "Library-backed strict phrase comparison does not support QUESTIONABLE_NUMBER as a coherent future provenance subtype without further review."
        )

    report_md = target_dir / REPORT_MD_NAME
    atomic_write_text(report_md, "\n".join(md_lines) + "\n")

    return report
