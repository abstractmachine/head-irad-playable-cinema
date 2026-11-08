"""
Stable media_id computation for Crossing metadata.

ID rules (priority order):
  tmdb_<id>            TMDB movies (record has 'tmdb' or 'tmdb_id' field)
  yt_<id>              YouTube videos (record has 'youtube_id' or 'yt_id' field)
  vimeo_<id>           Vimeo videos (record has 'vimeo_id' field)
  game_<slug>_<hash>   Gameplay clips (media_type == "gameplay")
  file_<hash>          Generic local files

The hash component is always a short SHA-256 digest of the original filename,
ensuring the ID is stable and independent of descriptive label text.
"""
import hashlib
import re
import unicodedata

INSTANCE_REGISTRATION_POLICY = "frame-ordinal-field-label-reference-path-v1"


def compute_media_id(record: dict, media_type: str = "movie") -> str:
    """Return a stable media_id for a metadata record.

    Args:
        record:     Dict with at minimum a 'filename' or 'title' field.
        media_type: "movie" | "gameplay"  (controls fallback slug prefix)
    """
    # TMDB movie -------------------------------------------------------
    tmdb = str(record.get("tmdb") or record.get("tmdb_id") or "").strip()
    if tmdb:
        return f"tmdb_{tmdb}"

    # YouTube ----------------------------------------------------------
    yt = str(record.get("youtube_id") or record.get("yt_id") or "").strip()
    if yt:
        return f"yt_{yt}"

    # Vimeo ------------------------------------------------------------
    vimeo = str(record.get("vimeo_id") or "").strip()
    if vimeo:
        return f"vimeo_{vimeo}"

    # Hash input: original filename is the most stable anchor
    filename = str(
        record.get("original_filename") or record.get("filename") or ""
    ).strip()
    title = str(record.get("title") or "").strip()
    hash_input = filename or title

    # Gameplay clip ----------------------------------------------------
    if media_type in ("gameplay",):
        # Prefer an explicit 'game' key, then fall back to the first word of title/filename
        game_hint = str(record.get("game") or title or filename)
        slug = _game_slug(game_hint)
        return f"game_{slug}_{_short_hash(hash_input)}"

    # Generic local file -----------------------------------------------
    return f"file_{_short_hash(hash_input)}"


def _short_hash(text: str, length: int = 8) -> str:
    """Return a stable, truncated SHA-256 hex digest of *text*."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:length]


def _game_slug(text: str) -> str:
    """Convert a game name or clip title to a short, safe identifier slug.

    Takes only the first token so that 'rdr2 full no hud …' → 'rdr2'.
    """
    text = text.lower()
    text = re.sub(r"[-_]+", " ", text)          # normalise separators
    text = re.sub(r"[^a-z0-9\s]", "", text)     # strip special chars
    words = text.split()
    return words[0] if words else "unknown"


# ---------------------------------------------------------------------------
# Stable shot identity
# ---------------------------------------------------------------------------

def build_shot_id(media_id: str, start_frame: int, end_frame: int) -> str:
    """Return the canonical stable shot identifier.

    Format:  <media_id>@fSTART-fEND   (minimum 6-digit zero-padded frame numbers;
                                        expands beyond 6 digits for long clips)
    Example: tmdb_39435@f000234-f000398
    Example: game_rdr2_ce5e0bba@f1012726-f1013511  (7-digit, long gameplay clip)

    This is the ONLY place that defines this format.  Frames are inclusive
    at both boundaries:  previous shot ends at f000233, next starts at f000234.
    """
    return f"{media_id}@f{start_frame:06d}-f{end_frame:06d}"


def parse_shot_id(shot_id: str) -> tuple:
    """Parse a stable shot_id back into (media_id, start_frame, end_frame).

    Raises ValueError if the string does not match the expected format.
    """
    m = re.match(r'^(.+)@f(\d{6,})-f(\d{6,})$', shot_id)
    if not m:
        raise ValueError(
            f"Invalid shot_id: {shot_id!r}. "
            "Expected '<media_id>@fSTART-fEND' with 6+ digit zero-padded frames."
        )
    return m.group(1), int(m.group(2)), int(m.group(3))


def label_slug(label: str) -> str:
    """Classification slug: NFKC/casefold, letters/numbers, hyphen separators."""
    normalized = unicodedata.normalize("NFKC", label).casefold().strip()
    pieces = []
    separator = False
    for character in normalized:
        if character.isalnum():
            if separator and pieces:
                pieces.append("-")
            pieces.append(character)
            separator = False
        else:
            separator = True
    return "".join(pieces)


def instance_identifier(shot_id: str, number: int) -> str:
    """Format an already registered shot ordinal; this never allocates an ID."""
    media, start, end = parse_shot_id(shot_id)
    if (start > end or build_shot_id(media, start, end) != shot_id
            or any(character in shot_id for character in "/\\#\x00")
            or type(number) is not int or number < 1):
        raise ValueError("Invalid canonical instance components")
    return f"{shot_id}#{number:04d}"


def parse_instance_id(instance_id: str) -> tuple:
    """Parse a registered identity without interpreting classification/location."""
    shot, separator, number = instance_id.rpartition("#")
    if (not separator or not number.isascii() or not number.isdigit()
            or instance_identifier(shot, int(number)) != instance_id):
        raise ValueError("Invalid canonical instance identifier")
    return shot, int(number)
