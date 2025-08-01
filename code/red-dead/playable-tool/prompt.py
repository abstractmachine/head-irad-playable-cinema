DEBUG = False  # Set to True to enable debug output

import os
from PyQt5.QtCore import Qt, pyqtSignal
from PyQt5.QtWidgets import (
    QMainWindow, QVBoxLayout, QHBoxLayout, QWidget, QTextEdit, QPushButton, 
    QLabel, QSizePolicy, QComboBox, QStackedLayout
)

from caption import parse_system_prompt

class PromptWindow(QMainWindow):
    request_save = pyqtSignal()
    request_load = pyqtSignal(dict)

    def __init__(self, ui, subtitles_window=None):
        super().__init__()
        self.ui = ui
        self.subtitles_window = subtitles_window

        self.setWindowTitle("Prompt")

        self.metadata = {}
        self.row_data = {}

        if DEBUG: print("DEBUG: PromptWindow initialized")

        # Main layout: horizontal (buttons left, text right)
        main_layout = QHBoxLayout()
        main_layout.setContentsMargins(0, 0, 0, 0)  # Window margins set to 0
        main_layout.setSpacing(0)                   # No spacing between button and text

        # Left: vertical buttons
        button_width, button_height = self.ui.get_dimensions('button')
        button_layout = QVBoxLayout()
        button_layout.setSpacing(0)                 # No spacing between buttons
        button_layout.setContentsMargins(0, 0, 0, 0)  # Button layout margins set to 0

        self.prompt_type_dropdown = QComboBox()
        self.prompt_type_dropdown.addItems([
            "Shot", "Test", "Tags"
        ])
        self.prompt_type_dropdown.setFont(self.ui.get_font('button'))
        self.prompt_type_dropdown.setFixedSize(80, button_height)
        self.prompt_type_dropdown.currentIndexChanged.connect(self.handle_prompt_type_changed)
        button_layout.addWidget(self.prompt_type_dropdown)

        # align button_layout to the right
        self.prompt_type_dropdown.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        self.prompt_type_dropdown.setStyleSheet("QComboBox { padding: 0px 0px 0px 10px; margin: 0px 0px 0px 10px; }")

        self.test_button = QPushButton("Test")
        self.test_button.setFont(self.ui.get_font('button'))
        self.test_button.setFixedSize(80, button_height)
        self.test_button.setStyleSheet("QPushButton { padding: 0px 0px 0px 0px; margin: 10px 0px 0px 10px; }")
        self.test_button.clicked.connect(self.handle_test_button)
        button_layout.addWidget(self.test_button)

        button_layout.addStretch()
        main_layout.addLayout(button_layout, stretch=0)

        # Right: stacked text fields
        text_layout = QVBoxLayout()
        text_layout.setSpacing(0)                   # No spacing inside text layout
        text_layout.setContentsMargins(0, 0, 0, 0)  # Text layout margins set to 0

        self.stacked_layout = QStackedLayout()

        self.system_prompt_field = QTextEdit()
        self.system_prompt_field.setPlaceholderText("")
        self.system_prompt_field.setFont(self.ui.get_font('prompt'))
        self.system_prompt_field.setStyleSheet("QTextEdit { border: none; padding: 0px; }")  # No border, no padding
        self.stacked_layout.addWidget(self.system_prompt_field)

        self.test_field = QTextEdit()
        self.test_field.setReadOnly(True)
        self.test_field.setFont(self.ui.get_font('prompt'))
        self.test_field.setStyleSheet("QTextEdit { border: none; padding: 0px; }")
        self.stacked_layout.addWidget(self.test_field)

        self.cheatsheet_field = QTextEdit()
        self.cheatsheet_field.setReadOnly(True)
        self.cheatsheet_field.setFont(self.ui.get_font('prompt'))
        self.cheatsheet_field.setStyleSheet("QTextEdit { border: none; padding: 0px; }")
        self.stacked_layout.addWidget(self.cheatsheet_field)

        text_layout.addLayout(self.stacked_layout)
        main_layout.addLayout(text_layout, stretch=1)

        container = QWidget()
        container.setLayout(main_layout)
        self.setCentralWidget(container)

        # Shot prompt file path
        self.shot_prompt_path = os.path.join(
            os.path.dirname(__file__),
            "preferences",
            "shot_prompt.txt"
        )
        self.cheatsheet_path = os.path.join(
            os.path.dirname(__file__),
            "preferences",
            "prompt-tooltip.txt"
        )

        # Show the correct field at startup
        self.handle_prompt_type_changed(0)

        # Connect textChanged signal to save_current_prompt slot
        self.system_prompt_field.textChanged.connect(self.save_current_prompt)

    def handle_prompt_type_changed(self, idx):
        prompt_type = self.prompt_type_dropdown.currentText()
        if DEBUG: print(f"DEBUG: Prompt type changed to {prompt_type}")
        self.test_button.setEnabled(prompt_type == "Test")
        # self.shot_prompt_button.setEnabled(prompt_type == "Shot")  # Removed
        if prompt_type == "Shot":
            self.system_prompt_field.setReadOnly(False)
            self.stacked_layout.setCurrentWidget(self.system_prompt_field)
            self.load_shot_prompt()
        elif prompt_type == "Test":
            self.stacked_layout.setCurrentWidget(self.test_field)
        elif prompt_type == "Tags":
            self.stacked_layout.setCurrentWidget(self.cheatsheet_field)
            self.load_cheatsheet()
            
    def handle_row_data(self, row_data):
        """Handle row data emitted from shotlist."""
        self.row_data = row_data or {}
        if DEBUG: print(f"DEBUG Prompt: Received row data: {row_data}")

    def handle_test_button(self):
        # Get current prompt text
        prompt_text = self.system_prompt_field.toPlainText()
        # Parse prompt using the parser from annotate.py
        interpreted = parse_system_prompt(prompt_text, getattr(self, "current_metadata", self.metadata), self.row_data, self.subtitles_window)
        # Show interpreted result in test field
        self.test_field.setPlainText(interpreted)

    def load_cheatsheet(self):
        if DEBUG: print("DEBUG: Loading tags cheatsheet")
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

    def on_movie_loaded_with_metadata(self, movie_path, metadata):
        if DEBUG: print(f"DEBUG Prompt: on_movie_loaded called with {movie_path}")
        self.current_metadata = metadata

    def load_shot_prompt(self):
        if DEBUG: print("DEBUG: load_shot_prompt called")
        try:
            with open(self.shot_prompt_path, "r", encoding="utf-8") as f:
                text = f.read()
            self.system_prompt_field.setPlainText(text)
            if DEBUG: print("DEBUG: Loaded shot prompt from file")
        except Exception as e:
            shot_prompt = "Analyze these video frames and provide a detailed description of the scene, focusing on visual elements, actions, and composition."
            self.system_prompt_field.setPlainText(shot_prompt)
            print(f"Could not load shot prompt: {e}")
            if DEBUG: print(f"DEBUG: Could not load shot prompt: {e}")

    def save_current_prompt(self):
        text = self.system_prompt_field.toPlainText()
        try:
            with open(self.shot_prompt_path, "w", encoding="utf-8") as f:
                f.write(text)
            if DEBUG: print(f"DEBUG: Saved shot prompt to {self.shot_prompt_path}")
        except Exception as e:
            print(f"Error saving shot prompt: {e}")
            if DEBUG: print(f"DEBUG: Error saving shot prompt: {e}")

    def on_request_save(self):
        geo = self.geometry()
        if DEBUG: print(f"DEBUG: on_request_save called, geometry={geo}")
        self._pending_save_data = {
        }
        return self._pending_save_data

    def on_request_load(self, data):
        if DEBUG: print(f"DEBUG: on_request_load called with {data}")
        if data:
            # do nothing
            pass
