DEBUG = True  # Set to True to enable debug output
ERROR = True  # Set to True to enable error output

from PyQt5.QtCore import Qt, pyqtSignal, QTimer
from PyQt5.QtWidgets import (
    QMainWindow, QWidget, QHBoxLayout, QVBoxLayout, QLineEdit, QPushButton, QTextEdit, QComboBox
)
from PyQt5.QtGui import QTextCursor
import random
import os

from utility import minimum_load_interval, HIGHLIGHT_BACKGROUND_COLOR, HIGHLIGHT_COLOR
from project import ProjectManager

class RobotsWindow(QMainWindow):
    """
    Chaos generator window that sends random events at specified intervals.
    Used for testing application robustness and simulating unpredictable user behavior.
    """
    
    # Signals
    preferences_save = pyqtSignal()
    preferences_load = pyqtSignal(dict)

    # Project
    project_folder_was_set = pyqtSignal(str)
    project_folder_was_changed = pyqtSignal(str)  # Emitted when project folder changes

    # Gremlins
    chaos = pyqtSignal()
    
    def __init__(self, ui):
        super().__init__()
        self.ui = ui
        self.setWindowTitle("Gremlins")
        self.setMinimumSize(125, 70)

        # Add this for preferences compatibility
        self._pending_save_data = {}

        # Project management
        self.project_manager = ProjectManager(parent=self)

        # State
        self.is_running = False
        self.interval_seconds = 3.0
        
        # Timer for chaos events
        self.chaos_timer = QTimer()
        self.chaos_timer.timeout.connect(self.emit_chaos)

        # Layout variable
        self.layout_index = -1
        self.saved_layouts = []
        
        self.setup_ui(ui)
        
    def setup_ui(self, ui):
        """Set up the user interface"""
        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        
        layout = QVBoxLayout()
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)

        button_width, button_height = ui.get_dimensions("button")

        # --- Top row: Project Folder button, Layouts dropdown, Save/Delete buttons ---
        top_buttons_layout = QHBoxLayout()
        top_buttons_layout.setContentsMargins(0, 0, 0, 0)
        top_buttons_layout.setSpacing(2)

        self.select_button = QPushButton("Project Folder")
        self.select_button.setFont(self.ui.get_font('button'))
        self.select_button.clicked.connect(self.project_manager.select_project_folder)
        self.select_button.setFixedSize(120, button_height)
        top_buttons_layout.addWidget(self.select_button)

        self.layouts_dropdown = QComboBox()
        self.layouts_dropdown.setFixedHeight(button_height)
        self.layouts_dropdown.setMinimumWidth(120)
        self.layouts_dropdown.setCurrentIndex(-1)
        self.layouts_dropdown.currentIndexChanged.connect(self.layout_selected)
        top_buttons_layout.addWidget(self.layouts_dropdown)

        self.save_button = QPushButton("Save")
        self.save_button.setFixedSize(60, button_height)
        self.save_button.setStyleSheet("padding: 0px 5px 0px 5px;")
        self.save_button.setEnabled(False)
        top_buttons_layout.addWidget(self.save_button)

        self.delete_button = QPushButton("Delete")
        self.delete_button.setFixedSize(60, button_height)
        self.delete_button.setStyleSheet("padding: 0px 5px 0px 5px;")
        self.delete_button.setEnabled(False)
        top_buttons_layout.addWidget(self.delete_button)

        layout.addLayout(top_buttons_layout)
        layout.setAlignment(top_buttons_layout, Qt.AlignLeft)

        # --- Second row: Gremlins button and interval field ---
        chaos_layout = QHBoxLayout()
        chaos_layout.setContentsMargins(0, 0, 0, 0)
        chaos_layout.setSpacing(2)

        self.toggle_button = QPushButton("Gremlins")
        self.toggle_button.setFixedSize(button_width, button_height)
        self.toggle_button.clicked.connect(self.toggle_chaos)
        self.toggle_button.setStyleSheet("padding: 0px 5px 0px 5px;")
        chaos_layout.addWidget(self.toggle_button)

        self.interval_field = QLineEdit()
        self.interval_field.setText(str(self.interval_seconds))
        self.interval_field.setPlaceholderText("seconds")
        self.interval_field.setFixedSize(button_width, button_height)
        self.interval_field.setAlignment(Qt.AlignCenter)
        self.interval_field.textChanged.connect(self.on_interval_changed)
        self.interval_field.setToolTip("Chaos interval in seconds")
        chaos_layout.addWidget(self.interval_field)

        layout.addLayout(chaos_layout)
        layout.setAlignment(chaos_layout, Qt.AlignLeft)
        layout.setAlignment(Qt.AlignTop)

        # --- Console at the bottom ---
        self.console = QTextEdit()
        self.console.setReadOnly(True)
        self.console.setMinimumHeight(60)
        font = ui.get_font('console')
        self.console.setStyleSheet(f"font-family: {font.family()}; font-size: {font.pointSize()}px;")
        layout.addWidget(self.console)

        central_widget.setLayout(layout)

    # --------- PROJECT MANAGEMENT ---------

    def on_project_folder_loaded(self, folder):
        """Project folder was set (no label to update)"""
        if DEBUG: print(f"DEBUG: Project folder was set: {folder}")
        message = f"Project folder set: {folder}" if folder else "No project folder selected"
        self.console_write(message)
        self.get_layouts()  # Populate layouts dropdown when project folder is loaded

    # --------- LAYOUT MANAGEMENT ---------

    def get_layouts(self):
        """Populate the layouts dropdown with .layout files from the project's layouts folder, ignoring dotfiles and stripping extension"""
        self.saved_layouts = []
        project_folder = self.project_manager.get_project_folder()
        if project_folder:
            layouts_folder = os.path.join(project_folder, "layouts")
            if os.path.isdir(layouts_folder):
                for fname in os.listdir(layouts_folder):
                    if fname.startswith('.'):
                        continue
                    if fname.endswith(".layout"):
                        layout_name = fname[:-7]  # Remove '.layout'
                        self.saved_layouts.append(layout_name)
        self.layouts_dropdown.blockSignals(True)
        self.layouts_dropdown.clear()
        self.layouts_dropdown.addItem("Default")
        for layout_name in self.saved_layouts:
            self.layouts_dropdown.addItem(layout_name)
        self.layouts_dropdown.setCurrentIndex(-1)
        self.layouts_dropdown.blockSignals(False)

    def layout_selected(self, index):
        """Handle layout selection changes"""
        if index < 0:
            return  # Ignore signal when nothing is selected
        layout_name = self.layouts_dropdown.itemText(index)
        if DEBUG: print(f"DEBUG: Layout selected: {layout_name}")
        # self.project_manager.set_current_layout(layout_name)

    # @property
    # def project_loaded(self):
    #     return self.project_manager.project_loaded

    # @property
    # def project_changed(self):
    #     return self.project_manager.project_changed

    # def get_project_folder(self):
    #     return self.project_manager.get_project_folder()

    # def get_folder_path(self, folder_name):
    #     return self.project_manager.get_folder_path(folder_name)

    # def get_file_path(self, file_path):
    #     return self.project_manager.get_file_path(file_path)

    # def folder_exists(self, folder_name):
    #     return self.project_manager.folder_exists(folder_name)

    # def file_exists(self, file_path):
    #     return self.project_manager.file_exists(file_path)

    # def get_required_files(self):
    #     return self.project_manager.get_required_files()
    
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

    # -------------- CONSOLE ---------------

    def console_write(self, text):
        """Write text to the console, managing history size"""
        current_text = self.console.toPlainText()
        lines = current_text.split('\n') if current_text else []
        lines.append(str(text))
        if len(lines) > 1000:
            lines = ['{…}'] + lines[-900:]
        self.console.setPlainText('\n'.join(lines))
        self.console.moveCursor(QTextCursor.End)

    # ------------ PREFERENCES -------------

    def on_preferences_save(self):
        self._pending_save_data = self.project_manager.get_preferences_data()

    def on_preferences_load(self, data):
        self.project_manager.load_preferences_data(data)
