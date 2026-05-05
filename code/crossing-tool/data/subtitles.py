"""Shared subtitle utilities for Playable Cinema.

This module is the single source of truth for all subtitle semantics:

  - resolving the subtitle file path (space-format and legacy dash-format)
  - checking subtitle existence
  - parsing SRT files into Cue objects
  - finding the active subtitle cue for a given playback position

It has no UI dependencies and can be called from the CLI, the Shotlist
Visualizer, and later from MCP without modification.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class Cue:
    """A single SRT subtitle cue."""
    start_secs: float
    end_secs: float
    text: str


# ---------------------------------------------------------------------------
# Path resolution
# ---------------------------------------------------------------------------

def _subtitle_dir(project_path: str, media_type: str) -> Path:
    return Path(project_path) / "media" / "subtitles" / media_type


def subtitle_path_for(project_path: str, media_type: str, filename: str) -> Path | None:
    """Return the resolved subtitle Path for *filename*, or None if absent.

    Checks the canonical (space-separated) name first, then the legacy
    dash-separated name that older downloads may have used.
    """
    d = _subtitle_dir(project_path, media_type)
    stem = Path(filename).stem
    canonical = d / (stem + ".srt")
    if canonical.exists():
        return canonical
    legacy = d / (stem.replace(" ", "-") + ".srt")
    if legacy.exists():
        return legacy
    return None


def subtitle_exists(project_path: str, media_type: str, filename: str) -> bool:
    """Return True if a subtitle file exists for *filename*."""
    return subtitle_path_for(project_path, media_type, filename) is not None


# ---------------------------------------------------------------------------
# SRT parsing
# ---------------------------------------------------------------------------

_TIMESTAMP_RE = re.compile(
    r"(\d{1,2}):(\d{2}):(\d{2})[,.](\d{3})"
    r"\s*-->\s*"
    r"(\d{1,2}):(\d{2}):(\d{2})[,.](\d{3})"
)


def _parse_timestamp(h: str, m: str, s: str, ms: str) -> float:
    return int(h) * 3600 + int(m) * 60 + int(s) + int(ms) / 1000.0


def _parse_srt(text: str) -> list[Cue]:
    """Parse raw SRT text into a list of Cue objects.

    Conservative: ignores malformed blocks without raising.
    """
    cues: list[Cue] = []
    # Split into blocks on one or more blank lines
    blocks = re.split(r"\n\s*\n", text.strip())
    for block in blocks:
        lines = block.strip().splitlines()
        if len(lines) < 2:
            continue
        # Skip optional sequence number on the first line
        start_line = 0
        if lines[0].strip().isdigit():
            start_line = 1
        if start_line >= len(lines):
            continue
        m = _TIMESTAMP_RE.match(lines[start_line].strip())
        if not m:
            continue
        start_secs = _parse_timestamp(m.group(1), m.group(2), m.group(3), m.group(4))
        end_secs   = _parse_timestamp(m.group(5), m.group(6), m.group(7), m.group(8))
        # Everything after the timestamp line is the subtitle text
        raw_text = "\n".join(lines[start_line + 1:]).strip()
        # Strip basic HTML tags (e.g. <i>, <b>)
        clean = re.sub(r"<[^>]+>", "", raw_text).strip()
        if clean:
            cues.append(Cue(start_secs=start_secs, end_secs=end_secs, text=clean))
    return cues


def load_subtitle_cues(project_path: str, media_type: str, filename: str) -> list[Cue]:
    """Load and parse subtitle cues for *filename*.

    Returns an empty list if no subtitle file is found or parsing fails.
    """
    path = subtitle_path_for(project_path, media_type, filename)
    if path is None:
        return []
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
        return _parse_srt(text)
    except OSError:
        return []


# ---------------------------------------------------------------------------
# Active cue lookup
# ---------------------------------------------------------------------------

def active_subtitle(cues: list[Cue], seconds: float) -> str:
    """Return the subtitle text active at *seconds*, or an empty string.

    Uses a simple linear scan; subtitle lists are typically short enough
    that no binary search is needed.  For very long files a binary search
    on start_secs could be added later.
    """
    for cue in cues:
        if cue.start_secs <= seconds <= cue.end_secs:
            return cue.text
    return ""
