#!/bin/bash
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
exec "$HOME/.local/bin/uv" --directory "$SCRIPT_DIR" run python crossing_mcp.py
