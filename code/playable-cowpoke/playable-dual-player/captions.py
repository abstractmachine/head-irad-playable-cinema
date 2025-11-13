import re
from pathlib import Path

def check_encoded_files(csv_path: Path, model_name="bge-small-en-v1.5"):
    """
    Given e.g. 'Breakheart-Pass(1975){tmdb-8043}.csv',
    return paths to expected .txt and .npy files if they exist.
    """
    base = csv_path.with_suffix("")  # remove .csv extension
    txt_path = base.with_suffix(".txt")
    npy_path = base.with_suffix(f".npy")
    return txt_path if txt_path.exists() else None, npy_path if npy_path.exists() else None

def format_caption(caption: str) -> str:
    # Add newline after { and before }
    formatted = re.sub(r"\{", "{\n", caption)
    formatted = re.sub(r"\}", "\n}", formatted)
    # Put each "string": on its own line
    formatted = re.sub(r'("[^"]*")\s*:', r"\n\1:", formatted)
    # Collapse excessive newlines
    formatted = re.sub(r"\n+", "\n", formatted)
    return formatted.strip()

