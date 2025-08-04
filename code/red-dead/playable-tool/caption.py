DEBUG = True  # Set to True to enable debug output

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

# Import the new function from prompt.py instead of using the old one
from prompt import parse_system_prompt_files

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

        # Vertical button layout on the right, no margins, 0 spacing between buttons
        button_layout = QVBoxLayout()
        button_layout.setContentsMargins(0, 0, 0, 0)
        button_layout.setSpacing(0)

        button_width, button_height = self.ui.get_dimensions('button')
        tiny_width, tiny_height = self.ui.get_dimensions('tiny')

        # System dropdown (now larger and first)
        self.prompt_type_dropdown = QComboBox()
        self.prompt_type_dropdown.addItems(["System", "Shot", "Scene", "Gameplay"])
        self.prompt_type_dropdown.setCurrentIndex(0)
        self.prompt_type_dropdown.setFont(self.ui.get_font('tiny-condensed'))
        self.prompt_type_dropdown.setFixedSize(115, button_height)
        self.prompt_type_dropdown.setStyleSheet("QComboBox { padding: 0px 0px 0px 2.5em; margin: 0px 0px 0px 4px; }")
        button_layout.addWidget(self.prompt_type_dropdown)

        self.annotate_button = QPushButton("Annotate")
        self.annotate_button.setEnabled(False)
        self.annotate_button.setFont(self.ui.get_font('button'))
        self.annotate_button.setFixedSize(115, button_height)
        self.annotate_button.setStyleSheet("QPushButton { margin: 5px 0px 0px 4px; }")
        self.annotate_button.setToolTip("Rewrite current caption into current 'Caption' cell\nShortcut: A")
        button_layout.addWidget(self.annotate_button)

        # Frame count label and field in a horizontal layout
        frame_count_row = QHBoxLayout()
        frame_count_row.setContentsMargins(0, 0, 0, 0)
        frame_count_row.setSpacing(0)

        # API button (now smaller and second)
        self.api_button = QPushButton("API")
        self.api_button.setEnabled(False)
        self.api_button.setFont(self.ui.get_font('tiny'))
        self.api_button.setFixedSize(80, button_height)
        self.api_button.setToolTip("Send current shot to AI API and receive a caption\nShortcut: O")
        self.api_button.setStyleSheet("QPushButton { margin: 0px 4px 0px 0px; }")
        
        frame_count_row.addWidget(self.api_button)

        self.bot_button = QPushButton("Caption Bot Off")
        self.bot_button.setEnabled(False)
        self.bot_button.setFont(self.ui.get_font('button'))
        self.bot_button.setFixedSize(120, button_height)
        button_layout.addWidget(self.bot_button)

        self.frame_count_field = QLineEdit("5")
        self.frame_count_field.setFont(self.ui.get_font('tiny'))
        self.frame_count_field.setFixedSize(24, 24)
        self.frame_count_field.setAlignment(Qt.AlignCenter)
        self.frame_count_field.setToolTip("Number of image frames to send to API (0 = none)")
        self.frame_count_field.editingFinished.connect(self.validate_frame_count)
        self.frame_count_field.setStyleSheet("QLineEdit { margin: 0px 0px 0px 0px; }")
        self.frame_count_field.setFocusPolicy(Qt.ClickFocus)
        frame_count_row.addWidget(self.frame_count_field)

        button_layout.addLayout(frame_count_row)

        # Replace separate Previous and Next buttons with a double button
        nav_button_layout = QHBoxLayout()
        nav_button_layout.setContentsMargins(0, 0, 0, 0)
        nav_button_layout.setSpacing(0)  # No space between buttons

        self.previous_button = QPushButton("▲")
        self.previous_button.setEnabled(False)
        self.previous_button.setFont(self.ui.get_font('button'))
        self.previous_button.setFixedSize(52, button_height)  # Half width
        self.previous_button.setToolTip("Jump to previous shot")

        self.next_button = QPushButton("▼")
        self.next_button.setEnabled(False)
        self.next_button.setFont(self.ui.get_font('button'))
        self.next_button.setFixedSize(52, button_height)  # Half width
        self.next_button.setToolTip("Jump to next shot")

        # Style to make them look like one button
        self.previous_button.setStyleSheet("""
            QPushButton {
                margin: 5px 0px 0px 0px;
                border: none;
                border-top-right-radius: 0px;
                border-bottom-right-radius: 0px;
                border-right: 0px;
            }
        """)

        self.next_button.setStyleSheet("""
            QPushButton {
                margin: 5px 0px 0px 0px;
                border: none;
                border-top-left-radius: 0px;
                border-bottom-left-radius: 0px;
                border-left: 0px;
            }
        """)

        nav_button_layout.addWidget(self.previous_button)
        nav_button_layout.addWidget(self.next_button)
        button_layout.addLayout(nav_button_layout)

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
        self.is_first_row = True  # Add this line
        self.api_running = False
        self.project_folder = None
        self.current_movie_filename = None

        # New attribute to control auto-start behavior
        self.auto_start_bot = False

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

    def set_shotlist_status(self, loaded):
        if DEBUG: print(f"DEBUG: set_shotlist_status called, loaded={loaded}, auto_start_bot={self.auto_start_bot}, bot_active={self.bot_active}")
        self.shotlist_loaded = loaded
        self.api_button.setEnabled(loaded)
        self.annotate_button.setEnabled(loaded)
        self.next_button.setEnabled(loaded)
        self.previous_button.setEnabled(loaded)
        self.bot_button.setEnabled(loaded)
        if loaded and self.auto_start_bot and self.bot_active:
            if DEBUG: print("DEBUG: Auto-starting bot after shotlist loaded")
            self.auto_start_bot = False
            self.jump_to_first_available_shot()  # <-- Ensure valid shot selection
            QTimer.singleShot(100, self.start_bot_loop)  # Start bot after jump

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
            self.api_button.setText("API")
        for btn in [self.annotate_button, self.api_button]:
            btn.setEnabled(True)
        self.next_button.setEnabled(not self.is_last_row)
        self.previous_button.setEnabled(not self.is_first_row)
        self.caption_field.setPlainText(result)
        if DEBUG: print("DEBUG: About to call handle_bot_after_api_result")
        self.handle_bot_after_api_result()

    def handle_bot_after_api_result(self):
        if DEBUG: print(f"DEBUG: handle_bot_after_api_result called - bot_active={self.bot_active}")
        if self.bot_active:
            self.annotate_button.click()
            if not self.is_last_row:
                self.next_button.click()
            else:
                if DEBUG: print("DEBUG: Bot reached last row, moving to next movie")
                self.auto_start_bot = True  # <-- Add this line
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
        self.api_button.setText("API")
        for btn in [self.annotate_button, self.api_button]:
            btn.setEnabled(True)
        self.next_button.setEnabled(not self.is_last_row)
        self.previous_button.setEnabled(not self.is_first_row)
        
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

    def enable_auto_start_bot(self):
        if DEBUG: print(f"DEBUG: enable_auto_start_bot called, bot_active={self.bot_active}")
        self.auto_start_bot = True

    def toggle_bot(self):
        if DEBUG: print(f"DEBUG: toggle_bot called, bot_active={self.bot_active}")
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
        if not self.shotlist_loaded:
            if DEBUG: print("DEBUG: Bot waiting for shotlist to load")
            self.auto_start_bot = True
            return
        if DEBUG: print(f"DEBUG: start_bot_loop called - bot_active={self.bot_active}, api_running={self.api_running}")
        if not self.bot_active or self.api_running:
            if DEBUG: print("DEBUG: Bot loop aborted - bot not active or API running")
            return
        # Step 2: Press API button
        if DEBUG: print("DEBUG: Bot clicking API button")
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

    def handle_is_first_available_shot(self, is_first):
        """Handle whether this is the first available shot"""
        if DEBUG: print(f"DEBUG: handle_is_first_available_shot called - is_first={is_first}")
        self.is_first_row = is_first
        self.previous_button.setEnabled(not is_first and not self.api_running)

    def validate_frame_count(self):
        """Validate frame count input"""
        try:
            value = int(self.frame_count_field.text())
            if value < 0:
                self.frame_count_field.setText("0")
        except ValueError:
            self.frame_count_field.setText("5")  # Reset to default if invalid

    def jump_to_first_available_shot(self):
        # Emit a signal to ShotlistWindow to jump to the first available shot
        self.request_current_shot.emit(int(self.frame_count_field.text()))