#!/bin/bash

# Usage:
# sudo apt update
# sudo apt install qpdf
# ./rotate-even-pages.sh input.pdf

if [ $# -ne 1 ]; then
    echo "Usage: $0 input.pdf"
    exit 1
fi

INPUT="$1"
BASENAME="${INPUT%.pdf}"
OUTPUT="${BASENAME}_evenpages_rotated.pdf"

qpdf "$INPUT" --rotate=+180:1-z:even -- "$OUTPUT"

echo "Created: $OUTPUT"