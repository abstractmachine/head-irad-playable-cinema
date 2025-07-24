import os
import tempfile
import base64
import mimetypes
import openai

from PyQt5.QtGui import QFont, QFontDatabase, QTextOption
from PyQt5.QtCore import Qt, QThread, pyqtSignal, QObject, QTimer
from PyQt5.QtWidgets import (
    QMainWindow, QVBoxLayout, QHBoxLayout, QWidget, QLineEdit, QTextEdit, QPushButton, QSizePolicy
)

FRAMES_PER_SHOT = 5

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

    def __init__(self, frames):
        super().__init__()
        self.frames = frames

    def run(self):
        # print(f"API Worker received {len(self.frames)} frames")
        images_payload = []
        for frame in self.frames:
            images_payload.append({
                "type": "image_url",
                "image_url": {
                    "url": encode_image(frame),
                    "detail": "high"
                }
            })

        # Read system prompt
        system_prompt_path = os.path.join(os.path.dirname(__file__), "preferences", "system_prompt.txt")
        try:
            with open(system_prompt_path, "r", encoding="utf-8") as f:
                system_prompt = f.read()
        except Exception:
            system_prompt = "Describe the scene in these images."

        # Read API key from file
        api_key_path = os.path.join(os.path.dirname(__file__), "preferences", "api_key.txt")
        try:
            with open(api_key_path, "r", encoding="utf-8") as f:
                api_key = f.read().strip()
        except Exception as e:
            caption = f"Error reading API key: {e}"
            self.result.emit(caption)
            self.finished.emit()
            return

        messages = [
            {"role": "system", "content": system_prompt},
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

class AnnotateWindow(QMainWindow):
    request_save = pyqtSignal()
    request_load = pyqtSignal(dict)
    caption_submitted = pyqtSignal(str)
    request_current_shot = pyqtSignal(int)
    request_next_shot = pyqtSignal()

    def __init__(self):
        super().__init__()

        self.setWindowTitle("Annotate")
        self.setGeometry(400, 200, 600, 350)

        main_layout = QVBoxLayout()
        main_layout.setContentsMargins(10, 10, 10, 10)
        main_layout.setSpacing(10)

        # Caption field (multi-line widget, but we block newlines)
        self.caption_field = QTextEdit()
        self.caption_field.setPlaceholderText("")
        self.caption_field.setFont(QFont("Helvetica", 18))
        self.caption_field.setStyleSheet("QTextEdit { border: none; }")
        self.caption_field.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.caption_field.setWordWrapMode(QTextOption.WordWrap)
        main_layout.addWidget(self.caption_field, stretch=1)

        # Row of buttons (fixed height)
        button_layout = QHBoxLayout()
        button_layout.setSpacing(10)
        button_layout.addStretch()

        self.annotate_button = QPushButton("Annotate")
        self.annotate_button.setFixedWidth(100)
        self.annotate_button.setEnabled(False)
        self.annotate_button.setToolTip("Rewrite current caption into current 'Caption' cell\nShortcut: A")
        button_layout.addWidget(self.annotate_button)

        self.next_button = QPushButton("Next")
        self.next_button.setFixedWidth(100)
        self.next_button.setEnabled(False)
        self.next_button.setToolTip("Jump to next shot")
        button_layout.addWidget(self.next_button)

        self.api_button = QPushButton("OpenAI")
        self.api_button.setFixedWidth(100)
        self.api_button.setEnabled(False)
        self.api_button.setToolTip("Send current shot to OpenAI API and receive a caption\nShortcut: O")
        button_layout.addWidget(self.api_button)

        self.bot_button = QPushButton("Bot Off")
        self.bot_button.setFixedWidth(100)
        self.bot_button.setEnabled(False)
        button_layout.addWidget(self.bot_button)

        button_layout.addStretch()
        button_row_widget = QWidget()
        button_row_widget.setLayout(button_layout)
        main_layout.addWidget(button_row_widget, stretch=0)

        # System prompt field (2/3 of window)
        self.system_prompt_field = SystemPromptEdit(save_callback=self.save_system_prompt)
        self.system_prompt_field.setPlainText("Enter system prompt here...")
        font_path = os.path.join(os.path.dirname(__file__), "ui/fonts/HKGrotesk-Regular.otf")
        font_id = QFontDatabase.addApplicationFont(font_path)
        font_families = QFontDatabase.applicationFontFamilies(font_id)
        if font_families:
            hk_font_caption = QFont(font_families[0], 18)
            hk_font_system = QFont(font_families[0], 12)
        else:
            hk_font_caption = QFont("Helvetica", 18)
            hk_font_system = QFont("Helvetica", 12)

        self.caption_field.setFont(hk_font_caption)
        self.system_prompt_field.setFont(hk_font_system)
        self.system_prompt_field.setStyleSheet("QTextEdit { border: none; }")
        self.system_prompt_field.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        main_layout.addWidget(self.system_prompt_field, stretch=1)

        container = QWidget()
        container.setLayout(main_layout)
        self.setCentralWidget(container)

        # System prompt file path
        self.system_prompt_path = os.path.join(
            os.path.dirname(__file__),
            "preferences",
            "system_prompt.txt"
        )
        self.load_system_prompt()

        # Save system prompt only when focus is lost
        # self.system_prompt_field.installEventFilter(self)

        # Install event filter for global key press handling
        self.installEventFilter(self)

        # Ensure main window has focus at startup
        self.setFocus()

        self.ignore_next_enter = False

        self.setFocusPolicy(Qt.StrongFocus)

        self.annotate_button.clicked.connect(self.submit_caption)
        self.api_button.clicked.connect(self.handle_api_button)

        # Initialize current_timecodes
        self.current_timecodes = []

        self.next_button.clicked.connect(self.handle_next_button)

        # Bot functionality
        self.bot_active = False
        self.bot_anim_timer = QTimer(self)
        self.bot_anim_timer.timeout.connect(self.animate_bot_button)
        self.bot_anim_dots = 0

        self.bot_button.clicked.connect(self.toggle_bot)

        # Allows us to know if we are at the last active row in the shotlist
        self.is_last_row = False

        self.api_running = False

    def eventFilter(self, obj, event):

        # Save system prompt on focus out
        if obj is self.system_prompt_field and event.type() == event.FocusOut:
            self.save_system_prompt()
            return False

        # Global ENTER shortcut: only when NOT editing system prompt or caption field
        if event.type() == event.KeyPress and event.key() in (Qt.Key_Return, Qt.Key_Enter):
            if self.ignore_next_enter:
                self.ignore_next_enter = False
                return True
            if self.system_prompt_field.hasFocus() or self.caption_field.hasFocus():
                return False
            self.caption_field.setFocus()
            self.caption_field.selectAll()
            return True

        return super().eventFilter(obj, event)

    def keyPressEvent(self, event):
        # Only handle hotkeys, not ENTER
        key = event.key()
        if key == Qt.Key_A:
            self.annotate_button.click()
        elif key == Qt.Key_O:
            self.api_button.click()
        elif key == Qt.Key_B:
            self.bot_button.click()
        elif key == Qt.Key_N:
            self.next_button.click()
        else:
            super().keyPressEvent(event)

    def exit_caption_editing(self):
        self.activateWindow()  # Bring window to front
        self.centralWidget().setFocus()  # Set focus to main widget
        self.ignore_next_enter = True

    def load_system_prompt(self):
        try:
            with open(self.system_prompt_path, "r", encoding="utf-8") as f:
                text = f.read()
            self.system_prompt_field.setPlainText(text)
            self.system_prompt = text
        except Exception as e:
            self.system_prompt_field.setPlainText("Enter system prompt here...")
            self.system_prompt = "Enter system prompt here..."

    def save_system_prompt(self):
        text = self.system_prompt_field.toPlainText()
        self.system_prompt = text
        try:
            with open(self.system_prompt_path, "w", encoding="utf-8") as f:
                f.write(text)
        except Exception as e:
            print(f"Error saving system prompt: {e}")

    def on_request_save(self):
        geo = self.geometry()
        self._pending_save_data = {
            "x": geo.x(),
            "y": geo.y(),
            "width": geo.width(),
            "height": geo.height()
        }
        return self._pending_save_data

    def on_request_load(self, data):
        if data:
            x = data.get("x", 400)
            y = data.get("y", 200)
            w = data.get("width", 400)
            h = data.get("height", 150)
            self.setGeometry(x, y, w, h)

    def set_shotlist_status(self, exists):
        self.annotate_button.setEnabled(exists)
        self.api_button.setEnabled(exists)
        self.bot_button.setEnabled(exists)
        self.next_button.setEnabled(exists)

    def submit_caption(self):
        text = self.caption_field.toPlainText()
        # print("Annotate button pressed, submitting caption:", text)
        self.caption_submitted.emit(text)

    def handle_api_frames(self, frames):
        # print("handle_api_frames called")
        self.current_frames = frames

        self.api_thread = QThread()
        self.api_worker = ApiWorker(self.current_frames)  # Pass frames instead of timecodes
        self.api_worker.moveToThread(self.api_thread)
        self.api_thread.started.connect(self.api_worker.run)
        self.api_worker.result.connect(self.handle_api_result)
        self.api_worker.finished.connect(self.api_thread.quit)
        self.api_worker.finished.connect(self.api_worker.deleteLater)
        self.api_thread.finished.connect(self.api_thread.deleteLater)
        self.api_thread.start()

    def handle_api_button(self):
        self.api_running = True
        self.caption_field.clear()
        self.api_button.setText("")
        # Do NOT disable self.bot_button here!
        for btn in [self.annotate_button, self.api_button, self.next_button]:
            btn.setEnabled(False)

        self.request_current_shot.emit(FRAMES_PER_SHOT)
        self.api_anim_step = 0
        self.api_anim_timer = QTimer(self)
        self.api_anim_timer.timeout.connect(self.animate_api_button)
        self.api_anim_timer.start(400)  # update every 400ms

    def animate_api_button(self):
        dots = '.' * (self.api_anim_step % 4)
        self.api_button.setText(f"{dots}")
        self.api_anim_step += 1

    def handle_api_result(self, result):
        self.api_running = False
        if hasattr(self, 'api_anim_timer'):
            self.api_anim_timer.stop()
            self.api_button.setText("OpenAI")
        for btn in [self.annotate_button, self.api_button]:
            btn.setEnabled(True)
        self.next_button.setEnabled(not self.is_last_row)
        self.caption_field.setPlainText(result)
        self.handle_bot_after_api_result()

    def handle_bot_after_api_result(self):
        if self.bot_active:
            self.annotate_button.click()
            if not self.is_last_row:
                self.next_button.click()
            else:
                self.stop_bot()

    def handle_api_abort(self, message):
        self.api_running = False
        if hasattr(self, 'api_anim_timer'):
            self.api_anim_timer.stop()
            self.api_button.setText("OpenAI")
        for btn in [self.annotate_button, self.api_button]:
            btn.setEnabled(True)
        self.next_button.setEnabled(not self.is_last_row)
        self.api_button.setText("OpenAI")
        self.caption_field.setPlainText(f"API aborted: {message}")

    def set_caption_field(self, caption):
        self.caption_field.setPlainText(caption)

    def handle_next_button(self):
        self.request_next_shot.emit()

    def toggle_bot(self):
        if not self.bot_active:
            self.bot_active = True
            self.bot_button.setText("    Bot On")
            self.bot_button.setStyleSheet("text-align: left;")
            self.bot_anim_timer.start(500)
            self.start_bot_loop()
        else:
            self.stop_bot()

    def stop_bot(self):
        self.bot_active = False
        self.bot_anim_timer.stop()
        self.bot_button.setText("Bot Off")
        self.bot_button.setStyleSheet("text-align: center;")

    def start_bot_loop(self):
        if not self.bot_active or self.api_running:
            return
        # Step 2: Press OpenAI button
        self.api_button.click()

    def animate_bot_button(self):
        self.bot_anim_dots = (self.bot_anim_dots + 1) % 4
        self.bot_button.setText("    Bot On" + "." * self.bot_anim_dots)
        self.bot_button.setStyleSheet("text-align: left;")

    def handle_is_last_available_shot(self, is_last):
        self.is_last_row = is_last
        self.next_button.setEnabled(not is_last and not self.api_running)

        # Bot loop logic
        if self.bot_active and not self.api_running:
            QTimer.singleShot(100, self.start_bot_loop)
