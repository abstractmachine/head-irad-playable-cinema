#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT_DIR"

echo "Building Dead Crossing Presskit..."
echo

if [ ! -f "dead-crossing.md" ]; then
  echo "✗ Missing dead-crossing.md"
  exit 1
fi

echo "✓ Markdown loaded"

if [ ! -d "images" ]; then
  echo "✗ Missing images directory"
  exit 1
fi

echo "✓ Images found"

if [ ! -d ".venv" ]; then
  echo "✗ Python virtual environment not found"
  echo
  echo "Create it with:"
  echo "  python3 -m venv .venv"
  echo "  source .venv/bin/activate"
  echo "  pip install weasyprint"
  exit 1
fi

source .venv/bin/activate

pandoc dead-crossing.md \
  --from markdown+hard_line_breaks \
  --pdf-engine=weasyprint \
  --no-highlight \
  --template=templates/default.html5 \
  --resource-path=.:images:styles \
  --css=styles/type.css \
  --css=styles/base.css \
  --css=styles/print.css \
  --css=styles/cover.css \
  -o dead-crossing.pdf

echo "✓ PDF generated"
echo
echo "Output:"
echo "dead-crossing.pdf"

if command -v xdg-open >/dev/null 2>&1; then
  xdg-open dead-crossing.pdf >/dev/null 2>&1 &
fi
