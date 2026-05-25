"""
mcp_server/test_image_server.py — Standalone minimal MCP server for image-rendering diagnostics.

This server has NO Crossing dependencies.  Run it separately from the main
Crossing server to isolate whether Claude Desktop can render images from MCP
at all, independent of the Crossing stack.

Usage (Claude Desktop config):
  {
    "mcpServers": {
      "crossing-image-test": {
        "command": "ssh",
        "args": ["playable-cinema",
          "bash -lc 'cd /path/to/crossing-tool && uv run python mcp_server/test_image_server.py'"
        ]
      }
    }
  }

Test sequence:
  1. Call test_jpeg  → should render a 40×30 orange gradient inline
  2. Call test_png   → should render a 40×30 blue gradient inline
  3. Call test_large → should render a 320×240 gradient inline (~30 KB JPEG)
  4. If any render, the MCP image pipeline works; adapt Crossing to that format.
  5. If none render, check Claude Desktop version and MCP protocol support.
"""
from __future__ import annotations

import io
import json
import sys
from pathlib import Path

_DIR = Path(__file__).resolve().parent.parent
if str(_DIR) not in sys.path:
    sys.path.insert(0, str(_DIR))

from mcp.server.fastmcp import FastMCP, Image

mcp = FastMCP("crossing-image-test")


def _gradient(width: int, height: int, r_scale: float, g_scale: float, b_base: int):
    """Return a PIL Image with a colour gradient for visual identification."""
    from PIL import Image as PILImage
    img = PILImage.new("RGB", (width, height))
    px  = img.load()
    for x in range(width):
        for y in range(height):
            px[x, y] = (
                min(255, int(x * r_scale)),
                min(255, int(y * g_scale)),
                b_base,
            )
    return img


@mcp.tool()
def test_jpeg() -> list:
    """Return a 40×30 JPEG gradient.  Should render inline if JPEG is supported."""
    img = _gradient(40, 30, 6.3, 8.5, 120)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=85)
    data = buf.getvalue()
    return [
        json.dumps({"format": "jpeg", "bytes": len(data), "size": "40×30",
                    "expected": "orange-to-green gradient"}),
        Image(data=data, format="jpeg"),
    ]


@mcp.tool()
def test_png() -> list:
    """Return a 40×30 PNG gradient.  Should render inline if PNG is supported."""
    img = _gradient(40, 30, 2.0, 4.0, 220)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    data = buf.getvalue()
    return [
        json.dumps({"format": "png", "bytes": len(data), "size": "40×30",
                    "expected": "blue-tinted gradient"}),
        Image(data=data, format="png"),
    ]


@mcp.tool()
def test_large() -> list:
    """Return a 320×240 JPEG (~30 KB).  Tests whether payload size matters."""
    img = _gradient(320, 240, 0.8, 1.0, 80)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=85)
    data = buf.getvalue()
    return [
        json.dumps({"format": "jpeg", "bytes": len(data), "size": "320×240",
                    "expected": "larger gradient — tests 30 KB payload"}),
        Image(data=data, format="jpeg"),
    ]


@mcp.tool()
def test_bare_image() -> Image:
    """Return ONLY an Image object — no metadata string, no list wrapper."""
    img = _gradient(40, 30, 6.3, 8.5, 200)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=85)
    return Image(data=buf.getvalue(), format="jpeg")


if __name__ == "__main__":
    mcp.run()
