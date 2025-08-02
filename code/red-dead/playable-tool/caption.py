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
    QMainWindow, QVBoxLayout, QHBoxLayout, QWidget, QLineEdit, QTextEdit, QPushButton, QSizePolicy, QLabel
)

def parse_system_prompt(prompt_text, data, row_data=None, subtitles=None):
    """
    Replace tags like {title} or {image-count} in prompt_text with values from data dict.
    If a tag is missing, insert OBVIOUS DUMMY TEXT.
    """

    def replace_tag(match):
        tag = match.group(1)
        if tag in data:
            return str(data[tag])
        # if the tag is {shot-subtitles}
        elif tag == "shot-subtitles":
            # get shot subtitles from data
            subtitles_output = ""
            if row_data:
                timecode_start = row_data['Start']
                timecode_end = row_data['End']
                # subtitles_output += timecode_start + " - " + timecode_end + "\n"
                subtitles_output += subtitles.get_subtitles_between(timecode_start, timecode_end)
            else:
                subtitles_output = ""
            return subtitles_output
        else:
            return f"{tag}"

    return re.sub(r"\{([a-zA-Z0-9_-]+)\}", replace_tag, prompt_text)

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
        self.subtitles_window = subtitles_window  # Store reference to subtitles window
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

        # Read system prompt from movie-specific file if available, otherwise fallback
        system_prompt = self._get_shot_prompt()
        parsed_prompt = parse_system_prompt(system_prompt, self.metadata, self.row_data, self.subtitles_window)
        
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

    def _get_shot_prompt(self):
        """Get shot prompt, preferring movie-specific prompt over fallback"""

        # Fallback to global system prompt
        shot_prompt_path = os.path.join(os.path.dirname(__file__), "preferences", "shot_prompt.txt")
        try:
            with open(shot_prompt_path, "r", encoding="utf-8") as f:
                return f.read()
        except Exception:
            return "Describe the scene in these images."

class SystemPromptEdit(QTextEdit):
    def __init__(self, parent=None, save_callback=None):
        super().__init__(parent)
        self.save_callback = save_callback

    def focusOutEvent(self, event):
        if self.save_callback:
            self.save_callback()
        super().focusOutEvent(event)

class CaptionWindow(QWidget):
    request_save = pyqtSignal()
    request_load = pyqtSignal(dict)
    shot_caption_submitted = pyqtSignal(str)
    request_current_shot = pyqtSignal(int)
    request_next_shot = pyqtSignal()
    request_previous_shot = pyqtSignal()
    shot_bot_finished = pyqtSignal()

    def __init__(self, ui, subtitles_window):
        super().__init__()
        self.ui = ui  # Store UI instance
        self.subtitles_window = subtitles_window  # Store reference to subtitles window

        self.setMinimumHeight(80)

        # Main horizontal layout, no margins or spacing
        main_layout = QHBoxLayout()
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(0)

        # Caption field (multi-line widget) on the left
        self.caption_field = QTextEdit()
        self.caption_field.setPlaceholderText("")
        self.caption_field.setFont(self.ui.get_font('text'))
        self.caption_field.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.caption_field.setWordWrapMode(QTextOption.WordWrap)
        main_layout.addWidget(self.caption_field, stretch=1)

        # Vertical button layout on the right, no margins, 2px spacing between buttons
        button_layout = QVBoxLayout()
        button_layout.setContentsMargins(0, 0, 0, 0)
        button_layout.setSpacing(0)

        button_width, button_height = self.ui.get_dimensions('button')
        tiny_width, tiny_height = self.ui.get_dimensions('tiny')

        self.annotate_button = QPushButton("Annotate")
        self.annotate_button.setEnabled(False)
        self.annotate_button.setFont(self.ui.get_font('button'))
        self.annotate_button.setFixedSize(120, button_height)
        self.annotate_button.setToolTip("Rewrite current caption into current 'Caption' cell\nShortcut: A")
        button_layout.addWidget(self.annotate_button)

        self.previous_button = QPushButton("Previous")
        self.previous_button.setEnabled(False)
        self.previous_button.setFont(self.ui.get_font('button'))
        self.previous_button.setFixedSize(120, button_height)
        self.previous_button.setToolTip("Jump to previous shot")
        button_layout.addWidget(self.previous_button)

        self.next_button = QPushButton("Next")
        self.next_button.setEnabled(False)
        self.next_button.setFont(self.ui.get_font('button'))
        self.next_button.setFixedSize(120, button_height)
        self.next_button.setToolTip("Jump to next shot")
        button_layout.addWidget(self.next_button)

        self.api_button = QPushButton("OpenAI")
        self.api_button.setEnabled(False)
        self.api_button.setFont(self.ui.get_font('button'))
        self.api_button.setFixedSize(120, button_height)
        self.api_button.setToolTip("Send current shot to OpenAI API and receive a caption\nShortcut: O")
        button_layout.addWidget(self.api_button)

        # Frame count label and field in a horizontal layout
        frame_count_row = QHBoxLayout()
        frame_count_row.setContentsMargins(0, 0, 0, 0)
        frame_count_row.setSpacing(0)

        frame_count_label = QLabel(" Frames")
        frame_count_label.setFont(self.ui.get_font('tiny-condensed'))
        frame_count_label.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        frame_count_label.setFixedSize(70, tiny_height)
        frame_count_label.setStyleSheet("QLabel { color: #888; margin-top: 4px; }")

        frame_count_row.addWidget(frame_count_label)

        self.bot_button = QPushButton("Caption Bot Off")
        self.bot_button.setEnabled(False)
        self.bot_button.setFont(self.ui.get_font('button'))
        self.bot_button.setFixedSize(120, button_height)
        button_layout.addWidget(self.bot_button)

        self.frame_count_field = QLineEdit("5")
        self.frame_count_field.setFont(self.ui.get_font('tiny'))
        self.frame_count_field.setFixedSize(40, tiny_height)
        self.frame_count_field.setAlignment(Qt.AlignCenter)
        self.frame_count_field.setToolTip("Number of frames to send to OpenAI (0 = none)")
        self.frame_count_field.editingFinished.connect(self.validate_frame_count)
        self.frame_count_field.setStyleSheet("QLineEdit { margin-top: 2px; }")  # 2px top margin
        frame_count_row.addWidget(self.frame_count_field)

        button_layout.addLayout(frame_count_row)

        button_layout.addStretch()
        main_layout.addLayout(button_layout, stretch=0)

        self.setLayout(main_layout)

        # Ensure main window has focus at startup
        self.setFocus()
        self.setFocusPolicy(Qt.StrongFocus)

        self.annotate_button.clicked.connect(self.submit_caption)
        self.api_button.clicked.connect(self.handle_api_button)

        self.current_timecodes = []
        self.next_button.clicked.connect(self.handle_next_button)
        self.previous_button.clicked.connect(self.handle_previous_button)

        # Bot functionality
        self.bot_active = False
        self.bot_anim_timer = QTimer(self)
        self.bot_anim_timer.timeout.connect(self.animate_bot_button)
        self.bot_anim_dots = 0

        self.bot_button.clicked.connect(self.toggle_bot)

        self.is_last_row = False
        self.api_running = False
        self.project_folder = None
        self.current_movie_filename = None

    def keyPressEvent(self, event):
        # Only handle hotkeys, not ENTER
        key = event.key()
        if key == Qt.Key_A:
            self.annotate_button.click()
        elif key == Qt.Key_O:
            self.api_button.click()
        elif key == Qt.Key_B:
            self.bot_button.click()
        elif key == Qt.Key_Up:
            self.previous_button.click()
        elif key == Qt.Key_Down:
            self.next_button.click()
        else:
            super().keyPressEvent(event)

    def on_request_save(self):
        geo = self.geometry()
        self._pending_save_data = {
            "frame_count": self.frame_count_field.text()  # Save frame count
        }
        return self._pending_save_data

    def on_request_load(self, data):
        if data:
            # Load frame count
            if "frame_count" in data:
                self.frame_count_field.setText(data["frame_count"])

    def set_shotlist_status(self, exists):
        self.annotate_button.setEnabled(exists)
        self.api_button.setEnabled(exists)
        self.bot_button.setEnabled(exists)
        self.next_button.setEnabled(exists)
        self.previous_button.setEnabled(exists)

    def submit_caption(self):
        if DEBUG: print("DEBUG: submit_caption called")
        text = self.caption_field.toPlainText()
        self.shot_caption_submitted.emit(text)

    def set_project_folder(self, project_folder):
        """Set the project folder when cinema window loads a project"""
        self.project_folder = project_folder
        self.caption_field.clear()  # Clear the text field when project_folder is set

    def on_movie_loaded_with_metadata(self, movie_path, metadata):
        """Called when a new movie is loaded in the player"""
        movie_filename = os.path.basename(movie_path)
        self.metadata = metadata
        self.current_movie_filename = movie_filename

    def handle_row_data(self, row_data):
        """Handle row data emitted from shotlist."""
        self.row_data = row_data
        if DEBUG: print(f"DEBUG Caption: Received row data: {row_data}")

    def handle_api_frames(self, frames):
        self.current_frames = frames

        self.api_thread = QThread()
        # Pass project info to ApiWorker
        self.api_worker = ApiWorker(
            self.current_frames,
            self.metadata,
            self.row_data,
            self.subtitles_window,
            self.project_folder,
            self.current_movie_filename
        )
        self.api_worker.moveToThread(self.api_thread)
        self.api_thread.started.connect(self.api_worker.run)
        self.api_worker.result.connect(self.handle_api_result)
        self.api_worker.finished.connect(self.api_thread.quit)
        self.api_worker.finished.connect(self.api_worker.deleteLater)
        self.api_thread.finished.connect(self.api_thread.deleteLater)
        self.api_thread.start()

    def handle_api_button(self):        
        # Stop any existing animation timer first
        if hasattr(self, 'api_anim_timer') and self.api_anim_timer is not None:
            self.api_anim_timer.stop()
            self.api_anim_timer.deleteLater()
            self.api_anim_timer = None
        
        self.api_running = True
        self.caption_field.clear()
        self.api_button.setText("")
        # Do NOT disable self.bot_button here!
        for btn in [self.annotate_button, self.api_button, self.next_button, self.previous_button]:
            btn.setEnabled(False)

        # Get frame count from input field
        try:
            frame_count = int(self.frame_count_field.text())
        except ValueError:
            frame_count = 5  # Fallback to default
            
        self.request_current_shot.emit(frame_count)
        
        # Use QTimer.singleShot to check if API is still running after signal processing
        QTimer.singleShot(100, self.start_api_animation_if_still_running)

    def start_api_animation_if_still_running(self):
        """Start animation only if API is still running (wasn't aborted)"""
        if self.api_running:
            self.api_anim_step = 0
            self.api_anim_timer = QTimer(self)
            self.api_anim_timer.timeout.connect(self.animate_api_button)
            self.api_anim_timer.start(400)  # update every 400ms

    def animate_api_button(self):
        dots = '.' * (self.api_anim_step % 4)
        self.api_button.setText(f"{dots}")
        self.api_anim_step += 1

    def handle_api_result(self, result):
        if DEBUG: print("DEBUG: handle_api_result called")
        self.api_running = False
        if hasattr(self, 'api_anim_timer'):
            self.api_anim_timer.stop()
            self.api_button.setText("OpenAI")
        for btn in [self.annotate_button, self.api_button]:
            btn.setEnabled(True)
        self.next_button.setEnabled(not self.is_last_row)
        self.caption_field.setPlainText(result)
        if DEBUG: print("DEBUG: About to call handle_bot_after_api_result")
        self.handle_bot_after_api_result()

    def handle_bot_after_api_result(self):
        if DEBUG: print(f"DEBUG: handle_bot_after_api_result called - bot_active={self.bot_active}")
        if self.bot_active:
            if DEBUG: print("DEBUG: Bot clicking Annotate button")
            self.annotate_button.click()
            if not self.is_last_row:
                if DEBUG: print("DEBUG: Bot clicking Next button")
                self.next_button.click()
            else:
                if DEBUG: print("DEBUG: Bot reached last row, stopping")
                self.stop_bot()
                self.handle_bot_finished()

    def handle_api_abort(self, message):
        self.api_running = False
        
        # Stop the animation timer MORE AGGRESSIVELY
        if hasattr(self, 'api_anim_timer') and self.api_anim_timer is not None:
            try:
                self.api_anim_timer.stop()
                self.api_anim_timer.deleteLater()
            except Exception as e:
                pass
            finally:
                self.api_anim_timer = None
            
        # Reset button text and enable buttons
        self.api_button.setText("OpenAI")
        for btn in [self.annotate_button, self.api_button]:
            btn.setEnabled(True)
        self.next_button.setEnabled(not self.is_last_row)
        
        # Show error message in caption field
        self.caption_field.setPlainText(f"API aborted: {message}")

    def handle_bot_finished(self):
        if DEBUG: print("DEBUG: handle_bot_finished called")
        self.shot_bot_finished.emit()

    def set_shot_caption_field(self, caption):
        self.caption_field.setPlainText(caption)

    def handle_next_button(self):
        if DEBUG: print("DEBUG: handle_next_button called")
        self.request_next_shot.emit()

    def handle_previous_button(self):
        if DEBUG: print("DEBUG: handle_previous_button called")
        self.request_previous_shot.emit()

    def toggle_bot(self):
        if DEBUG: print("DEBUG: toggle_bot called")
        if not self.bot_active:
            if DEBUG: print("DEBUG: Starting bot")
            self.bot_active = True
            self.bot_button.setText("    Caption Bot On")
            self.bot_button.setStyleSheet("text-align: left;")
            self.bot_anim_timer.start(500)
            self.start_bot_loop()
        else:
            if DEBUG: print("DEBUG: Stopping bot")
            self.stop_bot()

    def stop_bot(self):
        if DEBUG: print("DEBUG: stop_bot called")
        self.bot_active = False
        self.bot_anim_timer.stop()
        self.bot_button.setText("Caption Bot Off")
        self.bot_button.setStyleSheet("text-align: center;")

    def start_bot_loop(self):
        if DEBUG: print(f"DEBUG: start_bot_loop called - bot_active={self.bot_active}, api_running={self.api_running}")
        if not self.bot_active or self.api_running:
            if DEBUG: print("DEBUG: Bot loop aborted - bot not active or API running")
            return
        # Step 2: Press OpenAI button
        if DEBUG: print("DEBUG: Bot clicking OpenAI button")
        self.api_button.click()

    def animate_bot_button(self):
        self.bot_anim_dots = (self.bot_anim_dots + 1) % 4
        self.bot_button.setText("    Caption Bot On" + "." * self.bot_anim_dots)
        self.bot_button.setStyleSheet("text-align: left;")

    def handle_is_last_available_shot(self, is_last):
        if DEBUG: print(f"DEBUG: handle_is_last_available_shot called - is_last={is_last}, bot_active={self.bot_active}, api_running={self.api_running}")
        self.is_last_row = is_last
        self.next_button.setEnabled(not is_last and not self.api_running)

        # Bot loop logic
        if self.bot_active and not self.api_running:
            if DEBUG: print("DEBUG: Bot continuing loop via handle_is_last_available_shot")
            QTimer.singleShot(100, self.start_bot_loop)

    def validate_frame_count(self):
        """Validate frame count input"""
        try:
            value = int(self.frame_count_field.text())
            if value < 0:
                self.frame_count_field.setText("0")
        except ValueError:
            self.frame_count_field.setText("5")  # Reset to default if invalid