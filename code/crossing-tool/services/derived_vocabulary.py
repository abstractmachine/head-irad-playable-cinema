"""Build and query the derived free-text vocabulary family.

Derived vocabulary is intentionally separate from canonical structured annotation
vocabulary. It indexes normalized terms from free-text fields for browse and
review only; it neither mutates annotations nor changes lexical or semantic
retrieval behavior.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
import json
from pathlib import Path
import re

from services.vocabulary_index import _ann_dir, _compute_source_hash, lemmatize_noun_token


DERIVED_NORMALIZATION_VERSION = "free_text_terms_v4"
DERIVED_FIELD = "description"
_MIN_DOCUMENT_FREQUENCY = 2
_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z'-]{2,}|[\u3400-\u9fff]+")
_FREE_TEXT_FIELDS = ("description", "text")
_STOPWORDS = frozenset({
    "about", "after", "again", "against", "along", "also", "among", "another",
    "around", "because", "before", "behind", "being", "below", "between", "both",
    "could", "during", "each", "from", "front", "have", "into", "near", "other",
    "over", "same", "scene", "shot", "shows", "showing", "some", "than", "that",
    "their", "them", "then", "there", "these", "they", "this", "those", "through",
    "toward", "under", "very", "where", "while", "with", "would", "above", "across",
    "appears", "standing", "sitting", "looking", "camera", "view", "visible", "image",
})


def derived_vocabulary_path(project_path: str, media_type: str = "movie") -> Path:
    return Path(project_path) / "data" / "vocabulary" / f"vocabulary-{media_type}-derived.json"


def _tokens(text: str) -> dict[str, set[str]]:
    terms: dict[str, set[str]] = defaultdict(set)
    for match in _TOKEN_RE.finditer(text.lower()):
        alias = match.group(0).strip("'-")
        if (alias.isascii() and len(alias) < 3) or alias in _STOPWORDS or alias.isnumeric():
            continue
        canonical = lemmatize_noun_token(alias)
        if canonical and canonical not in _STOPWORDS:
            terms[canonical].add(alias)
    return terms


def build_derived_vocabulary(
    project_path: str,
    media_type: str = "movie",
    *,
    force: bool = False,
    min_document_frequency: int = _MIN_DOCUMENT_FREQUENCY,
) -> dict:
    """Build the derived free-text vocabulary artifact."""
    output = derived_vocabulary_path(project_path, media_type)
    source_hash = _compute_source_hash(project_path, media_type)
    if not force and output.exists():
        try:
            cached = json.loads(output.read_text(encoding="utf-8"))
            meta = cached.get("meta", {})
            if (
                meta.get("source_hash") == source_hash
                and meta.get("normalization") == DERIVED_NORMALIZATION_VERSION
                and meta.get("min_document_frequency") == min_document_frequency
            ):
                return cached
        except (OSError, json.JSONDecodeError):
            pass

    annotation_dir = _ann_dir(project_path, media_type)
    if not annotation_dir.exists():
        raise FileNotFoundError(f"Annotation directory not found: {annotation_dir}")

    counts: dict[str, int] = defaultdict(int)
    aliases: dict[str, set[str]] = defaultdict(set)
    source_fields: dict[str, set[str]] = defaultdict(set)
    files_processed = 0
    documents_processed = 0
    for annotation_path in sorted(annotation_dir.glob("*.json")):
        if annotation_path.name.endswith(".manifest.json"):
            continue
        try:
            entries = json.loads(annotation_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(entries, list):
            continue
        files_processed += 1
        for entry in entries:
            shot = entry.get("shot") if isinstance(entry, dict) else None
            annotation = shot.get("annotation") if isinstance(shot, dict) else None
            if not isinstance(annotation, dict):
                continue
            document_terms: dict[str, set[str]] = defaultdict(set)
            document_fields: dict[str, set[str]] = defaultdict(set)
            for field in _FREE_TEXT_FIELDS:
                value = annotation.get(field)
                values = [value] if isinstance(value, str) else value if isinstance(value, list) else []
                for raw in values:
                    if raw is None:
                        continue
                    for canonical, surfaces in _tokens(str(raw)).items():
                        document_terms[canonical].update(surfaces)
                        document_fields[canonical].add(field)
            if not document_terms:
                continue
            documents_processed += 1
            for canonical, surfaces in document_terms.items():
                counts[canonical] += 1
                aliases[canonical].update(surfaces)
                source_fields[canonical].update(document_fields[canonical])

    terms = {
        canonical: {
            "count": frequency,
            "document_frequency": frequency,
            "aliases": sorted(aliases[canonical]),
            "origin": "derived",
            "source_fields": sorted(source_fields[canonical]),
            "normalization": DERIVED_NORMALIZATION_VERSION,
            "quality": min(1.0, 0.5 + 0.1 * (frequency - min_document_frequency)),
        }
        for canonical, frequency in sorted(counts.items(), key=lambda item: (-item[1], item[0]))
        if frequency >= min_document_frequency
    }
    artifact = {
        "meta": {
            "built_at": datetime.now(timezone.utc).isoformat(),
            "media_type": media_type,
            "family": "derived",
            "free_text_fields": list(_FREE_TEXT_FIELDS),
            "normalization": DERIVED_NORMALIZATION_VERSION,
            "min_document_frequency": min_document_frequency,
            "files_processed": files_processed,
            "documents_processed": documents_processed,
            "total_tokens": len(terms),
            "source_hash": source_hash,
            "provenance": "free_text",
        },
        "fields": {DERIVED_FIELD: terms},
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    from data.annotate import atomic_write_text
    atomic_write_text(output, json.dumps(artifact, indent=2, ensure_ascii=False) + "\n")
    return artifact


def load_derived_vocabulary(project_path: str, media_type: str = "movie") -> dict:
    return json.loads(derived_vocabulary_path(project_path, media_type).read_text(encoding="utf-8"))


def derived_vocabulary_is_stale(project_path: str, media_type: str = "movie") -> bool:
    try:
        index = load_derived_vocabulary(project_path, media_type)
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return True
    meta = index.get("meta", {})
    return (
        meta.get("normalization") != DERIVED_NORMALIZATION_VERSION
        or meta.get("source_hash") != _compute_source_hash(project_path, media_type)
    )


def get_derived_vocabulary(
    project_path: str,
    media_type: str = "movie",
    sort: str = "alphabetical",
    source_field: str | None = None,
) -> list[dict]:
    """Return derived terms, optionally limited to one free-text source field."""
    fields = load_derived_vocabulary(project_path, media_type).get("fields", {})
    items = [
        {"value": term, "count": int(entry.get("count", 0)), "provenance": entry}
        for term, entry in fields.get(DERIVED_FIELD, {}).items()
        if source_field is None or source_field in entry.get("source_fields", [])
    ]
    if sort == "alphabetical":
        items.sort(key=lambda item: item["value"].casefold())
    elif sort == "count":
        items.sort(key=lambda item: -item["count"])
    elif sort == "count_alphabetical":
        items.sort(key=lambda item: (-item["count"], item["value"].casefold()))
    return items
