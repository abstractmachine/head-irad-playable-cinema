"""
mcp_server/mcp.py — MCP server for the crossing CLI tool.

HOW TO RUN (on the Ubuntu server)
----------------------------------
1. Install the MCP library (once):
       uv add "mcp[cli]"

2. Set the project path via the CLI (saves to ~/.crossing/prefs.json):
       crossing tool path /path/to/your/project
   The server reads this saved preference automatically.
   You can also override it at runtime with CROSSING_PROJECT=/path if needed.

3. Run the server for testing:
       uv run python mcp_server/mcp.py

The server speaks stdio only — it is intended to be launched on-demand by the client.

HOW TO CONFIGURE CLAUDE DESKTOP (macOS/Windows, via SSH over Tailscale)
------------------------------------------------------------------------
Edit ~/Library/Application Support/Claude/claude_desktop_config.json
(macOS) or %APPDATA%\Claude\claude_desktop_config.json (Windows):

{
  "mcpServers": {
    "crossing": {
      "command": "ssh",
      "args": [
        "playable-cinema",
        "bash -lc 'cd /path/to/crossing-tool && uv run python mcp_server/mcp.py'"
      ]
    }
  }
}

Replace "playable-cinema" with your SSH host alias (or user@hostname) and update
the path to crossing-tool. The server uses whichever project path was last set
with "crossing tool path" on the server. Define the SSH alias in ~/.ssh/config
on the Mac to avoid repeating connection details.
"""

import json
import os
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Ensure the package root is importable when this script is run directly,
# regardless of the working directory.
# ---------------------------------------------------------------------------
_SCRIPT_DIR = Path(__file__).resolve().parent.parent  # crossing-tool root
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))

from mcp.server.fastmcp import FastMCP
from data.metadata import get_metadata
from tool import prefs as _prefs

# ---------------------------------------------------------------------------
# Project path resolution
# ---------------------------------------------------------------------------

def _resolve_project_path() -> str | None:
    """Return the project path from CROSSING_PROJECT env var or saved prefs."""
    env_path = os.environ.get("CROSSING_PROJECT")
    if env_path:
        return env_path
    return _prefs.get("path")


# ---------------------------------------------------------------------------
# MCP server
# ---------------------------------------------------------------------------

mcp = FastMCP("crossing")


@mcp.tool()
def list_movies(media_type: str = "movies") -> str:
    """Return film metadata for the configured crossing project as a JSON string.

    Args:
        media_type: Which media library to query — "movies" (default) or "gameplay".
    """
    if media_type not in ("movies", "gameplay"):
        return json.dumps({"error": f"Invalid media_type {media_type!r}. Must be 'movies' or 'gameplay'."})

    project_path = _resolve_project_path()
    if not project_path:
        return json.dumps({
            "error": (
                "No project path configured. "
                "Set the CROSSING_PROJECT environment variable or run: crossing path /your/project"
            )
        })

    if not Path(project_path).is_dir():
        return json.dumps({"error": f"Project path does not exist or is not a directory: {project_path}"})

    try:
        entries = get_metadata(project_path, media_type=media_type)
        return json.dumps(entries, indent=2)
    except Exception as exc:
        return json.dumps({"error": str(exc)})


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    mcp.run(transport="stdio")
