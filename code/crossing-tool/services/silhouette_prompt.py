"""Silhouette semantic-analysis prompt loader.

Finds the alphabetically-last .txt file under
    <project>/prompts/silhouettes/

and parses it as an INI-style prompt file.  Each section corresponds to one
classification dimension (``[viewpoint]``, ``[completeness]``, etc.).  Each
line inside a section is a ``label = prompt_template`` pair.  The special
``[meta]`` section may contain optional display keys and is not returned as a
classification dimension.

File naming convention (date-versioned, alphabetically latest wins):
    silhouettes-YYYY-MM-DD-vN.txt

Template syntax
---------------
Prompt templates may contain ``{label}`` and ``{field}`` placeholders which
are substituted at classification time via ``str.format_map``.  Any unknown
placeholder is left unchanged.  Example template::

    a complete {label}, fully visible with nothing cut off

Usage::

    filename, prompts = load_silhouette_prompts(project_path)
    # prompts == {
    #   "viewpoint":    {"side": "a {label} seen from the side ...", ...},
    #   "completeness": {"full": "a complete {label} ...", ...},
    #   ...
    # }

    dimension_prompts = prompts["completeness"]
    texts = [t.format_map(_SafeFormat(label="horse", field="animals"))
             for t in dimension_prompts.values()]
"""

from __future__ import annotations

import configparser
from pathlib import Path
from typing import Dict


# ---------------------------------------------------------------------------
# Public exception
# ---------------------------------------------------------------------------

class SilhouettePromptError(RuntimeError):
    """Raised when no silhouette prompt file can be located or parsed."""


# ---------------------------------------------------------------------------
# Safe format helper
# ---------------------------------------------------------------------------

class _SafeFormat(dict):
    """dict subclass that leaves unknown ``{key}`` placeholders intact."""
    def __missing__(self, key: str) -> str:
        return "{" + key + "}"


# ---------------------------------------------------------------------------
# Loader
# ---------------------------------------------------------------------------

def load_silhouette_prompts(project_path: str) -> tuple[str, Dict[str, Dict[str, str]]]:
    """Return *(prompt_filename, prompts_dict)* from the latest silhouette prompt.

    Searches ``<project>/prompts/silhouettes/`` for ``*.txt`` files and picks
    the alphabetically last one (date-versioned filenames sort chronologically
    by convention: ``silhouettes-YYYY-MM-DD-vN.txt``).

    Returns
    -------
    prompt_filename : str
        Basename of the chosen file (for provenance logging).
    prompts_dict : dict[str, dict[str, str]]
        Mapping of ``{dimension: {label: prompt_template}}``.
        The ``[meta]`` section (if present) is excluded.
        Example dimensions: ``viewpoint``, ``completeness``, ``occlusion``,
        ``isolation``.

    Raises
    ------
    SilhouettePromptError
        If the prompts directory is missing, contains no ``.txt`` files, or
        the chosen file cannot be parsed as valid INI sections.
    """
    prompts_dir = Path(project_path) / "prompts" / "silhouettes"
    if not prompts_dir.is_dir():
        raise SilhouettePromptError(
            f"No silhouette prompt directory found:\n  {prompts_dir}\n\n"
            f"Create a .txt prompt file there to enable semantic analysis.\n"
            f"Example: {prompts_dir}/silhouettes-YYYY-MM-DD-v1.txt"
        )

    txt_files = sorted(prompts_dir.glob("*.txt"))
    if not txt_files:
        raise SilhouettePromptError(
            f"No silhouette prompt file found in:\n  {prompts_dir}\n\n"
            f"Create a .txt prompt file there to enable semantic analysis.\n"
            f"Example: {prompts_dir}/silhouettes-YYYY-MM-DD-v1.txt"
        )

    latest = txt_files[-1]
    text = latest.read_text(encoding="utf-8")

    # optionxform = str preserves the case of option keys exactly as written in
    # the file.  The default (str.lower) would silently fold keys to lowercase,
    # which is wrong if the file ever uses mixed-case label names.
    parser = configparser.ConfigParser(
        delimiters=("=",),
        comment_prefixes=("#", ";"),
        inline_comment_prefixes=("#",),
    )
    parser.optionxform = str  # type: ignore[method-assign]

    try:
        parser.read_string(text)
    except configparser.Error as exc:
        raise SilhouettePromptError(
            f"Failed to parse silhouette prompt file:\n  {latest}\n\n{exc}"
        ) from exc

    # Keys that ConfigParser inherits from its implicit DEFAULT dict.
    # We must exclude them when iterating section options so that any keys
    # defined in [DEFAULT] do not bleed into every dimension section.
    _default_keys: set[str] = set(parser.defaults().keys())

    prompts: Dict[str, Dict[str, str]] = {}
    for section in parser.sections():
        # Skip the optional [meta] section used for display / version info.
        if section.lower() == "meta":
            continue
        dimension_prompts: Dict[str, str] = {}
        for label in parser.options(section):
            if label in _default_keys:
                continue
            template = parser.get(section, label)
            dimension_prompts[label] = template.strip()
        if dimension_prompts:
            prompts[section.lower()] = dimension_prompts

    # Validate that all four required classification dimensions are present.
    _REQUIRED = {"viewpoint", "completeness", "occlusion", "isolation"}
    missing = _REQUIRED - set(prompts.keys())
    if missing:
        missing_list = ", ".join(f"[{s}]" for s in sorted(missing))
        raise SilhouettePromptError(
            f"Silhouette prompt file is missing required section(s): "
            f"{missing_list}\n"
            f"  File: {latest}\n\n"
            f"Add each missing section with label = prompt_template lines."
        )

    return latest.name, prompts


def expand_prompt(template: str, label: str, field: str = "") -> str:
    """Substitute ``{label}`` and ``{field}`` in *template*, leaving others intact."""
    return template.format_map(_SafeFormat(label=label, field=field))
