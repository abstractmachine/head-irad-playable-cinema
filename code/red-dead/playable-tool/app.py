import sys
import json
import os
from PyQt5.QtWidgets import QApplication, QLineEdit, QTextEdit
from PyQt5.QtCore import QObject, QEvent
from PyQt5.QtCore import Qt

# our other windows
from player import PlayerWindow
from detector import DetectorWindow
from annotate import AnnotateWindow

PREFS_PATH = "preferences/preferences.json"

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
    key_filter = GlobalKeyFilter(windows)  # Pass the dict, not a list
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
    windows["player"].video_timecode_changed.connect(windows["detector"].clear_table_selection)
    windows["player"].video_timecode_changed.connect(windows["detector"].set_current_time)
    windows["player"].frames_extracted.connect(windows["annotate"].handle_api_frames)
    windows["detector"].jump_to_timecode_signal.connect(windows["player"].jump_to_timecode)
    windows["detector"].shotlist_status.connect(windows["annotate"].set_shotlist_status)
    windows["detector"].caption_selected.connect(windows["annotate"].set_caption_field)
    windows["detector"].abort_api.connect(windows["annotate"].handle_api_abort)
    windows["detector"].shot_timecodes.connect(windows["player"].handle_shot_timecodes)
    windows["annotate"].caption_submitted.connect(windows["detector"].update_caption_for_current_shot)
    windows["annotate"].request_current_shot.connect(windows["detector"].handle_request_current_shot)

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
