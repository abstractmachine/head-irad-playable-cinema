DEBUG = False  # Set to True to enable debug output

# System
import sys

# Qt
from PyQt5.QtWidgets import QApplication

# UI and styling
from ui import UI
from key_filter import GlobalKeyFilter

# Window imports
from nickelodeon import NickelodeonWindow
from playhouse import PlayhouseWindow
from playbill import PlaybillWindow
from shotlist import ShotlistWindow
from caption import CaptionWindow
from cinematheque import CinemathequeWindow
from prompt import PromptWindow
from subtitles import SubtitlesWindow
from inference import InferenceWindow
from project import ProjectWindow

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
        "project": ProjectWindow(ui),
        "inference": InferenceWindow(ui),
        "captions": CaptionWindow(ui, subtitles_window),
        "subtitles": subtitles_window,
        "prompt": PromptWindow(ui, subtitles_window),
        "playhouse": PlayhouseWindow(ui),
        "playbill": PlaybillWindow(ui),
        "nickelodeon": NickelodeonWindow(ui),
        "shotlist": ShotlistWindow(ui),
        "cinematheque": CinemathequeWindow(ui),
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
    key_filter = GlobalKeyFilter(windows, main_window)
    app.installEventFilter(key_filter)

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

    # Clean shutdown for VLC players
    def clean_quit():
        """Ensure VLC players terminate properly before app exits"""
        if DEBUG: print("DEBUG: Starting clean quit sequence")
        
        try:
            # Stop VLC players first
            if "nickelodeon" in windows:
                windows["nickelodeon"].player.terminate()
            if "playhouse" in windows:
                windows["playhouse"].player.terminate()
        except Exception as e:
            if DEBUG: print(f"DEBUG: Error stopping VLC players: {e}")
        
        # Close all windows properly
        for name, window in windows.items():
            try:
                if DEBUG: print(f"DEBUG: Closing window: {name}")
                window.close()
            except Exception as e:
                if DEBUG: print(f"DEBUG: Error closing window {name}: {e}")
        
        # Force garbage collection
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
