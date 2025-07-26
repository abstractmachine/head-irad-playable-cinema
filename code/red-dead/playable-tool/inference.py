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
        self.caption_model = "No caption model loaded"
        self.search_model = "No search model loaded"
        
        # Main layout
        main_layout = QVBoxLayout()
        main_layout.setContentsMargins(10, 10, 10, 10)
        main_layout.setSpacing(10)

        # Gameplay inference field (top)
        self.gameplay_inference = QTextEdit()
        self.gameplay_inference.setPlaceholderText("Live caption model inferencing outputs here.")
        self.gameplay_inference.setReadOnly(True)
        self.gameplay_inference.setFont(self.ui.get_font('text'))
        self.gameplay_inference.setStyleSheet("QTextEdit { border: none; padding: 5px; }")
        main_layout.addWidget(self.gameplay_inference, stretch=1)

        # Matched caption field (bottom)
        self.matched_caption = QTextEdit()
        self.matched_caption.setPlaceholderText("Matched caption will appear here.")
        self.matched_caption.setReadOnly(True)
        self.matched_caption.setFont(self.ui.get_font('text'))
        self.matched_caption.setStyleSheet("QTextEdit { border: none; padding: 5px; }")
        main_layout.addWidget(self.matched_caption, stretch=1)

        # Button layout
        button_layout = QHBoxLayout()
        button_layout.setSpacing(10)

        # Button dimensions
        button_width, button_height = self.ui.get_dimensions('button')

        # Off button
        self.off_button = QPushButton("Off")
        self.off_button.clicked.connect(self.turn_off_inference)
        self.off_button.setFixedSize(80, button_height)
        self.off_button.setFont(self.ui.get_font('button'))
        button_layout.addWidget(self.off_button)

        # Inference Model button
        self.model_button = QPushButton("Caption")
        self.model_button.clicked.connect(self.select_model)
        self.model_button.setFixedSize(button_width, button_height)
        self.model_button.setFont(self.ui.get_font('button'))
        button_layout.addWidget(self.model_button)

        # Current model display
        self.caption_model_field = QLabel(self.caption_model)
        self.caption_model_field.setFont(self.ui.get_font('monospace'))
        self.caption_model_field.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        tiny_height = self.ui.get_dimensions('tiny')[1]
        self.caption_model_field.setFixedHeight(tiny_height)
        button_layout.addWidget(self.caption_model_field, stretch=1)

        # Search Model button
        self.search_model_button = QPushButton("Search")
        self.search_model_button.clicked.connect(self.select_search_model)
        self.search_model_button.setFixedSize(button_width, button_height)
        self.search_model_button.setFont(self.ui.get_font('button'))
        button_layout.addWidget(self.search_model_button)

        # Search model display
        self.search_model_field = QLabel(self.search_model)
        self.search_model_field.setFont(self.ui.get_font('monospace'))
        self.search_model_field.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        self.search_model_field.setFixedHeight(tiny_height)
        button_layout.addWidget(self.search_model_field, stretch=1)

        main_layout.addLayout(button_layout)

        # Set up container
        container = QWidget()
        container.setLayout(main_layout)
        self.setCentralWidget(container)

    def turn_off_inference(self):
        """Handle Off button click"""
        self.gameplay_inference.setPlainText("Inference turned off.")
        self.matched_caption.setPlainText("")
        if DEBUG: print("Inference: Turned off")

    def select_model(self):
        """Handle Inference Model button click"""
        self.caption_model = "Example Model v1.0"
        self.caption_model_field.setText(self.caption_model)
        self.gameplay_inference.setPlainText("Caption Model loaded: " + self.caption_model)
        if DEBUG: print(f"Caption: Model selected - {self.caption_model}")

    def select_search_model(self):
        """Handle Search Model button click"""
        self.search_model = "Search Model v1.0"
        self.search_model_field.setText(self.search_model)
        self.matched_caption.setPlainText("Search model loaded: " + self.search_model)
        if DEBUG: print(f"Inference: Search model selected - {self.search_model}")

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
        self.gameplay_inference.setPlainText(f"Ready for inference on: {movie_name}")
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
            self.gameplay_inference.setPlainText("No metadata provided.")
            return

        movie_name = metadata.get('title', 'Unknown Title')
        movie_filename = metadata.get('filename', 'Unknown Filename')
        movie_year = metadata.get('year', 'Unknown Year')

        self.setWindowTitle(f"Inference - {movie_name}")

        info = f"Ready to match \nTitle: {movie_name}\nFilename: {movie_filename}\nYear: {movie_year}"
        self.gameplay_inference.setPlainText(info)
        

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
            "caption_model": self.caption_model
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
            saved_model = data.get("caption_model", "No caption model loaded")
            self.caption_model = saved_model
            self.caption_model_field.setText(self.caption_model)