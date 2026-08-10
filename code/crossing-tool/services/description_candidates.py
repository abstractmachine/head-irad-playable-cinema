"""Compatibility API for the derived vocabulary family.

New callers should use :mod:`services.derived_vocabulary`. This module keeps
the earlier description-candidate names available without creating a second
artifact family.
"""

from __future__ import annotations

from pathlib import Path

from services.derived_vocabulary import (
    DERIVED_NORMALIZATION_VERSION as CANDIDATE_NORMALIZATION_VERSION,
    _MIN_DOCUMENT_FREQUENCY,
    build_derived_vocabulary,
    derived_vocabulary_path,
    load_derived_vocabulary,
)


def candidate_path(project_path: str, media_type: str = "movie") -> Path:
    return derived_vocabulary_path(project_path, media_type)


def build_description_candidates(
    project_path: str,
    media_type: str = "movie",
    *,
    force: bool = False,
    min_document_frequency: int = _MIN_DOCUMENT_FREQUENCY,
) -> dict:
    """Compatibility alias for :func:`build_derived_vocabulary`."""
    return build_derived_vocabulary(
        project_path, media_type, force=force,
        min_document_frequency=min_document_frequency,
    )


def load_description_candidates(
    project_path: str, media_type: str = "movie"
) -> dict:
    """Compatibility alias for :func:`load_derived_vocabulary`."""
    return load_derived_vocabulary(project_path, media_type)
