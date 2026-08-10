"""Derived semantic vocabulary candidates extracted from shot descriptions.

This artifact is intentionally separate from the canonical structured vocabulary.
It never mutates annotations and is not used by exact vocabulary lookup or typed
lexical search.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
import json
from pathlib import Path
import re

from services.vocabulary_index import (
    _ann_dir,
    _compute_source_hash,
    lemmatize_noun_token,
)


CANDIDATE_NORMALIZATION_VERSION = "description_terms_v1"
_MIN_DOCUMENT_FREQUENCY = 2
_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z'-]{2,}")
_STOPWORDS = frozenset({
    "about", "after", "again", "against", "along", "also", "among", "another",
    "around", "because", "before", "behind", "being", "below", "between", "both",
    "could", "during", "each", "from", "front", "have", "into", "near", "other",
    "over", "same", "scene", "shot", "shows", "showing", "some", "than", "that",
    "their", "them", "then", "there", "these", "they", "this", "those", "through",
    "toward", "under", "very", "where", "while", "with", "would", "above", "across",
    "appears", "standing", "sitting", "looking", "camera", "view", "visible", "image",
})


def candidate_path(project_path: str, media_type: str = "movie") -> Path:
    return (
        Path(project_path) / "data" / "vocabulary"
        / f"description_candidates_{media_type}.json"
    )


def _description_text(annotation: dict) -> str:
    values = []
    for field in ("description", "caption"):
        value = annotation.get(field)
        if isinstance(value, str):
            values.append(value)
        elif isinstance(value, list):
            values.extend(str(item) for item in value if item is not None)
    return " ".join(values)


def _candidate_tokens(text: str) -> dict[str, set[str]]:
    candidates: dict[str, set[str]] = defaultdict(set)
    for match in _TOKEN_RE.finditer(text.lower()):
        alias = match.group(0).strip("'-")
        if len(alias) < 3 or alias in _STOPWORDS or alias.isnumeric():
            continue
        canonical = lemmatize_noun_token(alias)
        if canonical and canonical not in _STOPWORDS:
            candidates[canonical].add(alias)
    return candidates


def build_description_candidates(
    project_path: str,
    media_type: str = "movie",
    *,
    force: bool = False,
    min_document_frequency: int = _MIN_DOCUMENT_FREQUENCY,
) -> dict:
    """Build a reviewable description-derived candidate artifact."""
    output = candidate_path(project_path, media_type)
    source_hash = _compute_source_hash(project_path, media_type)
    if not force and output.exists():
        try:
            cached = json.loads(output.read_text(encoding="utf-8"))
            meta = cached.get("meta", {})
            if (
                meta.get("source_hash") == source_hash
                and meta.get("normalization") == CANDIDATE_NORMALIZATION_VERSION
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
            text = _description_text(annotation)
            if not text:
                continue
            documents_processed += 1
            document_candidates = _candidate_tokens(text)
            for canonical, surface_forms in document_candidates.items():
                counts[canonical] += 1
                aliases[canonical].update(surface_forms)

    candidates = {}
    for canonical, document_frequency in sorted(
        counts.items(), key=lambda item: (-item[1], item[0])
    ):
        if document_frequency < min_document_frequency:
            continue
        candidates[canonical] = {
            "document_frequency": document_frequency,
            "aliases": sorted(aliases[canonical]),
            "origin": "description",
            "source_field": "description",
            "normalization": CANDIDATE_NORMALIZATION_VERSION,
            "quality": min(1.0, 0.5 + 0.1 * (document_frequency - min_document_frequency)),
        }

    artifact = {
        "meta": {
            "built_at": datetime.now(timezone.utc).isoformat(),
            "media_type": media_type,
            "normalization": CANDIDATE_NORMALIZATION_VERSION,
            "min_document_frequency": min_document_frequency,
            "files_processed": files_processed,
            "documents_processed": documents_processed,
            "candidate_count": len(candidates),
            "source_hash": source_hash,
            "origin": "description",
        },
        "candidates": candidates,
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    from data.annotate import atomic_write_text
    atomic_write_text(output, json.dumps(artifact, indent=2, ensure_ascii=False) + "\n")
    return artifact


def load_description_candidates(
    project_path: str, media_type: str = "movie"
) -> dict:
    """Load the separate description candidate artifact."""
    return json.loads(candidate_path(project_path, media_type).read_text(encoding="utf-8"))
