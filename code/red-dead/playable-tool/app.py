DEBUG = False  # Set to True to enable debug output

from PyQt5 import sip
sip.setdestroyonexit(False)

# System
import sys

# Qt
from PyQt5.QtWidgets import QApplication
from PyQt5.QtMultimedia import QMediaContent

# UI and styling
from ui import UI
from keyboard import GlobalKeyFilter

# Window imports
from nickelodeon import NickelodeonWindow
from playhouse import PlayhouseWindow
from shotlist import ShotlistWindow
from playlist import PlaylistWindow
from cinematheque import CinemathequeWindow
from playbill import PlaybillWindow
from prompt import PromptWindow
from subtitles import SubtitlesWindow
from inference import InferenceWindow
from caption import CaptionWindow
from robots import RobotsWindow

# Layout and coordination
from layout import (
    PlayableCinemaMainWindow, save_preferences, load_preferences,
    save_window_geometry, save_dock_layout, load_window_geometry, load_dock_layout
)
from switchboard import Switchboard

def main():
    """
    Main application entry point.
    Sets up the Qt application, creates all windows, establishes connections,
    and handles startup/shutdown sequences.
    """
    # === Qt Application Setup ===
    QApplication.setStyle("Fusion")
    app = QApplication(sys.argv)
    
    # Load UI styling and fonts
    ui = UI()
    set_visual_style(app, ui)

    # === Window Creation ===
    # Some windows need references to others, so create in dependency order
    subtitles_window = SubtitlesWindow(ui)

    # Create all application windows
    windows = {
        "inference": InferenceWindow(ui),
        "captions": CaptionWindow(ui, subtitles_window),
        "subtitles": subtitles_window,
        "prompt": PromptWindow(ui, subtitles_window),
        "playhouse": PlayhouseWindow(ui),
        "playbill": PlaybillWindow(ui),
        "nickelodeon": NickelodeonWindow(ui),
        "shotlist": ShotlistWindow(ui),
        "playlist": PlaylistWindow(ui),
        "cinematheque": CinemathequeWindow(ui),
        "robots": RobotsWindow(ui),
    }

    # === Signal Setup ===
    # Connect preference save/load signals for all windows
    for win in windows.values():
        win.preferences_save.connect(win.on_preferences_save)
        win.preferences_load.connect(win.on_preferences_load)

    # === Main Window Setup ===
    main_window = PlayableCinemaMainWindow(windows)
    main_window.show()

    # === Central Coordination ===
    # Switchboard handles all inter-window communication
    switchboard = Switchboard(windows)

    # === Input Handling ===
    # Global keyboard shortcuts
    keyboard = GlobalKeyFilter(windows, main_window)
    app.installEventFilter(keyboard)

    # === Window State Restoration ===
    # Restore saved window layout and preferences
    load_window_geometry(main_window)
    load_dock_layout(main_window)
    main_window.update()
    load_preferences(windows, main_window)

    # === Shutdown Handlers ===
    # Save state when application exits
    app.aboutToQuit.connect(lambda: save_preferences(windows, main_window))
    app.aboutToQuit.connect(lambda: save_dock_layout(main_window))
    app.aboutToQuit.connect(lambda: save_window_geometry(main_window))

    # Clean shutdown for media players
    def clean_quit():
        """Ensure media players and threads terminate properly before app exits"""
        if DEBUG: print("DEBUG: Starting clean quit sequence")

        try:
            # Stop QMediaPlayer instances first
            if "nickelodeon" in windows:
                if hasattr(windows["nickelodeon"], 'player') and windows["nickelodeon"].player:
                    if DEBUG: print("DEBUG: Stopping Nickelodeon media player")
                    windows["nickelodeon"].player.media_player.stop()
                    windows["nickelodeon"].player.media_player.setMedia(QMediaContent())
            if "playhouse" in windows:
                if hasattr(windows["playhouse"], 'player') and windows["playhouse"].player:
                    if DEBUG: print("DEBUG: Stopping Playhouse media player")
                    windows["playhouse"].player.media_player.stop()
                    windows["playhouse"].player.media_player.setMedia(QMediaContent())

            # Stop catalog/cinematheque threads
            for win_name in ("cinematheque", "playbill"):  # adjust to actual keys using Catalog
                win = windows.get(win_name)
                if not win:
                    continue
                for t in (
                    getattr(win, "metadata_thread", None),
                    getattr(win, "loading_thread", None)
                ):
                    if t and t.isRunning():
                        if DEBUG: print(f"DEBUG: Stopping thread in {win_name}")
                        t.quit()
                        t.wait()

        except Exception as e:
            if DEBUG: print(f"DEBUG: Error during quit cleanup: {e}")

        # Close all windows
        for name, window in windows.items():
            try:
                if DEBUG: print(f"DEBUG: Closing window: {name}")
                window.close()
            except Exception as e:
                if DEBUG: print(f"DEBUG: Error closing window {name}: {e}")

        import gc
        gc.collect()
        if DEBUG: print("DEBUG: Clean quit sequence completed")

    app.aboutToQuit.connect(clean_quit)
    
    # === Start Application ===
    sys.exit(app.exec_())

# ================================================================
# STYLING
# ================================================================

def set_visual_style(app, ui):
    """Apply visual styling to the entire application"""
    app.setStyleSheet(ui.get_style_sheet("app"))

# ================================================================
# APPLICATION ENTRY POINT
# ================================================================

if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        import traceback
        traceback.print_exc()
