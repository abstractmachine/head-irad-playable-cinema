DEBUG = False  # Set to True to enable debug output

import os
from PyQt5.QtCore import Qt, pyqtSignal
from PyQt5.QtWidgets import (
    QMainWindow, QVBoxLayout, QHBoxLayout, QWidget, QTextEdit, QPushButton, QLabel
)

class InferenceWindow(QMainWindow):
    request_save = pyqtSignal()
    request_load = pyqtSignal(dict)

    def __init__(self, ui):
        super().__init__()
        self.ui = ui  # Store UI instance
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
        self.inference_field.setPlaceholderText("Test example: Inference not yet implemented.")
        self.inference_field.setReadOnly(True)  # Make it read-only

        # Set to 'text' font from UI
        self.inference_field.setFont(self.ui.get_font('text'))
        self.inference_field.setStyleSheet("QTextEdit { border: none; padding: 5px; }")
        main_layout.addWidget(self.inference_field, stretch=1)

        # Button layout
        button_layout = QHBoxLayout()
        button_layout.setSpacing(10)

        # Button dimensions
        button_width, button_height = self.ui.get_dimensions('button')

        # Off button
        self.off_button = QPushButton("Off")
        self.off_button.clicked.connect(self.turn_off_inference)
        self.off_button.setFixedSize(button_width, button_height)
        self.off_button.setFont(self.ui.get_font('button'))
        button_layout.addWidget(self.off_button)

        # Model button
        self.model_button = QPushButton("Model")
        self.model_button.clicked.connect(self.select_model)
        self.model_button.setFixedSize(button_width, button_height)
        self.model_button.setFont(self.ui.get_font('button'))
        button_layout.addWidget(self.model_button)

        # Current model display (simple label matching button height)
        self.current_model_field = QLabel(self.current_model)
        self.current_model_field.setFont(self.ui.get_font('monospace'))
        self.current_model_field.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        tiny_height = self.ui.get_dimensions('tiny')[1]
        self.current_model_field.setFixedHeight(tiny_height)  # Use tiny height
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
        self.current_model_field.setText(self.current_model)
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

    def on_movie_loaded_with_metadata(self, movie_path, metadata=None):
        """
        Called when a new movie is loaded in the player, with optional metadata.
        Expects metadata to contain 'title', 'filename', and 'year'.
        """
        if not metadata:
            if DEBUG:
                print("Inference: No metadata provided for movie.")
                print(f"Inference: Movie loaded - {movie_path}")
                print(f"Inference: Movie metadata {metadata}")
            self.inference_field.setPlainText("No metadata provided.")
            return

        movie_name = metadata.get('title', 'Unknown Title')
        movie_filename = metadata.get('filename', 'Unknown Filename')
        movie_year = metadata.get('year', 'Unknown Year')

        self.setWindowTitle(f"Inference - {movie_name}")

        info = f"Ready for inference on:\nTitle: {movie_name}\nFilename: {movie_filename}\nYear: {movie_year}"
        self.inference_field.setPlainText(info)

        if DEBUG:
            print(f"Inference: Movie loaded - {movie_filename}")
            print(f"Inference: Title - {movie_name}")
            print(f"Inference: Year - {movie_year}")

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
            self.current_model_field.setText(self.current_model)