DEBUG = False  # Set to True to enable debug output

import os
from PyQt5.QtCore import Qt, pyqtSignal
from PyQt5.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QTextEdit, QPushButton, QLabel
)

from utility import DARK_DOCK_BORDER, LIGHT_DOCK_BORDER

class InferenceWindow(QWidget):
    preferences_save = pyqtSignal()
    preferences_load = pyqtSignal(dict)

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
        top_layout.setSpacing(0)  # 2px space between the two text fields

        # Left column (gameplay_inference)
        left_col = QVBoxLayout()
        left_col.setSpacing(0)
        self.gameplay_inference = QTextEdit()
        self.gameplay_inference.setPlaceholderText("No caption model loaded.")
        self.gameplay_inference.setReadOnly(True)
        self.gameplay_inference.setFont(self.ui.get_font('text'))
        if ui.is_dark_mode():
            self.gameplay_inference.setStyleSheet(f"QTextEdit {{ border: none; border-right: 2px solid {DARK_DOCK_BORDER}; padding: 0px; margin: 0px; }}")
        else:
            self.gameplay_inference.setStyleSheet(f"QTextEdit {{ border: none; border-right: 2px solid {LIGHT_DOCK_BORDER}; padding: 0px; margin: 0px; }}")
        left_col.addWidget(self.gameplay_inference, stretch=1)

        # Right column (matched_caption)
        right_col = QVBoxLayout()
        right_col.setSpacing(0)
        self.matched_caption = QTextEdit()
        self.matched_caption.setPlaceholderText("No search model loaded.")
        self.matched_caption.setReadOnly(True)
        self.matched_caption.setFont(self.ui.get_font('text'))
        if ui.is_dark_mode():
            self.matched_caption.setStyleSheet(f"QTextEdit {{ border: none; border-left: 2px solid {DARK_DOCK_BORDER}; padding: 0px; margin: 0px; }}")
        else:
            self.matched_caption.setStyleSheet(f"QTextEdit {{ border: none; border-left: 2px solid {LIGHT_DOCK_BORDER}; padding: 0px; margin: 0px; }}")
        right_col.addWidget(self.matched_caption, stretch=1)

        # Add columns to top_layout
        top_layout.addLayout(left_col, stretch=1)
        top_layout.addLayout(right_col, stretch=1)

        # Only add the text fields, no buttons
        main_layout.addLayout(top_layout, stretch=1)

        # Set Text Color

        self.setLayout(main_layout)

    # Remove button methods, but keep these for switchboard:
    def turn_off_inference(self):
        self.gameplay_inference.setPlainText("")
        self.matched_caption.setPlainText("")
        if DEBUG: print("Inference: Turned off")

    def select_caption_model(self):
        self.caption_model = "BLIP not yet implemented"
        self.gameplay_inference.setPlainText("Captioning model: " + self.caption_model)
        if DEBUG: print(f"Caption: Model selected - {self.caption_model}")

    def select_search_model(self):
        self.search_model = "FAISS not yet implemented"
        self.matched_caption.setPlainText("Search model: " + self.search_model)
        if DEBUG: print(f"Inference: Search model selected - {self.search_model}")

    def on_project_folder_loaded(self, project_folder):
        """Set the project folder when cinematheque window loads a project"""
        self.project_folder = project_folder
        if DEBUG: print(f"Inference: Project folder set to {project_folder}")

    def clear_project(self):
        """Clear project - for consistency with other windows"""
        # A Placeholder for future functionality
        if DEBUG: print("DEBUG: ProjectWindow: clear_project called (no action needed)")

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

    def on_preferences_save(self):
        """Save window preferences"""
        self._pending_save_data = {
        }
        return self._pending_save_data

    def on_preferences_load(self, data):
        """Load window preferences"""
        if data:
            # Load saved model
            pass
            # saved_model = data.get("caption_model", "")
            # self.caption_model = saved_model
            # self.gameplay_inference.setPlainText(self.caption_model)