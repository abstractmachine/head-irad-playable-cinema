DEBUG = False  # Set to True to enable debug output
ERROR = True  # Set to True to enable error output

from PyQt5.QtCore import Qt, pyqtSignal, QTimer
from PyQt5.QtWidgets import (
    QMainWindow, QWidget, QHBoxLayout, QVBoxLayout, QLineEdit, QPushButton, QLabel, QFileDialog
)

from utility import minimum_load_interval, HIGHLIGHT_BACKGROUND_COLOR, HIGHLIGHT_COLOR
from project import ProjectManager  # <-- Import ProjectManager

class RobotsWindow(QMainWindow):
    """
    Chaos generator window that sends random events at specified intervals.
    Used for testing application robustness and simulating unpredictable user behavior.
    """
    
    # Signals
    preferences_save = pyqtSignal()
    preferences_load = pyqtSignal(dict)
    chaos = pyqtSignal()  # The chaos event signal
    
    def __init__(self, ui):
        super().__init__()
        self.ui = ui
        self.setWindowTitle("Gremlins")
        self.setMinimumSize(125, 70)

        # Add this for preferences compatibility
        self._pending_save_data = {}

        # Project management
        self.project_manager = ProjectManager(parent=self)
        self.project_manager.project_loaded.connect(self.on_project_loaded)
        self.project_manager.project_changed.connect(self.on_project_changed)

        # State
        self.is_running = False
        self.interval_seconds = 3.0
        
        # Timer for chaos events
        self.chaos_timer = QTimer()
        self.chaos_timer.timeout.connect(self.emit_chaos)
        
        self.setup_ui(ui)
        
    def setup_ui(self, ui):
        """Set up the user interface"""
        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        
        layout = QVBoxLayout()
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)

        # --- Top-left buttons layout ---
        button_width, button_height = ui.get_dimensions("button")
        top_buttons_layout = QHBoxLayout()
        top_buttons_layout.setContentsMargins(0, 0, 0, 0)
        top_buttons_layout.setSpacing(2)

        self.select_button = QPushButton("Project Folder")
        self.select_button.setFont(self.ui.get_font('button'))
        self.select_button.clicked.connect(self.select_project_folder)
        self.select_button.setFixedSize(120, button_height)
        top_buttons_layout.addWidget(self.select_button)

        self.toggle_button = QPushButton("Gremlins")
        self.toggle_button.setFixedSize(button_width, button_height)
        self.toggle_button.clicked.connect(self.toggle_chaos)
        # set padding with stylesheet
        self.toggle_button.setStyleSheet("padding: 0px 5px 0px 5px;")
        top_buttons_layout.addWidget(self.toggle_button)

        self.interval_field = QLineEdit()
        self.interval_field.setText(str(self.interval_seconds))
        self.interval_field.setPlaceholderText("seconds")
        self.interval_field.setFixedSize(button_width, button_height)
        self.interval_field.setAlignment(Qt.AlignCenter)
        self.interval_field.textChanged.connect(self.on_interval_changed)
        self.interval_field.setToolTip("Chaos interval in seconds")
        top_buttons_layout.addWidget(self.interval_field)

        layout.addLayout(top_buttons_layout)
        layout.setAlignment(top_buttons_layout, Qt.AlignLeft)
        layout.setAlignment(Qt.AlignTop)  # Align the QVBoxLayout to the top
        central_widget.setLayout(layout)

    # --------- PROJECT MANAGEMENT ---------

    def set_project_folder(self, folder):
        """Set the project folder via ProjectManager"""
        return self.project_manager.set_project_folder(folder)

    def select_project_folder(self):
        """Open folder dialog and set project folder"""
        folder = QFileDialog.getExistingDirectory(self, "Select Project Folder")
        if folder:
            self.project_manager.set_project_folder(folder)

    def project_folder_was_set(self, folder):
        """Project folder was set (no label to update)"""
        if DEBUG: print(f"DEBUG: Project folder was set: {folder}")

    def on_project_loaded(self, project_folder):
        """Project loaded (no label to update)"""
        if DEBUG: print(f"DEBUG: Project loaded: {project_folder}")

    def on_project_changed(self, project_folder):
        """Project changed (no label to update)"""
        if not project_folder:
            if ERROR: print("ERROR: No project folder selected")

    @property
    def project_loaded(self):
        return self.project_manager.project_loaded

    @property
    def project_changed(self):
        return self.project_manager.project_changed

    def get_project_folder(self):
        return self.project_manager.get_project_folder()

    def get_folder_path(self, folder_name):
        return self.project_manager.get_folder_path(folder_name)

    def get_file_path(self, file_path):
        return self.project_manager.get_file_path(file_path)

    def folder_exists(self, folder_name):
        return self.project_manager.folder_exists(folder_name)

    def file_exists(self, file_path):
        return self.project_manager.file_exists(file_path)

    def get_required_files(self):
        return self.project_manager.get_required_files()
    
    # ------ GREMLINS ---------

    def on_interval_changed(self, text):
        """Handle interval field changes"""
        try:
            value = float(text)
            if value < minimum_load_interval:
                value = minimum_load_interval
                if DEBUG: print(f"DEBUG: Gremlins interval forced to {minimum_load_interval}s")
            
            # Update interval
            self.interval_seconds = value
            if self.is_running:
                # Restart timer with new interval
                self.chaos_timer.stop()
                self.chaos_timer.start(int(self.interval_seconds * 1000))
            if DEBUG: print(f"DEBUG: Gremlins interval changed to {self.interval_seconds}s")
            
        except ValueError:
            # Invalid float input (empty field, letters, etc.) - just ignore
            if DEBUG: print(f"DEBUG: Invalid Gremlins interval input: '{text}'")
            pass
            
    def toggle_chaos(self):
        """Toggle chaos generation on/off"""
        if self.is_running:
            self.stop_chaos()
        else:
            self.start_chaos()
            
    def start_chaos(self):
        """Start generating chaos events"""
        self.is_running = True
        self.toggle_button.setText("Chaos!")
        
        # Start the timer
        interval_ms = int(self.interval_seconds * 1000)
        self.chaos_timer.start(interval_ms)
        
        if DEBUG: print(f"DEBUG: Gremlins started - chaos every {self.interval_seconds}s")
        
    def stop_chaos(self):
        """Stop generating chaos events"""
        self.is_running = False
        self.toggle_button.setText("Gremlins")

        # Stop the timer
        self.chaos_timer.stop()
        
        if DEBUG: print("DEBUG: Gremlins stopped")
        
    def emit_chaos(self):
        """Emit a chaos event"""
        if DEBUG: print("DEBUG: Gremlins emitting chaos event")
        
        # Blink the interval field
        self.blink_interval_field()
        
        # Emit the chaos signal
        self.chaos.emit()
        
    def blink_interval_field(self):
        """Make the interval field blink briefly"""
        # Store original style
        original_style = self.interval_field.styleSheet()
        
        # Set blink style (fuchsia background)
        self.interval_field.setStyleSheet(f"QLineEdit {{ color: {HIGHLIGHT_COLOR}; background-color: {HIGHLIGHT_BACKGROUND_COLOR}; }}")

        # Reset to original style after 90ms
        QTimer.singleShot(90, lambda: self.interval_field.setStyleSheet(original_style))
        
    def clear_project(self):
        if self.is_running:
            self.stop_chaos()
        # Reset project manager state if needed
        self.select_button.setEnabled(True)

    def closeEvent(self, event):
        """Handle window close"""
        if self.is_running:
            self.stop_chaos()
        super().closeEvent(event)

    # --------- METADATA REBUILDING ---------

    def on_metadata_rebuilding_started(self):
        if DEBUG:
            print("DEBUG: RobotsWindow received metadata_rebuilding_started")

    def on_metadata_rebuilding_stopped(self):
        if DEBUG:
            print("DEBUG: RobotsWindow received metadata_rebuilding_stopped")

    # ---- Save/Load Preferences ----

    def on_preferences_save(self):
        self._pending_save_data = self.project_manager.get_preferences_data()

    def on_preferences_load(self, data):
        self.project_manager.load_preferences_data(data)