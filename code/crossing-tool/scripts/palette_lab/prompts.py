"""Experiment-local prompt loading for the palette research laboratory.

Deliberately separate from the production loaders in ``data/annotate.py`` and
``data/film_motif.py`` — this reads ``<project>/prompts/palettes/`` with the
same latest-wins natural-sort convention but is free to change without
affecting any production command.
"""

from __future__ import annotations

import re
from pathlib import Path

PROMPT_SUBDIR = ("prompts", "palettes")


def _natural_key(path: Path) -> list:
    return [
        int(part) if part.isdigit() else part
        for part in re.split(r"(\d+)", path.name)
    ]


def prompt_dir(project_path: str) -> Path:
    return Path(project_path).joinpath(*PROMPT_SUBDIR)


def find_latest(project_path: str, role: str, kind: str) -> Path | None:
    """Return the newest ``palette-<role>-<kind>-*.txt``.

    *role* is ``focus`` or ``curator``; *kind* is ``system`` or ``user``.
    Zero-byte files are skipped as in-progress drafts.
    """
    directory = prompt_dir(project_path)
    if not directory.is_dir():
        return None
    matches = [
        path
        for path in directory.glob(f"palette-{role}-{kind}-*.txt")
        if path.is_file() and path.stat().st_size > 0
    ]
    if not matches:
        return None
    matches.sort(key=_natural_key, reverse=True)
    return matches[0]


def load_pair(
    project_path: str,
    role: str,
    *,
    system_file: str | None = None,
    user_file: str | None = None,
) -> dict:
    """Load one role's system+user prompt pair.

    Returns ``{"system": text, "user": text, "system_file": name,
    "user_file": name}``.  Raises ``FileNotFoundError`` rather than falling
    back to a built-in prompt: in a research instrument, silently running a
    different prompt than the one on disk would invalidate the result.
    """
    resolved: dict = {}
    for kind, override in (("system", system_file), ("user", user_file)):
        if override:
            path = Path(override)
            if not path.exists():
                path = Path(project_path) / override
        else:
            path = find_latest(project_path, role, kind)
        if path is None or not path.exists():
            raise FileNotFoundError(
                f"No palette-{role}-{kind}-*.txt prompt found in "
                f"{prompt_dir(project_path)}"
            )
        resolved[kind] = path.read_text(encoding="utf-8")
        resolved[f"{kind}_file"] = path.name
    return resolved


def strip_comments(text: str) -> str:
    """Drop leading ``#`` header lines so the model never sees file metadata."""
    lines = text.splitlines()
    kept = [line for line in lines if not line.startswith("#")]
    return "\n".join(kept).strip()


def substitute(template: str, variables: dict) -> str:
    """Fill ``$name`` placeholders using the production substitution helper.

    Keys are applied longest-first because the shared helper is a plain
    ordered ``str.replace`` — without this, ``$focus`` would corrupt
    ``$focusdesc``.
    """
    from data.annotate import _substitute_variables

    ordered = {
        key: variables[key]
        for key in sorted(variables, key=len, reverse=True)
    }
    return _substitute_variables(template, ordered)
