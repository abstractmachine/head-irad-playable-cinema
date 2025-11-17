DEBUG = False  # Set to True to enable debug output

from PyQt5.QtCore import Qt, pyqtSignal
from PyQt5.QtWidgets import (
    QGridLayout, QWidget, QTextEdit
)

from .prompt import parse_system_prompt_files
from .utility import DARK_DOCK_BORDER, LIGHT_DOCK_BORDER

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

        self.set_text_size(self.ui.main_text_size)

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

    def set_text_size(self, new_size):
        self.movie_shot_caption_field.setFontPointSize(new_size)
        self.movie_scene_caption_field.setFontPointSize(new_size)
        self.play_shot_caption_field.setFontPointSize(new_size)
        self.play_scene_caption_field.setFontPointSize(new_size)
        # reset text with new text sizes
        self.movie_shot_caption_field.setPlainText(self.movie_shot_caption_field.toPlainText())
        self.movie_scene_caption_field.setPlainText(self.movie_scene_caption_field.toPlainText())
        self.play_shot_caption_field.setPlainText(self.play_shot_caption_field.toPlainText())
        self.play_scene_caption_field.setPlainText(self.play_scene_caption_field.toPlainText())

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
