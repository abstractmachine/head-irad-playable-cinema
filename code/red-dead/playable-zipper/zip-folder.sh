#!/bin/bash

# Usage:
# ./zip-tool.sh folder-name [output.zip]

# --- Argument parsing ---
SOURCE_FOLDER=$1
ARCHIVE_NAME=${2:-"${SOURCE_FOLDER}.zip"}

if [ -z "$SOURCE_FOLDER" ]; then
  echo "❌ Error: You must provide a folder name to zip."
  echo "Usage: ./zip-tool.sh folder-name [output.zip]"
  exit 1
fi

# --- Create temp staging directory ---
TEMP_DIR="/tmp/clean-zip-$$"
mkdir -p "$TEMP_DIR"

# --- Copy only clean files ---
rsync -a \
  --exclude=".DS_Store" \
  --exclude="__MACOSX" \
  --exclude=".*" \
  "$SOURCE_FOLDER"/ "$TEMP_DIR/$SOURCE_FOLDER"/

# --- Zip it using ditto ---
ditto -c -k --keepParent "$TEMP_DIR/$SOURCE_FOLDER" "$ARCHIVE_NAME"

# --- Cleanup ---
rm -rf "$TEMP_DIR"

echo "✅ Created clean zip: $ARCHIVE_NAME"