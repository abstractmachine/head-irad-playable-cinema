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


_DISCORD_MAX_LEN = 2000


def _split_message(message: str, limit: int = _DISCORD_MAX_LEN) -> list[str]:
    """Split *message* into chunks that each fit within *limit* characters.

    Splits on newline boundaries where possible so lines are never broken.
    """
    if len(message) <= limit:
        return [message]

    chunks: list[str] = []
    current: list[str] = []
    current_len = 0

    for line in message.splitlines(keepends=True):
        if current_len + len(line) > limit:
            if current:
                chunks.append("".join(current))
            # If a single line exceeds the limit, hard-split it.
            while len(line) > limit:
                chunks.append(line[:limit])
                line = line[limit:]
            current = [line]
            current_len = len(line)
        else:
            current.append(line)
            current_len += len(line)

    if current:
        chunks.append("".join(current))

    return chunks


def _post_chunk(webhook_url: str, chunk: str) -> bool:
    """POST a single chunk to *webhook_url*. Returns True on success."""
    payload = json.dumps({"content": chunk}).encode("utf-8")
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


def discord_notify(message: str, project_path: str) -> bool:
    """Post *message* to the configured Discord webhook.

    Long messages are automatically split into multiple requests to stay within
    Discord's 2000-character content limit.

    Returns True if all chunks were sent successfully, False otherwise (errors
    are printed to stdout so they don't interrupt batch processes).

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

    chunks = _split_message(message)
    return all(_post_chunk(webhook_url, chunk) for chunk in chunks)
