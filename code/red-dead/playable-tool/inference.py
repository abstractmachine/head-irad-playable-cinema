import os
from PyQt5.QtGui import QFont, QFontDatabase
from PyQt5.QtCore import Qt, pyqtSignal
from PyQt5.QtWidgets import (
    QMainWindow, QVBoxLayout, QHBoxLayout, QWidget, QTextEdit, QPushButton
)

DEBUG = False  # Set to True for debugging output

class InferenceWindow(QMainWindow):
    request_save = pyqtSignal()
    request_load = pyqtSignal(dict)

    def __init__(self):
        super().__init__()
        self.setWindowTitle("Inference")
        
        # Initialize variables
        self.project_folder = None
        self.current_movie_filename = None
        self.current_model = "No model loaded"
        
        # Main layout
        main_layout = QVBoxLayout()
        main_layout.setContentsMargins(10, 10, 10, 10)
        main_layout.setSpacing(10)

        # Inference text display (read-only)
        self.inference_field = QTextEdit()
        self.inference_field.setPlaceholderText("Inference output will appear here...")
        self.inference_field.setReadOnly(True)  # Make it read-only
        
        # Load custom font - same as annotate.py and subtitles.py
        font_path = os.path.join(os.path.dirname(__file__), "ui/fonts/HKGrotesk-Regular.otf")
        font_id = QFontDatabase.addApplicationFont(font_path)
        font_families = QFontDatabase.applicationFontFamilies(font_id)
        if font_families:
            hk_font = QFont(font_families[0], 18)
        else:
            hk_font = QFont("Helvetica", 18)
        
        self.inference_field.setFont(hk_font)
        self.inference_field.setStyleSheet("QTextEdit { border: none; padding: 5px; }")
        main_layout.addWidget(self.inference_field, stretch=1)

        # Button layout
        button_layout = QHBoxLayout()
        button_layout.setSpacing(10)

        # Off button
        self.off_button = QPushButton("Off")
        self.off_button.clicked.connect(self.turn_off_inference)
        self.off_button.setFixedSize(80, 32)
        button_layout.addWidget(self.off_button)

        # Model button
        self.model_button = QPushButton("Model")
        self.model_button.clicked.connect(self.select_model)
        self.model_button.setFixedSize(80, 32)
        button_layout.addWidget(self.model_button)

        # Current model display (read-only text field)
        self.current_model_field = QTextEdit()
        self.current_model_field.setPlainText(self.current_model)
        self.current_model_field.setReadOnly(True)
        self.current_model_field.setMaximumHeight(32)  # Make it single line height
        self.current_model_field.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.current_model_field.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.current_model_field.setFont(QFont("Helvetica", 12))
        self.current_model_field.setStyleSheet("QTextEdit { border: none; padding: 5px; }")  # Removed border
        button_layout.addWidget(self.current_model_field, stretch=1)

        main_layout.addLayout(button_layout)

        # Set up container
        container = QWidget()
        container.setLayout(main_layout)
        self.setCentralWidget(container)

    def turn_off_inference(self):
        """Handle Off button click"""
        self.inference_field.setPlainText("Inference turned off.")
        if DEBUG: print("Inference: Turned off")

    def select_model(self):
        """Handle Model button click"""
        # For now, just simulate model selection
        # In the future, this could open a file dialog or model selection interface
        self.current_model = "Example Model v1.0"
        self.current_model_field.setPlainText(self.current_model)
        self.inference_field.setPlainText("Model loaded: " + self.current_model)
        if DEBUG: print(f"Inference: Model selected - {self.current_model}")

    def set_project_folder(self, project_folder):
        """Set the project folder when cinematheque window loads a project"""
        self.project_folder = project_folder
        if DEBUG: print(f"Inference: Project folder set to {project_folder}")

    def on_movie_loaded(self, movie_path):
        """Called when a new movie is loaded in the player"""
        movie_filename = os.path.basename(movie_path)
        
        # Don't reload if it's the same movie
        if self.current_movie_filename == movie_filename:
            return
            
        self.current_movie_filename = movie_filename
        
        # Update window title to show current movie
        movie_name = os.path.splitext(movie_filename)[0]
        self.setWindowTitle(f"Inference - {movie_name}")
        
        # Clear inference field for new movie
        self.inference_field.setPlainText(f"Ready for inference on: {movie_name}")
        if DEBUG: print(f"Inference: Movie loaded - {movie_filename}")

    def on_timecode_changed(self, timecode_ms):
        """Called whenever the video timecode changes"""
        # Convert milliseconds to HH:MM:SS format for display
        seconds = timecode_ms // 1000
        h = seconds // 3600
        m = (seconds % 3600) // 60
        s = seconds % 60
        
        timecode_str = f"{h:02}:{m:02}:{s:02}"
        
        # For now, just update with timecode info
        # In the future, this could trigger real-time inference
        # self.inference_field.setPlainText(f"Current timecode: {timecode_str}")

    def on_request_save(self):
        """Save window preferences"""
        geo = self.geometry()
        self._pending_save_data = {
            "x": geo.x(),
            "y": geo.y(),
            "width": geo.width(),
            "height": geo.height(),
            "current_model": self.current_model
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
            
            # Load saved model
            saved_model = data.get("current_model", "No model loaded")
            self.current_model = saved_model
            self.current_model_field.setPlainText(self.current_model)