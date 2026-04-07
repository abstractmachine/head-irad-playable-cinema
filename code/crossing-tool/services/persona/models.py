"""Data models for persona detection output.

These dataclasses represent the structured output of the persona detection
pipeline. They are intentionally minimal for v1 and designed to support
future validation, merging, splitting, and archetype annotation.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class PersonaAppearance:
    """One confirmed appearance of a persona in a specific shot."""

    shot_id: int
    confidence: float
    visibility: str          # 'foreground' | 'mid' | 'background'
    bbox: list[int]          # [x1, y1, x2, y2] of the representative detection
    frame_index: int         # absolute frame index used for this appearance
    embedding_norm: float    # L2 norm of the embedding before normalization


@dataclass
class PersonaCluster:
    """An anonymous recurring human figure clustered across shots."""

    persona_id: str          # e.g. 'p_001'
    label: Optional[str]     # always None in v1
    shots: list[int]
    shot_count: int
    first_shot: int
    last_shot: int
    cluster_confidence: float        # mean pairwise intra-cluster similarity [0..1]
    ambiguous_with: list[str]        # persona_ids that came close to merging
    appearances: list[PersonaAppearance]
    notes: Optional[str]             # always None in v1


@dataclass
class MovieInfo:
    tmdb_id: int
    title: str
    year: Optional[int]
    filename: str


@dataclass
class SourceInfo:
    video_path: str
    shotlist_path: str


@dataclass
class DetectorInfo:
    version: str
    method: str
    generated_at: str
    frames_per_shot: int
    cluster_threshold: float
    min_shots_to_be_persona: int


@dataclass
class PersonaDocument:
    """Top-level output document for one movie's persona detection run."""

    movie: MovieInfo
    source: SourceInfo
    detector: DetectorInfo
    personas: list[PersonaCluster]
    stats: dict                  # summary stats for quick inspection
