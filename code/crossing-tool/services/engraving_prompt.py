"""Engraving prompt loader.

Finds the alphabetically-last .txt file that matches the requested *mode*
under ``<project>/prompts/engravings/``.

Naming convention
-----------------
- ``engravings-silhouette-YYYY-MM-DD-vN.txt``  — silhouette mode
- ``engravings-full-YYYY-MM-DD-vN.txt``        — full mode

The loader filters candidates by the ``<mode>`` prefix segment so each mode
always resolves its own prompt file and the two sets are never mixed.

Fallback behaviour
------------------
If no mode-prefixed files are found the loader falls back to the overall
alphabetically-last ``.txt`` file in the directory.  This keeps backward
compatibility with projects that have only a single un-prefixed prompt file.

The template uses ``string.Template`` (``$variable``) syntax.  Canonical
variables expanded by the generation pipeline:

    $label       — silhouette subject label (e.g. "horse")
    $field       — category / field (e.g. "animals")
    $movie       — movie title + year (e.g. "Belle Starr (1941)")
    $shot_id     — canonical shot identifier (e.g. "tmdb_72473@f012472-f012544")
    $description — annotation-derived description text
    $motif       — cinematic motif string for the shot

Unknown ``$placeholders`` are left unchanged by ``safe_substitute``.
Missing variables default to empty strings.
"""

from __future__ import annotations

from pathlib import Path

ENGRAVING_MODES = ("silhouette", "full")


class EngravingPromptError(RuntimeError):
    """Raised when no prompt file can be located."""


def load_engraving_prompt(
    project_path: str,
    mode: str = "silhouette",
) -> tuple[str, str]:
    """Return *(prompt_filename, prompt_template)* for the given *mode*.

    Parameters
    ----------
    project_path:
        Absolute path to the crossing project directory.
    mode:
        ``"silhouette"`` or ``"full"``.  Selects the set of prompt files
        whose filename contains ``-<mode>-`` as a segment.  Falls back to the
        alphabetically-last ``.txt`` in the directory when no mode-prefixed
        file is found.

    Raises
    ------
    EngravingPromptError
        If the prompts directory is missing or contains no ``.txt`` files.
    ValueError
        If *mode* is not one of the recognised modes.
    """
    if mode not in ENGRAVING_MODES:
        raise ValueError(
            f"Unknown engraving mode {mode!r}. "
            f"Valid modes: {', '.join(ENGRAVING_MODES)}"
        )

    prompts_dir = Path(project_path) / "prompts" / "engravings"
    if not prompts_dir.is_dir():
        raise EngravingPromptError(
            f"No engraving prompt found:\n{prompts_dir}/*.txt\n\n"
            f"Create a .txt file in that directory to define the generation prompt."
        )

    all_txt = sorted(prompts_dir.glob("*.txt"))
    if not all_txt:
        raise EngravingPromptError(
            f"No engraving prompt found:\n{prompts_dir}/*.txt\n\n"
            f"Create a .txt file in that directory to define the generation prompt."
        )

    # Try to find mode-specific files first (e.g. "engravings-silhouette-*.txt")
    mode_prefix = f"engravings-{mode}-"
    mode_files = [f for f in all_txt if f.name.startswith(mode_prefix)]
    candidate = mode_files[-1] if mode_files else all_txt[-1]

    text = candidate.read_text(encoding="utf-8").strip()
    return candidate.name, text
