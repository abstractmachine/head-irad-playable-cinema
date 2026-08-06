"""Build and load a per-field vocabulary index from annotation JSON files.

The index counts how many shots contain each *canonical* token for the fields
listed under the ``vocabulary`` key in ``preferences/data/fields.yaml``.  Fields
like ``description`` and ``text`` that contain free prose are excluded by design.

Singular/plural variants are collapsed into one canonical entry during the build
pass.  The original surface forms are preserved in the ``aliases`` list for
traceability.

Vocabulary is independent from the semantic embedding index: it depends only
on raw annotation JSON and the project's field configuration.

Output path:
    <project>/data/index/vocabulary_<media_type>.json

Output schema::

    {
      "meta": {
        "built_at": "2026-05-08T12:00:00+00:00",
        "media_type": "movie",
        "vocabulary_fields": ["setting", "humans", "animals", ...],
        "canonicalization": "noun_lemma_v1",
        "files_processed": 314,
        "total_tokens": 12345,
        "source_hash": "abc123..."
      },
      "fields": {
        "animals": {
          "horse": {"count": 67441, "aliases": ["horse", "horses"]},
          "wolf":  {"count": 123,   "aliases": ["wolf", "wolves"]}
        },
        "setting": {
          "desert landscape": {"count": 45808, "aliases": ["desert landscape"]}
        }
      }
    }

Token counts are per-shot (a token repeated in one shot's list counts once).
Counts are recorded against the *canonical* form; plural and singular variants
of the same noun are merged.

Canonicalization policy
-----------------------
* ``setting`` values are never altered (phrase-like, no canonicalization).
* ``description`` and ``text`` are excluded from the cache entirely.
* Single-word tokens in ``animals``, ``humans``, ``objects``, and ``wearing``
  are lemmatized conservatively using an explicit irregular table followed by
  a safe suffix rule (strip trailing ``-s`` when the preceding character is a
  consonant).
* Multi-word tokens (e.g. "desert landscape") are preserved as-is.

Cache invalidation
------------------
``source_hash`` is an MD5 of the mtime+size of every annotation JSON file in
sorted order.  ``vocabulary_cache_is_stale`` recomputes this cheaply (no file
reads) and returns True when anything has changed since the cache was built.

The ``canonicalization`` meta key acts as a version marker: a change to the
lemmatization logic increments the version string and forces a rebuild.
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List


# ---------------------------------------------------------------------------
# Canonicalization helpers
# ---------------------------------------------------------------------------

#: Version marker stored in the cache meta.  Bump this string whenever the
#: lemmatization logic changes so that stale caches are automatically rebuilt.
CANONICALIZATION_VERSION = "noun_lemma_v1"

#: Fields where conservative noun lemmatization is applied.
#: ``setting`` is intentionally absent — its values are phrase-like.
#: ``action``, ``description``, and ``text`` are also excluded.
_CANONICALIZE_FIELDS: frozenset[str] = frozenset(
    {"animals", "humans", "objects", "wearing"}
)

#: Explicit plural → singular overrides for irregular nouns.
#: Prefer conservative entries over speculative suffix rules.
_IRREGULAR_SINGULARS: dict[str, str] = {
    # Irregular inflections
    "children": "child",
    "men":      "man",
    "women":    "woman",
    "people":   "person",
    "geese":    "goose",
    "mice":     "mouse",
    "oxen":     "ox",
    # -ves / -f alternation
    "wolves":   "wolf",
    "calves":   "calf",
    # Common fauna / objects in the corpus
    "horses":    "horse",
    "cows":      "cow",
    "dogs":      "dog",
    "cats":      "cat",
    "mules":     "mule",
    "donkeys":   "donkey",
    "chickens":  "chicken",
    "rabbits":   "rabbit",
    "birds":     "bird",
    "goats":     "goat",
    "pigs":      "pig",
    "turkeys":   "turkey",
    "ducks":     "duck",
    "pronghorns": "pronghorn",
}

#: Word-endings that look like a plural ``-s`` but are not.
#: Words matching these suffixes are left unchanged by the suffix rule.
_FALSE_PLURAL_SUFFIXES: tuple[str, ...] = (
    "ss", "is", "us", "as", "os", "ious", "ous",
)


def normalize_token(text: str) -> str:
    """Collapse internal whitespace and strip leading/trailing whitespace."""
    return re.sub(r"\s+", " ", text.strip())


def lemmatize_noun_token(text: str) -> str:
    """Conservative singular-form lemmatizer for a single lowercase word.

    Priority:
    1. Explicit irregular plural table (``_IRREGULAR_SINGULARS``).
    2. Strip trailing ``-s`` when the character before ``s`` is a consonant
       (not a, e, i, o, u) and the word does not end in a known false-plural
       suffix.

    Multi-word strings are returned unchanged.  When neither rule applies the
    original *text* is returned.  Favours under-normalizing over over-normalizing.
    """
    word = text.lower()

    # Rule 1 — explicit irregular table.
    if word in _IRREGULAR_SINGULARS:
        return _IRREGULAR_SINGULARS[word]

    # Multi-word phrases: preserve as-is.
    if " " in word:
        return text

    # Rule 2 — conservative strip of trailing ``-s``.
    if any(word.endswith(sfx) for sfx in _FALSE_PLURAL_SUFFIXES):
        return text

    if len(word) > 3 and word.endswith("s") and word[-2] not in "aeiou":
        return word[:-1]

    return text


def canonicalize_vocabulary_value(field: str, value: str) -> str:
    """Return the canonical form of *value* for the given annotation *field*.

    * ``setting`` and other non-canonicalized fields → return *value* unchanged
      (after whitespace normalization).
    * Multi-word tokens → preserve as-is (phrase protection).
    * Single-word tokens in canonicalized fields → apply noun lemmatization.
    """
    normalized = normalize_token(value)
    if not normalized:
        return normalized

    if field not in _CANONICALIZE_FIELDS:
        return normalized

    # Preserve multi-word phrases (e.g. "wooden crates", "desert landscape").
    if " " in normalized:
        return normalized

    return lemmatize_noun_token(normalized)


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

def _vocab_path(project_path: str, media_type: str) -> Path:
    return Path(project_path) / "data" / "vocabulary" / f"vocabulary_{media_type}.json"


def _ann_dir(project_path: str, media_type: str) -> Path:
    return Path(project_path) / "data" / "annotations" / "shots" / media_type


# ---------------------------------------------------------------------------
# In-memory caches (process-lifetime)
# ---------------------------------------------------------------------------

# Parsed vocabulary JSON, keyed by (path_str, mtime_ns, size).  Self-invalidates
# when the file is updated on disk (e.g. after ``crossing index vocabulary``).
_vocab_index_cache: dict[tuple, dict] = {}

# Source-hash results, keyed by (project_path, media_type).  Refreshed at most
# once every _SOURCE_HASH_TTL seconds so switching annotation fields within a
# session does not re-scan the annotation directory every time.
_source_hash_cache: dict[tuple, tuple] = {}  # key → (hash_str, monotonic_ts)
_SOURCE_HASH_TTL: float = 10.0  # seconds


# ---------------------------------------------------------------------------
# Source hash (cheap staleness check — no file content reads)
# ---------------------------------------------------------------------------

def _compute_source_hash(project_path: str, media_type: str) -> str:
    """Return an MD5 hex digest based on the mtime+size of every annotation JSON.

    The result is cached for up to ``_SOURCE_HASH_TTL`` seconds so that
    switching between annotation fields in a running visualizer does not
    re-scan the annotation directory on every field change.
    """
    key = (project_path, media_type)
    cached = _source_hash_cache.get(key)
    if cached is not None:
        result, ts = cached
        if time.monotonic() - ts < _SOURCE_HASH_TTL:
            return result

    ann = _ann_dir(project_path, media_type)
    if not ann.exists():
        _source_hash_cache[key] = ("", time.monotonic())
        return ""
    h = hashlib.md5()
    for p in sorted(ann.glob("*.json")):
        if p.name.endswith(".manifest.json"):
            continue
        st = p.stat()
        h.update(f"{p.name}:{st.st_mtime_ns}:{st.st_size}\n".encode())
    digest = h.hexdigest()
    _source_hash_cache[key] = (digest, time.monotonic())
    return digest


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def vocabulary_cache_is_stale(project_path: str, media_type: str = "movie") -> bool:
    """Return True when the cache is missing or the annotation files have changed.

    Also returns True when the cache was built with an older canonicalization
    version (so a code upgrade automatically triggers a rebuild).

    Uses the in-memory index cache so the vocabulary file is not re-read when
    this is called immediately before ``load_vocabulary_index`` (as is the case
    in ``_vocabulary_from_cache`` in ``services/search.py``).
    """
    path = _vocab_path(project_path, media_type)
    if not path.exists():
        return True
    try:
        index = load_vocabulary_index(project_path, media_type)
        meta = index.get("meta", {})
    except (OSError, json.JSONDecodeError, FileNotFoundError):
        return True
    if meta.get("canonicalization") != CANONICALIZATION_VERSION:
        return True
    stored_hash = meta.get("source_hash", "")
    return stored_hash != _compute_source_hash(project_path, media_type)


def build_vocabulary_index(
    project_path: str,
    media_type: str = "movie",
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
            cached_meta = cached.get("meta", {})
            if (
                cached_meta.get("source_hash") == source_hash
                and cached_meta.get("canonicalization") == CANONICALIZATION_VERSION
            ):
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
                "canonicalization": CANONICALIZATION_VERSION,
                "files_processed": 0,
                "total_tokens": 0,
                "source_hash": source_hash,
            },
            "fields": {},
        }
        out_path.parent.mkdir(parents=True, exist_ok=True)
        from data.annotate import atomic_write_text
        atomic_write_text(out_path, json.dumps(index, indent=2, ensure_ascii=False) + "\n")
        return index

    # For atomic fields we reuse the existing normalization logic.
    from data.annotate import normalize_label_list

    # field → canonical_token → {"count": int, "aliases": set[str]}  (single pass)
    # Using a plain dict so we can merge plural/singular variants under one key.
    canonical_counts: Dict[str, Dict[str, dict]] = {f: {} for f in vocab_fields}

    json_files = sorted(ann_dir_path.glob("*.json"))
    files_processed = 0

    for json_path in json_files:
        if json_path.name.endswith(".manifest.json"):
            continue
        try:
            data = json.loads(json_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            print(f"  ⚠  skipping {json_path.name}: {exc}", file=sys.stderr)
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

                # Count each unique *canonical* token once per shot.
                # dict.fromkeys preserves order and deduplicates raw tokens.
                field_counts = canonical_counts[field]
                seen_canonicals: set[str] = set()
                for raw_token in dict.fromkeys(raw_tokens):
                    if not raw_token:
                        continue
                    canonical = canonicalize_vocabulary_value(field, raw_token)
                    if not canonical:
                        continue
                    # Each canonical form is counted at most once per shot.
                    if canonical in seen_canonicals:
                        # Still record the alias surface form.
                        if canonical in field_counts:
                            field_counts[canonical]["aliases"].add(raw_token)
                        continue
                    seen_canonicals.add(canonical)
                    if canonical not in field_counts:
                        field_counts[canonical] = {"count": 0, "aliases": set()}
                    field_counts[canonical]["count"] += 1
                    field_counts[canonical]["aliases"].add(raw_token)

    # Sort each field by descending count; convert aliases sets to sorted lists.
    fields_sorted = {
        field: {
            canonical: {
                "count": entry["count"],
                "aliases": sorted(entry["aliases"]),
            }
            for canonical, entry in sorted(
                field_counts.items(), key=lambda kv: -kv[1]["count"]
            )
        }
        for field, field_counts in canonical_counts.items()
        if field_counts
    }

    total_tokens = sum(len(tokens) for tokens in fields_sorted.values())

    index = {
        "meta": {
            "built_at": datetime.now(timezone.utc).isoformat(),
            "media_type": media_type,
            "vocabulary_fields": vocab_fields,
            "canonicalization": CANONICALIZATION_VERSION,
            "files_processed": files_processed,
            "total_tokens": total_tokens,
            "source_hash": source_hash,
        },
        "fields": fields_sorted,
    }

    out_path.parent.mkdir(parents=True, exist_ok=True)
    from data.annotate import atomic_write_text
    atomic_write_text(out_path, json.dumps(index, indent=2, ensure_ascii=False) + "\n")

    return index


def load_vocabulary_index(project_path: str, media_type: str = "movie") -> dict:
    """Load the vocabulary index from cache.

    Raises FileNotFoundError if the cache file is missing (stale is allowed —
    callers that need freshness should check ``vocabulary_cache_is_stale``).

    The parsed index is kept in a process-level in-memory cache keyed by the
    file's (path, mtime_ns, size).  Switching between annotation fields in a
    running visualizer will therefore hit memory after the first load.
    """
    path = _vocab_path(project_path, media_type)
    if not path.exists():
        raise FileNotFoundError(
            f"Vocabulary index not found for '{media_type}'. "
            f"Run: crossing index vocabulary --media {media_type}"
        )
    st = path.stat()
    cache_key = (str(path), st.st_mtime_ns, st.st_size)
    cached = _vocab_index_cache.get(cache_key)
    if cached is not None:
        return cached
    data = json.loads(path.read_text(encoding="utf-8"))
    _vocab_index_cache[cache_key] = data
    return data


# ---------------------------------------------------------------------------
# Convenience query API
# ---------------------------------------------------------------------------

def get_vocabulary_fields(project_path: str, media_type: str = "movie") -> List[str]:
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
    media_type: str = "movie",
    sort: str = "alphabetical",
) -> List[dict]:
    """Return vocabulary entries for *field* as ``[{"value": str, "count": int}, ...]``.

    Parameters
    ----------
    field:        Annotation field name (must be in the vocabulary allowlist).
    project_path: Project root directory.
    media_type:   ``"movie"`` or ``"gameplay"``.
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

    # field_data is {canonical_token: {"count": int, "aliases": [...]}}
    # sorted by count descending at build time.
    # Support both the new dict-entry format and the legacy flat {token: count} format.
    items: list[tuple[str, int]] = []
    for token, entry in field_data.items():
        if isinstance(entry, dict):
            count = entry.get("count", 0)
        else:
            # Legacy cache format: entry is an int count.
            count = int(entry)
        items.append((token, count))

    if sort == "alphabetical":
        items.sort(key=lambda kv: kv[0].lower())

    return [{"value": token, "count": count} for token, count in items]

