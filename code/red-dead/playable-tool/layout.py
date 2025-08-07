"""
Window layout management for Playable Cinema.
Handles dock widgets, window geometry, layout persistence, and debugging.
"""

import os
import json
import hashlib
import sys
from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import QMainWindow, QDockWidget, QWidget

DEBUG = False  # Set to True to enable debug output

# Layout preferences paths
PREFS_PATH = "./preferences/preferences.json"
DOCK_LAYOUT_FOLDER = "./preferences/layouts/"

class NativeDockWidget(QDockWidget):
    """
    Custom dock widget that prevents closing and handles floating behavior properly.
    When user tries to close a floating dock, it re-docks instead of disappearing.
    """
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.topLevelChanged.connect(self.on_top_level_changed)

    def on_top_level_changed(self, floating):
        """Handle when dock becomes floating or docked"""
        if floating:
            # Make it a proper window when floating
            self.setWindowFlags(Qt.Window)
            self.show()
        else:
            # Make it a widget when docked
            self.setWindowFlags(Qt.Widget)
            self.show()

    def closeEvent(self, event):
        """Prevent closing - re-dock instead"""
        event.ignore()
        self.setFloating(False)
        self.raise_()

class PlayableCinemaMainWindow(QMainWindow):
    """
    Main application window that contains all the dock widgets.
    Uses a tabbed dock layout with logical groupings of related functionality.
    """
    def __init__(self, windows):
        super().__init__()
        if DEBUG: print("DEBUG: PlayableCinemaMainWindow __init__ start")
        
        # Basic window setup
        self.setWindowTitle("Playable Cinema")
        self.setDockOptions(QMainWindow.AllowNestedDocks | QMainWindow.AllowTabbedDocks)
        self.resize(1500, 768)

        # Create central widget (required by Qt but not used for content)
        central = QWidget()
        self.setCentralWidget(central)
        self.setWindowFlags(Qt.Window)

        # === LEFT SIDE: Analysis and Control Panels (Tabbed) ===
        # These are the primary work areas that users switch between frequently
        if DEBUG: print("DEBUG: Setting up left-side tabbed panels")
        dock_project = self.add_dock("Project", windows["project"], Qt.LeftDockWidgetArea)
        dock_inference = self.add_dock("Inference", windows["inference"], Qt.LeftDockWidgetArea)
        dock_prompts = self.add_dock("Prompts", windows["prompt"], Qt.LeftDockWidgetArea)
        dock_captions = self.add_dock("Captions", windows["captions"], Qt.LeftDockWidgetArea)

        # Tab them together for space efficiency
        if DEBUG: print("DEBUG: Tabifying left-side panels")
        self.tabifyDockWidget(dock_project, dock_inference)
        self.tabifyDockWidget(dock_project, dock_captions)
        self.tabifyDockWidget(dock_project, dock_prompts)

        # Project should be the default visible tab
        dock_project.raise_()

        # === OTHER AREAS: Supporting panels ===
        if DEBUG: print("DEBUG: Setting up supporting panels")
        # Right: Text content (subtitles)
        self.add_dock("Subtitles", windows["subtitles"], Qt.RightDockWidgetArea)
        
        # Top: Video players
        self.add_dock("Playhouse", windows["playhouse"], Qt.TopDockWidgetArea)
        self.add_dock("Nickelodeon", windows["nickelodeon"], Qt.TopDockWidgetArea)
        
        # Bottom: Content libraries and timelines
        self.add_dock("Shotlist", windows["shotlist"], Qt.BottomDockWidgetArea)
        self.add_dock("Cinemathèque", windows["cinematheque"], Qt.BottomDockWidgetArea)
        self.add_dock("Playbill", windows["playbill"], Qt.BottomDockWidgetArea)

        central.setFocusPolicy(Qt.ClickFocus)

    def add_dock(self, name, widget, area):
        """Create and configure a dock widget with consistent settings"""
        dock = NativeDockWidget(name, self)
        dock.setWidget(widget)
        dock.setObjectName(name)
        dock.setFloating(False)
        dock.setAllowedAreas(Qt.AllDockWidgetAreas)
        dock.setFeatures(QDockWidget.DockWidgetMovable | QDockWidget.DockWidgetFloatable)
        self.addDockWidget(area, dock)
        return dock

# ================================================================
# PREFERENCES MANAGEMENT
# ================================================================

def save_preferences(windows, main_window):
    """Save window-specific preferences to JSON file"""
    prefs = {}
    for key, win in windows.items():
        win.preferences_save.emit()
        prefs[key] = win._pending_save_data

    # Ensure preferences directory exists
    os.makedirs(os.path.dirname(PREFS_PATH), exist_ok=True)
    
    with open(PREFS_PATH, "w") as f:
        json.dump(prefs, f, indent=2)

def load_preferences(windows, main_window):
    """Load window-specific preferences from JSON file"""
    if os.path.exists(PREFS_PATH):
        with open(PREFS_PATH, "r") as f:
            prefs = json.load(f)
        for key, win in windows.items():
            win.preferences_load.emit(prefs.get(key, {}))
    else:
        # Send empty dict if no preferences file exists
        for win in windows.values():
            win.preferences_load.emit({})

# ================================================================
# WINDOW LAYOUT PERSISTENCE
# ================================================================

def save_window_geometry(main_window, filename="previous.geometry"):
    """Save main window size and position to binary file"""
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

def save_dock_layout(main_window, filename="previous.layout"):
    """Save dock arrangement and visibility to binary file"""
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

def load_window_geometry(main_window, filename="previous.geometry"):
    """Restore main window size and position from binary file"""
    geometry_file = os.path.join(DOCK_LAYOUT_FOLDER, filename)
    if os.path.exists(geometry_file):
        if DEBUG: print(f"DEBUG: Loading window geometry from {geometry_file}")
        with open(geometry_file, "rb") as f:
            geometry = f.read()
            ok = main_window.restoreGeometry(geometry)
            if DEBUG: print(f"DEBUG: restoreGeometry returned {ok}")
    else:
        if DEBUG: print("DEBUG: No window geometry file found to load.")

def load_dock_layout(main_window, filename="previous.layout"):
    """Restore dock arrangement and visibility from binary file"""
    dock_layout_file = os.path.join(DOCK_LAYOUT_FOLDER, filename)
    if os.path.exists(dock_layout_file):
        size = os.path.getsize(dock_layout_file)
        with open(dock_layout_file, "rb") as f:
            state = f.read()
            h = hashlib.sha256(state).hexdigest()
            if DEBUG: print(f"DEBUG: Layout file size: {size}, sha256: {h}")
            ok = main_window.restoreState(state)
            if DEBUG: print(f"DEBUG: restoreState returned {ok}")
            check_all_docks_hidden(main_window)
            if not ok:
                print("WARNING: Failed to restore dock layout. Removing layout file.")
                os.remove(dock_layout_file)
    else:
        if DEBUG: print("DEBUG: No dock layout file found to load.")

# ================================================================
# DEBUGGING AND DIAGNOSTICS
# ================================================================

def report_window_and_dock_geometry(main_window, label=""):
    """Debug function to print detailed window and dock information"""
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

def check_all_docks_hidden(main_window):
    """
    Check if all docks are hidden after layout restoration.
    This can happen when restoring corrupted layout files.
    Forces all docks visible if they're all hidden.
    """
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
    
    # If ALL docks are hidden, force them visible (corrupted layout file)
    if len(docks) > 0 and len(hidden_docks) == len(docks):
        if DEBUG: print("WARNING: All docks are hidden after restoreState! Forcing all docks visible.")
        for dock in docks:
            if DEBUG: print(f"DEBUG: Forcing dock '{dock.objectName()}' visible.")
            dock.show()
    if DEBUG: print("DEBUG: --- End Dock Visibility Report ---")

def reset_dock_layout(main_window):
    """
    Reset dock layout to default by removing saved files and restarting.
    Used for recovery when layout becomes corrupted.
    """
    layout_file = os.path.join(DOCK_LAYOUT_FOLDER, "previous.layout")
    geometry_file = os.path.join(DOCK_LAYOUT_FOLDER, "previous.geometry")
    
    if os.path.exists(layout_file):
        os.remove(layout_file)
    if os.path.exists(geometry_file):
        os.remove(geometry_file)
    
    # Restart the application to get default layout
    os.execl(sys.executable, sys.executable, *sys.argv)