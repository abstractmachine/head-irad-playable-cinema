import os
from PyQt5.QtGui import QFont, QFontDatabase
from PyQt5.QtCore import Qt, pyqtSignal
from PyQt5.QtWidgets import (
    QMainWindow, QVBoxLayout, QHBoxLayout, QWidget, QTextEdit, QPushButton, 
    QLabel, QSizePolicy
)

class PromptWindow(QMainWindow):
    request_save = pyqtSignal()
    request_load = pyqtSignal(dict)

    def __init__(self):
        super().__init__()
        self.setWindowTitle("Prompt")
        
        # Main layout
        main_layout = QVBoxLayout()
        main_layout.setContentsMargins(10, 10, 10, 10)
        main_layout.setSpacing(10)

        # System prompt editor
        self.system_prompt_field = QTextEdit()
        self.system_prompt_field.setPlaceholderText("Enter your system prompt here...")
        
        # Load custom font
        font_path = os.path.join(os.path.dirname(__file__), "ui/fonts/HKGrotesk-Regular.otf")
        font_id = QFontDatabase.addApplicationFont(font_path)
        font_families = QFontDatabase.applicationFontFamilies(font_id)
        if font_families:
            hk_font = QFont(font_families[0], 12)
        else:
            hk_font = QFont("Helvetica", 12)
        
        self.system_prompt_field.setFont(hk_font)
        self.system_prompt_field.setStyleSheet("QTextEdit { border: none; padding: 5px; }")
        main_layout.addWidget(self.system_prompt_field, stretch=1)

        # Set up container
        container = QWidget()
        container.setLayout(main_layout)
        self.setCentralWidget(container)

        # System prompt file path
        self.system_prompt_path = os.path.join(
            os.path.dirname(__file__),
            "preferences",
            "system_prompt.txt"
        )
        
        # Load system prompt at startup
        self.load_system_prompt()

        # Auto-save on every character change
        self.system_prompt_field.textChanged.connect(self.save_system_prompt)

    def load_system_prompt(self):
        """Load system prompt from file"""
        try:
            with open(self.system_prompt_path, "r", encoding="utf-8") as f:
                text = f.read()
            self.system_prompt_field.setPlainText(text)
        except Exception as e:
            default_prompt = "Analyze these video frames and provide a detailed description of the scene, focusing on visual elements, actions, and composition."
            self.system_prompt_field.setPlainText(default_prompt)
            print(f"Could not load system prompt: {e}")

    def save_system_prompt(self):
        """Save system prompt to file automatically on each change"""
        text = self.system_prompt_field.toPlainText()
        try:
            # Ensure directory exists
            os.makedirs(os.path.dirname(self.system_prompt_path), exist_ok=True)
            with open(self.system_prompt_path, "w", encoding="utf-8") as f:
                f.write(text)
        except Exception as e:
            print(f"Error saving system prompt: {e}")

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