"""Film-level semantic title generation for flipbook front covers.

Generates a single "semantic condensation" title for an entire film using the
complete motif progression as input.  This is distinct from shot-level motif
generation (``data/motif.py``) — one title per film, not one motif per shot.

Storage schema
--------------
The title is stored in a dedicated per-film file at:

    <project>/data/film_titles/<media_type>/<stem>.json

Schema::

    {
        "value":         "carrying",
        "model":         "Qwen3-VL-8B-Instruct",
        "system_prompt": "title-system-2026-05-22-v1.txt",
        "user_prompt":   "title-user-2026-05-22-v1.txt",
        "generated_at":  "2026-05-22T14:30:00+00:00"
    }

This is separate from the per-shot motif data which lives inside the
canonical annotation JSON (``data/annotations/shots/.annotations.json``).

Prompt discovery
----------------
Prompts are loaded from:

    <project>/prompts/movies/motifs/

The module looks for ``title-system-*.txt`` and ``title-user-*.txt`` files,
selecting the most recent version by natural sort (same convention as
``data/motif.py``).  Explicit overrides can be passed at call time.

Prompt variables
----------------
``$title``            — original movie title (from metadata)
``$year``             — movie year
``$director``         — director name
``$overview``         — plot overview / synopsis
``$tagline``          — marketing tagline (may be empty)
``$duration``         — runtime in minutes
``$imdb``             — IMDb ID (e.g. tt0049475)
``$tmdb``             — TMDB numeric ID
``$title_candidates`` — newline-separated list of candidate fragments
                        extracted deterministically from the movie title
``$motif_history``    — complete ordered motif progression, one per line,
                        derived from the annotation JSON (``shot.motif``)

Normalization
-------------
Same light rules as shot motifs:
- First non-empty line
- Lowercase
- Normalize internal whitespace
- Strip trailing punctuation
"""

from __future__ import annotations

import json
import re
import string
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional, Tuple


# ---------------------------------------------------------------------------
# Candidate extraction
# ---------------------------------------------------------------------------

_ARTICLES: frozenset = frozenset({"a", "an", "the"})

_FILLERS: frozenset = frozenset({
    # conjunctions
    "and", "or", "but", "nor", "yet", "so",
    # common prepositions
    "in", "on", "at", "to", "by", "of", "for", "with", "from",
    "into", "onto", "upon", "under", "over", "through",
    "between", "against", "about", "above", "below",
    "behind", "before", "after", "during", "among", "around",
    # forms of "to be"
    "is", "are", "was", "were", "be", "been", "being", "am",
    # versus / other connectors
    "vs", "versus", "aka",
})

_NUMBER_WORDS: dict = {
    1: "one", 2: "two", 3: "three", 4: "four", 5: "five",
    6: "six", 7: "seven", 8: "eight", 9: "nine", 10: "ten",
    11: "eleven", 12: "twelve", 13: "thirteen", 14: "fourteen",
    15: "fifteen", 16: "sixteen", 17: "seventeen", 18: "eighteen",
    19: "nineteen", 20: "twenty",
}

_TOK_SPLIT = re.compile(r"[\s\-\u2013\u2014/:;!?\"'()\[\]]+")


def _normalize_token(raw: str) -> str:
    """Strip leading/trailing special chars and lowercase a single token."""
    t = raw.strip(string.punctuation + "$\xa3\u20ac# \t")
    t = t.replace(",", "").replace(".", "")
    return t.lower()


def _token_to_candidate(tok: str) -> str:
    """Convert a normalised token to its canonical candidate form.

    Small integers 1–20 are converted to English word form
    (``3`` → ``"three"``).  Larger integers are kept as digit strings.
    """
    try:
        n = int(tok)
        return _NUMBER_WORDS.get(n, tok)
    except ValueError:
        return tok


def extract_title_candidates(title: str) -> list:
    """Extract deterministic fragment candidates from an original movie title.

    Steps
    -----
    1. Tokenise on whitespace and common punctuation separators.
    2. Normalise each token (strip special chars, lowercase).
    3. Convert small integer tokens (1–20) to English word forms.
    4. Discard articles and filler words.
    5. Find consecutive runs of kept tokens in the original sequence;
       runs of length \u22652 become additional compound candidates.
    6. Return deduplicated, order-preserving list (singles first, then compounds).

    Examples::

        "A Bullet Is Waiting"          \u2192 ["bullet", "waiting"]
        "3 Godfathers"                 \u2192 ["three", "godfathers", "three godfathers"]
        "$10,000 for a Massacre"       \u2192 ["10000", "massacre"]
        "Bad Day at Black Rock"        \u2192 ["bad", "day", "black", "rock", "bad day", "black rock"]
        "Billy the Kid Versus Dracula" \u2192 ["billy", "kid", "dracula"]
        "Fort Massacre"                \u2192 ["fort", "massacre", "fort massacre"]
    """
    raw_tokens = [t for t in _TOK_SPLIT.split(title) if t]

    normalized = []
    for raw in raw_tokens:
        n = _normalize_token(raw)
        if n:
            normalized.append(_token_to_candidate(n))

    kept_mask = [
        t not in _ARTICLES and t not in _FILLERS and len(t) >= 1
        for t in normalized
    ]
    kept_tokens = [t for t, keep in zip(normalized, kept_mask) if keep]

    # Compound candidates: consecutive runs of kept tokens
    compounds: list = []
    run: list = []
    for tok, keep in zip(normalized, kept_mask):
        if keep:
            run.append(tok)
        else:
            if len(run) >= 2:
                compounds.append(" ".join(run))
            run = []
    if len(run) >= 2:
        compounds.append(" ".join(run))

    seen: set = set()
    result: list = []
    for t in kept_tokens + compounds:
        if t and t not in seen:
            seen.add(t)
            result.append(t)
    return result


def validate_generated_title(
    candidates: list,
    value: str,
) -> tuple:
    """Validate a model-generated title against the extracted candidate list.

    Returns ``(final_value, used_fallback)``.

    Acceptance rules (checked in order)
    ------------------------------------
    1. Exact match with any candidate \u2192 accept.
    2. Value is a single word that appears inside a compound candidate \u2192 accept.
    3. A single-word candidate appears in the value\u2019s word-set
       (model wrapped it in articles/prepositions) \u2192 collapse to that candidate.
    4. Fallback: choose the highest-scoring candidate.

    Scoring heuristic
    -----------------
    Non-numeric words score their character length.
    Numeric strings score 0 (prefer word residues over bare numbers).
    """
    if not candidates:
        return value or "\u2014", False

    if not value:
        return _best_candidate(candidates), True

    # 1. Exact match
    if value in candidates:
        return value, False

    # 2. Value is a word inside a compound candidate
    for c in candidates:
        if " " in c and value in c.split():
            return value, False

    # 3. Single-word candidate found in value’s words
    value_words = set(value.split())
    for c in candidates:
        if " " not in c and c in value_words:
            return c, True

    # 4. Fallback
    return _best_candidate(candidates), True


def _best_candidate(candidates: list) -> str:
    """Return the highest-scoring candidate from the list.

    Prefers longer non-numeric words; numeric strings score 0.
    """
    def _score(c: str) -> int:
        try:
            int(c)
            return 0
        except ValueError:
            return len(c)

    return max(candidates, key=_score) if candidates else "\u2014"


# ---------------------------------------------------------------------------
# Prompt discovery
# ---------------------------------------------------------------------------

def _natural_sort_key(p: Path) -> list:
    """Natural (version-aware) sort key for prompt filenames."""
    parts = re.split(r"(\d+)", p.name)
    return [int(x) if x.isdigit() else x for x in parts]


def find_latest_title_prompt(project_path: str, prefix: str) -> Optional[Path]:
    """Return the most recent ``title-{prefix}-*.txt`` prompt file.

    Searches ``<project>/prompts/movies/motifs/``.
    With ``prefix="system"`` matches ``title-system-*.txt``.
    With ``prefix="user"``   matches ``title-user-*.txt``.

    Returns ``None`` when the directory does not exist or no match is found.
    """
    d = Path(project_path) / "prompts" / "movie" / "motifs"
    if not d.exists() or not d.is_dir():
        return None
    pattern = f"title-{prefix}-*.txt"
    # Skip zero-byte files (often in-progress drafts that haven't been written yet)
    files = [p for p in d.glob(pattern) if p.is_file() and p.stat().st_size > 0]
    if not files:
        return None
    files.sort(key=_natural_sort_key, reverse=True)
    return files[0]


def load_title_prompts(
    project_path: str,
    system_prompt_file: Optional[str] = None,
    user_prompt_file: Optional[str] = None,
) -> Tuple[str, str, Optional[str], Optional[str]]:
    """Load system and user title prompts.

    Resolution order for each prompt:
      1. Explicit file path (absolute or relative to project root)
      2. Latest ``title-system-*.txt`` / ``title-user-*.txt``
         under ``prompts/movies/motifs/``
      3. Minimal built-in fallback

    Returns
    -------
    (system_text, user_text, system_filename, user_filename)
        Filenames are the bare filenames of the files actually loaded,
        or ``None`` when the built-in fallback is used.
    """
    # --- system prompt ---
    system_text: Optional[str] = None
    system_filename: Optional[str] = None

    if system_prompt_file:
        p = Path(system_prompt_file)
        if not p.exists():
            p = Path(project_path) / system_prompt_file
        if p.exists():
            system_text = p.read_text(encoding="utf-8")
            system_filename = p.name

    if system_text is None:
        latest = find_latest_title_prompt(project_path, prefix="system")
        if latest:
            system_text = latest.read_text(encoding="utf-8")
            system_filename = latest.name

    if system_text is None:
        system_text = (
            "You are a cinematic title condensation system. "
            "Generate a single semantic condensation title for the entire film. "
            "Output only the title, nothing else."
        )

    # --- user prompt ---
    user_text: Optional[str] = None
    user_filename: Optional[str] = None

    if user_prompt_file:
        p = Path(user_prompt_file)
        if not p.exists():
            p = Path(project_path) / user_prompt_file
        if p.exists():
            user_text = p.read_text(encoding="utf-8")
            user_filename = p.name

    if user_text is None:
        latest = find_latest_title_prompt(project_path, prefix="user")
        if latest:
            user_text = latest.read_text(encoding="utf-8")
            user_filename = latest.name

    if user_text is None:
        user_text = (
            "Original movie title:\n$title\n\n"
            "Year:\n$year\n\n"
            "Complete motif progression:\n$motif_history\n\n"
            "Generate a single semantic condensation title for this film."
        )

    return system_text, user_text, system_filename, user_filename


# ---------------------------------------------------------------------------
# Variable substitution
# ---------------------------------------------------------------------------

def _substitute_variables(template: str, variables: dict) -> str:
    """Replace ``$key`` placeholders with values.  Unknown keys → empty string."""
    for key, value in variables.items():
        template = template.replace(f"${key}", value or "")
    return template


# ---------------------------------------------------------------------------
# Normalization
# ---------------------------------------------------------------------------

def normalize_film_title(text: str) -> str:
    """Normalize a model-generated film title string.

    Rules (intentionally light — same as shot motif normalization):
    - Take the first non-empty line
    - Lowercase
    - Normalize internal whitespace
    - Strip trailing punctuation and whitespace

    Does NOT stem, split compound words, or rewrite vocabulary choices.
    """
    if not text:
        return ""
    for raw_line in text.strip().splitlines():
        line = raw_line.strip()
        # Skip preamble patterns emitted by thinking models
        if line.startswith("<") or line.startswith("**") or not line:
            continue
        line = line.lower()
        line = " ".join(line.split())
        line = line.rstrip(string.punctuation + " ")
        if line:
            return line
    return ""


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------

def get_film_title_path(project_path: str, filename: str, media_type: str) -> "Path":
    """Return the canonical path for the per-film title JSON file.

    ``<project>/data/film_titles/<media_type>/<stem>.json``
    """
    stem = Path(filename).stem
    return Path(project_path) / "data" / "film_titles" / media_type / f"{stem}.json"


def load_film_motif(
    project_path: str,
    filename: str,
    media_type: str,
) -> Optional[dict]:
    """Load the cached film-level title motif dict, or ``None`` if not yet generated.

    Reads from ``<project>/data/film_titles/<media_type>/<stem>.json``.
    """
    path = get_film_title_path(project_path, filename, media_type)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    return data if isinstance(data, dict) and data.get("value", "").strip() else None


def set_film_title(
    project_path: str,
    filename: str,
    media_type: str,
    value: str,
) -> dict:
    """Manually set the film title motif value, bypassing AI generation.

    Saves the value directly into the film title file as a manual override and
    returns the new title motif dict.
    """
    import datetime

    value = value.strip()
    if not value:
        raise ValueError("Title value must not be empty")

    title_motif = {
        "value": value,
        "model": "manual",
        "provenance": {
            "method": "manual_override",
            "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        },
    }
    path = get_film_title_path(project_path, filename, media_type)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(title_motif, indent=2, ensure_ascii=False), encoding="utf-8")
    return title_motif


# ---------------------------------------------------------------------------
# Single-film generation
# ---------------------------------------------------------------------------

def generate_film_title(
    project_path: str,
    filename: str,
    media_type: str = "movie",
    model_name: str = "Qwen3-VL-8B-Instruct",
    force: bool = False,
    verbose: bool = False,
    system_prompt_file: Optional[str] = None,
    user_prompt_file: Optional[str] = None,
    pipeline: Any = None,
) -> dict:
    """Generate and cache the semantic condensation title for one film.

    The title is derived from the complete shot motif progression stored in
    the annotation JSON.  It is cached under
    ``<project>/data/film_motifs/<media_type>/<stem>.json``.

    Parameters
    ----------
    project_path :      Project root directory.
    filename :          Video filename (e.g. ``"The Searchers (1956).mp4"``).
    media_type :        ``"movie"`` or ``"gameplay"``.
    model_name :        Model name / path for generation.
    force :             Regenerate even if cached title already exists.
    verbose :           Print progress lines.
    system_prompt_file: Override system prompt path.
    user_prompt_file :  Override user prompt path.
    pipeline :          Pre-loaded pipeline (avoids reloading for batch runs).

    Returns
    -------
    dict with keys matching the film_motif schema:
    ``value``, ``model``, ``system_prompt``, ``user_prompt``, ``generated_at``.

    Raises
    ------
    FileNotFoundError: If the annotation JSON does not exist.
    ValueError:        If no motifs are found in the annotation JSON.
    """
    from data.annotate import _load_text_generation_pipeline
    from data.metadata import get_metadata
    from data.motif import motif_history_text

    # Load the existing cached title (if any)
    existing_title = load_film_motif(project_path, filename, media_type)

    # Short-circuit if title already generated and not forcing
    if not force and isinstance(existing_title, dict) and existing_title.get("value", "").strip():
        if verbose:
            print(
                f"  skip  {Path(filename).stem}: "
                f"film title already exists ({existing_title['value']})"
            )
        return existing_title

    # Build motif history from annotation JSON
    from data.annotate import get_annotation_json_path
    import json as _json
    json_path = get_annotation_json_path(project_path, filename, media_type)
    if not json_path.exists():
        raise FileNotFoundError(
            f"No annotation JSON found: {json_path}\n"
            f"  Run: crossing annotate shot '{filename}' --media {media_type} first."
        )
    try:
        ann_entries: list = _json.loads(json_path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise ValueError(f"Cannot read annotation JSON: {exc}") from exc

    motif_history = motif_history_text(ann_entries)
    if not motif_history.strip():
        raise ValueError(
            f"No shot motifs found for '{filename}'.\n"
            f"  Run: crossing generate motif --movie '{filename}' first."
        )

    # Movie metadata
    meta_entries = get_metadata(project_path, media_type=media_type)
    meta = next((e for e in meta_entries if e.get("filename") == filename), {})
    title    = meta.get("title")    or Path(filename).stem
    year     = str(meta.get("year")      or "")
    director = str(meta.get("director")  or "")
    overview = str(meta.get("overview")  or "")
    tagline  = str(meta.get("tagline")   or "")
    duration = str(meta.get("duration")  or "")
    imdb     = str(meta.get("imdb")      or "")
    tmdb     = str(meta.get("tmdb")      or "")

    # Extract deterministic title fragment candidates
    candidates      = extract_title_candidates(title)
    candidates_text = "\n".join(candidates)

    # Load prompts
    system_text, user_text, system_filename, user_filename = load_title_prompts(
        project_path, system_prompt_file, user_prompt_file
    )

    # Substitute variables — all metadata + candidates available in both prompts
    _vars = {
        "title":            title,
        "year":             year,
        "director":         director,
        "overview":         overview,
        "tagline":          tagline,
        "duration":         duration,
        "imdb":             imdb,
        "tmdb":             tmdb,
        "title_candidates": candidates_text,
        "motif_history":    motif_history,
    }
    filled_system = _substitute_variables(system_text, _vars)
    filled_user   = _substitute_variables(user_text,   _vars)

    # Load pipeline if not provided
    if pipeline is None:
        if verbose:
            print(f"  Loading model '{model_name}'…")
        pipeline = _load_text_generation_pipeline(project_path, model_name)

    # Generate
    from data.annotate import _call_model
    messages = [
        {"role": "system", "content": filled_system},
        {"role": "user",   "content": filled_user},
    ]
    overrides = {"max_new_tokens": 32, "do_sample": False}
    _full, raw, _dev_log = _call_model(
        pipeline, messages, overrides=overrides, images=None
    )

    raw_value = normalize_film_title(raw) or normalize_film_title(_full) or ""
    value, used_fallback = validate_generated_title(candidates, raw_value)
    if not value:
        value = "—"

    film_motif = {
        "value":         value,
        "model":         model_name,
        "system_prompt": system_filename or "",
        "user_prompt":   user_filename or "",
        "generated_at":  (
            datetime.now(timezone.utc)
            .replace(microsecond=0)
            .isoformat()
        ),
        "candidates":    candidates,
        "raw_output":    raw_value,
        "fallback":      used_fallback,
    }

    # Write title to the dedicated film title file
    title_path = get_film_title_path(project_path, filename, media_type)
    title_path.parent.mkdir(parents=True, exist_ok=True)
    title_path.write_text(json.dumps(film_motif, indent=2, ensure_ascii=False), encoding="utf-8")

    if verbose:
        print(f"  → {title}: {value}")

    return film_motif


# ---------------------------------------------------------------------------
# Batch generation
# ---------------------------------------------------------------------------

def generate_film_titles_for_all_movies(
    project_path: str,
    media_type: str = "movie",
    model_name: str = "Qwen3-VL-8B-Instruct",
    *,
    force: bool = False,
    verbose: bool = False,
    system_prompt_file: Optional[str] = None,
    user_prompt_file: Optional[str] = None,
    on_item_done=None,
) -> dict:
    """Generate film titles for all movies that have motif-annotated JSONs.

    Pre-scans before loading the model so it can skip the model load
    entirely when nothing needs to be generated.

    Returns
    -------
    dict with ``processed``, ``skipped``, ``failed``.
    """
    from data.metadata import get_metadata
    from data.annotate import _load_text_generation_pipeline
    from data.motif import load_motif_words

    meta_entries = get_metadata(project_path, media_type=media_type)

    # Pre-scan: determine which films still need generation
    needs_work: list[str] = []
    skippable: list[str] = []
    for meta in meta_entries:
        fn = meta.get("filename")
        if not fn:
            continue
        # Skip if no shot motifs exist yet (nothing to summarise)
        if not load_motif_words(project_path, fn, media_type):
            continue
        if not force:
            existing_title = load_film_motif(project_path, fn, media_type)
            if isinstance(existing_title, dict) and existing_title.get("value", "").strip():
                skippable.append(fn)
                continue
        needs_work.append(fn)

    if not needs_work:
        print(
            "Nothing to generate — all films have titles. "
            "Use --force to regenerate."
        )
        return {"processed": 0, "skipped": len(skippable), "failed": 0}

    # Load model once for the entire batch
    print(f"Loading model '{model_name}'…")
    pipeline = _load_text_generation_pipeline(project_path, model_name)

    processed = 0
    failed    = 0
    for fn in needs_work:
        meta  = next((e for e in meta_entries if e.get("filename") == fn), {})
        title = meta.get("title") or fn
        print(f"  {title}…", end=" ", flush=True)
        try:
            result = generate_film_title(
                project_path, fn, media_type, model_name,
                force=force, verbose=False,
                system_prompt_file=system_prompt_file,
                user_prompt_file=user_prompt_file,
                pipeline=pipeline,
            )
            print(result["value"])
            processed += 1
            if on_item_done is not None:
                on_item_done(title, result, None)
        except Exception as exc:
            print(f"error: {exc}")
            failed += 1
            if on_item_done is not None:
                on_item_done(title, None, exc)

    return {
        "processed": processed,
        "skipped":   len(skippable),
        "failed":    failed,
    }
