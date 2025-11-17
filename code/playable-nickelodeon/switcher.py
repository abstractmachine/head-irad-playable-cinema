import random
import time
from typing import Optional, Tuple
from cinematheque import get_movies, fullpath

try:
    import ffmpeg
    HAS_FFMPEG = True
except ImportError:
    HAS_FFMPEG = False
    print("[switcher] ffmpeg-python not installed; duration checks disabled")

# State
current_index = -1
current_title = ""
current_start_ms = 0

# Gremlin state
_gremlin_enabled: bool = False
_gremlin_interval_s: int = 5
_gremlin_next_t: float = 0.0  # monotonic target time (seconds)

def _get_duration_ms(filepath: str) -> int | None:
    """Return duration in milliseconds using ffmpeg.probe, or None on error."""
    if not HAS_FFMPEG:
        return None
    try:
        probe = ffmpeg.probe(filepath)
        duration_s = float(probe["format"]["duration"])
        return int(duration_s * 1000)
    except Exception as e:
        print(f"[switcher] ffmpeg probe failed for {filepath}: {e}")
        return None

def _random_safe_start_ms(duration_ms: int | None) -> int:
    """Random start avoiding first/last 2% if duration known; else 0."""
    if duration_ms and duration_ms > 10000:
        margin = int(duration_ms * 0.02)
        return random.randint(margin, max(margin, duration_ms - margin))
    return 0

def _select_film(index: int, *, randomize_start: bool) -> tuple[int, str, int]:
    """
    Select a film by index. Returns (index, filename, start_ms).
    randomize_start=True uses a safe random offset; else start at 0.
    """
    global current_index, current_title, current_start_ms

    movies = get_movies()
    if not movies or index < 0 or index >= len(movies):
        return (-1, "", 0)

    filename = movies[index]
    filepath = fullpath(filename)

    if randomize_start:
        duration_ms = _get_duration_ms(filepath)
        start_ms = _random_safe_start_ms(duration_ms)
    else:
        start_ms = 0

    current_index = index
    current_title = filename
    current_start_ms = start_ms
    return (current_index, current_title, current_start_ms)

def select_random() -> tuple[int, str, int]:
    """Pick a random film with a random safe time."""
    movies = get_movies()
    if not movies:
        return (-1, "", 0)
    idx = random.randrange(len(movies))
    return _select_film(idx, randomize_start=True)

def select_next() -> tuple[int, str, int]:
    """Select next film (wraps), start at 0."""
    movies = get_movies()
    if not movies:
        return (-1, "", 0)
    next_idx = (current_index + 1) % len(movies)
    return _select_film(next_idx, randomize_start=False)

def select_previous() -> tuple[int, str, int]:
    """Select previous film (wraps), start at 0."""
    movies = get_movies()
    if not movies:
        return (-1, "", 0)
    if current_index == -1:
        prev_idx = len(movies) - 1
    else:
        prev_idx = (current_index - 1) % len(movies)
    return _select_film(prev_idx, randomize_start=False)

def randomize_time() -> tuple[int, str, int]:
    """Re-randomize the start time for the currently selected film."""
    global current_start_ms
    movies = get_movies()
    if not movies or current_index < 0 or current_index >= len(movies):
        return (-1, "", 0)
    filename = movies[current_index]
    filepath = fullpath(filename)
    duration_ms = _get_duration_ms(filepath)
    current_start_ms = _random_safe_start_ms(duration_ms)
    return (current_index, filename, current_start_ms)

def get_current() -> tuple[int, str, int]:
    """Return current selection (index, title, start_ms)."""
    return (current_index, current_title, current_start_ms)

# --- Gremlin API ---

def gremlin_toggle() -> tuple[bool, int]:
    """Toggle gremlin on/off. Returns (enabled, interval_s)."""
    global _gremlin_enabled, _gremlin_next_t
    _gremlin_enabled = not _gremlin_enabled
    if _gremlin_enabled:
        _gremlin_next_t = time.monotonic() + _gremlin_interval_s
    return (_gremlin_enabled, _gremlin_interval_s)

def gremlin_set_interval(seconds: int) -> tuple[bool, int]:
    """Set gremlin interval (1..999). Returns (enabled, interval_s)."""
    global _gremlin_interval_s, _gremlin_next_t
    seconds = max(1, min(int(seconds), 999))
    _gremlin_interval_s = seconds
    # Reset schedule so change applies immediately
    if _gremlin_enabled:
        _gremlin_next_t = time.monotonic() + _gremlin_interval_s
    return (_gremlin_enabled, _gremlin_interval_s)

def gremlin_get() -> tuple[bool, int]:
    """Get gremlin state (enabled, interval_s)."""
    return (_gremlin_enabled, _gremlin_interval_s)

def gremlin_tick() -> Optional[Tuple[int, str, int]]:
    """
    Run gremlin scheduling. If due and enabled, triggers select_random()
    and returns the selection tuple; else returns None.
    """
    global _gremlin_next_t
    if not _gremlin_enabled:
        return None
    now = time.monotonic()
    if now >= _gremlin_next_t:
        _gremlin_next_t = now + _gremlin_interval_s
        return select_random()
    return None