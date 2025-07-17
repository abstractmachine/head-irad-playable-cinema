import sys
import json
import os

# application
from PyQt5.QtWidgets import QLineEdit, QApplication

# shortcuts
from PyQt5.QtCore import QObject, QEvent

# our windows
from player import PlayerWindow
from detector import DetectorWindow

PREFS_PATH = "preferences.json"

def load_positions(windows):
    if os.path.exists(PREFS_PATH):
        with open(PREFS_PATH, "r") as f:
            prefs = json.load(f)
        for key, win in windows.items():
            if key in prefs:
                pos = prefs[key]
                win.move(pos["x"], pos["y"])
                if "width" in pos and "height" in pos:
                    win.resize(pos["width"], pos["height"])

def save_preferences(windows):
    prefs = {}
    if os.path.exists(PREFS_PATH):
        with open(PREFS_PATH, "r") as f:
            prefs = json.load(f)
    # Update positions and sizes for each window
    for key, win in windows.items():
        pos = win.pos()
        size = win.size()
        prefs[key] = {
            "x": pos.x(),
            "y": pos.y(),
            "width": size.width(),
            "height": size.height()
        }
        # Save seek field values for PlayerWindow
        if key == "player":
            prefs[key]["normal_seek"] = win.normal_seek.text()
            prefs[key]["fast_seek"] = win.fast_seek.text()
    with open(PREFS_PATH, "w") as f:
        json.dump(prefs, f)

class GlobalKeyFilter(QObject):
    def __init__(self, windows):
        super().__init__()
        self.windows = windows

    def eventFilter(self, obj, event):
        if event.type() == QEvent.KeyPress:
            focus_widget = QApplication.focusWidget()
            # Only handle global shortcuts if focus is NOT on a QLineEdit
            if not isinstance(focus_widget, QLineEdit):
                for win in self.windows:
                    win.handle_global_key(event)
                return True  # Event handled
            # If focus is on a QLineEdit, let Qt handle the event
            return False
        return False

def main():
    app = QApplication(sys.argv)
    player_window = PlayerWindow()
    detector_window = DetectorWindow()
    windows = {"player": player_window, "detector": detector_window}

    load_positions(windows)

    # Connect signal so detector gets notified when a video is loaded
    player_window.video_loaded.connect(detector_window.process_video)

    key_filter = GlobalKeyFilter(list(windows.values()))
    app.installEventFilter(key_filter)
    detector_window.show()
    player_window.show()

    # Save positions on exit
    app.aboutToQuit.connect(lambda: save_preferences(windows))

    sys.exit(app.exec_())

if __name__ == "__main__":
    main()