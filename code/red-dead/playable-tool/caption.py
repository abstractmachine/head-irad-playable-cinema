DEBUG = False  # Set to True to enable debug output

import os
import tempfile
import base64
import mimetypes
import openai

from PyQt5.QtCore import Qt, pyqtSignal, QObject
from PyQt5.QtWidgets import (
    QGridLayout, QWidget, QTextEdit, QLabel
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
    captions_submitted = pyqtSignal(str, str, str, str)
    caption_was_edited = pyqtSignal(str, str, str)

    def __init__(self, ui, subtitles_window):
        super().__init__()
        self.ui = ui
        self.subtitles_window = subtitles_window

        self.setMinimumHeight(160)

        # Create all four caption fields
        self.movie_shot_caption_field = QTextEdit()
        self.movie_shot_caption_field.setPlaceholderText("{movie-shot}")
        self.movie_shot_caption_field.setFont(self.ui.get_font('text'))

        self.movie_scene_caption_field = QTextEdit()
        self.movie_scene_caption_field.setPlaceholderText("{movie-scene}")
        self.movie_scene_caption_field.setFont(self.ui.get_font('text'))

        self.play_shot_caption_field = QTextEdit()
        self.play_shot_caption_field.setPlaceholderText("{play-shot}")
        self.play_shot_caption_field.setFont(self.ui.get_font('text'))

        self.play_scene_caption_field = QTextEdit()
        self.play_scene_caption_field.setPlaceholderText("{play-scene}")
        self.play_scene_caption_field.setFont(self.ui.get_font('text'))

        # Layout: 2x2 grid
        grid = QGridLayout()
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setSpacing(2)

        # Remove all QLabel widgets, only add QTextEdit fields
        grid.addWidget(self.movie_shot_caption_field, 0, 0)
        grid.addWidget(self.movie_scene_caption_field, 0, 1)
        grid.addWidget(self.play_shot_caption_field, 1, 0)
        grid.addWidget(self.play_scene_caption_field, 1, 1)

        self.setLayout(grid)

        # Ensure main window has focus at startup
        self.setFocus()
        self.setFocusPolicy(Qt.StrongFocus)

        self.project_folder = None
        self.current_movie_filename = None

        self.movie_shot_caption_field.textChanged.connect(self._on_movie_shot_caption_edited)
        self.movie_scene_caption_field.textChanged.connect(self._on_movie_scene_caption_edited)
        self.play_shot_caption_field.textChanged.connect(self._on_play_shot_caption_edited)
        self.play_scene_caption_field.textChanged.connect(self._on_play_scene_caption_edited)

    def keyPressEvent(self, event):
        super().keyPressEvent(event)

    def on_preferences_save(self):
        self._pending_save_data = {
        }
        return self._pending_save_data

    def on_preferences_load(self, data):
        pass  # No frame_count to load anymore

    def set_shotlist_status(self, loaded):
        pass  # No buttons to enable/disable

    def on_project_folder_loaded(self, project_folder):
        # Called when a new project folder is loaded
        self.clear_captions()

    def on_movie_loaded(self):
        if DEBUG: print("DEBUG: ProjectWindow: on_movie_loaded called")
        self.clear_captions()

    def clear_project(self):
        if DEBUG: print("DEBUG: ProjectWindow: clear_project called (no action needed)")
        self.clear_captions()

    def clear_captions(self):
        if DEBUG: print("DEBUG: ProjectWindow: clear_captions called (no action needed)")
        self.set_caption("movie", "shot", "")
        self.set_caption("movie", "scene", "")
        self.set_caption("play", "shot", "")
        self.set_caption("play", "scene", "")

    def submit_captions(self):
        movie_shot_text = self.get_caption("movie", "shot")
        movie_scene_text = self.get_caption("movie", "scene")
        play_shot_text = self.get_caption("play", "shot")
        play_scene_text = self.get_caption("play", "scene")

        self.captions_submitted.emit(movie_shot_text, movie_scene_text, play_shot_text, play_scene_text)

    def receive_captions(self, movie_shot, movie_scene, play_shot, play_scene):
        self.set_caption("movie", "shot", movie_shot)
        self.set_caption("movie", "scene", movie_scene)
        self.set_caption("play", "shot", play_shot)
        self.set_caption("play", "scene", play_scene)

    def set_caption(self, source, caption_type, text):
        """
        source: 'movie' or 'play'
        caption_type: 'shot' or 'scene'
        text: caption string
        """

        # Update the correct field
        if source == "movie" and caption_type == "shot":
            self.movie_shot_caption_field.setPlainText(text)
        elif source == "movie" and caption_type == "scene":
            self.movie_scene_caption_field.setPlainText(text)
        elif source == "play" and caption_type == "shot":
            self.play_shot_caption_field.setPlainText(text)
        elif source == "play" and caption_type == "scene":
            self.play_scene_caption_field.setPlainText(text)

    def get_caption(self, source, caption_type):
        """
        source: 'movie' or 'play'
        caption_type: 'shot' or 'scene'
        """
        if source == "movie" and caption_type == "shot":
            return self.movie_shot_caption_field.toPlainText()
        elif source == "movie" and caption_type == "scene":
            return self.movie_scene_caption_field.toPlainText()
        elif source == "play" and caption_type == "shot":
            return self.play_shot_caption_field.toPlainText()
        elif source == "play" and caption_type == "scene":
            return self.play_scene_caption_field.toPlainText()
        return ""

    def _on_movie_shot_caption_edited(self):
        text = self.movie_shot_caption_field.toPlainText()
        self.caption_was_edited.emit("movie", "shot", text)

    def _on_movie_scene_caption_edited(self):
        text = self.movie_scene_caption_field.toPlainText()
        self.caption_was_edited.emit("movie", "scene", text)

    def _on_play_shot_caption_edited(self):
        text = self.play_shot_caption_field.toPlainText()
        self.caption_was_edited.emit("play", "shot", text)

    def _on_play_scene_caption_edited(self):
        text = self.play_scene_caption_field.toPlainText()
        self.caption_was_edited.emit("play", "scene", text)

    # Example usage:
    # self.set_caption("movie", "shot", "This is a movie shot caption")
    # self.set_caption("play", "scene", "This is a play scene caption")
