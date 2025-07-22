import sys
import json
import os
from PyQt5.QtWidgets import QApplication, QLineEdit, QTextEdit
from PyQt5.QtWidgets import QTabWidget

from PyQt5.QtCore import QObject, QEvent
from PyQt5.QtCore import Qt

# our other windows
from player import PlayerWindow
from shotlist import ShotlistWindow
from annotate import AnnotateWindow
from cinema import CinemaWindow

PREFS_PATH = "./preferences/preferences.json"

def main():
    app = QApplication(sys.argv)

    # create a dictionary of our windows
    windows = {
        "player": PlayerWindow(),
        "shotlist": ShotlistWindow(),
        "annotate": AnnotateWindow(),
        "cinema": CinemaWindow()
    }

    # Install global key filter
    key_filter = GlobalKeyFilter(windows)  # Pass the dict, not a list
    app.installEventFilter(key_filter)

    # Connect signals for preferences
    for win in windows.values():
        win.request_save.connect(win.on_request_save)
        win.request_load.connect(win.on_request_load)

    # Create a main window with tabs
    tab_widget = QTabWidget()
    tab_widget.addTab(windows["cinema"], "Cinema")
    tab_widget.addTab(windows["shotlist"], "Shotlist")
    tab_widget.show()

    # Load preferences at startup (now includes tab_widget)
    load_preferences(windows, tab_widget)

    # Save preferences on exit (now includes tab_widget)
    app.aboutToQuit.connect(lambda: save_preferences(windows, tab_widget))

    # Signal Connections
    windows["player"].video_loaded.connect(windows["shotlist"].process_video)
    windows["player"].video_timecode_changed.connect(windows["shotlist"].clear_table_selection)
    windows["player"].video_timecode_changed.connect(windows["shotlist"].set_current_time)
    windows["player"].frames_extracted.connect(windows["annotate"].handle_api_frames)
    windows["shotlist"].jump_to_timecode_signal.connect(windows["player"].jump_to_timecode)
    windows["shotlist"].shotlist_status.connect(windows["annotate"].set_shotlist_status)
    windows["shotlist"].caption_selected.connect(windows["annotate"].set_caption_field)
    windows["shotlist"].abort_api.connect(windows["annotate"].handle_api_abort)
    windows["shotlist"].shot_timecodes.connect(windows["player"].handle_shot_timecodes)
    windows["annotate"].caption_submitted.connect(windows["shotlist"].update_caption_for_current_shot)
    windows["annotate"].request_current_shot.connect(windows["shotlist"].handle_request_current_shot)
    windows["annotate"].request_next_shot.connect(windows["shotlist"].jump_to_next_shot)
    windows["shotlist"].is_last_available_shot.connect(windows["annotate"].handle_is_last_available_shot)
    
    # Show the app windows
    # windows["shotlist"].show()
    windows["player"].show()
    windows["annotate"].show()

    def clean_quit():
        try:
            windows["player"].media_player.stop()
        except Exception:
            pass
        windows["player"].close()
        windows["shotlist"].close()
        windows["annotate"].close()
        windows["cinema"].close()

    app.aboutToQuit.connect(clean_quit)
    sys.exit(app.exec_())

def save_preferences(windows, tab_widget):
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
    
    with open(PREFS_PATH, "w") as f:
        json.dump(prefs, f)

def load_preferences(windows, tab_widget):
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
    else:
        for win in windows.values():
            win.request_load.emit({})

class GlobalKeyFilter(QObject):
    def __init__(self, windows):
        super().__init__()
        self.windows = windows  # This is now a dict

    def eventFilter(self, obj, event):
        if event.type() == QEvent.KeyPress:
            focus_widget = QApplication.focusWidget()
            annotate = self.windows["annotate"]
            # Check ignore_next_enter before forwarding
            if event.key() in (Qt.Key_Return, Qt.Key_Enter):
                if annotate.ignore_next_enter:
                    annotate.ignore_next_enter = False
                    return True
                if focus_widget is annotate.system_prompt_field:
                    # Let system_prompt_field handle ENTER/RETURN for newlines
                    return False
                if not isinstance(focus_widget, QLineEdit):
                    annotate.keyPressEvent(event)
                    return True
            elif not isinstance(focus_widget, (QLineEdit, QTextEdit)):
                self.windows["player"].handle_global_key(event)
                self.windows["annotate"].keyPressEvent(event)
                return True
        return False

if __name__ == "__main__":
    main()
