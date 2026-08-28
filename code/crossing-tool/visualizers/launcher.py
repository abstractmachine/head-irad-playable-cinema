"""Central visualizer launcher.

This module provides the canonical standalone visualizer startup paths.

Two shared entry points live here:

- `run_visualizer_window()` — the plain launch pattern used by most
  visualizers: raise an existing in-process window if one exists, otherwise
  get-or-create the `QApplication`, apply the shared theme, construct the
  window, show it, and (only if this call created the `QApplication`) run
  the event loop. Visualizer modules should keep only their own thin
  `run_visualizer()` wrapper that builds the window with its
  project/media-specific arguments and calls this helper.

- `launch_visualizer()` — the Illustration-specific path. Illustration must
  never have its `QWidget` hierarchy constructed inside a process that
  already has a running `QApplication` (e.g. when opened from another
  visualizer's in-process window), so instead of constructing in-process it
  spawns an independent OS process. Visualizers with the same requirement
  can be added here later; do not use this for visualizers that are fine
  being constructed in-process (use `run_visualizer_window()` instead).
"""
from __future__ import annotations

import sys
from typing import Callable, Optional
from pathlib import Path
import subprocess as _sp

from PyQt5.QtWidgets import QApplication, QWidget

from styles import theme
from tool.shortcuts import install_keyboard_manager

# The window-raise helper is safe to import at module level.
from visualizers._window_helpers import raise_existing_window


def run_visualizer_window(
    subcommand: str,
    build_window: Callable[[], QWidget],
    *,
    check_existing: bool = True,
    post_show: Optional[Callable[[QWidget], None]] = None,
) -> Optional[QWidget]:
    """Shared standalone launch path for visualizers with no cross-process
    spawn requirement.

    Raises an already-open in-process window matching *subcommand* (unless
    *check_existing* is False). Otherwise checks the cross-process
    single-instance guard (`visualizers.components.singleton_guard`): if
    another OS process already owns *subcommand* for the current project,
    that process is pinged to raise its window and this call returns
    without creating a duplicate. Otherwise gets or creates the
    `QApplication` (applying the shared theme only when this call creates
    it), calls `build_window()` to construct the visualizer's main window,
    shows it, calls *post_show* with the window if supplied (for the rare
    bit of per-visualizer setup that must happen after `show()` but before
    the event loop runs), and — only if this call created the
    `QApplication` — runs the event loop and exits the process when it
    finishes.

    Returns the constructed window, or None if an existing window was
    raised instead (in-process or in another process).
    """
    if check_existing and raise_existing_window(subcommand):
        return None

    app = QApplication.instance()
    created_app = app is None
    if created_app:
        app = QApplication(sys.argv)
        theme.apply_theme(app)
    # Guaranteed regardless of which visualizer class is built below, so
    # every window gets F1-F10/F12/Tab/Shift+Tab even when it happens to be
    # the first window opened in a fresh process.
    install_keyboard_manager(app)

    from tool import prefs as _prefs
    from visualizers.components.singleton_guard import claim_or_ping_and_bind
    if not claim_or_ping_and_bind(subcommand, _prefs.get("path") or "", app):
        if created_app:
            sys.exit(0)
        return None

    win = build_window()
    win.show()
    if post_show is not None:
        post_show(win)

    if created_app:
        sys.exit(app.exec_())
    return win


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
    initial_source_tab: str = "silhouettes",
) -> None:
    """Launch a visualizer that must never be constructed in-process when
    another `QApplication` is already running (spawns an independent OS
    process in that case instead). Currently only `subcommand == "illustration"`
    needs this; other visualizers should use `run_visualizer_window()`.
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
            initial_source_tab=initial_source_tab,
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
        # ALL_MEDIA is the literal "--all-media--" query sentinel. It must
        # be supplied with '=' so argparse does not parse it as an option.
        f"--media={media_type}",
    ]
    if initial_film:
        cmd += ["--film", initial_film]
    if initial_field:
        cmd += ["--field", initial_field]
    if initial_label:
        cmd += ["--label", initial_label]
    if initial_shot:
        cmd += ["--shot", str(initial_shot)]
    if initial_source_tab == "engravings":
        cmd += ["--source-tab", initial_source_tab]

    try:
        _sp.Popen(cmd)
    except Exception:
        # Best-effort: we do not want to crash the caller if spawning fails.
        pass
    return None
