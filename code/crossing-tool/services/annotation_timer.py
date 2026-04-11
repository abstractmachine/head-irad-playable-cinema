"""Rolling-window timing estimator for ``crossing annotate``.

This module is intentionally free of any project or filesystem dependencies
so it can be tested independently of the annotation pipeline.
"""

from __future__ import annotations

from collections import deque
from typing import Deque, Optional


class AnnotationTimer:
    """Records per-shot durations and computes human-readable ETA strings.

    Maintains a rolling window of the last *window* completed annotation
    durations and can project remaining time for the current movie and for
    the whole corpus.

    Parameters
    ----------
    window:
        Maximum number of recent observations to keep (default 10).
    """

    DEFAULT_WINDOW: int = 10

    def __init__(self, window: int = DEFAULT_WINDOW) -> None:
        self._durations: Deque[float] = deque(maxlen=window)
        self.window = window

    # ------------------------------------------------------------------
    # Data recording

    def record(self, duration_seconds: float) -> None:
        """Record one completed annotation duration (seconds ≥ 0)."""
        if duration_seconds >= 0:
            self._durations.append(duration_seconds)

    # ------------------------------------------------------------------
    # Computed properties

    @property
    def count(self) -> int:
        """Number of observations currently in the rolling window."""
        return len(self._durations)

    @property
    def avg(self) -> Optional[float]:
        """Rolling average in seconds, or *None* when no data is available."""
        if not self._durations:
            return None
        return sum(self._durations) / len(self._durations)

    # ------------------------------------------------------------------
    # Formatting helpers

    @staticmethod
    def format_duration(seconds: float) -> str:
        """Format *seconds* as a compact human-readable string.

        Examples::

            format_duration(45)    → '45s'
            format_duration(102)   → '1m42s'
            format_duration(44280) → '12h18m'
        """
        total = int(round(seconds))
        if total < 60:
            return f"{total}s"
        if total < 3600:
            m, s = divmod(total, 60)
            return f"{m}m{s:02d}s"
        if total < 86400:
            h, rem = divmod(total, 3600)
            m = rem // 60
            return f"{h}h{m:02d}m"
        d, rem = divmod(total, 86400)
        h = rem // 3600
        return f"{d}d{h:02d}h"

    # ------------------------------------------------------------------
    # Verbose output

    def print_estimates(
        self,
        shots_remaining_movie: int,
        shots_remaining_corpus: int,
    ) -> None:
        """Print ETA lines to stdout.

        Prints an 'unavailable' notice when no observations have been
        recorded yet.  Never raises.

        Parameters
        ----------
        shots_remaining_movie:
            Shots still to process in the current movie (not counting the
            shot that just completed).
        shots_remaining_corpus:
            Total shots still to process across all remaining corpus movies,
            including the current movie's remaining shots.
        """
        avg = self.avg
        if avg is None:
            print("[estimate] unavailable (no completed annotations yet)")
            return

        n = self.count
        window_label = (
            f"last {n}" if n == self.window else f"{n} shot{'s' if n != 1 else ''}"
        )
        print(f"[estimate] avg/shot: {self.format_duration(avg)} ({window_label})")

        if shots_remaining_movie > 0:
            print(
                f"[estimate] current movie remaining: "
                f"{self.format_duration(avg * shots_remaining_movie)}"
                f" ({shots_remaining_movie} shots)"
            )
        else:
            print("[estimate] current movie remaining: done")

        if shots_remaining_corpus > 0:
            print(
                f"[estimate] corpus remaining: "
                f"{self.format_duration(avg * shots_remaining_corpus)}"
                f" ({shots_remaining_corpus} shots)"
            )
        else:
            print("[estimate] corpus remaining: done")
