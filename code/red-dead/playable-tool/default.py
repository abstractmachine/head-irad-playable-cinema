DEBUG = False  # Set to True to enable debug output

import os
from PyQt5.QtCore import Qt, pyqtSignal
from PyQt5.QtWidgets import (
    QMainWindow, QVBoxLayout, QHBoxLayout, QWidget, QTextEdit, QPushButton, 
    QLabel, QSizePolicy
)

class DefaultWindow(QMainWindow):
    request_save = pyqtSignal()
    request_load = pyqtSignal(dict)

    def __init__(self, ui):
        super().__init__()
        self.ui = ui  # Store UI instance
        self.setWindowTitle("Default Prompt")
        
        # Main layout
        main_layout = QVBoxLayout()
        main_layout.setContentsMargins(10, 10, 10, 10)
        main_layout.setSpacing(10)

        # Default prompt editor
        self.default_prompt_field = QTextEdit()
        self.default_prompt_field.setPlaceholderText("")
        
        # Use UI font system instead of custom font loading
        self.default_prompt_field.setFont(self.ui.get_font('prompt'))
        self.default_prompt_field.setStyleSheet("QTextEdit { border: none; padding: 5px; }")
        main_layout.addWidget(self.default_prompt_field, stretch=1)

        # Set up container
        container = QWidget()
        container.setLayout(main_layout)
        self.setCentralWidget(container)

        # Default prompt file path
        self.default_prompt_path = os.path.join(
            os.path.dirname(__file__),
            "preferences",
            "default_prompt.txt"
        )
        
        # Load default prompt at startup
        self.load_default_prompt()

        # Auto-save on every character change
        self.default_prompt_field.textChanged.connect(self.save_default_prompt)

    def load_default_prompt(self):
        """Load default prompt from file"""
        try:
            with open(self.default_prompt_path, "r", encoding="utf-8") as f:
                text = f.read()
            self.default_prompt_field.setPlainText(text)
        except Exception as e:
            default_prompt = "Analyze these video frames and provide a detailed description of the scene, focusing on visual elements, actions, and composition."
            self.default_prompt_field.setPlainText(default_prompt)
            print(f"Could not load default prompt: {e}")

    def save_default_prompt(self):
        """Save default prompt to file automatically on each change"""
        text = self.default_prompt_field.toPlainText()
        try:
            # Ensure directory exists
            os.makedirs(os.path.dirname(self.default_prompt_path), exist_ok=True)
            with open(self.default_prompt_path, "w", encoding="utf-8") as f:
                f.write(text)
        except Exception as e:
            print(f"Error saving default prompt: {e}")

    def on_request_save(self):
        """Save window preferences"""
        geo = self.geometry()
        self._pending_save_data = {
            "x": geo.x(),
            "y": geo.y(),
            "width": geo.width(),
            "height": geo.height()
        }
        return self._pending_save_data

    def on_request_load(self, data):
        """Load window preferences"""
        if data:
            x = data.get("x", 100)
            y = data.get("y", 100)
            w = data.get("width", 600)
            h = data.get("height", 400)
            self.setGeometry(x, y, w, h)