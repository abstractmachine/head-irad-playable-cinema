import os
from PyQt5.QtGui import QFont, QFontDatabase
from PyQt5.QtCore import Qt, pyqtSignal
from PyQt5.QtWidgets import (
    QMainWindow, QVBoxLayout, QHBoxLayout, QWidget, QTextEdit, QPushButton, 
    QLabel, QSizePolicy
)

class DefaultWindow(QMainWindow):
    request_save = pyqtSignal()
    request_load = pyqtSignal(dict)

    def __init__(self):
        super().__init__()
        self.setWindowTitle("Default Prompt")
        
        # Main layout
        main_layout = QVBoxLayout()
        main_layout.setContentsMargins(10, 10, 10, 10)
        main_layout.setSpacing(10)

        # Default prompt editor
        self.default_prompt_field = QTextEdit()
        self.default_prompt_field.setPlaceholderText("")
        
        # Load and set tooltip from file
        self.load_tooltip()
        
        # Load custom font
        font_path = os.path.join(os.path.dirname(__file__), "ui/fonts/HKGrotesk-Regular.otf")
        font_id = QFontDatabase.addApplicationFont(font_path)
        font_families = QFontDatabase.applicationFontFamilies(font_id)
        if font_families:
            hk_font = QFont(font_families[0], 12)
        else:
            hk_font = QFont("Helvetica", 12)
        
        self.default_prompt_field.setFont(hk_font)
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

    def load_tooltip(self):
        """Load tooltip text from preferences/prompt-tooltip.txt"""
        tooltip_path = os.path.join(
            os.path.dirname(__file__),
            "preferences",
            "prompt-tooltip.txt"
        )
        
        try:
            with open(tooltip_path, "r", encoding="utf-8") as f:
                tooltip_text = f.read().strip()
                self.default_prompt_field.setToolTip(tooltip_text)
        except Exception as e:
            # Fallback tooltip if file doesn't exist
            default_tooltip = "Edit the default system prompt that will be used as a template for new movies."
            self.default_prompt_field.setToolTip(default_tooltip)
            print(f"Could not load tooltip from {tooltip_path}: {e}")