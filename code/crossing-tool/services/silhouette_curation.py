"""Curation persistence helpers for the silhouette catalog.

Provides the persistence layer for human-best selection: marking, unmarking,
querying, and utility functions that the visualizer and tests can share.

The ``human_best`` field is written directly into the canonical object JSON
alongside all other scoring data.  When ``human_best`` is absent or ``False``
the object is not marked as human-selected; the field is removed rather than
set to ``False`` to keep JSONs backward compatible.
"""

from __future__ import annotations

from pathlib import Path
import json

HUMAN_BEST_FIELD = "human_best"


def label_bucket(label: str) -> str:
    """Return the alphabetical bucket key for *label*.

    Returns the uppercase first letter, or ``'#'`` for non-letter labels.

    >>> label_bucket("horse")
    'H'
    >>> label_bucket("123")
    '#'
    >>> label_bucket("")
    '#'
    """
    if not label:
        return "#"
    first = label[0].upper()
    return first if first.isalpha() else "#"


def set_human_best(json_path: str | Path, *, human_best: bool = True) -> None:
    """Write or remove the ``human_best`` flag in a catalog object JSON.

    When *human_best* is ``True``, ``"human_best": true`` is written.
    When ``False``, the field is removed entirely (backward compatible).

    Raises ``FileNotFoundError`` if the JSON file does not exist.
    """
    json_path = Path(json_path)
    data = json.loads(json_path.read_text(encoding="utf-8"))
    if human_best:
        data[HUMAN_BEST_FIELD] = True
    else:
        data.pop(HUMAN_BEST_FIELD, None)
    from data.annotate import atomic_write_text

    atomic_write_text(json_path, json.dumps(data, indent=2, ensure_ascii=False))


def clear_human_best_for_label(
    all_label_records: list[dict],
    except_path: str | Path | None = None,
) -> None:
    """Clear ``human_best`` from every record in a label group, optionally excluding one.

    Only records that currently have ``human_best: true`` are written to disk.
    In-memory record dicts are updated in place.

    Parameters
    ----------
    all_label_records:
        Full list of catalog records for the target label (across all films).
    except_path:
        JSON file path to leave untouched.  Pass the target record's path when
        transferring the best marker to a new object.
    """
    except_str = str(except_path) if except_path else None
    for rec in all_label_records:
        if not rec.get(HUMAN_BEST_FIELD):
            continue
        rec_path = rec.get("path")
        if rec_path is None:
            continue
        if except_str and str(rec_path) == except_str:
            continue
        json_path = Path(rec_path)
        if not json_path.exists():
            continue
        try:
            set_human_best(json_path, human_best=False)
            rec.pop(HUMAN_BEST_FIELD, None)
        except Exception:
            pass


def mark_best(target_rec: dict, all_label_records: list[dict]) -> bool:
    """Mark *target_rec* as human-best.

    Multiple objects in the same label group can be marked simultaneously —
    this call does NOT clear other records' ``human_best`` flags.
    Both in-memory and on-disk state are updated (best-effort).

    Returns ``True`` on success, ``False`` when the target JSON is invalid.
    """
    target_path = target_rec.get("path")
    if not target_path or not Path(target_path).exists():
        return False

    try:
        set_human_best(target_path, human_best=True)
        target_rec[HUMAN_BEST_FIELD] = True
        return True
    except Exception:
        return False


def unmark_best(rec: dict) -> bool:
    """Remove the ``human_best`` marker from *rec*.

    Updates both the in-memory dict and the on-disk JSON.
    Returns ``True`` if a marker was present (and removed), ``False`` otherwise.
    """
    if not rec.get(HUMAN_BEST_FIELD):
        return False
    json_path = Path(rec.get("path", ""))
    if not json_path.exists():
        return False
    try:
        set_human_best(json_path, human_best=False)
        rec.pop(HUMAN_BEST_FIELD, None)
        return True
    except Exception:
        return False


def find_best_in_records(records: list[dict]) -> int:
    """Return the index of the first record with ``human_best=True``, or ``-1``."""
    for i, rec in enumerate(records):
        if rec.get(HUMAN_BEST_FIELD):
            return i
    return -1
