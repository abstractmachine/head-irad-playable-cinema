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


def _add_media_arg(
    p: argparse.ArgumentParser,
    *,
    default: str | None = "movies",
    required: bool = False,
) -> None:
    """Add ``--media {movies,gameplay}`` to *p*.

    Most subcommands default to ``"movies"``.  Pass ``default=None`` for
    optional media selectors, or ``required=True`` for commands that must have
    an explicit value (e.g. ``remove``).
    """
    p.add_argument(
        "--media",
        choices=["movies", "gameplay"],
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
