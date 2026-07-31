"""Central visualizer launcher.

This module provides a canonical standalone visualizer startup path.

Step 1: support the Illustration visualizer only. Callers should use
`launch_visualizer()` to reproduce the exact behaviour the CLI currently
uses (raise existing in-process window, create/reuse QApplication,
apply theme, construct the window, show it and exec the event loop).

Do not migrate callers in this change; this module is introduced as an
implementation detail so visualizers can be launched from one place later.
"""
from __future__ import annotations

import sys
from typing import Optional

from PyQt5.QtWidgets import QApplication

from styles import theme

# The window-raise helper is safe to import at module level.
from visualizers._window_helpers import raise_existing_window


def launch_visualizer(
    subcommand: str,
    project_path: str,
    media_type: Optional[str] = "movie",
    model_name: Optional[str] = None,
    field: Optional[str] = None,
    initial_film: Optional[str] = None,
    initial_field: Optional[str] = None,
    initial_label: Optional[str] = None,
    initial_shot: Optional[str] = None,
) -> None:
    """Launch a visualizer in a standalone process-like path.

    For this first step only `subcommand == "illustration"` is supported
    and the behaviour matches the previous CLI entrypoint in
    `visualizers.illustration_visualizer.run_visualizer()`.
    """
    from tool import prefs as _prefs

    if subcommand != "illustration":
        raise NotImplementedError("launch_visualizer currently supports only 'illustration'")

    # If an in-process window exists, raise it and return immediately.
    if raise_existing_window("illustration"):
        return

    # Create or reuse QApplication instance and apply the shared theme.
    app = QApplication.instance() or QApplication(sys.argv)
    theme.apply_theme(app)

    # Determine model_name using the same prefs fallback used previously.
    if model_name is None:
        model_name = _prefs.get("model_segmentation", "sam3.pt") or "sam3.pt"

    # Import the visualizer window lazily to avoid import cycles at module
    # import time; the caller (CLI) will have imported the module already.
    from visualizers.illustration_visualizer import IllustrationWindow

    win = IllustrationWindow(
        project_path,
        media_type=media_type,
        model_name=model_name,
        initial_film=initial_film or (field and None),
        initial_field=initial_field or field,
        initial_label=initial_label,
        initial_shot=initial_shot,
    )
    win.show()
    sys.exit(app.exec_())
