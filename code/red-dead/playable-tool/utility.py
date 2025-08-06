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