DEBUG = False  # Set to True to enable debug output

# System
import sys
import json
import os
import hashlib
# QT
from PyQt5.QtCore import QObject, QEvent
from PyQt5.QtCore import Qt
# Widgets
from PyQt5.QtWidgets import QApplication, QLineEdit, QTextEdit, QWidget
from PyQt5.QtWidgets import QTabWidget
from PyQt5.QtWidgets import QMainWindow, QDockWidget, QWidget
from PyQt5.QtWidgets import QHBoxLayout

# our UI class
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

# TODO: Move these to the system default preferences path
PREFS_PATH = "./preferences/preferences.json"
DOCK_LAYOUT_FOLDER = "./preferences/layouts/"

class PlayableCinemaMainWindow(QMainWindow):
    def __init__(self, windows):
        super().__init__()
        if DEBUG: print("DEBUG: PlayableCinemaMainWindow __init__ start")
        self.setWindowTitle("Playable Cinema")
        self.setDockOptions(QMainWindow.AllowNestedDocks | QMainWindow.AllowTabbedDocks)
        self.resize(1500, 768)

        central = QWidget()
        self.setCentralWidget(central)

        # Add each analysis widget as a separate dock
        if DEBUG: print("DEBUG: Adding dock: Inference")
        dock_inference = self.add_dock("Inference", windows["inference"], Qt.LeftDockWidgetArea)
        if DEBUG: print("DEBUG: Adding dock: Prompts")
        dock_prompts = self.add_dock("Prompts", windows["prompt"], Qt.LeftDockWidgetArea)
        if DEBUG: print("DEBUG: Adding dock: Captions")
        dock_captions = self.add_dock("Captions", windows["captions"], Qt.LeftDockWidgetArea)

        # Tabify them so they start on the same row, but can be undocked/moved
        if DEBUG: print("DEBUG: Tabifying Inference <-> Captions")
        self.tabifyDockWidget(dock_inference, dock_captions)
        if DEBUG: print("DEBUG: Tabifying Inference <-> Prompts")
        self.tabifyDockWidget(dock_inference, dock_prompts)

        # Raise the Captions dock so it's visible by default
        if DEBUG: print("DEBUG: Raising Captions dock")
        dock_captions.raise_()

        # Add other docks as before
        if DEBUG: print("DEBUG: Adding dock: Subtitles")
        self.add_dock("Subtitles", windows["subtitles"], Qt.RightDockWidgetArea)
        if DEBUG: print("DEBUG: Adding dock: Playhouse")
        self.add_dock("Playhouse", windows["playhouse"], Qt.TopDockWidgetArea)
        if DEBUG: print("DEBUG: Adding dock: Nickelodeon")
        self.add_dock("Nickelodeon", windows["nickelodeon"], Qt.TopDockWidgetArea)
        if DEBUG: print("DEBUG: Adding dock: Shotlist")
        self.add_dock("Shotlist", windows["shotlist"], Qt.BottomDockWidgetArea)
        if DEBUG: print("DEBUG: Adding dock: Cinemathèque")
        self.add_dock("Cinemathèque", windows["cinematheque"], Qt.BottomDockWidgetArea)
        if DEBUG: print("DEBUG: PlayableCinemaMainWindow __init__ end")

        central.setFocusPolicy(Qt.ClickFocus)

    def add_dock(self, name, widget, area):
        if DEBUG: print(f"DEBUG: Creating QDockWidget: {name} ({widget})")
        dock = QDockWidget(name, self)
        dock.setWidget(widget)
        dock.setObjectName(name)
        dock.setFloating(False)
        dock.setAllowedAreas(Qt.AllDockWidgetAreas)
        dock.setFeatures(QDockWidget.DockWidgetMovable | QDockWidget.DockWidgetFloatable)
        self.addDockWidget(area, dock)
        if DEBUG: print(f"DEBUG: Added QDockWidget: {name} to area {area}")
        return dock  # Return the dock so it can be tabified

def main():
    # Fusion style for Qt
    QApplication.setStyle("Fusion")

    # Initialize the application
    app = QApplication(sys.argv)

    # Create UI instance with all fonts loaded
    ui = UI()

    # Set the visual style for the application
    set_visual_style(app, ui)

    # Several window are going to need to access subtitles, so we create it first
    subtitles_window = SubtitlesWindow(ui)

    # create a dictionary of our windows, passing ui to each
    windows = {
        "inference": InferenceWindow(ui),
        "captions": CaptionWindow(ui, subtitles_window),
        "subtitles": subtitles_window,
        "prompt": PromptWindow(ui, subtitles_window),
        "playhouse": PlayhouseWindow(ui),
        "nickelodeon": NickelodeonWindow(ui),
        "shotlist": ShotlistWindow(ui),
        "cinematheque": CinemathequeWindow(ui),
    }

    # Connect signals for preferences
    for win in windows.values():
        win.request_save.connect(win.on_request_save)
        win.request_load.connect(win.on_request_load)

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
    windows["captions"].request_previous_shot.connect(windows["shotlist"].jump_to_previous_shot)
    windows["captions"].shot_bot_finished.connect(windows["cinematheque"].shot_bot_finished)

    main_window = PlayableCinemaMainWindow(windows)
    main_window.show()

    # Install global key filter
    key_filter = GlobalKeyFilter(windows, main_window)
    app.installEventFilter(key_filter)

    # Restore window geometry and then restore its internal dock layout
    load_window_geometry(main_window)
    load_dock_layout(main_window)

    # refresh screen to ensure all docks are visible
    main_window.update()

    # Load preferences at startup (after main_window is created)
    load_preferences(windows, main_window)

    # Save preferences on exit
    app.aboutToQuit.connect(lambda: save_preferences(windows, main_window))

    # Save dock layout on exit
    app.aboutToQuit.connect(lambda: save_dock_layout(main_window))
    app.aboutToQuit.connect(lambda: save_window_geometry(main_window))

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
def set_visual_style(app, ui):
    """Set the visual style for the application"""
    app.setStyleSheet(ui.get_style_sheet("app"))

# Save and load preferences for all windows and widgets
def save_preferences(windows, main_window):
    prefs = {}
    for key, win in windows.items():
        win.request_save.emit()
        prefs[key] = win._pending_save_data  # Each window sets this attribute

    # Remove dock layout and window geometry saving
    # (No longer saving "dock_layout" or "window_geometry" here)

    with open(PREFS_PATH, "w") as f:
        json.dump(prefs, f)

def load_preferences(windows, main_window):
    if os.path.exists(PREFS_PATH):
        with open(PREFS_PATH, "r") as f:
            prefs = json.load(f)
        for key, win in windows.items():
            win.request_load.emit(prefs.get(key, {}))
    else:
        for win in windows.values():
            win.request_load.emit({})

def report_window_and_dock_geometry(main_window, label=""):
    print(f"DEBUG: --- Window & Dock Geometry Report {label} ---")
    geo = main_window.geometry()
    print(f"DEBUG: MainWindow geometry: (x={geo.x()}, y={geo.y()}, w={geo.width()}, h={geo.height()})")
    for dock in main_window.findChildren(QDockWidget):
        dock_geo = dock.geometry()
        print(
            f"DEBUG: Dock '{dock.objectName()}' | visible={dock.isVisible()} | "
            f"floating={dock.isFloating()} | area={main_window.dockWidgetArea(dock)} | "
            f"geometry=(x={dock_geo.x()}, y={dock_geo.y()}, w={dock_geo.width()}, h={dock_geo.height()})"
        )
    print(f"DEBUG: --- End Window & Dock Geometry Report {label} ---")

# Save the current window geometry to a binary file in the layouts folder.
def save_window_geometry(main_window, filename="previous.geometry"):
    if DEBUG: print("DEBUG: save_window_geometry called")
    if DEBUG: report_window_and_dock_geometry(main_window, label="BEFORE SAVE")
    if not os.path.exists(DOCK_LAYOUT_FOLDER):
        os.makedirs(DOCK_LAYOUT_FOLDER)
    geometry_file = os.path.join(DOCK_LAYOUT_FOLDER, filename)
    geometry = main_window.saveGeometry()
    with open(geometry_file, "wb") as f:
        f.write(geometry.data())
    if DEBUG: print(f"DEBUG: Window geometry saved to {geometry_file}")
    if DEBUG: report_window_and_dock_geometry(main_window, label="AFTER SAVE")

# Save the current dock layout to a binary file in the layouts folder.
def save_dock_layout(main_window, filename="previous.layout"):
    """Save the current dock layout to a binary file in the layouts folder."""
    if DEBUG: print("DEBUG: save_dock_layout called")
    if DEBUG: report_window_and_dock_geometry(main_window, label="BEFORE SAVE")
    if not os.path.exists(DOCK_LAYOUT_FOLDER):
        os.makedirs(DOCK_LAYOUT_FOLDER)
    dock_layout_file = os.path.join(DOCK_LAYOUT_FOLDER, filename)
    if os.path.exists(dock_layout_file):
        if DEBUG: print(f"DEBUG: Removing old layout file: {dock_layout_file}")
        os.remove(dock_layout_file)
    state = main_window.saveState()
    with open(dock_layout_file, "wb") as f:
        f.write(state.data())
    if DEBUG: print(f"DEBUG: Dock layout saved to {dock_layout_file}")
    if DEBUG: report_window_and_dock_geometry(main_window, label="AFTER SAVE")

# Restore the window geometry from a binary file in the layouts folder, if it exists.
def load_window_geometry(main_window, filename="previous.geometry"):
    geometry_file = os.path.join(DOCK_LAYOUT_FOLDER, filename)
    if os.path.exists(geometry_file):
        if DEBUG: print(f"DEBUG: Loading window geometry from {geometry_file}")
        with open(geometry_file, "rb") as f:
            geometry = f.read()
            ok = main_window.restoreGeometry(geometry)
            if DEBUG: print(f"DEBUG: restoreGeometry returned {ok}")
    else:
        if DEBUG: print("DEBUG: No window geometry file found to load.")

# Load the dock layout from a binary file in the layouts folder, if it exists.
def check_all_docks_hidden(main_window):
    """Check if all QDockWidgets in the main window are hidden or not visible.
    If so, unhide them all and print a warning."""
    docks = [w for w in main_window.findChildren(QDockWidget)]
    for dock in docks:
        if not dock.isVisible():
            if DEBUG: print(f"DEBUG: **** Dock '{dock.objectName()}' is not visible, showing it.")
            dock.show()

    if DEBUG: print("DEBUG: --- Dock Visibility Report ---")
    for dock in docks:
        if DEBUG: print(
            f"DEBUG: Dock '{dock.objectName()}' | visible={dock.isVisible()} | "
            f"floating={dock.isFloating()} | area={main_window.dockWidgetArea(dock)} | "
            f"geometry={dock.geometry().getRect() if hasattr(dock.geometry(), 'getRect') else dock.geometry()}"
        )
    hidden_docks = [dock for dock in docks if not dock.isVisible()]
    if DEBUG: print(f"DEBUG: {len(hidden_docks)}/{len(docks)} docks hidden after restoreState.")
    if len(docks) > 0 and len(hidden_docks) == len(docks):
        if DEBUG: print("WARNING: All docks are hidden after restoreState! Forcing all docks visible.")
        for dock in docks:
            if DEBUG: print(f"DEBUG: Forcing dock '{dock.objectName()}' visible.")
            dock.show()
    if DEBUG: print("DEBUG: --- End Dock Visibility Report ---")

def load_dock_layout(main_window, filename="previous.layout"):
    """Load the dock layout from a binary file in the layouts folder, if it exists."""
    dock_layout_file = os.path.join(DOCK_LAYOUT_FOLDER, filename)
    if os.path.exists(dock_layout_file):
        size = os.path.getsize(dock_layout_file)
        with open(dock_layout_file, "rb") as f:
            state = f.read()
            h = hashlib.sha256(state).hexdigest()
            if DEBUG: print(f"DEBUG: Layout file size: {size}, sha256: {h}")
            ok = main_window.restoreState(state)
            if DEBUG: print(f"DEBUG: restoreState returned {ok}")
            check_all_docks_hidden(main_window)  # <-- Add this line
            if not ok:
                print("WARNING: Failed to restore dock layout. Removing layout file.")
                os.remove(dock_layout_file)
    else:
        if DEBUG: print("DEBUG: No dock layout file found to load.")

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
    def __init__(self, windows, main_window):
        super().__init__()
        self.windows = windows  # This is now a dict
        self.main_window = main_window

    def eventFilter(self, obj, event):
        if event.type() == QEvent.KeyPress:
            # Avoid handling global shortcuts if focus is in a text field
            widget = QApplication.focusWidget()
            from PyQt5.QtWidgets import QLineEdit, QTextEdit, QPlainTextEdit
            if isinstance(widget, (QLineEdit, QTextEdit, QPlainTextEdit)):
                return False  # Let the text field handle the key event

            cine = self.windows["cinematheque"]
            if event.key() == Qt.Key_PageDown:
                if DEBUG: print("DEBUG: PageDown pressed (Cinematheque navigation)")
                cine.select_next_movie()
                return True
            elif event.key() == Qt.Key_PageUp:
                if DEBUG: print("DEBUG: PageUp pressed (Cinematheque navigation)")
                cine.select_previous_movie()
                return True
            elif event.key() == Qt.Key_W:
                if DEBUG: print("DEBUG: W pressed (resetting dock layout)")
                reset_dock_layout(self.main_window)
                return True

            # Keep all other key handling for shortcuts (A, O, B, N, etc.)
            self.windows["nickelodeon"].handle_global_key(event)
            self.windows["captions"].keyPressEvent(event)
            return True
        return False

def reset_dock_layout(main_window):
    """Reset the dock layout to its default state."""
    # Remove saved layout and geometry files
    layout_file = os.path.join(DOCK_LAYOUT_FOLDER, "previous.layout")
    geometry_file = os.path.join(DOCK_LAYOUT_FOLDER, "previous.geometry")
    if os.path.exists(layout_file):
        os.remove(layout_file)
    if os.path.exists(geometry_file):
        os.remove(geometry_file)
    # Restart the app (do NOT call main_window.close())
    os.execl(sys.executable, sys.executable, *sys.argv)

# Ensure the main function is called when the script is run
if __name__ == "__main__":
    main()
