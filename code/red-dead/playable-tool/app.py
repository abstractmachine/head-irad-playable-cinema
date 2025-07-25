import sys
import json
import os
from PyQt5.QtWidgets import QApplication, QLineEdit, QTextEdit
from PyQt5.QtWidgets import QTabWidget

from PyQt5.QtCore import QObject, QEvent
from PyQt5.QtCore import Qt

# our other windows
from nickelodeon import NickelodeonWindow
from shotlist import ShotlistWindow
from annotate import AnnotateWindow
from cinema import CinemaWindow
from prompt import PromptWindow
from default import DefaultWindow
from subtitles import SubtitlesWindow

PREFS_PATH = "./preferences/preferences.json"

def main():
    app = QApplication(sys.argv)

    # create a dictionary of our windows
    windows = {
        "nickelodeon": NickelodeonWindow(),
        "shotlist": ShotlistWindow(),
        "annotate": AnnotateWindow(),
        "cinema": CinemaWindow(),
        "default": DefaultWindow(),
        "prompt": PromptWindow(),
        "subtitles": SubtitlesWindow()
    }

    # Install global key filter
    key_filter = GlobalKeyFilter(windows)  # Pass the dict, not a list
    app.installEventFilter(key_filter)

    # Connect signals for preferences
    for win in windows.values():
        win.request_save.connect(win.on_request_save)
        win.request_load.connect(win.on_request_load)

    # Create main tab widget for cinema and shotlist
    tab_widget = QTabWidget()
    tab_widget.addTab(windows["cinema"], "Cinemathèque")
    tab_widget.addTab(windows["shotlist"], "Shotlist")
    tab_widget.show()

    # Create text tab widget for annotate, default, prompt, and subtitles
    text_widget = QTabWidget()
    text_widget.addTab(windows["annotate"], "Annotator")
    
    # Add default tab and set its tooltip
    default_tab_index = text_widget.addTab(windows["default"], "Default")
    default_tooltip = load_tooltip_text("prompt-tooltip.txt")
    text_widget.setTabToolTip(default_tab_index, default_tooltip)
    
    # Add prompt tab and set its tooltip
    prompt_tab_index = text_widget.addTab(windows["prompt"], "Shot Prompt")
    prompt_tooltip = load_tooltip_text("prompt-tooltip.txt")
    text_widget.setTabToolTip(prompt_tab_index, prompt_tooltip)
    
    text_widget.addTab(windows["subtitles"], "Subtitles")
    text_widget.show()

    # Signal Connections
    windows["nickelodeon"].video_loaded.connect(windows["shotlist"].process_video)  # Changed from "player"
    windows["nickelodeon"].video_timecode_changed.connect(windows["shotlist"].clear_table_selection)
    windows["nickelodeon"].video_timecode_changed.connect(windows["shotlist"].set_current_time)
    windows["nickelodeon"].frames_extracted.connect(windows["annotate"].handle_api_frames)
    windows["nickelodeon"].video_loaded.connect(windows["cinema"].on_movie_loading_complete)
    windows["nickelodeon"].video_loaded.connect(windows["prompt"].on_movie_loaded)
    windows["cinema"].movie_selected.connect(windows["nickelodeon"].load_video_from_path)
    windows["cinema"].project_loaded.connect(windows["prompt"].set_project_folder)
    windows["cinema"].project_loaded.connect(windows["shotlist"].set_project_folder)

    # Simple connections for annotate window (same pattern as prompt)
    windows["nickelodeon"].video_loaded.connect(windows["annotate"].on_movie_loaded)
    windows["cinema"].project_loaded.connect(windows["annotate"].set_project_folder)
    
    # Simple connections for subtitles window (same pattern as prompt)
    windows["nickelodeon"].video_loaded.connect(windows["subtitles"].on_movie_loaded)
    windows["cinema"].project_loaded.connect(windows["subtitles"].set_project_folder)
    windows["nickelodeon"].video_timecode_changed.connect(windows["subtitles"].on_timecode_changed)

    windows["shotlist"].jump_to_timecode_signal.connect(windows["nickelodeon"].jump_to_timecode)
    windows["shotlist"].shotlist_status.connect(windows["annotate"].set_shotlist_status)
    windows["shotlist"].caption_selected.connect(windows["annotate"].set_caption_field)
    windows["shotlist"].abort_api.connect(windows["annotate"].handle_api_abort)
    windows["shotlist"].shot_timecodes.connect(windows["nickelodeon"].handle_shot_timecodes)
    windows["shotlist"].is_last_available_shot.connect(windows["annotate"].handle_is_last_available_shot)
    windows["annotate"].caption_submitted.connect(windows["shotlist"].update_caption_for_current_shot)
    windows["annotate"].request_current_shot.connect(windows["shotlist"].handle_request_current_shot)
    windows["annotate"].request_next_shot.connect(windows["shotlist"].jump_to_next_shot)

    # Load preferences at startup (now includes both tab widgets) - MOVE THIS AFTER CONNECTIONS
    load_preferences(windows, tab_widget, text_widget)

    # Save preferences on exit (now includes both tab widgets)
    app.aboutToQuit.connect(lambda: save_preferences(windows, tab_widget, text_widget))

    # Show the app windows
    windows["nickelodeon"].show()  # Changed from "player"

    def clean_quit():
        try:
            windows["nickelodeon"].player.terminate()  # Updated for MPV - Changed from "player"
        except Exception:
            pass
        for window in windows.values():
            window.close()

    app.aboutToQuit.connect(clean_quit)
    sys.exit(app.exec_())

def save_preferences(windows, tab_widget, text_widget):
    prefs = {}
    for key, win in windows.items():
        win.request_save.emit()
        prefs[key] = win._pending_save_data  # Each window sets this attribute
    
    # Save tab widget position and size
    pos = tab_widget.pos()
    size = tab_widget.size()
    prefs["tab_widget"] = {
        "x": pos.x(),
        "y": pos.y(),
        "width": size.width(),
        "height": size.height()
    }
    
    # Save text widget position and size
    pos = text_widget.pos()
    size = text_widget.size()
    prefs["text_widget"] = {
        "x": pos.x(),
        "y": pos.y(),
        "width": size.width(),
        "height": size.height()
    }
    
    with open(PREFS_PATH, "w") as f:
        json.dump(prefs, f)

def load_preferences(windows, tab_widget, text_widget):
    if os.path.exists(PREFS_PATH):
        with open(PREFS_PATH, "r") as f:
            prefs = json.load(f)
        for key, win in windows.items():
            win.request_load.emit(prefs.get(key, {}))
        
        # Load tab widget position and size
        tab_prefs = prefs.get("tab_widget", {})
        if "x" in tab_prefs and "y" in tab_prefs:
            tab_widget.move(tab_prefs["x"], tab_prefs["y"])
        if "width" in tab_prefs and "height" in tab_prefs:
            tab_widget.resize(tab_prefs["width"], tab_prefs["height"])
            
        # Load text widget position and size
        text_prefs = prefs.get("text_widget", {})
        if "x" in text_prefs and "y" in text_prefs:
            text_widget.move(text_prefs["x"], text_prefs["y"])
        if "width" in text_prefs and "height" in text_prefs:
            text_widget.resize(text_prefs["width"], text_prefs["height"])
    else:
        for win in windows.values():
            win.request_load.emit({})

def load_tooltip_text(filename):
    """Load tooltip text from preferences file"""
    tooltip_path = os.path.join(os.path.dirname(__file__), "preferences", filename)
    
    try:
        with open(tooltip_path, "r", encoding="utf-8") as f:
            return f.read().strip()
    except:
        return ""  # Just return empty string if file doesn't exist

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
                self.windows["annotate"].keyPressEvent(event)
                return True
        return False

if __name__ == "__main__":
    main()
