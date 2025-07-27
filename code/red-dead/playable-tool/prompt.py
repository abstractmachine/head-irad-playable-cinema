DEBUG = True  # Set to True to enable debug output

import os
from PyQt5.QtCore import Qt, pyqtSignal
from PyQt5.QtWidgets import (
    QMainWindow, QVBoxLayout, QHBoxLayout, QWidget, QTextEdit, QPushButton, 
    QLabel, QSizePolicy, QComboBox, QStackedLayout
)

class PromptWindow(QMainWindow):
    request_save = pyqtSignal()
    request_load = pyqtSignal(dict)

    def __init__(self, ui):
        super().__init__()
        self.ui = ui
        self.setWindowTitle("Prompt")
        
        self.project_folder = None
        self.current_movie_filename = None
        self.current_prompt_path = None

        # DEBUG: Print when window is initialized
        if DEBUG: print("DEBUG: PromptWindow initialized")

        # Main layout
        main_layout = QVBoxLayout()
        main_layout.setContentsMargins(10, 10, 10, 10)
        main_layout.setSpacing(10)

        # Stacked layout for prompt fields
        self.stacked_layout = QStackedLayout()

        # System prompt editor
        self.system_prompt_field = QTextEdit()
        self.system_prompt_field.setPlaceholderText("No movie loaded")
        self.system_prompt_field.setFont(self.ui.get_font('prompt'))
        self.system_prompt_field.setStyleSheet("QTextEdit { border: none; padding: 5px; }")
        self.stacked_layout.addWidget(self.system_prompt_field)

        # Test field (read-only)
        self.test_field = QTextEdit()
        self.test_field.setReadOnly(True)
        self.test_field.setFont(self.ui.get_font('prompt'))
        self.test_field.setStyleSheet("QTextEdit { border: none; padding: 5px; }")
        self.stacked_layout.addWidget(self.test_field)

        # Cheatsheet field (read-only)
        self.cheatsheet_field = QTextEdit()
        self.cheatsheet_field.setReadOnly(True)
        self.cheatsheet_field.setFont(self.ui.get_font('prompt'))
        self.cheatsheet_field.setStyleSheet("QTextEdit { border: none; padding: 5px; }")
        self.stacked_layout.addWidget(self.cheatsheet_field)

        main_layout.addLayout(self.stacked_layout)

        button_width, button_height = self.ui.get_dimensions('button')

        # Dropdown for prompt type
        self.prompt_type_dropdown = QComboBox()
        self.prompt_type_dropdown.addItems([
            "Movie", "Default", "Test", "Cheatsheet"
        ])
        self.prompt_type_dropdown.setFont(self.ui.get_font('button'))
        self.prompt_type_dropdown.setFixedSize(button_width, button_height)
        self.prompt_type_dropdown.currentIndexChanged.connect(self.handle_prompt_type_changed)

        # Default button (new)
        self.default_button = QPushButton("Default")
        self.default_button.setFont(self.ui.get_font('button'))
        self.default_button.setFixedSize(button_width, button_height)
        self.default_button.setEnabled(False)  # Inactive at start
        self.default_button.clicked.connect(self.handle_default_button)

        # Test button
        self.test_button = QPushButton("Test")
        self.test_button.setFont(self.ui.get_font('button'))
        self.test_button.setFixedSize(button_width, button_height)
        self.test_button.clicked.connect(self.handle_test_button)

        # Layout for dropdown and buttons
        controls_layout = QHBoxLayout()
        controls_layout.addWidget(self.prompt_type_dropdown)
        controls_layout.addWidget(self.default_button)
        controls_layout.addWidget(self.test_button)
        main_layout.addLayout(controls_layout)

        container = QWidget()
        container.setLayout(main_layout)
        self.setCentralWidget(container)

        # Default prompt file path
        self.default_prompt_path = os.path.join(
            os.path.dirname(__file__),
            "preferences",
            "default_prompt.txt"
        )
        self.cheatsheet_path = os.path.join(
            os.path.dirname(__file__),
            "preferences",
            "prompt-tooltip.txt"
        )

        # Connect textChanged signal to save_current_prompt slot
        self.system_prompt_field.textChanged.connect(self.save_current_prompt)

        # Show the correct field at startup
        self.handle_prompt_type_changed(0)

    def handle_prompt_type_changed(self, idx):
        prompt_type = self.prompt_type_dropdown.currentText()
        if DEBUG: print(f"DEBUG: Prompt type changed to {prompt_type}")
        self.test_button.setEnabled(prompt_type == "Test")
        # Enable Default button only if a movie is loaded and prompt_type is Movie
        self.default_button.setEnabled(
            prompt_type == "Movie" and self.current_movie_filename is not None
        )
        if prompt_type == "Movie":
            self.system_prompt_field.setReadOnly(False)
            self.stacked_layout.setCurrentWidget(self.system_prompt_field)
            self.load_movie_prompt()
        elif prompt_type == "Default":
            self.system_prompt_field.setReadOnly(False)
            self.stacked_layout.setCurrentWidget(self.system_prompt_field)
            self.load_default_prompt()
        elif prompt_type == "Test":
            self.stacked_layout.setCurrentWidget(self.test_field)
        elif prompt_type == "Cheatsheet":
            self.stacked_layout.setCurrentWidget(self.cheatsheet_field)
            self.load_cheatsheet()

    def handle_test_button(self):
        if DEBUG: print("DEBUG: Test button pressed")
        self.test_field.setPlainText("Test button pressed!")

    def load_cheatsheet(self):
        if DEBUG: print("DEBUG: Loading cheatsheet")
        if os.path.exists(self.cheatsheet_path):
            try:
                with open(self.cheatsheet_path, "r", encoding="utf-8") as f:
                    text = f.read()
                self.cheatsheet_field.setPlainText(text)
            except Exception as e:
                self.cheatsheet_field.setPlainText(f"Error loading cheatsheet: {e}")
                if DEBUG: print(f"DEBUG: Error loading cheatsheet: {e}")
        else:
            self.cheatsheet_field.setPlainText("No cheatsheet found.")
            if DEBUG: print("DEBUG: Cheatsheet file not found.")

    def set_project_folder(self, project_folder):
        if DEBUG: print(f"DEBUG: set_project_folder called with {project_folder}")
        self.project_folder = project_folder

    def on_movie_loaded(self, movie_path):
        if DEBUG: print(f"DEBUG: on_movie_loaded called with {movie_path}")
        if not self.project_folder:
            print("Warning: No project folder set in prompt window")
            return
        movie_filename = os.path.basename(movie_path)
        if self.current_movie_filename == movie_filename:
            if DEBUG: print("DEBUG: Movie already loaded, skipping reload")
            return
        self.current_movie_filename = movie_filename
        if movie_filename.lower().endswith('.mp4'):
            prompt_filename = movie_filename[:-4] + '.txt'
        else:
            prompt_filename = movie_filename + '.txt'
        self.current_prompt_path = os.path.join(self.project_folder, "prompts", prompt_filename)
        movie_name = os.path.splitext(movie_filename)[0]
        self.setWindowTitle(f"Prompt - {movie_name}")
        self.load_movie_prompt()
        self.handle_prompt_type_changed(self.prompt_type_dropdown.currentIndex())

    def load_movie_prompt(self):
        if DEBUG: print(f"DEBUG: load_movie_prompt called for {self.current_prompt_path}")
        if not self.current_prompt_path:
            return
        if os.path.exists(self.current_prompt_path):
            try:
                with open(self.current_prompt_path, "r", encoding="utf-8") as f:
                    text = f.read()
                self.system_prompt_field.setPlainText(text)
                if DEBUG: print("DEBUG: Loaded movie prompt from file")
            except Exception as e:
                print(f"Error loading movie prompt: {e}")
                if DEBUG: print(f"DEBUG: Error loading movie prompt: {e}")
                self.load_default_prompt()
        else:
            if DEBUG: print("DEBUG: Movie prompt not found, creating from default")
            self.create_movie_prompt_from_default()

    def create_movie_prompt_from_default(self):
        if DEBUG: print("DEBUG: create_movie_prompt_from_default called")
        try:
            default_text = ""
            if os.path.exists(self.default_prompt_path):
                with open(self.default_prompt_path, "r", encoding="utf-8") as f:
                    default_text = f.read()
            else:
                default_text = "Analyze these video frames and provide a detailed description of the scene, focusing on visual elements, actions, and composition."
            self.system_prompt_field.setPlainText(default_text)
            self.save_current_prompt()
            if DEBUG: print("DEBUG: Created movie prompt from default")
        except Exception as e:
            print(f"Error creating movie prompt from default: {e}")
            if DEBUG: print(f"DEBUG: Error creating movie prompt from default: {e}")
            self.load_default_prompt()

    def load_default_prompt(self):
        if DEBUG: print("DEBUG: load_default_prompt called")
        try:
            with open(self.default_prompt_path, "r", encoding="utf-8") as f:
                text = f.read()
            self.system_prompt_field.setPlainText(text)
            if DEBUG: print("DEBUG: Loaded default prompt from file")
        except Exception as e:
            default_prompt = "Analyze these video frames and provide a detailed description of the scene, focusing on visual elements, actions, and composition."
            self.system_prompt_field.setPlainText(default_prompt)
            print(f"Could not load default prompt: {e}")
            if DEBUG: print(f"DEBUG: Could not load default prompt: {e}")

    def save_current_prompt(self):
        text = self.system_prompt_field.toPlainText()
        prompt_type = self.prompt_type_dropdown.currentText()
        if DEBUG: print(f"DEBUG: save_current_prompt called, type={prompt_type}, text length={len(text)}")
        if prompt_type == "Movie" and self.current_prompt_path and self.project_folder:
            try:
                prompts_dir = os.path.dirname(self.current_prompt_path)
                os.makedirs(prompts_dir, exist_ok=True)
                with open(self.current_prompt_path, "w", encoding="utf-8") as f:
                    f.write(text)
                if DEBUG: print(f"DEBUG: Saved movie prompt to {self.current_prompt_path}")
            except Exception as e:
                print(f"Error saving movie prompt: {e}")
                if DEBUG: print(f"DEBUG: Error saving movie prompt: {e}")
        elif prompt_type == "Default":
            try:
                with open(self.default_prompt_path, "w", encoding="utf-8") as f:
                    f.write(text)
                if DEBUG: print(f"DEBUG: Saved default prompt to {self.default_prompt_path}")
            except Exception as e:
                print(f"Error saving default prompt: {e}")
                if DEBUG: print(f"DEBUG: Error saving default prompt: {e}")

    def on_request_save(self):
        geo = self.geometry()
        if DEBUG: print(f"DEBUG: on_request_save called, geometry={geo}")
        self._pending_save_data = {
            "x": geo.x(),
            "y": geo.y(),
            "width": geo.width(),
            "height": geo.height()
        }
        return self._pending_save_data

    def on_request_load(self, data):
        if DEBUG: print(f"DEBUG: on_request_load called with {data}")
        if data:
            x = data.get("x", 100)
            y = data.get("y", 100)
            w = data.get("width", 600)
            h = data.get("height", 400)
            self.setGeometry(x, y, w, h)

    def handle_default_button(self):
        if DEBUG: print("DEBUG: Default button pressed")
        self.create_movie_prompt_from_default()
