import unicodedata
import re
import html

minimum_load_interval = 0.25  # Minimum time between loads in seconds

HIGHLIGHT_BACKGROUND_COLOR = "#FF00FF" # Fuchsia
HIGHLIGHT_COLOR = "#FFFFFF" # White
DARK_DOCK_BORDER = "#111"
LIGHT_DOCK_BORDER = "#eee"

def euro_text(text):
    """Clean text while preserving most Unicode characters"""
    if not isinstance(text, str):
        return ""
    
    # Normalize Unicode (but don't remove non-ASCII)
    text = unicodedata.normalize('NFKC', text)
    
    # Remove only control characters and problematic CSV characters
    text = re.sub(r'[\x00-\x08\x0B\x0C\x0E-\x1F\x7F-\x9F]', '', text)
    text = text.replace('"', "'")
    text = text.replace('\n', ' ')
    text = text.replace('\r', ' ')
    
    return text.strip()

def html_encode_text(text):
    """Encode text with HTML entities for safe CSV storage"""
    if not isinstance(text, str):
        return ""
    
    # First clean problematic characters
    text = euro_text(text)
    
    # Encode non-ASCII characters as HTML entities
    return html.escape(text, quote=False).encode('ascii', 'xmlcharrefreplace').decode('ascii')

def html_decode_text(text):
    """Decode HTML entities back to Unicode text"""
    if not isinstance(text, str):
        return ""
    
    return html.unescape(text)

def pct_to_milliseconds(pct, duration):
    """Convert percentage to milliseconds based on duration"""
    if not isinstance(duration, (int, float)) or duration <= 0:
        return None
    
    # Handle string percentages like "84%"
    if isinstance(pct, str):
        if pct.endswith('%'):
            try:
                pct_value = float(pct[:-1])  # Remove % and convert to float
            except ValueError:
                return None
        else:
            try:
                pct_value = float(pct)
            except ValueError:
                return None
    elif isinstance(pct, (int, float)):
        pct_value = float(pct)
    else:
        return None
    
    # Clamp percentage to valid range
    pct_value = max(0.0, min(100.0, pct_value))
    
    return int((pct_value / 100.0) * duration)

def timecode_to_milliseconds(timecode):
    if not isinstance(timecode, str):
        return None

    # Accept both ',' and '.' as millisecond separators
    timecode = timecode.replace(',', '.')

    try:
        parts = timecode.split(":")
        if len(parts) != 3:
            return None

        h = int(parts[0])
        m = int(parts[1])
        s = float(parts[2])

        # Validate ranges
        if h < 0 or m < 0 or m >= 60 or s < 0 or s >= 60:
            return None

        time_ms = int((h * 3600 + m * 60 + s) * 1000)
        return time_ms

    except (ValueError, IndexError):
        return None
    
def parse_srt_time(self, time_str):
    """Convert SRT time format (HH:MM:SS,mmm or HH:MM:SS.mmm) to milliseconds"""
    # Replace ',' with '.' for compatibility
    time_str = time_str.replace(',', '.')
    ms = timecode_to_milliseconds(time_str)
    if ms is None:
        raise ValueError(f"Invalid SRT time format: '{time_str}'")
    return ms

def milliseconds_to_timecode(milliseconds, include_milliseconds=True):
    """
    Convert milliseconds to timecode string.
    
    Args:
        milliseconds (int): Time in milliseconds
        include_milliseconds (bool): Whether to include milliseconds in output
        
    Returns:
        str: Timecode in format "HH:MM:SS" or "HH:MM:SS.sss"
    """
    if not isinstance(milliseconds, (int, float)) or milliseconds < 0:
        return "00:00:00"
    
    # Convert to total seconds
    total_seconds = milliseconds / 1000.0
    
    # Extract hours, minutes, seconds
    h = int(total_seconds // 3600)
    m = int((total_seconds % 3600) // 60)
    s = total_seconds % 60
    
    if include_milliseconds:
        return f"{h:02d}:{m:02d}:{s:06.3f}"
    else:
        return f"{h:02d}:{m:02d}:{int(s):02d}"