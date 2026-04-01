import re
from pathlib import Path


def normalize_filename(filename: str) -> str:
    """Convert a dash-separated filename to Title Case with spaced markers.

    'film-name-with-dashes(1967){tmdb-118017}.mp4'
    → 'Film Name With Dashes (1967) {tmdb-118017}.mp4'

    If the title portion already contains spaces the filename is returned
    unchanged (considered already formatted).
    Missing year or tmdb markers are handled gracefully.
    """
    p = Path(filename)
    stem = p.stem
    ext = p.suffix

    year_match = re.search(r'\((\d{4})\)', stem)
    tmdb_match = re.search(r'\{tmdb-(\d+)\}', stem)

    # Title is everything before the first marker
    title_end = len(stem)
    if year_match:
        title_end = min(title_end, year_match.start())
    if tmdb_match:
        title_end = min(title_end, tmdb_match.start())

    title_raw = stem[:title_end].strip().rstrip('-').strip()

    # Already formatted if title portion has spaces
    if ' ' in title_raw:
        return filename

    if not title_raw:
        return filename

    title = title_raw.replace('-', ' ').strip().title()

    parts = [title]
    if year_match:
        parts.append(f'({year_match.group(1)})')
    if tmdb_match:
        parts.append('{' + f'tmdb-{tmdb_match.group(1)}' + '}')

    return ' '.join(parts) + ext
