import unicodedata
import re
import html

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

def timecode_to_milliseconds(timecode):
    """
    Convert timecode string (HH:MM:SS or HH:MM:SS.sss) to milliseconds.
    
    Args:
        timecode (str): Timecode in format "HH:MM:SS" or "HH:MM:SS.sss"
        
    Returns:
        int: Time in milliseconds, or None if invalid format
    """
    if not isinstance(timecode, str):
        return None
        
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