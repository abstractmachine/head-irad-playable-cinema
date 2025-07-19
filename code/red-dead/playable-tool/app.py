import sys
import json
import os
from PyQt5.QtWidgets import QApplication, QLineEdit, QTextEdit
from PyQt5.QtCore import QObject, QEvent

# our other wi
from player import PlayerWindow
from detector import DetectorWindow
from annotate import AnnotateWindow

PREFS_PATH = "preferences.json"

class GlobalKeyFilter(QObject):
    def __init__(self, windows):
        super().__init__()
        self.windows = windows  # Should be [player_window]

    def eventFilter(self, obj, event):
        if event.type() == QEvent.KeyPress:
            focus_widget = QApplication.focusWidget()
            # Only handle global shortcuts if focus is NOT on a QLineEdit or QTextEdit
            if not isinstance(focus_widget, (QLineEdit, QTextEdit)):
                # Send key event to player window
                self.windows[0].handle_global_key(event)
                return True  # Event handled
        return False

def save_preferences(windows):
    prefs = {}
    for key, win in windows.items():
        win.request_save.emit()
        prefs[key] = win._pending_save_data  # Each window sets this attribute
    with open(PREFS_PATH, "w") as f:
        json.dump(prefs, f)

def load_preferences(windows):
    if os.path.exists(PREFS_PATH):
        with open(PREFS_PATH, "r") as f:
            prefs = json.load(f)
        for key, win in windows.items():
            win.request_load.emit(prefs.get(key, {}))
    else:
        for win in windows.values():
            win.request_load.emit({})

def main():
    app = QApplication(sys.argv)
    player_window = PlayerWindow()
    detector_window = DetectorWindow()
    annotate_window = AnnotateWindow(player_window, detector_window)
    windows = {
        "player": player_window,
        "detector": detector_window,
        "annotate": annotate_window
    }

    # Install global key filter
    key_filter = GlobalKeyFilter([windows["player"]])
    app.installEventFilter(key_filter)

    # Connect signals for preferences
    for win in windows.values():
        win.request_save.connect(win.on_request_save)
        win.request_load.connect(win.on_request_load)

    # Load preferences at startup
    load_preferences(windows)

    # Save preferences on exit
    app.aboutToQuit.connect(lambda: save_preferences(windows))

    # Signal Connections
    windows["player"].video_loaded.connect(windows["detector"].process_video)
    windows["detector"].jump_to_timecode_signal.connect(windows["player"].jump_to_timecode)
    windows["player"].video_timecode_changed.connect(windows["detector"].clear_table_selection)

    # Show the app windows
    windows["detector"].show()
    windows["player"].show()
    windows["annotate"].show()

    def clean_quit():
        try:
            windows["player"].media_player.stop()
        except Exception:
            pass
        windows["player"].close()
        windows["detector"].close()
        windows["annotate"].close()

    app.aboutToQuit.connect(clean_quit)
    sys.exit(app.exec_())

if __name__ == "__main__":
    main()