"""Human-readable formatting for vocabulary query results.

Supported formats
-----------------
json       Raw JSON (default when stdout is not a TTY).
table      Aligned two-column terminal table (value | count).
list       One value per line, no counts.
markdown   Pipe-delimited Markdown table.
bar        Horizontal bar chart scaled to the maximum count.
auto       Chooses ``table`` when stdout is a TTY, ``json`` otherwise.

Public API
----------
    format_vocabulary_items(items, fmt, field_name=None, top=None) -> str
    format_vocabulary_map(vocab_map, fmt, top=None) -> str

Items are expected as ``[{"value": str, "count": int}, ...]`` (the shape
returned by ``get_vocabulary`` / ``vocabulary_from_field``).  Plain strings
(from ``--show_count`` omitted) are also accepted; counts will show as ``-``.
"""

from __future__ import annotations

import json
import sys
from typing import Sequence

_BAR_WIDTH = 40  # max character width of a bar


def _is_tty() -> bool:
    return hasattr(sys.stdout, "isatty") and sys.stdout.isatty()


def _resolve_format(fmt: str) -> str:
    if fmt == "auto":
        return "table" if _is_tty() else "json"
    return fmt


def _normalise_items(items: Sequence) -> list[dict]:
    """Accept both ``{"value": …, "count": …}`` dicts and plain strings."""
    out = []
    for item in items:
        if isinstance(item, dict):
            out.append({"value": str(item.get("value", "")), "count": item.get("count")})
        else:
            out.append({"value": str(item), "count": None})
    return out


# ---------------------------------------------------------------------------
# Single-field formatters
# ---------------------------------------------------------------------------

def _fmt_json(items: list[dict], **_) -> str:
    serialisable = [
        {"value": it["value"], "count": it["count"]}
        if it["count"] is not None
        else it["value"]
        for it in items
    ]
    return json.dumps(serialisable, indent=2, ensure_ascii=False)


def _fmt_table(items: list[dict], field_name: str | None = None, **_) -> str:
    lines: list[str] = []
    if field_name:
        lines.append(field_name)
    if not items:
        lines.append("  (no values)")
        return "\n".join(lines)

    has_counts = any(it["count"] is not None for it in items)
    col_w = max(len(it["value"]) for it in items)

    if has_counts:
        header = f"{'value':<{col_w}}  {'count':>8}"
        lines.append(header)
        lines.append("-" * len(header))
        for it in items:
            count_str = f"{it['count']:>8}" if it["count"] is not None else "        -"
            lines.append(f"{it['value']:<{col_w}}  {count_str}")
    else:
        lines.append("value")
        lines.append("-" * col_w)
        for it in items:
            lines.append(it["value"])
    return "\n".join(lines)


def _fmt_list(items: list[dict], **_) -> str:
    return "\n".join(it["value"] for it in items)


def _fmt_markdown(items: list[dict], field_name: str | None = None, **_) -> str:
    lines: list[str] = []
    if field_name:
        lines.append(f"### {field_name}")
        lines.append("")
    if not items:
        return "\n".join(lines) + "\n*(no values)*"

    has_counts = any(it["count"] is not None for it in items)
    if has_counts:
        lines.append("| value | count |")
        lines.append("| --- | ---: |")
        for it in items:
            count_str = str(it["count"]) if it["count"] is not None else "-"
            lines.append(f"| {it['value']} | {count_str} |")
    else:
        lines.append("| value |")
        lines.append("| --- |")
        for it in items:
            lines.append(f"| {it['value']} |")
    return "\n".join(lines)


def _fmt_bar(items: list[dict], field_name: str | None = None, **_) -> str:
    lines: list[str] = []
    if field_name:
        lines.append(field_name)
    if not items:
        lines.append("  (no values)")
        return "\n".join(lines)

    counts = [it["count"] for it in items if it["count"] is not None]
    max_count = max(counts) if counts else 1
    val_w = max(len(it["value"]) for it in items)

    for it in items:
        cnt = it["count"] if it["count"] is not None else 0
        bar_len = round((cnt / max_count) * _BAR_WIDTH) if max_count else 0
        bar = "█" * bar_len
        count_str = f"{cnt:>10,}"
        lines.append(f"{it['value']:<{val_w}}  {bar:<{_BAR_WIDTH}}  {count_str}")
    return "\n".join(lines)


_FORMATTERS = {
    "json":     _fmt_json,
    "table":    _fmt_table,
    "list":     _fmt_list,
    "markdown": _fmt_markdown,
    "bar":      _fmt_bar,
}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def format_vocabulary_items(
    items: Sequence,
    fmt: str = "auto",
    field_name: str | None = None,
    top: int | None = None,
) -> str:
    """Format a single field's vocabulary for terminal output.

    Parameters
    ----------
    items:      ``[{"value": str, "count": int}, ...]`` or plain strings.
    fmt:        Output format (``json``, ``table``, ``list``, ``markdown``,
                ``bar``, or ``auto``).
    field_name: Optional header label (used by table / bar / markdown).
    top:        Truncate to the first *top* items before formatting.
    """
    resolved = _resolve_format(fmt)
    normalised = _normalise_items(items)
    if top is not None and top > 0:
        normalised = normalised[:top]
    formatter = _FORMATTERS.get(resolved, _fmt_table)
    return formatter(normalised, field_name=field_name)


def format_vocabulary_map(
    vocab_map: dict[str, Sequence],
    fmt: str = "auto",
    top: int | None = None,
) -> str:
    """Format a ``{field: [items]}`` vocabulary map for terminal output.

    Fields are separated by a blank line.  For ``json`` the whole map is
    serialised as a single JSON object (matching the current ``--all-fields``
    behavior).
    """
    resolved = _resolve_format(fmt)

    if resolved == "json":
        # Serialize the whole map as one JSON object, preserving the existing
        # --all-fields output format exactly.
        serialisable: dict = {}
        for field, items in vocab_map.items():
            norm = _normalise_items(items)
            if top is not None and top > 0:
                norm = norm[:top]
            serialisable[field] = [
                {"value": it["value"], "count": it["count"]}
                if it["count"] is not None
                else it["value"]
                for it in norm
            ]
        return json.dumps(serialisable, indent=2, ensure_ascii=False)

    sections: list[str] = []
    for field, items in vocab_map.items():
        sections.append(format_vocabulary_items(items, fmt=resolved, field_name=field, top=top))
    return "\n\n".join(sections)
