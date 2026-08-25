"""IllustrationSource — browser-facing data access boundary.

The source owns record retrieval. It separates where data comes from from how
it is browsed. The browser consumes records and never performs storage access.

IllustrationBrowser only calls:

    source.reload(media_type)   — refresh the record list
    source.items()              — get the current records
    source.thumbnail_path(record)   — resolve the thumbnail PNG for one record

The browser never calls ``scan_catalog``, walks directories, or reads JSON.

Built-in sources load compact, derived browse indexes. Missing or stale
indexes remain explicit and never trigger a hidden canonical-directory scan.

Adding a new illustration type
------------------------------
Create a new ``IllustrationSource`` subclass and implement ``_load()`` plus
``thumbnail_path()``. Pass that source into ``IllustrationBrowser``.
Do not modify browser internals for new data origins.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path
from typing import Optional


class IllustrationSource(ABC):
    """Abstract base class for visualizer record providers.

    Subclasses implement ``_load()`` (bulk fetch for a given media type) and
    ``thumbnail_path()`` (thumbnail path for a single record).  The template method
    ``reload()`` calls ``_load()`` and caches the result so ``items()`` is
    always cheap.

    Ownership
    ---------
    The source is the owner of record access and in-memory record cache.
    Browser, inspector, and tool panels read records through this API.

    Parameters
    ----------
    project_path:
        Absolute path to the Crossing project directory.
    """

    def __init__(self, project_path: str) -> None:
        self._project_path = project_path
        self._records: list[dict] = []
        self._load_status: dict = {"status": "missing"}
        self._media_type = "movie"
        self._provenance_state: Optional[str] = None

    def set_provenance_state(self, provenance_state: Optional[str]) -> None:
        """Filter silhouette catalog queries by provenance state.

        ``None`` means "All". The state is stored on the source so every
        indexed query path (facets, page, records, in-memory fallbacks) sees
        the same browse scope without the browser having to rescan JSON files.
        """
        self._provenance_state = provenance_state or None

    def reload(self, media_type: str = "movie") -> None:
        """Refresh index status without materializing the complete catalog."""
        self._media_type = media_type
        self._records = self._load(media_type)

    def items(self) -> list[dict]:
        """Return a snapshot of cached records.

        The returned list is a copy so callers can iterate safely without
        mutating source-owned storage.
        """
        return list(self._records)

    def load_status(self) -> dict:
        """Return status from the most recent index load."""
        return dict(self._load_status)

    def facets(self, **filters) -> dict:
        """Return indexed browse facets for the current media type."""
        from services.illustration_index import ALL

        records = self._filter_records(self._records, {
            **filters,
            "provenance_state": self._provenance_state,
        })
        title = filters.get("title")
        field = filters.get("field")
        letter = filters.get("letter")
        counts: dict[str, int] = {}
        for record in records:
            label = record.get("label", "")
            if label:
                counts[label] = counts.get(label, 0) + 1
        return {
            **self._load_status,
            "titles": sorted({_record_title(r) for r in self._records}, key=str.casefold),
            "fields": sorted({r.get("field") or ALL for r in records}, key=str.casefold),
            "letters": sorted({_record_initial(r) for r in records if r.get("label")}),
            "labels": [
                {"label": label, "count": count}
                for label, count in sorted(counts.items(), key=lambda item: item[0].casefold())
            ],
        }

    def page(self, **filters) -> dict:
        """Return one filtered page; custom in-memory sources use this fallback."""
        records = self._filter_records(
            self._records,
            {**filters, "provenance_state": self._provenance_state},
        )
        offset = max(0, int(filters.get("offset", 0)))
        limit = max(1, int(filters.get("limit", 50)))
        self._records = records[offset:offset + limit]
        return {**self._load_status, "total": len(records), "records": list(self._records)}

    def records(self, **filters) -> list[dict]:
        """Return a bounded action query."""
        return self._filter_records(
            self._records,
            {**filters, "provenance_state": self._provenance_state},
        )[:int(filters.get("limit", 100_000))]

    @staticmethod
    def _filter_records(records: list[dict], filters: dict) -> list[dict]:
        from services.illustration_index import ALL

        result = list(records)
        if filters.get("title"):
            result = [r for r in result if _record_title(r) == filters["title"]]
        if filters.get("field") not in (None, "", ALL):
            result = [r for r in result if (r.get("field") or ALL) == filters["field"]]
        if filters.get("letter") not in (None, "", ALL):
            result = [r for r in result if _record_initial(r) == filters["letter"]]
        if filters.get("label") not in (None, "", ALL):
            result = [r for r in result if r.get("label") == filters["label"]]
        provenance_state = filters.get("provenance_state")
        if provenance_state not in (None, "", ALL):
            result = [
                r for r in result
                if (r.get("search_provenance") or {}).get("state") == provenance_state
            ]
        return result

    @abstractmethod
    def _load(self, media_type: str) -> list[dict]:
        """Load and return all records for *media_type*.

        Called by ``reload()``.  Must not raise — return ``[]`` on failure.
        """

    @abstractmethod
    def thumbnail_path(self, record: dict) -> Optional[Path]:
        """Resolve the thumbnail PNG path for *record*.

        Returns the ``Path`` to a readable PNG, or ``None`` if unavailable.
        Used by ``ThumbnailLoader`` and by the grid for drag-and-drop payloads.
        """


# ---------------------------------------------------------------------------
# SilhouetteSource
# ---------------------------------------------------------------------------

class SilhouetteSource(IllustrationSource):
    """Source for silhouette catalog records.

    Loads the compact silhouette browse index. Each record includes a ``path``
    key pointing to its canonical JSON sidecar; the sibling ``.png`` is the
    transparent extracted object.

    Sort support
    ------------
    Call ``set_sort_keys(keys)`` to change the multi-key sort order.  Sorting
    is applied to source-owned cached records in-memory — no additional disk
    scan.
    The sort is preserved across ``reload()`` calls.

    Parameters
    ----------
    project_path:
        Absolute path to the Crossing project directory.
    """

    def __init__(self, project_path: str) -> None:
        super().__init__(project_path)
        self._sort_keys: list[str] = []   # empty = no sort (catalog scan order)

    # ------------------------------------------------------------------ sort

    def set_sort_keys(self, sort_keys: list[str]) -> None:
        """Set sort keys for the next worker-driven indexed reload.

        Call this when the user changes the sort controls, then reload the
        browser so large catalog sorts stay off the GUI thread.

        Parameters
        ----------
        sort_keys:
            Ordered list of sort-key strings from ``_SORT_OPTS``.  The first
            key is the primary sort.  Unrecognised keys are ignored.
            Defaults to ``["confidence"]`` when the list is empty.
        """
        self._sort_keys = list(sort_keys)   # empty list = no sort

    @staticmethod
    def _numeric_score(rec: dict, key: str) -> float:
        """Extract a numeric sort score for *key* from *rec*.

        Priority
        --------
        1. ``<key>_score`` field — pre-computed score stored by the pipeline.
        2. ``<key>`` field directly — raw value when the score field is absent.
        3. Derived computation for ``fullness`` and ``size``, which are often
           stored as raw geometry rather than pre-computed scores.
        4. Zero — safe fallback so sort is stable even for missing fields.
        """
        if key == "confidence":
            return float(rec.get("confidence") or 0.0)

        # Pre-computed score or direct field value
        v = rec.get(f"{key}_score")
        if v is None:
            v = rec.get(key)
        if v is not None:
            try:
                return float(v)
            except Exception:
                pass

        # Derived metrics — mirrors the display computation in CatalogBrowser
        if key == "fullness":
            mask_area = rec.get("mask_area")
            bbox = rec.get("bbox") or []
            if mask_area is not None and len(bbox) >= 4:
                bbox_area = float(max(1, bbox[2] * bbox[3]))
                return max(0.0, min(1.0, float(mask_area) / bbox_area))

        elif key == "size":
            mask_area = rec.get("mask_area")
            frame_size = rec.get("frame_size") or []
            if mask_area is not None and len(frame_size) >= 2:
                frame_area = float(max(1, frame_size[0] * frame_size[1]))
                area_frac = float(mask_area) / frame_area
                return max(0.0, min(1.0, (area_frac - 0.002) / max(1e-9, 0.298)))

        return 0.0

    def _apply_sort(self, records: list[dict]) -> list[dict]:
        """Return *records* sorted using a weighted average of all active keys.

        Each active sort key contributes equally to the final score:
        - 1 key selected  → 100 % weight
        - 2 keys selected →  50 % each
        - 3 keys selected →  33.3 % each

        The ``"alphabetical"`` key is treated separately: when it is the
        *only* active key the list is sorted alphabetically by label name.
        When mixed with numeric keys, alphabetical is ignored in the average
        (numeric relevance takes precedence).
        """
        active_keys = [k for k in (self._sort_keys or []) if k]
        if not active_keys:
            return list(records)   # "-----" / none — preserve catalog scan order

        # Special case: binary sort — records with an isolated engraving first.
        if "engraved_first" in active_keys:
            from services.engraving_paths import engraving_dir_for_source
            def _is_engraved(rec: dict) -> int:
                json_path = rec.get("path")
                if not json_path:
                    return 0
                try:
                    d = engraving_dir_for_source(
                        self._project_path, json_path, rec, mode="isolated"
                    )
                    return 1 if d.exists() else 0
                except Exception:
                    return 0
            return sorted(records, key=_is_engraved, reverse=True)
        numeric_keys = [k for k in active_keys if k != "alphabetical"]
        use_alpha_only = "alphabetical" in active_keys and not numeric_keys

        if use_alpha_only:
            return sorted(records, key=lambda r: str.casefold(r.get("label") or ""))

        if numeric_keys:
            n = len(numeric_keys)
            return sorted(
                records,
                key=lambda r: sum(self._numeric_score(r, k) for k in numeric_keys) / n,
                reverse=True,
            )

        return records

    # ------------------------------------------------------------------ IllustrationSource interface

    def _load(self, media_type: str) -> list[dict]:
        if not media_type:
            self._load_status = {"status": "missing"}
            return []
        from services.illustration_index import load_index

        self._load_status = load_index(self._project_path, "silhouettes", media_type)
        return []

    def facets(self, **filters) -> dict:
        from services.illustration_index import query_facets
        return query_facets(
            self._project_path, "silhouettes", self._media_type,
            provenance_state=self._provenance_state, **filters,
        )

    def page(self, **filters) -> dict:
        from services.illustration_index import query_page
        result = query_page(
            self._project_path, "silhouettes", self._media_type,
            provenance_state=self._provenance_state, sort_keys=self._sort_keys,
            **filters,
        )
        self._records = list(result.get("records", []))
        return result

    def records(self, **filters) -> list[dict]:
        from services.illustration_index import query_records
        return query_records(
            self._project_path, "silhouettes", self._media_type,
            provenance_state=self._provenance_state, sort_keys=self._sort_keys,
            **filters,
        )

    def thumbnail_path(self, record: dict) -> Optional[Path]:
        """Return the sibling PNG for a silhouette JSON sidecar record."""
        json_path = record.get("path")
        if not json_path:
            return None
        p = Path(str(json_path)).with_suffix(".png")
        return p if p.exists() else None


# ---------------------------------------------------------------------------
# EngravingSource
# ---------------------------------------------------------------------------

class EngravingSource(IllustrationSource):
    """Source for engraving catalog records.

    Loads the compact engraving browse index. Canonical assets retain this
    directory structure::

        catalog/<media_type>/<filename_stem>/<label>/<object_id>/<mode>/
            engraving.json
            raw.png
            <named-output>.png

    Parameters
    ----------
    project_path:
        Absolute path to the Crossing project directory.
    """

    def __init__(self, project_path: str) -> None:
        super().__init__(project_path)
        self._mode_filter: Optional[str] = None
        self._sort_keys: list[str] = []

    def set_sort_keys(self, sort_keys: list[str]) -> None:
        self._sort_keys = list(sort_keys)

    # ------------------------------------------------------------------ mode filter

    def reload(self, media_type: str = "movie") -> None:
        super().reload(media_type)

    def set_mode_filter(self, mode: Optional[str]) -> None:
        """Show only records whose ``mode`` field matches *mode*.

        Pass ``None`` or ``""`` to show all modes.
        Filtering is a source concern; the browser remains source-agnostic.
        """
        self._mode_filter = mode or None

    def _load(self, media_type: str) -> list[dict]:
        if not media_type:
            self._load_status = {"status": "missing"}
            return []
        from services.illustration_index import load_index

        self._load_status = load_index(self._project_path, "engravings", media_type)
        return []

    def facets(self, **filters) -> dict:
        from services.illustration_index import query_facets
        return query_facets(
            self._project_path, "engravings", self._media_type,
            mode=self._mode_filter, **filters,
        )

    def page(self, **filters) -> dict:
        from services.illustration_index import query_page
        result = query_page(
            self._project_path, "engravings", self._media_type,
            mode=self._mode_filter, sort_keys=self._sort_keys, **filters,
        )
        self._records = list(result.get("records", []))
        return result

    def records(self, **filters) -> list[dict]:
        from services.illustration_index import query_records
        return query_records(
            self._project_path, "engravings", self._media_type,
            mode=self._mode_filter, sort_keys=self._sort_keys, **filters,
        )

    def thumbnail_path(self, record: dict) -> Optional[Path]:
        """Return the best available output PNG for an engraving record."""
        for key in ("output_png", "raw_png"):
            val = record.get(key, "")
            if val:
                p = Path(str(val))
                if p.exists():
                    return p
        return None


def _record_title(record: dict) -> str:
    import re
    return re.sub(
        r"\s*\{tmdb-\d+\}", "", str(record.get("filename_stem", ""))
    ).strip()


def _record_initial(record: dict) -> str:
    label = str(record.get("label") or "")
    return label[:1].upper() if label[:1].isalpha() else "#"
