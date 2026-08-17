"""
tool/helpers.py — Shared argparse argument helpers for cli.py.

Each helper adds one standard flag to a subparser *p*.  Keeping declarations
here eliminates the repetitive add_argument boilerplate that was spread across
every subcommand and ensures every flag uses the same choices, defaults, types,
and metavar values consistently.

Usage:
    from tool.helpers import _add_media_arg, _add_tmdb_arg, _add_verbose_arg, _add_dry_run_arg

    _add_media_arg(p)
    _add_tmdb_arg(p)
    _add_verbose_arg(p, help="Print per-shot progress to stdout")
    _add_dry_run_arg(p, help="Report issues without writing any changes")
"""

import argparse

# ---------------------------------------------------------------------------
# Media-type normalisation
# ---------------------------------------------------------------------------

_MEDIA_TYPE_ALIASES: dict[str, str] = {
    "movie":    "movie",
    "gameplay": "gameplay",
}

_VALID_MEDIA_TYPES = frozenset(_MEDIA_TYPE_ALIASES)


def normalize_media_type(s: str) -> str:
    """Return the canonical media type for *s*.

    Only ``"movie"`` and ``"gameplay"`` are valid.  Any other value,
    including the legacy plural ``"movies"``, raises ``ValueError``.

    >>> normalize_media_type("movie")
    'movie'
    >>> normalize_media_type("gameplay")
    'gameplay'
    """
    try:
        return _MEDIA_TYPE_ALIASES[s]
    except KeyError:
        raise ValueError(
            f"Invalid media type: {s!r}. "
            f"Valid media types: {', '.join(sorted(_VALID_MEDIA_TYPES))}"
        ) from None


def _add_media_arg(
    p: argparse.ArgumentParser,
    *,
    default: str | None = "movie",
    required: bool = False,
    allow_both: bool = False,
) -> None:
    """Add a canonical ``--media`` selector to *p*.

    Most subcommands default to ``"movie"``.  Pass ``default=None`` for
    optional media selectors, or ``required=True`` for commands that must have
    an explicit value (e.g. ``remove``). Commands that explicitly handle both
    media types may pass ``allow_both=True``.
    """
    choices = ["movie", "gameplay"]
    if allow_both:
        choices.append("both")
    p.add_argument(
        "--media",
        choices=choices,
        default=default,
        required=required,
    )


def _add_tmdb_arg(
    p: argparse.ArgumentParser,
    *,
    help: str = "TMDb ID",
) -> None:
    """Add ``--tmdb ID`` (integer) to *p*."""
    p.add_argument("--tmdb", type=int, default=None, metavar="ID", help=help)


def _add_verbose_arg(
    p: argparse.ArgumentParser,
    *,
    help: str = "Print progress to stdout",
) -> None:
    """Add ``--verbose`` (store_true) to *p*."""
    p.add_argument("--verbose", action="store_true", help=help)


def _add_dry_run_arg(
    p: argparse.ArgumentParser,
    *,
    help: str = "Show what would happen without writing any files",
) -> None:
    """Add ``--dry-run`` (store_true, dest='dry_run') to *p*."""
    p.add_argument("--dry-run", dest="dry_run", action="store_true", help=help)
