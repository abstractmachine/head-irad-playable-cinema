"""Engraving prompt loader.

Finds the alphabetically-last .txt file under
    <project>/prompts/engravings/

and returns its filename and raw template text.

The template uses ``string.Template`` (``$variable``) syntax.  Canonical
variables expanded by the generation pipeline:

    $label       — silhouette subject label (e.g. "horse")
    $field       — category / field (e.g. "animals")
    $movie       — movie title + year (e.g. "Belle Starr (1941)")
    $shot_id     — canonical shot identifier (e.g. "tmdb_72473@f012472-f012544")
    $description — annotation-derived description text

Unknown ``$placeholders`` are left unchanged by ``safe_substitute``.
Missing variables default to empty strings.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional


class EngravingPromptError(RuntimeError):
    """Raised when no prompt file can be located."""


def load_engraving_prompt(project_path: str) -> tuple[str, str]:
    """Return *(prompt_filename, prompt_template)* from the latest engraving prompt.

    Searches ``<project>/prompts/engravings/`` for ``*.txt`` files and picks
    the alphabetically last one (date-versioned filenames sort chronologically
    by convention: ``engravings-YYYY-MM-DD-vN.txt``).

    The returned text is the raw template string — callers should expand
    ``$variable`` placeholders via ``string.Template.safe_substitute``.

    Raises
    ------
    EngravingPromptError
        If the prompts directory is missing or contains no ``.txt`` files.
    """
    prompts_dir = Path(project_path) / "prompts" / "engravings"
    if not prompts_dir.is_dir():
        raise EngravingPromptError(
            f"No engraving prompt found:\n{prompts_dir}/*.txt\n\n"
            f"Create a .txt file in that directory to define the generation prompt."
        )

    txt_files = sorted(prompts_dir.glob("*.txt"))
    if not txt_files:
        raise EngravingPromptError(
            f"No engraving prompt found:\n{prompts_dir}/*.txt\n\n"
            f"Create a .txt file in that directory to define the generation prompt."
        )

    latest = txt_files[-1]
    text = latest.read_text(encoding="utf-8").strip()
    return latest.name, text
