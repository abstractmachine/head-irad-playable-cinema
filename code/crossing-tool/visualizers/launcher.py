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
from pathlib import Path
import subprocess as _sp

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

    # If there's no QApplication, behave like the CLI/standalone path:
    # create the QApplication, apply theme and construct the window.
    app = QApplication.instance()
    if app is None:
        created_app = True
        app = QApplication(sys.argv)
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

        # CLI / standalone case: enter the event loop and exit the process
        # when it finishes to preserve the previous behaviour.
        sys.exit(app.exec_())

    # If we reached here an application is already running in this process.
    # Per the new architecture, do NOT construct another visualizer's
    # QWidget hierarchy inside this process. Instead spawn an independent
    # process that will create its own QApplication and window.
    vis_script = Path(__file__).parent / "illustration_visualizer.py"
    cmd = [
        sys.executable, str(vis_script),
        "--project", project_path,
        "--media", media_type,
    ]
    if initial_film:
        cmd += ["--film", initial_film]
    if initial_field:
        cmd += ["--field", initial_field]
    if initial_label:
        cmd += ["--label", initial_label]
    if initial_shot:
        cmd += ["--shot", str(initial_shot)]

    try:
        _sp.Popen(cmd)
    except Exception:
        # Best-effort: we do not want to crash the caller if spawning fails.
        pass
    return None
