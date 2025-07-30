DEBUG = False  # Set to True to enable debug output

import sys
import json
import os
import subprocess
from PyQt5.QtWidgets import QApplication, QLineEdit, QTextEdit
from PyQt5.QtWidgets import QTabWidget
from PyQt5.QtWidgets import QMainWindow, QDockWidget, QWidget

from PyQt5.QtCore import QObject, QEvent
from PyQt5.QtCore import Qt
from PyQt5.QtGui import QFont

# Import our UI class
from ui import UI

# our other windows
from nickelodeon import NickelodeonWindow
from playhouse import PlayhouseWindow
from shotlist import ShotlistWindow
from caption import CaptionWindow
from cinematheque import CinemathequeWindow
from prompt import PromptWindow
from subtitles import SubtitlesWindow
from inference import InferenceWindow

PREFS_PATH = "./preferences/preferences.json"

class PlayableCinemaMainWindow(QMainWindow):
    def __init__(self, windows):
        super().__init__()
        self.setWindowTitle("Playable Cinema")
        self.setDockOptions(QMainWindow.AllowNestedDocks | QMainWindow.AllowTabbedDocks)
        self.resize(1600, 900)

        central = QWidget()
        self.setCentralWidget(central)

        # Add all other windows as dock widgets
        self.add_dock("Shotlist", windows["shotlist"], Qt.RightDockWidgetArea)
        self.add_dock("Captions", windows["captions"], Qt.BottomDockWidgetArea)
        self.add_dock("Prompts", windows["prompt"], Qt.BottomDockWidgetArea)
        self.add_dock("Subtitles", windows["subtitles"], Qt.BottomDockWidgetArea)
        self.add_dock("Inference", windows["inference"], Qt.BottomDockWidgetArea)
        self.add_dock("Nickelodeon", windows["nickelodeon"], Qt.LeftDockWidgetArea)
        self.add_dock("Playhouse", windows["playhouse"], Qt.LeftDockWidgetArea)
        self.add_dock("Cinemathèque", windows["cinematheque"], Qt.LeftDockWidgetArea)

    def add_dock(self, name, widget, area):
        dock = QDockWidget(name, self)
        dock.setWidget(widget)
        dock.setObjectName(name)
        dock.setFloating(False)
        dock.setAllowedAreas(Qt.AllDockWidgetAreas)
        self.addDockWidget(area, dock)

def is_dark_mode():
    try:
        result = subprocess.run(
            ["defaults", "read", "-g", "AppleInterfaceStyle"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
        )
        return "Dark" in result.stdout
    except Exception:
        return False

def main():
    # Initialize the application
    app = QApplication(sys.argv)

    # Create UI instance with all fonts loaded
    ui = UI()

    # Set the visual style for the application
    set_visual_style(app)

    # Several window are going to need to access subtitles, so we create it first
    subtitles_window = SubtitlesWindow(ui)

    # create a dictionary of our windows, passing ui to each
    windows = {
        "subtitles": subtitles_window,
        "captions": CaptionWindow(ui, subtitles_window),
        "prompt": PromptWindow(ui, subtitles_window),
        "nickelodeon": NickelodeonWindow(ui),
        "playhouse": PlayhouseWindow(ui),
        "shotlist": ShotlistWindow(ui),
        "cinematheque": CinemathequeWindow(ui),
        "inference": InferenceWindow(ui)
    }

    # Install global key filter
    key_filter = GlobalKeyFilter(windows)
    app.installEventFilter(key_filter)

    # Connect signals for preferences
    for win in windows.values():
        win.request_save.connect(win.on_request_save)
        win.request_load.connect(win.on_request_load)

    # The font we'll use for tab labels
    button_font = ui.get_font('button')

    # Create main tab widget for cinematheque and shotlist
    cinema_widget = QTabWidget()
    cinema_widget.addTab(windows["cinematheque"], "Cinemathèque")
    cinema_widget.addTab(windows["shotlist"], "Shotlist")
    cinema_widget.tabBar().setFont(button_font)
    cinema_widget.show()

    # Create text tab widget for captions, prompt, subtitles, and inference
    play_widget = QTabWidget()
    play_widget.addTab(windows["captions"], "Captions")
    play_widget.addTab(windows["prompt"], "Prompts")
    play_widget.addTab(windows["subtitles"], "Subtitles")
    play_widget.addTab(windows["inference"], "Inference")
    play_widget.tabBar().setFont(button_font)
    play_widget.show()

    # Signal Connections
    windows["nickelodeon"].video_loaded_with_metadata.connect(windows["shotlist"].on_movie_loaded_with_metadata)
    windows["nickelodeon"].video_timecode_changed.connect(windows["shotlist"].clear_table_selection)
    windows["nickelodeon"].video_timecode_changed.connect(windows["shotlist"].set_current_time)
    windows["nickelodeon"].frames_extracted.connect(windows["captions"].handle_api_frames)
    windows["nickelodeon"].video_loaded_with_metadata.connect(windows["cinematheque"].on_movie_loaded_with_metadata)
    windows["nickelodeon"].video_loaded_with_metadata.connect(windows["prompt"].on_movie_loaded_with_metadata)
    windows["nickelodeon"].video_loaded_with_metadata.connect(windows["captions"].on_movie_loaded_with_metadata)
    windows["nickelodeon"].video_loaded_with_metadata.connect(windows["subtitles"].on_movie_loaded_with_metadata)
    windows["nickelodeon"].video_timecode_changed.connect(windows["subtitles"].on_timecode_changed)
    windows["nickelodeon"].video_loaded_with_metadata.connect(windows["inference"].on_movie_loaded_with_metadata)
    windows["nickelodeon"].video_timecode_changed.connect(windows["inference"].on_timecode_changed)

    windows["cinematheque"].movie_selected.connect(windows["nickelodeon"].load_video_from_path_with_metadata)
    windows["cinematheque"].project_loaded.connect(windows["shotlist"].set_project_folder)
    windows["cinematheque"].project_loaded.connect(windows["captions"].set_project_folder)
    windows["cinematheque"].project_loaded.connect(windows["subtitles"].set_project_folder)
    windows["cinematheque"].project_loaded.connect(windows["inference"].set_project_folder)
    windows["cinematheque"].shotlist_bot_start.connect(windows["shotlist"].start_shotlist_bot)

    windows["shotlist"].jump_to_timecode_signal.connect(windows["nickelodeon"].jump_to_timecode)
    windows["shotlist"].shotlist_status.connect(windows["captions"].set_shotlist_status)
    windows["shotlist"].shot_caption_selected.connect(windows["captions"].set_shot_caption_field)
    windows["shotlist"].abort_api.connect(windows["captions"].handle_api_abort)
    windows["shotlist"].shot_timecodes.connect(windows["nickelodeon"].handle_shot_timecodes)
    windows["shotlist"].is_last_available_shot.connect(windows["captions"].handle_is_last_available_shot)
    windows["shotlist"].shotlist_status.connect(windows["cinematheque"].on_shotlist_status)
    windows["shotlist"].row_data.connect(windows["captions"].handle_row_data)
    windows["shotlist"].row_data.connect(windows["prompt"].handle_row_data)

    windows["captions"].shot_caption_submitted.connect(windows["shotlist"].update_shot_caption_for_current_shot)
    windows["captions"].request_current_shot.connect(windows["shotlist"].handle_request_current_shot)
    windows["captions"].request_next_shot.connect(windows["shotlist"].jump_to_next_shot)
    windows["captions"].bot_finished.connect(windows["cinematheque"].on_bot_finished)

    # Load preferences at startup
    load_preferences(windows, cinema_widget, play_widget)

    # Save preferences on exit
    app.aboutToQuit.connect(lambda: save_preferences(windows, cinema_widget, play_widget))

    main_window = PlayableCinemaMainWindow(windows)
    main_window.show()

    # Because we have a VLC player, we need to ensure it closes properly on app exit
    def clean_quit():
        try:
            windows["nickelodeon"].player.terminate()
            windows["playhouse"].player.terminate()
        except Exception:
            pass
        for window in windows.values():
            window.close()

    # Connect the clean quit function to the app's aboutToQuit signal
    app.aboutToQuit.connect(clean_quit)
    sys.exit(app.exec_())

# Set the visual style for the application
def set_visual_style(app):
    """Set the visual style for the application"""

    if is_dark_mode():
        app.setStyleSheet(f"""
        QDockWidget {{ border: none; background: transparent; }}
        QDockWidget::title {{
            background: #111;
            color: #888;
            text-align: center;
            padding-left: 8px;
            padding-top: 2px;
            padding-bottom: 2px;
        }}
        """)
    else:
        app.setStyleSheet(f"""
        QDockWidget {{ border: none; background: transparent; }}
        QDockWidget::title {{
            background: #eee;
            color: #888;
            text-align: center;
            padding-left: 8px;
            padding-top: 2px;
            padding-bottom: 2px;
        }}
        """)

# Save and load preferences for all windows and widgets
def save_preferences(windows, cinema_widget, play_widget):
    prefs = {}
    for key, win in windows.items():
        win.request_save.emit()
        prefs[key] = win._pending_save_data  # Each window sets this attribute
    
    # Save tab widget position and size
    pos = cinema_widget.pos()
    size = cinema_widget.size()
    prefs["cinema_widget"] = {
        "x": pos.x(),
        "y": pos.y(),
        "width": size.width(),
        "height": size.height()
    }
    
    # Save text widget position and size
    pos = play_widget.pos()
    size = play_widget.size()
    prefs["play_widget"] = {
        "x": pos.x(),
        "y": pos.y(),
        "width": size.width(),
        "height": size.height()
    }
    
    with open(PREFS_PATH, "w") as f:
        json.dump(prefs, f)

# Load preferences for all windows and widgets
def load_preferences(windows, cinema_widget, play_widget):
    if os.path.exists(PREFS_PATH):
        with open(PREFS_PATH, "r") as f:
            prefs = json.load(f)
        for key, win in windows.items():
            win.request_load.emit(prefs.get(key, {}))
        
        # Load tab widget position and size
        tab_prefs = prefs.get("cinema_widget", {})
        if "x" in tab_prefs and "y" in tab_prefs:
            cinema_widget.move(tab_prefs["x"], tab_prefs["y"])
        if "width" in tab_prefs and "height" in tab_prefs:
            cinema_widget.resize(tab_prefs["width"], tab_prefs["height"])
            
        # Load text widget position and size
        text_prefs = prefs.get("play_widget", {})
        if "x" in text_prefs and "y" in text_prefs:
            play_widget.move(text_prefs["x"], text_prefs["y"])
        if "width" in text_prefs and "height" in text_prefs:
            play_widget.resize(text_prefs["width"], text_prefs["height"])
    else:
        for win in windows.values():
            win.request_load.emit({})

# Load tooltip text from a file
def load_tooltip_text(filename):
    """Load tooltip text from preferences file"""
    tooltip_path = os.path.join(os.path.dirname(__file__), "preferences", filename)
    
    try:
        with open(tooltip_path, "r", encoding="utf-8") as f:
            return f.read().strip()
    except:
        return ""  # Just return empty string if file doesn't exist

# Global key filter to handle key events across all windows
class GlobalKeyFilter(QObject):
    def __init__(self, windows):
        super().__init__()
        self.windows = windows  # This is now a dict

    def eventFilter(self, obj, event):
        if event.type() == QEvent.KeyPress:
            focus_widget = QApplication.focusWidget()
            
            # Keep all other key handling for shortcuts (A, O, B, N, etc.)
            if not isinstance(focus_widget, (QLineEdit, QTextEdit)):
                self.windows["nickelodeon"].handle_global_key(event)  # Changed from "player"
                self.windows["captions"].keyPressEvent(event)
                return True
        return False

# Ensure the main function is called when the script is run
if __name__ == "__main__":
    main()
