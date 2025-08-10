DEBUG = False  # Set to True to enable debug output
ERROR = True  # Set to True to enable error output

# Qt stuff
from PyQt5.QtCore import Qt, pyqtSignal, QTimer
from PyQt5.QtWidgets import (
    QMainWindow, QWidget, QHBoxLayout, QVBoxLayout, QLineEdit, QPushButton, QTextEdit, QComboBox,
    QDialog, QVBoxLayout, QLabel
)
from PyQt5.QtGui import QPixmap
from PyQt5.QtGui import QTextCursor

# System stuff
import os

# Our methods
from utility import minimum_load_interval, HIGHLIGHT_BACKGROUND_COLOR, HIGHLIGHT_COLOR
from project import ProjectManager
from layout import load_window_geometry, load_dock_layout


class SaveLayoutDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Save Layout")
        self.setMinimumWidth(320)
        layout = QVBoxLayout()
        # Icon
        icon = QPixmap("ui/icons/cowgirl_writing.png")
        icon_label = QLabel()
        icon_size = 256
        icon_label.setPixmap(icon.scaled(icon_size, icon_size))
        icon_label.setAlignment(Qt.AlignCenter)
        layout.addWidget(icon_label)
        # Text box
        self.name_edit = QLineEdit()
        self.name_edit.setPlaceholderText("Enter layout name")
        self.name_edit.setFixedSize(360, 32)
        self.name_edit.setMaxLength(16)  # Limit to 16 characters
        # Center Text Box Horizontally in Window
        self.name_edit.setAlignment(Qt.AlignCenter)
        layout.addWidget(self.name_edit)
        # Buttons
        button_layout = QHBoxLayout()
        self.save_btn = QPushButton("Save")
        self.cancel_btn = QPushButton("Cancel")
        button_layout.addWidget(self.save_btn)
        button_layout.addWidget(self.cancel_btn)
        layout.addLayout(button_layout)
        self.setLayout(layout)
        # Connections
        self.save_btn.clicked.connect(self.accept)
        self.cancel_btn.clicked.connect(self.reject)

    def get_name(self):
        return self.name_edit.text().strip()


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

    # Inference buttons
    caption_model_requested = pyqtSignal()
    search_model_requested = pyqtSignal()
    inference_off_requested = pyqtSignal()

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
        # self.select_button.setFont(self.ui.get_font('button'))
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
        # self.save_button.setStyleSheet("padding: 0px 5px 0px 5px;")
        self.save_button.setEnabled(True)
        self.save_button.clicked.connect(self.save_layout)
        top_buttons_layout.addWidget(self.save_button)

        self.delete_button = QPushButton("Delete")
        self.delete_button.setFixedSize(60, button_height)
        # self.delete_button.setStyleSheet("padding: 0px 5px 0px 5px;")
        self.delete_button.setEnabled(False)
        self.delete_button.clicked.connect(self.delete_layout)
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
        # self.toggle_button.setStyleSheet("padding: 0px 5px 0px 5px;")
        chaos_layout.addWidget(self.toggle_button)

        self.interval_field = QLineEdit()
        self.interval_field.setText(str(self.interval_seconds))
        self.interval_field.setPlaceholderText("5.0")
        self.interval_field.setFixedSize(button_width, button_height)
        self.interval_field.setAlignment(Qt.AlignCenter)
        self.interval_field.textChanged.connect(self.on_interval_changed)
        self.interval_field.setToolTip("Chaos interval in seconds")
        chaos_layout.addWidget(self.interval_field)

        # --- Inference Buttons ---
        self.caption_model_button = QPushButton("BLIP")
        self.caption_model_button.setFixedSize(80, button_height)
        # self.caption_model_button.setFont(self.ui.get_font('button'))
        self.caption_model_button.clicked.connect(self.caption_model_requested.emit)
        chaos_layout.addWidget(self.caption_model_button)

        self.search_model_button = QPushButton("FAISS")
        self.search_model_button.setFixedSize(80, button_height)
        # self.search_model_button.setFont(self.ui.get_font('button'))
        self.search_model_button.clicked.connect(self.search_model_requested.emit)
        chaos_layout.addWidget(self.search_model_button)

        self.off_button = QPushButton("Inference")
        self.off_button.setFixedSize(button_width, button_height)
        # self.off_button.setFont(self.ui.get_font('button'))
        self.off_button.clicked.connect(self.inference_off_requested.emit)
        chaos_layout.addWidget(self.off_button)

        layout.addLayout(chaos_layout)
        layout.setAlignment(chaos_layout, Qt.AlignLeft)
        layout.setAlignment(Qt.AlignTop)

        # --- Third row: Shotlist buttons ---
        shotlist_layout = QHBoxLayout()
        shotlist_layout.setContentsMargins(0, 0, 0, 0)
        shotlist_layout.setSpacing(2)

        # Method dropdown
        self.method_dropdown = QComboBox()
        self.method_dropdown.addItems([
            "detect-adaptive",
            "detect-content",
            "detect-hist",
            "detect-threshold"
        ])
        self.method_dropdown.setFixedHeight(button_height)
        # self.method_dropdown.setFont(ui.get_font('button'))
        shotlist_layout.addWidget(self.method_dropdown)

        # Weights field
        self.weights_field = QLineEdit("-t 3.0")
        self.weights_field.setAlignment(Qt.AlignCenter)
        self.weights_field.setFont(ui.get_font('tiny-condensed'))
        self.weights_field.setFixedSize(80, button_height)
        shotlist_layout.addWidget(self.weights_field)

        # Detect Shots button
        self.detect_button = QPushButton("Shots")
        self.detect_button.setFixedSize(80, button_height)
        # self.detect_button.setFont(ui.get_font('button'))
        shotlist_layout.addWidget(self.detect_button)

        # Detect Scenes button
        self.detect_scenes_button = QPushButton("Scenes")
        self.detect_scenes_button.setFixedSize(80, button_height)
        # self.detect_scenes_button.setFont(ui.get_font('button'))
        shotlist_layout.addWidget(self.detect_scenes_button)

        # Delete button
        self.delete_button_shotlist = QPushButton("Delete")
        self.delete_button_shotlist.setFixedSize(button_width, button_height)
        # self.delete_button_shotlist.setFont(ui.get_font('button'))
        shotlist_layout.addWidget(self.delete_button_shotlist)

        layout.addLayout(shotlist_layout)
        layout.setAlignment(shotlist_layout, Qt.AlignLeft)

        # --- Fourth row: Caption buttons ---
        caption_layout = QHBoxLayout()
        caption_layout.setContentsMargins(0, 0, 0, 0)
        caption_layout.setSpacing(2)

        # System dropdown
        self.prompt_type_dropdown = QComboBox()
        self.prompt_type_dropdown.addItems(["System", "Shot", "Scene", "Gameplay"])
        self.prompt_type_dropdown.setCurrentIndex(0)
        self.prompt_type_dropdown.setFont(self.ui.get_font('tiny-condensed'))
        self.prompt_type_dropdown.setFixedSize(115, button_height)
        caption_layout.addWidget(self.prompt_type_dropdown)

        # Annotate button
        self.annotate_button = QPushButton("Annotate")
        self.annotate_button.setEnabled(False)
        # self.annotate_button.setFont(self.ui.get_font('button'))
        self.annotate_button.setFixedSize(115, button_height)
        self.annotate_button.setToolTip("Rewrite current caption into current 'Caption' cell\nShortcut: A")
        caption_layout.addWidget(self.annotate_button)

        # API button
        self.api_button = QPushButton("API")
        self.api_button.setEnabled(False)
        self.api_button.setFont(self.ui.get_font('tiny'))
        self.api_button.setFixedSize(80, button_height)
        self.api_button.setToolTip("Send current shot to AI API and receive a caption\nShortcut: O")
        caption_layout.addWidget(self.api_button)

        # Frame count field
        self.frame_count_field = QLineEdit("5")
        self.frame_count_field.setFont(self.ui.get_font('tiny'))
        self.frame_count_field.setFixedSize(24, 24)
        self.frame_count_field.setAlignment(Qt.AlignCenter)
        self.frame_count_field.setToolTip("Number of image frames to send to API (0 = none)")
        self.frame_count_field.setStyleSheet("QLineEdit { margin: 0px 0px 0px 0px; }")
        caption_layout.addWidget(self.frame_count_field)

        caption_layout.addStretch()

        layout.addLayout(caption_layout)
        layout.setAlignment(caption_layout, Qt.AlignLeft)

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
            self.delete_button.setEnabled(False)
            return  # Ignore signal when nothing is selected
        layout_name = self.layouts_dropdown.itemText(index)
        if DEBUG: print(f"DEBUG: Layout selected: {layout_name}")

        # Enable delete button only for non-Default layouts
        self.delete_button.setEnabled(index != 0)

        main_window = self.window()
        app_folder = os.path.dirname(os.path.abspath(__file__))

        if layout_name == "Default":
            layout_folder = os.path.join(app_folder, "preferences", "layouts")
            geometry_file = os.path.join(layout_folder, "default.geometry")
            layout_file = os.path.join(layout_folder, "default.layout")
            if DEBUG: print(f"DEBUG: Loading geometry from {geometry_file}")
            if DEBUG: print(f"DEBUG: Loading dock layout from {layout_file}")
            load_window_geometry(main_window, geometry_file)
            load_dock_layout(main_window, layout_file)
            self.layouts_dropdown.setCurrentIndex(-1)
        else:
            # Load from project folder's layouts directory
            project_folder = self.project_manager.get_project_folder()
            layout_folder = os.path.join(project_folder, "layouts")
            geometry_file = os.path.join(layout_folder, f"{layout_name}.geometry")
            layout_file = os.path.join(layout_folder, f"{layout_name}.layout")
            if DEBUG: print(f"DEBUG: Loading geometry from {geometry_file}")
            if DEBUG: print(f"DEBUG: Loading dock layout from {layout_file}")
            load_window_geometry(main_window, geometry_file)
            load_dock_layout(main_window, layout_file)

    def delete_layout(self):
        """Delete the selected layout from the project folder"""
        index = self.layouts_dropdown.currentIndex()
        if index <= 0:
            return  # Don't delete Default or nothing
        layout_name = self.layouts_dropdown.itemText(index)
        project_folder = self.project_manager.get_project_folder()
        layout_folder = os.path.join(project_folder, "layouts")
        # Use layout.py utility to delete files
        from layout import delete_layout_files
        deleted_files = delete_layout_files(layout_folder, layout_name)
        self.console_write(f"Deleted layout: {layout_name}")
        # Refresh the dropdown
        self.get_layouts()
        # Turn off Delete button
        self.delete_button.setEnabled(False)

    def save_layout(self):
        """Show dialog to get layout name and handle save logic"""
        # Show dialog
        dialog = SaveLayoutDialog(self)
        result = dialog.exec_()
        if result != QDialog.Accepted:
            return  # User cancelled

        parsed_name = self.parse_layout_save_name(dialog.get_name())
        if not parsed_name:
            return  # Error already handled
        # save this layout
        main_window = self.window()
        project_folder = self.project_manager.get_project_folder()
        layout_folder = os.path.join(project_folder, "layouts")
        if not os.path.exists(layout_folder):
            os.makedirs(layout_folder)
        geometry_file = os.path.join(layout_folder, f"{parsed_name}.geometry")
        layout_file = os.path.join(layout_folder, f"{parsed_name}.layout")
        from layout import save_window_geometry, save_dock_layout
        save_window_geometry(main_window, geometry_file)
        save_dock_layout(main_window, layout_file)
        self.console_write(f"Saved layout: {parsed_name}")
        self.get_layouts()

    def parse_layout_save_name(self, name):
        """Parse and validate layout save name according to rules"""
        if not name:
            self.console_write("No filename provided")
            return None
        if name.lower() == "default":
            self.console_write("Invalid filename")
            return None
        # Replace spaces with '_'
        name = name.replace(' ', '_')
        # Replace any character not AZaz09-_ with '-'
        import re
        name = re.sub(r'[^A-Za-z0-9\-_]', '-', name)
        # Check first character
        if not re.match(r'^[A-Za-z0-9]', name):
            self.console_write("Invalid filename")
            return None
        return name

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
        self.toggle_button.setText("Gremlins")
        self.toggle_button.setStyleSheet("background-color: #f0f; color: #fff;")
        
        # Start the timer
        interval_ms = int(self.interval_seconds * 1000)
        self.chaos_timer.start(interval_ms)
        
        if DEBUG: print(f"DEBUG: Gremlins started - chaos every {self.interval_seconds}s")
        
    def stop_chaos(self):
        """Stop generating chaos events"""
        self.is_running = False
        self.toggle_button.setText("Gremlins")
        self.toggle_button.setStyleSheet("")

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
        self._pending_save_data["weights_field"] = self.weights_field.text()
        self._pending_save_data["method_selected"] = self.method_dropdown.currentText()

    def on_preferences_load(self, data):
        self.project_manager.load_preferences_data(data)
        if "weights_field" in data:
            self.weights_field.setText(data["weights_field"])
        if "method_selected" in data:
            idx = self.method_dropdown.findText(data["method_selected"])
            if idx != -1:
                self.method_dropdown.setCurrentIndex(idx)
