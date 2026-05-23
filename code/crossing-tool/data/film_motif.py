"""Film-level semantic title generation for flipbook front covers.

Generates a single "semantic condensation" title for an entire film using the
complete motif progression as input.  This is distinct from shot-level motif
generation (``data/motif.py``) — one title per film, not one motif per shot.

Storage schema
--------------
The title is cached as a standalone JSON file:

    <project>/data/film_motifs/<media_type>/<stem>.json

Schema::

    {
        "value":         "carrying",
        "model":         "Qwen3-VL-8B-Instruct",
        "system_prompt": "title-system-2026-05-22-v1.txt",
        "user_prompt":   "title-user-2026-05-22-v1.txt",
        "generated_at":  "2026-05-22T14:30:00+00:00"
    }

Prompt discovery
----------------
Prompts are loaded from:

    <project>/prompts/movies/motifs/

The module looks for ``title-system-*.txt`` and ``title-user-*.txt`` files,
selecting the most recent version by natural sort (same convention as
``data/motif.py``).  Explicit overrides can be passed at call time.

Prompt variables
----------------
``$title``         — original movie title (from metadata)
``$year``          — movie year
``$motif_history`` — complete ordered motif progression, one per line,
                     derived from the shot annotation JSON

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
    d = Path(project_path) / "prompts" / "movies" / "motifs"
    if not d.exists() or not d.is_dir():
        return None
    pattern = f"title-{prefix}-*.txt"
    files = [p for p in d.glob(pattern) if p.is_file()]
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
# Cache path helpers
# ---------------------------------------------------------------------------

def get_film_motif_path(project_path: str, filename: str, media_type: str) -> Path:
    """Return the canonical cache path for a film motif JSON.

    ``<project>/data/film_motifs/<media_type>/<stem>.json``
    """
    stem = Path(filename).stem
    return Path(project_path) / "data" / "film_motifs" / media_type / f"{stem}.json"


def load_film_motif(
    project_path: str,
    filename: str,
    media_type: str,
) -> Optional[dict]:
    """Load a cached film motif dict, or ``None`` if not yet generated."""
    path = get_film_motif_path(project_path, filename, media_type)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Single-film generation
# ---------------------------------------------------------------------------

def generate_film_title(
    project_path: str,
    filename: str,
    media_type: str = "movies",
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
    media_type :        ``"movies"`` or ``"gameplay"``.
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
    from data.annotate import get_annotation_json_path, _load_text_generation_pipeline
    from data.metadata import get_metadata
    from data.motif import motif_history_text

    # Short-circuit if already cached and not forcing
    cache_path = get_film_motif_path(project_path, filename, media_type)
    if cache_path.exists() and not force:
        try:
            existing = json.loads(cache_path.read_text(encoding="utf-8"))
            if existing.get("value", "").strip():
                if verbose:
                    print(
                        f"  skip  {Path(filename).stem}: "
                        f"film title already exists ({existing['value']})"
                    )
                return existing
        except Exception:
            pass  # fall through to regenerate

    # Load annotation entries
    json_path = get_annotation_json_path(project_path, filename, media_type)
    if not json_path.exists():
        raise FileNotFoundError(
            f"No annotation JSON found: {json_path}\n"
            f"  Run: crossing annotate shot --movie '{filename}' first."
        )
    entries: list = json.loads(json_path.read_text(encoding="utf-8"))

    # Build motif history from annotations
    motif_history = motif_history_text(entries)
    if not motif_history.strip():
        raise ValueError(
            f"No motifs found in annotation JSON for '{filename}'.\n"
            f"  Run: crossing generate motif --movie '{filename}' first."
        )

    # Movie metadata
    meta_entries = get_metadata(project_path, media_type=media_type)
    meta = next((e for e in meta_entries if e.get("filename") == filename), {})
    title = meta.get("title") or Path(filename).stem
    year  = str(meta.get("year") or "")

    # Load prompts
    system_text, user_text, system_filename, user_filename = load_title_prompts(
        project_path, system_prompt_file, user_prompt_file
    )

    # Substitute variables
    filled_user = _substitute_variables(user_text, {
        "title":         title,
        "year":          year,
        "motif_history": motif_history,
    })

    # Load pipeline if not provided
    if pipeline is None:
        if verbose:
            print(f"  Loading model '{model_name}'…")
        pipeline = _load_text_generation_pipeline(project_path, model_name)

    # Generate
    from data.annotate import _call_model
    messages = [
        {"role": "system", "content": system_text},
        {"role": "user",   "content": filled_user},
    ]
    overrides = {"max_new_tokens": 32, "do_sample": False}
    _full, raw, _dev_log = _call_model(
        pipeline, messages, overrides=overrides, images=None
    )

    value = normalize_film_title(raw)
    if not value:
        value = normalize_film_title(_full)
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
    }

    # Write cache
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(
        json.dumps(film_motif, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    if verbose:
        print(f"  → {title}: {value}")

    return film_motif


# ---------------------------------------------------------------------------
# Batch generation
# ---------------------------------------------------------------------------

def generate_film_titles_for_all_movies(
    project_path: str,
    media_type: str = "movies",
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
    from data.annotate import get_annotation_json_path, _load_text_generation_pipeline

    meta_entries = get_metadata(project_path, media_type=media_type)

    # Pre-scan: determine which films still need generation
    needs_work: list[str] = []
    skippable: list[str] = []
    for meta in meta_entries:
        fn = meta.get("filename")
        if not fn:
            continue
        jp = get_annotation_json_path(project_path, fn, media_type)
        if not jp.exists():
            continue
        if not force:
            cache = get_film_motif_path(project_path, fn, media_type)
            if cache.exists():
                try:
                    existing = json.loads(cache.read_text(encoding="utf-8"))
                    if existing.get("value", "").strip():
                        skippable.append(fn)
                        continue
                except Exception:
                    pass
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
