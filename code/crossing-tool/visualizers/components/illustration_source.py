"""IllustrationSource — data-access abstraction for IllustrationBrowser.

The Source separates *where illustrations come from* from *how they are
displayed and filtered*.  IllustrationBrowser only calls:

    source.reload(media_type)   — refresh the record list
    source.items()              — get the current records
    source.thumbnail_path(record)   — resolve the thumbnail PNG for one record

The Browser never calls ``scan_catalog``, walks directories, or reads JSON.

Built-in sources
----------------
SilhouetteSource
    Loads from the silhouette catalog via
    ``services.silhouette_catalog.scan_catalog``.

EngravingSource
    Walks ``data/engravings/catalog/<media_type>/`` and builds flat records
    from ``engraving.json`` sidecars.

Adding a new illustration type
-------------------------------
Subclass ``IllustrationSource`` and implement ``_load()`` and ``thumbnail_path()``.
Pass the new source to ``IllustrationBrowser`` at construction time.  No
browser code needs to change.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path
from typing import Optional


class IllustrationSource(ABC):
    """Abstract base class for illustration data sources.

    Subclasses implement ``_load()`` (bulk fetch for a given media type) and
    ``thumbnail_path()`` (thumbnail path for a single record).  The template method
    ``reload()`` calls ``_load()`` and caches the result so ``items()`` is
    always cheap.

    Parameters
    ----------
    project_path:
        Absolute path to the Crossing project directory.
    """

    def __init__(self, project_path: str) -> None:
        self._project_path = project_path
        self._records: list[dict] = []

    def reload(self, media_type: str = "movie") -> None:
        """Reload all records for *media_type*.

        Results are cached internally; call ``items()`` to retrieve them.
        """
        self._records = self._load(media_type)

    def items(self) -> list[dict]:
        """Return the current flat list of illustration records."""
        return list(self._records)

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
    """Loads illustration records from the silhouette catalog.

    Delegates to ``services.silhouette_catalog.scan_catalog`` which returns a
    flat list of object metadata dicts, each augmented with a ``path`` key
    pointing to the JSON sidecar file.  The sibling ``.png`` is the
    transparent extracted object.

    Sort support
    ------------
    Call ``set_sort_keys(keys)`` to change the multi-key sort order.  Sorting
    is applied to the cached records in-memory — no additional disk scan.
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
        """Sort the cached records by *sort_keys* without reloading from disk.

        Call this when the user changes the sort controls.  Follow with
        ``IllustrationBrowser.refresh()`` to display the new order.

        Parameters
        ----------
        sort_keys:
            Ordered list of sort-key strings from ``_SORT_OPTS``.  The first
            key is the primary sort.  Unrecognised keys are ignored.
            Defaults to ``["confidence"]`` when the list is empty.
        """
        self._sort_keys = list(sort_keys)   # empty list = no sort
        self._records = self._apply_sort(list(self._records))

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
            return []
        try:
            from services.silhouette_catalog import scan_catalog
            records = [
                r for r in scan_catalog(self._project_path, media_type=media_type)
                if "error" not in r
            ]
            return self._apply_sort(records)
        except Exception:
            return []

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
    """Loads illustration records from the engraving catalog.

    Walks ``data/engravings/catalog/<media_type>/`` recursively, reading every
    ``engraving.json`` sidecar.  Derives identity fields (label, filename_stem,
    mode, object_id) from the directory structure::

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
        self._all_eng_records: list[dict] = []

    # ------------------------------------------------------------------ mode filter

    def reload(self, media_type: str = "movie") -> None:
        """Reload from disk then re-apply the current mode filter."""
        self._all_eng_records = self._load(media_type)
        self._apply_mode_filter()

    def set_mode_filter(self, mode: Optional[str]) -> None:
        """Show only records whose ``mode`` field matches *mode*.

        Pass ``None`` or ``""`` to show all modes.
        """
        self._mode_filter = mode or None
        self._apply_mode_filter()

    def _apply_mode_filter(self) -> None:
        if self._mode_filter:
            self._records = [
                r for r in self._all_eng_records
                if r.get("mode") == self._mode_filter
            ]
        else:
            self._records = list(self._all_eng_records)

    def _on_records_loaded(self, items: list) -> None:
        """Called by IllustrationBrowser._on_catalog_loaded() after a background
        scan so the mode filter is applied before the browser updates _all_items.
        """
        self._all_eng_records = list(items)
        self._apply_mode_filter()

    def _load(self, media_type: str) -> list[dict]:
        import json as _json

        base = (
            Path(self._project_path)
            / "data" / "engravings" / "catalog"
            / media_type
        )
        if not base.is_dir():
            return []

        results: list[dict] = []
        for eng_json_path in sorted(base.rglob("engraving.json")):
            try:
                meta = _json.loads(eng_json_path.read_text(encoding="utf-8"))

                # Derive identity fields from the directory structure:
                #   catalog/<media_type>/<filename_stem>/<label>/<object_id>/<mode>/
                mode_dir      = eng_json_path.parent
                label_dir     = mode_dir.parent.parent
                film_dir      = label_dir.parent

                label         = label_dir.name
                filename_stem = film_dir.name
                mode          = mode_dir.name
                object_id     = mode_dir.parent.name

                raw_png_path = mode_dir / "raw.png"
                raw_png      = str(raw_png_path) if raw_png_path.exists() else ""

                # Prefer the named output PNG; fall back to raw.png
                output_png = str(meta.get("output_png", ""))
                if output_png and not Path(output_png).exists():
                    named = [
                        p for p in mode_dir.glob("*.png")
                        if p.name != "raw.png"
                    ]
                    output_png = str(named[0]) if named else raw_png

                # Use the explicit status field as the authoritative lifecycle state.
                # Only "generated" engravings have a viewable image.
                # Migration of legacy files (no status) happens transparently
                # inside read_engraving_meta when the status is first read.
                from services.engraving_paths import read_engraving_meta
                eng_meta = read_engraving_meta(eng_json_path)
                if (eng_meta or {}).get("status") != "generated":
                    continue

                record: dict = {
                    "label":          label,
                    "field":          "--all",   # no annotation taxonomy
                    "filename_stem":  filename_stem,
                    "media_type":     media_type,
                    "mode":           mode,
                    "object_id":      object_id,
                    "output_png":     output_png or raw_png,
                    "raw_png":        raw_png,
                    "path":           eng_json_path,
                }
                # Merge scalar fields from engraving.json (model, steps, etc.)
                for k, v in meta.items():
                    if k not in record and not isinstance(v, (dict, list)):
                        record[k] = v

                results.append(record)
            except Exception:
                continue

        return results

    def thumbnail_path(self, record: dict) -> Optional[Path]:
        """Return the best available output PNG for an engraving record."""
        for key in ("output_png", "raw_png"):
            val = record.get(key, "")
            if val:
                p = Path(str(val))
                if p.exists():
                    return p
        return None
