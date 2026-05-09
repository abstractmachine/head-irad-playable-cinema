"""Build and load a per-field vocabulary index from annotation JSON files.

The index counts how many shots contain each token for the fields listed under
the ``vocabulary`` key in ``preferences/data/fields.yaml``.  Fields like
``description`` and ``text`` that contain free prose are excluded by design.

Vocabulary is independent from the semantic embedding index: it depends only
on raw annotation JSON and the project's field configuration.

Output path:
    <project>/data/index/vocabulary_<media_type>.json

Output schema::

    {
      "meta": {
        "built_at": "2026-05-08T12:00:00+00:00",
        "media_type": "movies",
        "vocabulary_fields": ["setting", "humans", "animals", ...],
        "files_processed": 314,
        "total_tokens": 12345,
        "source_hash": "abc123..."
      },
      "fields": {
        "animals": {"horse": 40648, "horses": 26793},
        "setting": {"saloon": 3400, "desert": 2100}
      }
    }

Token counts are per-shot (a token repeated in one shot's list counts once).

Cache invalidation
------------------
``source_hash`` is an MD5 of the mtime+size of every annotation JSON file in
sorted order.  ``vocabulary_cache_is_stale`` recomputes this cheaply (no file
reads) and returns True when anything has changed since the cache was built.
"""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

def _vocab_path(project_path: str, media_type: str) -> Path:
    return Path(project_path) / "data" / "index" / f"vocabulary_{media_type}.json"


def _ann_dir(project_path: str, media_type: str) -> Path:
    return Path(project_path) / "data" / "annotations" / "shots" / media_type


# ---------------------------------------------------------------------------
# Source hash (cheap staleness check — no file content reads)
# ---------------------------------------------------------------------------

def _compute_source_hash(project_path: str, media_type: str) -> str:
    """Return an MD5 hex digest based on the mtime+size of every annotation JSON."""
    ann = _ann_dir(project_path, media_type)
    if not ann.exists():
        return ""
    h = hashlib.md5()
    for p in sorted(ann.glob("*.json")):
        if p.name.endswith(".manifest.json"):
            continue
        st = p.stat()
        h.update(f"{p.name}:{st.st_mtime_ns}:{st.st_size}\n".encode())
    return h.hexdigest()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def vocabulary_cache_is_stale(project_path: str, media_type: str = "movies") -> bool:
    """Return True when the cache is missing or the annotation files have changed."""
    path = _vocab_path(project_path, media_type)
    if not path.exists():
        return True
    try:
        meta = json.loads(path.read_text(encoding="utf-8")).get("meta", {})
    except (OSError, json.JSONDecodeError):
        return True
    stored_hash = meta.get("source_hash", "")
    return stored_hash != _compute_source_hash(project_path, media_type)


def build_vocabulary_index(
    project_path: str,
    media_type: str = "movies",
    force: bool = False,
) -> dict:
    """Scan annotation JSON files and build a vocabulary index.

    Only fields listed under the ``vocabulary`` key in ``fields.yaml`` are
    indexed.  For fields that are also in the ``atomic`` list, comma-joined
    items are split and normalized using the same logic as annotation repair.

    Returns the index dict (also written to disk).
    Skips an up-to-date cache unless *force* is True.
    """
    out_path = _vocab_path(project_path, media_type)

    source_hash = _compute_source_hash(project_path, media_type)

    if not force and out_path.exists():
        try:
            cached = json.loads(out_path.read_text(encoding="utf-8"))
            if cached.get("meta", {}).get("source_hash") == source_hash:
                return cached
        except (OSError, json.JSONDecodeError):
            pass

    ann_dir_path = _ann_dir(project_path, media_type)
    if not ann_dir_path.exists():
        raise FileNotFoundError(f"Annotation directory not found: {ann_dir_path}")

    # --- load project configuration ---
    try:
        from data.index import load_vocabulary_fields, load_atomic_fields
        vocab_fields: List[str] = load_vocabulary_fields(project_path)
        atomic_fields: List[str] = load_atomic_fields(project_path)
    except FileNotFoundError:
        vocab_fields = []
        atomic_fields = []

    vocab_set  = frozenset(vocab_fields)
    atomic_set = frozenset(atomic_fields)

    if not vocab_set:
        # No allowlist configured — index nothing and return an empty cache.
        index = {
            "meta": {
                "built_at": datetime.now(timezone.utc).isoformat(),
                "media_type": media_type,
                "vocabulary_fields": [],
                "files_processed": 0,
                "total_tokens": 0,
                "source_hash": source_hash,
            },
            "fields": {},
        }
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(index, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        return index

    # For atomic fields we reuse the existing normalization logic.
    from data.annotate import normalize_label_list

    # field → token → shot count  (single pass)
    counts: Dict[str, Dict[str, int]] = {f: defaultdict(int) for f in vocab_fields}

    json_files = sorted(ann_dir_path.glob("*.json"))
    files_processed = 0

    for json_path in json_files:
        if json_path.name.endswith(".manifest.json"):
            continue
        try:
            data = json.loads(json_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            print(f"  ⚠  skipping {json_path.name}: {exc}")
            continue

        if not isinstance(data, list):
            continue

        files_processed += 1

        for entry in data:
            if not isinstance(entry, dict):
                continue
            shot_block = entry.get("shot")
            if not isinstance(shot_block, dict):
                continue
            ann = shot_block.get("annotation")
            if not isinstance(ann, dict):
                continue

            for field in vocab_fields:
                value = ann.get(field)
                if value is None:
                    continue

                # Normalise into a list of raw tokens
                if isinstance(value, list):
                    raw_tokens = [str(v).strip() for v in value if v is not None and str(v).strip()]
                elif isinstance(value, str):
                    stripped = value.strip()
                    raw_tokens = [stripped] if stripped else []
                else:
                    continue

                if not raw_tokens:
                    continue

                # For atomic fields, apply comma-split + dedup normalization.
                if field in atomic_set:
                    raw_tokens = normalize_label_list(raw_tokens, field, label_fields=atomic_set)

                # Count each unique token once per shot.
                for token in dict.fromkeys(raw_tokens):  # preserves order, deduplicates
                    if token:
                        counts[field][token] += 1

    # Sort each field by descending count.
    fields_sorted = {
        field: dict(sorted(counts[field].items(), key=lambda kv: -kv[1]))
        for field in vocab_fields
        if counts[field]
    }

    total_tokens = sum(len(tokens) for tokens in fields_sorted.values())

    index = {
        "meta": {
            "built_at": datetime.now(timezone.utc).isoformat(),
            "media_type": media_type,
            "vocabulary_fields": vocab_fields,
            "files_processed": files_processed,
            "total_tokens": total_tokens,
            "source_hash": source_hash,
        },
        "fields": fields_sorted,
    }

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(index, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    return index


def load_vocabulary_index(project_path: str, media_type: str = "movies") -> dict:
    """Load the vocabulary index from cache.

    Raises FileNotFoundError if the cache file is missing (stale is allowed —
    callers that need freshness should check ``vocabulary_cache_is_stale``).
    """
    path = _vocab_path(project_path, media_type)
    if not path.exists():
        raise FileNotFoundError(
            f"Vocabulary index not found for '{media_type}'. "
            f"Run: crossing index vocabulary --media {media_type}"
        )
    return json.loads(path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Convenience query API
# ---------------------------------------------------------------------------

def get_vocabulary_fields(project_path: str, media_type: str = "movies") -> List[str]:
    """Return the list of fields included in the vocabulary index.

    Reads ``vocabulary_fields`` from the cache meta when available, otherwise
    falls back to ``load_vocabulary_fields`` from the project config.

    Raises FileNotFoundError if neither the cache nor fields.yaml exists.
    """
    try:
        index = load_vocabulary_index(project_path, media_type)
        fields = index.get("meta", {}).get("vocabulary_fields")
        if isinstance(fields, list):
            return fields
    except FileNotFoundError:
        pass
    from data.index import load_vocabulary_fields
    return load_vocabulary_fields(project_path)


def get_vocabulary(
    field: str,
    project_path: str,
    media_type: str = "movies",
    sort: str = "alphabetical",
) -> List[dict]:
    """Return vocabulary entries for *field* as ``[{"value": str, "count": int}, ...]``.

    Parameters
    ----------
    field:        Annotation field name (must be in the vocabulary allowlist).
    project_path: Project root directory.
    media_type:   ``"movies"`` or ``"gameplay"``.
    sort:         ``"alphabetical"`` (default) or ``"count"`` (descending).

    Raises
    ------
    FileNotFoundError
        If the vocabulary cache does not exist.
    KeyError
        If *field* is not present in the cached index.
    """
    index = load_vocabulary_index(project_path, media_type)
    field_data = index.get("fields", {}).get(field)
    if field_data is None:
        available = sorted(index.get("fields", {}).keys())
        raise KeyError(
            f"Field '{field}' not in vocabulary index. "
            f"Available: {available}"
        )

    # field_data is {token: count} already sorted by count descending
    items = list(field_data.items())  # [(token, count), ...]

    if sort == "alphabetical":
        items.sort(key=lambda kv: kv[0].lower())

    return [{"value": token, "count": count} for token, count in items]

