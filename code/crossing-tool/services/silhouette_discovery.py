"""Canonical active-only Illustration-index access for MCP silhouette operations."""

from __future__ import annotations

import hashlib
import heapq
import math
from pathlib import Path
from typing import Any, Iterator

from services.illustration_index import ALL, ALL_MEDIA, MEDIA_TYPES, load_index, query_page
from services.silhouette_catalog import ASSIGNMENT_ACTIVE, catalog_base_dir


_DEFAULT_PAGE_LIMIT = 100
_MAX_PAGE_LIMIT = 250
_STREAM_PAGE_LIMIT = 250

_SORT_KEYS = {
    "catalog_order": [],
    "alphabetical": ["alphabetical"],
    "score": ["confidence"],
    "confidence": ["confidence"],
    "pixel_area": ["pixel_area"],
    "usefulness": ["usefulness"],
    "engraving": ["engraving"],
    "fullness": ["fullness"],
    "size": ["size"],
    "completeness": ["completeness"],
    "isolation": ["isolation"],
}


class ActiveSilhouetteIndexError(ValueError):
    """Raised when a normal MCP operation cannot use the active index."""

    def __init__(self, code: str, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}

    def payload(self) -> dict[str, Any]:
        return {
            "ok": False,
            "error": {
                "code": self.code,
                "stage": "ACTIVE_ILLUSTRATION_INDEX",
                "message": self.message,
                "details": self.details,
            },
        }


def _require_media_type(media_type: str) -> str:
    if media_type == "all":
        return ALL_MEDIA
    if media_type in MEDIA_TYPES or media_type == ALL_MEDIA:
        return media_type
    raise ActiveSilhouetteIndexError(
        "INVALID_MEDIA_TYPE",
        "media_type must be 'movie', 'gameplay', or '--all-media--'.",
        {"media_type": media_type},
    )


def _require_ready_index(project_path: str | Path, media_type: str) -> dict[str, Any]:
    status = load_index(project_path, "silhouettes", media_type)
    if status.get("status") == "ready":
        return status
    raise ActiveSilhouetteIndexError(
        "ILLUSTRATION_INDEX_NOT_READY",
        "Normal silhouette MCP operations require a ready Illustration index.",
        {
            "media_type": media_type,
            "index_status": status.get("status") or "unknown",
            "physical_record_count": status.get("count"),
        },
    )


def _parse_cursor(cursor: str | None) -> int:
    if cursor in (None, ""):
        return 0
    try:
        value = int(str(cursor))
    except ValueError as exc:
        raise ActiveSilhouetteIndexError(
            "INVALID_CURSOR",
            "cursor must be a non-negative integer offset returned by this tool.",
            {"cursor": cursor},
        ) from exc
    if value < 0:
        raise ActiveSilhouetteIndexError(
            "INVALID_CURSOR",
            "cursor must be a non-negative integer offset returned by this tool.",
            {"cursor": cursor},
        )
    return value


def _parse_limit(limit: int | None) -> int:
    try:
        value = int(limit or _DEFAULT_PAGE_LIMIT)
    except (TypeError, ValueError) as exc:
        raise ActiveSilhouetteIndexError(
            "INVALID_LIMIT",
            "limit must be a positive integer.",
            {"limit": limit},
        ) from exc
    if value < 1:
        raise ActiveSilhouetteIndexError(
            "INVALID_LIMIT",
            "limit must be a positive integer.",
            {"limit": limit},
        )
    return min(value, _MAX_PAGE_LIMIT)


def _parse_threshold(value: float | int | None, name: str) -> float | None:
    if value is None:
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise ActiveSilhouetteIndexError(
            "INVALID_THRESHOLD",
            f"{name} must be a finite non-negative number.",
            {name: value},
        ) from exc
    if not math.isfinite(parsed) or parsed < 0:
        raise ActiveSilhouetteIndexError(
            "INVALID_THRESHOLD",
            f"{name} must be a finite non-negative number.",
            {name: value},
        )
    return parsed


def _scope_media_id(scope: str | None) -> str | None:
    if scope in (None, "", "all", ALL):
        return None
    if scope.startswith("movie-") and len(scope) > len("movie-"):
        return scope[len("movie-"):]
    raise ActiveSilhouetteIndexError(
        "INVALID_SCOPE",
        "scope must be 'all' or 'movie-<media_id>'.",
        {"scope": scope},
    )


def _record_paths(project_path: str | Path, record: dict[str, Any]) -> tuple[Path, Path | None]:
    project = Path(project_path).resolve()
    record_media_type = str(record.get("media_type") or "")
    if record_media_type not in MEDIA_TYPES:
        raise ActiveSilhouetteIndexError(
            "INDEX_RECORD_INVALID",
            "Active Illustration index record has an invalid media type.",
            {"media_type": record_media_type},
        )
    catalog_root = catalog_base_dir(str(project), record_media_type).resolve()
    record_path = Path(record.get("path") or "").resolve()
    if record_path.suffix.lower() != ".json" or not record_path.is_relative_to(catalog_root):
        raise ActiveSilhouetteIndexError(
            "INDEX_RECORD_INVALID",
            "Active Illustration index record has an invalid catalog object path.",
            {"path": str(record.get("path") or "")},
        )
    png_name = str(record.get("png") or "")
    if not png_name:
        return record_path, None
    if Path(png_name).name != png_name:
        raise ActiveSilhouetteIndexError(
            "INDEX_RECORD_INVALID",
            "Active Illustration index record has an invalid silhouette PNG reference.",
            {"candidate_id": str(record_path.relative_to(project))},
        )
    png_path = (record_path.parent / png_name).resolve()
    if png_path.suffix.lower() != ".png" or not png_path.is_relative_to(catalog_root):
        raise ActiveSilhouetteIndexError(
            "INDEX_RECORD_INVALID",
            "Active Illustration index record has an invalid silhouette PNG path.",
            {"candidate_id": str(record_path.relative_to(project))},
        )
    return record_path, png_path


def candidate_reference(project_path: str | Path, record: dict[str, Any]) -> dict[str, Any]:
    """Return a stable public reference for an active indexed catalog object."""
    project = Path(project_path).resolve()
    record_path, png_path = _record_paths(project, record)
    return {
        "candidate_id": str(record_path.relative_to(project)),
        "object_id": str(record.get("object_id") or record_path.stem),
        "media_type": record.get("media_type"),
        "media_id": record.get("media_id"),
        "film_title": record.get("title") or record.get("filename_stem"),
        "film_filename": record.get("filename"),
        "shot_id": record.get("shot_id"),
        "field": record.get("field"),
        "word": record.get("label"),
        "frame_index": record.get("frame"),
        "bbox": record.get("bbox"),
        "score": record.get("confidence"),
        "mask_area": record.get("mask_area"),
        "silhouette_path": str(png_path.relative_to(project)) if png_path else None,
    }


def active_candidate_page(
    project_path: str | Path,
    *,
    word: str = "",
    field: str = "",
    scope: str = "all",
    media_type: str = "movie",
    sort_by: str = "catalog_order",
    descending: bool = False,
    min_pixel_area: float | int | None = None,
    min_score: float | int | None = None,
    limit: int | None = _DEFAULT_PAGE_LIMIT,
    cursor: str | None = None,
    shot_id: str | None = None,
    frame_index: int | None = None,
    candidate_id: str | None = None,
) -> dict[str, Any]:
    """Return one bounded page from the canonical active silhouette population."""
    resolved_media_type = _require_media_type(media_type)
    status = _require_ready_index(project_path, resolved_media_type)
    if sort_by not in _SORT_KEYS:
        raise ActiveSilhouetteIndexError(
            "INVALID_SORT",
            f"sort_by must be one of: {', '.join(sorted(_SORT_KEYS))}.",
            {"sort_by": sort_by},
        )
    offset = _parse_cursor(cursor)
    page_limit = _parse_limit(limit)
    scope_media_id = _scope_media_id(scope)
    result = query_page(
        project_path,
        "silhouettes",
        resolved_media_type,
        label=word or None,
        field=field or None,
        media_id=scope_media_id,
        shot_id=shot_id,
        frame_index=frame_index,
        record_path=candidate_id,
        assignment_state=ASSIGNMENT_ACTIVE,
        min_mask_area=_parse_threshold(min_pixel_area, "min_pixel_area"),
        min_confidence=_parse_threshold(min_score, "min_score"),
        sort_keys=_SORT_KEYS[sort_by],
        descending=bool(descending),
        offset=offset,
        limit=page_limit,
    )
    total = int(result.get("total", 0))
    records = list(result.get("records", []))
    next_offset = offset + len(records)
    return {
        "status": status.get("status"),
        "media_type": resolved_media_type,
        "word": word or None,
        "field": field or None,
        "scope": scope,
        "sort_by": sort_by,
        "descending": bool(descending),
        "min_pixel_area": _parse_threshold(min_pixel_area, "min_pixel_area"),
        "min_score": _parse_threshold(min_score, "min_score"),
        "total": total,
        "cursor": str(offset),
        "next_cursor": str(next_offset) if next_offset < total else None,
        "records": records,
    }


def list_active_candidate_references(
    project_path: str | Path,
    **filters: Any,
) -> dict[str, Any]:
    """Return one active-index page with stable, public candidate references."""
    page = active_candidate_page(project_path, **filters)
    records = page.pop("records")
    total = int(page.pop("total"))
    entries = [candidate_reference(project_path, record) for record in records]
    return {
        **page,
        "active_candidate_count": total,
        "returned_count": len(entries),
        "entries": entries,
    }


def iter_active_candidates(
    project_path: str | Path,
    **filters: Any,
) -> Iterator[dict[str, Any]]:
    """Yield active index records page by page without catalog traversal."""
    cursor: str | None = None
    while True:
        page = active_candidate_page(
            project_path,
            limit=_STREAM_PAGE_LIMIT,
            cursor=cursor,
            **filters,
        )
        yield from page["records"]
        cursor = page["next_cursor"]
        if cursor is None:
            return


def resolve_exact_active_candidate(
    project_path: str | Path,
    *,
    media_type: str,
    media_id: str,
    shot_id: str,
    frame_index: int,
    word: str,
    field: str,
    candidate_id: str | None = None,
    png_path: str | None = None,
) -> dict[str, Any]:
    """Resolve one exact active record entirely through the Illustration index."""
    resolved_media_type = _require_media_type(media_type)
    if resolved_media_type == ALL_MEDIA:
        raise ActiveSilhouetteIndexError(
            "INVALID_MEDIA_TYPE",
            "Exact candidate resolution requires a concrete media type.",
            {"media_type": media_type},
        )
    requested_png: Path | None = None
    if candidate_id:
        project = Path(project_path).resolve()
        expected_root = catalog_base_dir(str(project), resolved_media_type).resolve()
        requested_path = (project / candidate_id).resolve()
        if (
            requested_path.suffix.lower() != ".json"
            or not requested_path.is_relative_to(expected_root)
        ):
            raise ActiveSilhouetteIndexError(
                "CANDIDATE_PATH_INVALID",
                "candidate_id must resolve beneath the silhouette catalog root.",
                {"candidate_id": candidate_id},
            )
        candidate_id = str(requested_path.relative_to(project))
    if png_path:
        project = Path(project_path).resolve()
        expected_root = catalog_base_dir(str(project), resolved_media_type).resolve()
        requested_png = (project / png_path).resolve()
        if (
            requested_png.suffix.lower() != ".png"
            or not requested_png.is_relative_to(expected_root)
        ):
            raise ActiveSilhouetteIndexError(
                "CANDIDATE_PATH_INVALID",
                "png_path must resolve beneath the silhouette catalog root.",
                {"png_path": png_path},
            )

    page = active_candidate_page(
        project_path,
        word=word,
        field=field,
        scope=f"movie-{media_id}",
        media_type=resolved_media_type,
        shot_id=shot_id,
        frame_index=frame_index,
        candidate_id=candidate_id,
        limit=_MAX_PAGE_LIMIT,
    )
    records = page["records"]
    if requested_png is not None:
        records = [
            record for record in records
            if _record_paths(project_path, record)[1] == requested_png
        ]
    if len(records) == 1:
        return records[0]
    if not records:
        raise ActiveSilhouetteIndexError(
            "CANDIDATE_NOT_FOUND",
            "No active Illustration-index record matches every supplied exact selector.",
            {
                "candidate_id": candidate_id,
                "media_type": resolved_media_type,
                "media_id": media_id,
                "shot_id": shot_id,
                "frame_index": frame_index,
                "word": word,
                "field": field,
                "png_path": png_path,
            },
        )
    raise ActiveSilhouetteIndexError(
        "CANDIDATE_AMBIGUOUS",
        "Multiple active Illustration-index records match every supplied exact selector.",
        {"candidates": [candidate_reference(project_path, record) for record in records]},
    )


def select_largest_active_candidate(project_path: str | Path, **filters: Any) -> dict[str, Any] | None:
    """Return the largest exact active-index candidate for a discovery query."""
    best: dict[str, Any] | None = None
    best_key: tuple[float, str] | None = None
    for record in iter_active_candidates(project_path, **filters):
        reference = candidate_reference(project_path, record)
        try:
            area = float(record.get("mask_area") or 0)
        except (TypeError, ValueError):
            area = 0.0
        key = (area, reference["candidate_id"])
        if best_key is None or key > best_key:
            best, best_key = record, key
    return best


def _number(record: dict[str, Any], name: str) -> float:
    try:
        return float(record.get(name))
    except (TypeError, ValueError):
        return 0.0


def _ranking_score(record: dict[str, Any]) -> float:
    """Rank existing active metadata only; this function performs no inference."""
    return round(
        0.45 * _number(record, "usefulness_score")
        + 0.20 * _number(record, "confidence")
        + 0.15 * _number(record, "isolation_score")
        + 0.10 * _number(record, "completeness_score")
        + 0.10 * _number(record, "occlusion_score"),
        6,
    )


def rank_active_candidates(
    project_path: str | Path,
    *,
    limit: int = 20,
    **filters: Any,
) -> dict[str, Any]:
    """Rank active candidates from persisted deterministic metadata only."""
    result_limit = _parse_limit(limit)
    ranked = heapq.nlargest(
        result_limit,
        (
            (
                _ranking_score(record),
                candidate_reference(project_path, record)["candidate_id"],
                record,
            )
            for record in iter_active_candidates(project_path, **filters)
        ),
        key=lambda item: (item[0], item[1]),
    )
    candidates = []
    for rank, (rank_score, _candidate_id, record) in enumerate(ranked, start=1):
        item = candidate_reference(project_path, record)
        item["rank"] = rank
        item["rank_score"] = rank_score
        candidates.append(item)
    return {
        "ranking_method": "persisted_quality_metadata_v1",
        "count": len(candidates),
        "candidates": candidates,
    }


def _quantile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * fraction
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    if lower == upper:
        return round(ordered[lower], 6)
    return round(
        ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower),
        6,
    )


def summarize_active_catalog(
    project_path: str | Path,
    *,
    top_n: int = 10,
    **filters: Any,
) -> dict[str, Any]:
    """Summarize one active candidate population without returning all rows."""
    top_limit = _parse_limit(top_n)
    areas: list[float] = []
    film_ids: set[str] = set()
    shot_ids: set[tuple[str, str]] = set()
    top: list[tuple[float, str, dict[str, Any]]] = []
    total = 0
    for record in iter_active_candidates(project_path, **filters):
        total += 1
        film_ids.add(str(record.get("media_id") or record.get("filename_stem") or ""))
        shot_ids.add((str(record.get("media_id") or ""), str(record.get("shot_id") or "")))
        area = _number(record, "mask_area")
        areas.append(area)
        candidate_id = candidate_reference(project_path, record)["candidate_id"]
        if len(top) < top_limit:
            heapq.heappush(top, (area, candidate_id, record))
        elif (area, candidate_id) > (top[0][0], top[0][1]):
            heapq.heapreplace(top, (area, candidate_id, record))
    top_candidates = [
        candidate_reference(project_path, record)
        for _area, _candidate_id, record in sorted(top, reverse=True)
    ]
    return {
        "active_candidate_count": total,
        "distinct_active_films": len(film_ids),
        "distinct_active_shots": len(shot_ids),
        "mask_area": {
            "min": round(min(areas), 6) if areas else None,
            "max": round(max(areas), 6) if areas else None,
            "mean": round(sum(areas) / len(areas), 6) if areas else None,
            "p25": _quantile(areas, 0.25),
            "p50": _quantile(areas, 0.50),
            "p75": _quantile(areas, 0.75),
        },
        "top_candidates": top_candidates,
    }


def cluster_active_variants(
    project_path: str | Path,
    *,
    limit: int = 20,
    candidates_per_cluster: int = 20,
    **filters: Any,
) -> dict[str, Any]:
    """Cluster active records by existing semantic variant metadata only."""
    cluster_limit = _parse_limit(limit)
    per_cluster_limit = _parse_limit(candidates_per_cluster)
    groups: dict[tuple[str, str, str, str], list[dict[str, Any]]] = {}
    for record in iter_active_candidates(project_path, **filters):
        signature = (
            str(record.get("viewpoint") or "unknown"),
            str(record.get("completeness") or "unknown"),
            str(record.get("occlusion") or "unknown"),
            str(record.get("isolation") or "unknown"),
        )
        groups.setdefault(signature, []).append(record)

    clusters = []
    for signature, records in groups.items():
        ranked = sorted(
            records,
            key=lambda record: (
                _ranking_score(record),
                candidate_reference(project_path, record)["candidate_id"],
            ),
            reverse=True,
        )
        references = [candidate_reference(project_path, record) for record in ranked]
        film_ids = {
            str(record.get("media_id") or record.get("filename_stem") or "")
            for record in records
        }
        shot_ids = {
            (str(record.get("media_id") or ""), str(record.get("shot_id") or ""))
            for record in records
        }
        identity = "|".join(signature)
        clusters.append({
            "cluster_id": f"variant-{hashlib.sha256(identity.encode('utf-8')).hexdigest()[:16]}",
            "signature": {
                "viewpoint": signature[0],
                "completeness": signature[1],
                "occlusion": signature[2],
                "isolation": signature[3],
            },
            "active_candidate_count": len(records),
            "distinct_active_films": len(film_ids),
            "distinct_active_shots": len(shot_ids),
            "representative": references[0],
            "candidates": references[:per_cluster_limit],
            "candidates_truncated": len(references) > per_cluster_limit,
        })
    clusters.sort(
        key=lambda cluster: (
            -cluster["active_candidate_count"],
            cluster["cluster_id"],
        )
    )
    return {
        "clustering_method": "persisted_semantic_variant_metadata_v1",
        "cluster_count": len(clusters),
        "clusters": clusters[:cluster_limit],
        "clusters_truncated": len(clusters) > cluster_limit,
    }
