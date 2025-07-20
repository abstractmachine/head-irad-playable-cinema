from PyQt5.QtWidgets import (
    QMainWindow, QVBoxLayout, QHBoxLayout, QWidget, QLineEdit, QTextEdit, QPushButton, QSizePolicy
)
from PyQt5.QtGui import QFont, QFontDatabase, QTextOption
from PyQt5.QtCore import pyqtSignal, Qt
import os

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

    def __init__(self, player_window, detector_window):
        super().__init__()
        self.player_window = player_window
        self.detector_window = detector_window

        self.setWindowTitle("Annotate")
        self.setGeometry(400, 200, 600, 350)

        main_layout = QVBoxLayout()
        main_layout.setContentsMargins(10, 10, 10, 10)
        main_layout.setSpacing(10)

        # Caption field (multi-line widget, but we block newlines)
        self.caption_field = QTextEdit()
        self.caption_field.setPlaceholderText("...")
        self.caption_field.setFont(QFont("Helvetica", 18))
        self.caption_field.setStyleSheet("QTextEdit { border: none; color: black; }")
        self.caption_field.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.caption_field.setWordWrapMode(QTextOption.WordWrap)
        main_layout.addWidget(self.caption_field, stretch=1)

        # Row of buttons (fixed height)
        button_layout = QHBoxLayout()
        button_layout.setSpacing(10)
        button_layout.addStretch()

        self.annotate_button = QPushButton("Annotate")
        self.annotate_button.setFixedWidth(120)
        self.annotate_button.setEnabled(False)  # Disabled at startup
        button_layout.addWidget(self.annotate_button)

        self.test_button = QPushButton("API")
        self.test_button.setFixedWidth(120)
        self.test_button.setEnabled(False)  # Disabled at startup
        button_layout.addWidget(self.test_button)

        self.bot_button = QPushButton("Bot")
        self.bot_button.setFixedWidth(120)
        self.bot_button.setEnabled(False)  # Disabled at startup
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
            system_font = QFont(font_families[0], 12)
        else:
            system_font = QFont("Helvetica", 12)
        self.system_prompt_field.setFont(system_font)
        self.system_prompt_field.setStyleSheet("QTextEdit { border: none; color: black; }")
        self.system_prompt_field.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        main_layout.addWidget(self.system_prompt_field, stretch=2)

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

    def eventFilter(self, obj, event):
        # Block ENTER/newline in caption_field and exit editing instead
        if obj is self.caption_field and event.type() == event.KeyPress:
            if event.key() in (Qt.Key_Return, Qt.Key_Enter):
                self.exit_caption_editing()
                return True  # Block newline
            return False

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
        if key == Qt.Key_T:
            self.test_button.click()
        elif key == Qt.Key_A:
            self.annotate_button.click()
        elif key == Qt.Key_B:
            self.bot_button.click()
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
        self.test_button.setEnabled(exists)
        self.bot_button.setEnabled(exists)