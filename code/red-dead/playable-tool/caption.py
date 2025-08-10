DEBUG = False  # Set to True to enable debug output

import os
import tempfile
import base64
import mimetypes
import openai
import re

from PyQt5.QtGui import QTextOption
from PyQt5.QtCore import Qt, QThread, pyqtSignal, QObject, QTimer
from PyQt5.QtWidgets import (
    QMainWindow, QVBoxLayout, QHBoxLayout, QWidget, QLineEdit, QTextEdit, QPushButton, QSizePolicy, QLabel, QComboBox
)

from prompt import parse_system_prompt_files
from utility import DARK_DOCK_BORDER, LIGHT_DOCK_BORDER

def encode_image(image_array):
    import cv2
    import time

    # Create temp file, close handle before writing with OpenCV
    with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tmp:
        temp_jpg_path = tmp.name

    # Write image with OpenCV (file is closed)
    cv2.imwrite(temp_jpg_path, image_array)
    mime_type, _ = mimetypes.guess_type(temp_jpg_path)

    # Read and encode
    with open(temp_jpg_path, "rb") as image_file:
        encoded = f"data:{mime_type};base64," + base64.b64encode(image_file.read()).decode("utf-8")

    # Try to remove, retry if needed (Windows)
    for _ in range(3):
        try:
            os.remove(temp_jpg_path)
            break
        except PermissionError:
            time.sleep(0.1)
    return encoded

class ApiWorker(QObject):
    finished = pyqtSignal()
    result = pyqtSignal(object)

    def __init__(self, frames, metadata, row_data, subtitles_window, project_folder=None, movie_filename=None):
        super().__init__()
        self.frames = frames
        self.metadata = metadata
        self.row_data = row_data
        self.subtitles_window = subtitles_window
        self.project_folder = project_folder
        self.movie_filename = movie_filename

    def run(self):        
        images_payload = []
        for frame in self.frames:
            images_payload.append({
                "type": "image_url",
                "image_url": {
                    "url": encode_image(frame),
                    "detail": "high"
                }
            })
        
        if DEBUG: print("DEBUG: Interpreting system prompt")

        # add image-count to metadata
        self.metadata["image-count"] = len(self.frames)

        # Use the new parse_system_prompt_files function
        parsed_prompt = parse_system_prompt_files(
            self.project_folder,
            self.metadata,
            self.row_data,
            self.subtitles_window
        )
        
        # Check result of parsing
        if parsed_prompt == "" or parsed_prompt is None:
            if DEBUG: print("DEBUG: No specific prompt found, using generic prompt")
            # Fallback to generic prompt if prompts directory doesn't exist
            parsed_prompt = "Describe the scene in these images."

        if DEBUG: print(f"DEBUG: Parsed prompt: {parsed_prompt}")
        
        api_key_path = os.path.join(self.project_folder, "preferences", "openai_api_key.txt")
        try:
            with open(api_key_path, "r", encoding="utf-8") as f:
                api_key = f.read().strip()
        except Exception as e:
            caption = f"Error reading API key: {e}"
            self.result.emit(caption)
            self.finished.emit()
            return

        messages = [
            {"role": "system", "content": parsed_prompt},
            {"role": "user", "content": images_payload}
        ]
        
        try:
            response = openai.OpenAI(api_key=api_key).chat.completions.create(
                model="gpt-4o",
                messages=messages,
                temperature=0.2,
                max_tokens=300,
            )
            caption = response.choices[0].message.content
        except Exception as e:
            caption = f"Error generating caption: {e}"

        self.result.emit(caption)
        self.finished.emit()

class SystemPromptEdit(QTextEdit):
    def __init__(self, parent=None, save_callback=None):
        super().__init__(parent)
        self.save_callback = save_callback

    def focusOutEvent(self, event):
        if self.save_callback:
            self.save_callback()
        super().focusOutEvent(event)

class CaptionWindow(QWidget):
    preferences_save = pyqtSignal()
    preferences_load = pyqtSignal(dict)
    shot_caption_submitted = pyqtSignal(str)
    request_current_shot = pyqtSignal(int)
    request_next_shot = pyqtSignal()
    request_previous_shot = pyqtSignal()

    def __init__(self, ui, subtitles_window):
        super().__init__()
        self.ui = ui  # Store UI instance
        self.subtitles_window = subtitles_window  # Store reference to subtitles window

        self.setMinimumHeight(80)

        # Main horizontal layout, no margins or spacing
        main_layout = QHBoxLayout()
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(0)

        # Shot caption field (multi-line widget)
        self.shot_caption_field = QTextEdit()
        self.shot_caption_field.setPlaceholderText("Shot captions")
        self.shot_caption_field.setFont(self.ui.get_font('text'))
        self.shot_caption_field.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.shot_caption_field.setWordWrapMode(QTextOption.WordWrap)
        if ui.is_dark_mode():
            self.shot_caption_field.setStyleSheet(f"QTextEdit {{ border: none; border-right: 2px solid {DARK_DOCK_BORDER}; padding: 0px; margin: 0px; }}")
        else:
            self.shot_caption_field.setStyleSheet(f"QTextEdit {{ border: none; border-right: 2px solid {LIGHT_DOCK_BORDER}; padding: 0px; margin: 0px; }}")

        main_layout.addWidget(self.shot_caption_field, stretch=1)

        # Scene caption field (multi-line widget)
        self.scene_caption_field = QTextEdit()
        self.scene_caption_field.setPlaceholderText("Scene captions")
        self.scene_caption_field.setFont(self.ui.get_font('text'))
        self.scene_caption_field.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.scene_caption_field.setWordWrapMode(QTextOption.WordWrap)
        if ui.is_dark_mode():
            self.scene_caption_field.setStyleSheet(f"QTextEdit {{ border: none; border-left: 2px solid {DARK_DOCK_BORDER}; padding: 0px; margin: 0px; }}")
        else:
            self.scene_caption_field.setStyleSheet(f"QTextEdit {{ border: none; border-left: 2px solid {LIGHT_DOCK_BORDER}; padding: 0px; margin: 0px; }}")


        main_layout.addWidget(self.scene_caption_field, stretch=1)

        self.setLayout(main_layout)

        # Ensure main window has focus at startup
        self.setFocus()
        self.setFocusPolicy(Qt.StrongFocus)

        self.current_timecodes = []
        self.is_last_row = False
        self.is_first_row = True
        self.api_running = False
        self.project_folder = None
        self.current_movie_filename = None

    def keyPressEvent(self, event):
        super().keyPressEvent(event)

    def on_preferences_save(self):
        geo = self.geometry()
        self._pending_save_data = {
            # No frame_count to save anymore
        }
        return self._pending_save_data

    def on_preferences_load(self, data):
        pass  # No frame_count to load anymore

    def set_shotlist_status(self, loaded):
        pass  # No buttons to enable/disable

    def submit_caption(self):
        text = self.shot_caption_field.toPlainText()
        self.shot_caption_submitted.emit(text)

    def clear_project(self):
        if DEBUG: print("DEBUG: ProjectWindow: clear_project called (no action needed)")

    def on_project_folder_loaded(self, project_folder):
        self.project_folder = project_folder
        self.shot_caption_field.clear()

    def on_movie_loaded_with_metadata(self, movie_path, metadata):
        movie_filename = os.path.basename(movie_path)
        self.metadata = metadata
        self.current_movie_filename = movie_filename

    def handle_row_data(self, row_data):
        self.row_data = row_data
        if DEBUG: print(f"DEBUG Caption: Received row data: {row_data}")

    def set_shot_caption_field(self, caption):
        self.shot_caption_field.setPlainText(caption)

    def handle_is_last_available_shot(self, is_last):
        self.is_last_row = is_last

    def handle_is_first_available_shot(self, is_first):
        if DEBUG: print(f"DEBUG: handle_is_first_available_shot called - is_first={is_first}")
        self.is_first_row = is_first

    def jump_to_first_available_shot(self):
        self.request_current_shot.emit(0)  # No frame count field anymore