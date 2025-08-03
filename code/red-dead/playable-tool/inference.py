DEBUG = False  # Set to True to enable debug output

import os
from PyQt5.QtCore import Qt, pyqtSignal
from PyQt5.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QTextEdit, QPushButton, QLabel
)

class InferenceWindow(QWidget):
    request_save = pyqtSignal()
    request_load = pyqtSignal(dict)

    def __init__(self, ui):
        super().__init__()
        self.ui = ui  # Store UI instance

        self.setMinimumHeight(80)
        self.project_folder = None
        self.current_movie_filename = None
        self.caption_model = ""
        self.search_model = ""

        # Main layout
        main_layout = QHBoxLayout()
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(0)

        # --- Top area: Two columns (each with QTextEdit) ---
        top_layout = QHBoxLayout()
        top_layout.setSpacing(2)  # 2px space between the two text fields

        # Left column (gameplay_inference)
        left_col = QVBoxLayout()
        left_col.setSpacing(0)
        self.gameplay_inference = QTextEdit()
        self.gameplay_inference.setPlaceholderText("No caption model loaded.")
        self.gameplay_inference.setReadOnly(True)
        self.gameplay_inference.setFont(self.ui.get_font('text'))
        self.gameplay_inference.setStyleSheet("QTextEdit { border: none; padding: 0px; margin: 0px; }")
        left_col.addWidget(self.gameplay_inference, stretch=1)

        # Right column (matched_caption)
        right_col = QVBoxLayout()
        right_col.setSpacing(0)
        self.matched_caption = QTextEdit()
        self.matched_caption.setPlaceholderText("No search model loaded.")
        self.matched_caption.setReadOnly(True)
        self.matched_caption.setFont(self.ui.get_font('text'))
        self.matched_caption.setStyleSheet("QTextEdit { border: none; padding: 0px; margin: 0px; }")
        right_col.addWidget(self.matched_caption, stretch=1)

        # Add columns to top_layout
        top_layout.addLayout(left_col, stretch=1)
        top_layout.addLayout(right_col, stretch=1)

        # --- Button area: Vertical on the right ---
        button_layout = QVBoxLayout()
        button_layout.setSpacing(2)
        button_layout.setContentsMargins(0, 0, 0, 0)
        button_layout.setAlignment(Qt.AlignTop)
        button_width, button_height = self.ui.get_dimensions('button')

        self.caption_model_button = QPushButton("Caption")
        self.caption_model_button.clicked.connect(self.select_caption_model)
        self.caption_model_button.setFixedSize(80, button_height)
        self.caption_model_button.setFont(self.ui.get_font('button'))
        button_layout.addWidget(self.caption_model_button)

        self.off_button = QPushButton("Off")
        self.off_button.clicked.connect(self.turn_off_inference)
        self.off_button.setFixedSize(80, button_height)
        self.off_button.setFont(self.ui.get_font('button'))
        button_layout.addWidget(self.off_button)

        self.search_model_button = QPushButton("Search")
        self.search_model_button.clicked.connect(self.select_search_model)
        self.search_model_button.setFixedSize(80, button_height)
        self.search_model_button.setFont(self.ui.get_font('button'))
        button_layout.addWidget(self.search_model_button)

        # Assemble main layout: text fields (stretch=2), then vertical buttons (stretch=0)
        main_layout.addLayout(top_layout, stretch=2)
        main_layout.addLayout(button_layout, stretch=0)

        self.setLayout(main_layout)

    def turn_off_inference(self):
        """Handle Off button click"""
        self.gameplay_inference.setPlainText("")
        self.matched_caption.setPlainText("")
        if DEBUG: print("Inference: Turned off")

    def select_caption_model(self):
        """Handle Inference Model button click"""
        self.caption_model = "Caption Model v0.1"
        self.gameplay_inference.setPlainText("Caption Model loaded: " + self.caption_model)
        if DEBUG: print(f"Caption: Model selected - {self.caption_model}")

    def select_search_model(self):
        """Handle Search Model button click"""
        self.search_model = "Search Model v0.1"
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
        self.matched_caption.setPlainText(f"{movie_name}")
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
            self.matched_caption.setPlainText("No metadata provided.")
            return

        movie_name = metadata.get('title', 'Unknown Title')
        movie_filename = metadata.get('filename', 'Unknown Filename')
        movie_year = metadata.get('year', 'Unknown Year')

        self.setWindowTitle(f"Inference - {movie_name}")

        info = f"{movie_name}\n ({movie_year})"
        self.matched_caption.setPlainText(info)
        
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
        self._pending_save_data = {
            "caption_model": self.caption_model
        }
        return self._pending_save_data

    def on_request_load(self, data):
        """Load window preferences"""
        if data:
            # Load saved model
            saved_model = data.get("caption_model", "")
            self.caption_model = saved_model
            self.gameplay_inference.setPlainText(self.caption_model)