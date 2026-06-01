"""Book data module — create, delete, list, and manage PDF books.

Books live inside:
    <project>/output/books/<slug>/

Each book folder contains:
    book.json   — metadata (slug, pdf reference, page count)
    book.pdf    — imported PDF (optional)
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------

def books_dir(project_path: str) -> Path:
    """Return the root books directory for the project."""
    return Path(project_path) / "output" / "books"


def book_dir(project_path: str, slug: str) -> Path:
    """Return the directory for a specific book."""
    return books_dir(project_path) / slug


def book_json_path(project_path: str, slug: str) -> Path:
    """Return the path to a book's book.json file."""
    return book_dir(project_path, slug) / "book.json"


# ---------------------------------------------------------------------------
# Load / save
# ---------------------------------------------------------------------------

def load_book(project_path: str, slug: str) -> dict:
    """Load and return a book's metadata dict. Raises FileNotFoundError if absent."""
    path = book_json_path(project_path, slug)
    if not path.exists():
        raise FileNotFoundError(f"Book not found: {slug}")
    return json.loads(path.read_text(encoding="utf-8"))


def save_book(project_path: str, slug: str, data: dict) -> None:
    """Write *data* to a book's book.json."""
    path = book_json_path(project_path, slug)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------

def list_books(project_path: str) -> list[dict]:
    """Return all books as a list of metadata dicts, sorted by slug."""
    root = books_dir(project_path)
    if not root.exists():
        return []
    result = []
    for entry in sorted(root.iterdir()):
        if not entry.is_dir():
            continue
        json_path = entry / "book.json"
        if not json_path.exists():
            continue
        try:
            data = json.loads(json_path.read_text(encoding="utf-8"))
            result.append(data)
        except Exception:
            continue
    return result


def create_book(project_path: str, slug: str) -> dict:
    """Create a new book directory and book.json. Raises FileExistsError if exists."""
    folder = book_dir(project_path, slug)
    if folder.exists():
        raise FileExistsError(f"Book already exists: {slug}")
    folder.mkdir(parents=True, exist_ok=False)
    data = {
        "slug": slug,
        "pdf": None,
        "page_count": 0,
    }
    save_book(project_path, slug, data)
    return data


def delete_book(project_path: str, slug: str) -> None:
    """Delete a book directory entirely. Raises FileNotFoundError if absent."""
    folder = book_dir(project_path, slug)
    if not folder.exists():
        raise FileNotFoundError(f"Book not found: {slug}")
    shutil.rmtree(str(folder))


# ---------------------------------------------------------------------------
# PDF import
# ---------------------------------------------------------------------------

def import_pdf(project_path: str, slug: str, pdf_source: str, *, force: bool = False) -> dict:
    """Copy *pdf_source* into the book folder and update book.json.

    Returns the updated book metadata dict.
    Raises FileNotFoundError if the source PDF or book do not exist.
    Raises FileExistsError if a PDF is already present and *force* is False.
    """
    import fitz  # PyMuPDF

    src = Path(pdf_source)
    if not src.exists():
        raise FileNotFoundError(f"Source PDF not found: {pdf_source}")

    folder = book_dir(project_path, slug)
    if not folder.exists():
        raise FileNotFoundError(f"Book not found: {slug}")

    dest = folder / "book.pdf"
    if dest.exists() and not force:
        raise FileExistsError(
            f"PDF already imported for '{slug}'. Use --force to overwrite."
        )

    shutil.copy2(str(src), str(dest))

    # Count pages
    doc = fitz.open(str(dest))
    page_count = len(doc)
    doc.close()

    data = load_book(project_path, slug)
    data["pdf"] = "book.pdf"
    data["page_count"] = page_count
    save_book(project_path, slug, data)
    return data
