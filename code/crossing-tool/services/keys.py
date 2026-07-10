"""API key accessor for crossing services.

Keys are stored at::

    <project>/preferences/keys/<service>_api_key.txt

Set a key with::

    crossing tool key set openai sk-...
"""

from __future__ import annotations

from pathlib import Path


class MissingKeyError(RuntimeError):
    """Raised when a required API key is not configured."""


def get_key(service: str, project_path: str | None = None) -> str:
    """Return the stored API key for *service*.

    Parameters
    ----------
    service:
        Service name, e.g. ``"openai"``, ``"discord"``.
    project_path:
        Absolute path to the crossing project directory.  When omitted the
        active project path is read from tool preferences.

    Raises
    ------
    MissingKeyError
        If no project path is set or the key file does not exist.
    """
    if not project_path:
        from tool import prefs as _prefs
        project_path = _prefs.get("path")

    if not project_path:
        raise MissingKeyError(
            "No project path set.\n"
            "Run: crossing tool path /path/to/project"
        )

    key_file = Path(project_path) / "preferences" / "keys" / f"{service}_api_key.txt"
    if not key_file.exists():
        raise MissingKeyError(
            f"{service} API key not configured.\n"
            f"Set it with: crossing tool key set {service} <your-key>"
        )

    return key_file.read_text(encoding="utf-8").strip()
