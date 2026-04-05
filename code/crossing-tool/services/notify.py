"""Discord notification helper for long-running CLI processes."""

from __future__ import annotations

import urllib.request
import urllib.error
import json
from pathlib import Path


def _read_webhook(project_path: str) -> str | None:
    """Return the Discord webhook URL stored in preferences/keys/discord_api_key.txt.

    Returns None (silently) if the file does not exist, so callers that pass
    --notify get a graceful warning rather than a hard failure.
    """
    key_file = Path(project_path) / "preferences" / "keys" / "discord_api_key.txt"
    if not key_file.exists():
        return None
    url = key_file.read_text().strip()
    return url or None


def discord_notify(message: str, project_path: str) -> bool:
    """Post *message* to the configured Discord webhook.

    Returns True on success, False on any error (errors are printed to stdout
    so they don't interrupt batch processes).

    Args:
        message:      The text to send (plain string; no markdown required).
        project_path: Root path of the crossing project, used to locate the
                      key file at preferences/keys/discord_api_key.txt.
    """
    webhook_url = _read_webhook(project_path)
    if not webhook_url:
        print(
            "⚠ Discord notification skipped — no webhook URL set.\n"
            "  Run: crossing tool api_key set discord <your-webhook-url>"
        )
        return False

    payload = json.dumps({"content": message}).encode("utf-8")
    req = urllib.request.Request(
        webhook_url,
        data=payload,
        headers={
            "Content-Type": "application/json",
            "User-Agent": "crossing-tool/1.0",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status in (200, 204)
    except urllib.error.HTTPError as exc:
        body = ""
        try:
            body = exc.read().decode("utf-8", errors="replace")
        except Exception:
            pass
        detail = f" — {body}" if body else ""
        print(f"⚠ Discord notification failed: HTTP {exc.code} {exc.reason}{detail}")
        return False
    except Exception as exc:
        print(f"⚠ Discord notification failed: {exc}")
        return False
